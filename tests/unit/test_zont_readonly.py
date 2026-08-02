from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from zont_analyzer.adapters.zont_readonly.client import (
    ALLOWED_METHODS,
    ZontReadOnlyClient,
    decode_delta_time_array,
    redact,
)


def test_decode_delta_time_array_supports_delta_and_absolute_reset() -> None:
    decoded = decode_delta_time_array([[1_700_000_000, 20.0], [-60, 20.5], [1_700_000_180, 21]])
    assert [int(item[0].timestamp()) for item in decoded] == [1_700_000_000, 1_700_000_060, 1_700_000_180]
    assert [item[1] for item in decoded] == [20.0, 20.5, 21]


def test_decode_rejects_relative_first_row() -> None:
    with pytest.raises(ValueError, match="starts with"):
        decode_delta_time_array([[-60, 20]])


def test_client_can_only_call_audited_endpoints() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/devices"):
            return httpx.Response(200, json={"ok": True, "devices": [{"device_id": 7, "name": "Дом"}]})
        return httpx.Response(
            200,
            json={
                "responses": [
                    {
                        "ok": True,
                        "device_id": 7,
                        "z3k_temperature": {"4104": [[1_700_000_000, 21], [-60, 22]]},
                        "z3k_boiler_adapter": {"5000": {"s": [[1_700_000_000, ["ch", "fl"]], [-60, ["ch"]]]}},
                    }
                ]
            },
        )

    with ZontReadOnlyClient(
        token="secret",
        client_email="owner@example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.discover_devices()
        responses = client.load_history(
            device_ids=["7"],
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 1, 2, tzinfo=UTC),
            data_types=["z3k_temperature", "z3k_boiler_adapter"],
        )
        points, entities = client.normalize_history(responses[0])

    assert {"devices", "load_data"} == ALLOWED_METHODS
    assert requested == ["/api/devices", "/api/load_data"]
    assert len(points) == 6
    assert points[1].timestamp_utc.timestamp() - points[0].timestamp_utc.timestamp() == 60
    assert next(iter(entities.values()))["role"] == "temperature"
    flame = [point for point in points if point.metric_key == "flame"]
    assert [point.value_num for point in flame] == [1.0, 0.0]


def test_private_transport_rejects_non_allowlisted_method() -> None:
    client = ZontReadOnlyClient(
        token="secret", client_email="owner@example.test", transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    with pytest.raises(PermissionError):
        client._post_allowed("not_read_only", {})
    client.close()


def test_public_api_contains_no_mutating_or_generic_request_method() -> None:
    public = {name for name in dir(ZontReadOnlyClient) if not name.startswith("_")}
    assert public == {
        "close",
        "discover_devices",
        "healthcheck",
        "load_config_snapshot",
        "load_history",
        "normalize_history",
    }


def test_redaction_covers_nested_network_and_identity_fields() -> None:
    payload = {
        "wifi": {"pass": "wifi-secret", "netname": "private-ssid", "ip": "192.0.2.1"},
        "stationary_location": {"loc": [1, 2]},
        "iccid": {"value": "123"},
        "serial": "device-serial",
        "safe": {"temperature": 22.5},
    }
    cleaned = redact(payload)
    encoded = str(cleaned)
    assert "wifi-secret" not in encoded
    assert "private-ssid" not in encoded
    assert "192.0.2.1" not in encoded
    assert "device-serial" not in encoded
    assert cleaned["safe"]["temperature"] == 22.5


def test_history_retries_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error_ui": "rate limited"}, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"responses": []})

    monkeypatch.setattr("zont_analyzer.adapters.zont_readonly.client.time.sleep", lambda _delay: None)
    with ZontReadOnlyClient(
        token="secret",
        client_email="owner@example.test",
        transport=httpx.MockTransport(handler),
        history_request_interval_seconds=0,
    ) as client:
        assert client.load_history(
            device_ids=["7"],
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 1, 2, tzinfo=UTC),
            data_types=["temperature"],
        ) == []

    assert calls == 2

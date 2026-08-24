from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from zont_analyzer.adapters.zont_readonly.client import (
    ALLOWED_METHODS,
    ZontReadOnlyClient,
    decode_delta_time_array,
    redact,
)

CONTRACT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "zont_contract"


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

    assert {"devices", "load_data", "raw_events"} == ALLOWED_METHODS
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
        "load_events",
        "load_config_snapshot",
        "load_history",
        "normalize_events",
        "normalize_history",
    }


def test_reliability_events_are_privacy_minimized_and_stable() -> None:
    rows = [
        [
            "legacy-not-unique",
            1_700_000_000,
            "LossConnectionBoiler",
            43.1,
            56.2,
            None,
            {"object_id": 42, "object_name": "Котёл", "phone": "+70000000000"},
            True,
        ],
        ["ignored", 1_700_000_001, "OutSMS", 43.1, 56.2, None, {"phone": "+70000000000"}, False],
    ]

    first = ZontReadOnlyClient.normalize_events("7", rows)
    second = ZontReadOnlyClient.normalize_events("7", rows)

    assert len(first) == 1
    assert first[0].id == second[0].id
    assert first[0].event_type == "LossConnectionBoiler"
    assert first[0].details == {"object_id": 42, "object_name": "Котёл"}
    assert "+70000000000" not in first[0].model_dump_json()
    assert "43.1" not in first[0].model_dump_json()


def test_load_events_uses_filtered_read_only_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"ok": True, "events": [["id", 1_700_000_000, "OTFound", None, None, None, None, False]]},
        )

    with ZontReadOnlyClient(
        token="secret",
        client_email="owner@example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        events = client.load_events(
            device_id="7",
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2023, 1, 2, tzinfo=UTC),
        )

    assert captured["path"] == "/api/raw_events"
    assert '"only"' in str(captured["body"])
    assert "LossConnectionBoiler" in str(captured["body"])
    assert events[0][2] == "OTFound"


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
        assert (
            client.load_history(
                device_ids=["7"],
                start=datetime(2023, 1, 1, tzinfo=UTC),
                end=datetime(2023, 1, 2, tzinfo=UTC),
                data_types=["temperature"],
            )
            == []
        )

    assert calls == 2


def test_anonymized_live_contract_normalizes_radio_sensor_metrics() -> None:
    devices_payload = json.loads((CONTRACT_FIXTURES / "devices.json").read_text(encoding="utf-8"))
    history_payload = json.loads((CONTRACT_FIXTURES / "load_data.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/devices"):
            return httpx.Response(200, json=devices_payload)
        if request.url.path.endswith("/load_data"):
            return httpx.Response(200, json=history_payload)
        raise AssertionError(f"Unexpected path: {request.url.path}")

    with ZontReadOnlyClient(
        token="fixture-secret",
        client_email="fixture@example.test",
        transport=httpx.MockTransport(handler),
        history_request_interval_seconds=0,
    ) as client:
        devices = client.discover_devices()
        responses = client.load_history(
            device_ids=["100001"],
            start=datetime.fromtimestamp(1_700_000_000, UTC),
            end=datetime.fromtimestamp(1_700_001_200, UTC),
            data_types=["z3k_radio_sensor", "z3k_temperature", "z3k_heating_circuit"],
        )
        points, _entities = client.normalize_history(responses[0])

    radio_points = [point for point in points if point.source_type == "z3k_radio_sensor"]
    circuit = devices[0]["z3k_config"]["heating_circuits"][0]
    radio_sensor = devices[0]["z3k_config"]["radiosensors"][0]
    radio_state = devices[0]["io"]["z3k-state"]["30001"]
    humidity_rows = history_payload["responses"][0]["z3k_radio_sensor"]["30001"]["humidity"]
    assert circuit["air_temp_sensor"] == 30001
    assert devices[0]["io"]["z3k-state"]["20001"]["target_sensor_id"] == 30001
    assert radio_sensor["lower_humidity_threshold"] == 30
    assert radio_sensor["upper_humidity_threshold"] == 80
    assert {"battery", "rssi", "sensor_ok"} <= radio_state.keys()
    assert "quality" not in radio_state
    assert all(isinstance(row[1], int) for row in humidity_rows)
    assert {point.metric_key for point in radio_points} == {"battery", "dbm", "flags", "humidity", "temperature"}
    assert [point.value_num for point in radio_points if point.metric_key == "humidity"] == [56.0, 55.0]

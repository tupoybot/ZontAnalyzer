from __future__ import annotations

import fcntl
import threading
from datetime import date
from pathlib import Path

import httpx
import pytest

from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.application.pilot import reports_directory
from zont_analyzer.application.publication import publish_reports
from zont_analyzer.runtime import build_runtime


@pytest.fixture
def owner_server(tmp_path: Path):
    runtime = build_runtime(None, tmp_path)
    runtime.config.feedback.listen_port = 0
    runtime.config.feedback.public_api_base_url = "/api"
    runtime.db.save_devices([{"id": "test-device", "_equipment": {
        "boiler_model": {"value": "fixture-family", "source": "fixture:adapter.boiler_model"},
    }}])
    reports = [runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, day), use_ai=False) for day in (1, 3)]
    publish_reports(runtime)
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}/api", timeout=20) as client:
            yield runtime, reports, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_owner_roundtrip_is_daily_scoped_idempotent_and_preserves_feedback(owner_server) -> None:
    runtime, reports, client = owner_server
    feedback_before = runtime.db.recommendation_feedback()
    profiles = client.get("/equipment").json()["profiles"]
    assert profiles[0]["fields"]["boiler_model"]["value"] == "fixture-family"
    payload = {"fields": {"auto_adapt": {"value": False}, "has_gas_stove": {"value": None}}}
    response = client.put("/equipment/test-device", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["fields"]["auto_adapt"]["value"] is False
    assert client.put("/equipment/test-device", json=payload).json()["history"] == response.json()["history"]
    first, latest = [f"/reports/{report.id}/gas" for report in reports]
    assert client.put(latest, json={"value_m3": "200.5"}).status_code == 200
    saved = client.put(first, json={"value_m3": "150.25"})
    assert saved.status_code == 200
    assert saved.json()["reading"]["day"] == "2026-08-01"
    assert client.put(first, json={"value_m3": "150.250"}).json() == saved.json()
    assert client.put(first, json={"value_m3": "201"}).status_code == 422
    assert client.get(latest).json()["reading"]["value_m3"] == "200.5"
    removed = client.put(first, json={"delete": True})
    assert removed.status_code == 200 and removed.json()["reading"] is None
    assert removed.json()["audit"][-1]["action"] == "delete"
    assert client.get(first).json() == removed.json()
    assert runtime.db.recommendation_feedback() == feedback_before
    assert runtime.db.token_usage_this_month() == 0


def test_gas_writes_do_not_wait_for_publication_and_survive_restart(owner_server, monkeypatch) -> None:
    runtime, reports, client = owner_server
    output = reports_directory(runtime)
    latest = output / "latest.html"
    before = latest.read_bytes()
    url = f"/reports/{reports[-1].id}/gas"
    # A long worker publication must not block save, retry, correction or delete.
    with (output / ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for payload in ({"value_m3": "1929"}, {"value_m3": "1929"},
                            {"value_m3": "1930"}, {"delete": True}, {"value_m3": "1931"}):
                saved = client.put(url, json=payload, timeout=2)
                assert saved.status_code == 200, saved.text
                assert client.get(url).json() == saved.json()
            assert len(saved.json()["audit"]) == 4
            assert latest.read_bytes() == before
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    # The worker's publisher uses durable state, without an in-memory job.
    restarted = build_runtime(None, runtime.loaded.data_dir)
    from zont_analyzer.application import publication

    def fail(*args, **kwargs):
        raise OSError("publication unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_write_changed", fail)
        with pytest.raises(OSError, match="publication unavailable"):
            publish_reports(restarted)
    assert client.get(url).json()["reading"]["value_m3"] == "1931"
    assert latest.read_bytes() == before
    publish_reports(restarted)
    assert "Текущее показание: 1931 м³" in latest.read_text()
    assert runtime.db.token_usage_this_month() == 0


def test_owner_api_accepts_selected_dates_and_rejects_spoofed_scope_and_cross_origin(owner_server) -> None:
    _, reports, client = owner_server
    url = f"/reports/{reports[0].id}/gas"
    selected = client.put(url, json={"day": "2026-08-02", "value_m3": "1"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["selected_day"] == "2026-08-02"
    assert client.get(url + "?day=2026-08-02").json()["reading"]["id"] == selected.json()["reading"]["id"]
    for extra in ({"device_id": "other"}, {"meter_segment": "invented"}, {"day": "2026-8-2"}):
        response = client.put(url, json={"value_m3": "1", **extra})
        assert response.status_code == 422
    assert client.put(url, content='{"value_m3": 1}', headers={"Content-Type": "text/plain"}).status_code == 422
    assert client.put(url, json={"value_m3": 1}, headers={"Origin": "https://other.invalid"}).status_code == 403
    assert client.put(url, json={"value_m3": 1}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/equipment/test-device", json={"fields": {"boiler_model": {
        "value": "spoof", "source": "auto",
    }}}).status_code == 422
    assert client.put("/equipment/missing", json={"fields": {}}).status_code == 404
    assert client.get("/reports/missing/gas").status_code == 404
    assert client.get(url).json()["reading"] is None


def test_monthly_tariff_api_audit_and_selective_publication(owner_server, monkeypatch) -> None:
    from zont_analyzer.application.gas import GasService

    runtime, reports, client = owner_server
    refreshed = []
    original = GasService.refresh_cost

    def track(self, report):
        refreshed.append(report.id)
        return original(self, report)

    monkeypatch.setattr(GasService, "refresh_cost", track)
    feedback = runtime.db.recommendation_feedback()
    assert client.get("/gas-tariffs").json() == {"history": []}
    payload = {"price": "8,01", "currency": "RUB", "effective_month": "2026-08"}
    saved = client.put("/gas-tariffs", json=payload)
    assert saved.status_code == 200, saved.text
    assert set(refreshed) == {report.id for report in reports}
    first = saved.json()["tariff"]
    refreshed.clear()
    assert client.put("/gas-tariffs", json=payload).json()["idempotent"] is True
    assert refreshed == []
    future = client.put("/gas-tariffs", json={**payload, "effective_month": "2026-09", "price": "9"})
    assert future.status_code == 200, future.text
    assert refreshed == []
    corrected = client.put("/gas-tariffs", json={"action": "correct", "id": first["id"],
        "price": "8.02", "currency": "RUB", "correction_reason": "Опечатка"})
    assert corrected.status_code == 200, corrected.text
    assert set(refreshed) == {report.id for report in reports}
    history = client.get("/gas-tariffs").json()["history"]
    assert len(history) == 2 and history[0]["price"] == "8.02"
    assert runtime.db.recommendation_feedback() == feedback
    assert runtime.db.token_usage_this_month() == 0


def test_tariff_api_rejects_invalid_values_and_cross_origin(owner_server) -> None:
    _, _, client = owner_server
    payload = {"price": "8,01", "currency": "RUB", "effective_month": "2026-08"}
    for fields in ({"price": "-1"}, {"price": "NaN"}, {"currency": "XXX"},
                   {"effective_month": "2026-08-15"}, {"scope": "other"},
                   {"effective_from": "2026-08-15T00:00:00Z"}):
        assert client.put("/gas-tariffs", json={**payload, **fields}).status_code == 422
    assert client.put("/gas-tariffs", json=payload,
                      headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/gas-tariffs").json() == {"history": []}

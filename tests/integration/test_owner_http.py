from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import httpx
import pytest

from zont_analyzer.application.feedback import build_feedback_server
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


def test_owner_api_rejects_spoofed_source_scope_dates_and_cross_origin(owner_server) -> None:
    _, reports, client = owner_server
    url = f"/reports/{reports[0].id}/gas"
    for extra in ({"day": "2026-08-02"}, {"device_id": "other"}, {"meter_segment": "invented"}):
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

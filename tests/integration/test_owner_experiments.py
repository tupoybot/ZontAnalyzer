from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_feedback_http import _runtime
from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.reports import render_html


def _payload() -> dict[str, Any]:
    return {"request_id": "request-0001", "state": "planned", "changes": [
        {"device_id": "device-1", "circuit_id": "1", "parameter": "curve", "before": "1", "after": "2"},
        {"device_id": "device-1", "parameter": "limit", "before": "50", "after": "45"}],
        "goal": "Comfort", "expected_effect": "Stable temperature", "observation_window": "7 days"}


@pytest.mark.ydb
def test_owner_journal_http_roundtrip_history_and_security(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.loaded.config.feedback.listen_port = 0
    runtime.loaded.config.feedback.public_api_base_url = "/api"
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    report.recommendations = []
    assert "Начать эксперимент / Зафиксировать изменение" in render_html(report)
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/reports/{report.id}/experiments"
    try:
        with httpx.Client(timeout=10) as client:
            payload = _payload()
            assert client.put(url, json=payload, headers={"Origin": "https://other.example"}).status_code == 403
            for field, value in (("device_id", "other"), ("circuit_id", "999")):
                invalid = {**payload, "changes": [{**payload["changes"][0], field: value}]}
                assert client.put(url, json=invalid).status_code == 422
            first = client.put(url, json=payload)
            assert first.status_code == 200, first.text
            saved = first.json()["experiments"][0]
            assert len(saved["changes"]) == 2
            assert saved["recommendation_id"] is None
            assert runtime.db.intervention_history() == []
            assert client.put(url, json=payload).json() == first.json()
            edit = {**payload, "request_id": "request-0002", "experiment_id": saved["id"],
                    "expected_revision": 1, "state": "performed", "goal": "Revised goal"}
            unknown = client.put(url, json=edit)
            assert unknown.status_code == 200, unknown.text
            assert runtime.db.intervention_history() == []
            dated = {**edit, "request_id": "request-0003", "expected_revision": 2,
                     "performed_at": "2026-08-01T12:00:00"}
            result = client.put(url, json=dated)
            assert result.status_code == 200, result.text
            value = result.json()["experiments"][0]
            assert value["original"]["goal"] == "Comfort"
            assert len(value["history"]) == 3
            assert len(runtime.db.intervention_history()) == 1
            followup = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 5), use_ai=False)
            assert followup.context["intervention_outcomes"]
            assert client.put(url, json={**dated, "request_id": "request-stale"}).status_code == 422
            rollback = {**dated, "request_id": "request-0004", "expected_revision": 3,
                        "state": "rolled_back", "rollback_at": "2026-08-02T12:00:00"}
            assert client.put(url, json=rollback).status_code == 200
            latest = client.get(url).json()["experiments"][0]
            assert latest["confirmation"] == "owner_reported"
            assert latest["state"] == "rolled_back"
            assert latest["original"] == saved["original"]
            assert client.put(url, json=payload).status_code == 200
            assert len(client.get(url).json()["experiments"]) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.ydb
def test_journal_report_binding_and_replay_survive_discovery_changes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}},
                             {"device_id": "device-2", "z3k_config": {"heating_circuits": [{"id": 2}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    # Ordinary reports save their equipment scope even when discovery has more than one device.
    assert {d["id"] for d in runtime.db.experiments_for_report(report.id)["devices"]} == {"device-1", "device-2"}
    report.context["control_settings_history"] = {"device_id": "device-1", "circuit_id": "1"}
    runtime.db.save_report(report, "fixture")
    assert [d["id"] for d in runtime.db.experiments_for_report(report.id)["devices"]] == ["device-1"]
    payload = _payload()
    first = runtime.db.save_owner_experiment(report.id, payload)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": []}},
                             {"device_id": "device-3"}])
    assert runtime.db.save_owner_experiment(report.id, payload) == first
    other_report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    with pytest.raises(ValueError, match="отчёту"):
        runtime.db.save_owner_experiment(other_report.id, payload)
    with pytest.raises(ValueError, match="контур"):
        runtime.db.save_owner_experiment(report.id, {**payload, "request_id": "request-new1"})


@pytest.mark.ydb
def test_linked_journal_action_deduplicates_legacy_without_hiding_distinct_actions(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    rec = report.recommendations[0].id
    assert rec is not None
    runtime.db.set_recommendation_feedback(rec, "applied", experiment={
        "category": "settings", "parameter": "curve", "before": 1.0, "after": 2,
        "performed_at": "2026-08-01T12:00:00+04:00"})
    payload = {**_payload(), "recommendation_id": rec, "state": "performed",
               "performed_at": "2026-08-01T08:00:00+00:00", "changes": [
                   {"device_id": "device-1", "parameter": "curve", "before": "1", "after": "2.0"}]}
    runtime.db.save_owner_experiment(report.id, payload)
    assert len(runtime.db.intervention_history()) == 1
    distinct = {**payload, "request_id": "request-other", "changes": [
        {**payload["changes"][0], "after": "3"}]}
    runtime.db.save_owner_experiment(report.id, distinct)
    assert len(runtime.db.intervention_history()) == 2


@pytest.mark.ydb
def test_firmware_rollback_feed_reverses_values_and_retains_owner_evidence(tmp_path: Path) -> None:
    from zont_analyzer.application.reasoning_context import reasoning_context

    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1"}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    payload = {**_payload(), "category": "firmware_update", "state": "performed",
               "performed_at": "2026-08-01T08:00:00+00:00", "changes": [
                   {"device_id": "device-1", "parameter": "firmware", "before": "v1", "after": "v2"}]}
    first = runtime.db.save_owner_experiment(report.id, payload)
    runtime.db.save_owner_experiment(report.id, {
        **payload, "request_id": "request-back", "experiment_id": first["id"], "expected_revision": 1,
        "state": "rolled_back", "rollback_at": "2026-08-02T09:00:00+00:00"})
    history = runtime.db.intervention_history()
    rollback = history[0]["experiment"]
    assert rollback["category"] == "firmware_rollback"
    assert rollback["changes"][0]["before"] == "v2"
    assert rollback["changes"][0]["after"] == "v1"
    context = reasoning_context([], [], "UTC", intervention_history=history)
    event = context["dhw_profiles"]["firmware"]["owner_recorded_timeline"][0]
    assert event["event"] == "owner_reported_rollback"
    assert event["confirmation"] == "owner_reported"
    assert event["telemetry_confirmation"] == "not_verified"

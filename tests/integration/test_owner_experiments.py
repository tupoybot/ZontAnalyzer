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
    assert len(runtime.db.intervention_history(limit=1)) == 1
    assert runtime.db.intervention_history(limit=2)[0]["experiment"]["changes"][0]["after"] == "3"


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


@pytest.mark.ydb
def test_event_feed_orders_action_time_bounds_reads_and_keeps_pre_rollback_cutoff(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime

    from zont_analyzer.adapters.ydb.database import Transaction

    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    records = {}
    for identifier, day in (("request-latest", 3), ("request-oldest", 1), ("request-middle", 2)):
        records[identifier] = runtime.db.save_owner_experiment(report.id, {
            **_payload(), "request_id": identifier, "state": "performed",
            "performed_at": f"2026-08-0{day}T08:00:00+00:00"})
    queries: list[str] = []
    original = Transaction.execute

    def traced(tx, query, parameters=None):
        queries.append(query)
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", traced)
    events = runtime.db.owner_experiments.factual_history(limit=2)
    assert [event["intervention_id"] for event in events] == ["request-latest", "request-middle"]
    assert len(queries) == 1
    assert "owner_experiment_events VIEW by_applied" in queries[0]
    assert "ORDER BY applied_at DESC,id DESC LIMIT $limit" in queries[0]
    oldest = records["request-oldest"]
    updated = {**oldest["history"][0]["request"], "experiment_id": oldest["id"],
               "expected_revision": 1, "request_id": "request-rollback", "state": "rolled_back",
               "rollback_at": "2026-08-04T09:00:00+00:00"}
    runtime.db.save_owner_experiment(report.id, updated)
    history = runtime.db.owner_experiments.factual_history(limit=2, before=datetime(2026, 8, 2, tzinfo=UTC))
    assert [event["intervention_id"] for event in history] == ["request-oldest"]
    assert "WHERE applied_at<$before" in queries[-1]
    latest = runtime.db.owner_experiments.factual_history(limit=1)
    assert latest[0]["intervention_id"] == "request-oldest:rollback"
    assert "history" not in latest[0]["experiment"]


@pytest.mark.ydb
def test_event_projection_is_atomic_replaces_unknown_time_and_skips_plans(tmp_path: Path, monkeypatch) -> None:
    from zont_analyzer.adapters.ydb.feedback import FeedbackRepository

    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    payload = _payload()
    plan = runtime.db.save_owner_experiment(report.id, payload)
    assert runtime.db.owner_experiments.factual_history() == []
    assert runtime.db.owner_experiments.uncertain_history() == []
    known = {**payload, "experiment_id": plan["id"], "expected_revision": 1,
             "request_id": "request-known", "state": "performed", "performed_at": "2026-08-01T08:00:00+00:00"}
    saved = runtime.db.save_owner_experiment(report.id, known)
    event = runtime.db.owner_experiments.factual_history()[0]
    unknown = {**known, "expected_revision": 2, "request_id": "request-unknown", "performed_at": None}
    with monkeypatch.context() as patch:
        def fail(*args):
            raise ValueError("forced failure after journal and event writes")
        patch.setattr(FeedbackRepository, "_queue_render", fail)
        with pytest.raises(ValueError, match="forced failure"):
            runtime.db.save_owner_experiment(report.id, unknown)
    assert runtime.db.experiments_for_report(report.id)["experiments"][0] == saved
    assert runtime.db.owner_experiments.factual_history() == [event]
    assert runtime.db.owner_experiments.uncertain_history() == []
    corrected = runtime.db.save_owner_experiment(report.id, unknown)
    assert corrected["revision"] == 3
    assert len(corrected["history"]) == 3
    assert runtime.db.owner_experiments.factual_history() == []
    uncertain = runtime.db.owner_experiments.uncertain_history()
    assert len(uncertain) == 1
    assert uncertain[0]["intervention_id"] == plan["id"] + ":undated"
    assert uncertain[0]["temporal_boundary"] is None
    assert uncertain[0]["temporal_boundary_source"] == "unknown_time"
    assert uncertain[0]["experiment"]["performed_at"] is None
    assert runtime.db.save_owner_experiment(report.id, known) == corrected
    assert runtime.db.owner_experiments.factual_history() == []
    assert runtime.db.owner_experiments.uncertain_history() == uncertain
    restored = {**known, "request_id": "request-restored", "expected_revision": 3}
    runtime.db.save_owner_experiment(report.id, restored)
    assert runtime.db.owner_experiments.uncertain_history() == []
    assert len(runtime.db.owner_experiments.factual_history()) == 1


@pytest.mark.ydb
def test_manual_publication_tracks_old_new_times_scopes_and_unknown_actions(tmp_path: Path) -> None:
    import json
    from datetime import UTC, datetime

    from zont_analyzer.adapters.ydb.feedback import _micros

    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)

    def markers():
        rows = runtime.db.storage.transaction(lambda tx: tx.execute(
            "SELECT identifier,revision,payload FROM publication_changes WHERE scope='regulation-manual';"
        )[0].rows)
        return {str(row.identifier): (int(row.revision), json.loads(row.payload)) for row in rows}

    plan = runtime.db.save_owner_experiment(report.id, _payload())
    assert markers() == {}
    performed = {**_payload(), "state": "performed", "request_id": "request-performed",
                 "experiment_id": plan["id"], "expected_revision": 1,
                 "performed_at": "2026-08-02T08:00:00+00:00"}
    first = runtime.db.save_owner_experiment(report.id, performed)
    at = _micros(datetime(2026, 8, 2, 8, tzinfo=UTC))
    assert {key: value[1] for key, value in markers().items()} == {
        "device-1/1": {"change_start": at, "change_end": at},
        "device-1/*": {"change_start": at, "change_end": at}}
    snapshot = markers()
    assert runtime.db.save_owner_experiment(report.id, performed) == first
    assert markers() == snapshot
    corrected = {**performed, "expected_revision": 2, "request_id": "request-corrected",
                 "performed_at": "2026-08-01T08:00:00+00:00", "changes": [_payload()["changes"][0]]}
    runtime.db.save_owner_experiment(report.id, corrected)
    earlier = _micros(datetime(2026, 8, 1, 8, tzinfo=UTC))
    assert markers()["device-1/1"][1] == {"change_start": earlier, "change_end": at}
    assert markers()["device-1/*"][1] == {"change_start": at, "change_end": at}
    unknown = {**corrected, "expected_revision": 3, "request_id": "request-unknown", "performed_at": None,
               "state": "rolled_back", "rollback_at": "2026-08-03T08:00:00+00:00"}
    runtime.db.save_owner_experiment(report.id, unknown)
    assert markers()["device-1/1"][1] == {}
    assert len(runtime.db.owner_experiments.factual_history()) == 1
    assert len(runtime.db.owner_experiments.uncertain_history()) == 1
    both_unknown = {**unknown, "expected_revision": 4, "request_id": "request-no-times", "rollback_at": None}
    runtime.db.save_owner_experiment(report.id, both_unknown)
    assert runtime.db.owner_experiments.factual_history() == []
    assert {event["intervention_id"] for event in runtime.db.owner_experiments.uncertain_history()} == {
        plan["id"] + ":undated", plan["id"] + ":rollback-undated"}
    assert len(runtime.db.owner_experiments.uncertain_history(limit=1)) == 1
    with pytest.raises(ValueError, match="between"):
        runtime.db.owner_experiments.uncertain_history(limit=1001)


@pytest.mark.ydb
def test_bounded_projection_repair_preserves_journal_and_is_idempotent(tmp_path: Path) -> None:
    from tools.backfill_owner_experiment_events import repair_batch

    runtime = _runtime(tmp_path)
    runtime.db.save_devices([{"device_id": "device-1", "z3k_config": {"heating_circuits": [{"id": 1}]}}])
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    plan = runtime.db.save_owner_experiment(report.id, {**_payload(), "request_id": "repair-a-plan"})
    unknown = runtime.db.save_owner_experiment(report.id, {
        **_payload(), "request_id": "repair-b-unknown", "state": "performed"})
    runtime.db.save_owner_experiment(report.id, {
        **_payload(), "request_id": "repair-c-known", "state": "performed",
        "performed_at": "2026-08-01T08:00:00+00:00"})
    damaged = runtime.db.save_owner_experiment(report.id, {
        **_payload(), "request_id": "repair-d-damaged", "state": "performed",
        "performed_at": "2026-08-02T08:00:00+00:00"})
    before = runtime.db.experiments_for_report(report.id)
    db = runtime.db.storage
    db.transaction(lambda tx: tx.execute(
        "DECLARE $id AS Utf8; DELETE FROM owner_experiment_events WHERE id=$id;",
        {"$id": unknown["id"] + ":undated"}))
    db.transaction(lambda tx: tx.execute(
        "DECLARE $id AS Utf8; UPDATE owner_experiment_events SET applied_at=1 WHERE id=$id;",
        {"$id": damaged["id"]}))
    dry = repair_batch(db)
    assert dry == {"scanned": 4, "mismatched": 2, "repaired": 0, "dry_run": True, "continuation_id": None}
    assert runtime.db.owner_experiments.uncertain_history() == []
    first = repair_batch(db, limit=1, apply=True)
    assert first["continuation_id"] == plan["id"]
    assert first["repaired"] == 0
    second = repair_batch(db, limit=1, after_id=first["continuation_id"], apply=True)
    assert second["continuation_id"] == unknown["id"]
    assert second["repaired"] == 1
    assert len(runtime.db.owner_experiments.uncertain_history()) == 1
    remainder = repair_batch(db, after_id=second["continuation_id"], apply=True)
    assert remainder["scanned"] == 2
    assert remainder["repaired"] == 1
    assert runtime.db.experiments_for_report(report.id) == before
    assert repair_batch(db, apply=True)["repaired"] == 0
    assert len(runtime.db.owner_experiments.factual_history()) == 2
    with pytest.raises(ValueError, match="between"):
        repair_batch(db, limit=101)

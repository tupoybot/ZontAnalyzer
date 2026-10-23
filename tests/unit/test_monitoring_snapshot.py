"""Read-only operational snapshots preserve missing evidence and isolate failures."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.unit.test_cloud_runtime import _request
from tests.unit.test_cloud_runtime import server as server
from zont_analyzer.cloud import monitoring, runtime
from zont_analyzer.config import AppConfig
from zont_analyzer.observability import capture


def _application(*, budget=None, state=None, proposals=(), latest=None, overrides=None):
    storage = Mock()
    transaction = Mock()
    transaction.execute.return_value = [SimpleNamespace(rows=[] if overrides is None else [
        SimpleNamespace(version=1, effective_at=0, payload=json.dumps({"overrides": overrides})),
    ])]
    storage.transaction.side_effect = lambda callback: callback(transaction)
    storage.execute.side_effect = [
        [SimpleNamespace(rows=[] if latest is None else [SimpleNamespace(id=1)])],
        *([] if latest is None else [[SimpleNamespace(rows=[SimpleNamespace(timestamp_utc=latest.timestamp())])]]),
        [SimpleNamespace(rows=[] if budget is None else [SimpleNamespace(**budget)])],
        [SimpleNamespace(rows=[] if state is None else [SimpleNamespace(payload=json.dumps(state))])],
        [SimpleNamespace(rows=[SimpleNamespace(payload=json.dumps(row)) for row in proposals])],
    ]
    db = SimpleNamespace(storage=storage, close=Mock(), latest_sample_time=Mock(return_value=None))
    return SimpleNamespace(db=db, config=AppConfig())


def test_empty_snapshot_does_not_invent_success_time_or_reliability():
    app = _application()
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
    assert ("zont_snapshot_success", 1.0, {}) in events
    assert ("zont_model_review_initialized", 0.0, {}) in events
    names = {name for name, _, _ in events}
    assert "zont_model_review_last_success_timestamp_seconds" not in names
    assert "zont_model_review_next_due_timestamp_seconds" not in names
    assert "zont_reliability_uptime_seconds" not in names
    assert "zont_telemetry_timestamp_seconds" not in names
    assert app.db.storage.execute.call_count == 4
    assert all("SELECT" in call.args[0] and "UPSERT" not in call.args[0]
               for call in app.db.storage.execute.call_args_list)


def test_snapshot_counts_known_statuses_without_exporting_payloads():
    at = datetime(2026, 9, 1, tzinfo=UTC)
    app = _application(
        budget={"charged_tokens": 123, "reserved_tokens": 456},
        state={"attempts": 2, "last_error": "private upstream body", "last_success_at": at.isoformat()},
        proposals=[{"status": "open", "private": "secret"}, {"status": "open"}, {"status": "deferred"},
                   {"status": "untrusted-private-status"}],
    )
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
    assert ("zont_monthly_ai_tokens", 123.0, {}) in events
    assert ("zont_monthly_ai_reserved_tokens", 456.0, {}) in events
    assert ("zont_model_review_error", 1.0, {}) in events
    assert ("zont_model_review_last_success_timestamp_seconds", at.timestamp(), {}) in events
    assert ("zont_model_review_proposals", 2.0, {"status": "open"}) in events
    assert ("zont_model_review_proposals", 1.0, {"status": "deferred"}) in events
    assert ("zont_model_review_proposals", 0.0, {"status": "accepted"}) in events
    assert "private" not in repr(events) and "secret" not in repr(events)


@pytest.mark.parametrize("failure", ["query", "invalid_state", "too_many_proposals"])
def test_snapshot_failures_are_isolated_and_emit_failure_gauge(failure):
    app = _application(proposals=[{"status": "open"}] * (1001 if failure == "too_many_proposals" else 0))
    if failure == "query":
        app.db.storage.execute.side_effect = RuntimeError("private query content")
    elif failure == "invalid_state":
        app.db.storage.execute.side_effect = [
            [SimpleNamespace(rows=[])], [SimpleNamespace(rows=[])],
            [SimpleNamespace(rows=[SimpleNamespace(payload="not JSON")])],
        ]
    events = []
    with capture(events.append):
        assert monitoring.snapshot(app) is None
    assert ("zont_snapshot_success", 0.0, {}) in events
    assert not any(name == "zont_model_review_proposals" for name, _, _ in events)
    if failure == "too_many_proposals":
        assert "LIMIT 1001" in app.db.storage.execute.call_args.args[0]
    assert "private" not in repr(events)


def test_reliability_exports_only_existing_evidence_and_never_infers_zero():
    report = SimpleNamespace(generated_at=datetime(2026, 9, 1, tzinfo=UTC), metrics=[])
    events = []
    with capture(events.append):
        monitoring.report_metrics(report)
    assert [name for name, _, _ in events] == ["zont_report_generated_timestamp_seconds"]
    report.metrics = [
        SimpleNamespace(name="zont_uptime_seconds", value=120.0,
                        context={"lower_bound": True, "data_fresh": False,
                                 "continuity_uncertain": "unknown", "private": "secret"}),
        SimpleNamespace(name="other_metric", value=99.0, context={}),
    ]
    events.clear()
    with capture(events.append):
        monitoring.report_metrics(report)
    assert ("zont_reliability_uptime_seconds", 120.0, {"component": "zont"}) in events
    assert ("zont_reliability_evidence", 1.0, {"component": "zont", "quality": "lower_bound"}) in events
    assert ("zont_reliability_evidence", 0.0, {"component": "zont", "quality": "data_fresh"}) in events
    assert not any(labels.get("component") == "boiler" or labels.get("quality") == "continuity_uncertain"
                   for _, _, labels in events)
    assert "secret" not in repr(events)


@pytest.mark.parametrize("latest", [None, datetime(2026, 9, 1, tzinfo=UTC)])
def test_monitoring_dispatch_reads_without_providers_and_closes_runtime(monkeypatch, latest):
    app = _application(latest=latest)
    queue_sample = Mock()
    monkeypatch.setattr(monitoring, "queues", queue_sample)
    factory = Mock(return_value=app)
    monkeypatch.setattr("zont_analyzer.runtime.open_runtime", factory)
    events = []
    with capture(events.append):
        assert runtime._dispatch_monitoring({}) == {"status": "observed"}
    factory.assert_called_once_with()
    queue_sample.assert_called_once_with(app)
    assert ("zont_telemetry_present", float(latest is not None), {}) in events
    timestamps = [value for name, value, _ in events if name == "zont_telemetry_timestamp_seconds"]
    assert timestamps == ([] if latest is None else [latest.timestamp()])
    app.db.close.assert_called_once()
    app.db.latest_sample_time.assert_not_called()
    factory.reset_mock()
    with pytest.raises(ValueError):
        runtime._dispatch_monitoring({"force_provider": True})
    factory.assert_not_called()


def test_internal_monitoring_timer_ignores_body_and_public_route_requires_auth(server, monkeypatch):
    instance, _ = server
    instance.tunnel.is_ready = False
    dispatch = Mock(return_value={"status": "observed"})
    monkeypatch.setattr(runtime, "run_bounded", dispatch)
    status, body = _request(instance, "invalid:credentials", "POST", "/internal/monitoring",
                            {"force_provider": True, "operation": "reports"})
    assert status == 200 and body["result"] == {"status": "observed"}
    assert dispatch.call_args.args[:2] == (runtime.DISPATCHERS["monitoring"], {})
    dispatch.reset_mock()
    assert _request(instance, "invalid:credentials", "POST", "/jobs/monitoring", {})[0] == 401
    assert _request(instance, "invalid:credentials", "GET", "/internal/monitoring")[0] == 401
    dispatch.assert_not_called()


@pytest.mark.ydb
def test_snapshot_queries_match_native_schema_and_are_read_only(tmp_path):
    from tests.ydb_support import make_database

    db = make_database(tmp_path)
    month = datetime.now(UTC).strftime("%Y-%m")
    db.storage.execute(
        "DECLARE $month AS Utf8; UPSERT INTO ai_budget_months (month,charged_tokens,reserved_tokens) "
        "VALUES ($month,17,23);", {"$month": month},
    )
    db.storage.execute(
        "DECLARE $payload AS Utf8; UPSERT INTO model_review_state(scope,payload,version) "
        "VALUES ('installation',$payload,1);", {"$payload": json.dumps({"attempts": 2})},
    )
    db.storage.execute(
        "DECLARE $payload AS Utf8; UPSERT INTO model_review_proposals(id,payload) "
        "VALUES ('fixture',$payload);", {"$payload": json.dumps({"status": "open"})},
    )
    db.storage.execute(
        "UPSERT INTO telemetry_series (device_id,source_type,entity_id,metric_key,id) "
        "VALUES ('fixture','temperature','fixture','value',1);",
    )
    db.storage.execute(
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num) "
        "VALUES (1,1700000000,21.0),(1,1700000300,22.0);",
    )
    app = SimpleNamespace(db=db, config=AppConfig())
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
        monitoring.snapshot(app)
    assert events.count(("zont_snapshot_success", 1.0, {})) == 2
    assert events.count(("zont_telemetry_timestamp_seconds", 1700000300.0, {})) == 2
    assert events.count(("zont_monthly_ai_tokens", 17.0, {})) == 2
    assert events.count(("zont_monthly_ai_reserved_tokens", 23.0, {})) == 2
    assert events.count(("zont_model_review_proposals", 1.0, {"status": "open"})) == 2
    state = db.storage.execute("SELECT version FROM model_review_state WHERE scope='installation';")[0].rows
    assert state[0].version == 1


def test_snapshot_series_limit_fails_before_reading_any_samples():
    app = _application()
    app.db.storage.execute.side_effect = [[SimpleNamespace(rows=[SimpleNamespace(id=i) for i in range(65)])]]
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
    assert ("zont_snapshot_success", 0.0, {}) in events
    app.db.storage.execute.assert_called_once_with("SELECT id FROM telemetry_series LIMIT 65;")
    assert not any(name == "zont_telemetry_timestamp_seconds" for name, _, _ in events)


@pytest.mark.ydb
def test_snapshot_batches_indexed_latest_samples_and_preserves_empty_series(tmp_path, monkeypatch):
    from tests.ydb_support import make_database

    db = make_database(tmp_path)
    db.storage.execute(
        "UPSERT INTO telemetry_series (device_id,source_type,entity_id,metric_key,id) VALUES "
        "('fixture','t','a','value',1),('fixture','t','b','value',2),('fixture','t','empty','value',3);"
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num) VALUES "
        "(1,1700000000,21.0),(1,1700000300,22.0),(2,1700000600,23.0);",
    )
    execute = Mock(wraps=db.storage.execute)
    monkeypatch.setattr(db.storage, "execute", execute)
    events = []
    with capture(events.append):
        monitoring._snapshot(SimpleNamespace(db=db, config=AppConfig()))
    assert ("zont_telemetry_timestamp_seconds", 1700000600.0, {}) in events
    samples = [call for call in execute.call_args_list if "FROM telemetry_samples" in call.args[0]]
    assert len(samples) == 1
    assert samples[0].args[0].count("LIMIT 1;") == 3


@pytest.mark.parametrize("enabled", [True, False])
def test_effective_review_schedule_honors_overrides_without_mutating_state(enabled):
    last_success = datetime(2026, 9, 1, tzinfo=UTC)
    app = _application(
        state={"last_success_at": last_success.isoformat(),
               "next_due_at": (last_success + timedelta(days=60)).isoformat()},
        overrides={"review_enabled": enabled, "review_interval_days": 7},
    )
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
    assert ("zont_snapshot_success", 1.0, {}) in events
    assert ("zont_model_review_enabled", float(enabled), {}) in events
    due = [value for name, value, _ in events if name == "zont_model_review_next_due_timestamp_seconds"]
    assert due == ([(last_success + timedelta(days=7)).timestamp()] if enabled else [])
    assert app.db.storage.transaction.call_count == 1


def test_review_retry_schedule_keeps_runner_backoff_instead_of_interval():
    last_success = datetime(2026, 9, 1, tzinfo=UTC)
    retry_at = last_success + timedelta(hours=6)
    app = _application(
        state={"last_success_at": last_success.isoformat(), "last_error": "temporary failure",
               "next_due_at": retry_at.isoformat()},
        overrides={"review_interval_days": 7},
    )
    events = []
    with capture(events.append):
        monitoring.snapshot(app)
    assert ("zont_model_review_next_due_timestamp_seconds", retry_at.timestamp(), {}) in events


@pytest.mark.ydb
def test_effective_review_overrides_match_runner_with_native_storage(tmp_path):
    from tests.ydb_support import make_database
    from zont_analyzer.application.ai_settings import AISettingsStore
    from zont_analyzer.application.model_review import ModelReviewStore

    db = make_database(tmp_path)
    config = AppConfig()
    settings = AISettingsStore(db, config)
    last_success = datetime(2026, 9, 1, tzinfo=UTC)
    state = {"last_success_at": last_success.isoformat(),
             "next_due_at": (last_success + timedelta(days=60)).isoformat()}
    db.storage.execute(
        "DECLARE $payload AS Utf8; UPSERT INTO model_review_state(scope,payload,version) "
        "VALUES ('installation',$payload,1);", {"$payload": json.dumps(state)},
    )
    app = SimpleNamespace(db=db, config=config)
    for enabled in (True, False):
        # Seed a persisted owner override; the monitoring snapshot itself remains read-only.
        settings.save({"expected_version": settings.snapshot()["version"],
                       "values": {"review_enabled": enabled, "review_interval_days": 7}})
        before = settings.snapshot()
        events = []
        with capture(events.append):
            monitoring.snapshot(app)
        assert ("zont_snapshot_success", 1.0, {}) in events
        assert ("zont_model_review_enabled", float(enabled), {}) in events
        due = [value for name, value, _ in events if name == "zont_model_review_next_due_timestamp_seconds"]
        expected = ModelReviewStore._next_due(state, before["effective"])
        assert due == ([expected.timestamp()] if enabled else [])
        assert settings.snapshot() == before
        persisted = db.storage.execute(
            "SELECT payload,version FROM model_review_state WHERE scope='installation';",
        )[0].rows[0]
        assert persisted.version == 1 and json.loads(persisted.payload) == state

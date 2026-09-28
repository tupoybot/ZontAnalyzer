"""Queue samples are bounded, read-only and explicit about missing evidence."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer.cloud import monitoring, user_jobs
from zont_analyzer.observability import capture


def _app(publication=(), manual=()):
    storage = Mock()
    storage.execute.side_effect = [
        [SimpleNamespace(rows=[SimpleNamespace(queued_at=value) for value in publication])],
        [SimpleNamespace(rows=list(manual))],
    ]
    return SimpleNamespace(db=SimpleNamespace(storage=storage))


def _job(state="released", until=0, status="queued", updated="2026-09-01T00:00:00+00:00"):
    return SimpleNamespace(state=state, lease_until=until,
                           checkpoint=json.dumps({"status": status, "updated_at": updated,
                                                  "question": "private question", "report_id": "private id"}))


def _measure(app, publication=None):
    events = []
    with capture(events.append):
        monitoring.queues(app, publication)
    return events


def test_empty_queues_have_explicit_zeros_and_clear_oldest_time():
    app = _app()
    events = _measure(app)
    for kind in ("publication", "manual"):
        assert ("zont_queue_snapshot_success", 1.0, {"kind": kind}) in events
        assert ("zont_queue_truncated", 0.0, {"kind": kind}) in events
        for status in ("pending", "running", "blocked"):
            assert ("zont_queue_items", 0.0, {"kind": kind, "status": status}) in events
    assert [(value, labels) for name, value, labels in events
            if name == "zont_queue_oldest_timestamp_seconds"] == [
                (0.0, {"kind": "publication"}), (0.0, {"kind": "manual"}),
            ]
    queries = [call.args[0] for call in app.db.storage.execute.call_args_list]
    assert all("SELECT" in query and "LIMIT 1001" in query for query in queries)
    assert "VIEW by_queue" in queries[0]
    assert "job_key >= 'm5:' AND job_key < 'm5;'" in queries[1]


def test_manual_queue_distinguishes_live_expired_and_blocked_work_without_payload_labels(monkeypatch):
    monkeypatch.setattr(monitoring.time, "time_ns", lambda: 100_000_000_000)
    app = _app(publication=[90_000_000, 80_000_000], manual=[
        _job(), _job("active", 101_000_000, "running"),
        _job("active", 100_000_000, "running"),
        _job(status="reconciliation_required"),
    ])
    events = _measure(app)
    assert ("zont_queue_items", 2.0, {"kind": "publication", "status": "pending"}) in events
    assert ("zont_queue_oldest_timestamp_seconds", 80.0, {"kind": "publication"}) in events
    assert ("zont_queue_items", 2.0, {"kind": "manual", "status": "pending"}) in events
    assert ("zont_queue_items", 1.0, {"kind": "manual", "status": "running"}) in events
    assert ("zont_queue_items", 1.0, {"kind": "manual", "status": "blocked"}) in events
    assert ("zont_queue_oldest_timestamp_seconds", datetime(2026, 9, 1, tzinfo=UTC).timestamp(),
            {"kind": "manual"}) in events
    assert "private" not in repr(events)


def test_successful_publication_reuses_exact_count_without_queue_read():
    app = _app()
    app.db.storage.execute.side_effect = [[SimpleNamespace(rows=[])]]
    events = _measure(app, {"pending_reports": 1234, "pending_oldest_timestamp_seconds": 30})
    assert ("zont_queue_items", 1234.0, {"kind": "publication", "status": "pending"}) in events
    assert ("zont_queue_truncated", 0.0, {"kind": "publication"}) in events
    assert ("zont_queue_oldest_timestamp_seconds", 30.0, {"kind": "publication"}) in events
    app.db.storage.execute.assert_called_once()
    assert "FROM jobs" in app.db.storage.execute.call_args.args[0]


def test_capped_samples_show_lower_bounds_and_suppress_unproven_oldest():
    events = _measure(_app(publication=[1_000_000] * 1001, manual=[_job()] * 1001))
    for kind in ("publication", "manual"):
        assert ("zont_queue_items", 1000.0, {"kind": kind, "status": "pending"}) in events
        assert ("zont_queue_truncated", 1.0, {"kind": kind}) in events
        assert ("zont_queue_snapshot_success", 1.0, {"kind": kind}) in events
    assert all(value == 0 for name, value, _ in events if name == "zont_queue_oldest_timestamp_seconds")


@pytest.mark.parametrize("failure", ["publication_query", "manual_checkpoint"])
def test_failures_emit_current_failed_attempt_without_false_empty_count(failure):
    app = _app()
    kind = "publication" if failure == "publication_query" else "manual"
    if failure == "publication_query":
        app.db.storage.execute.side_effect = [RuntimeError("private query"), [SimpleNamespace(rows=[])]]
    else:
        app.db.storage.execute.side_effect = [
            [SimpleNamespace(rows=[])], [SimpleNamespace(rows=[SimpleNamespace(checkpoint="invalid")])],
        ]
    events = _measure(app)
    assert ("zont_queue_snapshot_success", 0.0, {"kind": kind}) in events
    assert any(name == "zont_queue_observed_timestamp_seconds" and labels == {"kind": kind}
               for name, _, labels in events)
    assert not any(name == "zont_queue_items" and labels["kind"] == kind for name, _, labels in events)
    assert "private" not in repr(events)


def test_near_deadline_skips_queries_and_marks_both_kinds_unavailable(monkeypatch):
    app = _app()
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: 93)
    events = []
    with capture(events.append):
        monitoring.queues(app, deadline=100)
    app.db.storage.execute.assert_not_called()
    for kind in ("publication", "manual"):
        assert ("zont_queue_snapshot_success", 0.0, {"kind": kind}) in events
        assert any(name == "zont_queue_observed_timestamp_seconds" and labels == {"kind": kind}
                   for name, _, labels in events)
    assert not any(name == "zont_queue_items" for name, _, _ in events)


def test_sampling_runs_after_heavy_lease_release_and_reuses_publication_result(monkeypatch):
    app = SimpleNamespace(db=SimpleNamespace(close=Mock(), set_app_meta=Mock()))
    monkeypatch.setattr(user_jobs, "open_runtime", Mock(return_value=app))
    heavy = Mock()
    monkeypatch.setattr(user_jobs.HeavyWorkLease, "acquire", Mock(return_value=heavy))
    monkeypatch.setattr(user_jobs, "drain", Mock(return_value={"processed": 0, "jobs": []}))
    publication = {"pending_reports": 2, "pending_oldest_timestamp_seconds": 42}
    monkeypatch.setattr("zont_analyzer.application.publication.publish_reports", Mock(return_value=publication))

    def sample(runtime, result, **kwargs):
        assert runtime is app and result is publication and "deadline" in kwargs
        heavy.release.assert_called_once()
        app.db.close.assert_not_called()

    monkeypatch.setattr(monitoring, "queues", sample)
    assert user_jobs.execute({})["publication"] == publication
    app.db.close.assert_called_once()


@pytest.mark.parametrize("busy", [True, False])
def test_maintenance_samples_queues_even_when_busy_or_publication_fails(monkeypatch, busy):
    app = SimpleNamespace(db=SimpleNamespace(close=Mock(), set_app_meta=Mock()))
    monkeypatch.setattr(user_jobs, "open_runtime", Mock(return_value=app))
    heavy = Mock()
    monkeypatch.setattr(user_jobs.HeavyWorkLease, "acquire", Mock(return_value=None if busy else heavy))
    monkeypatch.setattr(user_jobs, "drain", Mock(return_value={"processed": 0, "jobs": []}))
    monkeypatch.setattr("zont_analyzer.application.publication.publish_reports",
                        Mock(side_effect=RuntimeError("failed publication")))
    sample = Mock()
    monkeypatch.setattr(monitoring, "queues", sample)
    if busy:
        assert user_jobs.execute({})["status"] == "busy"
    else:
        with pytest.raises(RuntimeError, match="failed publication"):
            user_jobs.execute({})
    assert sample.call_count == 1 and sample.call_args.args == (app, None)
    assert "deadline" in sample.call_args.kwargs
    app.db.close.assert_called_once()
    app.db.set_app_meta.assert_not_called()


@pytest.mark.ydb
def test_queue_queries_match_native_indexes_and_exclude_done_and_other_jobs(tmp_path):
    from tests.ydb_support import make_database

    db = make_database(tmp_path)
    db.storage.execute(
        "UPSERT INTO publication_items(href,dirty,queued_at) "
        "VALUES ('pending',1,2000000),('clean',0,1000000);",
    )
    for key, state, payload in (
        ("m5:regenerate:pending", "released", {"status": "queued"}),
        ("m5:review", "released", {"status": "reconciliation_required"}),
        ("m5:regenerate:done", "done", {"status": "success"}),
        ("scheduler", "active", {"status": "running"}),
    ):
        db.storage.execute(
            "DECLARE $key AS Utf8; DECLARE $state AS Utf8; DECLARE $checkpoint AS Utf8; "
            "UPSERT INTO jobs(job_key,state,lease_until,checkpoint) VALUES ($key,$state,0,$checkpoint);",
            {"$key": key, "$state": state, "$checkpoint": json.dumps(payload)},
        )
    events = _measure(SimpleNamespace(db=db))
    assert ("zont_queue_items", 1.0, {"kind": "publication", "status": "pending"}) in events
    assert ("zont_queue_oldest_timestamp_seconds", 2.0, {"kind": "publication"}) in events
    assert ("zont_queue_items", 1.0, {"kind": "manual", "status": "pending"}) in events
    assert ("zont_queue_items", 1.0, {"kind": "manual", "status": "blocked"}) in events
    assert ("zont_queue_items", 0.0, {"kind": "manual", "status": "running"}) in events
    assert len(db.storage.execute("SELECT job_key FROM jobs;")[0].rows) == 4


@pytest.mark.ydb
def test_publisher_returns_oldest_from_committed_pending_items_and_clears_on_drain(tmp_path):
    from tests.unit.test_incremental_publication import _daily_reports, _runtime
    from zont_analyzer.application.publication import publish_reports

    app = _runtime(tmp_path)
    _daily_reports(app, 2)
    result = publish_reports(app, batch_size=1)
    rows = app.db.storage.execute(
        "SELECT queued_at FROM publication_items VIEW by_queue WHERE dirty>0;",
    )[0].rows
    assert result["pending_reports"] == len(rows) > 0
    assert result["pending_oldest_timestamp_seconds"] == min(row.queued_at for row in rows) / 1_000_000
    while result["pending_reports"]:
        result = publish_reports(app, batch_size=1)
    assert result["pending_oldest_timestamp_seconds"] is None
    assert publish_reports(app)["pending_oldest_timestamp_seconds"] is None

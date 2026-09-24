"""Report transactions and owner feedback against an isolated real YDB."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest

from zont_analyzer.adapters.ydb.reports import ReportRepository
from zont_analyzer.domain.models import QualityResult, Recommendation, Report


def _report(day: int = 1, *, report_id: str | None = None) -> Report:
    start = datetime(2026, 1, day, tzinfo=UTC)
    return Report(
        id=report_id or uuid4().hex,
        kind="daily",
        period_start=start,
        period_end=start + timedelta(days=1),
        generated_at=start + timedelta(days=1, minutes=1),
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
            implausible_jumps=0, sample_count=1,
        ),
        summary="first summary",
        recommendations=[Recommendation(
            title="Check schedule", category="safe_user_setting", priority="low",
            confidence=0.8, hypothesis="Schedule could be improved",
            suggested_manual_action="Observe", expected_effect="More comfort",
            observation_period_days=7,
        )],
    )


def test_report_atomic_save_idempotence_feedback_and_publication(
    ydb_database: object,
) -> None:
    repo = ReportRepository(ydb_database, clock=lambda: 1_000_000)  # type: ignore[arg-type]
    report = _report()
    assert repo.save_report(report, "rendered") == 1
    assert repo.save_report(report, "rendered") == 1
    with pytest.raises(ValueError, match="telemetry changed"):
        repo.save_report(report, "rendered", telemetry_scope="device:1", telemetry_revision=5)
    loaded = repo.report(report.id)
    assert loaded is not None
    assert loaded.summary == report.summary
    assert loaded.recommendations[0].id == f"rec:{report.id}:1"
    recommendation_id = loaded.recommendations[0].id
    assert recommendation_id is not None
    old = repo.feedback(recommendation_id)
    assert old is not None and old.status == "new"
    experiment = {"category": "settings", "parameter": "target", "before": 19,
                  "after": 20, "control_snapshot": {"fields": {"target": 19}}}
    changed = repo.set_feedback(recommendation_id, "applied", "Owner note", experiment,
                                expected_updated_at=old.updated_at)
    assert changed.status == "applied"
    assert changed.experiment == experiment
    assert repo.set_feedback(recommendation_id, "applied", "Owner note", experiment) == changed
    with pytest.raises(ValueError, match="stale"):
        repo.set_feedback(recommendation_id, "rejected", "other", expected_updated_at=old.updated_at)

    revised = report.model_copy(deep=True)
    revised.summary = "new analysis"
    assert repo.save_report(revised, "new rendered", expected_revision=1) == 2
    preserved = repo.feedback(recommendation_id)
    assert preserved is not None
    assert preserved.status == "applied"
    assert preserved.note == "Owner note"
    assert preserved.experiment == experiment
    assert len(repo.feedback_history()) == 1

    db = ydb_database  # type: ignore[assignment]
    assert len(db.execute("SELECT * FROM recommendation_audit;")[0].rows) == 1
    outbox = db.execute("SELECT * FROM notification_outbox;")[0].rows
    assert len(outbox) == 1
    assert outbox[0].payload == "rendered"
    publication = db.execute("SELECT * FROM publication_changes;")[0].rows
    assert len(publication) == 1
    assert publication[0].revision == 2
    row = db.execute("SELECT period_start, period_end FROM reports;")[0].rows[0]
    assert row.period_start == int(report.period_start.timestamp())
    assert row.period_end == int(report.period_end.timestamp())


def test_report_rolls_back_on_recommendation_collision_and_stale_telemetry(
    ydb_database: object,
) -> None:
    repo = ReportRepository(ydb_database)  # type: ignore[arg-type]
    first = _report(1)
    repo.save_report(first, "first")
    existing = repo.report(first.id)
    assert existing is not None
    rec_id = existing.recommendations[0].id
    assert rec_id is not None
    second = _report(2)
    second.recommendations[0].id = rec_id
    with pytest.raises(ValueError, match="recommendation ID"):
        repo.save_report(second, "second")
    assert repo.report(second.id) is None
    assert len(ydb_database.execute("SELECT * FROM notification_outbox;")[0].rows) == 1  # type: ignore[attr-defined]

    second.recommendations[0].id = uuid4().hex
    with pytest.raises(ValueError, match="telemetry changed"):
        repo.save_report(second, "second", telemetry_scope="device:1", telemetry_revision=5)
    assert repo.report(second.id) is None


def test_growing_season_moves_one_report_with_revision_cas(ydb_database: object) -> None:
    repo = ReportRepository(ydb_database)  # type: ignore[arg-type]
    first = _report().model_copy(update={"kind": "seasonal"})
    assert repo.save_report(first, "first window") == 1
    expanded = first.model_copy(deep=True)
    expanded.period_end += timedelta(days=7)
    expanded.generated_at += timedelta(days=7)
    assert repo.save_report(expanded, "second window", expected_revision=1) == 2
    rows = ydb_database.execute("SELECT id,period_start,period_end,revision FROM reports;")[0].rows  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert rows[0].id == first.id and rows[0].revision == 2
    assert rows[0].period_end == int(expanded.period_end.timestamp())
    assert repo.report(first.id).period_end == expanded.period_end
    with pytest.raises(ValueError, match="stale report revision"):
        repo.save_report(expanded.model_copy(update={"summary": "stale writer"}), "stale", expected_revision=1)
    assert len(ydb_database.execute("SELECT id FROM reports;")[0].rows) == 1  # type: ignore[attr-defined]


def test_season_move_preserves_period_and_id_collision_guards(ydb_database: object) -> None:
    repo = ReportRepository(ydb_database)  # type: ignore[arg-type]
    first = _report().model_copy(update={"kind": "seasonal"})
    repo.save_report(first, "first")
    later = first.model_copy(deep=True)
    later.period_end += timedelta(days=7)
    other = _report(report_id="other-season").model_copy(update={"kind": "seasonal",
                                                          "period_end": later.period_end})
    repo.save_report(other, "other")
    with pytest.raises(ValueError, match="period already belongs"):
        repo.save_report(later, "collision", expected_revision=1)
    changed_start = first.model_copy(update={"period_start": first.period_start + timedelta(hours=1)})
    with pytest.raises(ValueError, match="ID already belongs"):
        repo.save_report(changed_start, "wrong start", expected_revision=1)
    daily = _report(2)
    repo.save_report(daily, "daily")
    with pytest.raises(ValueError, match="ID already belongs"):
        repo.save_report(daily.model_copy(update={"period_end": daily.period_end + timedelta(days=1)}),
                         "daily moved", expected_revision=1)
    assert len(ydb_database.execute("SELECT id FROM reports;")[0].rows) == 3  # type: ignore[attr-defined]


def test_feedback_audit_keeps_each_decision_and_pages_by_id(ydb_database: object) -> None:
    repo = ReportRepository(ydb_database, clock=lambda: 500)  # type: ignore[arg-type]
    report = _report()
    repo.save_report(report, "rendered")
    loaded = repo.report(report.id)
    assert loaded is not None
    rec_id = loaded.recommendations[0].id
    assert rec_id is not None
    repo.set_feedback(rec_id, "applied", "First note", {"before": 19, "after": 20})
    repo.set_feedback(rec_id, "rejected", "Changed mind")

    first_page = repo.feedback_audit(rec_id, limit=1)
    assert len(first_page) == 1
    assert first_page[0].payload == {
        "status": "applied", "note": "First note",
        "experiment": {"before": 19, "after": 20},
    }
    second_page = repo.feedback_audit(rec_id, after_id=first_page[0].id, limit=1)
    assert len(second_page) == 1
    assert second_page[0].id > first_page[0].id
    assert second_page[0].payload == {
        "status": "rejected", "note": "Changed mind", "experiment": None,
    }
    assert repo.feedback_audit(rec_id, after_id=second_page[0].id) == []
    assert repo.feedback_audit(limit=0) == []


def test_concurrent_cas_and_ordered_bounded_history(ydb_database: object) -> None:
    repo = ReportRepository(ydb_database)  # type: ignore[arg-type]
    reports = [_report(day) for day in (1, 2, 3)]
    for report in reports:
        assert repo.save_report(report, report.id) == 1
    before = datetime(2026, 1, 5, tzinfo=UTC)
    assert [r.id for r in repo.prior_reports(before, limit=2)] == [reports[2].id, reports[1].id]
    assert [r.id for r in repo.completed_reports(before, limit=2)] == [reports[0].id, reports[1].id]
    assert repo.prior_reports(before, limit=0) == []

    barrier = Barrier(2)

    def revise(index: int) -> bool:
        variant = reports[0].model_copy(deep=True)
        variant.summary = f"revision {index}"
        barrier.wait()
        try:
            return repo.save_report(variant, f"render {index}", expected_revision=1) == 2
        except ValueError as exc:
            assert "stale report revision" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(revise, (1, 2))) == 1
    assert repo.report(reports[0].id).summary in {"revision 1", "revision 2"}  # type: ignore[union-attr]


def test_report_rejects_changed_source_revision(ydb_database: object) -> None:
    from zont_analyzer.adapters.ydb.application import Database

    db = Database(ydb_database)  # type: ignore[arg-type]
    revision = db.source_revision()
    report = _report()
    db.telemetry.save_devices([{"id": "changed-device"}])
    with pytest.raises(ValueError, match="inputs changed"):
        db.save_report(report, "stale result", source_revision=revision)
    assert db.report(report.id) is None
    db.save_report(report, "current result", source_revision=db.source_revision())
    assert db.report(report.id) is not None


def test_report_commit_rejects_expired_job_attempt(ydb_database: object) -> None:
    from zont_analyzer.adapters.ydb.jobs import JobLeaseRepository

    now = [1_000_000]
    jobs = JobLeaseRepository(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    reports = ReportRepository(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    first = jobs.acquire("daily-job", "first", lease_seconds=1)
    assert first is not None
    now[0] += 2_000_000
    second = jobs.acquire("daily-job", "second", lease_seconds=60)
    assert second is not None
    assert jobs.checkpoint("daily-job", "second", second.attempt, json.dumps({
        "phase": "analyze", "input_fingerprint": "input-v1", "source_revision": 0,
    }))
    report = _report()
    with pytest.raises(ValueError, match="job ownership expired"):
        reports.save_report(report, "stale", job_fence=("daily-job", "first", first.attempt))
    assert reports.report(report.id) is None
    reports.save_report(report, "current", source_revision=0,
                        job_fence=("daily-job", "second", second.attempt))
    saved = jobs.get("daily-job")
    assert saved is not None
    assert json.loads(saved.checkpoint or "{}").get("phase") == "saved"
    with pytest.raises(ValueError, match="job ownership expired"):
        reports.save_report(report, "current", job_fence=("daily-job", "first", first.attempt))

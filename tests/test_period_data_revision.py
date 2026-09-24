from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import delete_samples, make_database, seed_events, seed_samples
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.domain import MetricValue, QualityResult, Report, SourceEvent, TelemetryPoint

EMPTY_REVISION = hashlib.sha256(b"[]").hexdigest()


def _point(timestamp: datetime, *, entity: str = "room", value: float = 21.0) -> TelemetryPoint:
    return TelemetryPoint(
        device_id="1",
        source_type="synthetic",
        entity_id=entity,
        metric_key="temperature",
        timestamp_utc=timestamp,
        value_num=value,
        unit="°C",
    )


def _database(tmp_path: Path) -> Database:
    return make_database(tmp_path)


def _report(report_id: str, end: datetime, *, context: dict | None = None) -> Report:
    return Report(
        id=report_id, kind="daily", period_start=end - timedelta(days=1), period_end=end,
        generated_at=end, timezone="UTC", algorithm_version="test",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
        metrics=[MetricValue(id="metric-1", name="fixture", value=1, unit="count")],
        context=context or {}, summary="fixture report",
    )


@pytest.mark.ydb
def test_period_revision_is_exact_at_utc_boundaries_and_keeps_empty_sentinel(tmp_path: Path) -> None:
    database = _database(tmp_path)
    # September 7 local day in Samara (UTC+4).
    start = datetime(2026, 9, 6, 20, tzinfo=UTC)
    end = start + timedelta(days=1)

    assert database.period_data_revision(start, end) == EMPTY_REVISION

    seed_samples(database, [_point(start + timedelta(hours=1))])
    revision = database.period_data_revision(start, end)
    assert revision.startswith("telemetry-v2:")

    # It shares a UTC day with the period, but is outside [start, end).
    seed_samples(database, [
        _point(start - timedelta(seconds=1), entity="before", value=5.0),
        _point(end, entity="boundary", value=5.0),
        _point(end + timedelta(hours=1), entity="outside", value=5.0),
    ])
    assert database.period_data_revision(start, end) == revision
    seed_samples(database, [_point(end - timedelta(seconds=1), entity="inside", value=5.0)])
    assert database.period_data_revision(start, end) != revision


@pytest.mark.ydb
def test_warm_revision_cache_does_not_scan_telemetry_and_ignores_identical_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 7, tzinfo=UTC)
    end = start + timedelta(days=1)
    point = _point(start)
    seed_samples(database, [point])
    expected = database.period_data_revision(start, end)
    seed_samples(database, [point])
    monkeypatch.setattr(database, "_samples", lambda *_args, **_kwargs: pytest.fail("warm revision scanned samples"))
    assert database.period_data_revision(start, end) == expected


@pytest.mark.ydb
def test_period_revision_includes_all_sample_content_and_deletions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    point = _point(start + timedelta(hours=1))
    seed_samples(database, [point])
    original = database.period_data_revision(start, end)

    seed_samples(database, [point.model_copy(update={"value_num": 22.0})])
    corrected = database.period_data_revision(start, end)
    assert corrected != original

    seed_samples(database, [
        point.model_copy(update={"entity_id": "state", "value_num": None, "value_text": "on", "quality": "invalid"})
    ])
    with_text_and_quality = database.period_data_revision(start, end)
    assert with_text_and_quality != corrected

    assert delete_samples(database, start, end) == 2
    assert database.period_data_revision(start, end) != with_text_and_quality


@pytest.mark.ydb
def test_legacy_period_revision_remains_available_for_lazy_rollout(tmp_path: Path) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)

    before = database.legacy_period_data_revision(start, end)
    seed_samples(database, [_point(start)])
    assert database.legacy_period_data_revision(start, end) != before


@pytest.mark.ydb
def test_source_event_revision_includes_all_prior_semantic_content(tmp_path: Path) -> None:
    database = _database(tmp_path)
    boundary = datetime(2026, 9, 2, tzinfo=UTC)
    original = database.source_event_revision(boundary)
    event = SourceEvent(
        id="event-1",
        device_id="1",
        event_type="PowerOn",
        timestamp_utc=boundary - timedelta(days=2),
        details={"reason": "restored"},
    )
    seed_events(database, [event])
    changed = database.source_event_revision(boundary)

    assert changed != original
    assert database.source_event_revision(boundary - timedelta(days=3)) == original
    seed_events(database, [event.model_copy(update={"important": True})])
    assert database.source_event_revision(boundary) != changed


@pytest.mark.ydb
def test_legacy_event_baselines_seed_from_report_metadata_without_loading_json(tmp_path: Path) -> None:
    database = _database(tmp_path)
    period_end = datetime(2026, 9, 2, tzinfo=UTC)
    report = _report("legacy-report", period_end)
    database.save_report(report, report.summary)

    assert database.seed_source_event_report_baselines() == 1
    assert database.get_app_meta("source-event-report-baseline:v1:legacy-report") == (
        database.source_event_revision(period_end)
    )
    assert database.seed_source_event_report_baselines() == 0


@pytest.mark.ydb
def test_upgrade_report_telemetry_revision_is_compare_and_swap_metadata_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    generated_at = datetime(2026, 9, 2, tzinfo=UTC)
    original = _report("report-1", generated_at, context={
        "input_revision": {"telemetry": "legacy", "weather": "unchanged"},
        "ai": {"response": "preserve"},
    })
    database.save_report(original, original.summary)

    assert database.upgrade_report_telemetry_revision("report-1", "legacy", "telemetry-v2:exact")
    stored = database.report("report-1")
    assert stored is not None
    assert stored.context["input_revision"] == {"telemetry": "telemetry-v2:exact", "weather": "unchanged"}
    assert stored.context["ai"] == original.context["ai"]
    assert stored.metrics == original.metrics
    assert not database.upgrade_report_telemetry_revision("report-1", "legacy", "other")
    assert database.report("report-1").generated_at == generated_at

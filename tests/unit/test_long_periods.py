from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.long_periods import aggregate_long_period
from zont_analyzer.domain import DetectedEvent, MetricValue, QualityResult, Report
from zont_analyzer.domain.periods import Period


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    return db


def _period(start: datetime, end: datetime, *, timezone: str = "UTC") -> Period:
    return Period(
        kind="seasonal",
        start=start,
        end=end,
        observed_end=end,
        timezone=timezone,
        complete=True,
        season="spring",
        year=start.year,
    )


def _daily(
    start: datetime,
    end: datetime,
    *,
    count: float,
    outdoor: float,
    ratio: float,
    denominator: float,
    quality: float = 0.8,
    summary: str = "DAILY AI TEXT MUST NOT BE COPIED",
) -> Report:
    stamp = int(start.timestamp())
    return Report(
        id=f"report:daily:{stamp}:report-v2",
        kind="daily",
        period_start=start,
        period_end=end,
        generated_at=end,
        timezone="America/New_York" if (end - start) != timedelta(days=1) else "UTC",
        quality=QualityResult(
            score=quality,
            coverage_pct=quality * 100,
            max_gap_seconds=300,
            stuck_pct=2,
            implausible_jumps=1,
            sample_count=100,
        ),
        metrics=[
            MetricValue(id=f"count:{stamp}", name="dhw_episode_count", value=count, unit="count"),
            MetricValue(
                id=f"outdoor:{stamp}", name="outdoor_mean_temperature_c", value=outdoor, unit="°C"
            ),
        ],
        events=[
            DetectedEvent(
                id=f"event:{stamp}", kind="dhw_reheat_episode", started_at=start, ended_at=start
            )
        ],
        context={
            "sensors": {"control": "room"},
            "dhw_interaction": {"current_mode": "comfort"},
            "reliability": {"uptime": "daily-only"},
            "temporal_evidence": {
                "signals": {
                    "control_temperature": {"role": "control_temperature"},
                    "outdoor_temperature": {"role": "outdoor_temperature"},
                },
                "quality": {
                    "control_temperature": {"coverage_pct": quality * 100},
                    "outdoor_temperature": {"coverage_pct": quality * 100},
                },
                "metrics": [
                    {
                        "id": f"ratio:{stamp}",
                        "name": "burner_runtime_request_ratio",
                        "value": ratio,
                        "unit": "ratio",
                        "source": "derived",
                        "denominator": denominator,
                        "denominator_unit": "active_request_seconds",
                        "coverage_pct": quality * 100,
                    },
                    {
                        "id": f"cycle:{stamp}",
                        "name": "burner_cycle_median_seconds",
                        "value": 300 + count,
                        "unit": "seconds",
                        "source": "derived",
                    },
                ],
                "windows": [],
            },
        },
        summary=summary,
        ai_used=True,
    )


def _save(db: Database, report: Report) -> None:
    db.save_report(report, report.summary)


def test_aggregates_known_totals_weighted_means_and_denominator_rates(tmp_path: Path) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 3, 1, tzinfo=UTC)
    for offset, values in enumerate(((2, -4, 0.25, 100), (3, 2, 0.75, 300))):
        left = start + timedelta(days=offset)
        _save(db, _daily(left, left + timedelta(days=1), count=values[0], outdoor=values[1],
                         ratio=values[2], denominator=values[3]))

    report = aggregate_long_period(db, _period(start, start + timedelta(days=2)))

    metrics = {item.name: item for item in report.metrics}
    assert metrics["dhw_episode_count"].value == 5
    assert metrics["dhw_episode_count"].context["aggregation"] == "sum_of_known_daily_values"
    assert metrics["outdoor_mean_temperature_c"].value == -1
    evidence = {item["name"]: item for item in report.context["temporal_evidence"]["metrics"]}
    assert evidence["burner_runtime_request_ratio"]["value"] == pytest.approx(0.625)
    assert evidence["burner_runtime_request_ratio"]["denominator"] == 400
    daily = report.context["long_period_aggregation"]["daily_statistics"]
    assert daily["burner_cycle_median_seconds"]["median_of_daily_values"] == 302.5
    assert "not a seasonal sample quantile" in daily["burner_cycle_median_seconds"]["scope"]
    assert report.events[0].kind == "period_event_summary"
    assert report.events[0].details["original_kind"] == "dhw_reheat_episode"
    assert report.events[0].details["count"] == 2
    assert "DAILY AI TEXT" not in report.summary
    assert report.recommendations == []
    assert report.ai_used is False


def test_missing_days_reduce_coverage_without_adding_fake_zero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 4, 1, tzinfo=UTC)
    _save(db, _daily(start, start + timedelta(days=1), count=4, outdoor=8, ratio=0.5, denominator=100))

    report = aggregate_long_period(db, _period(start, start + timedelta(days=3)))

    aggregation = report.context["long_period_aggregation"]
    assert aggregation["expected_days"] == 3
    assert aggregation["included_days"] == 1
    assert aggregation["missing_dates"] == ["2026-04-02", "2026-04-03"]
    assert {item.name: item.value for item in report.metrics}["dhw_episode_count"] == 4
    assert report.quality.coverage_pct == pytest.approx(80 / 3, abs=0.01)
    assert "missing_daily_reports" in report.quality.flags


def test_expected_duration_uses_real_dst_local_days_and_windows_are_bounded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 3, 1, 5, tzinfo=UTC)
    end = datetime(2026, 3, 21, 4, tzinfo=UTC)
    period = _period(start, end, timezone="America/New_York")
    day = start
    for index in range(20):
        next_day = day + timedelta(days=1)
        if index == 7:  # 2026-03-08 is the 23-hour spring-forward day.
            next_day -= timedelta(hours=1)
        _save(db, _daily(day, next_day, count=1, outdoor=index, ratio=0.5, denominator=100))
        day = next_day

    report = aggregate_long_period(db, period)

    aggregation = report.context["long_period_aggregation"]
    assert aggregation["expected_days"] == 20
    assert aggregation["expected_duration_seconds"] == 20 * 86400 - 3600
    assert len(report.context["temporal_evidence"]["windows"]) == 12


def test_empty_period_is_explicit_and_contains_no_synthetic_metrics(tmp_path: Path) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 5, 1, tzinfo=UTC)

    report = aggregate_long_period(db, _period(start, start + timedelta(days=2)))

    assert report.metrics == []
    assert report.events == []
    assert report.quality.score == 0
    assert report.quality.coverage_pct == 0
    assert report.quality.sample_count == 0
    assert report.context["long_period_aggregation"]["missing_dates"] == ["2026-05-01", "2026-05-02"]
    assert report.context["latest_daily_context"]["source_report_id"] is None
    assert "no_completed_daily_reports" in report.quality.flags

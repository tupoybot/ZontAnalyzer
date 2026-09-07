from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.domain import QualityResult, Report, TelemetryPoint
from zont_analyzer.domain.periods import calendar_period
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.presentation import kpis, period_target
from zont_analyzer.runtime import build_runtime


@pytest.mark.parametrize("kind", ["weekly", "monthly", "seasonal"])
def test_period_mean_uses_historical_duration_not_last_snapshot_or_present_override(
    tmp_path: Path, kind: str,
) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.home.timezone = "UTC"
    runtime.config.preferences.target_temperature_c = 26
    analysis = AnalysisService(runtime.db, runtime.config)
    if kind == "seasonal":
        period = analysis.seasonal_period(2026, "autumn", as_of=datetime(2026, 9, 7, tzinfo=UTC))
    else:
        period = calendar_period(kind, date(2026, 8, 31), "UTC")
    hours = int((period.observed_end - period.start).total_seconds() / 3600)
    points = [TelemetryPoint(
        device_id="1", source_type="synthetic", entity_id="target", metric_key="temperature",
        timestamp_utc=period.start + timedelta(hours=hour), value_num=20 if hour < 48 else 24, unit="°C",
    ) for hour in range(hours)]
    # A snapshot at the next period's boundary must not affect this period.
    points.append(points[-1].model_copy(update={"timestamp_utc": period.observed_end, "value_num": 30}))
    runtime.db.upsert_samples(points, {"target": "target_temperature"})

    report = analysis.analyze_period(period, use_ai=False)
    expected = (20 * 48 + 24 * (hours - 48)) / hours
    assert report.context["period_target_mean_c"] == pytest.approx(expected, abs=.0001)
    assert report.context["period_target_coverage_pct"] == 100
    assert report.context["current_target_c"] == 24
    assert "Цель · средняя за период" in kpis(report)
    assert "Средняя целевая температура за период" in render_text(report)


def test_late_single_snapshot_cannot_become_whole_period_mean(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.home.timezone = "UTC"
    analysis = AnalysisService(runtime.db, runtime.config)
    period = calendar_period("weekly", date(2026, 8, 31), "UTC")
    runtime.db.upsert_samples([TelemetryPoint(
        device_id="1", source_type="synthetic", entity_id="target", metric_key="temperature",
        timestamp_utc=period.observed_end - timedelta(hours=1), value_num=24, unit="°C",
    )], {"target": "target_temperature"})
    report = analysis.analyze_period(period, use_ai=False)
    assert report.context["current_target_c"] == 24
    assert period_target(report) == (None, 0)
    assert "Нет данных" in kpis(report)


def _report(kind: str, context: dict) -> Report:
    return Report(
        id="target-test", kind=kind, period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC), generated_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="UTC", quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0,
        stuck_pct=0, implausible_jumps=0, sample_count=2), context=context, summary="Synthetic report",
    )


def test_legacy_period_uses_saved_weighted_mean_and_daily_retains_last_value() -> None:
    context = {"current_target_c": 24, "current_mode": {"name": "Ремонт"}, "temporal_evidence": {
        "quality": {"target_temperature": {"mean": 20.1208, "coverage_pct": 80}},
    }}
    report = _report("monthly", context)
    assert period_target(report) == (20.1208, 80)
    assert "20,1" in kpis(report)
    assert "По 80% периода" in kpis(report)
    assert "Средняя цель за период" in render_html(report)
    assert period_target(_report("daily", context)) == (24, None)
    assert "Цель · на конец периода" in kpis(_report("daily", context))
    assert period_target(_report("seasonal", {"current_target_c": 30})) == (None, 0)

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.ydb_support import make_database
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.gas import GasService
from zont_analyzer.application.gas_aggregation import DailyGasAggregation, reprice_aggregate
from zont_analyzer.application.gas_cost import calculate_gas_cost
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.domain.periods import Period, calendar_period, midnight
from zont_analyzer.reports.presentation import gas_purpose_text

JANUARY = {"id": "jan", "effective_from": "2026-01-01T00:00:00+00:00", "currency": "RUB", "price": "8"}
FEBRUARY = {"id": "feb", "effective_from": "2026-02-01T00:00:00+00:00", "currency": "USD", "price": "2"}


def _day(
    date_value: date, volume: float | None, *, zone: str = "UTC", model: str = "model-v1",
    scope: str = "boiler", tariffs: list[dict[str, Any]] | None = None,
) -> Report:
    window = calendar_period("daily", date_value, zone)
    split = {
        "scope": "modelled_boiler", "status": "estimated",
        "total_modelled_m3": volume, "allocated_observed_m3": volume, "unallocated_m3": 0,
        "components": {
            "heating": {"volume_m3": volume * .75 if volume is not None else None},
            "dhw": {"volume_m3": volume * .25 if volume is not None else None},
            "purpose_unknown": {"volume_m3": 0},
        },
    }
    return Report(
        id=AnalysisService.report_id_for("daily", window.start), kind="daily",
        period_start=window.start, period_end=window.end, generated_at=window.end,
        timezone=zone,
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=0),
        summary="Test day",
        context={
            "gas": {
                "status": "estimated" if volume is not None else "unknown",
                "scope": scope, "volume_m3": volume, "model_version": model, "algorithm_version": "daily-v1",
                "coverage_pct": 100,
                "lower_m3": volume * .75 if volume is not None else None,
                "upper_m3": volume * 1.25 if volume is not None else None,
                "flame_hours": 2, "heating_flame_hours": 1.5, "dhw_flame_hours": .5,
                "purpose_unknown_flame_hours": 0,
                "purpose_split": split if volume is not None else {},
                "cost": calculate_gas_cost(window.start, window.end, volume, tariffs or [], timezone=zone),
            },
            "gas_savings": {"status": "unknown", "comparisons": []},
        },
    )


def test_aggregation_keeps_missing_days_saved_cost_and_calibration_versions() -> None:
    period = calendar_period("weekly", date(2026, 1, 26), "UTC")
    aggregation = DailyGasAggregation(period)
    first = _day(date(2026, 1, 26), 2, tariffs=[JANUARY])
    second = _day(date(2026, 1, 27), 0, tariffs=[JANUARY])
    last = _day(date(2026, 2, 1), 3, model="model-v2", tariffs=[JANUARY, FEBRUARY])
    preview = _day(date(2026, 1, 28), 100)
    preview.generated_at = preview.period_end - timedelta(hours=1)
    no_gas = _day(date(2026, 1, 29), None)
    no_gas.context.pop("gas")
    for row in (first, second, last, preview, no_gas):
        aggregation.include(row)

    gas, savings = aggregation.contexts()
    assert gas["status"] == "partial"
    assert gas["volume_m3"] == 5
    assert gas["lower_m3"] == 3.75 and gas["upper_m3"] == 6.25
    assert gas["average_daily_m3"] is None
    assert gas["coverage_pct"] == pytest.approx(300 / 7)
    assert gas["daily"][1]["volume_m3"] == 0
    assert gas["daily"][2]["volume_m3"] is None
    assert gas["aggregation"]["available_days"] == 3
    assert gas["aggregation"]["unavailable_days"] == ["2026-01-29"]
    assert gas["aggregation"]["missing_report_days"] == ["2026-01-28", "2026-01-30", "2026-01-31"]
    assert gas["model_version"] is None
    assert gas["aggregation"]["model_versions"] == [{"days": 2, "version": "model-v1"},
                                                     {"days": 1, "version": "model-v2"}]
    assert gas["cost"]["status"] == "partial"
    assert gas["cost"]["amounts"] == [{"currency": "RUB", "amount": "16"}, {"currency": "USD", "amount": "6"}]
    assert [(row["tariff_id"], row["volume_m3"]) for row in gas["cost"]["slices"]] == [("jan", 2), ("feb", 3)]
    purpose = gas["purpose_split"]
    assert purpose["status"] == "partial"
    assert purpose["components"]["heating"]["volume_m3"] == 3.75
    assert purpose["components"]["dhw"]["volume_m3"] == 1.25
    assert purpose["components"]["purpose_unknown"]["volume_m3"] == 0
    assert savings["source_report_id"] == last.id

    reprice_aggregate(gas, [{**JANUARY, "price": "9"}, FEBRUARY], timezone="UTC")
    assert gas["cost"]["amounts"] == [{"currency": "RUB", "amount": "18"}, {"currency": "USD", "amount": "6"}]
    assert gas["volume_m3"] == 5 and gas["model_version"] is None


def test_dst_duration_and_mixed_scope_subtotals() -> None:
    zone = "America/New_York"
    start = midnight(date(2026, 3, 7), zone)
    end = midnight(date(2026, 3, 10), zone)
    period = Period(kind="seasonal", start=start, end=end, observed_end=end, timezone=zone, complete=True)
    aggregation = DailyGasAggregation(period)
    aggregation.include(_day(date(2026, 3, 7), 2, zone=zone))
    aggregation.include(_day(date(2026, 3, 8), 4, zone=zone, scope="whole_meter"))

    gas, _savings = aggregation.contexts()
    assert gas["period_hours"] == 71
    assert gas["coverage_pct"] == pytest.approx(47 / 71 * 100)
    assert gas["scope"] == "mixed"
    assert gas["volume_m3"] is None
    assert gas["cost"]["status"] == "unknown"
    assert [(row["scope"], row["volume_m3"]) for row in gas["aggregation"]["scope_totals"]] == [
        ("boiler", 2), ("whole_meter", 4),
    ]


def test_purpose_share_requires_a_model_total_for_every_contributing_day() -> None:
    period = calendar_period("weekly", date(2026, 1, 26), "UTC")
    aggregation = DailyGasAggregation(period)
    known = _day(date(2026, 1, 26), 2)
    partial = _day(date(2026, 1, 27), None)
    partial.context["gas"]["purpose_split"] = {
        "scope": "modelled_boiler", "status": "partial", "total_modelled_m3": None,
        "allocated_observed_m3": 3, "components": {"heating": {"volume_m3": 3}},
    }
    aggregation.include(known)
    aggregation.include(partial)

    gas, _savings = aggregation.contexts()
    split = gas["purpose_split"]
    assert split["status"] == "partial"
    assert split["total_modelled_m3"] is None
    assert split["components"]["heating"]["volume_m3"] == 4.5
    report = known.model_copy(update={"context": {"gas": gas}})
    lines = gas_purpose_text(report)
    assert "Отопление: 4,50 м³" in lines
    assert all("%" not in line for line in lines)
    # Archived snapshots can still contain an older, inconsistent subtotal.
    split["total_modelled_m3"] = 2
    assert gas_purpose_text(report) == lines


@pytest.mark.ydb
def test_analysis_reads_saved_day_gas_only_for_all_calendar_periods(tmp_path: Path, monkeypatch) -> None:
    db = make_database(tmp_path)
    day = _day(date(2026, 1, 29), 2, tariffs=[JANUARY])
    db.save_report(day, day.summary)
    wrong_boundary = day.model_copy(update={
        "id": "daily:wrong-boundary", "period_end": day.period_end - timedelta(hours=1),
        "generated_at": day.generated_at + timedelta(minutes=1),
    })
    db.save_report(wrong_boundary, wrong_boundary.summary)
    config = AppConfig()
    config.home.timezone = "UTC"
    service = AnalysisService(db, config)
    original_daily_reports = db.daily_reports
    ranges = []

    def tracked_daily_reports(start, end, *, timezone=None):
        ranges.append((start, end))
        yield from original_daily_reports(start, end, timezone=timezone)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Calendar report read current gas calibration or telemetry")

    monkeypatch.setattr(db, "daily_reports", tracked_daily_reports)
    monkeypatch.setattr(OwnerContextStore, "gas_readings_for_analysis", forbidden)
    for method in ("context", "window", "model", "savings"):
        monkeypatch.setattr(GasService, method, forbidden)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 2, 15, tzinfo=UTC)
    season = Period(kind="seasonal", start=start, end=end, observed_end=end, timezone="UTC", complete=True)
    short_season = season.model_copy(update={"end": day.period_end, "observed_end": day.period_end})
    for report in (
        service.analyze_week(2026, 5, use_ai=False),
        service.analyze_month(2026, 1, use_ai=False),
        service.analyze_period(short_season, use_ai=False),
        service.analyze_period(season, use_ai=False),
    ):
        assert ranges.count((report.period_start, report.period_end)) == 1
        assert report.context["gas"]["volume_m3"] == 2
        assert report.context["gas"]["status"] == "partial"
        assert report.context["gas"]["cost"]["amounts"] == [{"currency": "RUB", "amount": "16"}]
        assert report.context["gas_savings"]["source_report_id"] == day.id

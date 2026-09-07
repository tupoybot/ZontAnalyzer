from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import run_period_schedule, scheduled_periods
from zont_analyzer.domain import AnalysisResult
from zont_analyzer.domain.periods import SeasonBoundaries, midnight
from zont_analyzer.runtime import build_runtime


def test_empty_database_schedule_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    today = date(2026, 9, 7)

    first = run_period_schedule(runtime, analysis, today, limit=100)
    second = run_period_schedule(runtime, analysis, today, limit=100)

    assert first
    assert second == []
    assert all(item["kind"] in {"weekly", "monthly", "seasonal"} for item in first)
    assert any(item["kind"] == "seasonal" and item["complete"] is False for item in first)
    assert all(item["observed_end"] for item in first)


def test_schedule_uses_custom_owner_boundaries_and_keeps_current_season_incomplete(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.home.seasons = SeasonBoundaries(
        spring="02-15", summer="05-15", autumn="08-15", winter="11-15"
    )
    analysis = AnalysisService(runtime.db, runtime.config)
    periods = scheduled_periods(analysis, date(2026, 9, 1), date(2026, 9, 7))

    current = next(item for item in periods if item.kind == "seasonal" and item.season == "autumn")
    assert current.boundary_source == "home_config"
    assert current.complete is False
    assert current.observed_end.astimezone(ZoneInfo(runtime.config.home.timezone)).date() == date(2026, 9, 7)


def test_period_schedule_respects_limit_and_releases_work_for_next_catchup(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    today = date(2026, 9, 7)

    first = run_period_schedule(runtime, analysis, today, limit=1)
    second = run_period_schedule(runtime, analysis, today, limit=1)

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["id"] != second[0]["id"]


def test_regenerate_failure_keeps_stored_report_and_success_is_fresh_non_persisting(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(None, tmp_path)
    original = AnalysisService(runtime.db, runtime.config).analyze_daily(
        date(2026, 9, 6), use_ai=False
    )
    before = runtime.db.report(original.id).model_dump_json()
    config = runtime.config.model_copy(deep=True)
    config.openai.enabled = True

    class FailingAnalyst:
        def analyze(self, _packet: dict[str, object]) -> AnalysisResult:
            raise RuntimeError("synthetic analyst failure")

    failing = AnalysisService(runtime.db, config, analyst=FailingAnalyst())
    try:
        failing.regenerate(original, request_nonce="failure-nonce")
    except RuntimeError as exc:
        assert str(exc) == "synthetic analyst failure"
    else:
        raise AssertionError("failing analyst must abort regeneration")
    assert runtime.db.report(original.id).model_dump_json() == before

    captured: list[dict[str, object]] = []

    class SuccessfulAnalyst:
        def analyze(self, packet: dict[str, object]) -> AnalysisResult:
            captured.append(packet)
            return AnalysisResult(summary="fresh deterministic result")

    successful = AnalysisService(runtime.db, config, analyst=SuccessfulAnalyst())
    candidate = successful.regenerate(original, request_nonce="fresh-nonce")
    assert candidate.summary == "fresh deterministic result"
    assert captured and captured[0]["period"]["request_nonce"] == "fresh-nonce"
    assert runtime.db.report(original.id).model_dump_json() == before


def test_current_season_checkpoint_stays_at_monday_until_next_completed_week(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    zone = ZoneInfo(runtime.config.home.timezone)
    for day in range(7, 14):
        periods = scheduled_periods(analysis, date(2026, 9, 1), date(2026, 9, day))
        autumn = next(item for item in periods if item.kind == "seasonal" and item.season == "autumn")
        assert autumn.observed_end.astimezone(zone).date() == date(2026, 9, 7)
        assert not autumn.complete
    following = scheduled_periods(analysis, date(2026, 9, 1), date(2026, 9, 14))
    autumn = next(item for item in following if item.kind == "seasonal" and item.season == "autumn")
    assert autumn.observed_end.astimezone(zone).date() == date(2026, 9, 14)


def test_season_ends_midweek_without_waiting_and_new_season_waits_for_first_monday(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    periods = scheduled_periods(analysis, date(2026, 8, 1), date(2026, 9, 2))
    seasons = [item for item in periods if item.kind == "seasonal"]
    assert len(seasons) == 1
    assert seasons[0].season == "summer" and seasons[0].complete
    assert seasons[0].observed_end.astimezone(ZoneInfo(runtime.config.home.timezone)).date() == date(2026, 9, 1)


def test_season_ai_runs_once_per_week_despite_fresh_daily_telemetry(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from zont_analyzer.domain import TelemetryPoint

    runtime = build_runtime(None, tmp_path)
    runtime.config.analysis.minimum_quality_score = 0
    calls: list[str] = []

    class Analyst:
        def analyze(self, packet: dict) -> AnalysisResult:
            calls.append(packet["period"]["kind"])
            return AnalysisResult(summary="Synthetic period interpretation")

    analysis = AnalysisService(runtime.db, runtime.config, analyst=Analyst())
    run_period_schedule(runtime, analysis, date(2026, 9, 7), limit=100)
    assert calls.count("seasonal") == 1
    for day in range(8, 14):
        runtime.db.upsert_samples([TelemetryPoint(
            device_id="test", source_type="synthetic", entity_id="room", metric_key="temperature",
            timestamp_utc=datetime(2026, 9, day - 1, 12, tzinfo=UTC), value_num=21, unit="°C",
        )], {"room": "room_temperature"})
        result = run_period_schedule(runtime, analysis, date(2026, 9, day), limit=100)
        assert not any(item["kind"] == "seasonal" for item in result)
    run_period_schedule(runtime, analysis, date(2026, 9, 14), limit=100)
    assert calls.count("seasonal") == 2


@pytest.mark.parametrize("day", [7, 10])
def test_weekly_schedule_preserves_manual_season_result(tmp_path: Path, day: int) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    fresh = analysis.analyze_period(analysis.seasonal_period(
        2026, "autumn", as_of=midnight(date(2026, 9, day), runtime.config.home.timezone),
    ), use_ai=False)
    result = run_period_schedule(runtime, analysis, date(2026, 9, day), limit=100)
    assert not any(item["id"] == fresh.id for item in result)
    assert runtime.db.report(fresh.id).period_end == fresh.period_end
    following = run_period_schedule(runtime, analysis, date(2026, 9, 14), limit=100)
    assert any(item["id"] == fresh.id for item in following)

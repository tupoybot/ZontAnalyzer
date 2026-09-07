from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import run_period_schedule, scheduled_periods
from zont_analyzer.domain import AnalysisResult
from zont_analyzer.domain.periods import SeasonBoundaries
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

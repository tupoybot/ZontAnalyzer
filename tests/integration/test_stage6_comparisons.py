from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MethodType

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.comparison_context import build_comparison_context, window_from_report
from zont_analyzer.application.period_comparison import select_baseline
from zont_analyzer.application.period_schedule import schedule_signature
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import MetricValue, Prediction, QualityResult, Report, TelemetryPoint
from zont_analyzer.domain.periods import SeasonBoundaries, calendar_period, season_period
from zont_analyzer.reports import render_text


def _report(
    start: datetime,
    *,
    outdoor: float = -5,
    mode: str = "auto",
    dhw_pct: float = 5,
    room_error: float = 0.2,
    coverage: float = 95,
    weather_coverage: float = 95,
    target: float = 21,
    excluded: bool = False,
    report_id: str | None = None,
    with_prediction: bool = False,
) -> Report:
    end = start + timedelta(days=1)
    window = {
        "id": f"window:{start.isoformat()}",
        "started_at": start.isoformat(),
        "ended_at": end.isoformat(),
        "kind": "hour",
        "excluded_reasons": ["dhw"] if excluded else [],
        "signals": {
            "control_temperature": {"mean": 21.0, "slope_per_hour": -0.1},
            "room:bedroom": {"mean": 20.7},
            "setting:mode_id": {"mean": 1, "coverage_pct": 100},
            "target_temperature": {"mean": target, "coverage_pct": 100},
        },
        "facts": {
            "room_error_c": {"mean": room_error},
            "delta_t_c": {"mean": 10.0},
            "dhw_pct": {"mean": dhw_pct},
            "heating_share_pct": {"mean": 55.0},
            "flame_pct": {"mean": 0.0},
        },
    }
    windows = [window]
    if excluded:
        clean = dict(window)
        clean["excluded_reasons"] = []
        clean["started_at"] = (start + timedelta(hours=1)).isoformat()
        clean["ended_at"] = (end + timedelta(hours=1)).isoformat()
        clean["facts"] = dict(window["facts"])
        clean["facts"]["room_error_c"] = {"mean": 0.2}
        clean["facts"]["delta_t_c"] = {"mean": 10.0}
        windows.append(clean)
    temporal = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "windows": windows,
        "signals": {
            "control_temperature": {"role": "control_temperature"},
            "setting:mode_id": {"role": "other"},
            "target_temperature": {"role": "target_temperature"},
        },
        "quality": {"outdoor_temperature": {"mean": outdoor, "coverage_pct": weather_coverage}},
        "metrics": [{
            "id": f"runtime:{start.isoformat()}",
            "name": "burner_runtime_request_ratio",
            "value": 0.5,
            "unit": "ratio",
            "source": "derived",
            "coverage_pct": coverage,
        }],
        "exclusions": {"dhw": dhw_pct / 100 * 86400},
    }
    return Report(
        id=report_id or f"report:daily:{int(start.timestamp())}:report-v2",
        kind="daily",
        period_start=start,
        period_end=end,
        generated_at=end,
        timezone="UTC",
        quality=QualityResult(
            score=coverage / 100,
            coverage_pct=coverage,
            max_gap_seconds=300,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=288,
        ),
        context={"temporal_evidence": temporal, "current_mode": {"id": mode}},
        metrics=[MetricValue(
            id=f"outdoor:{start.isoformat()}",
            name="outdoor_mean_temperature_c",
            value=outdoor,
            unit="°C",
        )],
        predictions=[
            Prediction(
                id="prediction:original",
                scenario="setting_change",
                expected_effect="Room error should decrease",
                confidence=0.7,
                confidence_basis="synthetic baseline",
                verification="Compare three complete days",
            )
        ] if with_prediction else [],
        summary="synthetic daily report",
    )


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    return db


def _save(db: Database, report: Report) -> None:
    db.save_report(report, render_text(report))


def _season_report(start: datetime, end: datetime, *, year: int, season: str) -> Report:
    return Report(
        id=f"report:seasonal:{int(start.timestamp())}:report-v2",
        kind="seasonal",
        period_start=start,
        period_end=end,
        generated_at=end,
        timezone="UTC",
        quality=QualityResult(
            score=0.95,
            coverage_pct=95,
            max_gap_seconds=300,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=1000,
        ),
        context={"period": {"kind": "seasonal", "start": start.isoformat(), "end": end.isoformat(),
                             "observed_end": end.isoformat(), "timezone": "UTC", "complete": True,
                             "season": season, "year": year}},
        summary="synthetic season",
    )


def test_season_pairs_house_context_and_missing_history_are_explicit(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for day in (
        date(2026, 3, 2), date(2025, 9, 2), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4),
    ):
        _save(db, _report(datetime.combine(day, datetime.min.time(), tzinfo=UTC)))
    boundaries = SeasonBoundaries()
    autumn = season_period(2026, "autumn", "UTC", boundaries, as_of=datetime(2026, 12, 1, tzinfo=UTC))
    current = _season_report(autumn.start, autumn.end, year=2026, season="autumn")

    def no_analysis(_start: datetime, _end: datetime) -> Report:
        raise AssertionError("no intervention means no comparison callback")

    result = build_comparison_context(db, current, autumn, boundaries=boundaries, analyze_window=no_analysis)
    labels = {item["label"] for item in result["period_comparisons"]}
    assert {"Эта весна", "Прошлая осень"} <= labels
    assert all(item.get("matched_windows") for item in result["period_comparisons"])
    assert result["house_context"]["days"] == 3
    assert result["house_context"]["thermal_inertia"]["status"] == "observational_proxy"

    winter = season_period(2027, "winter", "UTC", boundaries, as_of=datetime(2027, 3, 1, tzinfo=UTC))
    winter_report = _season_report(winter.start, winter.end, year=2027, season="winter")
    missing = build_comparison_context(db, winter_report, winter, boundaries=boundaries, analyze_window=no_analysis)
    assert any(item["label"] == "Предыдущая осень" for item in missing["period_comparisons"])


def test_daily_comparison_context_excludes_persisted_current_report(tmp_path: Path) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 9, 2, tzinfo=UTC)
    current = _report(start)
    period = calendar_period("daily", start.date(), "UTC")
    boundaries = SeasonBoundaries()

    def no_analysis(_start: datetime, _end: datetime) -> Report:
        raise AssertionError("no intervention means no comparison callback")

    before = build_comparison_context(
        db, current, period, boundaries=boundaries, analyze_window=no_analysis,
    )
    _save(db, current)
    after = build_comparison_context(
        db, current, period, boundaries=boundaries, analyze_window=no_analysis,
    )

    assert before == after
    assert current.id not in after["house_context"]["source_report_ids"]


def test_firmware_outcome_preserves_prediction_and_second_intervention_blocks_isolation(tmp_path: Path) -> None:
    db = _db(tmp_path)
    current_start = datetime(2026, 10, 1, tzinfo=UTC)
    current = _season_report(current_start, current_start + timedelta(days=30), year=2026, season="autumn")
    original = _report(datetime(2026, 9, 1, tzinfo=UTC), with_prediction=True, report_id="report:original")
    _save(db, original)
    interventions = [
        {
            "intervention_id": "firmware-update", "recommendation_id": "rec-1",
            "recorded_at": current_start.isoformat(), "temporal_boundary": "2026-09-10T10:00:00+00:00",
            "owner_note": "updated", "experiment": {
                "category": "firmware_update", "before": "642", "after": "678",
                "performed_at": "2026-09-10T10:00:00+00:00",
            },
        },
        {
            "intervention_id": "firmware-rollback", "recommendation_id": "rec-2",
            "recorded_at": current_start.isoformat(), "temporal_boundary": "2026-09-12T10:00:00+00:00",
            "owner_note": "rollback", "experiment": {
                "category": "firmware_rollback", "before": "678", "after": "642",
                "performed_at": "2026-09-12T10:00:00+00:00",
            },
        },
    ]
    db.intervention_history = MethodType(lambda self, **_kwargs: interventions, db)  # type: ignore[method-assign]
    db.recommendation = MethodType(
        lambda self, _id: {"report_id": original.id, "hypothesis": "firmware changes cycling"}, db
    )  # type: ignore[method-assign]
    calls: list[tuple[datetime, datetime]] = []

    def analyze_window(start: datetime, end: datetime) -> Report:
        calls.append((start, end))
        return _report(start)

    boundaries = SeasonBoundaries()
    result = build_comparison_context(db, current, season_period(2026, "autumn", "UTC", boundaries,
                                                                 as_of=current.period_end),
                                      boundaries=boundaries, analyze_window=analyze_window)
    assert len(calls) <= 4
    outcome = result["intervention_outcomes"][0]
    assert outcome["experiment"]["before"] == "642"
    assert outcome["previous_prediction"][0]["id"] == "prediction:original"
    assert outcome["comparison"]["status"] == "limited"
    assert "second_intervention_between_windows" in outcome["comparison"]["confounders"]


def test_stale_weather_and_changed_target_profile_make_daily_baseline_unavailable() -> None:
    start = datetime(2026, 9, 2, tzinfo=UTC)
    current = _report(start, target=22)
    stale = _report(start - timedelta(days=2), weather_coverage=40)
    assert select_baseline(window_from_report(current), [window_from_report(stale)]) is None

    matched_weather = _report(start - timedelta(days=2), weather_coverage=95, target=21)
    assert select_baseline(window_from_report(current), [window_from_report(matched_weather)]) is None


def test_excluded_windows_do_not_contribute_room_or_delta_t_facts(tmp_path: Path) -> None:
    del tmp_path
    report = _report(datetime(2026, 9, 2, tzinfo=UTC), room_error=9, excluded=True)
    window = window_from_report(report)
    room_error = next(item for item in window.precomputed_metrics if item.name == "room_error_c")
    delta_t = next(item for item in window.precomputed_metrics if item.name == "delta_t_c")
    assert room_error.value == 0.2
    assert delta_t.value == 10


def test_telemetry_revision_changes_only_for_new_or_corrected_data_and_invalidates_signature(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    start = datetime(2026, 9, 2, tzinfo=UTC)
    point = TelemetryPoint(
        device_id="1", source_type="synthetic", entity_id="room", metric_key="temperature",
        timestamp_utc=start, value_num=21.0, unit="°C",
    )
    db.upsert_samples([point], {"room": "room_temperature"})
    end = start + timedelta(days=1)
    revision = db.period_data_revision(start, end)
    analysis = AnalysisService(db, AppConfig())
    period = calendar_period("daily", start.date(), analysis.config.home.timezone)
    signature_before = schedule_signature(analysis, period)
    db.upsert_samples([point], {"room": "room_temperature"})
    assert db.period_data_revision(start, end) == revision

    corrected = point.model_copy(update={"value_num": 22.0})
    db.upsert_samples([corrected], {"room": "room_temperature"})
    changed = db.period_data_revision(start, end)
    assert changed != revision
    assert schedule_signature(analysis, period) != signature_before

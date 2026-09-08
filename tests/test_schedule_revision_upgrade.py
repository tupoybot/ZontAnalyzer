from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import run_period_schedule, schedule_signature
from zont_analyzer.domain import TelemetryPoint
from zont_analyzer.domain.periods import calendar_period
from zont_analyzer.runtime import build_runtime


def _point(timestamp, value: float) -> TelemetryPoint:
    return TelemetryPoint(
        device_id="1",
        source_type="synthetic",
        entity_id="room",
        metric_key="temperature",
        timestamp_utc=timestamp,
        value_num=value,
        unit="°C",
    )


def test_schedule_adopts_legacy_signature_without_reanalysis_and_respects_exact_period_bounds(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    timezone = runtime.config.home.effective_timezone
    period = calendar_period("weekly", date(2026, 8, 31), timezone)
    inside = period.start + timedelta(hours=1)
    runtime.db.upsert_samples([_point(inside, 21.0)], {"room": "room_temperature"})

    original = analysis.analyze_period(period, use_ai=False)
    legacy_signature = schedule_signature(analysis, period, legacy_revision=True)
    original.context["schedule_signature"] = legacy_signature
    original.summary = "Saved AI interpretation"
    original.ai_used = True
    runtime.db.save_report(original, "saved")
    generated_at = original.generated_at

    monkeypatch.setattr(
        "zont_analyzer.application.period_schedule.scheduled_periods",
        lambda _analysis, _first, _today: [period],
    )

    def unexpected_analysis(*_args, **_kwargs):
        raise AssertionError("signature-format adoption must not reanalyse the period")

    monkeypatch.setattr(analysis, "analyze_period", unexpected_analysis)
    assert run_period_schedule(runtime, analysis, date(2026, 9, 8)) == []
    adopted = runtime.db.report(original.id)
    assert adopted is not None
    assert adopted.context["schedule_signature"] == schedule_signature(analysis, period)
    assert adopted.summary == "Saved AI interpretation"
    assert adopted.ai_used
    assert adopted.generated_at == generated_at

    # This falls on the same UTC day as the local-calendar boundary, but is not
    # part of the report's exact [start, observed_end) telemetry interval.
    runtime.db.upsert_samples([_point(period.observed_end + timedelta(hours=1), 5.0)])
    assert run_period_schedule(runtime, analysis, date(2026, 9, 8)) == []

    calls = 0

    def updated_analysis(_period, *, use_ai: bool = True):
        nonlocal calls
        calls += 1
        return adopted.model_copy(deep=True)

    monkeypatch.setattr(analysis, "analyze_period", updated_analysis)
    runtime.db.upsert_samples([_point(inside, 22.0)])
    result = run_period_schedule(runtime, analysis, date(2026, 9, 8))
    assert calls == 1
    assert result == [{
        "id": original.id,
        "kind": "weekly",
        "complete": True,
        "observed_end": period.observed_end.isoformat(),
        "ai_used": True,
    }]

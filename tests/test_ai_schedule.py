from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import run_period_schedule, schedule_signature
from zont_analyzer.domain import TelemetryPoint
from zont_analyzer.domain.periods import calendar_period
from zont_analyzer.runtime import build_runtime


def _point(timestamp, value: float) -> TelemetryPoint:
    return TelemetryPoint(device_id="1", source_type="synthetic", entity_id="room", metric_key="temperature",
                          timestamp_utc=timestamp, value_num=value, unit="°C")


def test_ai_setting_signature_upgrade_preserves_history_but_telemetry_reanalyses(tmp_path: Path, monkeypatch) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = AnalysisService(runtime.db, runtime.config)
    period = calendar_period("weekly", date(2026, 8, 31), runtime.config.home.effective_timezone)
    inside = period.start + timedelta(hours=1)
    runtime.db.upsert_samples([_point(inside, 21)], {"room": "room_temperature"})
    saved = analysis.analyze_period(period, use_ai=False)
    saved.context["schedule_signature"] = schedule_signature(analysis, period, legacy_ai_config=True)
    saved.summary, saved.ai_used = "Сохранённый AI", True
    runtime.db.save_report(saved, "saved")
    monkeypatch.setattr("zont_analyzer.application.period_schedule.scheduled_periods", lambda *_: [period])
    monkeypatch.setattr(analysis, "analyze_period", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(runtime, "analysis", lambda **_kwargs: analysis)
    assert run_period_schedule(runtime, analysis, date(2026, 9, 8)) == []
    adopted = runtime.db.report(saved.id)
    assert adopted.context["schedule_signature"] == schedule_signature(analysis, period)
    assert adopted.summary == "Сохранённый AI" and adopted.ai_used

    calls = 0
    def reanalyse(_period, *, use_ai=True):
        nonlocal calls
        calls += 1
        return adopted.model_copy(deep=True)
    monkeypatch.setattr(analysis, "analyze_period", reanalyse)
    runtime.db.upsert_samples([_point(inside, 22)], {"room": "room_temperature"})
    assert run_period_schedule(runtime, analysis, date(2026, 9, 8))
    assert calls == 1

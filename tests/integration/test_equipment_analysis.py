from datetime import UTC, date, datetime
from pathlib import Path

from zont_analyzer.adapters.openai.provider import analysis_packet
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.runtime import build_runtime


def test_owner_context_uses_effective_history_and_reaches_bounded_ai_packet(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.db.save_devices([{"id": "fixture"}])
    store = OwnerContextStore(runtime.db)
    store.update_profile("fixture", {"fields": {"auto_adapt": {"value": True}},
                                     "effective_from": "2026-08-01T00:00:00+00:00"})
    store.update_profile("fixture", {"fields": {"auto_adapt": {"value": False}},
                                     "effective_from": "2026-08-02T08:00:00+00:00"})
    assert store.profile("fixture", datetime(2026, 7, 1, tzinfo=UTC))["fields"] == {}
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    profile = report.context["equipment_profiles"][0]
    assert profile["fields"]["auto_adapt"]["value"] is True
    assert profile["changes_during_period"][0]["value"] is False
    packet = analysis_packet(quality=report.quality.model_dump(), metrics=report.metrics, events=report.events,
                             period={"kind": report.kind}, context=report.context)
    assert packet["control_context"]["equipment_profiles"][0]["fields"]["auto_adapt"]["value"] is True
    store.update_profile("fixture", {"fields": {"nominal_power_kw": {"value": 24}}})
    assert "nominal_power_kw" not in store.profile("fixture", report.period_start)["fields"]

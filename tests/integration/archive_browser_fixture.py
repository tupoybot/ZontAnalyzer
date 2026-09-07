"""Create a deliberately sparse, complete archive for the browser E2E test."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.application.publication import publish_reports
from zont_analyzer.domain import Hypothesis, TimeInterval
from zont_analyzer.reports import render_text
from zont_analyzer.runtime import build_runtime

runtime = build_runtime(Path("/config/config.yaml"), Path("/data"))
# Keep one deliberately synthetic installation available to the equipment API.
runtime.db.save_devices([{
    "device_id": "browser-synthetic-device",
    "name": "Browser synthetic boiler",
    "model": "FutureModel XSS-safe",
    "_equipment": {
        "coordinates": {"value": {"latitude": 48.25, "longitude": 12.5}, "source": "fixture:loc"},
        "boiler_model": {"value": "FutureModel XSS-safe", "source": "fixture:adapter"},
    },
}])
OwnerContextStore(runtime.db).update_profile("browser-synthetic-device", {
    "fields": {"installation_notes": {"value": "<b>synthetic owner note</b>"}},
})
analysis = runtime.analysis(no_ai=True)


def save_with_gas(report, gas: dict) -> None:
    report.context["gas"] = gas
    runtime.db.save_report(report, render_text(report))

# The gaps are intentional: navigation must skip 2 and 4 August.
for selected in (date(2026, 8, 1), date(2026, 8, 3), date(2026, 8, 5)):
    report = analysis.analyze_daily(selected, use_ai=False)
    report.hypotheses = [Hypothesis(
        id="h:browser", statement="Синтетическая гипотеза <unsafe>", confidence=.4,
        confidence_basis="Нет прямого сигнала", rationale="Только косвенные признаки",
        interval=TimeInterval(started_at=report.period_start, ended_at=report.period_end, timezone=report.timezone),
        alternatives=["Автоматика"], evidence_for=[{"id": "unconfirmed:browser"}],
    )]
    status = {1: "unknown", 3: "measured", 5: "estimated"}[selected.day]
    gas = {"status": status, "volume_m3": None if status == "unknown" else 12.3,
           "lower_m3": 10, "upper_m3": 15, "reliability_index_pct": 72,
           "coverage_pct": 88, "model_version": "gas-browser-1", "observed_days": 1,
           "observed_hours": 20, "flame_hours": 4, "flame_pct": 20,
           "scope": "boiler", "reasons": ["неполный интервал"], "complete": False,
           "ai_stale": selected.day == 5}
    save_with_gas(report, gas)
for week in (27, 29, 31):
    save_with_gas(analysis.analyze_week(2026, week, use_ai=False), {
        "status": "measured", "volume_m3": 80, "coverage_pct": 100,
        "reliability_index_pct": 80, "observed_days": 7, "complete": True,
    })
for month in (4, 6, 7):
    save_with_gas(analysis.analyze_month(2026, month, use_ai=False), {
        "status": "extrapolated", "volume_m3": 300, "lower_m3": 250, "upper_m3": 380,
        "coverage_pct": 60, "reliability_index_pct": 45, "observed_days": 18,
        "complete": True,
    })
for year, season in ((2025, "autumn"), (2026, "spring"), (2026, "autumn")):
    save_with_gas(analysis.analyze_season(year, season, use_ai=False), {
        "status": "estimated", "volume_m3": 900, "lower_m3": 700, "upper_m3": 1300,
        "coverage_pct": 55, "reliability_index_pct": 35, "observed_days": 45,
        "complete": False, "reasons": ["сезон наблюдался не полностью"],
    })

# Rendering fixtures deliberately exercise gas UI states independently of model tests.
with patch("zont_analyzer.application.gas.GasService.refresh", lambda self, report: report):
    result = publish_reports(runtime)
assert result["reports"] == 12

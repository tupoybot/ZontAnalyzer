"""Create a deliberately sparse, complete archive for the browser E2E test."""
from __future__ import annotations

from datetime import date
from pathlib import Path

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

# The gaps are intentional: navigation must skip 2 and 4 August.
for selected in (date(2026, 8, 1), date(2026, 8, 3), date(2026, 8, 5)):
    report = analysis.analyze_daily(selected, use_ai=False)
    report.hypotheses = [Hypothesis(
        id="h:browser", statement="Синтетическая гипотеза <unsafe>", confidence=.4,
        confidence_basis="Нет прямого сигнала", rationale="Только косвенные признаки",
        interval=TimeInterval(started_at=report.period_start, ended_at=report.period_end, timezone=report.timezone),
        alternatives=["Автоматика"], evidence_for=[{"id": "unconfirmed:browser"}],
    )]
    runtime.db.save_report(report, render_text(report))
for week in (27, 29, 31):
    analysis.analyze_week(2026, week, use_ai=False)
for month in (4, 6, 7):
    analysis.analyze_month(2026, month, use_ai=False)
for year, season in ((2025, "autumn"), (2026, "spring"), (2026, "autumn")):
    analysis.analyze_season(year, season, use_ai=False)

result = publish_reports(runtime)
assert result["reports"] == 12

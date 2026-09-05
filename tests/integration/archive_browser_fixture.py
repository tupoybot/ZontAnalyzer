"""Create a deliberately sparse, complete archive for the browser E2E test."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from zont_analyzer.application.publication import publish_reports
from zont_analyzer.runtime import build_runtime

runtime = build_runtime(Path("/config/config.yaml"), Path("/data"))
analysis = runtime.analysis(no_ai=True)

# The gaps are intentional: navigation must skip 2 and 4 August.
for selected in (date(2026, 8, 1), date(2026, 8, 3), date(2026, 8, 5)):
    analysis.analyze_daily(selected, use_ai=False)
analysis.analyze_week(2026, 31, use_ai=False)
analysis.analyze_month(2026, 7, use_ai=False)

result = publish_reports(runtime)
assert result["reports"] == 5

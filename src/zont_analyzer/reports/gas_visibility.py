"""Hide suspended derived gas output without changing stored reports or AI text."""
from typing import Any

from zont_analyzer.application.gas_feature import GAS_DISABLED_NOTICE, gas_analysis_enabled
from zont_analyzer.domain import Report


def gas_display_report(report: Report) -> Report:
    if gas_analysis_enabled():
        return report

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: strip(item) for key, item in value.items()
                    if key not in {"gas", "gas_savings", "gas_cost_comparison", "gas_interpretation_stale"}}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    context = strip(report.context)
    context["gas_disabled_notice"] = GAS_DISABLED_NOTICE
    return report.model_copy(update={"context": context})

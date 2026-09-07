"""Application boundary for exposing gas comparison results to reports/AI."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from zont_analyzer.analytics.gas_savings import GasInterval, GasSavings, WeatherBaseline, compare_gas_savings


def gas_comparison_context(
    baseline: WeatherBaseline,
    before: GasInterval,
    after: GasInterval,
) -> dict[str, Any]:
    """Return compact, provenance-rich JSON-compatible comparison context.

    Arithmetic remains in :mod:`analytics.gas_savings`; this adapter is safe to
    pass to renderers and prompts and intentionally contains no AI interpretation.
    """
    result = compare_gas_savings(baseline, before, after)
    return {
        "observed_m3": result.observed_m3,
        "expected_m3_weather_normalized": result.expected_m3,
        "raw_savings": {"m3": result.raw_savings_m3, "pct": result.raw_savings_pct},
        "normalized_savings": {"m3": result.normalized_savings_m3, "pct": result.normalized_savings_pct},
        "uncertainty_m3": result.uncertainty_m3,
        "uncertainty_range_m3": {
            "lower": result.uncertainty_lower_m3,
            "upper": result.uncertainty_upper_m3,
            "method": result.uncertainty_method,
            "probabilistic": False,
        },
        "reliability_index": result.reliability_index,
        "provenance": {
            "validation": "независимое показание после изменения" if result.measured_validation else "только модель",
            "model_only": result.model_only,
            "baseline_frozen": baseline.frozen,
            "training_intervals": baseline.training_intervals,
            "model_training_intervals": baseline.model_training_intervals,
            "held_out_intervals": baseline.validation_intervals,
            "held_out_rmse_m3": baseline.validation_rmse_m3,
            "base_temperature_c": baseline.base_temperature_c,
            "uses_validated_dhw_adjustment": baseline.uses_dhw_adjustment,
            "extrapolated": result.extrapolated,
        },
        "assumptions": list(result.assumptions),
        "confounders": list(result.confounders),
        "diagnostics": list(result.diagnostics),
        "effect_status": result.effect_status,
        "sensitivity": {
            "occupancy_signal": {"before": before.occupancy_signal, "after": after.occupancy_signal},
            "occupancy_correction_applied": False,
            "shares_evidence_with_dhw": True,
            "interpretation": "только гипотеза; сравнить альтернативы без второй поправки"
            if before.occupancy_signal is not None or after.occupancy_signal is not None
            else "данных о присутствии нет",
            "comfort": "неизвестно: одна температура не подтверждает субъективный комфорт",
        },
    }


def summarize_gas_comparisons(results: Iterable[GasSavings]) -> dict[str, Any]:
    """Summarize results without averaging percentages or discarding provenance."""
    values = list(results)
    return {
        "comparisons": len(values),
        "raw_savings_m3": sum(item.raw_savings_m3 for item in values),
        "normalized_savings_m3": sum(item.normalized_savings_m3 for item in values),
        "model_only_comparisons": sum(item.model_only for item in values),
        "extrapolated_comparisons": sum(item.extrapolated for item in values),
        "independent_measured_comparisons": sum(item.measured_validation for item in values),
    }

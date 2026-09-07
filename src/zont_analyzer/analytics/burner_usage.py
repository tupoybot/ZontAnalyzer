"""Explicit denominators for observed flame time and its purpose breakdown."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def burner_usage(gas: Mapping[str, Any], period_hours: float) -> dict[str, Any]:
    def hours(key: str) -> float | None:
        value = gas.get(key)
        return float(value) if (isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value) and value >= 0) else None

    flame = hours("flame_hours")
    observed = hours("observed_hours")
    result: dict[str, Any] = {
        "burner_usage_version": "burner-usage-v1",
        "period_hours": period_hours,
        "flame_pct": flame / period_hours * 100 if flame is not None and period_hours > 0 else None,
        "flame_pct_denominator": "full_report_period",
        "purpose_pct_denominator": "observed_flame_time",
        "unobserved_hours": max(0, period_hours - observed) if observed is not None else None,
    }
    for purpose in ("heating", "dhw", "purpose_unknown"):
        duration = hours(f"{purpose}_flame_hours")
        result[f"{purpose}_flame_pct"] = (
            duration / flame * 100 if duration is not None and flame is not None and flame > 0 else None
        )
    return result

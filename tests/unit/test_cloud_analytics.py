from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zont_analyzer.cloud.analytics import AnalyticsInput, analyze


def _payload() -> dict[str, object]:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return {
        "period_start": start.isoformat(),
        "period_end": (start + timedelta(minutes=20)).isoformat(),
        "target_c": 22.0,
        "comfort_band_c": 0.5,
        "samples": [
            {"timestamp": start.isoformat(), "value": 20.0},
            {"timestamp": (start + timedelta(minutes=10)).isoformat(), "value": 23.0},
            {"timestamp": (start + timedelta(minutes=20)).isoformat(), "value": 23.0},
        ],
    }


def test_analytics_uses_existing_pure_calculations_without_storage() -> None:
    result = analyze(_payload())

    assert result["quality"]["sample_count"] == 3
    assert {item["name"] for item in result["metrics"]} >= {"mean_temperature_c", "time_in_target_band_pct"}
    assert result["events"][0]["kind"] == "temperature_below_heating_setpoint"


def test_analytics_rejects_unbounded_or_ambiguous_series() -> None:
    changes = [
        lambda payload: payload.update({"period_end": "2026-09-03T00:00:00Z"}),
        lambda payload: payload["samples"].append(payload["samples"][0]),
        lambda payload: payload["samples"].__setitem__(0, {"timestamp": "2026-09-01T00:00:00", "value": 20}),
        lambda payload: payload["samples"].__setitem__(1, {"timestamp": "2026-09-01T00:10:00Z", "value": float("nan")}),
    ]
    for change in changes:
        payload = _payload()
        change(payload)

        with pytest.raises(ValidationError):
            AnalyticsInput.model_validate(payload)

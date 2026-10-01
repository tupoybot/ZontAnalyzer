"""The gas calibration cutoff is the local midnight after the final reading day."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.integration.test_stage8_gas import history
from tests.ydb_support import seed_samples
from zont_analyzer.application.gas import GasService
from zont_analyzer.domain import TelemetryPoint


def _point(at: datetime, *, state: bool | None = None, modulation: float | None = None) -> TelemetryPoint:
    if state is not None:
        return TelemetryPoint(
            device_id="1",
            entity_id="boiler",
            source_type="z3k_boiler_adapter",
            metric_key="s",
            timestamp_utc=at,
            value_text="['fl', 'ch']" if state else "[]",
        )
    assert modulation is not None
    return TelemetryPoint(
        device_id="1",
        entity_id="boiler",
        source_type="z3k_boiler_adapter",
        metric_key="rml",
        timestamp_utc=at,
        value_num=modulation,
    )


@pytest.mark.ydb
def test_calibration_ignores_samples_at_and_after_local_midnight_after_last_reading(
    tmp_path: Path,
) -> None:
    runtime, owner, reports = history(tmp_path)
    runtime.config.home.timezone = "Asia/Kolkata"
    owner.update_gas(reports[0].id, {"value_m3": 100})
    owner.update_gas(reports[2].id, {"value_m3": 292})

    baseline_service = GasService(runtime.db, runtime.config)
    last_reading_day = max(date.fromisoformat(row["day"]) for row in baseline_service.readings)
    local_boundary = datetime.combine(
        last_reading_day + timedelta(days=1), time.min, ZoneInfo("Asia/Kolkata"),
    )
    assert local_boundary.utcoffset() == timedelta(hours=5, minutes=30)
    boundary = local_boundary.astimezone(UTC)
    assert boundary.hour == 18 and boundary.minute == 30  # UTC+05:30 local midnight.
    assert baseline_service.timezone == "Asia/Kolkata"

    baseline_window = baseline_service.window(boundary - timedelta(hours=12), boundary)
    baseline_version, baseline_model, baseline_intervals = baseline_service.model()
    assert len(baseline_intervals) == 1
    assert baseline_intervals[0].end + timedelta(hours=12) == boundary

    # These existing samples are on the cutoff and later, outside the last
    # meter reading day's calibration windows. Change both state and modulation.
    after_cutoff = [
        _point(boundary, state=False),
        _point(boundary, modulation=100.0),
        _point(boundary + timedelta(minutes=10), state=False),
        _point(boundary + timedelta(minutes=10), modulation=100.0),
    ]
    seed_samples(runtime.db, after_cutoff)
    after_service = GasService(runtime.db, runtime.config)
    assert after_service.timezone == "Asia/Kolkata"
    after_window = after_service.window(boundary - timedelta(hours=12), boundary)
    after_version, after_model, after_intervals = after_service.model()

    assert after_window == baseline_window
    assert after_version == baseline_version
    assert after_model == baseline_model
    assert after_intervals == baseline_intervals

    # A historical change inside the meter interval changes its measured
    # exposure and the fitted calibration, proving pre-boundary data still counts.
    historical = datetime(2026, 1, 5, 6, 30, tzinfo=UTC)
    seed_samples(runtime.db, [_point(historical, state=False)])
    historical_service = GasService(runtime.db, runtime.config)
    historical_version, historical_model, historical_intervals = historical_service.model()

    assert historical_intervals != after_intervals
    assert historical_intervals[0].observed_minutes == after_intervals[0].observed_minutes
    assert historical_intervals[0].flame_minutes < after_intervals[0].flame_minutes
    assert historical_version != after_version
    assert historical_model != after_model

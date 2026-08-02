from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.events import detect_burner_events
from zont_analyzer.analytics.metrics import burner_metrics, temperature_metrics
from zont_analyzer.analytics.quality import assess_quality


def test_temperature_metrics_are_time_weighted() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        (start, 20.0),
        (start + timedelta(minutes=1), 30.0),
        (start + timedelta(minutes=11), 30.0),
    ]
    metrics = temperature_metrics(samples, period_id="day", target_c=25, comfort_band_c=0.5)
    mean = next(item for item in metrics if item.name == "mean_temperature_c")
    # 20 °C for one minute and 30 °C for ten minutes, not a point-wise mean.
    assert mean.value == pytest.approx((20 + 300) / 11, abs=0.001)

    by_name = {item.name: item for item in metrics}
    assert by_name["time_above_target_band_pct"].value == pytest.approx(10 / 11 * 100, abs=0.001)
    assert by_name["time_below_target_band_pct"].value == pytest.approx(1 / 11 * 100, abs=0.001)
    assert by_name["degree_hours_above_target"].value == pytest.approx(50 / 60, abs=0.001)
    assert by_name["degree_hours_above_target"].unit == "°C·h"
    assert "degree_minutes_above_target" not in by_name


def test_quality_flags_large_gaps() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        (start, 20),
        (start + timedelta(minutes=1), 20.1),
        (start + timedelta(hours=20), 20.2),
    ]
    quality = assess_quality(samples, start, start + timedelta(days=1))
    assert quality.score < 0.7
    assert "low_coverage" in quality.flags
    assert "large_gap" in quality.flags


def test_burner_metrics_count_cycles() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    values = [0, 1, 1, 0, 0, 1, 0]
    samples = [(start + timedelta(minutes=i), value) for i, value in enumerate(values)]
    metrics = burner_metrics(samples, period_id="day", period_hours=1, short_cycle_minutes=3)
    by_name = {item.name: item.value for item in metrics}
    assert by_name["burner_starts"] == 2
    assert by_name["short_cycle_share_pct"] == 100


def test_burner_cycle_does_not_span_telemetry_gap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        (start, 0),
        (start + timedelta(minutes=1), 1),
        (start + timedelta(hours=8), 1),
        (start + timedelta(hours=8, minutes=1), 0),
    ]

    events = detect_burner_events(samples, period_id="day", short_cycle_minutes=5)
    metrics = burner_metrics(samples, period_id="day", period_hours=24, short_cycle_minutes=5)

    assert len(events) == 1
    assert events[0].details["duration_seconds"] == 60
    assert next(item for item in metrics if item.name == "burner_starts").value == 1


def test_burner_cycle_during_control_transition_is_context_not_anomaly() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        (start, 0),
        (start + timedelta(minutes=1), 1),
        (start + timedelta(minutes=3), 0),
        (start + timedelta(hours=3), 0),
        (start + timedelta(hours=3, minutes=1), 1),
        (start + timedelta(hours=3, minutes=4), 1),
        (start + timedelta(hours=3, minutes=7), 0),
    ]
    context = [(start, start + timedelta(hours=2))]

    events = detect_burner_events(
        samples,
        period_id="context",
        short_cycle_minutes=5,
        context_windows=context,
    )
    metrics = burner_metrics(
        samples,
        period_id="context",
        period_hours=4,
        short_cycle_minutes=5,
        ignore_windows=context,
    )

    assert events[0].kind == "burner_cycle_after_control_change"
    assert events[0].severity == "info"
    assert next(item for item in metrics if item.name == "burner_starts").value == 1
    assert next(item for item in metrics if item.name == "short_cycle_share_pct").value == 0

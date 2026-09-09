from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.events import detect_burner_events, detect_temperature_events
from zont_analyzer.analytics.flame import detect_unconfirmed_burner_pulses
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
    assert by_name["time_in_target_band_pct"].context == {"comfort_band_c": 0.5}
    assert by_name["time_above_target_band_pct"].value == pytest.approx(10 / 11 * 100, abs=0.001)
    assert by_name["time_below_target_band_pct"].value == pytest.approx(1 / 11 * 100, abs=0.001)
    assert by_name["degree_hours_above_target"].value == pytest.approx(50 / 60, abs=0.001)
    assert by_name["degree_hours_above_target"].unit == "°C·h"
    assert "degree_minutes_above_target" not in by_name


def test_target_history_is_stateful_and_none_invalidates_it() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [(start + timedelta(hours=hour), 20.0) for hour in range(5)]
    metrics = temperature_metrics(
        samples,
        period_id="setpoint-state",
        target_c=22.0,
        comfort_band_c=0.5,
        # The fallback current target must not leak into the first hour; None
        # invalidates the target for the third hour.
        target_samples=[
            (start + timedelta(hours=1), 21.0),
            (start + timedelta(hours=2), None),
            (start + timedelta(hours=3), 20.0),
        ],
    )

    by_name = {item.name: item.value for item in metrics}
    assert by_name["heating_target_evaluation_time_pct"] == 50
    assert by_name["time_in_target_band_pct"] == 50
    assert by_name["time_below_target_band_pct"] == 50


def test_constant_target_does_not_bridge_a_sensor_outage() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [
        (start, 20.0),
        (start + timedelta(minutes=10), 20.0),
        (start + timedelta(minutes=20), 20.0),
        (start + timedelta(hours=8), 20.0),
        (start + timedelta(hours=8, minutes=10), 20.0),
    ]
    metrics = temperature_metrics(
        samples,
        period_id="outage",
        target_c=None,
        comfort_band_c=0.5,
        target_samples=[(start, 20.0)],
    )

    by_name = {item.name: item.value for item in metrics}
    # The single user-set target is valid throughout; the generic sensor gap
    # rule still excludes the eight-hour missing-temperature interval.
    assert by_name["heating_target_evaluation_time_pct"] == 100


def test_unknown_target_ends_temperature_event_before_next_sensor_sample() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    events = detect_temperature_events(
        [(start, 24.0), (start + timedelta(hours=2), 24.0)],
        period_id="target-null-event",
        target_c=None,
        comfort_band_c=0.5,
        target_samples=[(start, 20.0), (start + timedelta(hours=1), None)],
    )

    assert len(events) == 1
    assert events[0].ended_at == start + timedelta(hours=1)


def test_repeated_polled_setpoint_is_equivalent_to_one_state_transition() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    temperatures = [(start + timedelta(minutes=10 * item), 24.0) for item in range(5)]
    sparse = [(start, 20.0)]
    repeated = [(start + timedelta(minutes=item), 20.0) for item in range(500)]

    sparse_events = detect_temperature_events(
        temperatures, period_id="sparse-target", target_c=None, comfort_band_c=0.5, target_samples=sparse
    )
    repeated_events = detect_temperature_events(
        temperatures, period_id="repeated-target", target_c=None, comfort_band_c=0.5, target_samples=repeated
    )
    sparse_metrics = temperature_metrics(
        temperatures, period_id="sparse-target", target_c=None, comfort_band_c=0.5, target_samples=sparse
    )
    repeated_metrics = temperature_metrics(
        temperatures, period_id="repeated-target", target_c=None, comfort_band_c=0.5, target_samples=repeated
    )

    assert len(sparse_events) == len(repeated_events) == 1
    assert sparse_events[0].started_at == repeated_events[0].started_at
    assert sparse_events[0].ended_at == repeated_events[0].ended_at
    assert [(item.name, item.value) for item in sparse_metrics] == [
        (item.name, item.value) for item in repeated_metrics
    ]


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


def test_quality_uses_regular_radio_cadence_despite_short_burst() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = [(start + timedelta(minutes=5 + index * 10), 20 + index / 100) for index in range(144)]
    samples.append((start + timedelta(hours=12, minutes=6), 21.0))

    quality = assess_quality(samples, start, start + timedelta(days=1))

    assert quality.sample_count == 145
    assert quality.coverage_pct > 98
    assert quality.score > 0.9
    assert quality.flags == []


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


def test_short_flame_pulse_without_flow_rise_is_reported_and_filtered() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = detect_unconfirmed_burner_pulses(
        period_id="noise",
        boiler_state_samples=[
            (start, "[]"),
            (start + timedelta(minutes=1), "['dhw', 'fl']"),
            (start + timedelta(minutes=2), "[]"),
        ],
        flow_temperature_samples=[
            (start, 30.0),
            (start + timedelta(minutes=2), 30.1),
            (start + timedelta(minutes=7), 30.0),
        ],
        maximum_pulse_minutes=2,
    )

    assert result.ignored_windows == [(start + timedelta(minutes=1), start + timedelta(minutes=2))]
    assert result.events[0].severity == "info"
    assert result.events[0].details["inference"]["excluded_from_burner_and_dhw_cycle_statistics"] is True
    assert result.metrics[0].value == 1


def test_short_flame_pulse_with_delayed_flow_rise_is_retained() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = detect_unconfirmed_burner_pulses(
        period_id="real",
        boiler_state_samples=[
            (start, "[]"),
            (start + timedelta(minutes=1), "['dhw', 'fl']"),
            (start + timedelta(minutes=2), "[]"),
        ],
        flow_temperature_samples=[
            (start, 25.0),
            (start + timedelta(minutes=2), 25.1),
            (start + timedelta(minutes=7), 34.8),
        ],
        maximum_pulse_minutes=2,
    )

    assert not result.ignored_windows
    assert not result.events


def test_flame_noise_filter_is_conservative_without_response_data_or_for_long_cycle() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    insufficient = detect_unconfirmed_burner_pulses(
        period_id="missing",
        boiler_state_samples=[
            (start, "[]"),
            (start + timedelta(minutes=1), "['fl']"),
            (start + timedelta(minutes=2), "[]"),
        ],
        flow_temperature_samples=[(start, 30.0)],
        maximum_pulse_minutes=2,
    )
    long_cycle = detect_unconfirmed_burner_pulses(
        period_id="long",
        boiler_state_samples=[
            (start, "[]"),
            (start + timedelta(minutes=1), "['fl']"),
            (start + timedelta(minutes=4), "[]"),
        ],
        flow_temperature_samples=[(start, 30.0), (start + timedelta(minutes=5), 30.0)],
        maximum_pulse_minutes=2,
    )

    assert not insufficient.ignored_windows
    assert not long_cycle.ignored_windows

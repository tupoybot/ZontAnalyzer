from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import NamedTuple

from zont_analyzer.domain import DetectedEvent, MetricValue

from .dhw import parse_opentherm_flags

ALGORITHM_VERSION = "flame-noise-v1"


class FlameNoiseAnalysis(NamedTuple):
    ignored_windows: list[tuple[datetime, datetime]]
    events: list[DetectedEvent]
    metrics: list[MetricValue]


def _value_before(samples: list[tuple[datetime, float]], timestamp: datetime) -> tuple[datetime, float] | None:
    current: tuple[datetime, float] | None = None
    for sample_time, value in sorted(samples):
        if sample_time >= timestamp:
            break
        current = (sample_time, value)
    return current


def detect_unconfirmed_burner_pulses(
    *,
    period_id: str,
    boiler_state_samples: Sequence[tuple[datetime, str | Collection[str]]],
    flow_temperature_samples: list[tuple[datetime, float]],
    maximum_pulse_minutes: float,
    minimum_flow_rise_c: float = 0.5,
    response_window_minutes: float = 5.0,
) -> FlameNoiseAnalysis:
    """Find short flame flags that have no corroborating heat-carrier response.

    A short flag alone is never discarded. Filtering requires a recent baseline and
    flow-temperature samples through the pulse/response window. The reported pulse
    remains visible as an informational evidence event.
    """

    ordered_states = sorted(boiler_state_samples, key=lambda item: item[0])
    ordered_flow = sorted(flow_temperature_samples)
    cycles: list[tuple[datetime, datetime, frozenset[str]]] = []
    active_start: datetime | None = None
    active_flags = frozenset[str]()
    previous_flame = False
    for timestamp, encoded in ordered_states:
        flags = parse_opentherm_flags(encoded)
        flame = "fl" in flags
        if flame and not previous_flame:
            active_start = timestamp
            active_flags = flags
        elif flame and active_start is not None:
            active_flags = active_flags | flags
        elif not flame and previous_flame and active_start is not None:
            cycles.append((active_start, timestamp, active_flags))
            active_start = None
            active_flags = frozenset()
        previous_flame = flame

    ignored_windows: list[tuple[datetime, datetime]] = []
    events: list[DetectedEvent] = []
    maximum_duration = maximum_pulse_minutes * 60
    response_window = timedelta(minutes=response_window_minutes)
    for start, end, flags in cycles:
        duration = (end - start).total_seconds()
        if duration <= 0 or duration > maximum_duration:
            continue
        baseline = _value_before(ordered_flow, start)
        response_end = end + response_window
        response = [(timestamp, value) for timestamp, value in ordered_flow if start < timestamp <= response_end]
        if baseline is None or not response or response[-1][0] < response_end:
            continue
        baseline_time, baseline_c = baseline
        if start - baseline_time > response_window:
            continue
        peak_c = max(value for _timestamp, value in response)
        rise_c = peak_c - baseline_c
        if rise_c >= minimum_flow_rise_c:
            continue
        ignored_windows.append((start, end))
        purposes = sorted(flags & {"ch", "dhw"})
        events.append(
            DetectedEvent(
                id=f"event:{period_id}:unconfirmed_burner_pulse:{int(start.timestamp())}:{ALGORITHM_VERSION}",
                kind="unconfirmed_burner_pulse",
                started_at=start,
                ended_at=end,
                severity="info",
                details={
                    "facts": {
                        "reported_duration_seconds": duration,
                        "reported_purposes": purposes,
                        "flow_temperature_start_c": round(baseline_c, 3),
                        "flow_temperature_peak_c": round(peak_c, 3),
                        "flow_temperature_rise_c": round(rise_c, 3),
                        "minimum_confirming_rise_c": minimum_flow_rise_c,
                    },
                    "inference": {
                        "classification": "telemetry_noise_without_thermal_response",
                        "excluded_from_burner_and_dhw_cycle_statistics": True,
                    },
                    "hypothesis": (
                        "Короткий флаг пламени не подтверждён ростом температуры теплоносителя; "
                        "он исключён из рабочих циклов как вероятный шум телеметрии."
                    ),
                },
                algorithm_version=ALGORITHM_VERSION,
            )
        )

    metrics = [
        MetricValue(
            id=f"metric:{period_id}:unconfirmed_burner_pulse_count:{ALGORITHM_VERSION}",
            name="unconfirmed_burner_pulse_count",
            value=float(len(events)),
            unit="count",
            algorithm_version=ALGORITHM_VERSION,
            context={"excluded_from_burner_and_dhw_cycle_statistics": True},
        )
    ]
    return FlameNoiseAnalysis(ignored_windows=ignored_windows, events=events, metrics=metrics)


__all__ = ["ALGORITHM_VERSION", "FlameNoiseAnalysis", "detect_unconfirmed_burner_pulses"]

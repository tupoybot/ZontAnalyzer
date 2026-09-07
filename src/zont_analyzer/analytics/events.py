from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from statistics import median

from zont_analyzer.domain import DetectedEvent


def _target_transitions(
    samples: Sequence[tuple[datetime, float | None]],
) -> list[tuple[datetime, float | None]]:
    ordered = sorted({timestamp: value for timestamp, value in samples}.items())
    result: list[tuple[datetime, float | None]] = []
    seen = False
    previous: float | None = None
    for timestamp, value in ordered:
        if not seen or value != previous:
            result.append((timestamp, value))
            previous = value
            seen = True
    return result


def _maximum_continuous_gap(samples: list[tuple[datetime, float]]) -> float:
    gaps = [(samples[index + 1][0] - samples[index][0]).total_seconds() for index in range(len(samples) - 1)]
    positive = [gap for gap in gaps if gap > 0]
    if not positive:
        return 60.0
    expected = min(median(positive), min(positive) * 3)
    return max(expected * 2.5, 60.0)


def detect_temperature_events(
    samples: list[tuple[datetime, float]],
    *,
    period_id: str,
    target_c: float | None,
    comfort_band_c: float,
    target_samples: Sequence[tuple[datetime, float | None]] | None = None,
    ignore_windows: list[tuple[datetime, datetime]] | None = None,
) -> list[DetectedEvent]:
    ordered_targets = _target_transitions(target_samples or [])
    if target_c is None and not ordered_targets:
        return []
    ordered = sorted(samples)
    events: list[DetectedEvent] = []
    active_kind: str | None = None
    active_start: datetime | None = None
    peak = 0.0
    previous_timestamp: datetime | None = None
    maximum_gap = _maximum_continuous_gap(ordered)
    target_index = 0
    # Historical setpoints are state transitions.  A supplied history must not
    # use the current target as an invented value before its first observation.
    current_target: float | None = target_c if not ordered_targets else None

    def close_active(ended_at: datetime) -> None:
        nonlocal active_kind, active_start, peak
        if active_kind and active_start:
            events.append(
                DetectedEvent(
                    id=f"event:{period_id}:{active_kind}:{int(active_start.timestamp())}:events-v2",
                    kind=active_kind,
                    started_at=active_start,
                    ended_at=ended_at,
                    severity="warning" if peak > comfort_band_c * 2 else "info",
                    details={"peak_error_c": round(peak, 3)},
                )
            )
        active_kind = None
        active_start = None
        peak = 0.0

    for timestamp, value in ordered:
        if previous_timestamp is not None and (timestamp - previous_timestamp).total_seconds() > maximum_gap:
            close_active(previous_timestamp)
        # A target transition has a precise time even when temperature samples
        # are sparse.  Do not let an event claim continuity across a changed or
        # explicitly unknown setpoint.
        pending_targets = []
        while target_index < len(ordered_targets) and ordered_targets[target_index][0] <= timestamp:
            pending_targets.append(ordered_targets[target_index])
            target_index += 1
        if previous_timestamp is not None:
            changed_at = next(
                (target_time for target_time, _target in pending_targets if target_time > previous_timestamp),
                None,
            )
            if changed_at is not None:
                close_active(changed_at)
        for _target_time, target in pending_targets:
            current_target = target
        ignored = any(start <= timestamp < end for start, end in (ignore_windows or []))
        if ignored or current_target is None:
            close_active(timestamp)
            previous_timestamp = timestamp
            continue
        error = value - current_target
        kind = (
            "temperature_above_heating_setpoint"
            if error > comfort_band_c
            else "temperature_below_heating_setpoint"
            if error < -comfort_band_c
            else None
        )
        if kind != active_kind:
            if active_kind and active_start:
                close_active(timestamp)
            active_kind = kind
            active_start = timestamp if kind else None
            peak = abs(error) if kind else 0.0
        elif kind:
            peak = max(peak, abs(error))
        previous_timestamp = timestamp
    if active_kind and active_start and ordered:
        close_active(ordered[-1][0])
    return events


def detect_burner_events(
    samples: list[tuple[datetime, float]],
    *,
    period_id: str,
    short_cycle_minutes: float,
    context_windows: list[tuple[datetime, datetime]] | None = None,
) -> list[DetectedEvent]:
    ordered = sorted(samples)
    events: list[DetectedEvent] = []
    active_start: datetime | None = None
    previous_on = False
    previous_timestamp: datetime | None = None
    maximum_gap = _maximum_continuous_gap(ordered)
    for timestamp, value in ordered:
        if previous_timestamp is not None and (timestamp - previous_timestamp).total_seconds() > maximum_gap:
            active_start = None
            previous_on = False
        on = value > 0
        if on and not previous_on:
            active_start = timestamp
        elif not on and previous_on and active_start:
            duration = (timestamp - active_start).total_seconds()
            overlaps_context = any(
                active_start < window_end and timestamp >= window_start
                for window_start, window_end in (context_windows or [])
            )
            would_be_short = duration < short_cycle_minutes * 60
            kind = (
                "burner_cycle_after_control_change"
                if overlaps_context
                else "short_burner_cycle"
                if would_be_short
                else "burner_cycle"
            )
            events.append(
                DetectedEvent(
                    id=f"event:{period_id}:{kind}:{int(active_start.timestamp())}:events-v2",
                    kind=kind,
                    started_at=active_start,
                    ended_at=timestamp,
                    severity="warning" if kind == "short_burner_cycle" else "info",
                    details={
                        "duration_seconds": duration,
                        "control_context": "transition" if overlaps_context else None,
                        "would_be_short_without_context": would_be_short,
                    },
                )
            )
            active_start = None
        previous_on = on
        previous_timestamp = timestamp
    return events

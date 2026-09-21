"""Deterministic presentation ranking for report events.

The canonical event list is deliberately left untouched.  This module only
decides which events deserve the small highlights block in an HTML report.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite

from zont_analyzer.domain import DetectedEvent

_SEVERITY_SCORE = {"info": 0, "warning": 35, "critical": 75}
_ROUTINE_KINDS = {
    "burner_cycle",
    "unconfirmed_burner_pulse",
    "target_temperature_change",
    "heating_mode_change",
    "burner_cycle_after_control_change",
    "dhw_reheat_episode",
    "automatic_summer_mode_entered",
    "automatic_summer_mode_exited",
}
_SERVICE_INTERRUPTION_KINDS = {"boiler_connection_loss", "main_power_outage", "zont_connection_loss"}


@dataclass(frozen=True)
class RankedEvent:
    event: DetectedEvent
    score: float
    count: int = 1
    total_duration_minutes: float = 0.0
    events: tuple[DetectedEvent, ...] = ()


def _duration_minutes(event: DetectedEvent) -> float:
    if event.ended_at is not None:
        return max(0.0, (event.ended_at - event.started_at).total_seconds() / 60)
    value = event.details.get("duration_minutes")
    scale = 1
    if value is None:
        value, scale = event.details.get("duration_seconds", 0), 60
    try:
        duration = float(value) / scale
        return max(0.0, duration) if isfinite(duration) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _impact(event: DetectedEvent) -> float:
    try:
        value = float(event.details.get("peak_error_c", 0))
    except (TypeError, ValueError):
        return 0.0
    return min(abs(value), 20.0) if isfinite(value) else 0.0


def _is_noise(event: DetectedEvent, duration: float) -> bool:
    # A closed interval at one sample has no observable duration.  Keeping it
    # in technical details preserves evidence without calling it significant.
    return (
        event.severity != "critical"
        and duration < 1
        and event.kind in {"temperature_above_heating_setpoint", "temperature_below_heating_setpoint"}
    ) or (event.kind in _ROUTINE_KINDS and event.severity == "info")


def _score(event: DetectedEvent, duration: float) -> float:
    # Keep severity bands disjoint: a flood of low-severity repeats must not
    # outrank one critical incident.
    score = float(_SEVERITY_SCORE[event.severity] * 10) + min(duration / 10.0, 25.0) + _impact(event)
    if event.kind in _ROUTINE_KINDS:
        score -= 20.0
    if event.kind in _SERVICE_INTERRUPTION_KINDS:
        score += 60.0
    elif event.kind == "dhw_antilegionella_cycle":
        score += 10.0
    return score


def rank_events(events: Iterable[DetectedEvent], *, period_kind: str = "daily") -> list[RankedEvent]:
    """Return events in stable significance order for presentation.

    Weekly and monthly reports collapse repeated event kinds into one row.  A
    representative keeps the original evidence while count and total duration
    make the repetition visible to the reader.
    """

    ordered = sorted(events, key=lambda item: (item.started_at, item.id))
    grouped: list[RankedEvent] = []
    if period_kind in {"weekly", "monthly"}:
        by_kind: dict[str, list[DetectedEvent]] = {}
        for event in ordered:
            # Keep short/noisy observations separate from meaningful episodes.
            key = event.kind + (":routine" if _is_noise(event, _duration_minutes(event)) else "")
            by_kind.setdefault(key, []).append(event)
        candidates = [items for _kind, items in sorted(by_kind.items())]
    else:
        candidates = [[event] for event in ordered]
    for items in candidates:
        representative = max(
            items,
            key=lambda item: (
                _SEVERITY_SCORE[item.severity],
                _score(item, _duration_minutes(item)),
                -item.started_at.timestamp(),
            ),
        )
        duration = sum(_duration_minutes(item) for item in items)
        score = max(_score(item, _duration_minutes(item)) for item in items)
        if len(items) > 1:
            score += min(len(items) - 1, 10) * 1.5
        grouped.append(RankedEvent(representative, score, len(items), duration, tuple(items)))
    return sorted(grouped, key=lambda item: (-item.score, item.event.started_at, item.event.id))


def is_routine(item: RankedEvent) -> bool:
    """Whether an event belongs in the expandable technical details block."""

    duration = _duration_minutes(item.event)
    return _is_noise(item.event, duration) or item.score <= 8

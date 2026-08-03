from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Literal

from zont_analyzer.domain import DetectedEvent, MetricValue, SourceEvent

BOILER_LOSS_TYPES = frozenset({"LossConnectionBoiler", "OTLost"})
BOILER_RESTORE_TYPES = frozenset({"ReconnectingBoiler", "OTFound"})
MAIN_POWER_LOSS_TYPES = frozenset({"MainPowerLost"})
MAIN_POWER_RESTORE_TYPES = frozenset({"MainPowerFound", "MainPowerRestored"})
CONTROLLER_OFF_TYPES = frozenset({"PowerOff"})
CONTROLLER_ON_TYPES = frozenset({"PowerOn"})


@dataclass(frozen=True)
class ReliabilityAnalysis:
    metrics: list[MetricValue]
    events: list[DetectedEvent]
    context: dict[str, object]


@dataclass
class _BoilerIncident:
    lost_at: datetime
    restored_at: datetime | None
    previous_restore_at: datetime | None
    cause: Literal["power_outage", "zont_restart", "boiler_or_adapter"] = "boiler_or_adapter"


def _metric(period_id: str, name: str, value: float, unit: str, **context: object) -> MetricValue:
    return MetricValue(
        id=f"metric:{period_id}:{name}:reliability-v1",
        name=name,
        value=round(value, 3),
        unit=unit,
        algorithm_version="reliability-v1",
        context=context,
    )


def _ordered_events(events: list[SourceEvent], as_of: datetime) -> list[SourceEvent]:
    return sorted(
        {item.id: item for item in events if item.timestamp_utc < as_of}.values(),
        key=lambda item: (item.timestamp_utc, item.id),
    )


def _intervals_from_events(
    events: list[SourceEvent],
    *,
    loss_types: frozenset[str],
    restore_types: frozenset[str],
    as_of: datetime,
) -> list[tuple[datetime, datetime]]:
    active: datetime | None = None
    result: list[tuple[datetime, datetime]] = []
    for item in events:
        if item.event_type in loss_types and active is None:
            active = item.timestamp_utc
        elif item.event_type in restore_types and active is not None:
            if item.timestamp_utc >= active:
                result.append((active, item.timestamp_utc))
            active = None
    if active is not None:
        result.append((active, as_of))
    return result


def _main_power_intervals(
    events: list[SourceEvent],
    status_flags: list[tuple[datetime, float]],
    as_of: datetime,
) -> list[tuple[datetime, datetime]]:
    result = _intervals_from_events(
        events,
        loss_types=MAIN_POWER_LOSS_TYPES,
        restore_types=MAIN_POWER_RESTORE_TYPES,
        as_of=as_of,
    )
    active: datetime | None = None
    previous_powered: bool | None = None
    for timestamp, raw_value in sorted({timestamp: value for timestamp, value in status_flags}.items()):
        if timestamp >= as_of:
            break
        powered = bool(int(raw_value) & 1)
        if previous_powered is None:
            if not powered:
                active = timestamp
        elif previous_powered and not powered:
            active = timestamp
        elif not previous_powered and powered and active is not None:
            result.append((active, timestamp))
            active = None
        previous_powered = powered
    if active is not None:
        result.append((active, as_of))
    return _merge_intervals(result)


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1] + timedelta(seconds=120):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _boiler_incidents(events: list[SourceEvent]) -> tuple[list[_BoilerIncident], datetime | None]:
    incidents: list[_BoilerIncident] = []
    active: _BoilerIncident | None = None
    last_restore: datetime | None = None
    for item in events:
        if item.event_type in BOILER_LOSS_TYPES and active is None:
            active = _BoilerIncident(item.timestamp_utc, None, last_restore)
        elif item.event_type in BOILER_RESTORE_TYPES:
            if active is not None and item.timestamp_utc >= active.lost_at:
                active.restored_at = item.timestamp_utc
                incidents.append(active)
                active = None
            last_restore = item.timestamp_utc
    if active is not None:
        incidents.append(active)
    return incidents, last_restore


def _classify_incident(
    incident: _BoilerIncident,
    power_intervals: list[tuple[datetime, datetime]],
    controller_intervals: list[tuple[datetime, datetime]],
) -> None:
    skew = timedelta(minutes=2)
    if any(start - skew <= incident.lost_at <= end + skew for start, end in controller_intervals):
        incident.cause = "zont_restart"
    elif any(start - skew <= incident.lost_at <= end + skew for start, end in power_intervals):
        incident.cause = "power_outage"


def _first_sustained_at(
    timestamps: list[datetime],
    *,
    after: datetime | None = None,
    maximum_spacing: timedelta = timedelta(minutes=5),
) -> datetime | None:
    ordered = sorted({item for item in timestamps if after is None or item >= after})
    for index in range(len(ordered) - 2):
        first_gap = ordered[index + 1] - ordered[index]
        second_gap = ordered[index + 2] - ordered[index + 1]
        if first_gap <= maximum_spacing and second_gap <= maximum_spacing:
            return ordered[index]
    return None


def _last_gap_recovery(timestamps: list[datetime], as_of: datetime) -> datetime | None:
    ordered = sorted({item for item in timestamps if item < as_of})
    candidates: list[datetime] = []
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current - previous >= timedelta(minutes=10):
            sustained = _first_sustained_at(ordered, after=current)
            if sustained is not None:
                candidates.append(sustained)
    return candidates[-1] if candidates else None


def _overlap_seconds(start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (min(end, right) - max(start, left)).total_seconds()) for left, right in intervals)


def analyze_reliability(
    *,
    period_id: str,
    period_start: datetime,
    as_of: datetime,
    source_events: list[SourceEvent],
    boiler_metric_timestamps: list[datetime],
    zont_status_samples: list[tuple[datetime, float]],
    zont_metric_timestamps: list[datetime] | None = None,
) -> ReliabilityAnalysis:
    ordered_events = _ordered_events(source_events, as_of)
    zont_timestamps = [
        timestamp
        for timestamp in (
            zont_metric_timestamps
            if zont_metric_timestamps is not None
            else [item[0] for item in zont_status_samples]
        )
        if timestamp < as_of
    ]
    boiler_timestamps = [timestamp for timestamp in boiler_metric_timestamps if timestamp < as_of]
    power_intervals = _main_power_intervals(ordered_events, zont_status_samples, as_of)
    controller_intervals = _intervals_from_events(
        ordered_events,
        loss_types=CONTROLLER_OFF_TYPES,
        restore_types=CONTROLLER_ON_TYPES,
        as_of=as_of,
    )
    incidents, last_boiler_restore = _boiler_incidents(ordered_events)
    for incident in incidents:
        _classify_incident(incident, power_intervals, controller_intervals)

    metrics: list[MetricValue] = []
    detected: list[DetectedEvent] = []
    latest_power_on = max(
        (item.timestamp_utc for item in ordered_events if item.event_type in CONTROLLER_ON_TYPES),
        default=None,
    )
    latest_power_off = max(
        (item.timestamp_utc for item in ordered_events if item.event_type in CONTROLLER_OFF_TYPES),
        default=None,
    )
    gap_recovery = _last_gap_recovery(zont_timestamps, as_of)
    zont_anchor: datetime | None = None
    zont_basis = "insufficient_data"
    zont_lower_bound = False
    if latest_power_off is None or (latest_power_on is not None and latest_power_on >= latest_power_off):
        recovery_candidate = max((item for item in (latest_power_on, gap_recovery) if item is not None), default=None)
        if recovery_candidate is not None:
            zont_anchor = _first_sustained_at(zont_timestamps, after=recovery_candidate)
            zont_basis = (
                "stable_metrics_after_power_on" if latest_power_on == recovery_candidate else "stable_metrics_after_gap"
            )
        else:
            zont_anchor = _first_sustained_at(zont_timestamps)
            zont_basis = "first_sustained_metrics"
            zont_lower_bound = zont_anchor is not None
    if zont_anchor is not None:
        metrics.append(
            _metric(
                period_id,
                "zont_uptime_seconds",
                (as_of - zont_anchor).total_seconds(),
                "s",
                online=True,
                anchor_at=zont_anchor.isoformat(),
                basis=zont_basis,
                lower_bound=zont_lower_bound,
            )
        )

    open_incident = incidents[-1] if incidents and incidents[-1].restored_at is None else None
    boiler_anchor = last_boiler_restore
    boiler_basis = "boiler_connection_restored"
    boiler_lower_bound = False
    if open_incident is None:
        if zont_anchor is not None and (boiler_anchor is None or zont_anchor > boiler_anchor):
            boiler_anchor = _first_sustained_at(boiler_timestamps, after=zont_anchor)
            boiler_basis = "stable_boiler_metrics_after_zont_recovery"
        if boiler_anchor is None:
            boiler_anchor = _first_sustained_at(boiler_timestamps)
            boiler_basis = "first_sustained_boiler_metrics"
            boiler_lower_bound = boiler_anchor is not None
    if boiler_anchor is not None and open_incident is None:
        metrics.append(
            _metric(
                period_id,
                "boiler_uptime_seconds",
                (as_of - boiler_anchor).total_seconds(),
                "s",
                online=True,
                anchor_at=boiler_anchor.isoformat(),
                basis=boiler_basis,
                lower_bound=boiler_lower_bound,
            )
        )

    intrinsic_closed = [
        item for item in incidents if item.cause == "boiler_or_adapter" and item.restored_at is not None
    ]
    restore_seconds = [
        (item.restored_at - item.lost_at).total_seconds() for item in intrinsic_closed if item.restored_at
    ]
    operating_seconds: list[float] = []
    excluded_intervals = _merge_intervals([*power_intervals, *controller_intervals])
    for item in intrinsic_closed:
        if item.previous_restore_at is None:
            continue
        seconds = (item.lost_at - item.previous_restore_at).total_seconds()
        seconds -= _overlap_seconds(item.previous_restore_at, item.lost_at, excluded_intervals)
        if seconds >= 0:
            operating_seconds.append(seconds)
    if operating_seconds:
        metrics.append(
            _metric(
                period_id,
                "boiler_mtbf_hours",
                mean(operating_seconds) / 3600,
                "h",
                completed_intervals=len(operating_seconds),
                excludes_power_outages=True,
            )
        )
    if restore_seconds:
        metrics.append(
            _metric(
                period_id,
                "boiler_mtbr_hours",
                mean(restore_seconds) / 3600,
                "h",
                completed_intervals=len(restore_seconds),
                excludes_power_outages=True,
            )
        )

    for index, item in enumerate(incidents):
        if not (period_start <= item.lost_at < as_of):
            continue
        duration = (item.restored_at - item.lost_at).total_seconds() if item.restored_at else None
        detected.append(
            DetectedEvent(
                id=f"event:{period_id}:boiler_connection_loss:{int(item.lost_at.timestamp())}:{index}:reliability-v1",
                kind="boiler_connection_loss",
                started_at=item.lost_at,
                ended_at=item.restored_at,
                severity="warning" if item.cause == "boiler_or_adapter" else "info",
                details={
                    "cause": item.cause,
                    "duration_seconds": duration,
                    "excluded_from_boiler_reliability": item.cause != "boiler_or_adapter",
                },
                algorithm_version="reliability-v1",
            )
        )
    for index, (started, ended) in enumerate(power_intervals):
        if started < as_of and ended > period_start:
            detected.append(
                DetectedEvent(
                    id=f"event:{period_id}:main_power_outage:{int(started.timestamp())}:{index}:reliability-v1",
                    kind="main_power_outage",
                    started_at=started,
                    ended_at=ended,
                    severity="info",
                    details={"duration_seconds": (ended - started).total_seconds(), "zont_continued_on_battery": True},
                    algorithm_version="reliability-v1",
                )
            )

    context: dict[str, object] = {
        "boiler": {
            "online": open_incident is None and boiler_anchor is not None,
            "uptime_anchor": boiler_anchor.isoformat() if boiler_anchor else None,
            "uptime_basis": boiler_basis if boiler_anchor else "insufficient_data",
            "completed_connection_incidents": sum(item.restored_at is not None for item in incidents),
            "intrinsic_failures": len(intrinsic_closed),
            "power_related_losses": sum(item.cause == "power_outage" for item in incidents),
            "zont_restart_related_losses": sum(item.cause == "zont_restart" for item in incidents),
            "open_loss_at": open_incident.lost_at.isoformat() if open_incident else None,
        },
        "zont": {
            "online": zont_anchor is not None,
            "uptime_anchor": zont_anchor.isoformat() if zont_anchor else None,
            "uptime_basis": zont_basis,
            "lower_bound": zont_lower_bound,
        },
        "main_power_outages": len(power_intervals),
        "policy": "main-power outages and ZONT restarts are excluded from boiler MTBF/MTBR",
    }
    return ReliabilityAnalysis(
        metrics=metrics,
        events=sorted(detected, key=lambda item: item.started_at),
        context=context,
    )

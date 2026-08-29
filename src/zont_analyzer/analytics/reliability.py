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
DEFAULT_MAXIMUM_SAMPLE_AGE = timedelta(minutes=10)
INCIDENT_CAUSE_TOLERANCE = timedelta(minutes=2)
EVENT_INTERVAL_MERGE_TOLERANCE = timedelta(minutes=2)
SERVICE_FAILURE_CAUSES = frozenset({"power_outage", "boiler_or_adapter"})


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
    restore_inferred_from_metrics: bool = False


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
        if merged and start <= merged[-1][1] + EVENT_INTERVAL_MERGE_TOLERANCE:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _union_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
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
    if any(
        start - INCIDENT_CAUSE_TOLERANCE <= incident.lost_at <= end + INCIDENT_CAUSE_TOLERANCE
        for start, end in controller_intervals
    ):
        incident.cause = "zont_restart"
    elif any(abs(incident.lost_at - start) <= INCIDENT_CAUSE_TOLERANCE for start, _ in power_intervals):
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


def _telemetry_gap_intervals(
    timestamps: list[datetime],
    as_of: datetime,
    maximum_sample_age: timedelta,
) -> list[tuple[datetime, datetime]]:
    ordered = sorted({item for item in timestamps if item < as_of})
    if not ordered:
        return []
    result = [
        (previous + maximum_sample_age, current)
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if current - previous > maximum_sample_age
    ]
    if as_of - ordered[-1] > maximum_sample_age:
        result.append((ordered[-1] + maximum_sample_age, as_of))
    return result


def _freshness(
    timestamps: list[datetime],
    as_of: datetime,
    maximum_sample_age: timedelta,
) -> tuple[datetime | None, bool, float | None]:
    latest = max((item for item in timestamps if item < as_of), default=None)
    if latest is None:
        return None, False, None
    age_seconds = max(0.0, (as_of - latest).total_seconds())
    return latest, age_seconds <= maximum_sample_age.total_seconds(), age_seconds


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
    maximum_sample_age: timedelta = DEFAULT_MAXIMUM_SAMPLE_AGE,
) -> ReliabilityAnalysis:
    if maximum_sample_age <= timedelta(0):
        raise ValueError("maximum_sample_age must be positive")
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
    latest_zont_sample, zont_data_fresh, zont_sample_age_seconds = _freshness(
        zont_timestamps,
        as_of,
        maximum_sample_age,
    )
    latest_boiler_sample, boiler_data_fresh, boiler_sample_age_seconds = _freshness(
        boiler_timestamps,
        as_of,
        maximum_sample_age,
    )
    zont_gap_intervals = _telemetry_gap_intervals(zont_timestamps, as_of, maximum_sample_age)
    power_intervals = _main_power_intervals(ordered_events, zont_status_samples, as_of)
    controller_intervals = _merge_intervals(
        [
            *_intervals_from_events(
                ordered_events,
                loss_types=CONTROLLER_OFF_TYPES,
                restore_types=CONTROLLER_ON_TYPES,
                as_of=as_of,
            ),
            *zont_gap_intervals,
        ]
    )
    incidents, last_boiler_restore = _boiler_incidents(ordered_events)
    for incident in incidents:
        _classify_incident(incident, power_intervals, controller_intervals)
    open_incident = incidents[-1] if incidents and incidents[-1].restored_at is None else None
    if open_incident is not None:
        inferred_restore = _first_sustained_at(boiler_timestamps, after=open_incident.lost_at)
        if inferred_restore is not None:
            open_incident.restored_at = inferred_restore
            open_incident.restore_inferred_from_metrics = True
            last_boiler_restore = inferred_restore
            open_incident = None

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
    zont_online = zont_anchor is not None and zont_data_fresh
    if zont_anchor is not None:
        metrics.append(
            _metric(
                period_id,
                "zont_uptime_seconds",
                (as_of - zont_anchor).total_seconds() if zont_online else 0.0,
                "s",
                online=zont_online,
                anchor_at=zont_anchor.isoformat(),
                basis=zont_basis,
                lower_bound=zont_lower_bound,
                last_seen_at=latest_zont_sample.isoformat() if latest_zont_sample else None,
                sample_age_seconds=zont_sample_age_seconds,
                maximum_sample_age_seconds=maximum_sample_age.total_seconds(),
            )
        )

    boiler_anchor = last_boiler_restore
    boiler_basis = "boiler_connection_restored"
    boiler_lower_bound = False
    if open_incident is None:
        # A controller reboot (including a firmware update) does not establish a
        # boiler connection loss.  Keep the boiler's own restoration anchor in
        # that case; only an observed telemetry gap can require a fresh boiler
        # metric anchor when there is no later boiler restore event.
        if (
            zont_basis == "stable_metrics_after_gap"
            and zont_anchor is not None
            and (boiler_anchor is None or zont_anchor > boiler_anchor)
        ):
            boiler_anchor = _first_sustained_at(boiler_timestamps, after=zont_anchor)
            boiler_basis = "stable_boiler_metrics_after_zont_recovery"
        if boiler_anchor is None:
            boiler_anchor = _first_sustained_at(boiler_timestamps)
            boiler_basis = "first_sustained_boiler_metrics"
            boiler_lower_bound = boiler_anchor is not None
    boiler_online = (
        boiler_anchor is not None
        and open_incident is None
        and zont_data_fresh
        and boiler_data_fresh
    )
    if boiler_anchor is not None:
        metrics.append(
            _metric(
                period_id,
                "boiler_uptime_seconds",
                (as_of - boiler_anchor).total_seconds() if boiler_online else 0.0,
                "s",
                online=boiler_online,
                anchor_at=boiler_anchor.isoformat(),
                basis=boiler_basis,
                lower_bound=boiler_lower_bound,
                last_seen_at=latest_boiler_sample.isoformat() if latest_boiler_sample else None,
                sample_age_seconds=boiler_sample_age_seconds,
                maximum_sample_age_seconds=maximum_sample_age.total_seconds(),
                zont_data_fresh=zont_data_fresh,
            )
        )

    service_failures = [item for item in incidents if item.cause in SERVICE_FAILURE_CAUSES]
    completed_service_failures = [item for item in service_failures if item.restored_at is not None]
    restore_seconds = [
        (item.restored_at - item.lost_at).total_seconds()
        for item in completed_service_failures
        if item.restored_at and not item.restore_inferred_from_metrics
    ]
    confirmed_power_intervals = [
        (start, end)
        for start, end in power_intervals
        if any(
            item.cause == "power_outage" and abs(item.lost_at - start) <= INCIDENT_CAUSE_TOLERANCE
            for item in service_failures
        )
    ]
    service_downtime_intervals = [(item.lost_at, item.restored_at or as_of) for item in service_failures]
    observability_incident_intervals = [
        (item.lost_at, item.restored_at or as_of) for item in incidents if item.cause == "zont_restart"
    ]
    first_sustained_boiler = _first_sustained_at(boiler_timestamps)
    first_boiler_restore = min(
        (item.timestamp_utc for item in ordered_events if item.event_type in BOILER_RESTORE_TYPES),
        default=None,
    )
    observation_start = (
        max(first_sustained_boiler, first_boiler_restore)
        if first_sustained_boiler is not None and first_boiler_restore is not None
        else first_sustained_boiler
    )
    operating_seconds: float | None = None
    if observation_start is not None:
        excluded_intervals = _union_intervals(
            [
                *controller_intervals,
                *confirmed_power_intervals,
                *service_downtime_intervals,
                *observability_incident_intervals,
            ]
        )
        observed_seconds = max(0.0, (as_of - observation_start).total_seconds())
        operating_seconds = max(
            0.0,
            observed_seconds - _overlap_seconds(observation_start, as_of, excluded_intervals),
        )
    if operating_seconds is not None and zont_data_fresh and (service_failures or boiler_online):
        assert observation_start is not None
        failure_count = len(service_failures)
        metrics.append(
            _metric(
                period_id,
                "boiler_mtbf_hours",
                (operating_seconds / failure_count if failure_count else operating_seconds) / 3600,
                "h",
                formula="observed_operating_seconds / confirmed_service_failures",
                observed_operating_seconds=operating_seconds,
                observation_start=observation_start.isoformat(),
                confirmed_failures=failure_count,
                completed_failures=len(completed_service_failures),
                lower_bound=failure_count == 0,
                includes_current_uptime=boiler_online,
            )
        )
    if restore_seconds and zont_data_fresh:
        metrics.append(
            _metric(
                period_id,
                "boiler_mttr_hours",
                mean(restore_seconds) / 3600,
                "h",
                completed_failures=len(restore_seconds),
                includes_power_outages=True,
                excludes_inferred_recoveries=True,
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
                severity="info" if item.cause == "zont_restart" else "warning",
                details={
                    "cause": item.cause,
                    "duration_seconds": duration,
                    "excluded_from_boiler_reliability": item.cause == "zont_restart",
                    "restore_inferred_from_stable_metrics": item.restore_inferred_from_metrics,
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
            "online": boiler_online,
            "uptime_anchor": boiler_anchor.isoformat() if boiler_anchor else None,
            "uptime_basis": boiler_basis if boiler_anchor else "insufficient_data",
            "last_seen_at": latest_boiler_sample.isoformat() if latest_boiler_sample else None,
            "sample_age_seconds": boiler_sample_age_seconds,
            "data_fresh": boiler_data_fresh,
            "completed_connection_incidents": sum(item.restored_at is not None for item in incidents),
            "intrinsic_failures": sum(item.cause == "boiler_or_adapter" for item in service_failures),
            "confirmed_service_failures": len(service_failures),
            "completed_service_failures": len(completed_service_failures),
            "power_related_losses": sum(item.cause == "power_outage" for item in incidents),
            "zont_restart_related_losses": sum(item.cause == "zont_restart" for item in incidents),
            "open_loss_at": open_incident.lost_at.isoformat() if open_incident else None,
        },
        "zont": {
            "online": zont_online,
            "uptime_anchor": zont_anchor.isoformat() if zont_anchor else None,
            "uptime_basis": zont_basis,
            "lower_bound": zont_lower_bound,
            "last_seen_at": latest_zont_sample.isoformat() if latest_zont_sample else None,
            "sample_age_seconds": zont_sample_age_seconds,
            "data_fresh": zont_data_fresh,
            "telemetry_gaps": len(zont_gap_intervals),
        },
        "main_power_outages": len(power_intervals),
        "policy": (
            "power_outage and boiler_or_adapter incidents are service failures included in boiler MTBF/MTTR; "
            "zont_restart incidents are observability losses excluded from both metrics"
        ),
    }
    return ReliabilityAnalysis(
        metrics=metrics,
        events=sorted(detected, key=lambda item: item.started_at),
        context=context,
    )

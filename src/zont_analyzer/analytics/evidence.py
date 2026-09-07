"""Deterministic, bounded temporal evidence for the stage-2 AI packet.

This module deliberately reports measurements and elementary arithmetic only.  It
does not diagnose a heating system from those measurements.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

ALGORITHM_VERSION = "heating-evidence-v1"
MAX_PACKET_BYTES = 40_000
MAX_SIGNAL_SERIES = 24


@dataclass(frozen=True)
class SignalMetadata:
    """Stable description of one numeric telemetry series.

    ``identity`` is the source-side device/source/entity/key identity, not a
    user supplied room label.  ``role`` is intentionally a small vocabulary so
    calculations stay portable; unknown series are still represented as
    ``other``.
    """

    identity: str
    display_name: str
    unit: str
    origin: Literal["observed", "derived"] = "observed"
    provenance: str = "unknown"
    role: Literal[
        "control_temperature",
        "target_temperature",
        "outdoor_temperature",
        "flow_temperature",
        "return_temperature",
        "target_flow_temperature",
        "modulation",
        "dhw_temperature",
        "recirculation",
        "room",
        "other",
    ] = "other"


@dataclass(frozen=True, order=True)
class NumericSample:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class SignalSeries:
    key: str
    metadata: SignalMetadata
    samples: tuple[NumericSample, ...]


@dataclass(frozen=True, order=True)
class StateSample:
    """OpenTherm state at ``timestamp``; only flags are used for activity."""

    timestamp: datetime
    flags: frozenset[str] = field(default_factory=frozenset)
    identity: str = "unknown"


@dataclass(frozen=True, order=True)
class ExclusionWindow:
    start: datetime
    end: datetime
    reason: Literal["dhw", "inactive", "transition", "reliability", "noise"]
    source: str = "derived"


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceStatistic(_Output):
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    median: float | None = None
    p90: float | None = None
    first: float | None = None
    last: float | None = None
    change: float | None = None
    slope_per_hour: float | None = None
    coverage_pct: float = Field(ge=0, le=100)
    stale_seconds: float | None = Field(default=None, ge=0)
    sample_count: int = Field(ge=0)
    source: Literal["observed", "derived"]


class EvidenceWindow(_Output):
    id: str
    started_at: datetime
    ended_at: datetime
    timezone: str
    kind: Literal["hour", "representative"] = "hour"
    tags: list[str] = Field(default_factory=list)
    excluded_reasons: list[str] = Field(default_factory=list)
    signals: dict[str, EvidenceStatistic] = Field(default_factory=dict)
    facts: dict[str, EvidenceStatistic] = Field(default_factory=dict)


class EvidenceMetric(_Output):
    id: str
    name: str
    value: float | None = None
    unit: str
    source: Literal["observed", "derived"]
    denominator: float | None = None
    denominator_unit: str | None = None
    coverage_pct: float | None = Field(default=None, ge=0, le=100)
    unavailable_reason: str | None = None


class EvidenceExclusion(_Output):
    id: str
    started_at: datetime
    ended_at: datetime
    reason: str
    source: str


class EvidencePacket(_Output):
    algorithm_version: str = ALGORITHM_VERSION
    period_start: datetime
    period_end: datetime
    timezone: str
    capability_profile: Literal["unknown", "flame_zero_is_minimum"]
    signals: dict[str, dict[str, str]]
    windows: list[EvidenceWindow]
    metrics: list[EvidenceMetric]
    quality: dict[str, EvidenceStatistic]
    exclusions: dict[str, float]
    exclusion_windows: list[EvidenceExclusion]
    state_source: str | None = None
    unknowns: list[str]


def build_evidence(
    *,
    start: datetime,
    end: datetime,
    timezone: str,
    signals: tuple[SignalSeries, ...] | list[SignalSeries],
    state_samples: tuple[StateSample, ...] | list[StateSample] = (),
    exclusions: tuple[ExclusionWindow, ...] | list[ExclusionWindow] = (),
    period_id: str = "period",
    capability_profile: Literal["unknown", "flame_zero_is_minimum"] = "unknown",
    max_windows: int = 24,
    window_sink: list[EvidenceWindow] | None = None,
    min_coverage_pct: float = 70.0,
) -> EvidencePacket:
    """Build a compact packet using interval sweeps, never minute expansion."""

    if end <= start:
        raise ValueError("end must be after start")
    if max_windows < 1:
        raise ValueError("max_windows must be positive")
    if not 0 <= min_coverage_pct <= 100:
        raise ValueError("min_coverage_pct must be between 0 and 100")

    period_id = _safe_text(period_id, 80)
    normalised = [_normalise_series(series) for series in sorted(signals, key=lambda item: item.key)]
    # Keep the canonical roles first; arbitrary extra rooms remain deterministic.
    normalised.sort(key=lambda item: (item.metadata.role == "other", item.key))
    omitted_signals = [item.key for item in normalised[MAX_SIGNAL_SERIES:]]
    ordered_signals = {series.key: series for series in normalised[:MAX_SIGNAL_SERIES]}
    ordered_exclusions = tuple(
        sorted(
            (item for item in exclusions if item.start < item.end), key=lambda item: (item.start, item.end, item.reason)
        )
    )
    ordered_states = _normalise_states(state_samples)
    dhw_exclusions = tuple(
        ExclusionWindow(left, right, "dhw", "boiler_state")
        for left, right, state in _state_intervals(ordered_states, start, end)
        if "dhw" in state.flags
    )
    ordered_exclusions = _merge_exclusions((*ordered_exclusions, *dhw_exclusions), start, end)
    boundaries = _hour_boundaries(start, end)
    selected = _bounded_windows(boundaries, max_windows)
    windows = [
        _build_window(
            period_id=period_id,
            start=window_start,
            end=window_end,
            timezone=timezone,
            signals=ordered_signals,
            states=ordered_states,
            exclusions=ordered_exclusions,
        )
        for window_start, window_end in selected
    ]
    if window_sink is not None:
        window_sink.extend(windows)
    metrics, state_unknowns = _operational_metrics(
        period_id=period_id,
        start=start,
        end=end,
        states=ordered_states,
        signals=ordered_signals,
        exclusions=ordered_exclusions,
        capability_profile=capability_profile,
        min_coverage_pct=min_coverage_pct,
    )
    metadata = {
        key: {
            "identity": series.metadata.identity,
            "display_name": series.metadata.display_name,
            "unit": series.metadata.unit,
            "origin": series.metadata.origin,
            "provenance": series.metadata.provenance,
            "role": series.metadata.role,
        }
        for key, series in ordered_signals.items()
    }
    expected = {
        "control_temperature",
        "target_temperature",
        "outdoor_temperature",
        "flow_temperature",
        "return_temperature",
        "target_flow_temperature",
        "modulation",
        "dhw_temperature",
        "recirculation",
    }
    roles = {series.metadata.role for series in ordered_signals.values()}
    unknowns = sorted(
        {f"missing:{role}" for role in expected - roles}
        | set(state_unknowns)
        | ({f"omitted_signal_count:{len(omitted_signals)}"} if omitted_signals else set())
    )
    exclusion_seconds = {
        reason: round(
            _union_seconds(
                start, end, [(item.start, item.end) for item in ordered_exclusions if item.reason == reason]
            ),
            3,
        )
        for reason in ("dhw", "inactive", "transition", "reliability", "noise")
    }
    quality = {key: _statistic(series, start, end) for key, series in ordered_signals.items()}
    state_sources = sorted({sample.identity for sample in ordered_states if sample.identity != "unknown"})
    listed_exclusions = [
        EvidenceExclusion(
            id=f"exclusion:{period_id}:{item.reason}:{int(item.start.timestamp())}:{int(item.end.timestamp())}",
            started_at=item.start,
            ended_at=item.end,
            reason=item.reason,
            source=item.source,
        )
        for item in ordered_exclusions[:64]
    ]
    packet = EvidencePacket(
        period_start=start,
        period_end=end,
        timezone=timezone,
        capability_profile=capability_profile,
        signals=metadata,
        windows=windows,
        metrics=metrics,
        quality=quality,
        exclusions=exclusion_seconds,
        exclusion_windows=listed_exclusions,
        state_source=_safe_text(",".join(state_sources), 320) if state_sources else None,
        unknowns=unknowns,
    )
    if len(ordered_exclusions) > len(listed_exclusions):
        packet.unknowns.append(f"omitted:exclusion_windows:{len(ordered_exclusions) - len(listed_exclusions)}")
    # The packet is passed as JSON to a bounded AI context.  Reduce only whole
    # hourly windows and retain a deterministic first/last spread.
    while len(packet.model_dump_json().encode()) > MAX_PACKET_BYTES - 256 and len(packet.windows) > 1:
        retained = _bounded_windows([(item.started_at, item.ended_at) for item in windows], len(packet.windows) - 1)
        retained_starts = {left for left, _right in retained}
        packet = packet.model_copy(update={"windows": [item for item in windows if item.started_at in retained_starts]})
    if len(packet.model_dump_json().encode()) > MAX_PACKET_BYTES - 256:
        packet = packet.model_copy(
            update={"windows": [], "unknowns": sorted([*packet.unknowns, "omitted:windows_size_cap"])}
        )
    if len(packet.windows) < len(boundaries):
        packet.unknowns.append(f"omitted:hourly_windows:{len(boundaries) - len(packet.windows)}")
    # Metadata itself can exhaust the budget even with no windows. Drop whole
    # sources with their quality records; never publish orphaned numeric values.
    dropped = 0
    while len(packet.model_dump_json().encode()) > MAX_PACKET_BYTES - 128 and packet.exclusion_windows:
        packet.exclusion_windows.pop()
        dropped += 1
    if dropped:
        packet.unknowns.append(f"omitted:exclusion_windows_size_cap:{dropped}")
    dropped = 0
    while len(packet.model_dump_json().encode()) > MAX_PACKET_BYTES - 128 and packet.signals:
        key = next(reversed(packet.signals))
        packet.signals.pop(key)
        packet.quality.pop(key, None)
        dropped += 1
    if dropped:
        packet.unknowns.append(f"omitted:signal_metadata_size_cap:{dropped}")
    return packet


def _merge_exclusions(
    windows: tuple[ExclusionWindow, ...],
    start: datetime,
    end: datetime,
) -> tuple[ExclusionWindow, ...]:
    merged: list[ExclusionWindow] = []
    for reason in ("dhw", "inactive", "transition", "reliability", "noise"):
        candidates = sorted(
            (item for item in windows if item.reason == reason and item.start < end and item.end > start),
            key=lambda item: (item.start, item.end),
        )
        for item in candidates:
            left, right = max(start, item.start), min(end, item.end)
            if merged and merged[-1].reason == reason and merged[-1].end >= left:
                previous = merged.pop()
                merged.append(ExclusionWindow(previous.start, max(previous.end, right), item.reason))
            else:
                merged.append(ExclusionWindow(left, right, item.reason))
    return tuple(sorted(merged, key=lambda item: (item.start, item.end, item.reason)))


def _normalise_series(series: SignalSeries) -> SignalSeries:
    # Conflicting duplicate readings are ambiguous: omit that timestamp rather
    # than making caller order alter a fact.
    values: dict[datetime, set[float]] = {}
    for sample in series.samples:
        values.setdefault(sample.timestamp, set()).add(sample.value)
    metadata = SignalMetadata(
        identity=_safe_text(series.metadata.identity, 160),
        display_name=_safe_text(series.metadata.display_name, 96),
        unit=_safe_text(series.metadata.unit, 32),
        origin=series.metadata.origin,
        provenance=_safe_text(series.metadata.provenance, 160),
        role=series.metadata.role,
    )
    return SignalSeries(
        _safe_text(series.key, 96),
        metadata,
        tuple(
            NumericSample(timestamp, next(iter(values[timestamp])))
            for timestamp in sorted(values)
            if len(values[timestamp]) == 1
        ),
    )


def _normalise_states(samples: tuple[StateSample, ...] | list[StateSample]) -> tuple[StateSample, ...]:
    values: dict[datetime, set[tuple[frozenset[str], str]]] = {}
    for sample in samples:
        values.setdefault(sample.timestamp, set()).add(
            (frozenset(flag.casefold() for flag in sample.flags), sample.identity)
        )
    return tuple(
        StateSample(timestamp, next(iter(values[timestamp]))[0], _safe_text(next(iter(values[timestamp]))[1], 160))
        for timestamp in sorted(values)
        if len(values[timestamp]) == 1
    )


def _safe_text(value: str, limit: int) -> str:
    if len(value.encode()) <= limit:
        return value
    digest = sha256(value.encode()).hexdigest()[:12]
    kept = value.encode()[: max(0, limit - len(digest) - 1)].decode(errors="ignore")
    return f"{kept}:{digest}"


def _hour_boundaries(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        following = min(cursor + timedelta(hours=1), end)
        result.append((cursor, following))
        cursor = following
    return result


def _bounded_windows(windows: list[tuple[datetime, datetime]], maximum: int) -> list[tuple[datetime, datetime]]:
    if len(windows) <= maximum:
        return windows
    # Uniformly select real hourly windows; no synthetic aggregate period is
    # introduced for long histories, so the same cap applies at every length.
    indices = (
        sorted({round(index * (len(windows) - 1) / (maximum - 1)) for index in range(maximum)}) if maximum > 1 else [0]
    )
    return [windows[index] for index in indices]


def _cadence_seconds(samples: tuple[NumericSample, ...]) -> float:
    gaps = [
        (right.timestamp - left.timestamp).total_seconds()
        for left, right in zip(samples, samples[1:], strict=False)
        if right.timestamp > left.timestamp
    ]
    return max(60.0, median(gaps) * 3) if gaps else 0.0


def _statistic(
    series: SignalSeries,
    start: datetime,
    end: datetime,
    transform: Callable[[float], float] | None = None,
) -> EvidenceStatistic:
    samples = series.samples
    cadence = _cadence_seconds(samples)
    if not samples or cadence <= 0:
        return EvidenceStatistic(coverage_pct=0, sample_count=0, source="derived")
    points = [sample for sample in samples if start <= sample.timestamp <= end]
    previous = next((sample for sample in reversed(samples) if sample.timestamp < start), None)
    timeline = [start] + [sample.timestamp for sample in points if start < sample.timestamp < end] + [end]
    weighted = 0.0
    observed = 0.0
    values: list[float] = []
    weights: list[float] = []
    current = previous
    point_index = 0
    for left, right in zip(timeline, timeline[1:], strict=False):
        while point_index < len(points) and points[point_index].timestamp <= left:
            current = points[point_index]
            point_index += 1
        if current is None:
            continue
        usable_end = min(right, current.timestamp + timedelta(seconds=cadence))
        seconds = max(0.0, (usable_end - left).total_seconds())
        if seconds:
            value = transform(current.value) if transform else current.value
            weighted += value * seconds
            observed += seconds
            values.append(value)
            weights.append(seconds)
    latest = next((sample for sample in reversed(samples) if sample.timestamp <= end), None)
    stale = (end - latest.timestamp).total_seconds() if latest is not None else None
    first = values[0] if values else None
    last = values[-1] if values else None
    duration_hours = (end - start).total_seconds() / 3600
    return EvidenceStatistic(
        mean=round(weighted / observed, 4) if observed else None,
        minimum=round(min(values), 4) if values else None,
        maximum=round(max(values), 4) if values else None,
        median=round(_weighted_quantile(values, weights, 0.5) or 0.0, 4) if values else None,
        p90=round(_weighted_quantile(values, weights, 0.9) or 0.0, 4) if values else None,
        first=round(first, 4) if first is not None else None,
        last=round(last, 4) if last is not None else None,
        change=round(last - first, 4) if first is not None and last is not None else None,
        slope_per_hour=round((last - first) / duration_hours, 4)
        if first is not None and last is not None and duration_hours
        else None,
        coverage_pct=round(observed / (end - start).total_seconds() * 100, 2),
        stale_seconds=round(max(stale, 0.0), 2) if stale is not None else None,
        sample_count=len(points),
        source="derived",
    )


def _derived_statistic(
    left: SignalSeries | None,
    right: SignalSeries | None,
    start: datetime,
    end: datetime,
    operation: Callable[[float, float], float],
    exclusions: tuple[ExclusionWindow, ...] = (),
) -> EvidenceStatistic | None:
    if left is None or right is None:
        return None
    boundaries = {start, end}
    boundaries.update(sample.timestamp for sample in left.samples if start < sample.timestamp < end)
    boundaries.update(sample.timestamp for sample in right.samples if start < sample.timestamp < end)
    boundaries.update(item.start for item in exclusions if start < item.start < end)
    boundaries.update(item.end for item in exclusions if start < item.end < end)
    left_cadence = _cadence_seconds(left.samples)
    right_cadence = _cadence_seconds(right.samples)
    if not left_cadence or not right_cadence:
        return EvidenceStatistic(coverage_pct=0, sample_count=0, source="derived")
    left_value = _value_before(left.samples, start)
    right_value = _value_before(right.samples, start)
    left_points = iter(sample for sample in left.samples if start <= sample.timestamp < end)
    right_points = iter(sample for sample in right.samples if start <= sample.timestamp < end)
    next_left = next(left_points, None)
    next_right = next(right_points, None)
    weighted = 0.0
    observed = 0.0
    values: list[float] = []
    weights: list[float] = []
    ordered = sorted(boundaries)
    for window_start, window_end in zip(ordered, ordered[1:], strict=False):
        while next_left is not None and next_left.timestamp <= window_start:
            left_value, next_left = next_left, next(left_points, None)
        while next_right is not None and next_right.timestamp <= window_start:
            right_value, next_right = next_right, next(right_points, None)
        if left_value is None or right_value is None:
            continue
        fresh_until = min(
            window_end,
            left_value.timestamp + timedelta(seconds=left_cadence),
            right_value.timestamp + timedelta(seconds=right_cadence),
        )
        seconds = (
            0.0
            if _excluded(window_start, fresh_until, exclusions)
            else max(0.0, (fresh_until - window_start).total_seconds())
        )
        if seconds:
            value = operation(left_value.value, right_value.value)
            weighted += value * seconds
            observed += seconds
            values.append(value)
            weights.append(seconds)
    return EvidenceStatistic(
        mean=round(weighted / observed, 4) if observed else None,
        minimum=round(min(values), 4) if values else None,
        maximum=round(max(values), 4) if values else None,
        median=round(_weighted_quantile(values, weights, 0.5) or 0.0, 4) if values else None,
        p90=round(_weighted_quantile(values, weights, 0.9) or 0.0, 4) if values else None,
        first=round(values[0], 4) if values else None,
        last=round(values[-1], 4) if values else None,
        change=round(values[-1] - values[0], 4) if values else None,
        slope_per_hour=round((values[-1] - values[0]) / ((end - start).total_seconds() / 3600), 4) if values else None,
        coverage_pct=round(observed / (end - start).total_seconds() * 100, 2),
        stale_seconds=None,
        sample_count=len(values),
        source="derived",
    )


def _value_before(samples: tuple[NumericSample, ...], timestamp: datetime) -> NumericSample | None:
    return next((sample for sample in reversed(samples) if sample.timestamp <= timestamp), None)


def _by_role(signals: dict[str, SignalSeries], role: str) -> SignalSeries | None:
    return next((series for _key, series in sorted(signals.items()) if series.metadata.role == role), None)


def _build_window(
    *,
    period_id: str,
    start: datetime,
    end: datetime,
    timezone: str,
    signals: dict[str, SignalSeries],
    states: tuple[StateSample, ...],
    exclusions: tuple[ExclusionWindow, ...],
) -> EvidenceWindow:
    statistics = {key: _statistic(series, start, end) for key, series in signals.items()}
    facts: dict[str, EvidenceStatistic] = {}
    pairs = {
        "room_error_c": (_by_role(signals, "control_temperature"), _by_role(signals, "target_temperature")),
        "flow_vs_cs_c": (_by_role(signals, "flow_temperature"), _by_role(signals, "target_flow_temperature")),
        "delta_t_c": (_by_role(signals, "flow_temperature"), _by_role(signals, "return_temperature")),
    }
    for key, (left, right) in pairs.items():
        value = _derived_statistic(left, right, start, end, lambda a, b: a - b)
        if value is not None:
            facts[key] = value
    weather = _by_role(signals, "outdoor_temperature")
    if weather is not None:
        facts.update(_weather_facts(weather, start, end))
    state_intervals = _state_intervals(states, start, end)
    observed = sum((right - left).total_seconds() for left, right, _state in state_intervals)
    shares = {
        "heating_request_pct": sum(
            (right - left).total_seconds()
            for left, right, state in state_intervals
            if "ch" in state.flags and "dhw" not in state.flags
        ),
        "flame_pct": sum(
            (right - left).total_seconds()
            for left, right, state in state_intervals
            if "fl" in state.flags and "ch" in state.flags and "dhw" not in state.flags
        ),
        "dhw_pct": sum(
            (right - left).total_seconds() for left, right, state in state_intervals if "dhw" in state.flags
        ),
    }
    for name, active in shares.items():
        facts[name] = _single_value_stat(active / observed * 100 if observed else None, len(state_intervals))
    reasons = sorted(str(item.reason) for item in exclusions if item.start < end and item.end > start)
    local_hour = start.astimezone(ZoneInfo(timezone)).hour
    tags = ["night"] if local_hour < 6 else ["morning"] if local_hour < 10 else []
    return EvidenceWindow(
        id=f"evidence:{period_id}:hour:{int(start.timestamp())}:{ALGORITHM_VERSION}",
        started_at=start,
        ended_at=end,
        timezone=timezone,
        tags=tags,
        excluded_reasons=reasons,
        signals=statistics,
        facts=facts,
    )


def _weather_facts(series: SignalSeries, start: datetime, end: datetime) -> dict[str, EvidenceStatistic]:
    points = [sample for sample in series.samples if start <= sample.timestamp <= end]
    gaps = [
        (right.timestamp - left.timestamp).total_seconds()
        for left, right in zip(points, points[1:], strict=False)
        if right.timestamp > left.timestamp
    ]
    jumps = sum(abs(right.value - left.value) > 5 for left, right in zip(points, points[1:], strict=False))
    plateaus = sum(left.value == right.value for left, right in zip(points, points[1:], strict=False))
    # Numeric counts are facts about the raw source, not a conclusion about why
    # it has steps or jumps.
    return {
        "weather_cadence_seconds": EvidenceStatistic(
            mean=round(median(gaps), 3) if gaps else None,
            minimum=round(min(gaps), 3) if gaps else None,
            maximum=round(max(gaps), 3) if gaps else None,
            median=round(median(gaps), 3) if gaps else None,
            p90=round(_percentile(gaps, 0.9) or 0.0, 3) if gaps else None,
            coverage_pct=100 if points else 0,
            stale_seconds=None,
            sample_count=len(gaps),
            source="observed",
        ),
        "weather_plateau_transitions": EvidenceStatistic(
            mean=float(plateaus),
            minimum=float(plateaus),
            maximum=float(plateaus),
            median=float(plateaus),
            p90=float(plateaus),
            coverage_pct=100 if points else 0,
            stale_seconds=None,
            sample_count=len(points),
            source="observed",
        ),
        "weather_jumps_over_5c": EvidenceStatistic(
            mean=float(jumps),
            minimum=float(jumps),
            maximum=float(jumps),
            median=float(jumps),
            p90=float(jumps),
            coverage_pct=100 if points else 0,
            stale_seconds=None,
            sample_count=len(points),
            source="observed",
        ),
        "weather_change_c": _single_value_stat(
            points[-1].value - points[0].value if len(points) > 1 else None, len(points)
        ),
    }


def _single_value_stat(
    value: float | None, samples: int, *, source: Literal["observed", "derived"] = "derived"
) -> EvidenceStatistic:
    rounded = round(value, 4) if value is not None else None
    return EvidenceStatistic(
        mean=rounded,
        minimum=rounded,
        maximum=rounded,
        median=rounded,
        p90=rounded,
        first=rounded,
        last=rounded,
        change=0.0 if rounded is not None else None,
        slope_per_hour=0.0 if rounded is not None else None,
        coverage_pct=100 if value is not None else 0,
        stale_seconds=None,
        sample_count=samples,
        source=source,
    )


def _union_seconds(start: datetime, end: datetime, ranges: list[tuple[datetime, datetime]]) -> float:
    clipped = sorted((max(start, left), min(end, right)) for left, right in ranges if left < end and right > start)
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for left, right in clipped:
        if current_start is None:
            current_start, current_end = left, right
        else:
            assert current_end is not None
            if left <= current_end:
                current_end = max(current_end, right)
                continue
            assert current_start is not None
            total += (current_end - current_start).total_seconds()
            current_start, current_end = left, right
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return total


def _excluded(start: datetime, end: datetime, exclusions: tuple[ExclusionWindow, ...]) -> bool:
    return any(item.start < end and item.end > start for item in exclusions)


def _split_state_intervals(
    intervals: list[tuple[datetime, datetime, StateSample]], exclusions: tuple[ExclusionWindow, ...]
) -> list[tuple[datetime, datetime, StateSample]]:
    """Subtract exclusion boundaries so a partial overlap does not discard its neighbours."""
    result: list[tuple[datetime, datetime, StateSample]] = []
    for start, end, state in intervals:
        boundaries = {start, end}
        for exclusion in exclusions:
            if exclusion.start < end and exclusion.end > start:
                boundaries.update((max(start, exclusion.start), min(end, exclusion.end)))
        ordered = sorted(boundaries)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if left < right and not _excluded(left, right, exclusions):
                result.append((left, right, state))
    return result


def _state_intervals(
    states: tuple[StateSample, ...], start: datetime, end: datetime
) -> list[tuple[datetime, datetime, StateSample]]:
    if len(states) < 2:
        return []
    gaps = [(right.timestamp - left.timestamp).total_seconds() for left, right in zip(states, states[1:], strict=False)]
    positive = [gap for gap in gaps if gap > 0]
    allowed_gap = min(1800.0, max(60.0, median(positive) * 3)) if positive else 0.0
    result: list[tuple[datetime, datetime, StateSample]] = []
    for left, right in zip(states, states[1:], strict=False):
        interval_start, interval_end = max(start, left.timestamp), min(end, right.timestamp)
        if interval_start < interval_end and (right.timestamp - left.timestamp).total_seconds() <= allowed_gap:
            result.append((interval_start, interval_end, left))
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _weighted_quantile(values: list[float], weights: list[float], fraction: float) -> float | None:
    if not values or len(values) != len(weights):
        return None
    total = sum(weights)
    if total <= 0:
        return None
    threshold = total * fraction
    cumulative = 0.0
    for value, weight in sorted(zip(values, weights, strict=True)):
        cumulative += weight
        if cumulative >= threshold:
            return value
    return max(values)


def _metric(
    period_id: str,
    name: str,
    value: float | None,
    unit: str,
    *,
    denominator: float | None = None,
    denominator_unit: str | None = None,
    coverage_pct: float | None = None,
    unavailable_reason: str | None = None,
) -> EvidenceMetric:
    return EvidenceMetric(
        id=f"metric:{period_id}:{name}:{ALGORITHM_VERSION}",
        name=name,
        value=round(value, 4) if value is not None else None,
        unit=unit,
        source="derived",
        denominator=round(denominator, 4) if denominator is not None else None,
        denominator_unit=denominator_unit,
        coverage_pct=round(coverage_pct, 2) if coverage_pct is not None else None,
        unavailable_reason=unavailable_reason,
    )


def _operational_metrics(
    *,
    period_id: str,
    start: datetime,
    end: datetime,
    states: tuple[StateSample, ...],
    signals: dict[str, SignalSeries],
    exclusions: tuple[ExclusionWindow, ...],
    capability_profile: Literal["unknown", "flame_zero_is_minimum"],
    min_coverage_pct: float,
) -> tuple[list[EvidenceMetric], list[str]]:
    """Calculate explicit, non-diagnostic operational ratios from flag intervals."""

    period_seconds = (end - start).total_seconds()
    intervals = _state_intervals(states, start, end)
    observed_seconds = sum((right - left).total_seconds() for left, right, _state in intervals)
    coverage = observed_seconds / period_seconds * 100
    unknowns: list[str] = []
    if not intervals or coverage < min_coverage_pct:
        unknowns.append("insufficient:boiler_state_coverage")
        reason = "insufficient_boiler_state_coverage"
        names = (
            ("burner_starts_per_active_request_hour", "count/hour"),
            ("burner_starts_per_observed_hour", "count/hour"),
            ("burner_cycle_median_seconds", "seconds"),
            ("burner_cycle_p90_seconds", "seconds"),
            ("burner_longest_cycle_seconds", "seconds"),
            ("burner_runtime_request_ratio", "ratio"),
            ("active_request_without_flame_ratio", "ratio"),
            ("flame_modulation_mean", "vendor_percent"),
            ("flame_modulation_median", "vendor_percent"),
            ("flow_vs_cs_typical_c", "celsius"),
            ("flow_vs_cs_p90_c", "celsius"),
            ("flow_vs_cs_max_positive_overshoot_c", "celsius"),
        )
        return (
            [
                _metric(period_id, name, None, unit, coverage_pct=coverage, unavailable_reason=reason)
                for name, unit in names
            ],
            unknowns,
        )

    valid = _split_state_intervals(intervals, exclusions)
    observed_valid = sum((right - left).total_seconds() for left, right, _state in valid)
    demand = [(left, right, state) for left, right, state in valid if "ch" in state.flags and "dhw" not in state.flags]
    flame = [
        (left, right, state)
        for left, right, state in valid
        if "fl" in state.flags and "ch" in state.flags and "dhw" not in state.flags
    ]
    demand_seconds = sum((right - left).total_seconds() for left, right, _state in demand)
    flame_seconds = sum((right - left).total_seconds() for left, right, _state in flame)
    demand_flame_seconds = sum((right - left).total_seconds() for left, right, state in demand if "fl" in state.flags)
    cycles: list[tuple[datetime, datetime]] = []
    cycle_start: datetime | None = None
    previous_end: datetime | None = None
    previous_flame: bool | None = None
    starts = 0
    for left, right, state in valid:
        raw_flame = "fl" in state.flags
        heating = "ch" in state.flags and "dhw" not in state.flags
        if previous_end != left:
            cycle_start, previous_flame = None, None
        if raw_flame and heating and previous_flame is False:
            starts += 1
            cycle_start = left
        elif not raw_flame and cycle_start is not None:
            cycles.append((cycle_start, left))
            cycle_start = None
        elif raw_flame and not heating:
            cycle_start = None
        previous_flame, previous_end = raw_flame, right
    durations = [(right - left).total_seconds() for left, right in cycles if right > left]
    cycle_count = starts
    eligible_seconds = period_seconds - _union_seconds(start, end, [(item.start, item.end) for item in exclusions])
    base_coverage = observed_valid / eligible_seconds * 100 if eligible_seconds > 0 else 0.0
    enough_observed = observed_valid >= 60 and base_coverage >= min_coverage_pct
    enough_demand = demand_seconds >= 60 and enough_observed
    metrics = [
        _metric(
            period_id,
            "burner_starts_per_active_request_hour",
            cycle_count / (demand_seconds / 3600) if enough_demand else None,
            "count/hour",
            denominator=demand_seconds / 3600 if demand_seconds else None,
            denominator_unit="active_request_hour",
            coverage_pct=base_coverage,
            unavailable_reason=None if enough_demand else "no_qualified_active_request",
        ),
        _metric(
            period_id,
            "burner_starts_per_observed_hour",
            cycle_count / (observed_valid / 3600) if enough_observed else None,
            "count/hour",
            denominator=observed_valid / 3600 if observed_valid else None,
            denominator_unit="observed_hour",
            coverage_pct=base_coverage,
            unavailable_reason=None if enough_observed else "no_qualified_observed_time",
        ),
        _metric(
            period_id,
            "burner_cycle_median_seconds",
            median(durations) if durations else None,
            "seconds",
            coverage_pct=base_coverage,
            unavailable_reason=None if durations else "no_complete_qualified_flame_cycles",
        ),
        _metric(
            period_id,
            "burner_cycle_p90_seconds",
            _percentile(durations, 0.9),
            "seconds",
            coverage_pct=base_coverage,
            unavailable_reason=None if durations else "no_complete_qualified_flame_cycles",
        ),
        _metric(
            period_id,
            "burner_longest_cycle_seconds",
            max(durations) if durations else None,
            "seconds",
            coverage_pct=base_coverage,
            unavailable_reason=None if durations else "no_complete_qualified_flame_cycles",
        ),
        _metric(
            period_id,
            "burner_runtime_request_ratio",
            flame_seconds / demand_seconds if enough_demand else None,
            "ratio",
            denominator=demand_seconds,
            denominator_unit="active_request_seconds",
            coverage_pct=base_coverage,
            unavailable_reason=None if enough_demand else "no_qualified_active_request",
        ),
        _metric(
            period_id,
            "active_request_without_flame_ratio",
            (demand_seconds - demand_flame_seconds) / demand_seconds if enough_demand else None,
            "ratio",
            denominator=demand_seconds,
            denominator_unit="active_request_seconds",
            coverage_pct=base_coverage,
            unavailable_reason=None if enough_demand else "no_qualified_active_request",
        ),
    ]
    modulation = _by_role(signals, "modulation")
    if modulation is None:
        unknowns.append("missing:modulation")
        metrics.extend(
            [
                _metric(
                    period_id, "flame_modulation_mean", None, "vendor_percent", unavailable_reason="missing_modulation"
                ),
                _metric(
                    period_id,
                    "flame_modulation_median",
                    None,
                    "vendor_percent",
                    unavailable_reason="missing_modulation",
                ),
            ]
        )
    else:
        # The profile only describes the 0-at-flame convention. Flame itself is
        # always derived from `fl`, never from modulation.
        modulation_stat = _stat_over_intervals(modulation, [(left, right) for left, right, _state in flame])
        modulation_reason: str | None = (
            None if modulation_stat.coverage_pct >= min_coverage_pct else "insufficient_modulation_coverage"
        )
        metrics.extend(
            [
                _metric(
                    period_id,
                    "flame_modulation_mean",
                    modulation_stat.mean if modulation_reason is None else None,
                    modulation.metadata.unit,
                    coverage_pct=modulation_stat.coverage_pct,
                    unavailable_reason=modulation_reason,
                ),
                _metric(
                    period_id,
                    "flame_modulation_median",
                    modulation_stat.median if modulation_reason is None else None,
                    modulation.metadata.unit,
                    coverage_pct=modulation_stat.coverage_pct,
                    unavailable_reason=modulation_reason,
                ),
            ]
        )
        if capability_profile == "unknown":
            unknowns.append("unknown:modulation_zero_semantics")
    # Evaluate flow only where a qualified heating request was actually observed.
    # The complement also excludes state gaps instead of filling them with thermal data.
    non_heating_list: list[ExclusionWindow] = []
    cursor = start
    for left, right, _state in demand:
        if cursor < left:
            non_heating_list.append(ExclusionWindow(cursor, left, "inactive", "request_not_observed"))
        cursor = right
    if cursor < end:
        non_heating_list.append(ExclusionWindow(cursor, end, "inactive", "request_not_observed"))
    non_heating = tuple(non_heating_list)
    flow_error = _derived_statistic(
        _by_role(signals, "flow_temperature"),
        _by_role(signals, "target_flow_temperature"),
        start,
        end,
        lambda a, b: a - b,
        (*exclusions, *non_heating),
    )
    if flow_error is not None:
        flow_error.coverage_pct = (
            min(100.0, flow_error.coverage_pct * period_seconds / demand_seconds) if demand_seconds > 0 else 0.0
        )
    if flow_error is None or flow_error.coverage_pct < min_coverage_pct or not enough_demand:
        metrics.extend(
            [
                _metric(period_id, name, None, "celsius", unavailable_reason="insufficient_flow_cs_coverage")
                for name in ("flow_vs_cs_typical_c", "flow_vs_cs_p90_c", "flow_vs_cs_max_positive_overshoot_c")
            ]
        )
    else:
        metrics.extend(
            [
                _metric(
                    period_id,
                    "flow_vs_cs_typical_c",
                    flow_error.median,
                    "celsius",
                    denominator=demand_seconds,
                    denominator_unit="qualified_heating_seconds",
                    coverage_pct=flow_error.coverage_pct,
                ),
                _metric(
                    period_id,
                    "flow_vs_cs_p90_c",
                    flow_error.p90,
                    "celsius",
                    denominator=demand_seconds,
                    denominator_unit="qualified_heating_seconds",
                    coverage_pct=flow_error.coverage_pct,
                ),
                _metric(
                    period_id,
                    "flow_vs_cs_max_positive_overshoot_c",
                    max(flow_error.maximum or 0, 0),
                    "celsius",
                    denominator=demand_seconds,
                    denominator_unit="qualified_heating_seconds",
                    coverage_pct=flow_error.coverage_pct,
                ),
            ]
        )
    return metrics, unknowns


def _stat_over_intervals(series: SignalSeries, intervals: list[tuple[datetime, datetime]]) -> EvidenceStatistic:
    if not intervals:
        return EvidenceStatistic(coverage_pct=0, sample_count=0, source=series.metadata.origin)
    # Split at raw sample times before aggregating, preserving time weights
    # rather than taking an unweighted median of per-cycle means.
    pieces: list[tuple[datetime, datetime]] = []
    for left, right in intervals:
        boundaries = sorted(
            {left, right, *(sample.timestamp for sample in series.samples if left < sample.timestamp < right)}
        )
        pieces.extend(zip(boundaries, boundaries[1:], strict=False))
    stats = [_statistic(series, left, right) for left, right in pieces]
    seconds = [(right - left).total_seconds() for left, right in pieces]
    observed = sum(duration * stat.coverage_pct / 100 for duration, stat in zip(seconds, stats, strict=True))
    weighted = sum(
        (stat.mean or 0) * duration * stat.coverage_pct / 100 for duration, stat in zip(seconds, stats, strict=True)
    )
    total = sum(seconds)
    values = [stat.mean for stat in stats if stat.mean is not None]
    value_weights = [
        duration * stat.coverage_pct / 100
        for duration, stat in zip(seconds, stats, strict=True)
        if stat.mean is not None
    ]
    return EvidenceStatistic(
        mean=round(weighted / observed, 4) if observed else None,
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
        median=_weighted_quantile(values, value_weights, 0.5),
        p90=_weighted_quantile(values, value_weights, 0.9),
        first=values[0] if values else None,
        last=values[-1] if values else None,
        change=(values[-1] - values[0]) if values else None,
        slope_per_hour=None,
        coverage_pct=round(observed / total * 100, 2) if total else 0,
        stale_seconds=None,
        sample_count=sum(stat.sample_count for stat in stats),
        source="derived",
    )


__all__ = [
    "ALGORITHM_VERSION",
    "EvidenceExclusion",
    "EvidenceMetric",
    "EvidencePacket",
    "EvidenceStatistic",
    "EvidenceWindow",
    "ExclusionWindow",
    "NumericSample",
    "SignalMetadata",
    "SignalSeries",
    "StateSample",
    "build_evidence",
]

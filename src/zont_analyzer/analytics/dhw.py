from __future__ import annotations

import ast
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import mean, median
from typing import Any, NamedTuple, TypeVar

from zont_analyzer.domain import DetectedEvent, MetricValue

ALGORITHM_VERSION = "dhw-v1"
_T = TypeVar("_T")


class BoilerPurpose(StrEnum):
    HEATING = "heating"
    DHW = "dhw"
    CONCURRENT_OR_AMBIGUOUS = "concurrent_or_ambiguous"
    IDLE = "idle"


class HeatingDemand(StrEnum):
    CONFIRMED = "confirmed"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BoilerStateSample:
    timestamp: datetime
    purpose: BoilerPurpose
    flame_on: bool
    ch_on: bool
    dhw_on: bool


class DhwAnalysis(NamedTuple):
    metrics: list[MetricValue]
    events: list[DetectedEvent]
    context: dict[str, Any]


@dataclass(frozen=True)
class _StateInterval:
    start: datetime
    end: datetime
    sample: BoilerStateSample


@dataclass(frozen=True)
class _Episode:
    start: datetime
    end: datetime
    intervals: tuple[_StateInterval, ...]


def parse_opentherm_flags(value: str | Collection[str]) -> frozenset[str]:
    """Parse the stored ZONT OpenTherm flag list without accepting arbitrary code."""

    raw: object = value
    if isinstance(value, str):
        try:
            raw = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return frozenset()
    if not isinstance(raw, Collection) or isinstance(raw, (str, bytes, dict)):
        return frozenset()
    return frozenset(str(flag).casefold() for flag in raw)


def classify_opentherm_state(flags: str | Collection[str]) -> BoilerPurpose:
    parsed = parse_opentherm_flags(flags)
    ch_on = "ch" in parsed
    dhw_on = "dhw" in parsed
    if ch_on and dhw_on:
        return BoilerPurpose.CONCURRENT_OR_AMBIGUOUS
    if ch_on:
        return BoilerPurpose.HEATING
    if dhw_on:
        return BoilerPurpose.DHW
    return BoilerPurpose.IDLE


def classify_boiler_states(
    samples: list[tuple[datetime, str | Collection[str]]],
) -> list[BoilerStateSample]:
    result: list[BoilerStateSample] = []
    for timestamp, encoded in sorted(samples, key=lambda item: item[0]):
        flags = parse_opentherm_flags(encoded)
        result.append(
            BoilerStateSample(
                timestamp=timestamp,
                purpose=classify_opentherm_state(flags),
                flame_on="fl" in flags,
                ch_on="ch" in flags,
                dhw_on="dhw" in flags,
            )
        )
    return result


def _maximum_gap(timestamps: list[datetime], *, floor_seconds: float = 300.0) -> float:
    gaps = [
        (right - left).total_seconds() for left, right in zip(timestamps, timestamps[1:], strict=False) if right > left
    ]
    return max(floor_seconds, median(gaps) * 3) if gaps else floor_seconds


def _state_intervals(
    samples: list[BoilerStateSample], period_start: datetime, period_end: datetime
) -> tuple[list[_StateInterval], float]:
    if not samples:
        return [], 0.0
    deduplicated = {sample.timestamp: sample for sample in samples}
    ordered = [deduplicated[timestamp] for timestamp in sorted(deduplicated)]
    maximum_gap = _maximum_gap([sample.timestamp for sample in ordered])
    intervals: list[_StateInterval] = []
    for current, following in zip(ordered, ordered[1:], strict=False):
        start = max(current.timestamp, period_start)
        end = min(following.timestamp, period_end)
        if start < end and (following.timestamp - current.timestamp).total_seconds() <= maximum_gap:
            intervals.append(_StateInterval(start=start, end=end, sample=current))
    return intervals, maximum_gap


def _dhw_episodes(intervals: list[_StateInterval]) -> list[_Episode]:
    result: list[_Episode] = []
    current: list[_StateInterval] = []
    for interval in intervals:
        active = interval.sample.dhw_on
        continuous = not current or current[-1].end == interval.start
        if active and continuous:
            current.append(interval)
            continue
        if current:
            result.append(_Episode(current[0].start, current[-1].end, tuple(current)))
            current = []
        if active:
            current = [interval]
    if current:
        result.append(_Episode(current[0].start, current[-1].end, tuple(current)))
    return result


def _value_at(samples: list[tuple[datetime, _T]], timestamp: datetime) -> _T | None:
    current: _T | None = None
    for sample_time, value in sorted(samples, key=lambda item: item[0]):
        if sample_time > timestamp:
            break
        current = value
    return current


def _values_between(
    samples: list[tuple[datetime, float]], start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    return [(timestamp, value) for timestamp, value in samples if start <= timestamp <= end]


def _bool_at(samples: list[tuple[datetime, float | bool]], timestamp: datetime) -> bool | None:
    value = _value_at(samples, timestamp)
    return bool(value) if value is not None else None


def _mode_at(samples: list[tuple[datetime, float]], timestamp: datetime) -> int | None:
    value = _value_at(samples, timestamp)
    return int(value) if value is not None else None


def _mode_enabled_at(
    mode_samples: list[tuple[datetime, float]],
    mode_catalog: dict[int, dict[str, Any]],
    timestamp: datetime,
) -> bool | None:
    mode_id = _mode_at(mode_samples, timestamp)
    mode = mode_catalog.get(mode_id) if mode_id is not None else None
    if mode is None or "heating_enabled" not in mode:
        return None
    return bool(mode["heating_enabled"])


def _recent_positive(
    samples: list[tuple[datetime, float | bool]], timestamp: datetime, lookback: timedelta
) -> bool | None:
    recent = [bool(value) for sample_time, value in samples if timestamp - lookback <= sample_time < timestamp]
    return any(recent) if recent else None


def _recent_ch(states: list[BoilerStateSample], timestamp: datetime, lookback: timedelta) -> bool | None:
    recent = [sample.ch_on for sample in states if timestamp - lookback <= sample.timestamp < timestamp]
    return any(recent) if recent else None


def classify_heating_demand(
    *,
    timestamp: datetime,
    boiler_states: list[BoilerStateSample],
    heating_enabled: bool | None,
    heating_status_samples: list[tuple[datetime, float]] | None = None,
    heating_worktime_samples: list[tuple[datetime, float]] | None = None,
    indoor_temperature_samples: list[tuple[datetime, float]] | None = None,
    heating_target_samples: list[tuple[datetime, float]] | None = None,
    comfort_band_c: float = 0.5,
    lookback_minutes: float = 15.0,
) -> tuple[HeatingDemand, dict[str, Any]]:
    """Conservatively combine availability, activity and thermal-demand evidence."""

    lookback = timedelta(minutes=lookback_minutes)
    ch_recent = _recent_ch(boiler_states, timestamp, lookback)
    worktime_recent = _recent_positive(heating_worktime_samples or [], timestamp, lookback)
    status_value = _value_at(heating_status_samples or [], timestamp)
    status_active = bool(status_value) if status_value is not None else None
    indoor_c = _value_at(indoor_temperature_samples or [], timestamp)
    target_c = _value_at(heating_target_samples or [], timestamp)
    deficit_c = target_c - indoor_c if indoor_c is not None and target_c is not None else None
    thermal_demand = deficit_c > comfort_band_c if deficit_c is not None else None
    operational_demand = ch_recent is True or worktime_recent is True

    if heating_enabled is False:
        demand = HeatingDemand.NONE
    elif heating_enabled is True and operational_demand:
        conflicting_room = thermal_demand is False and deficit_c is not None and deficit_c < -comfort_band_c
        demand = HeatingDemand.UNKNOWN if conflicting_room and status_active is False else HeatingDemand.CONFIRMED
    elif heating_enabled is True and thermal_demand is True and status_active is True:
        demand = HeatingDemand.CONFIRMED
    elif (
        heating_enabled is True
        and ch_recent is False
        and worktime_recent is not True
        and thermal_demand is False
        and status_active is False
    ):
        demand = HeatingDemand.NONE
    else:
        demand = HeatingDemand.UNKNOWN

    return demand, {
        "heating_enabled": heating_enabled,
        "status_active": status_active,
        "ch_recent": ch_recent,
        "worktime_recent": worktime_recent,
        "indoor_temperature_c": indoor_c,
        "heating_target_c": target_c,
        "temperature_deficit_c": round(deficit_c, 3) if deficit_c is not None else None,
        "classification": demand.value,
    }


def _metric(period_id: str, name: str, value: float, unit: str, **context: Any) -> MetricValue:
    return MetricValue(
        id=f"metric:{period_id}:{name}:{ALGORITHM_VERSION}",
        name=name,
        value=round(value, 3),
        unit=unit,
        algorithm_version=ALGORITHM_VERSION,
        context=context,
    )


def _temperature_target_metrics(
    *,
    temperatures: list[tuple[datetime, float]],
    targets: list[tuple[datetime, float]],
    mode_samples: list[tuple[datetime, float]],
    mode_catalog: dict[int, dict[str, Any]],
    period_start: datetime,
    period_end: datetime,
    hysteresis_c: float,
) -> tuple[float, float, float]:
    """Return evaluated seconds, below-target seconds and below-target degree-hours."""

    ordered = sorted({timestamp: value for timestamp, value in temperatures}.items())
    if len(ordered) < 2:
        return 0.0, 0.0, 0.0
    maximum_gap = _maximum_gap([timestamp for timestamp, _ in ordered])
    evaluated = below = degree_hours = 0.0
    for (start, value), (end, _next) in zip(ordered, ordered[1:], strict=False):
        if start < period_start or end > period_end or (end - start).total_seconds() > maximum_gap:
            continue
        target = _value_at(targets, start)
        if target is None:
            continue
        explicitly_enabled = _mode_enabled_at(mode_samples, mode_catalog, start)
        if explicitly_enabled is False:
            continue
        seconds = (end - start).total_seconds()
        evaluated += seconds
        deficit = target - value
        if deficit > hysteresis_c:
            below += seconds
            degree_hours += deficit * seconds / 3600
    return evaluated, below, degree_hours


def _first_return(
    *,
    episode_end: datetime,
    states: list[BoilerStateSample],
    worktime: list[tuple[datetime, float]],
    window_end: datetime,
) -> tuple[datetime | None, str | None, bool]:
    candidates: list[tuple[datetime, str, bool]] = []
    for sample in states:
        if episode_end <= sample.timestamp <= window_end and sample.purpose == BoilerPurpose.HEATING:
            candidates.append((sample.timestamp, "ch", not sample.flame_on))
    for timestamp, value in worktime:
        if episode_end <= timestamp <= window_end and value > 0:
            state = _value_at([(sample.timestamp, sample) for sample in states], timestamp)
            candidates.append((timestamp, "worktime", state is not None and not state.flame_on))
    if not candidates:
        return None, None, False
    timestamp, source, without_flame = min(candidates, key=lambda item: item[0])
    return timestamp, source, without_flame


def _hot_tail_seconds(
    samples: list[tuple[datetime, float]], start: datetime, end: datetime, threshold_c: float
) -> float | None:
    initial = _value_at(samples, start)
    if initial is None or initial < threshold_c:
        return 0.0 if initial is not None else None
    for timestamp, value in sorted(samples):
        if start < timestamp <= end and value < threshold_c:
            return (timestamp - start).total_seconds()
    return None


def _observation_window_is_continuous(
    states: list[BoilerStateSample], start: datetime, end: datetime, maximum_gap_seconds: float
) -> bool:
    if end <= start:
        return True
    relevant = [sample.timestamp for sample in states if start <= sample.timestamp <= end]
    if not relevant or relevant[0] > start:
        return False
    return (
        all(
            (right - left).total_seconds() <= maximum_gap_seconds
            for left, right in zip(relevant, relevant[1:], strict=False)
        )
        and (end - relevant[-1]).total_seconds() <= maximum_gap_seconds
    )


def _pre_episode_drop_rate(
    samples: list[tuple[datetime, float]], timestamp: datetime, *, lookback_hours: float = 2.0
) -> float | None:
    recent = sorted(
        (sample_time, value)
        for sample_time, value in samples
        if timestamp - timedelta(hours=lookback_hours) <= sample_time <= timestamp
    )
    if len(recent) < 2:
        return None
    (before_time, before), (after_time, after) = recent[-2:]
    hours = (after_time - before_time).total_seconds() / 3600
    return (before - after) / hours if hours > 0 else None


def analyze_dhw_interactions(
    *,
    period_id: str,
    period_start: datetime,
    period_end: datetime,
    boiler_state_samples: list[tuple[datetime, str | Collection[str]]],
    dhw_temperature_samples: list[tuple[datetime, float]],
    dhw_target_samples: list[tuple[datetime, float]],
    dhw_mode_samples: list[tuple[datetime, float]] | None = None,
    dhw_status_samples: list[tuple[datetime, float]] | None = None,
    dhw_worktime_samples: list[tuple[datetime, float]] | None = None,
    dhw_mode_catalog: dict[int, dict[str, Any]] | None = None,
    dhw_circuit_config: dict[str, Any] | None = None,
    heating_mode_samples: list[tuple[datetime, float]] | None = None,
    heating_status_samples: list[tuple[datetime, float]] | None = None,
    heating_worktime_samples: list[tuple[datetime, float]] | None = None,
    heating_mode_catalog: dict[int, dict[str, Any]] | None = None,
    heating_available_samples: list[tuple[datetime, float | bool]] | None = None,
    indoor_temperature_samples: list[tuple[datetime, float]] | None = None,
    heating_target_samples: list[tuple[datetime, float]] | None = None,
    flow_temperature_samples: list[tuple[datetime, float]] | None = None,
    quality_score: float = 1.0,
    minimum_quality_score: float = 0.7,
    comfort_band_c: float = 0.5,
    return_window_minutes: float = 45.0,
    long_return_minutes: float = 15.0,
    hot_flow_threshold_c: float = 45.0,
    long_hot_tail_minutes: float = 15.0,
) -> DhwAnalysis:
    """Build deterministic DHW episodes and their observed interaction with space heating."""

    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    mode_samples = dhw_mode_samples or []
    mode_catalog = dhw_mode_catalog or {}
    circuit_config = dhw_circuit_config or {}
    heating_modes = heating_mode_samples or []
    heating_catalog = heating_mode_catalog or {}
    heating_worktime = heating_worktime_samples or []
    flow_temperatures = flow_temperature_samples or []
    states = classify_boiler_states(boiler_state_samples)
    intervals, maximum_state_gap = _state_intervals(states, period_start, period_end)
    episodes = _dhw_episodes(intervals)
    observed_state_seconds = sum((item.end - item.start).total_seconds() for item in intervals)
    dhw_seconds = sum(
        (item.end - item.start).total_seconds() for item in intervals if item.sample.purpose == BoilerPurpose.DHW
    )
    concurrent_seconds = sum(
        (item.end - item.start).total_seconds()
        for item in intervals
        if item.sample.purpose == BoilerPurpose.CONCURRENT_OR_AMBIGUOUS
    )
    hysteresis_c = float(circuit_config.get("hysteresis_c", circuit_config.get("hysteresis", comfort_band_c)))
    evaluated_seconds, below_seconds, below_degree_hours = _temperature_target_metrics(
        temperatures=dhw_temperature_samples,
        targets=dhw_target_samples,
        mode_samples=mode_samples,
        mode_catalog=mode_catalog,
        period_start=period_start,
        period_end=period_end,
        hysteresis_c=hysteresis_c,
    )
    data_reliable = quality_score >= minimum_quality_score
    return_window = timedelta(minutes=return_window_minutes)
    recovery_minutes: list[float] = []
    overshoots: list[float] = []
    confirmed_pause_minutes: list[float] = []
    return_delays: list[float] = []
    confirmed_long_returns = residual_returns = hot_tails = 0
    events: list[DetectedEvent] = []

    for episode in episodes:
        selected_mode_id = _mode_at(mode_samples, episode.start)
        selected_mode = mode_catalog.get(selected_mode_id) if selected_mode_id is not None else None
        dhw_enabled = _mode_enabled_at(mode_samples, mode_catalog, episode.start)
        heating_enabled = _bool_at(heating_available_samples or [], episode.start)
        if heating_enabled is None:
            heating_enabled = _mode_enabled_at(heating_modes, heating_catalog, episode.start)
        demand, demand_evidence = classify_heating_demand(
            timestamp=episode.start,
            boiler_states=states,
            heating_enabled=heating_enabled,
            heating_status_samples=heating_status_samples,
            heating_worktime_samples=heating_worktime,
            indoor_temperature_samples=indoor_temperature_samples,
            heating_target_samples=heating_target_samples,
            comfort_band_c=comfort_band_c,
        )
        target_c = _value_at(dhw_target_samples, episode.start)
        start_temperature_c = _value_at(dhw_temperature_samples, episode.start)
        status = _value_at(dhw_status_samples or [], episode.start)
        worktime = _value_at(dhw_worktime_samples or [], episode.start)
        followup_end = min(period_end, episode.end + return_window)
        temperatures = _values_between(dhw_temperature_samples, episode.start, followup_end)
        achieved_at = (
            next(
                (
                    timestamp
                    for timestamp, value in temperatures
                    if target_c is not None and value >= target_c - hysteresis_c
                ),
                None,
            )
            if target_c is not None
            else None
        )
        recovery = (achieved_at - episode.start).total_seconds() / 60 if achieved_at else None
        if recovery is not None:
            recovery_minutes.append(recovery)
        peak_temperature_c = max((value for _, value in temperatures), default=None)
        overshoot_c = (
            max(0.0, peak_temperature_c - target_c) if target_c is not None and peak_temperature_c is not None else None
        )
        if overshoot_c is not None:
            overshoots.append(overshoot_c)
        returned_at, return_source, returned_without_flame = _first_return(
            episode_end=episode.end,
            states=states,
            worktime=heating_worktime,
            window_end=followup_end,
        )
        return_delay = (returned_at - episode.end).total_seconds() / 60 if returned_at else None
        if return_delay is not None:
            return_delays.append(return_delay)
        episode_data_reliable = data_reliable and _observation_window_is_continuous(
            states,
            episode.start,
            returned_at or episode.end,
            maximum_state_gap,
        )
        if returned_without_flame and episode_data_reliable:
            residual_returns += 1
        pause_minutes = (returned_at - episode.start).total_seconds() / 60 if returned_at else None
        if episode_data_reliable and demand == HeatingDemand.CONFIRMED and pause_minutes is not None:
            confirmed_pause_minutes.append(pause_minutes)
        hot_tail = _hot_tail_seconds(flow_temperatures, episode.end, followup_end, hot_flow_threshold_c)
        hot_tail_minutes = hot_tail / 60 if hot_tail is not None else None
        long_return = return_delay is not None and return_delay > long_return_minutes
        long_hot_tail = hot_tail_minutes is not None and hot_tail_minutes > long_hot_tail_minutes
        if episode_data_reliable and demand == HeatingDemand.CONFIRMED and long_return:
            confirmed_long_returns += 1
        if episode_data_reliable and long_hot_tail:
            hot_tails += 1
        has_ambiguous = any(
            interval.sample.purpose == BoilerPurpose.CONCURRENT_OR_AMBIGUOUS for interval in episode.intervals
        )
        drop_rate = _pre_episode_drop_rate(dhw_temperature_samples, episode.start)
        drop_pattern = (
            "rapid_temperature_decline"
            if drop_rate is not None and drop_rate >= 6.0
            else "gradual_cooling"
            if drop_rate is not None and drop_rate > 0
            else "stable_or_rising"
            if drop_rate is not None
            else "unknown"
        )
        return_indoor_c = _value_at(indoor_temperature_samples or [], returned_at) if returned_at else None
        return_heating_target_c = _value_at(heating_target_samples or [], returned_at) if returned_at else None
        return_deficit_c = (
            return_heating_target_c - return_indoor_c
            if return_heating_target_c is not None and return_indoor_c is not None
            else None
        )
        facts: dict[str, Any] = {
            "selected_system_mode_id": selected_mode_id,
            "selected_system_mode_name": selected_mode.get("name") if selected_mode else None,
            "dhw_enabled_by_selected_mode": dhw_enabled,
            "dhw_temperature_start_c": start_temperature_c,
            "dhw_temperature_end_c": _value_at(dhw_temperature_samples, episode.end),
            "dhw_target_c": target_c,
            "dhw_peak_temperature_c": peak_temperature_c,
            "dhw_status": status,
            "dhw_worktime": worktime,
            "duration_minutes": round((episode.end - episode.start).total_seconds() / 60, 3),
            "target_reached_at": achieved_at.isoformat() if achieved_at else None,
            "recovery_minutes": round(recovery, 3) if recovery is not None else None,
            "overshoot_c": round(overshoot_c, 3) if overshoot_c is not None else None,
            "contains_concurrent_or_ambiguous_flags": has_ambiguous,
            "heating_return_at": returned_at.isoformat() if returned_at else None,
            "heating_return_source": return_source,
            "heating_return_delay_minutes": round(return_delay, 3) if return_delay is not None else None,
            "heating_return_without_flame": returned_without_flame,
            "hot_flow_tail_minutes": round(hot_tail_minutes, 3) if hot_tail_minutes is not None else None,
            "pre_episode_temperature_drop_c_per_hour": round(drop_rate, 3) if drop_rate is not None else None,
            "indoor_temperature_at_heating_return_c": return_indoor_c,
            "heating_target_at_return_c": return_heating_target_c,
            "temperature_deficit_at_return_c": round(return_deficit_c, 3) if return_deficit_c is not None else None,
            "data_quality_score": quality_score,
            "episode_observation_continuous": episode_data_reliable,
        }
        inference = {
            "heating_demand": demand.value if data_reliable else HeatingDemand.UNKNOWN.value,
            "heating_demand_evidence": demand_evidence,
            "confirmed_heating_pause_minutes": (
                round(pause_minutes, 3)
                if episode_data_reliable and demand == HeatingDemand.CONFIRMED and pause_minutes is not None
                else None
            ),
            "temperature_drop_pattern": drop_pattern,
            "dhw_mode_consistency": "conflicting" if dhw_enabled is False else "consistent_or_unknown",
            "residual_heat_return": episode_data_reliable and returned_without_flame,
            "long_heating_return": episode_data_reliable and demand == HeatingDemand.CONFIRMED and long_return,
            "long_hot_flow_tail": episode_data_reliable and long_hot_tail,
        }
        hypothesis = (
            "Горячий хвост температуры подачи может быть связан с задержкой возврата отопления; "
            "причинность через клапан или насос не наблюдается напрямую."
            if episode_data_reliable and demand == HeatingDemand.CONFIRMED and long_return and long_hot_tail
            else "Наблюдалось быстрое снижение температуры ГВС; водоразбор возможен, но не доказан."
            if drop_pattern == "rapid_temperature_decline"
            else (
                "Нет прямого сигнала клапана ГВС, насоса или расхода воды; "
                "гидравлическая причина остаётся неизвестной."
            )
        )
        event_id = f"event:{period_id}:dhw_episode:{int(episode.start.timestamp())}:{ALGORITHM_VERSION}"
        events.append(
            DetectedEvent(
                id=event_id,
                kind="dhw_reheat_episode",
                started_at=episode.start,
                ended_at=episode.end,
                severity="info",
                details={"facts": facts, "inference": inference, "hypothesis": hypothesis},
                algorithm_version=ALGORITHM_VERSION,
            )
        )
        if has_ambiguous:
            events.append(
                DetectedEvent(
                    id=(
                        f"event:{period_id}:dhw_concurrent_or_ambiguous:"
                        f"{int(episode.start.timestamp())}:{ALGORITHM_VERSION}"
                    ),
                    kind="dhw_concurrent_or_ambiguous",
                    started_at=episode.start,
                    ended_at=episode.end,
                    severity="info",
                    details={
                        "facts": {"episode_id": event_id, "opentherm_ch_and_dhw_were_both_set": True},
                        "inference": {"heat_purpose": BoilerPurpose.CONCURRENT_OR_AMBIGUOUS.value},
                        "hypothesis": "Телеметрия не доказывает, в какой гидравлический контур поступало тепло.",
                    },
                    algorithm_version=ALGORITHM_VERSION,
                )
            )
        if episode_data_reliable and demand == HeatingDemand.CONFIRMED and long_return:
            events.append(
                DetectedEvent(
                    id=f"event:{period_id}:dhw_long_heating_return:{int(episode.start.timestamp())}:{ALGORITHM_VERSION}",
                    kind="dhw_long_heating_return",
                    started_at=episode.end,
                    ended_at=returned_at,
                    severity="warning",
                    details={
                        "facts": {"episode_id": event_id, "return_delay_minutes": round(return_delay or 0, 3)},
                        "inference": {"heating_demand": HeatingDemand.CONFIRMED.value},
                        "hypothesis": hypothesis,
                    },
                    algorithm_version=ALGORITHM_VERSION,
                )
            )

    metrics: list[MetricValue] = [_metric(period_id, "dhw_episode_count", float(len(episodes)), "count")]
    if observed_state_seconds:
        metrics.extend(
            [
                _metric(
                    period_id,
                    "dhw_priority_time_pct",
                    dhw_seconds / observed_state_seconds * 100,
                    "%",
                    excludes_concurrent_or_ambiguous=True,
                ),
                _metric(
                    period_id,
                    "dhw_concurrent_or_ambiguous_time_pct",
                    concurrent_seconds / observed_state_seconds * 100,
                    "%",
                ),
            ]
        )
    if evaluated_seconds:
        metrics.extend(
            [
                _metric(
                    period_id,
                    "dhw_target_evaluation_time_pct",
                    evaluated_seconds / max((period_end - period_start).total_seconds(), 1) * 100,
                    "%",
                ),
                _metric(period_id, "dhw_time_below_target_pct", below_seconds / evaluated_seconds * 100, "%"),
                _metric(period_id, "dhw_degree_hours_below_target", below_degree_hours, "°C·h"),
            ]
        )
    if recovery_minutes:
        metrics.append(_metric(period_id, "dhw_mean_recovery_minutes", mean(recovery_minutes), "min"))
    if overshoots:
        metrics.append(_metric(period_id, "dhw_mean_overshoot_c", mean(overshoots), "°C"))
    metrics.extend(
        [
            _metric(period_id, "dhw_confirmed_heating_pause_count", float(len(confirmed_pause_minutes)), "count"),
            _metric(period_id, "dhw_long_heating_return_count", float(confirmed_long_returns), "count"),
            _metric(period_id, "dhw_residual_heat_return_count", float(residual_returns), "count"),
            _metric(period_id, "dhw_long_hot_flow_tail_count", float(hot_tails), "count"),
        ]
    )
    if confirmed_pause_minutes and data_reliable:
        metrics.append(
            _metric(period_id, "dhw_mean_confirmed_heating_pause_minutes", mean(confirmed_pause_minutes), "min")
        )
    if return_delays:
        metrics.append(_metric(period_id, "dhw_mean_heating_return_delay_minutes", mean(return_delays), "min"))

    current_mode_id = _mode_at(mode_samples, period_end)
    current_mode = mode_catalog.get(current_mode_id) if current_mode_id is not None else None
    context: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "quality_sufficient_for_alerts": data_reliable,
        "state_observed_time_pct": round(observed_state_seconds / (period_end - period_start).total_seconds() * 100, 3),
        "maximum_accepted_state_gap_seconds": maximum_state_gap,
        "dhw_circuit": {
            **circuit_config,
            "current_mode_id": current_mode_id,
            "current_mode": current_mode,
            "current_target_c": _value_at(dhw_target_samples, period_end),
            "current_status": _value_at(dhw_status_samples or [], period_end),
            "current_worktime": _value_at(dhw_worktime_samples or [], period_end),
            "historical_mode_observed": bool(mode_samples),
            "historical_target_observed": bool(dhw_target_samples),
        },
        "episode_event_ids": [event.id for event in events if event.kind == "dhw_reheat_episode"],
    }
    return DhwAnalysis(metrics=metrics, events=sorted(events, key=lambda item: item.started_at), context=context)


__all__ = [
    "ALGORITHM_VERSION",
    "BoilerPurpose",
    "BoilerStateSample",
    "DhwAnalysis",
    "HeatingDemand",
    "analyze_dhw_interactions",
    "classify_boiler_states",
    "classify_heating_demand",
    "classify_opentherm_state",
    "parse_opentherm_flags",
]

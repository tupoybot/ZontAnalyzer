"""Lossless interval facts for heating regulation, independent of an AI report.

The census retains runs, not selected windows or daily averages.  Measurements
are interpolated only between valid adjacent observations; a setpoint is held
until an explicit unknown.  Request activity and confirmed flame are distinct.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Literal

from .evidence import ExclusionWindow, SignalSeries, StateSample

ALGORITHM_VERSION = "regulation-v1"


@dataclass(frozen=True)
class RegulationInputs:
    circuit_id: str
    signals: dict[str, SignalSeries]
    device_id: str = "unknown"
    request_semantics: Literal["state", "preceding_minute"] = "state"
    states: tuple[StateSample, ...] = ()
    state_unknown_at: tuple[datetime, ...] = ()
    exclusions: tuple[ExclusionWindow, ...] = ()
    humidity_room_identity: str | None = None
    settings_version: str = "unknown"
    settings_versions: tuple[tuple[datetime, str], ...] = ()
    source_unknowns: tuple[str, ...] = ()
    reliability_context: dict[str, Any] = field(default_factory=dict)
    negative_input_guards: tuple[tuple[str, int], ...] = ()
    mode_kind: Literal["unknown", "warmup", "maintenance", "inactive"] = "unknown"
    max_gap_seconds: float = 900.0
    recovery_band_c: float = 0.2
    recovery_slope_c_per_hour: float = 0.3
    cooling_drop_c: float = 0.4
    cooling_rate_c_per_hour: float = 1.0


@dataclass(frozen=True)
class _Span:
    start: datetime
    end: datetime
    left: float
    right: float

    def value(self, timestamp: datetime) -> float:
        ratio = (timestamp - self.start).total_seconds() / (self.end - self.start).total_seconds()
        return self.left + (self.right - self.left) * ratio


@dataclass
class _Lookup:
    spans: list[_Span]
    starts: list[datetime] = field(init=False)

    def __post_init__(self) -> None:
        self.starts = [span.start for span in self.spans]

    def at(self, left: datetime, right: datetime) -> tuple[float, float] | None:
        index = bisect_right(self.starts, left) - 1
        if index >= 0:
            span = self.spans[index]
            if span.start <= left < right <= span.end:
                return span.value(left), span.value(right)
        return None


def _spans(series: SignalSeries, end: datetime, maximum_gap: float, preceding: bool = False,
           categorical: bool = False) -> list[_Span]:
    observed: dict[datetime, float | None] = {
        sample.timestamp: sample.value if math.isfinite(sample.value) else None for sample in series.samples
    }
    observed.update(dict.fromkeys(series.unknown_at))
    points = sorted(observed.items())
    result = []
    for index, (timestamp, value) in enumerate(points):
        if value is None:
            continue
        if preceding:
            # Source worktime is seconds active in the PRECEDING minute.  A
            # fractional minute proves activity, not its precise edge or duty.
            if 0 <= value <= 60:
                result.append(_Span(timestamp - timedelta(minutes=1), timestamp, value / 60, value / 60))
            continue
        following = points[index + 1] if index + 1 < len(points) else None
        if series.metadata.value_kind == "setpoint":
            stop = following[0] if following else end
            if stop > timestamp:
                result.append(_Span(timestamp, stop, value, value))
        elif following and following[1] is not None:
            gap = (following[0] - timestamp).total_seconds()
            if 0 < gap <= maximum_gap:
                # Request/mode are categorical, even when transported as numeric measurements.
                result.append(_Span(timestamp, following[0], value, value if categorical else following[1]))
    return result


def _positive_area(left: float, right: float, seconds: float) -> float:
    if left >= 0 and right >= 0:
        return (left + right) * seconds / 2
    if left <= 0 and right <= 0:
        return 0.0
    positive = max(left, right)
    return positive * seconds * positive / abs(right - left) / 2


def _stat(left: float, right: float, seconds: float) -> dict[str, float]:
    return {"seconds": seconds, "integral": (left + right) * seconds / 2,
            "positive_integral": _positive_area(left, right, seconds),
            "negative_integral": _positive_area(-left, -right, seconds),
            "minimum": min(left, right), "maximum": max(left, right),
            "variation": abs(right - left), "max_rate_per_hour": abs(right - left) * 3600 / seconds,
            "first": left, "last": right}


def _merge_stat(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("seconds", "integral", "positive_integral", "negative_integral", "variation"):
        target[key] += source[key]
    target["minimum"] = min(target["minimum"], source["minimum"])
    target["maximum"] = max(target["maximum"], source["maximum"])
    target["max_rate_per_hour"] = max(target["max_rate_per_hour"], source["max_rate_per_hour"])
    target["last"] = source["last"]


def _iso(value: datetime) -> str:
    return value.isoformat()


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _disturbances(inputs: RegulationInputs) -> list[dict[str, Any]]:
    room = inputs.signals.get("room")
    if room is None:
        return []
    unknown = set(room.unknown_at)
    samples = sorted(room.samples, key=lambda sample: sample.timestamp)
    humidity = inputs.signals.get("humidity")
    same_room = bool(humidity and inputs.humidity_room_identity == room.metadata.identity)
    humidity_points = [sample for sample in humidity.samples if 0 <= sample.value <= 100] if humidity else []
    humidity_unknown = (set(humidity.unknown_at)
                        | {sample.timestamp for sample in humidity.samples
                           if not math.isfinite(sample.value) or not 0 <= sample.value <= 100}) if humidity else set()
    result = []
    # Three consecutive valid observations prevent two remote points from
    # inventing a ventilation event. No probability is assigned.
    for index, (first, middle, last) in enumerate(zip(samples, samples[1:], samples[2:], strict=False)):
        duration = (last.timestamp - first.timestamp).total_seconds()
        if not 0 < duration <= 1800 or max((middle.timestamp - first.timestamp).total_seconds(),
                                         (last.timestamp - middle.timestamp).total_seconds()) > inputs.max_gap_seconds:
            continue
        if any(first.timestamp <= timestamp <= last.timestamp for timestamp in unknown):
            continue
        fall = first.value - last.value
        if (fall < inputs.cooling_drop_c or fall * 3600 / duration < inputs.cooling_rate_c_per_hour
                or not first.value > middle.value > last.value):
            continue
        preceding = samples[max(0, index - 2):index + 1]
        prior_stable = len(preceding) == 3 and all(
            0 < (right.timestamp - left.timestamp).total_seconds() <= inputs.max_gap_seconds
            and abs(right.value - left.value) * 3600 / (right.timestamp - left.timestamp).total_seconds()
            <= inputs.recovery_slope_c_per_hour
            for left, right in zip(preceding, preceding[1:], strict=False)
        )
        corroborated = False
        if same_room:
            near_first = min(humidity_points,
                             key=lambda point: abs((point.timestamp - first.timestamp).total_seconds()), default=None)
            near_last = min(humidity_points, key=lambda point: abs((point.timestamp - last.timestamp).total_seconds()),
                            default=None)
            near_middle = min(humidity_points,
                              key=lambda point: abs((point.timestamp - middle.timestamp).total_seconds()), default=None)
            if near_first and near_middle and near_last:
                corroborated = (
                    abs((near_first.timestamp - first.timestamp).total_seconds()) <= 300
                    and abs((near_last.timestamp - last.timestamp).total_seconds()) <= 300
                    and near_first.timestamp < near_last.timestamp
                    and near_first.timestamp < near_middle.timestamp < near_last.timestamp
                    and abs((near_middle.timestamp - middle.timestamp).total_seconds()) <= 300
                    and max((near_middle.timestamp - near_first.timestamp).total_seconds(),
                            (near_last.timestamp - near_middle.timestamp).total_seconds()) <= inputs.max_gap_seconds
                    and near_first.value - near_last.value >= 3
                    and not any(near_first.timestamp <= timestamp <= near_last.timestamp
                                for timestamp in humidity_unknown)
                )
        result.append({"start": first.timestamp, "impact_end": last.timestamp,
                       "reason": "ventilation_corroborated" if corroborated else "abrupt_room_cooling",
                       "certainty": "corroborated_same_room_humidity" if corroborated else "cause_uncertain",
                       "baseline_room": first.value, "prior_stable": prior_stable})
    # Consecutive cooling triples form one impact, preserving the first baseline.
    merged: list[dict[str, Any]] = []
    for item in result:
        if merged and item["start"] <= merged[-1]["impact_end"]:
            merged[-1]["impact_end"] = item["impact_end"]
            if item["reason"] == "ventilation_corroborated":
                merged[-1]["reason"] = item["reason"]
                merged[-1]["certainty"] = item["certainty"]
        else:
            merged.append(item)
    return merged


def _empty_run(left: datetime, right: datetime, *, request: bool | None, reasons: list[str],
               mode: str, target: float | None, outdoor: str) -> dict[str, Any]:
    return {"start": _iso(left), "end": _iso(right), "request": request, "reasons": reasons,
            "mode": mode, "target": target, "weather": outdoor, "stats": {},
            "control_mode": None,
            "settings_version": "unknown",
            "request_semantics": "state",
            "eligible_seconds": 0.0, "seconds": 0.0, "burner_starts": 0,
            "request_active_seconds": 0.0, "request_observed_seconds": 0.0,
            "burner_observed_seconds": 0.0, "limit_seconds": 0.0, "limit_observed_seconds": 0.0,
            "water_limit_seconds": 0.0, "water_limit_observed_seconds": 0.0,
            "reached_at": None, "reaction_at": None, "peak_at": None}


def _run_key(run: dict[str, Any]) -> tuple[Any, ...]:
    return (run["request"], tuple(run["reasons"]), run["mode"], run["target"], run["weather"],
            run["control_mode"], run["settings_version"], run["request_semantics"])


def _append_run(runs: list[dict[str, Any]], run: dict[str, Any]) -> None:
    if not runs or runs[-1]["end"] != run["start"] or _run_key(runs[-1]) != _run_key(run):
        runs.append(run)
        return
    previous = runs[-1]
    previous["end"] = run["end"]
    for key in ("seconds", "eligible_seconds", "burner_starts", "burner_observed_seconds",
                "request_active_seconds", "request_observed_seconds",
                "limit_seconds", "limit_observed_seconds", "water_limit_seconds", "water_limit_observed_seconds"):
        previous[key] += run[key]
    if previous.get("reaction_baseline") != run.get("reaction_baseline"):
        previous["reaction_continuity"] = False
        previous["reaction_at"] = None
    for key, stat in run["stats"].items():
        if key in previous["stats"]:
            old = previous["stats"][key]
            # A setpoint jump contributes to command variation; interpolated
            # observations meet continuously and add zero at a shared boundary.
            if key in {"command", "actual", "room"}:
                old["variation"] += abs(stat["first"] - old["last"])
            _merge_stat(old, stat)
        else:
            previous["stats"][key] = stat.copy()
    previous["reached_at"] = previous["reached_at"] or run["reached_at"]
    if previous.get("reaction_continuity", True):
        previous["reaction_at"] = previous["reaction_at"] or run["reaction_at"]
    if run["peak_at"] and (not previous["peak_at"]
                           or run["stats"].get("room", {}).get("maximum", -math.inf)
                           > previous.get("peak_value", -math.inf)):
        previous["peak_at"] = run["peak_at"]
        previous["peak_value"] = run["stats"]["room"]["maximum"]


def build_regulation_facts(*, start: datetime, end: datetime, inputs: RegulationInputs,
                           previous_boundary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Calculate one bounded day/circuit. Never truncate census runs.

    ``previous_boundary`` carries unresolved recovery across adjacent days. A
    caller must invalidate subsequent boundaries until this state converges.
    Generic noise exclusions are deliberately ignored: bad regulation is data.
    """
    if end <= start or inputs.max_gap_seconds <= 0:
        raise ValueError("invalid regulation interval")
    lookups = {key: _Lookup(_spans(series, end, inputs.max_gap_seconds,
                                  key == "request" and inputs.request_semantics == "preceding_minute",
                                  key in {"request", "mode"}))
               for key, series in inputs.signals.items()}
    request_totals: dict[str, Any] = {}
    if inputs.request_semantics == "preceding_minute" and "request" in inputs.signals:
        samples = sorted((sample for sample in inputs.signals["request"].samples
                          if 0 <= sample.value <= 60 and start < sample.timestamp <= end),
                         key=lambda sample: sample.timestamp)
        overlap = any((right.timestamp - left.timestamp).total_seconds() < 60
                      for left, right in zip(samples, samples[1:], strict=False))
        cropped = sum(0 < sample.value < 60 and sample.timestamp - timedelta(minutes=1) < start
                      for sample in samples)
        request_totals = {"minute_observation_count": len(samples),
                          "active_seconds_in_observation_windows_sum": sum(sample.value for sample in samples),
                          "overlapping_windows": overlap, "cropped_fractional_windows": cropped,
                          "sum_is_chronological_duration": not overlap and not cropped}
        if overlap:
            lookups["request"] = _Lookup([])
        else:
            # A fractional minute crossing midnight does not reveal which
            # seconds fell on either day. Keep the aggregate observation, but
            # only complete windows support chronological activity duration.
            lookups["request"] = _Lookup([span for span in lookups["request"].spans
                                          if not (0 < span.left < 1 and (span.start < start or span.end > end))])
    boundaries = {start, end}
    for lookup in lookups.values():
        for span in lookup.spans:
            if start < span.start < end:
                boundaries.add(span.start)
            if start < span.end < end:
                boundaries.add(span.end)
    boundaries.update(timestamp for timestamp, _version in inputs.settings_versions if start < timestamp < end)
    external = [item for item in inputs.exclusions if item.reason != "noise"]
    ordered_states = sorted(inputs.states, key=lambda sample: sample.timestamp)
    external.extend(ExclusionWindow(left.timestamp, right.timestamp, "dhw", "confirmed_boiler_state")
                    for left, right in zip(ordered_states, ordered_states[1:], strict=False)
                    if "dhw" in left.flags
                    and left.identity == right.identity
                    and (right.timestamp - left.timestamp).total_seconds() <= inputs.max_gap_seconds
                    and not any(left.timestamp <= timestamp < right.timestamp for timestamp in inputs.state_unknown_at))
    disturbances = _disturbances(inputs)
    for item in external:
        for timestamp in (item.start, item.end):
            if start < timestamp < end:
                boundaries.add(timestamp)
    for disturbance in disturbances:
        for timestamp in (disturbance["start"], disturbance["impact_end"]):
            if start < timestamp < end:
                boundaries.add(timestamp)
    states = sorted(inputs.states, key=lambda sample: sample.timestamp)
    for sample in states:
        if start < sample.timestamp < end:
            boundaries.add(sample.timestamp)
    boundaries.update(timestamp for timestamp in inputs.state_unknown_at if start < timestamp < end)
    state_times = [sample.timestamp for sample in states]
    runs: list[dict[str, Any]] = []
    recovery = None
    if previous_boundary and previous_boundary.get("end") == _iso(start):
        recovery = previous_boundary.get("recovery")
        recovery = dict(recovery) if recovery else None
    excluded: dict[str, float] = defaultdict(float)
    event_windows: list[dict[str, Any]] = []
    previous_values: dict[str, tuple[float, float]] = {}
    previous_right: datetime | None = None
    mode_kind = inputs.mode_kind
    request_anchor = None
    if previous_boundary and previous_boundary.get("end") == _iso(start):
        previous_values = {key: tuple(value) for key, value in previous_boundary.get("last_values", {}).items()}
        previous_right = start
        mode_kind = previous_boundary.get("mode_kind", mode_kind)
        request_anchor = previous_boundary.get("request_anchor")
        request_anchor = deepcopy(request_anchor) if request_anchor else None
    stable_intervals = int(recovery.get("stable_intervals", 0)) if recovery else 0
    previous_flame: bool | None = None
    previous_state_end: datetime | None = None
    if previous_boundary and previous_boundary.get("end") == _iso(start):
        previous_flame = previous_boundary.get("last_flame")
        previous_state_end = start if previous_flame is not None else None
    ordered = sorted(boundaries)
    command_times = sorted(sample.timestamp for sample in inputs.signals["command"].samples) \
        if "command" in inputs.signals else []
    room_times = {sample.timestamp for sample in inputs.signals["room"].samples} if "room" in inputs.signals else set()
    settings_versions = sorted(inputs.settings_versions)
    settings_times = [timestamp for timestamp, _version in settings_versions]
    for left, right in zip(ordered, ordered[1:], strict=False):
        seconds = (right - left).total_seconds()
        values = {key: value for key, lookup in lookups.items() if (value := lookup.at(left, right)) is not None}
        reasons: set[str] = {item.reason for item in external if item.start < right and item.end > left}
        room, target, request = values.get("room"), values.get("target"), values.get("request")
        active = request[0] > 0 if request else None
        if request and inputs.request_semantics == "state" and request[0] not in {0, 1}:
            active = None
        if room is None:
            reasons.add("room_missing_or_gap")
        if target is None:
            reasons.add("target_unknown")
        if active is None:
            reasons.add("request_unknown")
        if inputs.mode_kind == "inactive":
            reasons.add("inactive")
        if "inactive" in reasons:
            mode_kind = "inactive"
        elif mode_kind == "inactive":
            mode_kind = ("warmup" if room and target and room[0] < target[0] - inputs.recovery_band_c
                         else "maintenance" if room and target else "unknown")
        # A target/mode change is an external step, never room instability.
        change = previous_right == left and any(
            key in values and key in previous_values and values[key][0] != previous_values[key][1]
            for key in ("target", "mode")
        )
        if change and target and "target" in previous_values and target[0] > previous_values["target"][1]:
            mode_kind = "warmup"
        elif room and target and ((mode_kind == "warmup" and room[0] >= target[0]) or (
                mode_kind == "unknown" and abs(room[0] - target[0]) <= inputs.recovery_band_c)):
            mode_kind = "maintenance"
        impacts = [item for item in disturbances if item["start"] <= left < item["impact_end"]]
        # Cooling during target reductions/inactive mode is not evidence of
        # ventilation. Keep the generic transition recovery instead.
        if (impacts and target and mode_kind != "inactive" and not change and active is not None
                and not (recovery and recovery["reason"] == "target_or_mode_change")
                and (impacts[0]["reason"] == "ventilation_corroborated" or (
                    impacts[0]["prior_stable"]
                    and impacts[0]["baseline_room"] <= target[0] + inputs.recovery_band_c))):
            impact = impacts[0]
            recovery = {"reason": impact["reason"], "started_at": _iso(impact["start"]),
                        "impact_end": _iso(impact["impact_end"]), "baseline_room": impact["baseline_room"],
                        "certainty": impact["certainty"], "stable_intervals": 0}
        external_recovery = reasons & {"dhw", "transition", "reliability"}
        if change or external_recovery:
            reason = "target_or_mode_change" if change else sorted(external_recovery)[0]
            if recovery is None or recovery["reason"] != reason:
                recovery = {"reason": reason, "started_at": _iso(left), "impact_end": _iso(right),
                            "baseline_room": room[0] if room else None, "certainty": "observed_context",
                            "stable_intervals": 0}
            recovery["impact_end"] = _iso(right)
        # Invalid observations interrupt recovery. Their return must establish
        # a stable regime again, rather than bridge an unknown stretch.
        if (room is None or target is None) and recovery is None:
            recovery = {"reason": "data_gap", "started_at": _iso(left), "impact_end": _iso(right),
                        "baseline_room": None, "certainty": "observed_context", "stable_intervals": 0}
        if recovery:
            reasons.add("recovery:" + recovery["reason"])
            stable = bool(room and target and not external_recovery and not change
                          and left >= _time(recovery["impact_end"])
                          and abs(room[1] - target[1]) <= inputs.recovery_band_c
                          and abs(room[1] - room[0]) * 3600 / seconds <= inputs.recovery_slope_c_per_hour)
            # Three adjacent measured intervals establish a trend; an arbitrary
            # timer or a pair of distant endpoints cannot close recovery.
            if not stable or previous_right != left:
                stable_intervals = 0
            if stable and right in room_times:
                stable_intervals += 1
            recovery["stable_intervals"] = stable_intervals
            if stable_intervals >= 3:
                event_windows.append({**recovery, "ended_at": _iso(right), "recovered": True})
                recovery = None
                stable_intervals = 0
        weather = "unknown"
        if "outdoor" in values:
            temperature = sum(values["outdoor"]) / 2
            weather = "below_0" if temperature < 0 else "0_to_10" if temperature < 10 else "above_10"
        run = _empty_run(left, right, request=active, reasons=sorted(reasons), mode=mode_kind,
                         target=target[0] if target else None, outdoor=weather)
        run["control_mode"] = values["mode"][0] if "mode" in values else None
        run["request_semantics"] = inputs.request_semantics
        settings_index = bisect_right(settings_times, left) - 1
        run["settings_version"] = settings_versions[settings_index][1] if settings_index >= 0 else "unknown"
        run["seconds"] = seconds
        run["eligible_seconds"] = 0.0 if reasons else seconds
        if request is not None and active is not None:
            run["request_observed_seconds"] = seconds
            if inputs.request_semantics == "preceding_minute":
                run["request_active_seconds"] = seconds * request[0]
            else:
                run["request_active_seconds"] = seconds if active else 0.0
        for key in ("room", "target", "command", "actual", "outdoor"):
            if key in values:
                run["stats"][key] = _stat(*values[key], seconds)
        if "command" in values and "command" in previous_values and previous_right == left:
            jump = abs(values["command"][0] - previous_values["command"][1])
            command_index = bisect_right(command_times, left - timedelta(microseconds=1)) - 1
            command_previous = command_times[command_index] if command_index >= 0 else (
                _time(previous_boundary["command_last_sample_at"])
                if previous_boundary and previous_boundary.get("command_last_sample_at") else None)
            if jump and command_previous is not None:
                elapsed = (left - command_previous).total_seconds()
                run["stats"]["command"]["max_rate_per_hour"] = max(
                    run["stats"]["command"]["max_rate_per_hour"], jump * 3600 / elapsed)
        if room and target:
            errors = (room[0] - target[0], room[1] - target[1])
            run["stats"]["error"] = _stat(*errors, seconds)
            if errors[0] >= 0:
                run["reached_at"] = _iso(left)
            elif errors[1] >= 0:
                run["reached_at"] = _iso(left + timedelta(seconds=seconds * -errors[0] / (errors[1] - errors[0])))
            run["peak_at"] = _iso(right if room[1] > room[0] else left)
            run["peak_value"] = max(room)
        if "command" in values and "limit" in values:
            run["limit_observed_seconds"] = seconds
            command, limit = values["command"], values["limit"]
            first_delta, last_delta = command[0] - limit[0] + 0.1, command[1] - limit[1] + 0.1
            if min(first_delta, last_delta) >= 0:
                run["limit_seconds"] = seconds
            elif max(first_delta, last_delta) > 0:
                run["limit_seconds"] = seconds * max(first_delta, last_delta) / abs(last_delta - first_delta)
        if "command" in values and "water_limit" in values:
            run["water_limit_observed_seconds"] = seconds
            first_delta = values["command"][0] - values["water_limit"][0] + 0.1
            last_delta = values["command"][1] - values["water_limit"][1] + 0.1
            if min(first_delta, last_delta) >= 0:
                run["water_limit_seconds"] = seconds
            elif max(first_delta, last_delta) > 0:
                run["water_limit_seconds"] = seconds * max(first_delta, last_delta) / abs(last_delta - first_delta)
        state_index = bisect_right(state_times, left) - 1
        if state_index >= 0 and state_index + 1 < len(states):
            state, next_state = states[state_index], states[state_index + 1]
            if ((next_state.timestamp - state.timestamp).total_seconds() <= inputs.max_gap_seconds
                    and state.identity == next_state.identity
                    and not any(state.timestamp <= timestamp < next_state.timestamp
                                for timestamp in inputs.state_unknown_at)):
                flame = "ch" in state.flags and "fl" in state.flags and "dhw" not in state.flags
                run["burner_observed_seconds"] = seconds
                if flame and previous_flame is False and previous_state_end == left:
                    run["burner_starts"] = 1
                previous_flame, previous_state_end = flame, right
                if "dhw" in state.flags:
                    run["reasons"] = sorted(set(run["reasons"]) | {"dhw"})
                    run["eligible_seconds"] = 0.0
            else:
                previous_flame, previous_state_end = None, None
        else:
            previous_flame, previous_state_end = None, None
        anchor_key = [active, target[0] if target else None, run["control_mode"], run["reasons"]]
        if request_anchor is None or request_anchor["key"] != anchor_key or previous_right != left:
            request_anchor = {"key": anchor_key, "baseline": room[0] if room else None,
                              "reaction_at": None, "started_at": _iso(left)}
        baseline = request_anchor["baseline"]
        if room and baseline is not None and not request_anchor["reaction_at"]:
            threshold = baseline + 0.1
            if room[0] >= threshold:
                request_anchor["reaction_at"] = _iso(left)
            elif room[1] >= threshold and room[1] > room[0]:
                request_anchor["reaction_at"] = _iso(left + timedelta(
                    seconds=seconds * (threshold - room[0]) / (room[1] - room[0])))
        run["reaction_at"] = request_anchor["reaction_at"]
        run["reaction_baseline"] = baseline
        run["reaction_continuity"] = True
        for reason in run["reasons"]:
            excluded[reason] += seconds
        _append_run(runs, run)
        previous_values, previous_right = values, right
    if recovery:
        event_windows.append({**recovery, "ended_at": None, "recovered": False})
    last_command_index = bisect_right(command_times, end - timedelta(microseconds=1)) - 1
    last_command_at = _iso(command_times[last_command_index]) if last_command_index >= 0 else None
    result: dict[str, Any] = {"algorithm_version": ALGORITHM_VERSION, "circuit_id": inputs.circuit_id,
              "device_id": inputs.device_id, "scope": f"{inputs.device_id}/{inputs.circuit_id}",
              "period_start": _iso(start), "period_end": _iso(end), "settings_version": inputs.settings_version,
              "request_semantics": inputs.request_semantics, "runs": runs,
              "exclusion_seconds": dict(excluded), "exclusion_windows": event_windows,
              "boundary": {"end": _iso(end), "recovery": recovery, "last_values": previous_values,
                           "mode_kind": mode_kind, "last_flame": previous_flame, "request_anchor": request_anchor,
                           "command_last_sample_at": last_command_at},
              "sample_intervals": _sample_intervals(inputs, start, end),
              "sources": {key: {"identity": series.metadata.identity, "provenance": series.metadata.provenance,
                                 "value_kind": series.metadata.value_kind, "unit": series.metadata.unit}
                          for key, series in inputs.signals.items()},
              "source_context": inputs.reliability_context,
              "negative_input_guards": list(inputs.negative_input_guards),
              "request_observation_totals": request_totals,
              "unknowns": [f"missing:{key}" for key in ("room", "target", "request", "command", "actual", "outdoor",
                                                         "limit") if key not in lookups] + list(inputs.source_unknowns)}
    if request_totals.get("overlapping_windows"):
        result["unknowns"].append("overlapping_minute_request_windows_not_reconstructable")
    result.update(_summarize([result], start, end))
    return result


def _sample_intervals(inputs: RegulationInputs, start: datetime, end: datetime) -> dict[str, Any]:
    result = {}
    for key, series in inputs.signals.items():
        times = sorted({sample.timestamp for sample in series.samples if start <= sample.timestamp <= end})
        intervals = [(right - left).total_seconds() for left, right in zip(times, times[1:], strict=False)]
        result[key] = {"sample_count": sum(timestamp < end for timestamp in times),
                       "minimum_seconds": min(intervals) if intervals else None,
                       "maximum_seconds": max(intervals) if intervals else None,
                       "median_seconds": median(intervals) if intervals else None,
                       "explicit_unknown_count": sum(start <= timestamp < end for timestamp in series.unknown_at)}
    return result


def _metric(numerator: float, denominator: float, expected: float, *, unit: str,
            reasons: list[str] | None = None, divide: bool = True) -> dict[str, Any]:
    return {"value": numerator / denominator if denominator and divide else numerator if denominator else None,
            "numerator": numerator, "denominator": denominator, "denominator_unit": "seconds",
            "unit": unit, "coverage_pct": 100 * denominator / expected if expected else 0.0,
            "unavailable_reasons": reasons or ([] if denominator else ["no_eligible_observations"])}


def _cycle(on: dict[str, Any], off: dict[str, Any] | None, *, complete: bool) -> dict[str, Any]:
    room = on["stats"].get("room", {})
    error = on["stats"].get("error", {})
    end = _time(on["end"])
    off_room = off["stats"].get("room", {}) if off else {}
    result = {"started_at": on["start"], "heating_ended_at": on["end"],
            "ended_at": off["end"] if off else on["end"], "kind": "full" if complete else "partial",
            "heating_seconds": on["seconds"], "pause_seconds": off["seconds"] if off else None,
            "observed_active_seconds": on["request_active_seconds"],
            "activity_observed_seconds": on["request_observed_seconds"], "edge_precision": "observed_state",
            "start_error_c": error.get("first"), "end_error_c": error.get("last"),
            "target_reached_after_seconds": ((_time(on["reached_at"]) - _time(on["start"])).total_seconds()
                                              if on["reached_at"] else None),
            "room_reaction_after_seconds": ((_time(on["reaction_at"]) - _time(on["start"])).total_seconds()
                                             if on["reaction_at"] and _time(on["reaction_at"]) >= _time(on["start"])
                                             and on.get("reaction_continuity", True) else None),
            "room_range_c": max(room.get("maximum", -math.inf), off_room.get("maximum", -math.inf))
                            - min(room.get("minimum", math.inf), off_room.get("minimum", math.inf))
                            if room else None,
            "overshoot_c": max(0.0, max(error.get("maximum", 0.0),
                                        off["stats"].get("error", {}).get("maximum", 0.0) if off else 0.0))
                           if error else None,
            "post_heat_rise_c": max(0.0, off_room.get("maximum", room.get("last", 0.0))
                                     - room.get("last", 0.0)) if room and off_room else None,
            "peak_delay_seconds": max(0.0, (_time(off["peak_at"]) - end).total_seconds())
                                  if off and off["peak_at"] else None,
            "eligible": not on["reasons"] and bool(off and not off["reasons"]),
            "exclusion_reasons": sorted(set(on["reasons"]) | set(off["reasons"] if off else []))}
    if on.get("request_semantics") == "preceding_minute":
        result.update(edge_precision="preceding_minute", request_episode="observed_positive_minute_bins",
                      start_boundary_interval={"earliest": on["start"],
                                               "latest": _iso(_time(on["start"]) + timedelta(minutes=1))},
                      heating_end_boundary_interval={"earliest": _iso(_time(on["end"]) - timedelta(minutes=1)),
                                                     "latest": on["end"]},
                      heating_duration_bounds_seconds={"minimum": max(0.0, on["seconds"] - 120),
                                                       "maximum": on["seconds"]},
                      pause_duration_bounds_seconds={"minimum": off["seconds"], "maximum": off["seconds"] + 120}
                      if off else None,
                      overshoot_applicability="observed_episode_and_following_pause_window")
    return result


def _summarize(facts: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for fact in facts:
        for run in fact["runs"]:
            # Summary mutation must never alter persisted day facts.
            _append_run(runs, deepcopy(run))
    expected = (end - start).total_seconds()
    stats: dict[str, Any] = {}
    eligible = 0.0
    weather: dict[str, float] = defaultdict(float)
    modes: dict[str, float] = defaultdict(float)
    versions: dict[str, float] = defaultdict(float)
    excluded: dict[str, float] = defaultdict(float)
    observed_seconds = fact_seconds = 0.0
    limit_seconds = limit_observed = burner_seconds = burner_starts = 0.0
    water_limit_seconds = water_limit_observed = 0.0
    request_active = request_observed = 0.0
    all_burner_seconds = all_burner_starts = all_request_active = all_request_observed = 0.0
    previous_run = None
    for run in runs:
        fact_seconds += run["seconds"]
        if "room" in run["stats"] and "target" in run["stats"] and run["request"] is not None:
            observed_seconds += run["seconds"]
        all_request_active += run["request_active_seconds"]
        all_request_observed += run["request_observed_seconds"]
        if not any("reliability" in reason for reason in run["reasons"]):
            all_burner_seconds += run["burner_observed_seconds"]
            all_burner_starts += run["burner_starts"]
        for reason in run["reasons"]:
            excluded[reason] += run["seconds"]
        if not run["eligible_seconds"]:
            previous_run = None
            continue
        eligible += run["eligible_seconds"]
        weather[run["weather"]] += run["eligible_seconds"]
        modes[run["mode"]] += run["eligible_seconds"]
        versions[run["settings_version"]] += run["eligible_seconds"]
        limit_seconds += run["limit_seconds"]
        limit_observed += run["limit_observed_seconds"]
        water_limit_seconds += run["water_limit_seconds"]
        water_limit_observed += run["water_limit_observed_seconds"]
        burner_seconds += run["burner_observed_seconds"]
        burner_starts += run["burner_starts"]
        request_active += run["request_active_seconds"]
        request_observed += run["request_observed_seconds"]
        for key, stat in run["stats"].items():
            if key in stats:
                # Variation is summed within eligible continuous runs. A gap,
                # exclusion, mode/target boundary never supplies an artificial jump.
                if (key in {"command", "actual", "room"} and previous_run
                        and previous_run["end"] == run["start"]
                        and previous_run["target"] == run["target"]
                        and previous_run["control_mode"] == run["control_mode"]
                        and key in previous_run["stats"]):
                    stats[key]["variation"] += abs(stat["first"] - previous_run["stats"][key]["last"])
                _merge_stat(stats[key], stat)
            else:
                stats[key] = stat.copy()
        previous_run = run
    metrics = {}
    error = stats.get("error", {})
    denominator = error.get("seconds", 0.0)
    for name, numerator in (("room_signed_error_c", error.get("integral", 0.0)),
                            ("room_absolute_error_c", error.get("positive_integral", 0.0)
                             + error.get("negative_integral", 0.0))):
        metrics[name] = _metric(numerator, denominator, expected, unit="°C")
    metrics["room_overheat_degree_hours"] = _metric(error.get("positive_integral", 0.0) / 3600,
                                                    denominator, expected, unit="°C·h", divide=False)
    metrics["room_underheat_degree_hours"] = _metric(error.get("negative_integral", 0.0) / 3600,
                                                     denominator, expected, unit="°C·h", divide=False)
    for role in ("command", "actual", "room"):
        stat = stats.get(role, {})
        covered = stat.get("seconds", 0.0)
        metrics[f"{role}_total_variation_c"] = _metric(stat.get("variation", 0.0), covered,
                                                       expected, unit="°C", divide=False)
        metrics[f"{role}_variation_c_per_hour"] = _metric(stat.get("variation", 0.0) * 3600, covered,
                                                          expected, unit="°C/h")
        metrics[f"{role}_range_c"] = _metric(stat.get("maximum", 0.0) - stat.get("minimum", 0.0),
                                            covered, expected, unit="°C", divide=False)
        metrics[f"{role}_max_rate_c_per_hour"] = _metric(stat.get("max_rate_per_hour", 0.0), covered,
                                                         expected, unit="°C/h", divide=False)
    metrics["command_at_known_limit_pct"] = _metric(limit_seconds * 100, limit_observed, expected, unit="%")
    if not limit_observed:
        metrics["command_at_known_limit_pct"]["unavailable_reasons"] = ["applicable_historical_limit_unknown"]
    metrics["command_at_known_water_limit_pct"] = _metric(water_limit_seconds * 100, water_limit_observed,
                                                         expected, unit="%")
    if not water_limit_observed:
        metrics["command_at_known_water_limit_pct"]["unavailable_reasons"] = ["historical_water_limit_unknown"]
    metrics["confirmed_heating_burner_starts"] = _metric(all_burner_starts, all_burner_seconds, expected,
                                                        unit="count", divide=False)
    metrics["eligible_confirmed_heating_burner_starts"] = _metric(burner_starts, burner_seconds, expected,
                                                                 unit="count", divide=False)
    metrics["observed_request_active_seconds"] = _metric(all_request_active, all_request_observed, expected,
                                                         unit="seconds", divide=False)
    metrics["eligible_request_active_seconds"] = _metric(request_active, request_observed, expected,
                                                         unit="seconds", divide=False)
    # Cycle reconstruction ignores weather bins but never joins across unknown
    # data, exclusions, mode changes or target changes.
    cycle_runs: list[dict[str, Any]] = []
    for run in runs:
        merged = deepcopy(run)
        merged["weather"] = "all"
        merged["mode"] = "all_phases"
        merged["settings_version"] = "all_versions"
        _append_run(cycle_runs, merged)
    cycles = []
    for index, run in enumerate(cycle_runs):
        if run["request"] is not True:
            continue
        previous = cycle_runs[index - 1] if index else None
        off = cycle_runs[index + 1] if index + 1 < len(cycle_runs) else None
        following = cycle_runs[index + 2] if index + 2 < len(cycle_runs) else None
        valid_previous = bool(previous and previous["request"] is False and previous["end"] == run["start"]
                              and not previous["reasons"] and not run["reasons"]
                              and previous["target"] == run["target"]
                              and previous["control_mode"] == run["control_mode"])
        valid_off = bool(off and off["request"] is False and off["start"] == run["end"]
                         and off["target"] == run["target"] and off["control_mode"] == run["control_mode"])
        complete = bool(valid_previous and valid_off and off and following and following["request"] is True
                        and following["start"] == off["end"] and not off["reasons"]
                        and following["target"] == run["target"]
                        and following["control_mode"] == run["control_mode"]
                        and not following["reasons"])
        cycle = _cycle(run, off if valid_off else None, complete=complete)
        if not valid_previous:
            cycle["exclusion_reasons"] = sorted(set(cycle["exclusion_reasons"]) | {"start_boundary_unknown"})
        cycles.append(cycle)
    requests = {run["request"] for run in cycle_runs}
    if requests == {False} and eligible == expected:
        classification = "no_heat"
    elif requests == {True} and eligible == expected:
        classification = "continuous"
    elif any(cycle["kind"] == "full" for cycle in cycles):
        classification = "cycles"
    else:
        classification = "partial"
    approximate = any(fact.get("request_semantics") != "state" for fact in facts)
    if approximate and request_active < request_observed:
        classification = "partial" if classification == "continuous" else classification
    full = [cycle for cycle in cycles if cycle["kind"] == "full" and cycle["eligible"]]
    for key, unit in (("heating_seconds", "seconds"), ("pause_seconds", "seconds"), ("start_error_c", "°C"),
                      ("end_error_c", "°C"), ("room_reaction_after_seconds", "seconds"),
                      ("target_reached_after_seconds", "seconds"), ("overshoot_c", "°C"),
                      ("post_heat_rise_c", "°C"), ("peak_delay_seconds", "seconds")):
        measured = [cycle[key] for cycle in full if cycle[key] is not None]
        metrics["cycle_mean_" + key] = {"value": sum(measured) / len(measured) if measured else None,
                                      "numerator": sum(measured), "denominator": len(measured),
                                      "denominator_unit": "complete_eligible_cycles", "unit": unit,
                                      "coverage_pct": 100 * len(measured) / len(cycles) if cycles else 0,
                                      "unavailable_reasons": [] if measured else ["no_complete_eligible_cycles"]}
    return {"metrics": metrics, "classification": classification,
            "coverage": {"expected_seconds": expected, "fact_seconds": fact_seconds,
                         "observed_seconds": observed_seconds,
                         "eligible_seconds": eligible, "eligible_pct": 100 * eligible / expected if expected else 0,
                         "weather_seconds": dict(weather), "mode_seconds": dict(modes),
                         "settings_version_seconds": dict(versions),
                         "exclusion_seconds": dict(excluded)},
            "cycle_counts": {"full": sum(cycle["kind"] == "full" for cycle in cycles),
                             "partial": sum(cycle["kind"] == "partial" for cycle in cycles),
                             "eligible": sum(cycle["kind"] == "full" and cycle["eligible"] for cycle in cycles),
                             "excluded": sum(bool(cycle["exclusion_reasons"]) for cycle in cycles)},
            "cycles": cycles}


def aggregate_regulation_facts(facts: list[dict[str, Any]], *, start: datetime, end: datetime,
                               expected_days: int | None = None) -> dict[str, Any]:
    """Aggregate every compact day, deduplicate identical days, reject overlap.

    Do not derive missing facts from legacy report means. No raw samples are
    retained or passed to the AI; only the representative cycle list is capped.
    """
    selected: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact.get("algorithm_version") != ALGORITHM_VERSION:
            continue
        if start <= _time(fact["period_start"]) < _time(fact["period_end"]) <= end:
            key = fact["period_start"]
            if key in selected and selected[key] != fact:
                raise ValueError("conflicting regulation facts for one day")
            selected[key] = fact
    ordered = sorted(selected.values(), key=lambda item: item["period_start"])
    if len({item["scope"] for item in ordered}) > 1:
        raise ValueError("regulation facts from different circuits must remain separate")
    if any(_time(left["period_end"]) > _time(right["period_start"])
           for left, right in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("overlapping regulation fact intervals")
    result = _summarize(ordered, start, end)
    count = len(result["cycles"])
    # Complete counts and metrics were calculated before display sampling.
    result["cycles"] = result["cycles"][:8]
    incomplete = (not ordered or result["coverage"]["fact_seconds"] < (end - start).total_seconds()
                  or (expected_days is not None and len(ordered) != expected_days))
    sampling: dict[str, Any] = {}
    recovery_windows: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ordered:
        for window in item.get("exclusion_windows", []):
            key = (window["reason"], window["started_at"])
            previous = recovery_windows.get(key)
            if previous is None or (window.get("recovered") and (
                    not previous.get("recovered") or window.get("ended_at", "") > previous.get("ended_at", ""))):
                recovery_windows[key] = deepcopy(window)
        for key, source in item.get("sample_intervals", {}).items():
            target = sampling.setdefault(key, {"sample_count": 0, "explicit_unknown_count": 0,
                                               "minimum_seconds": None, "maximum_seconds": None})
            for name in ("sample_count", "explicit_unknown_count"):
                target[name] += source[name]
            for name, combine in (("minimum_seconds", min), ("maximum_seconds", max)):
                if source[name] is not None:
                    target[name] = combine(target[name], source[name]) if target[name] is not None else source[name]
    all_exclusions = sorted(recovery_windows.values(), key=lambda window: (window["started_at"], window["reason"]))
    examples = all_exclusions if len(all_exclusions) <= 12 else [
        all_exclusions[round(index * (len(all_exclusions) - 1) / 11)] for index in range(12)]
    source_incomplete = any(item.get("source_context", {}).get("bounded_context_incomplete") for item in ordered)
    request_observations = [item.get("request_observation_totals", {}) for item in ordered]
    request_totals = {"minute_observation_count": sum(item.get("minute_observation_count", 0)
                                                    for item in request_observations),
                      "active_seconds_in_observation_windows_sum": sum(
                          item.get("active_seconds_in_observation_windows_sum", 0) for item in request_observations),
                      "overlapping_windows": any(item.get("overlapping_windows") for item in request_observations),
                      "cropped_fractional_windows": sum(item.get("cropped_fractional_windows", 0)
                                                         for item in request_observations),
                      "sum_is_chronological_duration": all(item.get("sum_is_chronological_duration", True)
                                                            for item in request_observations)}
    result.update({"algorithm_version": ALGORITHM_VERSION, "period_start": _iso(start), "period_end": _iso(end),
                   "included_days": len(ordered), "expected_days": expected_days,
                   "cycle_examples_total": count, "circuit_id": ordered[0]["circuit_id"] if ordered else None,
                   "scope": ordered[0]["scope"] if ordered else None, "sample_intervals": sampling,
                   "exclusion_windows": examples, "exclusion_windows_total": len(all_exclusions),
                   "exclusion_windows_truncated": len(all_exclusions) > len(examples),
                   "request_observation_totals": request_totals,
                   "incomplete": incomplete or source_incomplete, "source_context_incomplete": source_incomplete,
                   "unknowns": sorted({value for item in ordered for value in item.get("unknowns", [])}
                                      | ({"missing_facts_require_local_backfill"} if incomplete else set())),
                   "settings_versions": sorted({item["settings_version"] for item in ordered})})
    return result

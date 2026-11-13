"""Observed setting changes and conservative, deterministic before/after evidence.

No AI, controller writes, inferred intention, or gas-saving claims belong here.
All runs, rather than presentation samples of cycles/windows, are considered.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

ALGORITHM_VERSION = "regulation-comparison-v2"


@dataclass(frozen=True)
class ComparisonRules:
    minimum_hours: float = 6.0
    minimum_complete_cycles: int = 3
    minimum_continuous_hours: float = 12.0
    minimum_condition_coverage: float = 0.7
    minimum_metric_coverage: float = 0.8
    weather_bin_c: float = 2.0
    room_error_tolerance_c: float = 0.05
    overshoot_tolerance_c: float = 0.1
    flow_variation_tolerance_c_per_hour: float = 0.5
    maximum_boundary_hours: float = 24.0
    interval_examples: int = 24
    forecast_examples: int = 4


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def when(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1_000_000, UTC)
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError("comparison evidence requires timezone-aware timestamps")
    return result.astimezone(UTC)


def _equal(left: Any, right: Any) -> bool:
    # Owner forms serialize numbers as strings. Booleans are never numbers here.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        if a.is_finite() and b.is_finite():
            return a == b
    except InvalidOperation:
        pass
    return bool(left == right)


def _manual_proves(item: dict[str, Any], event: dict[str, Any], timeline: dict[str, Any]) -> bool:
    experiment = item.get("experiment") or {}
    changes = experiment.get("changes") or []
    if experiment.get("category") != "settings" or not changes:
        return False
    fields = set(event["changed_fields"])
    # Every changed field must be accounted for, with no other circuit/action.
    if {change.get("parameter") for change in changes} != fields or len(changes) != len(fields):
        return False
    for change in changes:
        name = change["parameter"]
        if (str(change.get("device_id")) != str(timeline.get("device_id"))
                or str(change.get("circuit_id")) != str(timeline.get("circuit_id"))):
            return False
        old = event["before_state"].get("fields", {}).get(name, {})
        new = event["after_state"].get("fields", {}).get(name, {})
        if (old.get("status") != "observed" or new.get("status") != "observed"
                or "value" not in old or "value" not in new
                or not _equal(change.get("before"), old["value"])
                or not _equal(change.get("after"), new["value"])):
            return False
    return True


def observed_changes(timeline: dict[str, Any], manual_history: list[dict[str, Any]]) -> dict[str, Any]:
    """Join only fully proven manual/automatic actions; preserve conflicting notes.

    An owner's time refines a boundary only within the observed open/closed
    interval and after matching the complete changed field set and scope.
    """
    versions = sorted(timeline.get("versions", []), key=lambda value: when(value["observed_at"]))
    events: list[dict[str, Any]] = []
    for previous, current in zip(versions, versions[1:], strict=False):
        if current.get("kind") != "change" or current["version"] == previous["version"]:
            continue
        boundary = current.get("boundary") or {}
        lower, upper = boundary.get("after"), boundary.get("no_later_than", current["observed_at"])
        events.append({
            "event_id": str(current["event_id"]), "sources": ["observed_configuration"],
            "before_version": previous["version"], "after_version": current["version"],
            "before_state": deepcopy(previous["state"]), "after_state": deepcopy(current["state"]),
            "changed_fields": list(current.get("changed_fields", [])),
            "boundary": {"after": when(lower).isoformat() if lower is not None else None,
                         "no_later_than": when(upper).isoformat(), "exact_effective_at": None,
                         "provenance": "observed_configuration_boundary"},
            "manual_records": [], "owner_time_conflict": False,
            "rollback": any(version["version"] == current["version"] for version in versions
                            if when(version["observed_at"]) < when(previous["observed_at"])),
            "author": "unknown", "intent": "unknown",
        })
    unmatched: list[dict[str, Any]] = []
    for item in manual_history:
        experiment = item.get("experiment") or {}
        at = experiment.get("performed_at")
        if not at:
            unmatched.append(deepcopy(item))
            continue
        matches = [event for event in events if event["boundary"]["after"] is not None
                   and when(event["boundary"]["after"]) < when(at) <= when(event["boundary"]["no_later_than"])
                   and _manual_proves(item, event, timeline)]
        if len(matches) != 1:
            unmatched.append(deepcopy(item))
            continue
        event = matches[0]
        event["manual_records"].append(deepcopy(item))
        event["sources"] = ["observed_configuration", "owner_reported"]
    for event in events:
        times = {when(item["experiment"]["performed_at"]).isoformat() for item in event["manual_records"]}
        if len(times) == 1:
            event["boundary"].update(exact_effective_at=next(iter(times)),
                                     provenance="owner_time_with_matching_observed_values")
        elif len(times) > 1:
            event["owner_time_conflict"] = True
    return {"events": events, "unmatched_manual_records": unmatched,
            "incomplete": bool(timeline.get("incomplete"))}


def _stratum(run: dict[str, Any], rules: ComparisonRules) -> tuple[Any, ...] | None:
    outdoor = run.get("stats", {}).get("outdoor", {})
    seconds = outdoor.get("seconds", 0)
    if (not seconds or run.get("target") is None or run.get("control_mode") is None
            or run.get("mode") not in {"maintenance", "warmup"}):
        return None
    # Require the whole measured weather span to lie inside one narrow bin.
    low = math.floor(outdoor["minimum"] / rules.weather_bin_c)
    high = math.floor(outdoor["maximum"] / rules.weather_bin_c)
    if low != high:
        return None
    return run["mode"], run["target"], run["control_mode"], low


def _metric_values(runs: list[dict[str, Any]]) -> dict[str, float | None]:
    error_seconds = sum(run["stats"].get("error", {}).get("seconds", 0) for run in runs)
    error = sum(run["stats"].get("error", {}).get("positive_integral", 0)
                + run["stats"].get("error", {}).get("negative_integral", 0) for run in runs)
    result: dict[str, float | None] = {"room_absolute_error_c": error / error_seconds if error_seconds else None}
    for role in ("actual", "command"):
        seconds = sum(run["stats"].get(role, {}).get("seconds", 0) for run in runs)
        variation = sum(run["stats"].get(role, {}).get("variation", 0) for run in runs)
        for old, new in zip(runs, runs[1:], strict=False):
            if (old["end"] == new["start"] and old.get("target") == new.get("target")
                    and old.get("control_mode") == new.get("control_mode")
                    and old.get("mode") == new.get("mode")
                    and role in old["stats"] and role in new["stats"]):
                variation += abs(new["stats"][role]["first"] - old["stats"][role]["last"])
        result[f"{role}_variation_c_per_hour"] = variation * 3600 / seconds if seconds else None
    return result


def _request_episodes(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Complete observed state cycles or minute-bin activity episodes.

    Positive activity bins do not prove uninterrupted demand or one physical
    subminute cycle. Their observed envelope has bounded uncertain edges.
    """
    segments: list[dict[str, Any]] = []
    for run in runs:
        semantics = run.get("request_semantics", "state")
        uncertainty = max(run.get("request_bin_seconds", 60), run.get("request_edge_uncertainty_seconds", 0)) \
            if semantics == "preceding_minute" else 0
        observed = run.get("request_observed_seconds", 0)
        active = run.get("request_active_seconds", 0)
        seconds = run["seconds"]
        valid = (semantics in {"state", "preceding_minute"} and 0 <= uncertainty <= 60
                 and observed + 1e-6 >= seconds and 0 <= active <= seconds + 1e-6
                 and (active > 0 if run.get("request") is True else active == 0)
                 and (semantics != "state" or run.get("request") is not True or active + 1e-6 >= seconds)
                 and run.get("stats", {}).get("error", {}).get("seconds", 0) + 1e-6 >= seconds)
        key = (run.get("request"), run.get("target"), run.get("control_mode"),
               run.get("mode"), run.get("settings_version"), semantics, uncertainty)
        if segments and segments[-1]["end"] == run["start"] and segments[-1]["key"] == key:
            segments[-1]["end"] = run["end"]
            segments[-1]["peak"] = max(segments[-1]["peak"], run["stats"].get("error", {}).get("maximum", 0))
            segments[-1]["seconds"] += seconds
            segments[-1]["active_seconds"] += active
            segments[-1]["observed_seconds"] += observed
            segments[-1]["valid"] = segments[-1]["valid"] and valid
        else:
            segments.append({"key": key, "start": run["start"], "end": run["end"],
                             "seconds": seconds, "active_seconds": active,
                             "observed_seconds": observed, "valid": valid,
                             "peak": run["stats"].get("error", {}).get("maximum", 0)})
    result = []
    for previous, on, off, following in zip(segments, segments[1:], segments[2:], segments[3:], strict=False):
        if (previous["key"][0] is False and on["key"][0] is True and off["key"][0] is False
                and following["key"][0] is True
                and previous["key"][1:] == on["key"][1:] == off["key"][1:] == following["key"][1:]
                and previous["end"] == on["start"] and on["end"] == off["start"]
                and off["end"] == following["start"]
                and all(segment["valid"] for segment in (previous, on, off, following))):
            edge = on["key"][-1]
            if edge and min(previous["seconds"], on["seconds"], off["seconds"], following["seconds"]) < edge:
                continue
            result.append({
                "started_at": on["start"], "heating_ended_at": on["end"], "ended_at": off["end"],
                "edge_precision": "preceding_minute" if edge else "observed_state",
                "edge_uncertainty_seconds": edge,
                "start_boundary_interval": {"earliest": on["start"],
                                            "latest": (when(on["start"]) + timedelta(seconds=edge)).isoformat()},
                "heating_end_boundary_interval": {
                    "earliest": (when(on["end"]) - timedelta(seconds=edge)).isoformat(), "latest": on["end"]},
                "heating_duration_bounds_seconds": {"minimum": max(0.0, on["seconds"] - 2 * edge),
                                                    "maximum": on["seconds"]},
                "pause_duration_bounds_seconds": {"minimum": off["seconds"],
                                                  "maximum": off["seconds"] + 2 * edge},
                "observed_active_seconds": on["active_seconds"],
                "activity_observed_seconds": on["observed_seconds"],
                "overshoot_c": max(0.0, on["peak"], off["peak"]),
                "overshoot_semantics": "observed_positive_bin_and_following_zero_bin_envelope" if edge
                else "observed_request_cycle_and_pause",
            })
    return result


def _cycle_peaks(runs: list[dict[str, Any]]) -> list[float]:
    return [episode["overshoot_c"] for episode in _request_episodes(runs)]


def _summary(runs: list[dict[str, Any]], *, rules: ComparisonRules) -> dict[str, Any]:
    seconds = sum(run["seconds"] for run in runs)
    episodes = _request_episodes(runs)
    peaks = [episode["overshoot_c"] for episode in episodes]
    metrics = _metric_values(runs)
    metrics["cycle_overshoot_c"] = sum(peaks) / len(peaks) if peaks else None
    intervals = [{"start": run["start"], "end": run["end"], "settings_version": run["settings_version"]}
                 for run in runs]
    coverage = {role: sum(run["stats"].get(role, {}).get("seconds", 0) for run in runs) / seconds
                if seconds else 0 for role in ("error", "actual", "command")}
    active = sum(run.get("request_active_seconds", 0) for run in runs)
    observed = sum(run.get("request_observed_seconds", 0) for run in runs)
    longest_continuous = continuous_seconds = 0.0
    previous_active: dict[str, Any] | None = None
    for run in runs:
        fully_active = (run.get("request") is True
                        and run.get("request_observed_seconds", 0) + 1e-6 >= run["seconds"]
                        and run.get("request_active_seconds", 0) + 1e-6 >= run["seconds"])
        if fully_active:
            adjacent = bool(previous_active and previous_active["end"] == run["start"] and all(
                previous_active.get(key) == run.get(key)
                for key in ("target", "control_mode", "mode", "settings_version")))
            continuous_seconds = continuous_seconds + run["seconds"] if adjacent else run["seconds"]
            longest_continuous = max(longest_continuous, continuous_seconds)
            previous_active = run
        else:
            previous_active = None
            continuous_seconds = 0.0
    continuous = longest_continuous >= rules.minimum_continuous_hours * 3600
    return {"useful_hours": seconds / 3600, "complete_cycles": len(peaks), "continuous_heat": continuous,
            "longest_continuous_heat_hours": longest_continuous / 3600,
            "complete_request_episodes": len(episodes),
            "exact_state_cycles": sum(episode["edge_precision"] == "observed_state" for episode in episodes),
            "minute_bin_request_episodes": sum(episode["edge_precision"] == "preceding_minute" for episode in episodes),
            "request_episode_precision": sorted({episode["edge_precision"] for episode in episodes}),
            "request_edge_uncertainty_seconds": max((episode["edge_uncertainty_seconds"] for episode in episodes),
                                                    default=None),
            "request_episode_examples": episodes[:4], "request_episode_examples_truncated": len(episodes) > 4,
            "observed_request_active_seconds": active, "request_activity_observed_seconds": observed,
            "runs": len(runs), "metrics": metrics, "metric_coverage": coverage,
            "intervals": intervals[:rules.interval_examples], "interval_count": len(intervals),
            "intervals_truncated": len(intervals) > rules.interval_examples,
            "all_intervals_fingerprint": fingerprint(intervals),
            "settings_versions": sorted({run["settings_version"] for run in runs})}


def _direction(delta: float, tolerance: float) -> str:
    return "improved" if delta < -tolerance else "worsened" if delta > tolerance else "indistinguishable"


def _weighted_metrics(groups: dict[tuple[Any, ...], list[dict[str, Any]]],
                      weights: dict[tuple[Any, ...], float], name: str,
                      *, minimum_coverage: float = 0.8) -> float | None:
    values = []
    for key, weight in weights.items():
        if name == "cycle_overshoot_c":
            peaks = _cycle_peaks(groups[key])
            value = sum(peaks) / len(peaks) if peaks else None
        else:
            role = {"room_absolute_error_c": "error", "actual_variation_c_per_hour": "actual",
                    "command_variation_c_per_hour": "command"}[name]
            expected = sum(run["seconds"] for run in groups[key])
            covered = sum(run["stats"].get(role, {}).get("seconds", 0) for run in groups[key])
            value = (_metric_values(groups[key]).get(name)
                     if expected and covered / expected >= minimum_coverage else None)
        values.append((value, weight))
    if any(value is None for value, _weight in values):
        return None
    total = sum(weight for _value, weight in values)
    return sum(float(value) * weight for value, weight in values if value is not None) / total if total else None


def compare_change(event: dict[str, Any], *, events: list[dict[str, Any]],
                   facts: list[dict[str, Any]], start: datetime, end: datetime,
                   unmatched_manual: list[dict[str, Any]] | None = None,
                   incomplete_history: bool = False,
                   rules: ComparisonRules | None = None) -> dict[str, Any]:
    """Assess one joint intervention; preserve every exclusion and dependency.

    Metrics are standardized over shared weather/target/mode strata. This is an
    observational association, never identification of individual coefficients.
    """
    rules = rules or ComparisonRules()
    boundary = event["boundary"]
    lower = when(boundary["after"]) if boundary["after"] is not None else None
    upper = when(boundary["no_later_than"])
    exact = when(boundary["exact_effective_at"]) if boundary.get("exact_effective_at") else None
    before_end, after_start = exact or lower, exact or upper
    prior = [other for other in events if when(other["boundary"]["no_later_than"]) < upper]
    following = [other for other in events if when(other["boundary"]["no_later_than"]) > upper]
    left = max([start, *(when(other["boundary"]["no_later_than"]) for other in prior)])
    next_boundaries = [when(other["boundary"]["after"]) if other["boundary"]["after"] is not None
                       else when(other["boundary"]["no_later_than"]) for other in following]
    right = min([end, *next_boundaries])
    confounds = []
    unplaced_confounds = []
    following_manual = False
    for item in unmatched_manual or []:
        experiment = item.get("experiment") or {}
        at = experiment.get("performed_at")
        changes = experiment.get("changes") or []
        # Unknown scope remains a possible confound. Explicit other scopes do not.
        if (facts and changes and all(str(change.get("device_id")) + "/" + str(change.get("circuit_id"))
                                     != str(facts[0].get("scope")) for change in changes)
                and all(change.get("device_id") and change.get("circuit_id") for change in changes)):
            continue
        if not at:
            unplaced_confounds.append(str(item.get("intervention_id", "owner_record")))
        if at and left <= when(at) < right:
            confounds.append(str(item.get("intervention_id", "owner_record")))
            if before_end is not None and when(at) < before_end:
                left = max(left, when(at))
            elif when(at) >= after_start:
                right = min(right, when(at))
                following_manual = True
    event_evidence = {key: deepcopy(value) for key, value in event.items()
                      if key not in {"before_state", "after_state", "manual_records"}}
    event_evidence["changed_values"] = {
        key: {"before": deepcopy(event["before_state"].get("fields", {}).get(key)),
              "after": deepcopy(event["after_state"].get("fields", {}).get(key))} for key in event["changed_fields"]}
    event_evidence["manual_record_ids"] = [item.get("intervention_id") for item in event["manual_records"]]
    result: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION, "event_id": event["event_id"], "event": event_evidence,
        "stage": "detected", "outcome": None, "epistemic_level": "derived",
        "causal_status": "observational_association; joint_change_only",
        "component_effects": None, "gas_savings": None,
        "gas_reason": "requires_separate_measurement_or_validated_model_with_uncertainty",
        "observation_start": left.isoformat(), "observation_end": right.isoformat(),
        "boundary_excluded": {"start": lower.isoformat() if lower else None, "end": upper.isoformat(),
                              "refined_by_owner": exact is not None},
        "metrics": {}, "exclusion_seconds": {}, "limitations": [], "next_observation": [],
        "manual_confounds": confounds, "unplaced_manual_confounds": unplaced_confounds,
        "forecast_assessments": [], "rules": asdict(rules),
        "source_facts": [{"start": fact["period_start"], "end": fact["period_end"],
                          "fingerprint": fact.get("source_fingerprint"),
                          "algorithm_version": fact.get("algorithm_version"),
                          "settings_version": fact.get("settings_version")} for fact in facts],
    }
    selected: dict[str, list[dict[str, Any]]] = {"before": [], "after": []}
    excluded: dict[str, float] = defaultdict(float)
    seen: dict[tuple[datetime, datetime], dict[str, Any]] = {}
    for fact in facts:
        for source_run in fact.get("runs", []):
            run = {**source_run, "request_semantics": source_run.get(
                "request_semantics", fact.get("request_semantics", "unknown"))}
            if run["request_semantics"] == "preceding_minute":
                run["request_bin_seconds"] = source_run.get("request_bin_seconds", fact.get("request_bin_seconds", 60))
            a, b = when(run["start"]), when(run["end"])
            if a < left or b > right or b <= a:
                continue
            key = a, b
            if key in seen:
                if seen[key] != run:
                    raise ValueError("conflicting regulation comparison runs")
                continue
            seen[key] = run
    ordered_runs = sorted(seen.values(), key=lambda run: when(run["start"]))
    if any(when(a["end"]) > when(b["start"]) for a, b in zip(ordered_runs, ordered_runs[1:], strict=False)):
        raise ValueError("overlapping regulation comparison runs")
    for run in ordered_runs:
        a, b = when(run["start"]), when(run["end"])
        reasons = list(run.get("reasons", []))
        side = "before" if before_end is not None and b <= before_end else "after" if a >= after_start else None
        if side is None:
            reasons.append("uncertain_change_boundary")
        elif run.get("settings_version") != event[f"{'before' if side == 'before' else 'after'}_version"]:
            reasons.append("settings_version_not_applicable")
        if run.get("eligible_seconds", 0) < run["seconds"]:
            reasons.append("ineligible_regulation_fact")
        if _stratum(run, rules) is None:
            reasons.append("weather_target_or_mode_unknown_or_changing")
        if reasons:
            for reason in set(reasons):
                excluded[reason] += run["seconds"]
        elif side:
            selected[side].append(run)
    groups: dict[str, dict[tuple[Any, ...], list[dict[str, Any]]]] = {}
    for side in selected:
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for run in selected[side]:
            stratum = _stratum(run, rules)
            assert stratum is not None
            grouped[stratum].append(run)
        groups[side] = grouped
    shared = groups["before"].keys() & groups["after"].keys()
    weights = {key: min(sum(run["seconds"] for run in groups["before"][key]),
                        sum(run["seconds"] for run in groups["after"][key])) for key in shared}
    matched: dict[str, list[dict[str, Any]]] = {}
    for side in selected:
        matched[side] = [run for run in selected[side] if _stratum(run, rules) in shared]
        excluded[f"{side}:unmatched_weather_target_or_mode"] += sum(
            run["seconds"] for run in selected[side] if _stratum(run, rules) not in shared)
    summaries = {side: _summary(runs, rules=rules) for side, runs in matched.items()}
    result.update(summaries)
    result["exclusion_seconds"] = dict(excluded)
    result["shared_conditions"] = [list(key) for key in sorted(shared, key=repr)]
    result["standardized_hours"] = sum(weights.values()) / 3600
    reasons = result["next_observation"]
    if len({fact.get("scope") for fact in facts}) > 1:
        raise ValueError("comparison facts from different circuits must remain separate")
    if lower is None:
        reasons.append("previous_configuration_boundary_unknown")
    if incomplete_history:
        reasons.append("settings_history_incomplete")
    if lower and exact is None and (upper - lower).total_seconds() > rules.maximum_boundary_hours * 3600:
        reasons.append("change_boundary_too_wide; obtain_supported_owner_time_or_closer_observations")
    if event.get("owner_time_conflict"):
        result["limitations"].append("conflicting_owner_times; observed_boundary_retained")
    if not shared:
        reasons.append("observe_matching_weather_target_and_mode")
    if result["standardized_hours"] < rules.minimum_hours:
        reasons.append("observe_more_hours_balanced_across_shared_conditions")
    for side, summary in summaries.items():
        window_left, window_right = (left, before_end) if side == "before" else (after_start, right)
        expected = max(0.0, (window_right - window_left).total_seconds()) if window_right is not None else 0
        covered = sum(run["seconds"] for run in ordered_runs
                      if window_right is not None and when(run["start"]) >= window_left
                      and when(run["end"]) <= window_right)
        summary["fact_coverage"] = covered / expected if expected else 0.0
        if summary["fact_coverage"] < rules.minimum_condition_coverage:
            reasons.append(f"{side}:missing_regulation_facts_or_gaps")
        total = sum(run["seconds"] for run in selected[side])
        condition_coverage = sum(run["seconds"] for run in matched[side]) / total if total else 0
        summary["condition_coverage"] = condition_coverage
        if condition_coverage < rules.minimum_condition_coverage:
            reasons.append(f"{side}:matching_conditions_cover_too_little_history")
        if summary["useful_hours"] < rules.minimum_hours:
            reasons.append(f"{side}:observe_more_useful_hours")
        if summary["metric_coverage"]["error"] < rules.minimum_metric_coverage:
            reasons.append(f"{side}:room_error_coverage_insufficient")
        if summary["metric_coverage"]["actual"] < rules.minimum_metric_coverage:
            reasons.append(f"{side}:actual_flow_coverage_insufficient")
        summary["condition_metric_coverage"] = []
        for key in sorted(shared, key=repr):
            expected_seconds = sum(run["seconds"] for run in groups[side][key])
            coverage = {role: sum(run["stats"].get(role, {}).get("seconds", 0) for run in groups[side][key])
                        / expected_seconds for role in ("error", "actual", "command")}
            summary["condition_metric_coverage"].append({"conditions": list(key), "coverage": coverage})
        for role in ("error", "actual"):
            if any(item["coverage"][role] < rules.minimum_metric_coverage
                   for item in summary["condition_metric_coverage"]):
                reasons.append(f"{side}:{role}_coverage_insufficient_in_shared_condition")
        if summary["complete_cycles"] < rules.minimum_complete_cycles and not (
            summary["continuous_heat"] and summary["useful_hours"] >= rules.minimum_continuous_hours
        ):
            reasons.append(f"{side}:observe_more_complete_cycles_or_sustained_continuous_heat")
    tolerances = {"room_absolute_error_c": rules.room_error_tolerance_c,
                  "actual_variation_c_per_hour": rules.flow_variation_tolerance_c_per_hour,
                  "command_variation_c_per_hour": rules.flow_variation_tolerance_c_per_hour,
                  "cycle_overshoot_c": rules.overshoot_tolerance_c}
    for name, tolerance in tolerances.items():
        before_metric, after_metric = (_weighted_metrics(groups[side], weights, name,
                                                        minimum_coverage=rules.minimum_metric_coverage)
                                       for side in ("before", "after"))
        delta = after_metric - before_metric if before_metric is not None and after_metric is not None else None
        result["metrics"][name] = {"before": before_metric, "after": after_metric, "delta": delta,
                                   "direction": _direction(delta, tolerance) if delta is not None else "unavailable",
                                   "tolerance": tolerance,
                                   "unavailable_reason": None if delta is not None else (
                                       "no_complete_observed_cycles_in_every_shared_condition"
                                       if name == "cycle_overshoot_c"
                                       else "missing_signal_in_shared_conditions")}
    # Independent temporal halves must support each metric's direction. A single
    # exceptional cycle/run cannot establish stable improvement.
    stability: dict[str, str] = {}
    for name, tolerance in tolerances.items():
        halves: dict[str, list[float | None]] = {"before": [], "after": []}
        for half in (0, 1):
            divided = {side: {key: runs[:len(runs) // 2] if half == 0 else runs[len(runs) // 2:]
                              for key, runs in groups[side].items()} for side in groups}
            half_weights = {key: min(sum(run["seconds"] for run in divided["before"][key]),
                                     sum(run["seconds"] for run in divided["after"][key])) for key in shared}
            if any(weight < 3600 for weight in half_weights.values()):
                for side in halves:
                    halves[side].append(None)
            else:
                for side in halves:
                    halves[side].append(_weighted_metrics(divided[side], half_weights, name,
                                                          minimum_coverage=rules.minimum_metric_coverage))
        directions = [_direction(float(b) - float(a), tolerance) for a, b in
                      zip(halves["before"], halves["after"], strict=True) if a is not None and b is not None]
        expected_direction = result["metrics"][name]["direction"]
        stability[name] = ("consistent" if len(directions) == 2 and set(directions) == {expected_direction}
                           else "not_established")
        if (name in {"room_absolute_error_c", "actual_variation_c_per_hour", "cycle_overshoot_c"}
                and result["metrics"][name]["direction"] != "unavailable"
                and stability[name] != "consistent"):
            reasons.append(f"{name}:observe_stable_result_across_independent_intervals")
    result["stability"] = stability
    if following:
        result["limitations"].append("subsequent_change_truncates_after_window")
    if following_manual:
        result["limitations"].append("owner_reported_subsequent_action_truncates_after_window")
    if prior:
        result["limitations"].append("previous_change_truncates_baseline")
    if event.get("rollback"):
        result["limitations"].append("rollback; earlier_occupancy_and_thermal_state_not_identified")
    if len(event["changed_fields"]) > 1:
        result["limitations"].append("joint_parameter_change; individual_parameter_effects_not_identifiable")
    if confounds:
        result["limitations"].append("unmatched_owner_actions; cause_and_scope_may_be_uncertain")
    if unplaced_confounds:
        result["limitations"].append("owner_action_time_unknown; cannot_locate_or_exclude_its_effect")
    if any(summary["minute_bin_request_episodes"] for summary in summaries.values()):
        result["limitations"].append(
            "minute_bin_request_episodes; edges_uncertain_up_to_60s; physical_subminute_cycles_unknown")
        result["metrics"]["cycle_overshoot_c"]["measurement_semantics"] = (
            "observed_positive_bin_and_following_zero_bin_envelope; not_exact_physical_cycle_overshoot")
    if not reasons:
        result["stage"] = "available"
        outcome_directions = {value["direction"] for value in result["metrics"].values()} - {
            "unavailable", "indistinguishable"}
        result["outcome"] = ("mixed" if len(outcome_directions) > 1 else next(iter(outcome_directions))
                             if outcome_directions else "indistinguishable")
        if ((summaries["before"]["continuous_heat"] or summaries["after"]["continuous_heat"])
                and result["metrics"]["cycle_overshoot_c"]["direction"] == "unavailable"):
            result["limitations"].append("continuous_heat; cycle_overshoot_unavailable")
    elif (following or following_manual) and summaries["after"]["useful_hours"] < rules.minimum_hours:
        result["stage"] = "superseded"
    elif before_end is None or incomplete_history or any("boundary_too_wide" in reason for reason in reasons):
        result["stage"] = "insufficient"
    elif end <= upper or (not summaries["after"]["useful_hours"]
                         and (end - upper).total_seconds() < rules.minimum_hours * 3600
                         and not any(reason.startswith("recovery:") for reason in excluded)):
        result["stage"] = "detected"
    elif (end - upper).total_seconds() < rules.minimum_hours * 3600 or (
            ordered_runs and any(reason.startswith("recovery:") for reason in ordered_runs[-1].get("reasons", []))):
        result["stage"] = "transition_or_collecting"
    else:
        result["stage"] = "insufficient"
    result["forecast_count"] = len(event["manual_records"])
    result["forecasts_truncated"] = len(event["manual_records"]) > rules.forecast_examples
    for record in event["manual_records"][:rules.forecast_examples]:
        original = deepcopy((record.get("experiment") or {}).get("original"))
        generated = original.get("generated_at") if isinstance(original, dict) else None
        recorded = record.get("recorded_at")
        earliest_change = exact or lower
        prediction_timing = ("verified_before_change" if generated and earliest_change
                             and when(generated) <= earliest_change else "unknown_or_not_before_change")
        expectation_timing = ("recorded_before_change" if recorded and earliest_change
                              and when(recorded) <= earliest_change else "retrospective_or_unknown")
        result["forecast_assessments"].append({
            "intervention_id": record.get("intervention_id"), "original": original,
            "prediction_timing": prediction_timing, "owner_expectation_timing": expectation_timing,
            "assessment": "requires_explicit_measurable_prediction" if result["stage"] == "available"
            else "awaiting_comparison", "observed_outcome": result["outcome"],
        })
    return result

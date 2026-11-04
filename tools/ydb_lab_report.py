"""Summarize bounded YDB lab JSONL and expose completed measurements locally."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

EXPECTED_YEARS = (1, 5, 10)
MIN_REPEATS = 3
WINDOW_SECONDS = (1, 60)
MAX_SCENARIO_LABELS = 128
METRICS = (
    "ru_per_operation", "estimated_ru", "cpu_seconds", "read_bytes", "update_bytes",
    "delete_rows", "operation_seconds", "errors", "queue_depth", "queue_age_seconds",
    "stats_complete", "stats_coverage_ratio", "cpu_complete", "io_complete",
)


def _read_jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                # Ignore only an unterminated tail record while the writer is active.
                if not line.endswith(("\n", "\r")):
                    continue
                raise ValueError(f"Malformed JSONL record at {path}:{index}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Expected an object at {path}:{index}")
            yield row


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _observation_complete(row: dict[str, Any]) -> bool:
    cache = row.get("_report_cache") or {}
    if "observation_complete" in cache:
        return cache["observation_complete"]
    observation = row.get("observation") or {}
    events = row.get("events") or []
    billable_missing = any(event.get("billed") is True and
                           _finite(event.get("measured_ru")) is None for event in events)
    ordinary_complete = observation.get("measurement_complete") is True
    all_billed_complete = observation.get(
        "all_billed_rpc_complete", observation.get("all_rpc_complete")) is True
    return (all_billed_complete and
            observation.get("ru_missing_rpc_count") == 0 and
            not billable_missing and ordinary_complete)


def _metric_total(row: dict[str, Any], source: str) -> float | None:
    cached = (row.get("_report_cache") or {}).get("metric_totals", {})
    if source in cached:
        return cached[source]
    observation = row.get("observation") or {}
    events = row.get("events") or []
    if source == "ru":
        if any(event.get("billed") is True and _finite(event.get("measured_ru")) is None
               for event in events):
            return None
        if observation.get("ru_missing_rpc_count", 0) > 0:
            return None
        value = observation.get("measured_ru")
        if value is None:
            values = [_finite(event.get("measured_ru")) for event in events
                      if event.get("billed") is True]
            if values:
                return sum(values) if all(v is not None for v in values) else None
            return 0.0 if observation.get("billed_rpc_count") == 0 else None
        return _finite(value)
    if source == "cpu":
        value = (observation.get("metrics") or {}).get("total_cpu_us")
        number = _finite(value)
        return None if number is None else number / 1_000_000
    if source == "read_bytes":
        return _finite((observation.get("metrics") or {}).get("read_bytes"))
    if source == "update_bytes":
        return _finite((observation.get("metrics") or {}).get("update_bytes"))
    if source == "delete_rows":
        return _finite((observation.get("metrics") or {}).get("delete_rows"))
    if source == "estimated_ru":
        return _finite(observation.get("estimated_ru"))
    return None


def _partial_known_ru(row: dict[str, Any]) -> float:
    cache = row.get("_report_cache") or {}
    if "partial_known_ru" in cache:
        return cache["partial_known_ru"]
    events = row.get("events") or []
    values = [_finite(event.get("measured_ru")) for event in events
              if event.get("billed") is True and _finite(event.get("measured_ru")) is not None]
    if values:
        return sum(values)
    observed = _finite((row.get("observation") or {}).get("measured_ru"))
    return observed or 0.0


def _accounting_reasons(row: dict[str, Any]) -> list[str]:
    cache = row.get("_report_cache") or {}
    if "accounting_reasons" in cache:
        return cache["accounting_reasons"]
    observation = row.get("observation") or {}
    events = row.get("events") or []
    reasons = {reason for event in events for reason in event.get("incomplete_reasons", [])
               if isinstance(reason, str)}
    if observation.get("ru_missing_rpc_count", 0) > 0 or any(
            event.get("billed") is True and _finite(event.get("measured_ru")) is None
            for event in events):
        reasons.add("missing_ru")
    if observation.get("measurement_complete") is not True:
        reasons.add("measurement_incomplete")
    if observation.get("all_billed_rpc_complete", observation.get("all_rpc_complete")) is not True:
        reasons.add("billable_rpc_incomplete")
    return sorted(reasons)


def _summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    first = rows[0]
    durations = [_finite(row.get("elapsed_seconds")) for row in rows]
    rus = [_metric_total(row, "ru") for row in rows]
    cpu = [_metric_total(row, "cpu") for row in rows]
    observations = [row.get("observation") or {} for row in rows]
    return {
        "variant": first["variant"], "years": first["years"],
        "suite": first["suite"], "scenario": first["scenario"], "phase": first["phase"],
        "repeat_count": len(rows),
        "duration_seconds": _spread(durations), "measured_ru": _spread(rus),
        "partial_known_ru": _spread([_partial_known_ru(row) for row in rows]),
        "estimated_ru": _spread([_finite((row.get("observation") or {}).get("estimated_ru"))
                                  for row in rows]),
        "cpu_seconds": _spread(cpu),
        "read_bytes": _spread([_metric_total(row, "read_bytes") for row in rows]),
        "update_bytes": _spread([_metric_total(row, "update_bytes") for row in rows]),
        "delete_rows": _spread([_metric_total(row, "delete_rows") for row in rows]),
        "complete_accounting": all(_observation_complete(row) for row in rows),
        "accounting_incomplete_reasons": sorted({reason for row in rows
                                                  for reason in _accounting_reasons(row)}),
        "stats_complete": bool(observations) and all(obs.get("stats_complete") is True for obs in observations),
        "stats_coverage_ratio": (sum(obs.get("stats_complete") is True for obs in observations) /
                                 len(observations)) if observations else None,
        "cpu_complete": bool(observations) and all(obs.get("cpu_complete") is True for obs in observations),
        "io_complete": bool(observations) and all(obs.get("io_complete") is True for obs in observations),
        "errors": sum(int((row.get("observation") or {}).get("failed_rpc_count", 0)) for row in rows),
        "queue_depth": _spread([_finite(row.get("queue_depth")) for row in rows]),
        "queue_age_seconds": _spread([_finite(row.get("queue_age_seconds")) for row in rows]),
    }


def _spread(values: list[float | None]) -> dict[str, float | None]:
    if not values or any(value is None for value in values):
        return {"median": None, "min": None, "max": None}
    concrete = [float(value) for value in values]
    return {"median": statistics.median(concrete), "min": min(concrete), "max": max(concrete)}


def _delta(left: Any, right: Any) -> float | None:
    first, second = _finite(left), _finite(right)
    return None if first is None or second is None else second - first


def _hash_parity(rows: list[dict[str, Any]], *, strict: bool) -> tuple[bool, list[str]]:
    problems = []
    operations: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    operation_counts: dict[tuple[Any, ...], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    source_hashes: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    validations: dict[tuple[Any, Any, Any], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    validation_hashes: dict[tuple[Any, Any], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        variant = row.get("variant")
        if variant not in {"baseline", "candidate"}:
            problems.append(f"unknown variant: {variant!r}")
            continue
        if row.get("kind") not in {"operation", "validation", "overhead"}:
            problems.append(f"unknown record kind: {row.get('kind')!r}")
            continue
        if row.get("years") not in EXPECTED_YEARS or not isinstance(row.get("repeat"), int) or row["repeat"] < 1:
            problems.append(f"invalid years/repeat for {row.get('kind')}")
            continue
        if row.get("kind") == "operation":
            if row.get("suite") not in {"gas", "application"}:
                problems.append(f"invalid operation suite: {row.get('suite')!r}")
                continue
            key = (row.get("suite"), row.get("years"), row.get("repeat"),
                   row.get("scenario"), row.get("phase"))
            operations[key].add(variant)
            operation_counts[key][variant] += 1
            source_sha = row.get("source_sha")
            source_key = (variant, row.get("suite"), row.get("years"))
            if not isinstance(source_sha, str) or len(source_sha) != 40 or any(
                    character not in "0123456789abcdef" for character in source_sha):
                problems.append(f"invalid source_sha for {source_key}")
            else:
                source_hashes[source_key].add(source_sha)
        elif row.get("kind") == "validation":
            if row.get("suite") not in {"gas", "application"}:
                problems.append(f"invalid validation suite: {row.get('suite')!r}")
            for field in ("facts_sha256", "artifacts_sha256", "ai_sha256"):
                value = row.get(field)
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    problems.append(f"invalid {field} for {(row.get('suite'), row.get('years'), row.get('repeat'))}")
                else:
                    validation_hashes[(row.get("suite"), row.get("years"))][field].add(value)
            validations[(row.get("suite"), row.get("years"), row.get("repeat"))][variant].append(row)
        elif row.get("kind") == "overhead" and row.get("suite") not in {"gas", "application"}:
            problems.append(f"invalid overhead suite: {row.get('suite')!r}")
    for key, variants in operations.items():
        if variants != {"baseline", "candidate"}:
            problems.append(f"missing operation variant for suite/years/repeat/scenario {key}")
        for variant, count in operation_counts[key].items():
            if count != 1:
                problems.append(f"duplicate operation record for {key}, variant={variant}")
    for key, values in source_hashes.items():
        if len(values) > 1:
            problems.append(f"source_sha changed across repeats for {key}")
    for key, pair in validations.items():
        if set(pair) != {"baseline", "candidate"}:
            problems.append(f"missing validation variant for suite/years/repeat {key}")
            continue
        for field in ("facts_sha256", "artifacts_sha256", "ai_sha256"):
            left = {str(row.get(field)) for row in pair["baseline"]}
            right = {str(row.get(field)) for row in pair["candidate"]}
            if left != right:
                problems.append(f"{field} mismatch for suite/years/repeat {key}")
    for key, fields in validation_hashes.items():
        for field, values in fields.items():
            if len(values) > 1:
                problems.append(f"{field} changed across variants or repeats for suite/years {key}")
    if problems and strict:
        raise ValueError("baseline/candidate parity failed: " + "; ".join(problems))
    return not problems, problems


def _summarize_overhead(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool, set[tuple[Any, ...]]]:
    collected: dict[tuple[Any, ...], dict[bool, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row.get("kind") != "overhead":
            continue
        observed = row.get("observed")
        if not isinstance(observed, bool):
            raise ValueError("overhead row must have a boolean observed field")
        key = (row.get("variant"), row.get("years"), row.get("suite"), row.get("repeat"))
        if observed in collected[key]:
            raise ValueError(f"duplicate overhead measurement for {key}, observed={observed}")
        collected[key][observed] = row
    grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for key, pair in collected.items():
        if set(pair) != {False, True}:
            continue
        baseline, measured = pair[False], pair[True]
        row = {"variant": key[0], "years": key[1], "suite": key[2], "repeat": key[3],
               "elapsed_delta": _delta(baseline.get("elapsed_seconds"), measured.get("elapsed_seconds")),
               "cpu_delta": _delta(baseline.get("cpu_seconds"), measured.get("cpu_seconds")),
               "unobserved_elapsed": _finite(baseline.get("elapsed_seconds")),
               "observed_elapsed": _finite(measured.get("elapsed_seconds")),
               "unobserved_cpu": _finite(baseline.get("cpu_seconds")),
               "observed_cpu": _finite(measured.get("cpu_seconds")),
               "queries": measured.get("queries")}
        if row["elapsed_delta"] is None or row["cpu_delta"] is None:
            continue
        grouped[key[:3]].append(row)
    output = []
    for key, pair_rows in sorted(grouped.items(), key=lambda item: tuple(map(str, item[0]))):
        output.append({"variant": key[0], "years": key[1], "suite": key[2],
                       "repeat_count": len(pair_rows),
                       "wall_overhead_seconds": _spread([row["elapsed_delta"] for row in pair_rows]),
                       "cpu_overhead_seconds": _spread([row["cpu_delta"] for row in pair_rows]),
                       "unobserved_wall_seconds": _spread([row["unobserved_elapsed"] for row in pair_rows]),
                       "observed_wall_seconds": _spread([row["observed_elapsed"] for row in pair_rows]),
                       "unobserved_cpu_seconds": _spread([row["unobserved_cpu"] for row in pair_rows]),
                       "observed_cpu_seconds": _spread([row["observed_cpu"] for row in pair_rows]),
                       "queries": pair_rows[0]["queries"]})
    complete = bool(collected) and all(
        set(pair) == {False, True} and
        all(_finite(pair[observed].get("elapsed_seconds")) is not None and
            _finite(pair[observed].get("cpu_seconds")) is not None and
            pair[observed].get("queries") == 5 for observed in (False, True))
        for pair in collected.values())
    return output, complete, set(collected)


def _operation_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repeats: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("kind") == "operation" and row.get("phase") == "operation":
            repeats[(row.get("variant"), row.get("years"), row.get("suite"),
                     row.get("repeat"))].append(row)
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for key, items in repeats.items():
        measured = [_metric_total(row, "ru") for row in items]
        estimated = [_finite((row.get("observation") or {}).get("estimated_ru")) for row in items]
        known = [_partial_known_ru(row) for row in items]
        reasons = sorted({reason for row in items for reason in _accounting_reasons(row)})
        grouped[key[:3]].append({
            "repeat": key[3],
            "measured_ru": sum(measured) if all(value is not None for value in measured) else None,
            "partial_known_ru": sum(known),
            "estimated_ru": sum(estimated) if all(value is not None for value in estimated) else None,
            "accounting_complete": all(_observation_complete(row) for row in items),
            "accounting_incomplete_reasons": reasons,
        })
    output = []
    for key, samples in sorted(grouped.items(), key=lambda item: tuple(map(str, item[0]))):
        output.append({
            "variant": key[0], "years": key[1], "suite": key[2],
            "repeat_count": len(samples),
            "measured_ru_total": _spread([sample["measured_ru"] for sample in samples]),
            "partial_known_ru_total": _spread([sample["partial_known_ru"] for sample in samples]),
            "estimated_ru_total": _spread([sample["estimated_ru"] for sample in samples]),
            "accounting_complete": all(sample["accounting_complete"] for sample in samples),
            "accounting_incomplete_reasons": sorted({reason for sample in samples
                                                       for reason in sample["accounting_incomplete_reasons"]}),
            "missing_ru_repeat_count": sum(sample["measured_ru"] is None for sample in samples),
        })
    return output


def _windows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # This module owns bucketing semantics; the observer owns exact RPC-to-window attribution.
    from tools.ydb_lab_observer import windows

    result = []
    for row in rows:
        if row.get("kind") != "operation":
            continue
        cached = (row.get("_report_cache") or {}).get("windows")
        if cached is not None:
            result.extend(cached)
            continue
        for width in WINDOW_SECONDS:
            for window in windows(row.get("events") or [], row.get("elapsed_seconds", 0), width):
                result.append({"variant": row["variant"], "suite": row["suite"],
                               "years": row["years"],
                               "repeat": row["repeat"], "scenario": row["scenario"],
                               "phase": row["phase"], "width_seconds": width, **window})
    return result


def compact_record(row: dict[str, Any]) -> dict[str, Any]:
    """Prepare one JSONL record for bounded reporting and drop per-RPC payloads."""
    compact = dict(row)
    if row.get("kind") != "operation":
        return compact
    if "_report_cache" in row:
        compact.pop("events", None)
        return compact

    events = row.get("events") or []
    observation = row.get("observation") or {}
    billed = [event for event in events if event.get("billed") is True]
    stats_present = sum(event.get("stats_present") is True for event in billed)
    rpc_coverage = {
        "rpc_count": int(observation.get("rpc_count", len(events))),
        "billed_rpc_count": int(observation.get("billed_rpc_count", len(billed))),
        "control_rpc_count": int(observation.get("control_rpc_count", len(events) - len(billed))),
        "ru_present_rpc_count": int(observation.get("ru_present_rpc_count", 0)),
        "ru_missing_rpc_count": int(observation.get("ru_missing_rpc_count", 0)),
        "stats_present_rpc_count": stats_present,
        "stats_missing_rpc_count": len(billed) - stats_present,
        "cpu_complete_rpc_count": sum(event.get("cpu_complete") is True for event in billed),
        "io_complete_rpc_count": sum(event.get("io_complete") is True for event in billed),
        "failed_rpc_count": int(observation.get("failed_rpc_count", 0)),
        "all_billed_rpc_complete": observation.get(
            "all_billed_rpc_complete", observation.get("all_rpc_complete")) is True,
        "measurement_complete": observation.get("measurement_complete") is True,
        "rpc_counts": dict(observation.get("rpc_counts") or {
            name: sum(event.get("rpc") == name for event in events)
            for name in {event.get("rpc") for event in events if isinstance(event.get("rpc"), str)}
        }),
    }
    cache = {
        "observation_complete": _observation_complete(row),
        "metric_totals": {source: _metric_total(row, source)
                          for source in ("ru", "cpu", "read_bytes", "update_bytes",
                                         "delete_rows", "estimated_ru")},
        "partial_known_ru": _partial_known_ru(row),
        "accounting_reasons": _accounting_reasons(row),
        "stats_complete": observation.get("stats_complete") is True,
        "cpu_complete": observation.get("cpu_complete") is True,
        "io_complete": observation.get("io_complete") is True,
        "stats_coverage_count": [int(observation.get("stats_complete") is True), 1],
        "rpc_coverage": rpc_coverage,
        "windows": _windows([row]),
    }
    compact["_report_cache"] = cache
    compact.pop("events", None)
    return compact


def summarize_output(output: Path, *, write: bool = True, strict: bool = True) -> dict[str, Any]:
    output = Path(output)
    compact_path = output / "measurements.jsonl"
    input_path = compact_path if compact_path.exists() else output / "results.jsonl"
    rows = [compact_record(row) for row in _read_jsonl(input_path)]
    operations = [row for row in rows if row.get("kind") == "operation"]
    if any(row.get("phase") not in {"setup", "operation", "observer"} for row in operations):
        raise ValueError("operation record has an unsupported phase")
    parity_ok, parity_problems = _hash_parity(rows, strict=strict)
    overhead_summaries, overhead_pairs_complete, overhead_keys = _summarize_overhead(rows)
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in operations:
        grouped[(row.get("variant"), row.get("suite"), row.get("years"),
                 row.get("scenario"), row.get("phase"))].append(row)
    operation_summaries = [
        _summarize_group(group)
        for _, group in sorted(grouped.items(), key=lambda item: tuple(map(str, item[0])))
    ]
    operation_totals = _operation_totals(rows)
    window_rows = _windows(operations)
    rpc_coverage_counts = {
        "rpc_count": 0, "billed_rpc_count": 0, "control_rpc_count": 0,
        "ru_present_rpc_count": 0, "ru_missing_rpc_count": 0,
        "stats_present_rpc_count": 0, "stats_missing_rpc_count": 0,
        "cpu_complete_rpc_count": 0, "io_complete_rpc_count": 0,
        "failed_rpc_count": 0,
    }
    rpc_counts: dict[str, int] = defaultdict(int)
    for row in operations:
        cached_coverage = (row.get("_report_cache") or {}).get("rpc_coverage", {})
        for key in rpc_coverage_counts:
            rpc_coverage_counts[key] += int(cached_coverage.get(key, 0))
        for name, count in cached_coverage.get("rpc_counts", {}).items():
            rpc_counts[name] += int(count)
    rpc_coverage = {
        **rpc_coverage_counts,
        "rpc_counts": dict(sorted(rpc_counts.items())),
        "all_billed_rpc_complete": bool(operations) and all(
            (row.get("_report_cache") or {}).get("rpc_coverage", {}).get(
                "all_billed_rpc_complete") is True for row in operations),
        "measurement_complete": bool(operations) and all(
            (row.get("_report_cache") or {}).get("rpc_coverage", {}).get(
                "measurement_complete") is True for row in operations),
    }
    years_seen = {row.get("years") for row in operations}
    scenario_names = sorted({row.get("scenario") for row in operations
                             if isinstance(row.get("scenario"), str)})
    scenario_labels_truncated = len(scenario_names) > MAX_SCENARIO_LABELS
    repeat_counts = {year: min((len({r.get("repeat") for r in operations
                                     if r.get("variant") == variant and r.get("years") == year})
                                for variant in ("baseline", "candidate")), default=0)
                     for year in years_seen}
    accounting_complete = bool(operations) and all(_observation_complete(row) for row in operations)
    operation_groups = [group for group in operation_summaries if group["phase"] == "operation"]
    enough_repeats = bool(operation_groups) and all(
        group["repeat_count"] >= MIN_REPEATS for group in operation_groups)
    enough_repeats = enough_repeats and all(repeat_counts.get(year, 0) >= MIN_REPEATS
                                             for year in EXPECTED_YEARS)
    validation_suites = {row.get("suite") for row in rows if row.get("kind") == "validation"}
    suites_complete = validation_suites == {"gas", "application"}
    validation_years = {row.get("years") for row in rows if row.get("kind") == "validation"}
    validation_repeat_counts = {suite: min((len({row.get("repeat") for row in rows
                                                  if row.get("kind") == "validation" and
                                                  row.get("suite") == suite and row.get("years") == year and
                                                  row.get("variant") == variant})
                                             for variant in ("baseline", "candidate")
                                             for year in EXPECTED_YEARS), default=0)
                               for suite in ("gas", "application")}
    operation_pairs = {(row.get("suite"), row.get("years"), row.get("repeat")) for row in operations}
    validation_pairs = {(row.get("years"), row.get("repeat"), row.get("suite"), row.get("variant"))
                        for row in rows if row.get("kind") == "validation"}
    validation_coverage_complete = all(
        (year, repeat, suite, variant) in validation_pairs
        for suite, year, repeat in operation_pairs
        for variant in ("baseline", "candidate"))
    operation_run_keys = {(row.get("variant"), row.get("years"), row.get("suite"), row.get("repeat"))
                          for row in operations}
    overhead_coverage_complete = all(
        (variant, year, "application", repeat) in overhead_keys
        for variant, year, suite, repeat in operation_run_keys if suite == "application")
    overhead_complete = overhead_pairs_complete and overhead_coverage_complete
    years_complete = years_seen == set(EXPECTED_YEARS) and validation_years == set(EXPECTED_YEARS)
    validation_repeats_complete = all(count >= MIN_REPEATS for count in validation_repeat_counts.values())
    validation_passed = (parity_ok and years_complete and suites_complete and enough_repeats and
                         validation_repeats_complete and validation_coverage_complete and overhead_complete)
    validation_reasons = []
    if not parity_ok:
        validation_reasons.extend(parity_problems)
    if not years_complete:
        validation_reasons.append("incomplete_year_matrix")
    if not suites_complete:
        validation_reasons.append("incomplete_suite_matrix")
    if not enough_repeats or not validation_repeats_complete:
        validation_reasons.append("insufficient_repeats")
    if not validation_coverage_complete:
        validation_reasons.append("incomplete_validation_coverage")
    if not overhead_complete:
        validation_reasons.append("incomplete_overhead_pairs")
    accounting_reasons = sorted({reason for row in operations for reason in _accounting_reasons(row)})
    summary = {"validation_passed": validation_passed,
               "validation_reasons": validation_reasons,
               "validation_requirements": {"years": list(EXPECTED_YEARS),
                                           "minimum_repeats": MIN_REPEATS,
                                           "paired_scenario_coverage": True,
                                           "exact_validation_hashes": True,
                                           "paired_application_overhead": True},
               "years_seen": sorted(years_seen, key=str), "repeat_counts": repeat_counts,
               "validation_suites": sorted(str(suite) for suite in validation_suites),
               "validation_repeat_counts": validation_repeat_counts,
               "validation_coverage_complete": validation_coverage_complete,
               "overhead_complete": overhead_complete,
               "overhead": overhead_summaries,
               "scenario_label_count": min(len(scenario_names), MAX_SCENARIO_LABELS),
               "scenario_labels_truncated": scenario_labels_truncated,
               "accounting_complete": accounting_complete,
               "accounting_incomplete_reasons": accounting_reasons,
               "rpc_coverage": rpc_coverage,
               "hash_parity": parity_ok,
               "hash_parity_problems": parity_problems,
               "operations": operation_summaries, "operation_totals": operation_totals,
               "windows": window_rows}
    if write:
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _write_csv(output / "operations.csv", operation_summaries)
        _write_csv(output / "windows.csv", window_rows)
    return summary


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    flattened = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if isinstance(value, dict):
                item.update({f"{key}_{child}": nested for child, nested in value.items()})
            else:
                item[key] = value
        flattened.append(item)
    fields = list(dict.fromkeys(key for row in flattened for key in row))
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flattened)


def _metric_lines(summary: dict[str, Any]) -> str:
    # Prometheus labels are constrained to small controlled domains. Do not expose IDs or hashes.
    groups: dict[tuple[str, str, str, str], dict[str, float | None]] = {}
    latest: dict[tuple[str, str, str, str], int] = {}
    scenario_labels = sorted({row.get("scenario") for row in [*summary.get("windows", []),
                                                                *summary.get("operations", [])]
                              if isinstance(row.get("scenario"), str) and len(row["scenario"]) <= 32 and
                              row["scenario"].replace("_", "").isalnum()})[:MAX_SCENARIO_LABELS]
    coverage_counts: dict[tuple[str, str, str, str, str], list[int]] = {}
    for row in summary.get("windows", []):
        if row.get("phase") != "operation":
            continue
        variant, suite = row.get("variant"), row.get("suite")
        years, scenario = str(row.get("years")), row.get("scenario")
        if (variant not in {"baseline", "candidate"} or
                suite not in {"gas", "application"} or years not in {"1", "5", "10"}):
            continue
        if scenario not in scenario_labels:
            continue
        key = (variant, years, suite, scenario)
        repeat = row.get("repeat", 0)
        if repeat < latest.get(key, -1):
            continue
        if repeat > latest.get(key, -1):
            latest[key] = repeat
            groups[key] = {}
            for width in ("1", "60"):
                coverage_counts[(*key, width)] = [0, 0]
        entry = groups.setdefault(key, {})
        width = row.get("width_seconds")
        if width is None and row.get("start_seconds") is not None and row.get("end_seconds") is not None:
            width = row["end_seconds"] - row["start_seconds"]
        ru = _finite(row.get("measured_ru"))
        if width in (1.0, 60.0):
            window = str(int(width))
            metric_key = f"ru_rate_{window}"
            start, end = _finite(row.get("start_seconds")), _finite(row.get("end_seconds"))
            duration = None if start is None or end is None else end - start
            if ru is not None and duration is not None and duration > 0:
                rate = ru / duration
                entry[metric_key] = max(entry.get(metric_key, float("-inf")), rate)
                entry[f"{metric_key}_sum"] = entry.get(f"{metric_key}_sum", 0.0) + rate
                entry[f"{metric_key}_count"] = entry.get(f"{metric_key}_count", 0) + 1
            rpc_count = _finite(row.get("rpc_count"))
            if rpc_count is not None and duration is not None and duration > 0:
                rpc_rate = rpc_count / duration
                entry[f"rpc_rate_{window}_sum"] = entry.get(f"rpc_rate_{window}_sum", 0.0) + rpc_rate
                entry[f"rpc_rate_{window}_count"] = entry.get(f"rpc_rate_{window}_count", 0) + 1
            present = row.get("ru_present_rpc_count", 0)
            missing = row.get("ru_missing_rpc_count", 0)
            if isinstance(present, int) and isinstance(missing, int):
                counts = coverage_counts.setdefault((*key, window), [0, 0])
                counts[0] += present
                counts[1] += missing
    for row in summary.get("operations", []):
        if row.get("phase") != "operation":
            continue
        variant, suite = row.get("variant"), row.get("suite")
        years, scenario = str(row.get("years")), row.get("scenario")
        if (variant not in {"baseline", "candidate"} or
                suite not in {"gas", "application"} or years not in {"1", "5", "10"}):
            continue
        if scenario not in scenario_labels:
            continue
        values = {"ru_per_operation": row.get("measured_ru", {}).get("median"),
                  "estimated_ru": row.get("estimated_ru", {}).get("median"),
                  "cpu_seconds": row.get("cpu_seconds", {}).get("median"),
                  "read_bytes": row.get("read_bytes", {}).get("median"),
                  "update_bytes": row.get("update_bytes", {}).get("median"),
                  "delete_rows": row.get("delete_rows", {}).get("median"),
                  "operation_seconds": row.get("duration_seconds", {}).get("median"),
                  "errors": row.get("errors"), "queue_depth": row.get("queue_depth", {}).get("median"),
                  "queue_age_seconds": row.get("queue_age_seconds", {}).get("median"),
                  "stats_complete": int(row.get("stats_complete") is True),
                  "stats_coverage_ratio": row.get("stats_coverage_ratio"),
                  "cpu_complete": int(row.get("cpu_complete") is True),
                  "io_complete": int(row.get("io_complete") is True)}
        key = (variant, years, suite, scenario)
        groups.setdefault(key, {}).update({k: _finite(v) for k, v in values.items()})
    lines = []
    for metric in METRICS:
        name = "zont_lab_" + metric
        for (variant, years, suite, scenario), values in sorted(groups.items()):
            value = values.get(metric)
            if value is None:
                continue
            lines.append(
                f'{name}{{variant="{variant}",years="{years}",suite="{suite}",'
                f'scenario="{scenario}"}} {value:g}')
    for window in ("1", "60"):
        for (variant, years, suite, scenario), values in sorted(groups.items()):
            value = values.get(f"ru_rate_{window}")
            if value is not None:
                lines.append(
                    f'zont_lab_ru_per_second{{variant="{variant}",years="{years}",suite="{suite}",'
                    f'scenario="{scenario}",window="{window}"}} {value:g}')
            count = values.get(f"ru_rate_{window}_count", 0)
            total = values.get(f"ru_rate_{window}_sum", 0.0)
            if count:
                mean = total / count
                lines.append(
                    f'zont_lab_ru_per_second_mean{{variant="{variant}",years="{years}",suite="{suite}",'
                    f'scenario="{scenario}",window="{window}"}} {mean:g}')
            rpc_count = values.get(f"rpc_rate_{window}_count", 0)
            rpc_sum = values.get(f"rpc_rate_{window}_sum", 0.0)
            if rpc_count:
                rpc_mean = rpc_sum / rpc_count
                lines.append(
                    f'zont_lab_rpc_per_second{{variant="{variant}",years="{years}",suite="{suite}",'
                    f'scenario="{scenario}",window="{window}"}} {rpc_mean:g}')
    for (variant, years, suite, scenario, window), (present, missing) in sorted(coverage_counts.items()):
        if present + missing:
            coverage = present / (present + missing)
            lines.append(
                f'zont_lab_ru_coverage_ratio{{variant="{variant}",years="{years}",suite="{suite}",'
                f'scenario="{scenario}",window="{window}"}} {coverage:g}')
    for row in summary.get("operation_totals", []):
        labels = f'variant="{row["variant"]}",years="{row["years"]}",suite="{row["suite"]}"'
        measured = row.get("measured_ru_total", {}).get("median")
        partial = row.get("partial_known_ru_total", {}).get("median")
        estimate = row.get("estimated_ru_total", {}).get("median")
        for name, value in (("ru_total", measured), ("ru_partial_known_total", partial),
                            ("estimated_ru_total", estimate)):
            if value is not None:
                lines.append(f"zont_lab_{name}{{{labels}}} {value:g}")
    for row in summary.get("overhead", []):
        labels = f'variant="{row["variant"]}",years="{row["years"]}",suite="{row["suite"]}"'
        for name, key in (("observer_overhead_seconds", "wall_overhead_seconds"),
                          ("observer_overhead_cpu_seconds", "cpu_overhead_seconds")):
            value = row.get(key, {}).get("median")
            if value is not None:
                lines.append(f"zont_lab_{name}{{{labels}}} {value:g}")
    if summary.get("scenario_labels_truncated"):
        lines.append("zont_lab_scenario_labels_truncated 1")
    return "\n".join(lines) + ("\n" if lines else "")


def _memoized_report(output: Path):
    output = Path(output)
    state: dict[str, Any] = {"signature": None, "summary": None, "metrics": None}

    def load():
        compact_path = output / "measurements.jsonl"
        input_path = compact_path if compact_path.exists() else output / "results.jsonl"
        metadata = input_path.stat()
        signature = (str(input_path), metadata.st_mtime_ns, metadata.st_size)
        if signature != state["signature"]:
            summary = summarize_output(output, write=False, strict=False)
            state.update(signature=signature, summary=summary, metrics=_metric_lines(summary))
        return state["summary"], state["metrics"]

    return load


def serve(output: Path, port: int = 8765) -> None:
    output = Path(output)
    load_report = _memoized_report(output)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                summary, metrics = load_report()
            except (OSError, ValueError, ImportError) as exc:
                self.send_error(503, str(exc))
                return
            if self.path == "/metrics":
                body = metrics.encode()
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            elif self.path == "/report":
                body = json.dumps(summary, sort_keys=True).encode()
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.serve:
        serve(args.output, args.port)
    else:
        result = summarize_output(args.output)
        print(json.dumps({"validation_passed": result["validation_passed"],
                          "accounting_complete": result["accounting_complete"],
                          "summary": str(args.output / "summary.json")}))


if __name__ == "__main__":
    main()

"""Offline synthetic full-source/daily-fact benchmark; never calls an AI or YDB.

Run in the repository test image: ``python -m tools.benchmark_regulation``.
Only bounded examples reach analysis_packet; every run contributes to metrics.
JSON sizes are logical UTF-8 payload sizes, not physical database storage or RU.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import timedelta
from typing import Any

from tests.regulation_acceptance_cases import (
    START,
    TRIANGLE_ORACLE,
    daily_facts,
    late_peak,
    slice_inputs,
    triangle_inputs,
)
from zont_analyzer.adapters.openai.provider import ANALYSIS_PACKET_MAX_BYTES, analysis_packet
from zont_analyzer.analytics.regulation import aggregate_regulation_facts, build_regulation_facts


def size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def measured(function):
    cpu, wall = time.process_time(), time.perf_counter()
    result = function()
    return result, {"cpu_seconds": time.process_time() - cpu, "wall_seconds": time.perf_counter() - wall}


def assert_agreement(compact: dict[str, Any], detailed: dict[str, Any]) -> float:
    assert compact["cycle_counts"] == detailed["cycle_counts"], "cycle census differs"
    for key in ("eligible_seconds", "observed_seconds", "fact_seconds"):
        assert compact["coverage"][key] == detailed["coverage"][key], key
    errors = []
    for name, metric in detailed["metrics"].items():
        other = compact["metrics"][name]
        assert math.isclose(metric["denominator"], other["denominator"], abs_tol=1e-8), name
        if metric["value"] is None:
            assert other["value"] is None, name
        else:
            error = abs(metric["value"] - other["value"])
            errors.append(error)
            assert math.isclose(metric["value"], other["value"], rel_tol=1e-9, abs_tol=1e-8), name
    return max(errors, default=0)


def packet_for(facts: dict[str, Any], days: int) -> dict[str, Any]:
    return analysis_packet(quality={"score": 1}, metrics=[], events=[],
                           period={"kind": "daily" if days == 1 else "weekly" if days == 7 else "seasonal",
                                   "start": START.isoformat(), "end": (START + timedelta(days=days)).isoformat()},
                           context={"regulation_facts": {"circuits": [facts], "incomplete": False}})


def benchmark(days: int) -> dict[str, Any]:
    data, generation_time = measured(lambda: triangle_inputs(days))
    end = START + timedelta(days=days)
    detailed, detailed_time = measured(lambda: build_regulation_facts(start=START, end=end, inputs=data))
    daily, daily_time = measured(lambda: daily_facts(data, days))
    compact, compact_time = measured(lambda: aggregate_regulation_facts(daily, start=START, end=end,
                                                                       expected_days=days))
    repeated, repeat_time = measured(lambda: aggregate_regulation_facts(daily + daily, start=START, end=end,
                                                                        expected_days=days))
    assert repeated == compact, "repeated fact input changed the census"
    max_error = assert_agreement(compact, detailed)
    assert compact["cycle_counts"]["full"] == days * 8 - 1
    assert compact["cycle_counts"]["partial"] == 1
    assert compact["cycle_examples_total"] == days * 8
    for name, expected in TRIANGLE_ORACLE.items():
        assert math.isclose(compact["metrics"][name]["value"], expected, abs_tol=1e-8), name
    packet, packet_time = measured(lambda: packet_for(compact, days))
    packet_bytes = size(packet)
    assert packet_bytes <= ANALYSIS_PACKET_MAX_BYTES
    conveyed = packet["control_context"]["regulation_facts"]["circuits"][0]
    assert conveyed["cycle_counts"] == compact["cycle_counts"]
    assert conveyed["cycle_examples_total"] == days * 8
    assert conveyed["coverage"]["eligible_seconds"] == days * 86400
    assert "runs" not in conveyed
    for name, expected in TRIANGLE_ORACLE.items():
        assert math.isclose(conveyed["metrics"][name]["value"], expected, abs_tol=1e-8), name
    source = {role: {"value_kind": series.metadata.value_kind,
                     "samples": [(sample.timestamp.isoformat(), sample.value) for sample in series.samples]}
              for role, series in data.signals.items()}
    result: dict[str, Any] = {
        "days": days, "source_sample_count": sum(len(series.samples) for series in data.signals.values()),
        "daily_facts": len(daily), "retained_runs": sum(len(fact["runs"]) for fact in daily),
        "visible_request_episodes": compact["cycle_examples_total"], "cycle_counts": compact["cycle_counts"],
        "display_cycle_examples": len(compact["cycles"]),
        "eligible_seconds": compact["coverage"]["eligible_seconds"],
        "independent_triangle_oracle_agrees": True,
        "full_source_daily_max_metric_difference": max_error,
        "all_metric_denominators_and_cycle_counts_agree": True,
        "ai_packet_preserves_independent_metrics_and_complete_census": True,
        "identical_repeat_agrees": True,
        "logical_utf8_bytes": {"source_series": size(source), "persisted_daily_facts": size(daily),
                               "aggregate_facts": size(compact), "ai_packet": packet_bytes},
        "timing": {"fixture_generation": generation_time, "full_source_calculation": detailed_time,
                   "daily_fact_calculation": daily_time, "aggregate_cached_facts": compact_time,
                   "repeat_aggregate_with_duplicate_inputs": repeat_time, "ai_packet_assembly": packet_time},
        "database_requests": 0, "database_ru": None, "external_ai_calls": 0,
    }
    if days > 35:
        changed_day = 35
        late = late_peak(data, day=changed_day)
        left, right = START + timedelta(days=changed_day), START + timedelta(days=changed_day + 1)
        replacement, late_day_time = measured(lambda: build_regulation_facts(
            start=left, end=right, inputs=slice_inputs(late, left, right),
            previous_boundary=daily[changed_day - 1]["boundary"]))
        updated = daily.copy()
        updated[changed_day] = replacement
        revised, late_aggregate_time = measured(lambda: aggregate_regulation_facts(updated, start=START, end=end,
                                                                                  expected_days=days))
        oracle, late_oracle_time = measured(lambda: build_regulation_facts(start=START, end=end, inputs=late))
        late_error = assert_agreement(revised, oracle)
        assert math.isclose(revised["metrics"]["room_signed_error_c"]["value"],
                            .1 + 30 / (days * 86400), abs_tol=1e-8)
        assert math.isclose(revised["metrics"]["cycle_mean_overshoot_c"]["value"],
                            .8 + .1 / (days * 8 - 1), abs_tol=1e-8)
        assert replacement["boundary"] == daily[changed_day]["boundary"]
        assert packet_for(revised, days) != packet
        assert aggregate_regulation_facts(updated + updated, start=START, end=end, expected_days=days) == revised
        result["late_input"] = {
            "changed_day_index": changed_day, "recomputed_days": 1, "boundary_converged": True,
            "max_metric_difference_from_full_source": late_error,
            "independent_integral_and_peak_oracle_agrees": True, "ai_packet_changed": True,
            "repeat_has_no_duplicates": True,
            "timing": {"changed_day_calculation": late_day_time, "aggregate": late_aggregate_time,
                       "full_source_oracle": late_oracle_time},
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", nargs="+", type=int, default=[1, 7, 60])
    args = parser.parse_args()
    if any(day < 1 or day > 90 for day in args.days):
        parser.error("--days must be between 1 and 90 for this bounded local benchmark")
    result = {"schema_version": 1, "data_kind": "synthetic", "network_required": False,
              "scope": "offline numerical correctness, CPU and logical payload size; no release/runtime or RU claim",
              "scenarios": [benchmark(days) for days in args.days]}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

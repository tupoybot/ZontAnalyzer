import json
from copy import deepcopy

from zont_analyzer.adapters.openai.provider import ANALYSIS_PACKET_MAX_BYTES, analysis_packet


def packet(context):
    return analysis_packet(quality={"score": 1}, metrics=[], events=[],
                           period={"kind": "seasonal", "start": "2026-01-01", "end": "2026-04-01"},
                           context=context)


def circuit(number=0):
    return {"scope": f"fixture/{number}", "classification": "cycling", "cycle_counts": {"full": 719, "partial": 1},
            "coverage": {"eligible_seconds": 60 * 86400}, "incomplete": False,
            "metrics": {"room_absolute_error_c": {"value": 0.23, "unit": "°C", "coverage_pct": 99,
                                                   "denominator": 60 * 86400},
                        "actual_variation_c_per_hour": {"value": 12.0, "unit": "°C/h", "coverage_pct": 98}},
            "cycles": [{"started_at": str(i), "overshoot_c": 0.7} for i in range(8)],
            "exclusion_windows": [{"started_at": str(i), "reason": "abrupt_room_cooling", "recovered": True,
                                   "ended_at": str(i + 1)} for i in range(12)]}


def test_complete_census_survives_bounded_examples_without_mutating_source():
    context = {"regulation_facts": {"circuits": [circuit(i) for i in range(40)], "incomplete": False}}
    old = deepcopy(context)
    result = packet(context)
    facts = result["control_context"]["regulation_facts"]
    assert facts["packet_incomplete"] and facts["total_circuits"] == 40
    selected = facts["circuits"][0]
    assert selected["cycle_counts"] == {"full": 719, "partial": 1}
    assert selected["coverage"]["eligible_seconds"] == 60 * 86400
    assert selected["metrics"]["room_absolute_error_c"]["value"] == 0.23
    assert selected["cycles_total"] == 8 and len(selected["cycles"]) == 2
    assert context == old
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= ANALYSIS_PACKET_MAX_BYTES


def test_cache_bookkeeping_is_stable_but_omitted_late_facts_change_packet():
    context = {"regulation_facts": {"circuits": [circuit(i) for i in range(40)], "cache": {"built_days": 60}}}
    original = packet(context)
    context["regulation_facts"]["cache"] = {"hit_days": 60}
    assert packet(context) == original
    context["regulation_facts"]["circuits"][-1]["metrics"]["room_absolute_error_c"]["value"] = 0.93
    changed = packet(context)
    assert changed["control_context"]["regulation_facts"]["full_facts_fingerprint"] != (
        original["control_context"]["regulation_facts"]["full_facts_fingerprint"])


def test_latest_change_and_mixed_outcome_remain_when_history_is_large():
    versions = [{"event_id": str(i), "observed_at": i, "state": {"details": "x" * 2000},
                 "boundary": {"after": i - 1, "no_later_than": i, "exact_effective_at": None}}
                for i in range(100)]
    comparison = {"event_id": "latest", "stage": "available", "outcome": "mixed",
                  "event": {"changed_values": {"pid_prop_koef": {"before": 1, "after": 2}}},
                  "metrics": {"room_absolute_error_c": {"before": 0.4, "after": 0.2},
                              "actual_variation_c_per_hour": {"before": 5, "after": 10}},
                  "source_facts": [{"fingerprint": str(i), "start": str(i)} for i in range(60)],
                  "limitations": ["joint_parameter_change; individual_parameter_effects_not_identifiable"]}
    context = {"control_settings_history": {"versions": versions},
               "regulation_comparisons": {"results": [comparison], "history_incomplete": False}}
    result = packet(context)["control_context"]
    assert result["control_settings_history"]["versions"][-1]["event_id"] == "99"
    assert result["control_settings_history"]["packet_history_incomplete"]
    compared = result["regulation_comparisons"]["results"][0]
    assert compared["outcome"] == "mixed" and compared["stage"] == "available"
    assert compared["source_facts_total"] == 60
    assert compared["metrics"] == comparison["metrics"]

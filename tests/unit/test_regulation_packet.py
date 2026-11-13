import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.adapters.openai.provider import ANALYSIS_PACKET_MAX_BYTES, analysis_packet
from zont_analyzer.analytics.regulation import ALGORITHM_VERSION


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
    assert selected["cycles_total"] == 720 and len(selected["cycles"]) == 2
    assert selected["cycles_input_examples_count"] == 8
    assert selected["cycles_display_examples_count"] == 2
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
    for name, values in comparison["metrics"].items():
        assert all(compared["metrics"][name][field] == value for field, value in values.items())


def realistic_comparison(*, sparse_last_condition=False, outdoor_temperature_c=None):
    """Use the real comparison producer, including its largest normal sidecars."""
    from zont_analyzer.analytics.regulation_comparison import compare_change

    start = datetime(2026, 1, 1, tzinfo=UTC)
    change = start + timedelta(hours=192)
    after = change + timedelta(hours=1)
    end = after + timedelta(hours=192)
    old = {"fields": {"pid_prop_koef": {"status": "observed", "value": 1}}}
    new = {"fields": {"pid_prop_koef": {"status": "observed", "value": 2}}}
    original = {"goal": "Сохранить ровную температуру комнаты. " * 24,
                "expected_effect": "Ожидается уменьшение выбега при сохранении комфорта. " * 24,
                "observation_window": "Две недели сходной погоды и несколько полных циклов.",
                "generated_at": (start - timedelta(days=1)).isoformat(),
                "predictions": [{"scenario": "После изменения коэффициента при той же погоде.",
                                 "expected_effect": "Меньше выбег комнаты; проверить фактическую подачу. " * 16,
                                 "assumptions": ["Сходная погода и режимы."] * 8,
                                 "verification_plan": "Сравнить покрытые интервалы комнаты и подачи. " * 16}
                                for _ in range(4)]}
    event = {"event_id": "latest-real", "before_version": "old", "after_version": "new",
             "before_state": old, "after_state": new, "changed_fields": ["pid_prop_koef"],
             "boundary": {"after": change.isoformat(), "no_later_than": after.isoformat(),
                          "exact_effective_at": None}, "sources": ["observed_configuration"],
             "manual_records": [{"intervention_id": f"owner-{index}", "recorded_at": start.isoformat(),
                                 "experiment": {"original": deepcopy(original)}} for index in range(4)]}

    def stat(value, variation=0):
        return {"seconds": 3600, "integral": value * 3600,
                "positive_integral": max(value, 0) * 3600, "negative_integral": max(-value, 0) * 3600,
                "minimum": value, "maximum": value, "first": value, "last": value,
                "variation": variation, "max_rate_per_hour": variation}

    runs = []
    for origin, version, error, variation in ((start, "old", .4, 2), (after, "new", .2, 4)):
        for index in range(192):
            active = bool(index % 2)
            runs.append({"start": (origin + timedelta(hours=index)).isoformat(),
                         "end": (origin + timedelta(hours=index + 1)).isoformat(), "seconds": 3600,
                         "mode": "maintenance", "control_mode": 1, "target": 21,
                         "settings_version": version, "request_semantics": "preceding_minute",
                         "request": active, "request_active_seconds": 1800 if active else 0,
                         "request_observed_seconds": 3600, "request_edge_uncertainty_seconds": 60,
                         "eligible_seconds": 3600, "reasons": [],
                         "stats": {"error": stat(error), "actual": stat(40, variation),
                                   "command": stat(42, variation), "outdoor": stat(
                                       outdoor_temperature_c if outdoor_temperature_c is not None
                                       else -11 + (index // 16) * 2)}})
    if sparse_last_condition:
        for run in runs[178:192]:
            run["stats"].pop("actual")
    fact = {"period_start": start.isoformat(), "period_end": end.isoformat(),
            "algorithm_version": ALGORITHM_VERSION, "scope": "fixture/1", "runs": runs}
    return compare_change(event, events=[event], facts=[fact], start=start, end=end)


def test_real_comparison_with_coverage_episodes_and_frozen_forecasts_keeps_newest_core():
    newest = realistic_comparison()
    assert newest["stage"] == "available" and newest["outcome"] == "mixed"
    assert len(newest["before"]["condition_metric_coverage"]) == 12
    assert len(newest["before"]["request_episode_examples"]) == 4
    assert len(json.dumps(newest, ensure_ascii=False).encode()) > 12 * 1024
    older = deepcopy(newest)
    older["event_id"] = older["event"]["event_id"] = "older"
    older["event"]["boundary"]["no_later_than"] = "2025-12-01T00:00:00+00:00"
    context = {"regulation_comparisons": {"results": [newest, older], "total_changes_in_history": 2},
               "regulation_facts": {"circuits": [circuit()], "incomplete": False}}
    original = deepcopy(context)
    result = packet(context)
    block = result["control_context"]["regulation_comparisons"]
    assert block["results"] and block["results"][0]["event_id"] == "latest-real"
    retained = block["results"][0]
    assert retained["stage"] == "available" and retained["outcome"] == "mixed"
    for name, values in newest["metrics"].items():
        assert all(retained["metrics"][name][field] == value for field, value in values.items())
    assert retained["event"]["changed_values"] == newest["event"]["changed_values"]
    for side in ("before", "after"):
        assert retained[side]["metric_coverage"] == newest[side]["metric_coverage"]
        assert retained[side]["complete_request_episodes"] == newest[side]["complete_request_episodes"]
        assert retained[side]["request_activity_observed_seconds"] == 192 * 3600
        assert retained[side]["minute_bin_request_episodes"] == newest[side]["minute_bin_request_episodes"]
        assert retained[side]["condition_metric_coverage_total"] == 12
        assert retained[side]["condition_metric_coverage_minimum"] == {"error": 1, "actual": 1, "command": 1}
        assert retained[side]["settings_versions"] == newest[side]["settings_versions"]
    assert retained["forecast_count"] == 4
    assert retained["forecast_assessments_total"] == 4
    assert block["total_results"] == 2
    assert len(json.dumps(block, ensure_ascii=False).encode()) <= 12 * 1024
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= ANALYSIS_PACKET_MAX_BYTES
    assert result["provenance"]["truncation"]["omitted"]
    assert context == original


def test_omitted_condition_retains_low_coverage_and_insufficient_conclusion():
    compared = realistic_comparison(sparse_last_condition=True)
    assert compared["before"]["metric_coverage"]["actual"] > .8
    assert compared["stage"] == "insufficient"
    result = packet({"regulation_comparisons": {"results": [compared]}})
    retained = result["control_context"]["regulation_comparisons"]["results"][0]
    assert retained["stage"] == "insufficient" and retained["outcome"] is None
    assert retained["before"]["condition_metric_coverage_minimum"]["actual"] == .125
    assert retained["before"]["condition_metric_coverage_total"] == 12
    assert "before:actual_coverage_insufficient_in_shared_condition" in retained["next_observation"]
    assert retained["metrics"]["actual_variation_c_per_hour"]["direction"] == "unavailable"


def test_negative_weather_band_and_owner_time_are_explicit_physical_facts():
    compared = realistic_comparison(outdoor_temperature_c=-5.0)
    assert compared["shared_conditions"] == [["maintenance", 21, 1, -3]]
    boundary = compared["event"]["boundary"]
    boundary["exact_effective_at"] = "2026-01-09T00:30:00+00:00"
    boundary["provenance"] = "owner_time_with_matching_observed_values"
    source = deepcopy(compared)
    result = packet({"regulation_comparisons": {"results": [compared]},
                     "regulation_facts": {"circuits": [circuit()]}})
    block = result["control_context"]["regulation_comparisons"]
    retained = block["results"][0]
    condition = retained["shared_conditions"][0]
    assert condition["outdoor_temperature_band_c"] == {"lower_inclusive_c": -6.0, "upper_exclusive_c": -4.0}
    assert condition["heating_mode"] == "поддержание температуры"
    assert condition["target_temperature_c"] == 21
    assert condition["settings_versions"] == {"before": ["old"], "after": ["new"]}
    assert condition["controller_mode"]["observed_identifier"] == 1
    assert condition["controller_mode"]["name"] == "название режима не установлено"
    for side, version in (("before", "old"), ("after", "new")):
        coverage_condition = retained[side]["condition_metric_coverage"][0]["conditions"]
        assert coverage_condition["outdoor_temperature_band_c"] == condition["outdoor_temperature_band_c"]
        assert coverage_condition["settings_versions"] == {side: [version]}
    explicit = retained["event"]["boundary"]
    assert explicit["exact_effective_at"] == source["event"]["boundary"]["exact_effective_at"]
    assert explicit["source"] == "owner_reported_consistent_with_observation_window"
    assert explicit["telemetry_exact"] is False
    assert retained["metrics"]["actual_variation_c_per_hour"]["quantity"] == "measured_flow_temperature"
    assert retained["metrics"]["command_variation_c_per_hour"]["quantity"] == "requested_flow_temperature"
    assert retained["metrics"]["room_absolute_error_c"]["quantity"] == "room_air_temperature"
    assert retained["metrics"]["cycle_overshoot_c"]["quantity"] == "room_air_temperature"
    facts = result["control_context"]["regulation_facts"]
    assert facts["circuits"][0]["metrics"]["actual_variation_c_per_hour"]["quantity"] == "measured_flow_temperature"
    assert facts["circuits"][0]["metrics"]["room_absolute_error_c"]["quantity"] == "room_air_temperature"
    assert block["physical_quantities"]["measured_flow_temperature"] == "измеренная температура теплоносителя в подаче"
    assert "full_facts_fingerprint" in block
    assert compared == source
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= ANALYSIS_PACKET_MAX_BYTES


@pytest.mark.parametrize("humidity", ["missing", "same"])
def test_real_cooling_recovery_prepares_75_eligible_minutes_not_45(humidity):
    from tests.regulation_acceptance_cases import START, ventilation_inputs
    from zont_analyzer.analytics.regulation import build_regulation_facts

    facts = build_regulation_facts(start=START, end=START + timedelta(hours=2),
                                   inputs=ventilation_inputs(humidity))
    reason = "ventilation_corroborated" if humidity == "same" else "abrupt_room_cooling"
    assert facts["coverage"]["eligible_seconds"] == 4500
    assert facts["coverage"]["exclusion_seconds"] == {"recovery:" + reason: 2700}
    context = {"regulation_facts": {"circuits": [facts], "incomplete": False}}
    original = deepcopy(context)
    result = packet(context)
    retained = result["control_context"]["regulation_facts"]["circuits"][0]
    duration = retained["duration_summary"]
    assert duration["period_minutes"] == duration["observed_minutes"] == 120
    assert duration["eligible_minutes"] == 75
    assert duration["eligible_percent_of_report"] == 62.5
    assert duration["excluded_observed_minutes"] == 45
    assert duration["missing_fact_minutes"] == 0
    assert duration["excluded_by_reason_minutes"] == {"recovery:" + reason: 45}
    assert duration["reason_minutes_may_overlap_do_not_sum"]
    assert duration["missing_or_excluded_time_does_not_establish_a_physical_disturbance"]
    assert retained["metrics"]["observed_request_active_seconds"]["aggregation"] == "TOTAL_FOR_REPORT"
    assert retained["metrics"]["observed_request_active_seconds"]["value_minutes"] == 120
    assert retained["metrics"]["eligible_request_active_seconds"]["value_minutes"] == 75
    assert retained["metrics"]["cycle_mean_heating_seconds"]["aggregation"] == "MEAN_COMPLETE_ELIGIBLE_CYCLE"
    assert retained["metrics"]["cycle_mean_heating_seconds"]["value_minutes"] is None
    assert retained["cycle_counts"] == facts["cycle_counts"]
    assert retained["exclusion_windows"][0]["certainty"] == (
        "corroborated_same_room_humidity" if humidity == "same" else "cause_uncertain")
    assert retained["exclusion_windows"][0]["reason"] == reason
    assert retained["exclusion_windows"][0]["recovered"] is True
    assert retained["exclusion_windows"][0]["ended_at"] == (START + timedelta(minutes=75)).isoformat()
    assert "runs" not in retained and "boundary" not in retained
    assert context == original
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= ANALYSIS_PACKET_MAX_BYTES


def test_season_examples_do_not_replace_complete_exclusion_or_cycle_counts():
    source = circuit()
    source["cycle_examples_total"] = 720
    source["exclusion_windows_total"] = 60
    original = deepcopy(source)
    result = packet({"regulation_facts": {"circuits": [source]}})
    retained = result["control_context"]["regulation_facts"]["circuits"][0]
    assert retained["cycles_total"] == 720
    assert retained["cycle_examples_total"] == 720
    assert retained["cycles_input_examples_count"] == 8
    assert retained["cycles_display_examples_count"] == len(retained["cycles"]) == 2
    assert retained["exclusion_windows_total"] == 60
    assert retained["exclusion_windows_input_examples_count"] == 12
    assert retained["exclusion_windows_display_examples_count"] == len(retained["exclusion_windows"]) == 4
    assert source == original


def test_actual_sixty_day_aggregate_keeps_one_held_setpoint_and_full_census_in_packet():
    from tests.regulation_acceptance_cases import START, daily_facts, triangle_inputs
    from zont_analyzer.analytics.regulation import aggregate_regulation_facts

    days = daily_facts(triangle_inputs(60), 60)
    aggregate = aggregate_regulation_facts(days, start=START, end=START + timedelta(days=60), expected_days=60)
    assert aggregate["sample_intervals"]["target"]["sample_count"] == 1
    assert aggregate["source_semantics"]["target"] == {
        "value_kinds": ["setpoint"], "units": ["°C"], "included_days": 60,
        "missing_metadata_days": 0, "status": "known",
    }
    metrics = deepcopy(aggregate["metrics"])
    result = packet({"regulation_facts": {"circuits": [aggregate], "incomplete": aggregate["incomplete"]}})
    block = result["control_context"]["regulation_facts"]
    retained = block["circuits"][0]
    assert retained["cycles_total"] == 480
    assert retained["cycle_counts"]["full"] == 479 and retained["cycle_counts"]["partial"] == 1
    assert retained["cycles_input_examples_count"] == 8
    assert retained["cycles_display_examples_count"] == len(retained["cycles"]) <= 2
    assert retained["sample_intervals"]["target"]["sample_count"] == 1
    assert retained["source_semantics"]["target"]["value_kinds"] == ["setpoint"]
    assert retained["source_semantics"]["target"]["value_contract"] == "setpoint"
    assert retained["source_semantics"]["room"]["value_kinds"] == ["measurement"]
    assert "until change or explicit unknown" in block["source_value_contracts"]["setpoint"]
    assert "low sample count alone" in block["source_value_contracts"]["setpoint"]
    assert "never hold indefinitely" in block["source_value_contracts"]["measurement"]
    assert retained["incomplete"] is False
    example = retained["cycles"][0]
    assert example["heating_ended_at"] != example["ended_at"]
    assert example["end_error_c"] == pytest.approx(.45)
    assert retained["metrics"]["cycle_mean_end_error_c"]["value"] == pytest.approx(.45)
    assert retained["metrics"]["cycle_mean_end_error_c"]["observed_at"] == "heating_ended_at"
    assert retained["metrics"]["cycle_mean_start_error_c"]["observed_at"] == "started_at"
    assert datetime.fromisoformat(example["heating_ended_at"]) == START + timedelta(minutes=90)
    assert datetime.fromisoformat(example["ended_at"]) == START + timedelta(minutes=210)
    for name, metric in metrics.items():
        assert retained["metrics"][name]["value"] == metric["value"]
        assert retained["metrics"][name]["denominator"] == metric["denominator"]
    assert len(json.dumps(block, ensure_ascii=False, separators=(",", ":")).encode()) <= 12 * 1024
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= ANALYSIS_PACKET_MAX_BYTES


def test_mixed_or_missing_source_metadata_never_becomes_a_packet_hold_assumption():
    source = circuit()
    source["source_semantics"] = {
        "target": {"value_kinds": ["measurement", "setpoint"], "units": ["°C"], "status": "mixed"},
        "room": {"value_kinds": [], "units": [], "status": "missing"},
    }
    result = packet({"regulation_facts": {"circuits": [source]}})
    semantics = result["control_context"]["regulation_facts"]["circuits"][0]["source_semantics"]
    assert semantics["target"]["value_contract"] == semantics["room"]["value_contract"] == "mixed_or_missing_metadata"
    assert semantics["room"]["value_kinds"] == [] and semantics["room"]["units"] == []

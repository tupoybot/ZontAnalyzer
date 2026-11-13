"""Independent numerical and causal acceptance cases for issue #111."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.regulation_acceptance_cases import (
    START,
    TRIANGLE_ORACLE,
    change_timeline,
    comparison_facts,
    daily_facts,
    late_peak,
    signal,
    slice_inputs,
    triangle_inputs,
    ventilation_inputs,
)
from zont_analyzer.analytics.regulation import aggregate_regulation_facts, build_regulation_facts
from zont_analyzer.analytics.regulation_comparison import compare_change, observed_changes


def compare(*, room_scale=.5, flow_scale=2.0, confound=None, second=False, short=False):
    timeline = change_timeline(second=second)
    joined = observed_changes(timeline, [])
    facts = comparison_facts(room_scale=room_scale, flow_scale=flow_scale, confound=confound)
    if short:
        facts[1]["runs"] = [run for run in facts[1]["runs"]
                            if run["end"] <= (START + timedelta(hours=27)).isoformat()]
    result = compare_change(joined["events"][0], events=joined["events"], facts=facts,
                            start=START, end=START + timedelta(hours=49))
    return result


def test_repeating_overshoot_is_not_hidden_by_mean_temperature():
    facts = daily_facts(triangle_inputs(), 1)[0]
    assert facts["coverage"]["eligible_seconds"] == 86400
    for name, value in TRIANGLE_ORACLE.items():
        assert facts["metrics"][name]["value"] == pytest.approx(value), name
    # Independent initial 30-minute pause establishes the first start. The last
    # pause ends outside this report, so only the eighth request is partial.
    assert facts["cycle_counts"]["full"] == 7
    assert facts["cycle_counts"]["partial"] == 1
    assert len(facts["cycles"]) == 8
    assert facts["metrics"]["confirmed_heating_burner_starts"]["value"] is None


@pytest.mark.parametrize("humidity", ["same", "missing", "other"])
def test_ventilation_evidence_has_exact_impact_recovery_and_denominator(humidity):
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=2),
                                   inputs=ventilation_inputs(humidity))
    reason = "ventilation_corroborated" if humidity == "same" else "abrupt_room_cooling"
    assert len(facts["exclusion_windows"]) == 1
    window = facts["exclusion_windows"][0]
    assert window["reason"] == reason
    assert window["started_at"] == (START + timedelta(minutes=30)).isoformat()
    assert window["ended_at"] == (START + timedelta(minutes=75)).isoformat()
    assert window["recovered"] is True
    assert facts["coverage"]["eligible_seconds"] == 75 * 60
    assert facts["coverage"]["exclusion_seconds"]["recovery:" + reason] == 45 * 60
    assert facts["metrics"]["room_absolute_error_c"]["denominator"] == 75 * 60
    assert facts["metrics"]["room_absolute_error_c"]["value"] == 0


def test_zont_only_joint_change_preserves_values_uncertain_time_and_stage():
    joined = observed_changes(change_timeline(), [])
    assert len(joined["events"]) == 1
    event = joined["events"][0]
    assert event["manual_records"] == []
    assert event["author"] == event["intent"] == "unknown"
    assert event["boundary"]["exact_effective_at"] is None
    assert event["boundary"]["after"] == (START + timedelta(hours=24)).isoformat()
    assert event["boundary"]["no_later_than"] == (START + timedelta(hours=25)).isoformat()
    result = compare()
    assert result["stage"] == "available", result["next_observation"]
    assert result["event"]["changed_values"] == {
        "pid_prop_koef": {"before": {"status": "observed", "value": 1},
                          "after": {"status": "observed", "value": 3}},
        "pid_integral_koef": {"before": {"status": "observed", "value": 2},
                              "after": {"status": "observed", "value": 4}},
    }
    assert result["component_effects"] is None
    assert result["gas_savings"] is None
    assert any(reason.startswith("joint_parameter_change;") for reason in result["limitations"])


@pytest.mark.parametrize(("room_scale", "flow_scale", "expected"), [
    (.5, 2.0, "mixed"), (1.0, 1.0, "indistinguishable"), (1.5, 2.0, "worsened"),
])
def test_comparison_outcomes_have_independent_numeric_grounding(room_scale, flow_scale, expected):
    result = compare(room_scale=room_scale, flow_scale=flow_scale)
    assert result["stage"] == "available", result["next_observation"]
    assert result["outcome"] == expected
    assert result["before"]["useful_hours"] == 24
    assert result["after"]["useful_hours"] == 24
    room = result["metrics"]["room_absolute_error_c"]
    assert room["before"] == pytest.approx(5 / 14)
    assert room["after"] == pytest.approx(room_scale * 5 / 14)
    flow = result["metrics"]["actual_variation_c_per_hour"]
    assert flow["before"] == pytest.approx(40 / 3)
    assert flow["after"] == pytest.approx(flow_scale * 40 / 3)
    assert result["component_effects"] is None
    assert result["gas_savings"] is None


@pytest.mark.parametrize("confound", ["weather", "target"])
def test_weather_or_goal_change_prevents_unconditional_success(confound):
    result = compare(confound=confound)
    assert result["stage"] == "insufficient"
    assert result["outcome"] is None
    assert result["shared_conditions"] == []
    assert "observe_matching_weather_target_and_mode" in result["next_observation"]


def test_short_observation_and_second_change_do_not_become_success():
    short = compare(short=True)
    assert short["stage"] != "available"
    assert short["outcome"] is None
    second = compare(second=True)
    assert second["stage"] == "superseded"
    assert second["observation_end"] == (START + timedelta(hours=27)).isoformat()
    assert second["outcome"] is None


def test_disconnected_observed_hours_do_not_prove_continuous_heating():
    facts = []
    for after, offset in ((False, 0), (True, 25)):
        left = START + timedelta(hours=offset)
        data = triangle_inputs(start=left, version="v2" if after else "v1")
        data = replace(data, signals={**data.signals, "request": signal(
            "request", [(minute, 1.0) for minute in range(1441)], start=left)})
        pieces = [build_regulation_facts(start=left + timedelta(hours=hour),
                                         end=left + timedelta(hours=hour + 6), inputs=data) for hour in (0, 12)]
        fact = pieces[0]
        fact["runs"] = pieces[0]["runs"] + pieces[1]["runs"]
        facts.append(fact)
    joined = observed_changes(change_timeline(), [])
    result = compare_change(joined["events"][0], events=joined["events"], facts=facts,
                            start=START, end=START + timedelta(hours=49))
    assert result["before"]["useful_hours"] == result["after"]["useful_hours"] == 12
    assert result["before"]["continuous_heat"] is False
    assert result["after"]["continuous_heat"] is False
    assert result["outcome"] is None


def test_owner_prediction_is_frozen_when_joining_observed_joint_change():
    original = {"expected_effect": "MAE decreases by at least 0.1°C within 7 days",
                "predictions": [{"metric": "room_absolute_error_c", "direction": "decrease", "minimum_delta": .1}]}
    owner = {"intervention_id": "synthetic-experiment", "experiment": {
        "performed_at": (START + timedelta(hours=24, minutes=30)).isoformat(),
        "changes": [{"device_id": "synthetic-device", "circuit_id": "synthetic-room", "parameter": key,
                     "before": str(old), "after": str(new)} for key, old, new in
                    [("pid_prop_koef", 1, 3), ("pid_integral_koef", 2, 4)]],
        "original": original, "category": "settings", "status": "completed", "recommendation_id": None}}
    frozen = deepcopy(owner)
    joined = observed_changes(change_timeline(), [owner])
    assert len(joined["events"]) == 1
    assert joined["unmatched_manual_records"] == []
    result = compare_change(joined["events"][0], events=joined["events"], facts=comparison_facts(),
                            start=START, end=START + timedelta(hours=49))
    assert result["forecast_assessments"][0]["original"] == original
    assert owner == frozen


@pytest.mark.parametrize("days", [1, 7, 60])
def test_complete_period_census_equals_full_source_without_example_truncation(days):
    data = triangle_inputs(days)
    end = START + timedelta(days=days)
    direct = build_regulation_facts(start=START, end=end, inputs=data)
    compact = aggregate_regulation_facts(daily_facts(data, days), start=START, end=end, expected_days=days)
    assert compact["incomplete"] is False
    assert compact["cycle_examples_total"] == days * 8
    assert compact["cycle_counts"]["full"] == days * 8 - 1
    assert compact["cycle_counts"]["partial"] == 1
    assert compact["coverage"]["eligible_seconds"] == days * 86400
    assert compact["cycle_counts"] == direct["cycle_counts"]
    for name, metric in direct["metrics"].items():
        assert compact["metrics"][name]["value"] == pytest.approx(metric["value"]), name
        assert compact["metrics"][name]["denominator"] == pytest.approx(metric["denominator"]), name
    for name, value in TRIANGLE_ORACLE.items():
        assert compact["metrics"][name]["value"] == pytest.approx(value), name
    assert len(compact["cycles"]) <= 8


def test_late_season_peak_replaces_day_and_repeat_does_not_add_cycles():
    days, changed_day = 60, 35
    data = triangle_inputs(days)
    end = START + timedelta(days=days)
    original = daily_facts(data, days)
    late = late_peak(data, day=changed_day)
    left, right = START + timedelta(days=changed_day), START + timedelta(days=changed_day + 1)
    rebuilt = build_regulation_facts(start=left, end=right, inputs=slice_inputs(late, left, right),
                                     previous_boundary=original[changed_day - 1]["boundary"])
    updated = original.copy()
    updated[changed_day] = rebuilt
    actual = aggregate_regulation_facts(updated, start=START, end=end, expected_days=days)
    expected = build_regulation_facts(start=START, end=end, inputs=late)
    assert actual["cycle_counts"] == expected["cycle_counts"]
    assert actual["cycle_counts"]["full"] == 479
    assert actual["metrics"]["room_signed_error_c"]["value"] == pytest.approx(.1 + 30 / (60 * 86400))
    assert actual["metrics"]["cycle_mean_overshoot_c"]["value"] == pytest.approx(.8 + .1 / 479)
    for name, metric in expected["metrics"].items():
        assert actual["metrics"][name]["value"] == pytest.approx(metric["value"]), name
    assert aggregate_regulation_facts(updated + updated, start=START, end=end, expected_days=days) == actual
    assert rebuilt["boundary"] == original[changed_day]["boundary"]


def test_minute_activity_measures_seconds_without_inventing_flame_edges():
    data = triangle_inputs()
    request = signal("request", [(minute, 23.0) for minute in range(1, 11)])
    data = replace(data, request_semantics="preceding_minute", signals={**data.signals, "request": request})
    facts = build_regulation_facts(start=START, end=START + timedelta(minutes=10), inputs=data)
    assert facts["metrics"]["observed_request_active_seconds"]["value"] == 230
    assert facts["cycle_counts"]["full"] == 0
    assert facts["cycles"][0]["edge_precision"] == "preceding_minute"
    assert facts["cycles"][0]["heating_duration_bounds_seconds"] == {"minimum": 480, "maximum": 600}
    assert facts["metrics"]["confirmed_heating_burner_starts"]["value"] is None

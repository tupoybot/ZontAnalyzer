from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.evidence import (
    ExclusionWindow,
    NumericSample,
    SignalMetadata,
    SignalSeries,
    StateSample,
)
from zont_analyzer.analytics.regulation import (
    RegulationInputs,
    aggregate_regulation_facts,
    build_regulation_facts,
)
from zont_analyzer.application.regulation_context import regulation_fingerprint

START = datetime(2025, 1, 1, tzinfo=UTC)


def signal(key: str, values: list[float], *, step: int = 60, start: datetime = START,
           setpoint: bool = False, identity: str = "room-sensor") -> SignalSeries:
    return SignalSeries(key, SignalMetadata(identity, key, "°C", value_kind="setpoint" if setpoint else "measurement"),
                        tuple(NumericSample(start + timedelta(seconds=index * step), value)
                              for index, value in enumerate(values)))


def inputs(*, minutes: int = 60, room: list[float] | None = None,
           request: list[float] | None = None) -> RegulationInputs:
    return RegulationInputs("circuit", device_id="device", signals={
        "room": signal("room", room if room is not None else [20.0] * (minutes + 1)),
        "target": signal("target", [20.0], setpoint=True),
        "request": signal("request", request if request is not None else [1.0] * (minutes + 1)),
        "command": signal("command", [45.0] * (minutes + 1), setpoint=True),
        "actual": signal("actual", [44.0] * (minutes + 1)),
        "outdoor": signal("outdoor", [-5.0] * (minutes + 1)),
    })


def calculate(data: RegulationInputs, *, minutes: int = 60) -> dict:
    return build_regulation_facts(start=START, end=START + timedelta(minutes=minutes), inputs=data)


@pytest.mark.parametrize(("activity", "classification"), [(1.0, "continuous"), (0.0, "no_heat")])
def test_observed_continuous_and_no_heat_are_distinct(activity: float, classification: str) -> None:
    facts = calculate(inputs(request=[activity] * 61))
    assert facts["classification"] == classification
    assert facts["coverage"]["eligible_seconds"] == 3600
    assert facts["metrics"]["observed_request_active_seconds"]["value"] == activity * 3600
    assert facts["metrics"]["confirmed_heating_burner_starts"]["value"] is None


def test_missing_request_does_not_invent_no_heat_or_cycles() -> None:
    data = inputs()
    del data.signals["request"]
    facts = calculate(data)
    assert facts["classification"] == "partial"
    assert facts["coverage"]["eligible_seconds"] == 0
    assert "missing:request" in facts["unknowns"]


def test_bad_overshoot_and_long_heating_remain_in_census() -> None:
    request = [float(10 <= minute < 30 or minute >= 50) for minute in range(61)]
    room = [20 + min(minute, 35) * 0.02 for minute in range(61)]
    facts = calculate(inputs(room=room, request=request))
    assert facts["cycle_counts"]["full"] == 1
    cycle = next(cycle for cycle in facts["cycles"] if cycle["kind"] == "full")
    assert cycle["heating_seconds"] == 1200
    assert cycle["pause_seconds"] == 1200
    assert cycle["overshoot_c"] == pytest.approx(0.7)
    assert cycle["post_heat_rise_c"] == pytest.approx(0.1)
    assert cycle["peak_delay_seconds"] == 300
    assert cycle["room_reaction_after_seconds"] == pytest.approx(300)
    assert cycle["eligible"] is True
    assert facts["coverage"]["eligible_seconds"] == 3600
    assert facts["exclusion_seconds"] == {}


def test_command_swings_and_room_accuracy_have_separate_denominators() -> None:
    data = inputs()
    data.signals["command"] = signal("command", [40.0 if minute % 10 < 5 else 60.0 for minute in range(61)],
                                     setpoint=True)
    facts = calculate(data)
    assert facts["metrics"]["room_absolute_error_c"]["value"] == 0
    assert facts["metrics"]["command_total_variation_c"]["value"] == 220
    assert facts["metrics"]["command_max_rate_c_per_hour"]["value"] == 1200
    assert facts["metrics"]["command_at_known_limit_pct"]["value"] is None
    assert facts["coverage"]["eligible_seconds"] == 3600
    data.signals["limit"] = signal("limit", [60.0], setpoint=True)
    limited = calculate(data)
    assert limited["metrics"]["command_at_known_limit_pct"]["value"] == 50


def test_exact_error_integrals_cross_target_without_band_bias() -> None:
    data = inputs(minutes=10, room=[19.5 + minute * 0.1 for minute in range(11)])
    facts = calculate(data, minutes=10)
    assert facts["metrics"]["room_signed_error_c"]["value"] == pytest.approx(0)
    assert facts["metrics"]["room_absolute_error_c"]["value"] == pytest.approx(0.25)
    assert facts["metrics"]["room_overheat_degree_hours"]["value"] == pytest.approx(0.5 * 300 / 2 / 3600)
    assert facts["metrics"]["room_underheat_degree_hours"]["value"] == pytest.approx(0.5 * 300 / 2 / 3600)


@pytest.mark.parametrize("humidity", ["same", "missing", "other", "invalid", "unknown",
                                    "temperature_unit", "missing_unit"])
def test_abrupt_cooling_and_data_driven_recovery(humidity: str) -> None:
    room = [20.0] * 6 + [19.75, 19.5, 19.25, 19.0] + [19.0] * 10
    room += [19.0 + 0.1 * minute for minute in range(1, 11)] + [20.0] * 31
    data = inputs(room=room)
    if humidity != "missing":
        humidity_values = [50.0] * 6 + [48.0, 46.0, 44.0, 42.0] + [42.0] * 51
        if humidity == "invalid":
            humidity_values = [150.0] * len(humidity_values)
        series = signal("humidity", humidity_values)
        series = replace(series, metadata=replace(series.metadata,
            unit="°C" if humidity == "temperature_unit" else "" if humidity == "missing_unit" else "%"))
        if humidity == "unknown":
            series = replace(series, unknown_at=(START + timedelta(minutes=7),))
        data.signals["humidity"] = series
        data = replace(data, humidity_room_identity="room-sensor" if humidity != "other" else "different-room")
    facts = calculate(data)
    recovery = facts["exclusion_windows"][0]
    expected = "ventilation_corroborated" if humidity in {"same", "missing_unit"} else "abrupt_room_cooling"
    assert recovery["reason"] == expected
    assert recovery["recovered"] is True
    assert datetime.fromisoformat(recovery["ended_at"]) >= START + timedelta(minutes=32)
    assert facts["coverage"]["eligible_seconds"] < 3600
    assert facts["coverage"]["eligible_seconds"] > 0
    assert "probability" not in str(facts)


def test_two_remote_cooling_points_cannot_establish_disturbance() -> None:
    data = inputs()
    data.signals["room"] = signal("room", [20.0, 18.0], step=3600)
    facts = calculate(data)
    assert not any(item["reason"] == "abrupt_room_cooling" for item in facts["exclusion_windows"])
    assert facts["metrics"]["room_absolute_error_c"]["value"] is None


def test_dhw_target_and_reliability_recover_after_stable_observations() -> None:
    data = inputs()
    data = replace(data, exclusions=(
        ExclusionWindow(START + timedelta(minutes=5), START + timedelta(minutes=10), "dhw"),
        ExclusionWindow(START + timedelta(minutes=20), START + timedelta(minutes=25), "reliability"),
    ))
    data.signals["target"] = signal("target", [20, 20, 20, 21, 20, 20, 20], step=600, setpoint=True)
    facts = calculate(data)
    reasons = {item["reason"] for item in facts["exclusion_windows"]}
    assert {"dhw", "reliability", "target_or_mode_change"} <= reasons
    assert facts["coverage"]["exclusion_seconds"]["recovery:dhw"] == 8 * 60
    assert facts["coverage"]["exclusion_seconds"]["recovery:reliability"] == 8 * 60
    assert facts["metrics"]["room_absolute_error_c"]["denominator"] < 3600


def test_unknown_breaks_cycle_and_no_last_sample_extrapolation() -> None:
    data = inputs(request=[float(10 <= minute < 30 or minute >= 50) for minute in range(61)])
    data.signals["room"] = replace(data.signals["room"], unknown_at=(START + timedelta(minutes=20),))
    data.signals["actual"] = signal("actual", [44.0] * 31)
    facts = calculate(data)
    assert facts["cycle_counts"]["full"] == 0
    assert facts["metrics"]["actual_range_c"]["denominator"] <= 1800
    assert facts["coverage"]["exclusion_seconds"]["room_missing_or_gap"] == 120


def test_preceding_minute_activity_is_exact_seconds_without_precise_edges_or_burner_starts() -> None:
    data = inputs(minutes=10)
    data.signals["request"] = signal("request", [23.0] * 10, start=START + timedelta(minutes=1))
    facts = calculate(replace(data, request_semantics="preceding_minute"), minutes=10)
    assert facts["metrics"]["observed_request_active_seconds"]["value"] == pytest.approx(230)
    assert facts["metrics"]["confirmed_heating_burner_starts"]["value"] is None
    assert facts["cycle_counts"]["full"] == 0
    assert facts["cycles"][0]["observed_active_seconds"] == pytest.approx(230)
    assert facts["cycles"][0]["heating_seconds"] == 600
    assert facts["cycles"][0]["edge_precision"] == "preceding_minute"
    assert facts["cycles"][0]["heating_duration_bounds_seconds"] == {"minimum": 480, "maximum": 600}


def test_complete_minute_bin_episodes_keep_full_census_with_explicit_edge_uncertainty() -> None:
    data = inputs()
    data.signals["request"] = signal("request", [23.0 if 10 <= minute < 30 or minute >= 50 else 0.0
                                                 for minute in range(60)], start=START + timedelta(minutes=1))
    facts = calculate(replace(data, request_semantics="preceding_minute"))
    assert facts["cycle_counts"]["full"] == 1
    cycle = next(cycle for cycle in facts["cycles"] if cycle["kind"] == "full")
    assert cycle["observed_active_seconds"] == pytest.approx(20 * 23)
    assert cycle["heating_seconds"] == 1200
    assert cycle["edge_precision"] == "preceding_minute"
    assert cycle["heating_duration_bounds_seconds"] == {"minimum": 1080, "maximum": 1200}
    assert cycle["start_boundary_interval"]["latest"] == (START + timedelta(minutes=11)).isoformat()


def test_overlapping_minute_aggregates_preserve_observations_without_inventing_chronology() -> None:
    data = inputs(minutes=10)
    data.signals["request"] = signal("request", [23.0] * 21, step=30)
    facts = calculate(replace(data, request_semantics="preceding_minute"), minutes=10)
    assert facts["metrics"]["observed_request_active_seconds"]["value"] is None
    assert facts["request_observation_totals"]["active_seconds_in_observation_windows_sum"] == 20 * 23
    assert facts["request_observation_totals"]["sum_is_chronological_duration"] is False
    assert "overlapping_minute_request_windows_not_reconstructable" in facts["unknowns"]


def test_isolated_overlap_excludes_its_union_and_preserves_unaffected_full_cycles() -> None:
    data = inputs(minutes=240)
    regular = signal("request", [60.0 if 10 <= minute % 40 < 30 else 0.0 for minute in range(241)],
                     start=START + timedelta(minutes=1))
    data.signals["request"] = regular
    data = replace(data, request_semantics="preceding_minute")
    baseline = calculate(data, minutes=240)
    data.signals["request"] = replace(regular, samples=tuple(sorted((
        *regular.samples, NumericSample(START + timedelta(minutes=105, seconds=30), 23.0),
    ))))
    facts = calculate(data, minutes=240)
    assert baseline["cycle_counts"]["full"] == 5
    assert facts["cycle_counts"]["full"] == 4
    assert facts["metrics"]["observed_request_active_seconds"]["value"] == 7200 - 120
    assert facts["metrics"]["observed_request_active_seconds"]["denominator"] == 14400 - 120
    assert facts["coverage"]["exclusion_seconds"]["request_unknown"] == 120
    totals = facts["request_observation_totals"]
    assert totals["active_seconds_in_observation_windows_sum"] == 7200 + 23
    assert totals["overlap_excluded_window_count"] == 3
    assert totals["overlap_excluded_seconds"] == 120


def test_overlap_across_midnight_matches_full_source_with_boundary_lookahead() -> None:
    start = START + timedelta(hours=23)
    midnight, end = start + timedelta(hours=1), start + timedelta(hours=2)
    source = inputs(minutes=121)
    source.signals["request"] = signal(
        "request", [60.0 if 10 <= minute % 40 < 30 else 0.0 for minute in range(121)],
        start=START + timedelta(minutes=1),
    )
    source = replace(source, request_semantics="preceding_minute", signals={
        key: replace(series, samples=tuple(replace(sample, timestamp=sample.timestamp + timedelta(hours=23))
                                           for sample in series.samples)) for key, series in source.signals.items()})
    request = source.signals["request"]
    source.signals["request"] = replace(request, samples=tuple(sorted((
        *request.samples, NumericSample(midnight + timedelta(seconds=30), 23.0),
    ))))
    baseline = build_regulation_facts(start=start, end=end, inputs=source)
    days = []
    previous = None
    for left, right in ((start, midnight), (midnight, end)):
        # Each day has its own bounded source slice; the extra minute is
        # necessary to discover a window overlapping the preceding midnight.
        selected = replace(source, signals={key: replace(series, samples=tuple(
            sample for sample in series.samples
            if left - timedelta(hours=6) <= sample.timestamp <= right + timedelta(seconds=61)))
            for key, series in source.signals.items()})
        fact = build_regulation_facts(start=left, end=right, inputs=selected, previous_boundary=previous)
        days.append(fact)
        previous = fact["boundary"]
    combined = aggregate_regulation_facts(days, start=start, end=end)
    assert baseline["cycle_counts"]["full"] == 1
    assert combined["cycle_counts"] == baseline["cycle_counts"]
    assert baseline["metrics"]["observed_request_active_seconds"]["value"] == 3480
    assert combined["request_observation_totals"] == baseline["request_observation_totals"]
    assert combined["request_observation_totals"]["overlap_excluded_seconds"] == 120
    for key, metric in baseline["metrics"].items():
        for field in ("value", "numerator", "denominator"):
            if metric[field] is None:
                assert combined["metrics"][key][field] is None
            else:
                assert combined["metrics"][key][field] == pytest.approx(metric[field])


def test_fractional_minute_crossing_report_edge_does_not_split_unknown_active_seconds() -> None:
    data = inputs(minutes=10)
    data.signals["request"] = signal("request", [23.0] * 10, step=60, start=START + timedelta(seconds=30))
    facts = calculate(replace(data, request_semantics="preceding_minute"), minutes=10)
    assert facts["metrics"]["observed_request_active_seconds"]["value"] == 9 * 23
    assert facts["metrics"]["observed_request_active_seconds"]["denominator"] == 9 * 60


def test_reaction_requires_crossing_cycle_initial_temperature_not_a_local_rise_after_cooling() -> None:
    data = inputs(room=[20.0, 19.8, 19.9, 20.0, 20.1] + [20.1] * 56)
    facts = calculate(data)
    assert facts["cycles"][0]["room_reaction_after_seconds"] == pytest.approx(240)


def test_cooling_during_ordinary_request_pause_excludes_recovery_after_heating_resumes() -> None:
    room = [20.0] * 6 + [19.75, 19.5] + [19.5] * 23 + [20.0] * 30
    data = inputs(room=room, request=[0.0] * 10 + [1.0] * 51)
    facts = calculate(data)
    assert facts["exclusion_windows"][0]["reason"] == "abrupt_room_cooling"
    assert facts["coverage"]["exclusion_seconds"]["recovery:abrupt_room_cooling"] >= 25 * 60


def test_room_cooling_after_target_reduction_retains_known_transition_cause() -> None:
    data = inputs(room=[20.0] * 6 + [19.75, 19.5] + [19.0] * 53)
    data.signals["target"] = signal("target", [20.0, 19.0], step=300, setpoint=True)
    facts = calculate(data)
    assert any(item["reason"] == "target_or_mode_change" for item in facts["exclusion_windows"])
    assert not any(item["reason"] == "abrupt_room_cooling" for item in facts["exclusion_windows"])


def test_midnight_command_jump_preserves_first_rate_with_carried_predecessor_time() -> None:
    midnight = START + timedelta(hours=1)
    data = inputs(minutes=120)
    data.signals["command"] = signal("command", [40.0] * 6 + [60.0] * 7, step=600, setpoint=True)
    first_data = replace(data, signals={key: replace(series, samples=tuple(
        sample for sample in series.samples if sample.timestamp <= midnight)) for key, series in data.signals.items()})
    first = build_regulation_facts(start=START, end=midnight, inputs=first_data)
    next_data = replace(data, signals={key: replace(series, samples=tuple(
        sample for sample in series.samples if midnight <= sample.timestamp <= midnight + timedelta(hours=1)
        or key == "target")) for key, series in data.signals.items()})
    second = build_regulation_facts(start=midnight, end=midnight + timedelta(hours=1), inputs=next_data,
                                    previous_boundary=first["boundary"])
    assert second["metrics"]["command_max_rate_c_per_hour"]["value"] == 120


def test_only_confirmed_heating_flame_transitions_count() -> None:
    data = inputs(minutes=10)
    flags = [frozenset({"ch"}), frozenset({"ch", "fl"}), frozenset({"ch", "fl"}), frozenset({"ch"}),
             frozenset({"ch", "fl"}), frozenset({"dhw", "fl"}), frozenset({"ch"}), frozenset({"ch"}),
             frozenset({"ch", "fl"}), frozenset({"ch"}), frozenset({"ch"})]
    data = replace(data, states=tuple(StateSample(START + timedelta(minutes=index), value, "adapter")
                                     for index, value in enumerate(flags)))
    facts = calculate(data, minutes=10)
    # The start after DHW falls inside measured recovery and is excluded from
    # tuning statistics; it remains in census runs for audit.
    assert sum(run["burner_starts"] for run in facts["runs"]) == 3
    assert facts["metrics"]["confirmed_heating_burner_starts"]["value"] == 3
    assert facts["metrics"]["eligible_confirmed_heating_burner_starts"]["value"] == 2


def test_recovery_remains_open_across_midnight_and_boundary_hash_invalidates_neighbor() -> None:
    left = inputs(minutes=10, room=[20] * 6 + [19.75, 19.5, 19.25, 19, 19])
    first = calculate(left, minutes=10)
    assert first["boundary"]["recovery"] is not None
    midnight = START + timedelta(minutes=10)
    right = inputs(minutes=10, room=[19.0 + min(index, 5) * 0.2 for index in range(11)])
    right = replace(right, signals={key: replace(series, samples=tuple(
        replace(sample, timestamp=sample.timestamp + timedelta(minutes=10)) for sample in series.samples))
        for key, series in right.signals.items()})
    second = build_regulation_facts(start=midnight, end=midnight + timedelta(minutes=10), inputs=right,
                                    previous_boundary=first["boundary"])
    assert second["exclusion_windows"][0]["recovered"]
    assert second["exclusion_windows"][0]["started_at"] == first["exclusion_windows"][0]["started_at"]
    aggregate = aggregate_regulation_facts([first, second], start=START, end=midnight + timedelta(minutes=10))
    assert aggregate["exclusion_windows_total"] == 1
    assert aggregate["exclusion_windows"][0]["recovered"] is True
    assert not aggregate["exclusion_windows_truncated"]
    before = regulation_fingerprint(source_revision="same", settings_version="v", previous_boundary=first["boundary"])
    after = regulation_fingerprint(source_revision="same", settings_version="v", previous_boundary=None)
    assert before != after


def test_recovery_requires_room_observations_not_unrelated_fast_signal_boundaries() -> None:
    data = inputs()
    data.signals["room"] = signal("room", [20.0] * 7, step=600)
    data = replace(data, exclusions=(ExclusionWindow(START, START + timedelta(minutes=1), "dhw"),))
    facts = calculate(data)
    recovery = facts["exclusion_windows"][0]
    assert recovery["ended_at"] == (START + timedelta(minutes=30)).isoformat()
    assert facts["coverage"]["exclusion_seconds"]["recovery:dhw"] == 1800


def test_target_increase_marks_initial_warming_then_maintenance_without_assuming_it_from_low_room() -> None:
    data = inputs(room=[19.0] * 61)
    initial = calculate(data)
    assert initial["coverage"]["mode_seconds"] == {"unknown": 3600}
    data.signals["target"] = signal("target", [19.0, 20.0], step=600, setpoint=True)
    data.signals["room"] = signal("room", [19.0] * 11 + [19.0 + min(index, 10) * 0.1 for index in range(50)])
    changed = calculate(data)
    modes = {run["mode"] for run in changed["runs"]}
    assert {"warmup", "maintenance"} <= modes
    assert any(item["reason"] == "target_or_mode_change" for item in changed["exclusion_windows"])


def test_sixty_day_census_matches_full_source_without_midnight_loss_or_duplicate_count() -> None:
    days, step = 60, 600
    count = days * 24 * 6 + 1
    request = [float((index // 6) % 2) for index in range(count)]
    data = RegulationInputs("circuit", device_id="device", signals={
        "room": signal("room", [20.0] * count, step=step),
        "target": signal("target", [20.0], setpoint=True),
        "request": signal("request", request, step=step),
        "command": signal("command", [40.0 if index % 2 else 60.0 for index in range(count)],
                          step=step, setpoint=True),
    })
    end = START + timedelta(days=days)
    baseline = build_regulation_facts(start=START, end=end, inputs=data)
    daily = []
    boundary = None
    for day in range(days):
        start = START + timedelta(days=day)
        stop = start + timedelta(days=1)
        day_inputs = replace(data, signals={key: replace(series, samples=tuple(
            sample for sample in series.samples if start <= sample.timestamp <= stop or key == "target"))
            for key, series in data.signals.items()})
        fact = build_regulation_facts(start=start, end=stop, inputs=day_inputs, previous_boundary=boundary)
        daily.append(fact)
        boundary = fact["boundary"]
    aggregate = aggregate_regulation_facts(daily + [daily[0]], start=START, end=end, expected_days=60)
    assert aggregate["included_days"] == 60
    assert not aggregate["incomplete"]
    assert aggregate["cycle_counts"] == baseline["cycle_counts"]
    assert aggregate["cycle_counts"]["full"] == 719
    for name, metric in baseline["metrics"].items():
        if metric["value"] is not None:
            assert aggregate["metrics"][name]["value"] == pytest.approx(metric["value"])
        else:
            assert aggregate["metrics"][name]["value"] is None
    assert aggregate["coverage"]["eligible_seconds"] == 60 * 86400
    assert len(aggregate["cycles"]) == 8
    assert aggregate["cycle_examples_total"] == 720
    missing = aggregate_regulation_facts(daily[:-1], start=START, end=end, expected_days=60)
    assert missing["incomplete"]
    assert "missing_facts_require_local_backfill" in missing["unknowns"]
    with pytest.raises(ValueError, match="different circuits"):
        aggregate_regulation_facts([daily[0], {**daily[1], "scope": "other/circuit"}], start=START, end=end)

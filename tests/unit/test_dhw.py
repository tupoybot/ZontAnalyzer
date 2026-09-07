from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.dhw import (
    BoilerPurpose,
    HeatingDemand,
    analyze_dhw_interactions,
    classify_heating_demand,
    classify_opentherm_state,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


def _base_analysis(**overrides: object):
    arguments: dict[str, object] = {
        "period_id": "day:1",
        "period_start": START,
        "period_end": _at(40),
        "boiler_state_samples": [
            (_at(0), "['ch', 'fl']"),
            (_at(5), "['ch', 'fl']"),
            (_at(10), "['dhw', 'fl']"),
            (_at(15), "['dhw', 'fl']"),
            (_at(20), "['ch', 'fl']"),
            (_at(25), "['ch']"),
            (_at(30), "[]"),
        ],
        "dhw_temperature_samples": [
            (_at(0), 50.0),
            (_at(5), 48.0),
            (_at(10), 45.0),
            (_at(15), 52.0),
            (_at(20), 55.0),
            (_at(25), 56.0),
            (_at(30), 55.0),
            (_at(35), 54.0),
        ],
        "dhw_target_samples": [(_at(0), 55.0)],
        "dhw_mode_samples": [(_at(0), 1.0)],
        "dhw_status_samples": [(_at(0), 7.0)],
        "dhw_worktime_samples": [(_at(0), 0.0), (_at(10), 1.0)],
        "dhw_mode_catalog": {1: {"id": 1, "name": "Комфорт", "heating_enabled": True}},
        "dhw_circuit_config": {"hysteresis_c": 0.5},
        "heating_available_samples": [(_at(0), True)],
    }
    arguments.update(overrides)
    return analyze_dhw_interactions(**arguments)  # type: ignore[arg-type]


def _metrics(result: object) -> dict[str, float]:
    return {item.name: item.value for item in result.metrics}  # type: ignore[attr-defined]


def test_opentherm_states_are_mutually_exclusive() -> None:
    assert classify_opentherm_state(["ch", "fl"]) == BoilerPurpose.HEATING
    assert classify_opentherm_state(["dhw", "fl"]) == BoilerPurpose.DHW
    assert classify_opentherm_state(["ch", "dhw", "fl"]) == BoilerPurpose.CONCURRENT_OR_AMBIGUOUS
    assert classify_opentherm_state(["fl"]) == BoilerPurpose.IDLE
    assert classify_opentherm_state("not valid telemetry") == BoilerPurpose.IDLE


def test_normal_reheat_uses_historical_target_mode_status_and_worktime() -> None:
    result = _base_analysis()

    assert _metrics(result)["dhw_episode_count"] == 1
    assert _metrics(result)["dhw_mean_recovery_minutes"] == 10
    assert _metrics(result)["dhw_mean_overshoot_c"] == 1
    assert _metrics(result)["dhw_confirmed_heating_pause_count"] == 1
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")
    assert episode.algorithm_version == "dhw-v2"
    assert episode.id == f"event:day:1:dhw_episode:{int(_at(10).timestamp())}:dhw-v2"
    assert episode.details["facts"]["selected_system_mode_id"] == 1
    assert episode.details["facts"]["dhw_enabled_by_selected_mode"] is True
    assert episode.details["facts"]["dhw_status"] == 7
    assert episode.details["facts"]["dhw_worktime"] == 1
    assert set(episode.details) == {"facts", "inference", "hypothesis"}


def test_episode_uses_target_that_was_active_at_its_start() -> None:
    result = _base_analysis(dhw_target_samples=[(_at(0), 50.0), (_at(12), 55.0)])
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert episode.details["facts"]["dhw_target_c"] == 50
    assert episode.details["facts"]["recovery_minutes"] == 5


def test_dhw_target_none_stops_evaluation_until_next_known_state() -> None:
    result = _base_analysis(
        dhw_target_samples=[(_at(0), 55.0), (_at(7), None), (_at(13), 55.0)],
    )

    # Temperature is continuously measured to minute 35.  The two explicit
    # target states evaluate [0, 7) and [13, 35), while [7, 13) is unknown.
    assert _metrics(result)["dhw_target_evaluation_time_pct"] == 72.5
    assert result.context["dhw_circuit"]["current_target_c"] == 55.0


def test_selected_mode_can_disable_dhw_and_excludes_below_target_time() -> None:
    result = _base_analysis(
        boiler_state_samples=[(_at(0), "[]"), (_at(5), "[]"), (_at(10), "[]")],
        dhw_mode_samples=[(_at(0), 2.0)],
        dhw_mode_catalog={2: {"id": 2, "name": "Эконом", "heating_enabled": False}},
        recirculation_present=False,
    )

    assert result.context["dhw_circuit"]["current_mode_id"] == 2
    assert result.context["dhw_circuit"]["current_mode"]["heating_enabled"] is False
    assert "dhw_time_below_target_pct" not in _metrics(result)
    assert result.context["dhw_circuit"]["current_enabled"] is False
    assert result.context["dhw_circuit"]["current_target_c"] is None
    assert result.context["dhw_circuit"]["configured_or_last_target_c"] == 55
    assert not result.events


def test_activity_while_dhw_mode_is_off_is_not_an_ordinary_reheat() -> None:
    result = _base_analysis(
        boiler_state_samples=[(_at(0), "[]"), (_at(5), "['dhw', 'fl']"), (_at(10), "[]"), (_at(15), "[]")],
        dhw_temperature_samples=[(_at(0), 45.0), (_at(5), 45.0), (_at(10), 48.0), (_at(15), 49.0)],
        dhw_mode_samples=[(_at(0), 2.0)],
        dhw_mode_catalog={2: {"id": 2, "name": "Эконом", "circuit_enabled": False}},
        recirculation_present=False,
    )

    assert _metrics(result)["dhw_episode_count"] == 0
    assert _metrics(result)["dhw_activity_while_disabled_count"] == 1
    event = next(item for item in result.events if item.kind == "dhw_activity_while_disabled")
    assert event.severity == "info"


def test_off_to_sanitary_temperature_and_back_is_probable_antilegionella() -> None:
    result = _base_analysis(
        boiler_state_samples=[
            (_at(0), "[]"),
            (_at(5), "['dhw', 'fl']"),
            (_at(10), "['dhw', 'fl']"),
            (_at(15), "[]"),
            (_at(20), "[]"),
        ],
        dhw_temperature_samples=[
            (_at(0), 45.0),
            (_at(5), 46.0),
            (_at(10), 56.0),
            (_at(15), 60.0),
            (_at(20), 59.0),
        ],
        dhw_mode_samples=[(_at(0), 2.0)],
        dhw_mode_catalog={2: {"id": 2, "name": "Эконом", "circuit_enabled": False}},
        recirculation_present=False,
    )

    assert _metrics(result)["dhw_episode_count"] == 0
    assert _metrics(result)["dhw_antilegionella_cycle_count"] == 1
    event = next(item for item in result.events if item.kind == "dhw_antilegionella_cycle")
    assert event.severity == "info"
    assert event.details["inference"]["expected_service_cycle"] is True


def test_recirculation_candidate_is_explicitly_inference_only() -> None:
    arguments = {
        "boiler_state_samples": [(_at(0), "[]"), (_at(5), "[]"), (_at(10), "[]")],
        "dhw_temperature_samples": [(_at(0), 50.0), (_at(5), 49.2), (_at(10), 49.1)],
        "recirculation_present": True,
    }
    result = _base_analysis(**arguments)
    disabled = _base_analysis(**{**arguments, "recirculation_present": False})

    event = next(item for item in result.events if item.kind == "dhw_possible_recirculation_activity")
    assert event.details["facts"]["direct_pump_signal_available"] is False
    assert event.details["inference"]["confidence"] == "low"
    assert "AUTOADAPT" in event.details["hypothesis"]
    assert result.context["recirculation"]["inference_only"] is True
    assert not any(item.kind == "dhw_possible_recirculation_activity" for item in disabled.events)


def test_ch_pause_has_fast_return_and_is_not_an_alert() -> None:
    result = _base_analysis()
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert episode.details["inference"]["heating_demand"] == HeatingDemand.CONFIRMED
    assert episode.details["facts"]["heating_return_delay_minutes"] == 0
    assert episode.details["inference"]["confirmed_heating_pause_minutes"] == 10
    assert not any(event.kind == "dhw_long_heating_return" for event in result.events)


def test_heating_activity_can_return_without_flame_on_residual_heat() -> None:
    result = _base_analysis(
        boiler_state_samples=[
            (_at(0), "['ch', 'fl']"),
            (_at(5), "['dhw', 'fl']"),
            (_at(10), "[]"),
            (_at(15), "[]"),
            (_at(20), "[]"),
        ],
        heating_worktime_samples=[(_at(0), 1.0), (_at(10), 1.0)],
    )
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert episode.details["facts"]["heating_return_source"] == "worktime"
    assert episode.details["facts"]["heating_return_without_flame"] is True
    assert _metrics(result)["dhw_residual_heat_return_count"] == 1


def test_long_return_warns_only_with_confirmed_heating_demand() -> None:
    result = _base_analysis(
        boiler_state_samples=[
            (_at(0), "['ch', 'fl']"),
            (_at(5), "['dhw', 'fl']"),
            (_at(10), "[]"),
            (_at(15), "[]"),
            (_at(30), "['ch', 'fl']"),
            (_at(35), "['ch']"),
        ],
    )

    warning = next(event for event in result.events if event.kind == "dhw_long_heating_return")
    assert warning.severity == "warning"
    assert warning.details["facts"]["return_delay_minutes"] == 20
    assert warning.details["inference"]["heating_demand"] == "confirmed"
    assert _metrics(result)["dhw_long_heating_return_count"] == 1


def test_hot_flow_tail_is_a_fact_but_hydraulic_causality_remains_a_hypothesis() -> None:
    result = _base_analysis(
        boiler_state_samples=[
            (_at(0), "['ch', 'fl']"),
            (_at(5), "['dhw', 'fl']"),
            (_at(10), "[]"),
            (_at(15), "[]"),
            (_at(30), "['ch', 'fl']"),
            (_at(35), "['ch']"),
        ],
        flow_temperature_samples=[(_at(10), 70.0), (_at(20), 60.0), (_at(30), 40.0)],
    )
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert episode.details["facts"]["hot_flow_tail_minutes"] == 20
    assert episode.details["inference"]["long_hot_flow_tail"] is True
    assert "не наблюдается напрямую" in episode.details["hypothesis"]
    assert _metrics(result)["dhw_long_hot_flow_tail_count"] == 1


def test_no_heating_request_is_not_misreported_as_pause() -> None:
    states = [(_at(0), "[]"), (_at(5), "['dhw', 'fl']"), (_at(10), "[]"), (_at(30), "['ch']")]
    result = _base_analysis(
        boiler_state_samples=states,
        heating_status_samples=[(_at(0), 0.0)],
        heating_worktime_samples=[(_at(0), 0.0)],
        indoor_temperature_samples=[(_at(0), 22.0)],
        heating_target_samples=[(_at(0), 21.0)],
    )
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert episode.details["inference"]["heating_demand"] == HeatingDemand.NONE
    assert _metrics(result)["dhw_confirmed_heating_pause_count"] == 0
    assert not any(event.severity == "warning" for event in result.events)


def test_concurrent_flags_are_not_double_counted_as_dhw_priority() -> None:
    result = _base_analysis(
        boiler_state_samples=[
            (_at(0), "[]"),
            (_at(5), "['ch', 'dhw', 'fl']"),
            (_at(10), "['ch', 'dhw', 'fl']"),
            (_at(15), "[]"),
        ],
    )

    assert _metrics(result)["dhw_priority_time_pct"] == 0
    assert _metrics(result)["dhw_concurrent_or_ambiguous_time_pct"] > 0
    ambiguous = next(event for event in result.events if event.kind == "dhw_concurrent_or_ambiguous")
    assert ambiguous.severity == "info"


def test_bad_quality_suppresses_long_return_alert_and_demand_inference() -> None:
    result = _base_analysis(
        quality_score=0.3,
        boiler_state_samples=[
            (_at(0), "['ch', 'fl']"),
            (_at(5), "['dhw', 'fl']"),
            (_at(10), "[]"),
            (_at(15), "[]"),
            (_at(30), "['ch', 'fl']"),
        ],
    )
    episode = next(event for event in result.events if event.kind == "dhw_reheat_episode")

    assert result.context["quality_sufficient_for_alerts"] is False
    assert episode.details["inference"]["heating_demand"] == HeatingDemand.UNKNOWN
    assert _metrics(result)["dhw_long_heating_return_count"] == 0
    assert not any(event.severity == "warning" for event in result.events)


def test_worktime_zero_alone_does_not_prove_absence_of_heating_demand() -> None:
    demand, evidence = classify_heating_demand(
        timestamp=_at(10),
        boiler_states=[],
        heating_enabled=True,
        heating_worktime_samples=[(_at(5), 0.0)],
    )

    assert demand == HeatingDemand.UNKNOWN
    assert evidence["worktime_recent"] is False


def test_metric_ids_are_stable_and_use_metric_dto() -> None:
    result = _base_analysis()
    metric = next(item for item in result.metrics if item.name == "dhw_episode_count")

    assert metric.id == "metric:day:1:dhw_episode_count:dhw-v2"
    assert metric.algorithm_version == "dhw-v2"
    assert metric.value == pytest.approx(1)

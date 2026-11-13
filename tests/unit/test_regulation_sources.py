from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zont_analyzer.application.regulation_sources import (
    load_regulation_sources,
    regulation_scopes,
    regulation_source_selections,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def row(identifier: int, source: str, entity: str, metric: str, role: str) -> dict[str, Any]:
    return {"id": identifier, "device_id": "controller", "source_type": source,
            "entity_id": entity, "metric_key": metric, "role": role}


def fixture() -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    db = MagicMock()
    db.fetch_numeric_observations.return_value = [(START, 20.), (END, 21.)]
    db.fetch_text_samples.return_value = [(START, "['ch', 'fl']"), (END, "['dhw', 'fl']")]
    devices = [{"id": "controller", "raw": {"z3k_config": {
        "heating_circuits": [{"id": "heating", "type": 3, "air_temp_sensor": "room"}],
    }}}]
    series = [row(1, "z3k_heating_circuit", "c:heating", "target_temp", "target_temperature"),
              row(2, "z3k_radio_sensor", "sensor:room", "temperature", "control_indoor_temperature"),
              row(3, "z3k_radio_sensor", "sensor:room", "humidity", "humidity"),
              row(4, "z3k_heating_circuit", "c:heating", "worktime", "heating_request"),
              row(5, "z3k_boiler_adapter", "adapter:boiler", "bt", "flow_temperature"),
              row(6, "z3k_boiler_adapter", "adapter:boiler", "cs", "target_flow_temperature"),
              row(7, "z3k_boiler_adapter", "adapter:boiler", "s", "boiler_state")]
    return db, devices, series


def test_circuit_sources_keep_endpoint_request_semantics_and_same_sensor_humidity() -> None:
    db, devices, series = fixture()
    bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={"x": 1})
    inputs, = bundle.inputs
    assert inputs.circuit_id == "heating" and inputs.device_id == "controller"
    assert inputs.request_semantics == "preceding_minute"
    assert inputs.humidity_room_identity == inputs.signals["room"].metadata.identity
    assert inputs.states[0].flags == frozenset({"ch", "fl"})
    assert inputs.states[1].flags == frozenset({"dhw", "fl"})
    assert inputs.settings_version == "unknown" and "limit" not in inputs.signals
    assert inputs.signals["command"].metadata.value_kind == "setpoint"
    assert inputs.signals["actual"].metadata.value_kind == "measurement"
    assert len(db.fetch_numeric_observations.call_args_list) == 7
    for call in db.fetch_numeric_observations.call_args_list:
        assert call.args[1:3] == (START - timedelta(hours=6), END + timedelta(seconds=61))
    assert db.fetch_text_samples.call_args.args[2] == END + timedelta(seconds=61)


def test_pure_metadata_selection_is_identical_to_loaded_binding_without_observations() -> None:
    db, devices, series = fixture()
    described = regulation_source_selections(devices, series)
    db.fetch_numeric_observations.assert_not_called()
    db.fetch_text_samples.assert_not_called()
    bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert described == bundle.selections


def test_held_setpoint_fence_covers_late_insertions_before_six_hour_lookback() -> None:
    db, devices, series = fixture()
    anchor = START - timedelta(days=10)
    db.fetch_numeric_observations.side_effect = lambda identifier, *_args, **kw: (
        [(anchor, 20.0), (END, 20.0)] if kw.get("include_previous") else [(START, 20.0), (END, 20.0)])
    with patch("zont_analyzer.application.regulation_sources.RegulationRepository") as repository:
        repository.return_value.source_fence.return_value = {"telemetry:controller": 1}
        repository.return_value.read_fence.side_effect = lambda keys: {
            key: "" if key.startswith("meta:regulation-first-") else "stable" for key in keys}
        bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={},
                                         extend_setpoint_fence=True)
    assert "meta:telemetry-day:" + anchor.date().isoformat() in bundle.source_fence
    assert "meta:telemetry-day:" + (START - timedelta(days=1)).date().isoformat() in bundle.source_fence
    assert sum(call.kwargs.get("include_previous", False)
               for call in db.fetch_numeric_observations.call_args_list) == 4


def test_unique_house_room_and_foreign_humidity_never_establish_circuit_association() -> None:
    db, devices, series = fixture()
    devices[0]["raw"]["z3k_config"]["heating_circuits"][0].pop("air_temp_sensor")
    bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert "room" not in bundle.inputs[0].signals
    assert "humidity" not in bundle.inputs[0].signals
    assert bundle.inputs[0].humidity_room_identity is None
    devices[0]["raw"]["z3k_config"]["heating_circuits"][0]["air_temp_sensor"] = "room"
    series[2]["entity_id"] = "sensor:other-room"
    bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert "room" in bundle.inputs[0].signals and "humidity" not in bundle.inputs[0].signals


def test_multiple_circuits_omit_unassociated_boiler_and_share_room_fetch() -> None:
    db, devices, series = fixture()
    devices[0]["raw"]["z3k_config"]["heating_circuits"].append(
        {"id": "other", "type": 3, "air_temp_sensor": "room"})
    series.append(row(8, "z3k_heating_circuit", "c:other", "target_temp", "target_temperature"))
    bundle = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert len(bundle.inputs) == 2
    for inputs in bundle.inputs:
        assert not inputs.states and "actual" not in inputs.signals and "command" not in inputs.signals
    assert sum(call.args[0] == 2 for call in db.fetch_numeric_observations.call_args_list) == 1
    db.fetch_text_samples.assert_not_called()


def test_scoped_fence_precedes_metadata_and_fetches() -> None:
    db, devices, series = fixture()
    trace: list[str] = []
    db.list_devices.side_effect = lambda: trace.append("devices") or devices
    db.list_series.side_effect = lambda: trace.append("series") or series
    with patch("zont_analyzer.application.regulation_sources.RegulationRepository") as repository:
        repository.return_value.source_fence.side_effect = lambda **kw: trace.append("fence") or {"day": "v1"}
        result = load_regulation_sources(db, START, END, device_ids=("controller",))
        repository.return_value.source_fence.assert_called_once_with(
            start=START - timedelta(hours=6), end=END + timedelta(seconds=61), device_ids=("controller",))
    assert trace == ["fence", "devices", "series"]
    assert result.source_fence == {"day": "v1"}


def test_metadata_requires_preexisting_fence_and_period_is_bounded() -> None:
    db, devices, series = fixture()
    with pytest.raises(ValueError, match="fence"):
        load_regulation_sources(db, START, END, devices=devices, series=series)
    with pytest.raises(ValueError, match="31 days"):
        load_regulation_sources(db, START, START + timedelta(days=32))


def test_duplicate_target_or_linked_sensor_stays_unknown() -> None:
    db, devices, series = fixture()
    series.append(row(8, "z3k_temperature", "wired:room", "temperature", "control_indoor_temperature"))
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert "room" not in result.inputs[0].signals
    series.append(row(9, "z3k_heating_circuit", "c:heating", "setpoint_temp", "target_temperature"))
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert not result.inputs


def test_missing_target_on_other_configured_consumer_does_not_prove_boiler_association() -> None:
    db, devices, series = fixture()
    devices[0]["raw"]["z3k_config"]["heating_circuits"].append({"id": "unobserved", "type": 3})
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert "actual" not in result.inputs[0].signals and not result.inputs[0].states


def test_malformed_flags_and_invalid_rows_remain_unknown_but_valid_empty_flags_remain_idle() -> None:
    db, devices, series = fixture()
    middle = START + timedelta(minutes=1)
    invalid = middle + timedelta(minutes=1)
    db.fetch_text_samples.return_value = [(START, "[]"), (middle, "broken")]
    db.fetch_numeric_observations.return_value = [(START, None), (middle, None), (invalid, None)]
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    inputs, = result.inputs
    assert len(inputs.states) == 1 and inputs.states[0].flags == frozenset()
    assert inputs.state_unknown_at == (middle, invalid)


def test_boiler_and_dhw_configured_circuits_do_not_count_as_space_heating_consumers() -> None:
    db, devices, series = fixture()
    devices[0]["raw"]["z3k_config"]["heating_circuits"].extend([
        {"id": "source", "type": 0}, {"id": "hot-water", "type": 1},
    ])
    # Even stale generic role metadata cannot make a typed DHW circuit into a
    # second heating consumer or suppress the unambiguous boiler association.
    series.extend([
        row(8, "z3k_heating_circuit", "c:source", "target_temp", "target_temperature"),
        row(9, "z3k_heating_circuit", "c:hot-water", "target_temp", "target_temperature"),
    ])
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    inputs, = result.inputs
    assert inputs.circuit_id == "heating"
    assert {"room", "actual", "command"} <= inputs.signals.keys()
    assert result.selections["controller/heating"]["boiler_association"] == (
        "single_adapter_and_single_heating_consumer_on_device")


def test_metadata_scopes_share_typed_target_and_duplicate_rules_without_data_reads() -> None:
    db, devices, series = fixture()
    devices[0]["raw"]["z3k_config"]["heating_circuits"].append({"id": "hot-water", "type": 1})
    series.append(row(8, "z3k_heating_circuit", "c:hot-water", "target_temp", "target_temperature"))
    assert regulation_scopes(devices, series) == [("controller", "heating")]
    db.fetch_numeric_observations.assert_not_called()
    db.fetch_text_samples.assert_not_called()
    result = load_regulation_sources(db, START, END, devices=devices, series=series, source_fence={})
    assert regulation_scopes(devices, series) == [(item.device_id, item.circuit_id) for item in result.inputs]
    assert result.selections["controller/heating"]["room_link_applicability"] == (
        "current_discovered_binding; historical_binding_unverified")
    series.append(row(9, "z3k_heating_circuit", "c:heating", "setpoint_temp", "target_temperature"))
    assert regulation_scopes(devices, series) == []

import pytest

from zont_analyzer.analytics.settings import control_settings


def test_only_observed_bound_circuit_fields_are_exposed() -> None:
    device = {"password": "private", "z3k_config": {
        "heating_circuits": [{"id": 1, "pza": 10, "pid_prop_koef": 10, "summer_threshold": 20,
                              "winter_summer_switch": True, "guessed_slope": 7, "gas_valve": 8},
                             {"id": 2, "pid_prop_koef": 99}],
        "pzas": [{"id": 10, "x": [2930, 2830], "y": [3030, 3100]}],
    }}
    result = control_settings(device, "1", captured_at="2026-09-07T00:00:00+00:00")
    parameters = {item["field"]: item for item in result["parameters"]}
    assert parameters["pid_prop_koef"]["value"] == 10
    assert "guessed_slope" not in parameters and "gas_valve" not in parameters
    assert parameters["summer_threshold"]["source"].endswith("[id=1].summer_threshold")
    assert all(item["user_access"] == "needs_confirmation" for item in parameters.values())
    assert result["pza_curve"]["x_raw"] == [2930, 2830]
    assert result["pza_curve"]["points_c"] == [
        {"outdoor_c": 20.0, "flow_c": 30.0}, {"outdoor_c": 10.0, "flow_c": 37.0},
    ]
    assert "private" not in str(result)
    assert result["captured_at"] == "2026-09-07T00:00:00+00:00"


def test_missing_ambiguous_and_invalid_controls_stay_unknown() -> None:
    assert control_settings({}, "1")["status"] == "unknown"
    assert control_settings({"z3k_config": {"heating_circuits": [{"id": 1}, {"id": 1}]}}, "1")["status"] == "unknown"
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "pza": 10, "summer_threshold": float("nan")},
    ], "pzas": [{"id": 10, "x": [1, 2], "y": [3, float("inf")]}]}}, "1")
    assert result["pza_curve"] is None
    assert "summer_threshold:unavailable" in result["unknowns"]


def test_pilot_pid_and_pza_match_vendor_interface_and_all_seven_points() -> None:
    result = control_settings({"z3k_config": {
        "heating_circuits": [{"id": 20496, "type": 3, "setting_register": 2561, "pza": 8259,
                              "pid_prop_koef": 10.0, "pid_integral_koef": 1.0,
                              "water_min_temperature": 30, "water_max_temperature": 65}],
        "pzas": [{"id": 8259, "x": [2930, 2830, 2730, 2630, 2530, 2430, 2330],
                  "y": [3030, 3072, 3114, 3181, 3234, 3272, 3294]}],
    }}, "20496")
    regulation = result["regulation"]
    assert regulation["status"] == "decoded"
    assert regulation["mode"] == "air_pid"
    assert regulation["enabled"] is True
    assert regulation["pza_role"] == "upper_limit"
    assert [(p["outdoor_c"], p["flow_c"]) for p in result["pza_curve"]["points_c"]] == [
        (20, 30), (10, 34.2), (0, 38.4), (-10, 45.1), (-20, 50.4), (-30, 54.2), (-40, 56.4),
    ]
    parameters = {p["field"]: p for p in result["parameters"]}
    assert parameters["pid_prop_koef"]["value"] == 10
    assert parameters["pid_integral_koef"]["value"] == 1
    assert parameters["pid_prop_koef"]["unit"] == "zont_coefficient"
    assert "snapshot_only" in result["historical_applicability"]
    assert "active_control_algorithm_not_verified" not in result["unknowns"]


@pytest.mark.parametrize(("register", "mode", "enabled", "role"), [
    (0, "air", True, "target"), (1, "air_pid", True, "upper_limit"),
    (2, "water", True, "target"), (130, "water", True, "heat_request_only"),
    (65, "air_pid", False, "upper_limit"), (3, None, True, "unknown"),
    (True, None, None, "unknown"), (-1, None, None, "unknown"), (None, None, None, "unknown"),
])
def test_mode_bits_disable_and_invalid_registers(register, mode, enabled, role) -> None:
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "type": 3, "setting_register": register, "pza": 10},
    ]}}, "1")["regulation"]
    assert (result["mode"], result["enabled"], result["pza_role"]) == (mode, enabled, role)


@pytest.mark.parametrize("pza", [0, 255, None])
def test_unassigned_pza_is_not_an_unknown_algorithm(pza) -> None:
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "type": 3, "setting_register": 1, "pza": pza},
    ]}}, "1")
    assert result["regulation"]["mode"] == "air_pid"
    assert result["regulation"]["pza_role"] == "off"
    assert "pza_curve_unavailable_or_ambiguous" not in result["unknowns"]


@pytest.mark.parametrize(("x", "y"), [
    ([20, 10], [30, 40]), ([2930, 2930], [3030, 3130]),
    ([2930, 2830, 2880], [3030, 3130, 3230]), ([2930, 2830], [3030, 65535]),
    ([True, 2830], [3030, 3130]), ([2930, 2830], [3030, float("nan")]),
])
def test_invalid_curve_cannot_be_presented_as_decoded_celsius(x, y) -> None:
    result = control_settings({"z3k_config": {
        "heating_circuits": [{"id": 1, "type": 3, "setting_register": 1, "pza": 10}],
        "pzas": [{"id": 10, "x": x, "y": y}],
    }}, "1")
    assert not result["pza_curve"] or result["pza_curve"]["points_c"] is None


def test_boiler_type_and_duplicate_curve_are_not_mistaken_for_heating_pid() -> None:
    device = {"z3k_config": {
        "heating_circuits": [{"id": 1, "type": 0, "setting_register": 1, "pza": 10}],
        "pzas": [{"id": 10, "x": [2930, 2830], "y": [3030, 3130]}] * 2,
    }}
    result = control_settings(device, "1")
    assert result["regulation"]["status"] == "not_applicable"
    assert result["regulation"]["mode"] is None
    assert result["pza_curve"] is None

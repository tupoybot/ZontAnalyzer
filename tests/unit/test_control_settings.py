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
    assert "unverified" in result["pza_curve"]["encoding"]
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

from zont_analyzer.adapters.zont_readonly.client import infer_role


def test_dhw_circuit_roles_are_distinct_from_space_heating() -> None:
    expected = {
        "target_temp": "dhw_target_temperature",
        "setpoint_temp": "dhw_setpoint_temperature",
        "worktime": "dhw_activity",
        "status": "dhw_status",
        "mode_id": "dhw_operating_mode",
    }

    for metric_key, role in expected.items():
        assert infer_role("z3k_heating_circuit", "20603", metric_key, "ГВС") == (role, 0.95)


def test_space_heating_target_role_is_unchanged() -> None:
    assert infer_role("z3k_heating_circuit", "20496", "target_temp", "Отопление") == (
        "target_temperature",
        0.9,
    )

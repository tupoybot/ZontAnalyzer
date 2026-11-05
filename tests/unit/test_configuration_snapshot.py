from copy import deepcopy

from zont_analyzer.adapters.zont_readonly.configuration import configuration_snapshot


def test_runtime_changes_leave_configuration_and_original_response_intact():
    first = {"id": "fixture", "discovered_at": "earlier", "raw": {
        "online": True, "weather": {"temperature": 5}, "last_contact": 1,
        "internet_weather": 5, "last_receive_time": 1,
        "z3k_config": {"heating_circuits": [{"id": 1, "water_max_temperature": 70}]},
        "io": {"z3k-state": {"1": {"temperature": 20, "target_temp": 22, "target_sensor_id": 2}}},
        "unknown_configuration": {"temperature": 80},
    }}
    original = deepcopy(first)
    second = deepcopy(first)
    second["discovered_at"] = "later"
    second["raw"].update(online=False, weather={"temperature": 10}, last_contact=2,
                          internet_weather=10, last_receive_time=2)
    second["raw"]["io"]["z3k-state"]["1"].update(temperature=21, target_temp=23)
    snapshot = configuration_snapshot(first)
    assert snapshot == configuration_snapshot(second)
    assert first == original
    assert snapshot["raw"]["unknown_configuration"] == {"temperature": 80}
    snapshot["raw"]["z3k_config"]["heating_circuits"][0]["water_max_temperature"] = 50
    assert first == original


def test_configuration_and_fallback_sensor_and_schedule_changes_are_retained():
    first = {"id": "fixture", "z3k_config": {"unknown_setting": 1},
             "io": {"z3k-state": {"1": {"target_sensor_id": 2, "schedule_used": False}}}}
    for change in (
        {"z3k_config": {"unknown_setting": 2}},
        {"io": {"z3k-state": {"1": {"target_sensor_id": 3, "schedule_used": False}}}},
        {"io": {"z3k-state": {"1": {"target_sensor_id": 2, "schedule_used": True, "hour": 8}}}},
    ):
        assert configuration_snapshot(first) != configuration_snapshot({**first, **change})


def test_capabilities_are_order_independent_but_configuration_arrays_are_not():
    first = {"id": "fixture", "capabilities": ["heating", "history"], "z3k_config": {"schedule": [1, 2]}}
    assert configuration_snapshot(first) == configuration_snapshot({**first, "capabilities": ["history", "heating"]})
    assert configuration_snapshot(first) != configuration_snapshot({**first, "z3k_config": {"schedule": [2, 1]}})

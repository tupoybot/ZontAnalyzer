from __future__ import annotations

from zont_analyzer.application.ingestion import _linked_indoor_sensor_ids


def test_heating_circuit_target_sensor_is_indoor_regardless_of_room_name() -> None:
    devices = [
        {
            "id": "1",
            "raw": {
                "io": {
                    "z3k-state": {
                        "200": {"target_sensor_id": 100},
                        "300": {"target_sensor_id": 101},
                    }
                }
            },
        }
    ]
    names = {("1", "200"): "Отопление", ("1", "300"): "ГВС", ("1", "100"): "Любое имя комнаты"}
    series = [
        {
            "device_id": "1",
            "source_type": "z3k_heating_circuit",
            "entity_id": "zont:1:z3k_heating_circuit:200",
            "metric_key": "target_temp",
            "display_name": "Отопление",
        },
        {
            "device_id": "1",
            "source_type": "z3k_heating_circuit",
            "entity_id": "zont:1:z3k_heating_circuit:300",
            "metric_key": "target_temp",
            "display_name": "ГВС",
        },
    ]

    assert _linked_indoor_sensor_ids(devices, names, series) == {("1", "100")}

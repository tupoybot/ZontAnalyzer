from __future__ import annotations

import json
from pathlib import Path

from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.application.ingestion import _linked_indoor_sensor_ids, _object_names

CONTRACT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "zont_contract"


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


def test_anonymized_live_contract_reproduces_sensor_links_and_return_identity() -> None:
    devices_payload = json.loads((CONTRACT_FIXTURES / "devices.json").read_text(encoding="utf-8"))
    history_payload = json.loads((CONTRACT_FIXTURES / "load_data.json").read_text(encoding="utf-8"))
    raw_device = devices_payload["devices"][0]
    devices = [{"id": str(raw_device["device_id"]), "raw": raw_device}]
    points, entities = ZontReadOnlyClient.normalize_history(history_payload["responses"][0])
    names = _object_names(devices)
    series = [
        {
            "device_id": point.device_id,
            "source_type": point.source_type,
            "entity_id": point.entity_id,
            "metric_key": point.metric_key,
            "display_name": entities[point.entity_id]["display_name"],
        }
        for point in points
    ]

    assert _linked_indoor_sensor_ids(devices, names, series) == {("100001", "30001")}
    assert names[("100001", "30002")] == "Внешний датчик обратки"
    assert any(
        point.source_type == "z3k_temperature" and point.entity_id.endswith(":30002")
        for point in points
    )
    assert infer_role(
        "z3k_temperature",
        "30002",
        "z3k_temperature",
        names[("100001", "30002")],
    ) == ("return_temperature", 0.85)

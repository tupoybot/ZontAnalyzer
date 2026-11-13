from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from zont_analyzer.adapters.ydb.owner_experiments import OwnerExperimentsRepository


class DeviceCatalog:
    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        assert query == "SELECT id,payload FROM devices;"
        return [SimpleNamespace(rows=[SimpleNamespace(id=identifier, payload=json.dumps({"raw": {
            "z3k_config": {"heating_circuits": [{"id": "circuit"}]}}})) for identifier in ("one", "two")])]


@pytest.mark.parametrize(("context", "expected"), [
    ({"equipment_profiles": [{"device_id": "one"}]}, ["one"]),
    ({"equipment_profiles": [{"device_id": "one"}, {"device_id": "two"}]}, ["one", "two"]),
    ({"equipment_profiles": [{"device_id": "one"}], "control_settings_history": {"device_id": "two"}}, ["two"]),
    ({"sensors": {"control_temperature": {"device_id": "two"}}}, ["two"]),
    ({"sensors": {"room_temperatures": [{"device_id": "one"}]}}, ["one"]),
    ({}, []),
])
def test_report_scope_does_not_expand_to_new_discovered_devices(context: dict[str, Any], expected: list[str]) -> None:
    devices = OwnerExperimentsRepository._devices(DeviceCatalog(), {"context": context})
    assert [device["id"] for device in devices] == expected

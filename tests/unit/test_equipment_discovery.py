from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.equipment import equipment_facts


def test_equipment_contract_retains_only_explicit_geographic_fields() -> None:
    device = json.loads((Path(__file__).parents[1] / "fixtures/zont_contract/equipment.json").read_text())
    with ZontReadOnlyClient(
        token="test", client_email="test@example.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"devices": [device]})),
    ) as client:
        saved = client.discover_devices()[0]
    assert saved["password"] == "***"
    assert saved["stationary_location"] == "***"
    assert saved["z3k_config"]["location"] == "***"
    facts = saved["_equipment"]
    assert facts["coordinates"]["value"] == {"latitude": 48.25, "longitude": 12.5}
    assert "stationary_location.loc" in facts["coordinates"]["source"]
    assert facts["boiler_model"]["value"] == "fixture-boiler-family"
    assert "nominal_power_kw" not in facts


@pytest.mark.parametrize("loc", [None, [], [1], [True, 2], [float("nan"), 1], [181, 1], [1, 91], "***"])
def test_unavailable_or_invalid_location_stays_unknown(loc: object) -> None:
    assert "coordinates" not in equipment_facts({"stationary_location": {"loc": loc}})


def test_ambiguous_adapters_and_diagram_location_are_not_guessed() -> None:
    assert equipment_facts({"z3k_config": {"location": {"x": 12, "y": 48}, "boiler_adapters": [
        {"id": 1, "boiler_model": "one"}, {"id": 2, "boiler_model": "two"},
    ]}}) == {}

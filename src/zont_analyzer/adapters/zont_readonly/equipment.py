"""Minimal equipment facts from the observed devices contract, before redaction."""
from __future__ import annotations

import math
from typing import Any


def equipment_facts(device: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    location = device.get("stationary_location")
    coordinates = location.get("loc") if isinstance(location, dict) else None
    # ZONT geographic pairs use longitude, latitude. z3k_config.location is
    # diagram placement and must never be interpreted as geographic coordinates.
    if (isinstance(coordinates, list) and len(coordinates) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in coordinates)):
        longitude, latitude = coordinates
        if -180 <= longitude <= 180 and -90 <= latitude <= 90:
            facts["coordinates"] = {
                "value": {"latitude": latitude, "longitude": longitude},
                "source": "zont:stationary_location.loc [longitude,latitude]",
            }
    config = device.get("z3k_config")
    adapters = config.get("boiler_adapters") if isinstance(config, dict) else None
    if isinstance(adapters, list):
        models = [a for a in adapters if isinstance(a, dict) and isinstance(a.get("boiler_model"), str)
                  and a["boiler_model"].strip() and len(a["boiler_model"]) <= 200]
        # Do not guess which of several adapters heats the house.
        if len(models) == 1:
            adapter = models[0]
            facts["boiler_model"] = {
                "value": adapter["boiler_model"].strip(),
                "source": f"zont:z3k_config.boiler_adapters[id={adapter.get('id', 'unknown')}].boiler_model",
            }
    return facts

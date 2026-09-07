"""Join bounded heating history and current read-only configuration for reasoning."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from zont_analyzer.adapters.sqlite.database import Database
from zont_analyzer.analytics.heating import build_heating_evidence
from zont_analyzer.analytics.settings import control_settings
from zont_analyzer.application.comparison_context import daily_history


def heating_context(db: Database, context: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    sensor = context.get("sensors", {}).get("control_temperature") or {}
    device_id = str(sensor.get("device_id", ""))
    series = db.list_series()
    targets = [item for item in series if item["role"] == "target_temperature"
               and (not device_id or str(item["device_id"]) == device_id)]
    target = targets[0] if len(targets) == 1 else None
    if target:
        device_id = str(target["device_id"])
    devices = [item for item in db.list_devices() if str(item["id"]) == device_id]
    device = devices[0] if len(devices) == 1 else {}
    circuit_id = str(target["entity_id"]).rsplit(":", 1)[-1] if target else ""
    settings = context.get("control_settings") or control_settings(
        device.get("raw", {}), circuit_id, captured_at=device.get("discovered_at"),
    )
    coordinates = device.get("raw", {}).get("_equipment", {}).get("coordinates", {})
    # Historical configuration is never replaced by the latest snapshot. Location
    # is current site context for solar timing, explicitly dated and sourced.
    history = daily_history(db, start - timedelta(days=90), start, limit=32)
    if end - start > timedelta(days=31):
        history += daily_history(db, start, end, limit=32)
    temporal = context.get("temporal_evidence", {})
    source_windows = temporal.pop("heating_source_windows", None)
    heating_source = temporal | {"windows": source_windows} if source_windows is not None else temporal
    evidence = build_heating_evidence(
        heating_source, history,
        coordinates=coordinates.get("value"), max_windows=8,
    ).as_dict()
    evidence.update({
        "algorithm_version": "heating-v1", "epistemic_level": "derived",
        "location_source": coordinates.get("source", "unavailable"),
        "location_captured_at": device.get("discovered_at"),
        "history_selection": "At most 32 evenly spread daily reports per 90-day baseline/long period; not a census",
        "interpretation_limits": (
            "Matched telemetry mode/target does not prove unchanged PZA/PID, occupancy or equipment. "
            "Sunrise is calculated timing, not measured insolation. Compare interventions and house context."
        ),
    })
    return {"control_settings": settings, "heating_analysis": evidence}

"""Deterministic settings evidence refresh, with no analyst/API dependency."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from zont_analyzer.domain import Report

from .reasoning_context import report_facts_fingerprint


def ensure_settings_history(
    db: Any, context: dict[str, Any], start: datetime, end: datetime,
) -> dict[str, Any]:
    """Supply long-period history once, using an unambiguous stable binding."""
    if "control_settings_history" in context:
        return {}
    sensor = context.get("sensors", {}).get("control_temperature") or {}
    device_id = str(sensor.get("device_id", ""))
    targets = [item for item in db.list_series() if item["role"] == "target_temperature"
               and (not device_id or str(item["device_id"]) == device_id)]
    if len(targets) == 1:
        target = targets[0]
        device_id = str(target["device_id"])
        circuit_id = str(target["entity_id"]).rsplit(":", 1)[-1]
        history = db.control_settings_history(device_id, circuit_id, start, end)
    else:
        from zont_analyzer.analytics.settings_history import settings_timeline

        history = settings_timeline([], device_id=device_id, circuit_id="", start=start, end=end, incomplete=True)
    return {"control_settings_history": history}


def refresh_settings_history(db: Any, report: Report) -> Report:
    history = report.context.get("control_settings_history", {})
    device_id, circuit_id = history.get("device_id"), history.get("circuit_id")
    if not device_id or not circuit_id:
        # Legacy reports did not save an explicit device binding alongside the
        # snapshot. Only an unambiguous device/profile and stable circuit ID may
        # be used; never guess by circuit display name or list position.
        settings_id = report.context.get("control_settings", {}).get("id", "")
        profiles = report.context.get("equipment_profiles", [])
        if not settings_id.startswith("settings:") or len(profiles) != 1:
            return report
        device_id, circuit_id = profiles[0].get("device_id"), settings_id.removeprefix("settings:")
        if not device_id or not circuit_id:
            return report
    current = db.control_settings_history(device_id, circuit_id, report.period_start, report.period_end)
    if current == history or (current.get("fingerprint") and current.get("fingerprint") == history.get("fingerprint")):
        return report
    result = report.model_copy(deep=True)
    result.context["control_settings_history"] = current
    if report.ai_used:
        baseline = report.context.get("ai_facts_fingerprint")
        reused = report.context.get("pilot_ai_reuse") or report.context.get("ai_interpretation_reuse")
        if (not baseline and not reused and not report.context.get("gas", {}).get("ai_stale")
                and not report.context.get("control_settings_interpretation_stale")):
            baseline = report_facts_fingerprint(report)
        if baseline:
            result.context["ai_facts_fingerprint"] = baseline
        if baseline is None or report_facts_fingerprint(result) != baseline:
            result.context["control_settings_interpretation_stale"] = True
    return result


def report_depends_on_device(report: Report, device_id: str, circuit_ids: list[str] | None = None) -> bool:
    history = report.context.get("control_settings_history", {})
    circuit_id = history.get("circuit_id")
    if circuit_ids and circuit_id and circuit_id not in circuit_ids:
        return False
    if history.get("device_id"):
        return bool(history["device_id"] == device_id)
    profiles = report.context.get("equipment_profiles", [])
    identifiers = {str(item["device_id"]) for item in profiles if item.get("device_id")}
    return device_id in identifiers if identifiers else True

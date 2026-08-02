from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import DetectedEvent


def _mode_intent(name: str, *, heating_enabled: bool) -> str:
    normalized = name.casefold()
    if not heating_enabled:
        return "heating_off"
    if any(word in normalized for word in ("комфорт", "comfort")):
        return "comfort"
    if any(word in normalized for word in ("эконом", "eco")):
        return "economy"
    if any(word in normalized for word in ("дач", "away", "отъезд", "отпуск")):
        return "away"
    if any(word in normalized for word in ("межсез", "shoulder")):
        return "shoulder_season"
    return "custom_active"


def build_mode_catalog(
    devices: list[dict[str, Any]],
    *,
    device_id: str,
    circuit_id: str,
) -> dict[int, dict[str, Any]]:
    device = next((item for item in devices if str(item.get("id")) == device_id), None)
    if device is None:
        return {}
    config = device.get("raw", {}).get("z3k_config", {})
    mode_states = device.get("raw", {}).get("io", {}).get("z3k-state", {})
    if not isinstance(config, dict):
        return {}
    timetables = {
        int(item["id"]): item
        for item in config.get("interval_timetables", [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    intervals = {
        int(item["id"]): item
        for item in config.get("time_intervals", [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    catalog: dict[int, dict[str, Any]] = {}
    for item in config.get("heating_modes", []):
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        zone = next(
            (
                candidate
                for candidate in item.get("heating_zones", [])
                if isinstance(candidate, dict) and str(candidate.get("heating_circuit")) == circuit_id
            ),
            None,
        )
        if zone is None:
            continue
        setting = int(zone.get("temperature_setting") or 0)
        timetable = timetables.get(setting)
        schedule: list[dict[str, Any]] = []
        if timetable:
            for interval_id in timetable.get("time_intervals", []):
                interval = intervals.get(int(interval_id))
                if interval:
                    schedule.append(
                        {
                            "weekdays_mask": int(interval.get("action_register") or 0),
                            "start": f"{int(interval.get('sh') or 0):02d}:{int(interval.get('sm') or 0):02d}",
                            "end": f"{int(interval.get('eh') or 0):02d}:{int(interval.get('em') or 0):02d}",
                            "inside_mode_id": int(interval.get("temperature") or 0),
                        }
                    )
        heating_enabled = setting != 0
        name = str(item.get("name") or f"mode-{item['id']}")
        mode_state = mode_states.get(str(item["id"]), {}) if isinstance(mode_states, dict) else {}
        selection_schedule_enabled = bool(mode_state.get("schedule_used")) if isinstance(mode_state, dict) else False
        catalog[int(item["id"])] = {
            "id": int(item["id"]),
            "name": name,
            "heating_enabled": heating_enabled,
            "target_policy": "scheduled" if timetable else "fixed" if heating_enabled else "off",
            "intent": _mode_intent(name, heating_enabled=heating_enabled),
            "schedule": schedule,
            "outside_schedule_mode_id": int(timetable.get("outside_default") or 0) if timetable else None,
            "selection_schedule_enabled": selection_schedule_enabled,
            "selection_schedule_time": (
                f"{int(mode_state.get('hour') or 0):02d}:{int(mode_state.get('minute') or 0):02d}"
                if selection_schedule_enabled
                else None
            ),
        }
    return catalog


def build_heating_circuit_config(
    devices: list[dict[str, Any]],
    *,
    device_id: str,
    circuit_id: str,
) -> dict[str, Any]:
    device = next((item for item in devices if str(item.get("id")) == device_id), None)
    if device is None:
        return {}
    circuits = device.get("raw", {}).get("z3k_config", {}).get("heating_circuits", [])
    circuit = next(
        (item for item in circuits if isinstance(item, dict) and str(item.get("id")) == circuit_id),
        None,
    )
    if circuit is None:
        return {}
    threshold = circuit.get("summer_threshold")
    hysteresis = circuit.get("hysteresis")
    return {
        "id": int(circuit["id"]),
        "name": str(circuit.get("name") or circuit_id),
        "circuit_type": int(circuit["type"]) if circuit.get("type") is not None else None,
        "hysteresis_c": float(hysteresis) if hysteresis is not None else None,
        "automatic_summer_mode_enabled": bool(circuit.get("winter_summer_switch")),
        "summer_threshold_c": float(threshold) if threshold is not None else None,
    }


# ZONT encodes the circuit's automatic summer state as bit 7 of z3k_heating_circuit.status.
# This is distinct from mode_id: the selected user mode does not change when this bit toggles.
_AUTOMATIC_SUMMER_STATUS_MASK = 1 << 7


def detect_heating_availability(
    *,
    start: datetime,
    end: datetime,
    mode_samples: list[tuple[datetime, float]],
    status_samples: list[tuple[datetime, float]],
    mode_catalog: dict[int, dict[str, Any]],
    circuit_config: dict[str, Any],
    period_id: str,
) -> tuple[list[DetectedEvent], dict[str, Any], list[tuple[datetime, datetime]]]:
    ordered_modes = sorted(mode_samples)
    ordered_statuses = sorted(status_samples)
    automatic_enabled = bool(circuit_config.get("automatic_summer_mode_enabled"))

    def mode_at(timestamp: datetime) -> int | None:
        return _mode_at(ordered_modes, timestamp)

    def summer_at(timestamp: datetime) -> bool | None:
        current: bool | None = None
        for sample_time, value in ordered_statuses:
            if sample_time > timestamp:
                break
            current = bool(int(value) & _AUTOMATIC_SUMMER_STATUS_MASK)
        return current

    boundaries = {start, end}
    boundaries.update(timestamp for timestamp, _value in ordered_modes if start < timestamp < end)
    boundaries.update(timestamp for timestamp, _value in ordered_statuses if start < timestamp < end)
    ordered_boundaries = sorted(boundaries)
    inactive_windows: list[tuple[datetime, datetime]] = []
    inactive_reasons: set[str] = set()
    inactive_seconds = 0.0
    for window_start, window_end in zip(ordered_boundaries, ordered_boundaries[1:], strict=False):
        selected_mode_id = mode_at(window_start)
        selected_mode = mode_catalog.get(selected_mode_id) if selected_mode_id is not None else None
        selected_mode_disabled = selected_mode is not None and not bool(selected_mode.get("heating_enabled", True))
        automatic_summer_active = automatic_enabled and summer_at(window_start) is True
        if selected_mode_disabled or automatic_summer_active:
            inactive_windows.append((window_start, window_end))
            inactive_seconds += (window_end - window_start).total_seconds()
            if selected_mode_disabled:
                inactive_reasons.add("selected_mode_disables_circuit")
            if automatic_summer_active:
                inactive_reasons.add("automatic_summer_mode")

    events: list[DetectedEvent] = []
    previous_summer = summer_at(start)
    if automatic_enabled:
        for timestamp, value in ordered_statuses:
            if not start < timestamp < end:
                continue
            active = bool(int(value) & _AUTOMATIC_SUMMER_STATUS_MASK)
            if previous_summer is not None and active != previous_summer:
                kind = "automatic_summer_mode_entered" if active else "automatic_summer_mode_exited"
                events.append(
                    DetectedEvent(
                        id=f"event:{period_id}:{kind}:{int(timestamp.timestamp())}:events-v2",
                        kind=kind,
                        started_at=timestamp,
                        ended_at=timestamp,
                        severity="info",
                        details={
                            "source": "heating_circuit_status",
                            "summer_threshold_c": circuit_config.get("summer_threshold_c"),
                            "selected_mode_id": mode_at(timestamp),
                        },
                    )
                )
            previous_summer = active

    current_mode_id = mode_at(end)
    current_mode = mode_catalog.get(current_mode_id) if current_mode_id is not None else None
    current_summer = summer_at(end)
    current_mode_disabled = current_mode is not None and not bool(current_mode.get("heating_enabled", True))
    context = {
        **circuit_config,
        "automatic_summer_mode_active": current_summer if automatic_enabled else False,
        "automatic_summer_state_observed": current_summer is not None,
        "selected_mode_disables_circuit": current_mode_disabled,
        "space_heating_available": not current_mode_disabled and not (automatic_enabled and current_summer is True),
        "inactive_time_pct": round(inactive_seconds / max((end - start).total_seconds(), 1) * 100, 3),
        "inactive_reasons": sorted(inactive_reasons),
    }
    return events, context, inactive_windows


def _mode_at(samples: list[tuple[datetime, float]], timestamp: datetime) -> int | None:
    current: int | None = None
    for sample_time, value in samples:
        if sample_time > timestamp:
            break
        current = int(value)
    return current


def _near_schedule_boundary(timestamp: datetime, mode: dict[str, Any] | None, timezone: str) -> bool:
    if not mode or mode.get("target_policy") != "scheduled":
        return False
    local = timestamp.astimezone(ZoneInfo(timezone))
    weekday_bit = 1 << local.weekday()
    for interval in mode.get("schedule", []):
        if not int(interval.get("weekdays_mask", 0)) & weekday_bit:
            continue
        for value in (str(interval["start"]), str(interval["end"])):
            boundary_time = time.fromisoformat(value)
            boundary = local.replace(
                hour=boundary_time.hour,
                minute=boundary_time.minute,
                second=0,
                microsecond=0,
            )
            if abs((local - boundary).total_seconds()) <= 5 * 60:
                return True
    return False


def _near_mode_selection_schedule(timestamp: datetime, mode: dict[str, Any], timezone: str) -> bool:
    if not mode.get("selection_schedule_enabled") or not mode.get("selection_schedule_time"):
        return False
    local = timestamp.astimezone(ZoneInfo(timezone))
    boundary_time = time.fromisoformat(str(mode["selection_schedule_time"]))
    boundary = local.replace(
        hour=boundary_time.hour,
        minute=boundary_time.minute,
        second=0,
        microsecond=0,
    )
    return abs((local - boundary).total_seconds()) <= 5 * 60


def detect_control_context(
    *,
    mode_samples: list[tuple[datetime, float]],
    target_samples: list[tuple[datetime, float]],
    mode_catalog: dict[int, dict[str, Any]],
    period_id: str,
    timezone: str,
    transition_minutes: int = 120,
) -> tuple[list[DetectedEvent], dict[str, Any], list[tuple[datetime, datetime]]]:
    events: list[DetectedEvent] = []
    transition_windows: list[tuple[datetime, datetime]] = []
    ordered_modes = sorted(mode_samples)
    ordered_targets = sorted(target_samples)
    mode_change_times: list[datetime] = []

    previous_mode: int | None = None
    observed_mode_ids: set[int] = set()
    for timestamp, value in ordered_modes:
        mode_id = int(value)
        observed_mode_ids.add(mode_id)
        if previous_mode is not None and mode_id != previous_mode:
            before = mode_catalog.get(previous_mode, {"id": previous_mode, "name": f"mode-{previous_mode}"})
            after = mode_catalog.get(mode_id, {"id": mode_id, "name": f"mode-{mode_id}"})
            source = "scheduled" if _near_mode_selection_schedule(timestamp, after, timezone) else "likely_manual"
            events.append(
                DetectedEvent(
                    id=f"event:{period_id}:heating_mode_change:{int(timestamp.timestamp())}:events-v2",
                    kind="heating_mode_change",
                    started_at=timestamp,
                    ended_at=timestamp,
                    severity="info",
                    details={
                        "source": source,
                        "from_mode_id": previous_mode,
                        "from_mode_name": before["name"],
                        "to_mode_id": mode_id,
                        "to_mode_name": after["name"],
                        "to_mode_intent": after.get("intent", "unknown"),
                        "heating_enabled": after.get("heating_enabled"),
                    },
                )
            )
            mode_change_times.append(timestamp)
            transition_windows.append((timestamp, timestamp + timedelta(minutes=transition_minutes)))
        previous_mode = mode_id

    previous_target: float | None = None
    for timestamp, target in ordered_targets:
        if previous_target is not None and abs(target - previous_target) > 0.01:
            target_mode_id = _mode_at(ordered_modes, timestamp)
            mode = mode_catalog.get(target_mode_id) if target_mode_id is not None else None
            follows_mode_change = any(
                abs((timestamp - changed).total_seconds()) <= 5 * 60 for changed in mode_change_times
            )
            source = (
                "mode_change"
                if follows_mode_change
                else "scheduled"
                if _near_schedule_boundary(timestamp, mode, timezone)
                else "likely_manual"
            )
            events.append(
                DetectedEvent(
                    id=f"event:{period_id}:target_temperature_change:{int(timestamp.timestamp())}:events-v2",
                    kind="target_temperature_change",
                    started_at=timestamp,
                    ended_at=timestamp,
                    severity="info",
                    details={
                        "source": source,
                        "from_target_c": round(previous_target, 3),
                        "to_target_c": round(target, 3),
                        "mode_id": target_mode_id,
                        "mode_name": mode.get("name") if mode else None,
                    },
                )
            )
            transition_windows.append((timestamp, timestamp + timedelta(minutes=transition_minutes)))
        previous_target = target

    current_mode_id = int(ordered_modes[-1][1]) if ordered_modes else None
    current_mode = mode_catalog.get(current_mode_id) if current_mode_id is not None else None
    context = {
        "current_mode": current_mode,
        "current_mode_id": current_mode_id,
        "current_target_c": ordered_targets[-1][1] if ordered_targets else None,
        "observed_modes": [
            mode_catalog.get(mode_id, {"id": mode_id, "name": f"mode-{mode_id}"})
            for mode_id in sorted(observed_mode_ids)
        ],
        "mode_change_count": sum(event.kind == "heating_mode_change" for event in events),
        "target_change_count": sum(event.kind == "target_temperature_change" for event in events),
        "transition_window_minutes": transition_minutes,
    }
    return events, context, transition_windows

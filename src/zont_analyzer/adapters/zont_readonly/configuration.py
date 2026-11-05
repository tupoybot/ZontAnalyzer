"""Separate observed configuration from the latest device runtime response.

The full response still belongs in ``devices``. Never apply these exclusions to
that catalogue or to collected telemetry. Unknown top-level fields and the whole
z3k_config are retained conservatively; no recursive name-based filtering occurs.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

_RUNTIME_FIELDS = frozenset({
    "discovered_at", "online", "last_contact", "last_connect", "last_disconnect",
    "last_activity", "last_seen", "last_receive_time", "server_time", "current_time",
    "weather", "internet_weather",
})
# These observed IO fields are used as configuration fallbacks by ingestion and
# the mode catalogue. Temperatures, targets, flags and other IO remain runtime.
_IO_SETTINGS = frozenset({"target_sensor_id", "schedule_used", "hour", "minute"})


def configuration_snapshot(device: dict[str, Any]) -> dict[str, Any]:
    """Return a detached, timestamp-independent configuration observation."""
    result = deepcopy({key: value for key, value in device.items() if key not in _RUNTIME_FIELDS})
    raw = result.get("raw")
    if isinstance(raw, dict):
        result["raw"] = _configuration(raw)
    else:
        result = _configuration(result)
    return result


def _configuration(raw: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in raw.items() if key not in _RUNTIME_FIELDS and key != "io"}
    # Capability order changes between identical discoveries. It is a set of
    # feature names, unlike the order-sensitive schedules/configuration arrays.
    capabilities = result.get("capabilities")
    if isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities):
        result["capabilities"] = sorted(capabilities)
    io = raw.get("io")
    if isinstance(io, dict):
        states = io.get("z3k-state")
        if isinstance(states, dict):
            settings = {
                key: {field: value for field, value in state.items() if field in _IO_SETTINGS}
                for key, state in states.items() if isinstance(state, dict)
            }
            settings = {key: value for key, value in settings.items() if value}
            if settings:
                result["io"] = {"z3k-state": settings}
    return result

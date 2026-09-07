"""Resolve the display timezone from the read-only ZONT configuration."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zont_analyzer.config import AppConfig

if TYPE_CHECKING:
    from zont_analyzer.adapters.sqlite import Database

_MIN_OFFSET = -12
_MAX_OFFSET = 14


def _offset_timezone(offset: int) -> str:
    if offset == 0:
        return "UTC"
    # Etc/GMT uses the POSIX opposite sign convention.
    return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def _device_offset(raw: dict[str, Any]) -> tuple[int | None, str | None]:
    if "timezone" in raw:
        value = raw["timezone"]
        field = "timezone"
    else:
        config = raw.get("z3k_config")
        value = config.get("timezone") if isinstance(config, dict) else None
        field = "z3k_config.timezone"
    if isinstance(value, bool) or not isinstance(value, int):
        return None, field
    if not _MIN_OFFSET <= value <= _MAX_OFFSET:
        return None, field
    return value, field


def apply_device_timezone(db: Database, config: AppConfig) -> dict[str, Any]:
    """Apply the common ZONT offset to ``config.home`` and return provenance.

    ZONT exposes an integer UTC offset. We only apply it when every discovered
    device has a valid, identical value; otherwise the configured IANA zone is
    retained explicitly as a fallback.
    """
    devices = db.list_devices()
    observations: list[dict[str, Any]] = []
    invalid: list[str] = []
    for device in devices:
        device_id = str(device.get("id", "unknown"))
        raw = device.get("raw")
        if not isinstance(raw, dict):
            invalid.append(device_id)
            continue
        offset, field = _device_offset(raw)
        if offset is None:
            invalid.append(device_id)
        else:
            observations.append({"device_id": device_id, "field": field, "offset": offset})

    offsets = {item["offset"] for item in observations}
    reason: str | None = None
    if invalid:
        reason = "invalid_or_missing_zont_timezone"
    elif len(offsets) != 1:
        reason = "zont_timezone_conflict" if len(offsets) > 1 else "zont_timezone_missing"

    if reason is None:
        offset = next(iter(offsets))
        timezone = _offset_timezone(offset)
        config.home._zont_timezone = timezone
        provenance = {
            "source": "zont",
            "timezone": timezone,
            "offset_hours": offset,
            "devices": [item["device_id"] for item in observations],
            "fields": sorted({item["field"] for item in observations}),
        }
    else:
        config.home._zont_timezone = None
        timezone = config.home.timezone
        provenance = {
            "source": "configuration_fallback",
            "timezone": timezone,
            "reason": reason,
            "devices": [item["device_id"] for item in observations] + invalid,
        }
    config.home._timezone_provenance = provenance
    return provenance

"""Contracts for owner-recorded manual controller interventions.

These records describe what the owner says was changed.  They never imply that
the application wrote to a controller or that a later configuration snapshot
was present at the time of the change.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator

from .models import DomainModel

ExperimentCategory = Literal["settings", "firmware_update", "firmware_rollback", "other"]


def canonical_json_value(value: Any) -> Any:
    """Return a JSON-safe value, rejecting opaque objects at the API boundary."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("experiment values must be JSON-compatible") from exc
    return json.loads(encoded)


class Experiment(DomainModel):
    category: ExperimentCategory | None = None
    parameter: str | None = Field(default=None, max_length=200)
    before: Any | None = None
    after: Any | None = None
    performed_at: datetime | None = None

    @field_validator("performed_at")
    @classmethod
    def performed_at_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("performed_at must include a timezone offset")
        return value.astimezone(UTC) if value is not None else None

    @field_validator("before", "after")
    @classmethod
    def values_must_be_json_compatible(cls, value: Any) -> Any:
        normalized = canonical_json_value(value)
        if isinstance(normalized, str) and len(normalized) > 500:
            raise ValueError("experiment text values must not exceed 500 characters")
        return normalized

    @field_validator("parameter")
    @classmethod
    def parameter_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("parameter must not be blank")
        return value

    def storage_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def control_snapshot(device: dict[str, Any]) -> dict[str, Any] | None:
    """Extract only known control branches from a read-only discovery payload.

    ZONT device payloads vary by controller.  The extraction is deliberately
    conservative: it preserves only verified control branches from the ZONT
    discovery contract and reports no snapshot when none are present.
    """
    selected: dict[str, Any] = {}
    control_branches = {
        "heating_circuit", "heating_circuits", "heating_modes", "pza", "pzas", "pid",
        "interval_timetables", "time_intervals", "temp_step", "отопление", "контур_отопления",
    }

    def walk(value: Any, path: tuple[str, ...], inherited: bool = False) -> None:
        current = inherited or any(segment.lower().replace("-", "_") in control_branches for segment in path)
        if isinstance(value, dict):
            for key, child in sorted(value.items(), key=lambda item: str(item[0])):
                walk(child, (*path, str(key)), current)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, str(index)), current)
        elif current:
            selected[".".join(path)] = canonical_json_value(value)

    config = device.get("z3k_config")
    if isinstance(config, dict):
        walk(config, ("z3k_config",))
    return {"fields": selected} if selected else None


def snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

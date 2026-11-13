"""Contracts for owner-recorded manual controller interventions.

These records describe what the owner says was changed.  They never imply that
the application wrote to a controller or that a later configuration snapshot
was present at the time of the change.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

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


class ParameterChange(DomainModel):
    device_id: str = Field(min_length=1, max_length=200)
    circuit_id: str | None = Field(default=None, max_length=200)
    parameter: str = Field(min_length=1, max_length=200)
    before: str = Field(max_length=500)
    after: str = Field(max_length=500)


class OwnerExperiment(DomainModel):
    category: ExperimentCategory = "settings"
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    experiment_id: str | None = Field(default=None, max_length=100)
    expected_revision: int = Field(default=0, ge=0)
    recommendation_id: str | None = None
    state: Literal["planned", "performed", "completed", "stopped", "cancelled", "rollback_planned", "rolled_back"]
    changes: list[ParameterChange] = Field(min_length=1, max_length=20)
    performed_at: datetime | None = None
    goal: str = Field(min_length=1, max_length=2000)
    expected_effect: str = Field(max_length=2000)
    observation_window: str = Field(min_length=1, max_length=500)
    comment: str = Field(default="", max_length=2000)
    rollback_at: datetime | None = None

    @field_validator("performed_at", "rollback_at")
    @classmethod
    def aware_times(cls, value: datetime | None) -> datetime | None:
        return Experiment.performed_at_must_include_timezone(value)

    @model_validator(mode="after")
    def consistent_times(self) -> OwnerExperiment:
        if self.state in {"planned", "cancelled"} and self.performed_at is not None:
            raise ValueError("План и отмена не означают выполненное изменение.")
        if self.state != "rolled_back" and self.rollback_at is not None:
            raise ValueError("Время отката доступно только для выполненного отката.")
        if self.performed_at and self.rollback_at and self.rollback_at < self.performed_at:
            raise ValueError("Откат не может предшествовать изменению.")
        return self


def linked_action_identity(item: dict[str, Any], *, device_id: str | None = None) -> str | None:
    """Match only fully scoped, dated actions explicitly linked to one recommendation."""
    rec = item.get("recommendation_id")
    experiment = item.get("experiment") or {}
    when = experiment.get("performed_at")
    if not rec or not when:
        return None
    changes = experiment.get("changes")
    if changes is None:
        if not device_id or not experiment.get("parameter"):
            return None
        changes = [{"device_id": device_id, "circuit_id": experiment.get("circuit_id"),
                    **{key: experiment.get(key) for key in ("parameter", "before", "after")}}]

    def value(raw: Any) -> tuple[str, str]:
        if experiment.get("category") == "settings" and not isinstance(raw, bool) and raw is not None:
            try:
                numeric = Decimal(str(raw))
                if numeric.is_finite():
                    parts = numeric.as_tuple()
                    digits = list(parts.digits)
                    exponent = int(parts.exponent)
                    while digits and digits[-1] == 0:
                        digits.pop()
                        exponent += 1
                    return "number", repr((parts.sign, digits, exponent) if digits else (0, [], 0))
            except InvalidOperation:
                pass
        return "json", json.dumps(raw, ensure_ascii=False, sort_keys=True)

    normalized = []
    for change in changes:
        if not change.get("device_id") or not change.get("parameter"):
            return None
        normalized.append((str(change["device_id"]), change.get("circuit_id"),
                           str(change["parameter"]).strip(), value(change.get("before")), value(change.get("after"))))
    return json.dumps((rec, datetime.fromisoformat(when).astimezone(UTC).isoformat(),
                       experiment.get("category"), sorted(normalized, key=repr)), ensure_ascii=False)


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

"""Versioned, bounded observations of control settings; never device write commands."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from .settings import _FIELDS

CONTRACT = "control-history-v1"


def _hash_numbers(value: Any) -> Any:
    """Normalize JSON number spelling for hashing, preserving raw values and booleans."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _hash_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_numbers(item) for item in value]
    return value


def fingerprint(value: Any) -> str:
    return CONTRACT + ":" + hashlib.sha256(json.dumps(
        _hash_numbers(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def semantic_history(value: dict[str, Any]) -> dict[str, Any]:
    """Confirmation endpoints affect facts through applicable boundaries only.

    Preserve their actual timestamps in the report, while a later unchanged poll
    cannot change a fixed period's evidence or downstream cache identity.
    """
    result = {key: item for key, item in value.items() if key != "fingerprint"}
    start = value.get("period_start")
    left = None
    if isinstance(start, str):
        try:
            parsed = datetime.fromisoformat(start)
            if parsed.tzinfo is not None:
                left = int(parsed.timestamp() * 1_000_000)
        except ValueError:
            pass
    versions = []
    for version in value.get("versions", []):
        item = {key: field for key, field in version.items()
                if key not in {"last_confirmed_at", "last_observed_at"}}
        observed, boundary = version.get("observed_at"), version.get("boundary") or {}
        upper, lower = boundary.get("no_later_than"), boundary.get("after")
        if (left is not None and type(observed) is int and observed <= left
                and type(upper) is int and upper <= left and (lower is None or type(lower) is int and lower <= left)):
            # Equal legacy snapshots are folded into their first representative.
            # A wider input query can select an older representative without
            # changing the state known at this period's start. Canonicalize that
            # expired provenance only; raw observations and overlapping change
            # boundaries remain intact in the actual history packet.
            item.update(event_id="observed_before_period", kind="observed_before_period", observed_at=left,
                        changed_fields=[], boundary={"after": None, "no_later_than": left,
                                                     "exact_effective_at": None})
        versions.append(item)
    result["versions"] = versions
    return result


def _identity(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _field(item: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in item:
        return {"status": "missing"}
    value = item[key]
    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if key == "winter_summer_switch":
        valid = valid or isinstance(value, bool)
    if key in {"type", "setting_register", "pza"}:
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return {"status": "observed", "value": value} if valid else {"status": "invalid"}


def observed_controls(device: dict[str, Any], *, known: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """Compare stable circuit identities, allowing only supported control fields.

    Circuit collection order and object key order are immaterial. Curve coordinate
    order is meaningful. Missing/invalid fields are states, never zero defaults.
    """
    raw = device.get("raw", device)
    config = raw.get("z3k_config") if isinstance(raw, dict) else None
    circuits = config.get("heating_circuits") if isinstance(config, dict) else None
    grouped: dict[str, list[dict[str, Any]]] = {}
    if isinstance(circuits, list):
        for item in circuits:
            identifier = _identity(item.get("id")) if isinstance(item, dict) else None
            if identifier is not None:
                grouped.setdefault(identifier, []).append(item)
    result: dict[str, dict[str, Any]] = {}
    for identifier in sorted(set(grouped) | (known or set())):
        matches = grouped.get(identifier, [])
        if len(matches) != 1:
            result[identifier] = {"status": "ambiguous" if matches else "missing" if isinstance(circuits, list)
                                  else "invalid_or_missing_configuration"}
            continue
        circuit = matches[0]
        fields = {key: _field(circuit, key) for key in (*_FIELDS, "type", "setting_register")}
        curves = config.get("pzas") if isinstance(config, dict) else None
        selected = [curve for curve in curves if isinstance(curve, dict)
                    and _identity(curve.get("id")) == _identity(circuit.get("pza"))] if isinstance(curves, list) else []
        if circuit.get("pza") in (0, 255, None):
            curve_state: dict[str, Any] = {"status": "unassigned"}
        elif len(selected) != 1:
            curve_state = {"status": "ambiguous" if selected else "missing"}
        else:
            curve = selected[0]
            x, y = curve.get("x"), curve.get("y")
            valid = (isinstance(x, list) and isinstance(y, list) and 1 < len(x) == len(y) <= 32
                     and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                             for v in x + y))
            curve_state = {"status": "observed", "x": x, "y": y} if valid else {"status": "invalid"}
        fields["pza_curve"] = curve_state
        result[identifier] = {"status": "observed", "fields": fields}
    return result


def settings_timeline(
    observations: list[dict[str, Any]], *, device_id: str, circuit_id: str,
    start: datetime, end: datetime, incomplete: bool = False,
) -> dict[str, Any]:
    """Project observed versions and uncertain boundaries for a half-open period.

    First observations never establish a setting before that observation. Even a
    repeated confirmation establishes observations, not uninterrupted operation.
    """
    expanded = []
    for row in observations:
        confirmed = row.get("last_confirmed_at")
        expanded.append({**row, "sequence": row.get("sequence", row["event_id"]),
                         "last_confirmed_at": row["observed_at"] if confirmed is not None else None})
        if confirmed is not None and confirmed > row["observed_at"]:
            expanded.append({**row, "sequence": row.get("sequence", row["event_id"]), "observed_at": confirmed,
                             "event_id": f"{row['event_id']}:confirmed:{confirmed}", "boundary": None})
    versions: list[dict[str, Any]] = []
    for row in sorted(expanded, key=lambda item: (
        item["observed_at"], item["sequence"], item.get("ordering_rank", 0),
    )):
        state = row["state"]
        state_version = fingerprint(state)
        at = row["observed_at"]
        if versions and versions[-1]["version"] == state_version:
            # Legacy duplicates do not prove an unchanged interval. Only the
            # explicit confirmation written by new polling code is usable.
            if row.get("last_confirmed_at") is not None:
                versions[-1]["last_confirmed_at"] = row["last_confirmed_at"]
            versions[-1]["last_observed_at"] = at
            continue
        old = versions[-1] if versions else None
        fields = state.get("fields", {})
        old_fields = old["state"].get("fields", {}) if old else {}
        changed = sorted(key for key in fields.keys() | old_fields.keys()
                         if fingerprint(fields.get(key)) != fingerprint(old_fields.get(key)))
        versions.append({
            "event_id": row["event_id"], "kind": "change" if old else row.get("kind", "first_observation"),
            "observed_at": at, "last_confirmed_at": row.get("last_confirmed_at"),
            "last_observed_at": at,
            "state": state, "version": state_version,
            "changed_fields": changed if old else row.get("changed_fields", changed),
            "boundary": row.get("boundary") if (row.get("boundary") or {}).get("after") is not None else {
                "after": old["last_observed_at"] if old else None,
                "no_later_than": at, "exact_effective_at": None},
        })
    left, right = int(start.timestamp() * 1_000_000), int(end.timestamp() * 1_000_000)
    applicable = []
    for index, version in enumerate(versions):
        following = versions[index + 1] if index + 1 < len(versions) else None
        # Include the predecessor plus a future boundary only when its uncertainty
        # overlaps the report. Never project today's first snapshot backwards.
        boundary_after = version["boundary"]["after"]
        if version["observed_at"] >= right and (boundary_after is None or boundary_after >= right):
            continue
        if following and following["observed_at"] < left:
            continue
        applicable.append(version)
    payload = {"contract": CONTRACT, "device_id": device_id, "circuit_id": circuit_id,
               "period_start": start.isoformat(), "period_end": end.isoformat(), "versions": applicable,
               "incomplete": incomplete,
               "timestamp_unit": "UTC microseconds since Unix epoch",
               "unknown_before_first_observation": not versions or versions[0]["observed_at"] > left,
               "semantics": "observation times; uncertain changes; no continuous runtime or exact effective time"}
    payload["fingerprint"] = fingerprint(semantic_history(payload))
    return payload

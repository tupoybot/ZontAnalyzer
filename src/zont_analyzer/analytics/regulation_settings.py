"""Apply observed configuration history without projecting today's settings back.

Configuration observations are reference settings, never proof of continuous
runtime or that a boiler actually enforced a curve. Uncertain changes mask
tuning intervals; they do not split a day or manufacture request edges.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .evidence import ExclusionWindow, NumericSample, SignalMetadata, SignalSeries
from .regulation import RegulationInputs, _Lookup, _spans
from .settings import pza_points_c, regulation_settings


def _at(microseconds: int) -> datetime:
    return datetime.fromtimestamp(microseconds / 1_000_000, UTC)


def _raw_circuit(version: dict[str, Any]) -> dict[str, Any]:
    state = version.get("state", {})
    if state.get("status") != "observed":
        return {}
    return {key: field["value"] for key, field in state.get("fields", {}).items()
            if isinstance(field, dict) and field.get("status") == "observed" and "value" in field}


def _curve(version: dict[str, Any], circuit_id: str) -> list[dict[str, float]] | None:
    raw = _raw_circuit(version)
    regulation = regulation_settings(raw, circuit_id)
    if regulation.get("pza_role") != "upper_limit" or regulation.get("enabled") is not True:
        return None
    field = version.get("state", {}).get("fields", {}).get("pza_curve", {})
    return pza_points_c(field.get("x"), field.get("y")) if field.get("status") == "observed" else None


def _interpolate(points: list[dict[str, float]], outdoor: float) -> float | None:
    ordered = sorted(points, key=lambda point: point["outdoor_c"])
    if not ordered[0]["outdoor_c"] <= outdoor <= ordered[-1]["outdoor_c"]:
        # No unverified endpoint clamping or extrapolation.
        return None
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left["outdoor_c"] <= outdoor <= right["outdoor_c"]:
            ratio = (outdoor - left["outdoor_c"]) / (right["outdoor_c"] - left["outdoor_c"])
            return left["flow_c"] + ratio * (right["flow_c"] - left["flow_c"])
    return None


def apply_settings_history(inputs: RegulationInputs, history: dict[str, Any], *, start: datetime,
                           end: datetime, flow_association_verified: bool = False) -> RegulationInputs:
    """Return one day with a version schedule, masks and separate known limits.

    First observations do not establish prior settings. A change is uncertain
    between its stable ``boundary.after`` and ``observed_at``. Volatile later
    confirmation endpoints do not affect facts or cache identity.
    """
    if history.get("device_id") != inputs.device_id or str(history.get("circuit_id")) != inputs.circuit_id:
        raise ValueError("settings history belongs to another device/circuit")
    versions = sorted(history.get("versions", []), key=lambda version: version["observed_at"])
    times = [_at(version["observed_at"]) for version in versions]
    uncertain = []
    exclusions = list(inputs.exclusions)
    boundaries = {start, end}
    for version, timestamp in zip(versions, times, strict=True):
        if start < timestamp < end:
            boundaries.add(timestamp)
        if version.get("kind") != "change":
            continue
        after = version.get("boundary", {}).get("after")
        lower = _at(after) if after is not None else start
        if lower < timestamp:
            uncertain.append((lower, timestamp))
            if lower < end and timestamp > start:
                exclusions.append(ExclusionWindow(max(start, lower), min(end, timestamp),
                                                  "transition", "historical_settings_uncertain_boundary"))
            if start < lower < end:
                boundaries.add(lower)

    def version_at(timestamp: datetime) -> dict[str, Any] | None:
        if history.get("incomplete") or any(left <= timestamp < right for left, right in uncertain):
            return None
        index = bisect_right(times, timestamp) - 1
        return versions[index] if index >= 0 else None

    schedule: list[tuple[datetime, str]] = []
    for timestamp in sorted(boundaries - {end}):
        version = version_at(timestamp)
        name = version["version"] if version else "unknown"
        if not schedule or schedule[-1][1] != name:
            schedule.append((timestamp, name))
    signals = dict(inputs.signals)
    # Only the caller can verify the command/boiler-to-circuit relationship.
    # Remove no existing caller-proven limit when no history reference exists.
    if flow_association_verified:
        water_samples = []
        water_unknown = []
        for timestamp, _name in schedule:
            version = version_at(timestamp)
            maximum = _raw_circuit(version).get("water_max_temperature") if version else None
            if (isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
                    and math.isfinite(maximum) and 0 < maximum <= 150):
                water_samples.append(NumericSample(timestamp, float(maximum)))
            else:
                water_unknown.append(timestamp)
        signals["water_limit"] = SignalSeries(
            "water_limit", SignalMetadata(
                f"settings:{inputs.device_id}/{inputs.circuit_id}:water_max",
                "Historical configured water maximum", "°C",
                origin="derived", value_kind="setpoint",
                provenance="observed_configuration_reference;flow_association_verified;runtime_enforcement_unproven",
            ), tuple(water_samples), tuple(water_unknown),
        )
        outdoor = signals.get("outdoor")
        if outdoor:
            spans = _spans(outdoor, end, inputs.max_gap_seconds)
            lookup = _Lookup(spans)
            points = set(boundaries)
            for span in spans:
                points.update(timestamp for timestamp in (span.start, span.end) if start <= timestamp <= end)
                if span.left == span.right:
                    continue
                for version in versions:
                    curve = _curve(version, inputs.circuit_id)
                    for point in curve or []:
                        value = point["outdoor_c"]
                        if min(span.left, span.right) < value < max(span.left, span.right):
                            ratio = (value - span.left) / (span.right - span.left)
                            timestamp = span.start + (span.end - span.start) * ratio
                            if start <= timestamp <= end:
                                points.add(timestamp)
            limit_samples = []
            limit_unknown = []
            for timestamp in sorted(points):
                version = version_at(timestamp)
                curve = _curve(version, inputs.circuit_id) if version else None
                index = bisect_right(lookup.starts, timestamp) - 1
                selected_span = spans[index] if index >= 0 else None
                if selected_span and not selected_span.start <= timestamp <= selected_span.end:
                    selected_span = None
                outside = selected_span.value(timestamp) if selected_span else None
                cap = _interpolate(curve, outside) if curve and outside is not None else None
                if cap is None:
                    limit_unknown.append(timestamp)
                else:
                    limit_samples.append(NumericSample(timestamp, cap))
            signals["limit"] = SignalSeries(
                "limit", SignalMetadata(
                    f"settings:{inputs.device_id}/{inputs.circuit_id}:pza_upper_limit",
                    "Historical PZA curve upper limit",
                    "°C", origin="derived",
                    provenance="observed_configuration_reference;PID_upper_limit;runtime_enforcement_unproven",
                ), tuple(limit_samples), tuple(limit_unknown),
            )
    return replace(inputs, signals=signals, exclusions=tuple(exclusions), settings_versions=tuple(schedule),
                   settings_version=str(history.get("fingerprint") or "unknown"))

"""Bounded, diagnosis-free evidence for weather-dependent heating analysis."""

from __future__ import annotations

from calendar import isleap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import acos, cos, degrees, isfinite, pi, radians, sin, tan
from statistics import mean, median
from typing import Any, cast
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class HeatingEvidence:
    period_start: datetime | None
    period_end: datetime | None
    windows: tuple[dict[str, Any], ...]
    inactive_windows: tuple[dict[str, Any], ...]
    unknown_windows: tuple[dict[str, Any], ...]
    morning_windows: tuple[dict[str, Any], ...]
    comparisons: tuple[dict[str, Any], ...]
    quality: dict[str, Any]
    unknowns: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _json_safe(
                {
                    "period_start": self.period_start,
                    "period_end": self.period_end,
                    "windows": self.windows,
                    "inactive_windows": self.inactive_windows,
                    "unknown_windows": self.unknown_windows,
                    "morning_windows": self.morning_windows,
                    "comparisons": self.comparisons,
                    "quality": self.quality,
                    "unknowns": self.unknowns,
                }
            ),
        )


def build_heating_evidence(
    temporal_evidence: Mapping[str, Any] | Any,
    prior_reports: Sequence[Mapping[str, Any] | Any] = (),
    *,
    coordinates: Mapping[str, Any] | None = None,
    max_windows: int = 24,
) -> HeatingEvidence:
    """Extract explicit CH windows and bounded weather comparisons."""
    if max_windows < 1:
        raise ValueError("max_windows must be positive")
    packet = _packet(temporal_evidence)
    rows: list[dict[str, Any]] = []
    for item in _windows(packet):
        if item.get("kind") != "representative":
            row = _window(item, packet, "current")
            if row is not None:
                rows.append(row)
    for report in prior_reports:
        raw = _mapping(report)
        report_id = str(raw.get("id", "prior"))
        source = _packet(raw)
        for item in _windows(source):
            if item.get("kind") != "representative":
                row = _window(item, source, report_id)
                if row is not None:
                    rows.append(row)
    # A long aggregate may repeat the same hourly source as its daily history.
    deduplicated = {(row["started_at"], row["ended_at"]): row for row in reversed(rows)}
    rows = sorted(deduplicated.values(), key=lambda row: (row["started_at"], row["id"]))
    active = _balanced_cap([row for row in rows if row["activity"] == "active" and row["eligible"]], max_windows)
    inactive = [row for row in rows if row["activity"] == "inactive"][-max(1, max_windows // 2):]
    unknown = [
        row for row in rows if row["activity"] == "unknown" or (row["activity"] == "active" and not row["eligible"])
    ]
    unknown = unknown[-max(1, max_windows // 2):]
    location = _location(coordinates, packet)
    timezone = _timezone(packet)
    mornings: list[dict[str, Any]] = []
    unknowns: set[str] = set()
    for row in rows:
        sunrise = _sunrise(row["started_at"], location, timezone)
        if sunrise is None:
            unknowns.add("sunrise:unavailable_coordinates_or_polar_day")
        elif abs((row["started_at"] - sunrise).total_seconds()) <= 2 * 3600:
            morning = dict(row)
            morning.update(sunrise=sunrise, id=f"morning:{row['id']}", kind="morning")
            mornings.append(morning)
    if location is None:
        unknowns.add("location:latitude_longitude_unknown")
    if not rows:
        unknowns.add("windows:none")
    if not active:
        unknowns.add("comparable_heating_windows:unavailable")
    outdoor = [row["outdoor_mean_c"] for row in active if row["outdoor_mean_c"] is not None]
    quality = {
        "source_window_count": len(rows),
        "window_count": len(active),
        "inactive_window_count": len(inactive),
        "unknown_window_count": len(unknown),
        "outdoor_temperature": _stats(outdoor),
        "room_error": _stats(row["room_error_c"] for row in active if row["room_error_c"] is not None),
    }
    return HeatingEvidence(
        _datetime(packet.get("period_start")),
        _datetime(packet.get("period_end")),
        tuple(active),
        tuple(inactive),
        tuple(unknown),
        tuple(mornings[-max(1, max_windows // 2):]),
        tuple(_comparisons(active, max_windows)),
        quality,
        tuple(sorted(unknowns)),
    )


analyze_heating_evidence = build_heating_evidence
heating_evidence = build_heating_evidence


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return cast(Mapping[str, Any], value.model_dump(mode="python"))
    return {}


def _packet(value: Any) -> Mapping[str, Any]:
    packet = _mapping(value)
    context = packet.get("context")
    if isinstance(context, Mapping) and isinstance(context.get("temporal_evidence"), Mapping):
        return cast(Mapping[str, Any], context["temporal_evidence"])
    return packet


def _windows(packet: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = packet.get("windows", [])
    return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _stat(item: Any) -> float | None:
    return _number(item.get("mean", item.get("value"))) if isinstance(item, Mapping) else None


def _coverage(item: Any) -> float:
    if not isinstance(item, Mapping):
        return 0.0
    direct = _number(item.get("coverage_pct"))
    if direct is not None:
        return direct
    nested = [_coverage(value) for value in item.values() if isinstance(value, Mapping)]
    return min(nested) if nested else 0.0


def _value(item: Any) -> str | float | None:
    if not isinstance(item, Mapping):
        return None
    value = item.get("mean", item.get("value"))
    return value if isinstance(value, str) else _number(value)


def _window(item: Mapping[str, Any], packet: Mapping[str, Any], period_id: str) -> dict[str, Any] | None:
    start, end = _datetime(item.get("started_at")), _datetime(item.get("ended_at"))
    if start is None or end is None or end <= start:
        return None
    signals = item.get("signals", {}) if isinstance(item.get("signals"), Mapping) else {}
    facts = item.get("facts", {}) if isinstance(item.get("facts"), Mapping) else {}
    target_signal = signals.get("target_temperature")
    target, room = _stat(target_signal), _stat(signals.get("control_temperature"))
    error = _stat(facts.get("room_error_c"))
    if error is None and room is not None and target is not None:
        error = room - target
    mode = _value(signals.get("setting:mode_id"))
    request = _stat(facts.get("heating_request_pct"))
    request = request if request is not None else _stat(signals.get("heating_request"))
    excluded = tuple(str(value) for value in item.get("excluded_reasons", []) if value)
    activity = "active" if request is not None and request > 0 else "inactive" if request == 0 else "unknown"
    if _coverage(facts.get("heating_request_pct")) < 70:
        activity = "unknown"
    outdoor = _stat(signals.get("outdoor_temperature"))
    outdoor_signal = signals.get("outdoor_temperature")
    quality = min(
        _coverage(signals.get("control_temperature")),
        _coverage(target_signal),
        _coverage(outdoor_signal),
        _coverage(signals.get("setting:mode_id")),
        _coverage(facts.get("heating_request_pct")),
    )
    eligible = (
        activity == "active"
        and not excluded
        and quality >= 70
        and mode is not None
        and target is not None
        and outdoor is not None
        and error is not None
        and _stable(target_signal)
        and _stable(signals.get("setting:mode_id"))
    )
    return {
        "id": f"{period_id}:{int(start.timestamp())}",
        "period_id": period_id,
        "started_at": start,
        "ended_at": end,
        "source_window_id": item.get("id", f"{period_id}:{int(start.timestamp())}"),
        "outdoor_mean_c": outdoor,
        "weather_class": _weather_class(outdoor),
        "room_error_c": error,
        "target_c": target,
        "mode": mode,
        "target_matched": target is not None and _coverage(target_signal) >= 70 and _stable(target_signal),
        "heating_request_pct": request,
        "confounders": {key: {name: stat.get(name) for name in ("mean", "slope_per_hour", "coverage_pct")}
                        for key, stat in {**signals, **facts}.items() if key in {
                            "flow_temperature", "delta_t_c", "dhw_pct", "control_temperature"
                        } and isinstance(stat, Mapping)},
        "activity": activity,
        "eligible": eligible,
        "excluded_reasons": list(excluded),
        "quality": quality,
    }


def _balanced_cap(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    ordered = sorted(rows, key=lambda row: (row["outdoor_mean_c"] or 0, row["started_at"]))
    if maximum == 1:
        return [max(rows, key=lambda row: row["started_at"])]
    return [ordered[round(index * (len(ordered) - 1) / (maximum - 1))] for index in range(maximum)]


def _stable(stat: Any) -> bool:
    if not isinstance(stat, Mapping):
        return False
    low, high = _number(stat.get("minimum")), _number(stat.get("maximum"))
    return low is not None and high is not None and low == high


def _comparisons(rows: Sequence[Mapping[str, Any]], maximum: int) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, Any], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["mode"], row["target_c"]), []).append(row)
    result: list[dict[str, Any]] = []
    for (mode, target), values in sorted(groups.items(), key=str):
        if len(values) < 2:
            continue
        selected = _balanced_cap([dict(value) for value in values], maximum)
        result.append(
            {
                "id": f"comparison:{mode}:{target}",
                "mode": mode,
                "target_c": target,
                "window_ids": [value["id"] for value in selected],
                "room_error": _stats(value["room_error_c"] for value in selected if value["room_error_c"] is not None),
                "outdoor_temperature": _stats(
                    value["outdoor_mean_c"] for value in selected if value["outdoor_mean_c"] is not None
                ),
            }
        )
    return result


def _stats(values: Iterable[float]) -> dict[str, Any]:
    finite = [value for value in values if isfinite(value)]
    minimum = min(finite) if finite else None
    maximum = max(finite) if finite else None
    return {
        "sample_count": len(finite),
        "mean": mean(finite) if finite else None,
        "median": median(finite) if finite else None,
        "minimum": minimum,
        "maximum": maximum,
        "range_c": maximum - minimum if minimum is not None and maximum is not None else None,
    }


def _location(explicit: Mapping[str, Any] | None, packet: Mapping[str, Any]) -> tuple[float, float] | None:
    candidate: Any = explicit or packet.get("location") or packet.get("coordinates")
    if not isinstance(candidate, Mapping):
        return None
    lat, lon = (
        _number(candidate.get("latitude", candidate.get("lat"))),
        _number(candidate.get("longitude", candidate.get("lon"))),
    )
    return (lat, lon) if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _timezone(packet: Mapping[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(packet.get("timezone", "UTC")))
    except Exception:
        return ZoneInfo("UTC")


def _sunrise(when: datetime, location: tuple[float, float] | None, timezone: ZoneInfo) -> datetime | None:
    """NOAA Solar Calculator sunrise equation, evaluated on the local date."""
    if location is None:
        return None
    latitude, longitude = location
    day = when.astimezone(timezone).date()
    # NOAA General Solar Position Calculations, fractional year at local noon.
    # https://gml.noaa.gov/grad/solcalc/solareqns.PDF
    gamma = 2 * pi / (366 if isleap(day.year) else 365) * (day.timetuple().tm_yday - 1)
    equation = 229.18 * (0.000075 + 0.001868 * cos(gamma) - 0.032077 * sin(gamma)
                         - 0.014615 * cos(2 * gamma) - 0.040849 * sin(2 * gamma))
    declination = (0.006918 - 0.399912 * cos(gamma) + 0.070257 * sin(gamma)
                   - 0.006758 * cos(2 * gamma) + 0.000907 * sin(2 * gamma)
                   - 0.002697 * cos(3 * gamma) + 0.00148 * sin(3 * gamma))
    latitude_rad = radians(latitude)
    denominator = cos(latitude_rad) * cos(declination)
    if abs(denominator) < 1e-12:
        return None
    cosine_hour = cos(radians(90.833)) / denominator - tan(latitude_rad) * tan(declination)
    if not -1 <= cosine_hour <= 1:
        return None
    minutes = 720 - 4 * (longitude + degrees(acos(cosine_hour))) - equation
    # Do not modulo UTC minutes: eastern sunrise may fall on the previous UTC day.
    sunrise = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=minutes)
    # A political timezone can put the solar date on the other side of the dateline.
    sunrise += timedelta(days=(day - sunrise.astimezone(timezone).date()).days)
    return sunrise.astimezone(timezone)


def _weather_class(value: float | None) -> str | None:
    return None if value is None else "cold" if value < 5 else "mild" if value < 15 else "warm"


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value

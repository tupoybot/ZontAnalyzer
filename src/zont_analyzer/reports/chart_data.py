"""Report-bound, presentation-only telemetry packets for static charts."""
from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.analytics.dhw import parse_opentherm_flags
from zont_analyzer.domain import Report

MAX_POINTS_PER_SERIES = 480
CHART_DATA_SCHEMA_VERSION = 1

_ROLE_LABELS = {
    "control_temperature": "Контрольная температура",
    "target_temperature": "Цель помещения",
    "outdoor_temperature": "Наружная температура",
    "flow_temperature": "Подача",
    "target_flow_temperature": "Цель подачи (cs)",
    "return_temperature": "Обратка",
    "dhw_temperature": "Температура ГВС",
}

_CHART_ROLES = tuple(_ROLE_LABELS)


def build_chart_data(db: Database, report: Report) -> dict[str, Any] | None:
    """Return observed telemetry selected for this report, without changing it.

    The report's temporal-evidence identities are the original analytical
    selection.  Reusing them makes an archive rerender stable even if discovery
    later finds another sensor.  Older reports without these identities only
    get a series when its role is unambiguous.
    """
    rows = db.list_series()
    selected = _selected_rows(report, rows)
    series: dict[str, dict[str, Any]] = {}
    for role in _CHART_ROLES:
        row = selected.get(role)
        if row is None:
            continue
        points = _numeric_points(db, row, report)
        unit = str(row.get("unit") or "")
        if points:
            series[role] = {
                "label": _ROLE_LABELS[role],
                "unit": unit,
                "points": points,
            }
    bands = _state_bands(db, selected.get("boiler_state"), report)
    packet: dict[str, Any] = {"timezone": report.timezone, "series": series}
    if bands:
        packet["state_bands"] = bands
    return packet if series or bands else None


def cached_chart_data(db: Database, report: Report) -> dict[str, Any] | None:
    """Load or build a report-bound chart packet without changing canonical data.

    The cache deliberately follows the canonical report hash, rather than the
    mutable telemetry store.  Historical corrections therefore become visible
    after that report is recomputed; a retained archive keeps the observations
    it was first published with until then.
    """
    path, digest = _cache_path(db, report)
    try:
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CHART_DATA_SCHEMA_VERSION
                and cached.get("report_digest") == digest
                and isinstance(cached.get("data"), Mapping)
            ):
                return dict(cached["data"])
    except (OSError, ValueError, TypeError):
        pass
    data = build_chart_data(db, report)
    if data is not None:
        with suppress(OSError):
            _atomic_write_json(path, {
                "schema_version": CHART_DATA_SCHEMA_VERSION,
                "report_digest": digest,
                "data": data,
            })
    return data


def _cache_path(db: Database, report: Report) -> tuple[Path, str]:
    digest = sha256(report.model_dump_json().encode("utf-8")).hexdigest()
    report_key = sha256(report.id.encode("utf-8")).hexdigest()
    return db.path.parent / "chart-data-cache" / f"{report_key}.json", digest


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _selected_rows(report: Report, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    evidence = report.context.get("temporal_evidence")
    signals = evidence.get("signals") if isinstance(evidence, Mapping) else None
    by_identity = {_identity(row): row for row in rows}
    selected: dict[str, dict[str, Any]] = {}
    if isinstance(signals, Mapping):
        for details in signals.values():
            if not isinstance(details, Mapping):
                continue
            role, identity = details.get("role"), details.get("identity")
            if isinstance(role, str) and role in _CHART_ROLES and isinstance(identity, str):
                row = by_identity.get(identity)
                if row is not None:
                    selected[role] = row

    # Boiler state is selected by source semantics for CH/DHW state bands.
    boiler = [
        row for row in rows
        if row["source_type"] == "z3k_boiler_adapter" and row["metric_key"] == "s"
    ]
    if len(boiler) == 1:
        selected["boiler_state"] = boiler[0]

    for role in _CHART_ROLES:
        if role in selected:
            continue
        candidates = [row for row in rows if row.get("role") == role]
        if len(candidates) == 1:
            selected[role] = candidates[0]
    return selected


def _identity(row: Mapping[str, Any]) -> str:
    return "/".join(str(row[key]) for key in ("device_id", "source_type", "entity_id", "metric_key"))


def _numeric_points(db: Database, row: Mapping[str, Any], report: Report) -> list[dict[str, Any]]:
    samples = db.fetch_samples(int(row["id"]), report.period_start, report.period_end)
    return _point_dicts(samples)


def _point_dicts(samples: Iterable[tuple[datetime, float]]) -> list[dict[str, Any]]:
    ordered: dict[datetime, float] = {}
    for timestamp, value in samples:
        if math.isfinite(value):
            ordered[timestamp] = float(value)
    marked = _mark_gaps(sorted(ordered.items()))
    return [
        {"timestamp": timestamp.isoformat(), "value": value, **({"gap_before": True} if gap_before else {})}
        for timestamp, value, gap_before in _decimate(marked)
    ]


def _mark_gaps(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float, bool]]:
    gaps = [
        (right[0] - left[0]).total_seconds()
        for left, right in zip(points, points[1:], strict=False)
        if right[0] > left[0]
    ]
    threshold = median(gaps) * 3 if gaps else math.inf
    return [
        (timestamp, value, bool(index and (timestamp - points[index - 1][0]).total_seconds() > threshold))
        for index, (timestamp, value) in enumerate(points)
    ]


def _decimate(
    points: list[tuple[datetime, float, bool]], limit: int = MAX_POINTS_PER_SERIES,
) -> list[tuple[datetime, float, bool]]:
    """Keep genuine extrema and boundaries, including every observed gap edge."""
    if len(points) <= limit:
        return points
    mandatory = {0, len(points) - 1}
    for index, point in enumerate(points):
        if point[2]:
            mandatory.update({index - 1, index})
    # The number of source gaps is normally small.  If it exceeds the visual
    # budget, retaining all edges is more truthful than silently joining them.
    if len(mandatory) >= limit:
        return [points[index] for index in sorted(mandatory)]
    bucket_count = max(1, (limit - len(mandatory)) // 2)
    bucket_width = len(points) / bucket_count
    selected = set(mandatory)
    for bucket in range(bucket_count):
        start, end = int(bucket * bucket_width), min(len(points), int((bucket + 1) * bucket_width))
        if start >= end:
            continue
        values = range(start, end)
        selected.add(min(values, key=lambda index: points[index][1]))
        selected.add(max(values, key=lambda index: points[index][1]))
    return [points[index] for index in sorted(selected)]


def _state_bands(
    db: Database, boiler_row: Mapping[str, Any] | None, report: Report,
) -> list[dict[str, str]]:
    """Use only contiguous observed OpenTherm state samples as thermal bands."""
    if boiler_row is None:
        return []
    samples = db.fetch_text_samples(int(boiler_row["id"]), report.period_start, report.period_end)
    if len(samples) < 2:
        return []
    gaps = [
        (right[0] - left[0]).total_seconds()
        for left, right in zip(samples, samples[1:], strict=False)
        if right[0] > left[0]
    ]
    allowed_gap = min(1800.0, max(60.0, median(gaps) * 3)) if gaps else 0.0
    bands: list[dict[str, str]] = []
    for (started_at, encoded), (ended_at, _next) in zip(samples, samples[1:], strict=False):
        if (ended_at - started_at).total_seconds() > allowed_gap:
            continue
        flags = parse_opentherm_flags(encoded)
        if "fl" not in flags:
            continue
        if "ch" in flags and "dhw" in flags:
            state, label = "concurrent", "Горелка: одновременный CH и ГВС"
        elif "ch" in flags:
            state, label = "ch", "Горелка: отопление (CH)"
        elif "dhw" in flags:
            state, label = "dhw", "Горелка: ГВС"
        else:
            continue
        band = {
            "started_at": max(started_at, report.period_start).isoformat(),
            "ended_at": min(ended_at, report.period_end).isoformat(),
            "state": state,
            "label": label,
        }
        if bands and bands[-1]["state"] == state and bands[-1]["ended_at"] == band["started_at"]:
            bands[-1]["ended_at"] = band["ended_at"]
        else:
            bands.append(band)
    return bands

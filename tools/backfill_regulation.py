"""Bounded offline regulation backfill; artifacts contain private telemetry.

An exporter supplies already selected circuit inputs to ``export_inputs`` while
holding a source revision fence captured BEFORE fetching them. Calculation has
no database or network access. ``upload --apply`` writes only compact facts and
atomically rejects a changed source. Keep both artifacts outside the checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.analytics.evidence import (
    ExclusionWindow,
    NumericSample,
    SignalMetadata,
    SignalSeries,
    StateSample,
)
from zont_analyzer.analytics.regulation import ALGORITHM_VERSION, RegulationInputs, build_regulation_facts
from zont_analyzer.application.regulation_context import regulation_fingerprint

FORMAT = "regulation-backfill-v1"
MAX_DAYS = 31
MAX_BYTES = 64 * 1024 * 1024


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _merge_fence(target: dict[str, int | str], source: dict[str, int | str]) -> None:
    if any(key in target and target[key] != value for key, value in source.items()):
        raise ValueError("source changed between bounded export reads; export again")
    target.update(source)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path = path.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if path == checkout or checkout in path.parents:
        raise ValueError("private regulation artifacts must stay outside the checkout")
    raw = _canonical(data).encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("backfill artifact exceeds 64 MiB; export a smaller bounded period")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".regulation-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("backfill artifact exceeds 64 MiB")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("unsupported regulation backfill artifact")
    return value


def _encode_inputs(inputs: RegulationInputs) -> dict[str, Any]:
    value = asdict(inputs)
    value["signals"] = {key: {"key": series.key, "metadata": asdict(series.metadata),
                              "samples": [[sample.timestamp.isoformat(), sample.value] for sample in series.samples],
                              "unknown_at": [timestamp.isoformat() for timestamp in series.unknown_at]}
                        for key, series in inputs.signals.items()}
    value["states"] = [{"timestamp": sample.timestamp.isoformat(), "flags": sorted(sample.flags),
                        "identity": sample.identity} for sample in inputs.states]
    value["state_unknown_at"] = [timestamp.isoformat() for timestamp in inputs.state_unknown_at]
    value["settings_versions"] = [[timestamp.isoformat(), version] for timestamp, version in inputs.settings_versions]
    value["exclusions"] = [{"start": item.start.isoformat(), "end": item.end.isoformat(),
                            "reason": item.reason, "source": item.source} for item in inputs.exclusions]
    return value


def _decode_inputs(value: dict[str, Any]) -> RegulationInputs:
    value = dict(value)
    value["signals"] = {key: SignalSeries(
        key=item["key"], metadata=SignalMetadata(**item["metadata"]),
        samples=tuple(NumericSample(datetime.fromisoformat(timestamp), number)
                      for timestamp, number in item["samples"]),
        unknown_at=tuple(datetime.fromisoformat(timestamp) for timestamp in item["unknown_at"]),
    ) for key, item in value["signals"].items()}
    value["states"] = tuple(StateSample(datetime.fromisoformat(item["timestamp"]), frozenset(item["flags"]),
                                        item["identity"]) for item in value["states"])
    value["state_unknown_at"] = tuple(datetime.fromisoformat(timestamp)
                                      for timestamp in value.get("state_unknown_at", []))
    value["settings_versions"] = tuple((datetime.fromisoformat(timestamp), version)
                                       for timestamp, version in value.get("settings_versions", []))
    value["negative_input_guards"] = tuple((key, cutoff) for key, cutoff in value.get("negative_input_guards", []))
    value["source_unknowns"] = tuple(value.get("source_unknowns", []))
    value["exclusions"] = tuple(ExclusionWindow(datetime.fromisoformat(item["start"]),
                                                datetime.fromisoformat(item["end"]), item["reason"], item["source"])
                                  for item in value["exclusions"])
    return RegulationInputs(**value)


def export_inputs(db: Database, days: list[tuple[datetime, datetime, RegulationInputs]], *, output: Path,
                  source_fence: dict[str, int | str], previous_boundaries: dict[str, Any] | None = None,
                  cache_fingerprints: dict[str, str] | None = None,
                  cache_contexts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Write a private raw export after the caller's bounded read-only selection.

    Circuit ownership, historical limits and exclusions are the same selected
    ``RegulationInputs`` used by normal analysis, never guessed by this tool.
    The complete source fence must be captured before loading ``days``.
    """
    if not days or len({start for start, _end, _inputs in days}) > MAX_DAYS:
        raise ValueError("export between one and 31 days per bounded backfill batch")
    if len(days) > MAX_DAYS * 16:
        raise ValueError("backfill batch exceeds 16 circuits per day")
    repository = RegulationRepository(db.storage)
    actual = repository._read_fence(db.storage, list(source_fence)) if any(
        key.startswith("meta:") for key in source_fence) else repository.source_fence()
    if actual != source_fence:
        raise ValueError("source changed during regulation export; read the bounded inputs again")
    items = []
    for start, end, inputs in days:
        if end <= start or (end - start).total_seconds() > 25 * 3600:
            raise ValueError("export inputs must be one bounded local day each")
        items.append({"start": start.isoformat(), "end": end.isoformat(), "inputs": _encode_inputs(inputs)})
    value = {"format": FORMAT, "kind": "raw_inputs", "source_identity": db.identity,
             "source_fence": source_fence, "previous_boundaries": previous_boundaries or {}, "items": items,
             "cache_fingerprints": cache_fingerprints or {}, "cache_contexts": cache_contexts or {}}
    _write_private(output, value)
    return {"days": len({start for start, _end, _inputs in days}), "circuit_days": len(items),
            "bytes": output.stat().st_size}


def calculate_export(source: Path, output: Path) -> dict[str, Any]:
    exported = _read(source)
    if exported.get("kind") != "raw_inputs":
        raise ValueError("calculation requires a raw-input export")
    items = sorted(exported["items"], key=lambda item: item["start"])
    if not items or len({item["start"] for item in items}) > MAX_DAYS:
        raise ValueError("calculate at most 31 days per bounded batch")
    boundaries = dict(exported.get("previous_boundaries", {}))
    facts = []
    started = time.monotonic()
    seen = set()
    for item in items:
        start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        inputs = _decode_inputs(item["inputs"])
        scope = f"{inputs.device_id}/{inputs.circuit_id}"
        key = (scope, start)
        if key in seen:
            raise ValueError("duplicate regulation backfill circuit/day")
        seen.add(key)
        previous = boundaries.get(scope)
        context = exported.get("cache_contexts", {}).get(f"{scope}@{start.isoformat()}")
        if context is not None and previous is None:
            from zont_analyzer.application.regulation_reports import _bootstrap
            previous = _bootstrap(inputs, start, None)
        revision = hashlib.sha256(_canonical(item).encode()).hexdigest()
        fingerprint = regulation_fingerprint(source_revision=revision, settings_version=inputs.settings_version,
                                             previous_boundary=previous)
        fingerprint = exported.get("cache_fingerprints", {}).get(f"{scope}@{start.isoformat()}", fingerprint)
        fact = build_regulation_facts(start=start, end=end, inputs=inputs, previous_boundary=previous)
        if context is not None:
            from zont_analyzer.application.regulation_reports import attach_regulation_cache
            fingerprint = attach_regulation_cache(fact, source_fence=context["source_fence"],
                                                  selection=context["selection"], settings=context["settings"],
                                                  previous=previous)
        facts.append({"facts": fact, "source_fingerprint": fingerprint})
        boundaries[scope] = fact["boundary"]
    _write_private(output, {"format": FORMAT, "kind": "compact_facts", "algorithm_version": ALGORITHM_VERSION,
                            "source_identity": exported["source_identity"], "source_fence": exported["source_fence"],
                            "items": facts})
    return {"circuit_days": len(facts), "seconds": round(time.monotonic() - started, 4),
            "bytes": output.stat().st_size, "raw_bytes": source.stat().st_size}


def export_selected_period(db: Database, start: datetime, end: datetime, *, timezone: str,
                           output: Path) -> dict[str, Any]:
    """Read-only bounded export with the exact report selection/cache contract."""
    from zont_analyzer.application.regulation_reports import (
        _days,
        day_history,
        prepare_regulation_day,
        validated_regulation_days,
    )
    from zont_analyzer.application.regulation_sources import SOURCE_LOOKAHEAD_SECONDS, regulation_scopes

    windows = _days(start, end, timezone)
    if not windows or len(windows) > MAX_DAYS or windows[0][0] != start or windows[-1][1] != end:
        raise ValueError("export between one and 31 complete local days")
    repository = RegulationRepository(db.storage)
    initial = repository.source_fence()
    devices, series = db.list_devices(), db.list_series()
    scopes = regulation_scopes(devices, series)
    identifiers = tuple(sorted({device for device, _circuit in scopes}))
    source_windows = [(left - timedelta(hours=6), right + timedelta(seconds=SOURCE_LOOKAHEAD_SECONDS))
                      for left, right in windows]
    fences = repository.source_fences(source_windows, device_ids=identifiers)
    previous_start = windows[0][0] - timedelta(hours=1)
    prior = _days(previous_start, start, timezone)[0][0]
    validated = validated_regulation_days(db, prior, start, timezone=timezone)
    if any(day.get("reason") == "stale_boundary_ancestor_requires_local_backfill"
           for item in validated["circuits"] for day in item["missing_days"]):
        raise ValueError("stale boundary ancestor requires an earlier bounded export before this period")
    boundaries = {item["scope"]: item["facts"][-1]["boundary"] for item in validated["circuits"] if item["facts"]}
    days = []
    contexts = {}
    batch_fence: dict[str, int | str] = {}
    for device, circuit in scopes:
        scope = f"{device}/{circuit}"
        history = db.control_settings_history(device, circuit, start - timedelta(hours=6), end)
        for (left, right), source_window in zip(windows, source_windows, strict=True):
            selected, selection, fence = prepare_regulation_day(
                db, left, right, devices=devices, series=series, identifiers=identifiers,
                source_fence=fences[source_window], history=history, scope=scope,
            )
            if selected is None:
                continue
            days.append((left, right, selected))
            contexts[f"{scope}@{left.isoformat()}"] = {
                "selection": selection, "source_fence": fence,
                "settings": day_history(history, left - timedelta(hours=6), right),
            }
            _merge_fence(batch_fence, fence)
        boundary = boundaries.get(scope, {})
        _merge_fence(batch_fence, boundary.get("lineage", {}).get("markers", {}))
    current = repository.source_fence()
    if any(current.get(prefix + device, 0) != initial.get(prefix + device, 0)
           for device in identifiers for prefix in ("device:", "series:")):
        raise ValueError("source metadata changed during bounded export; export again")
    return export_inputs(db, days, output=output, source_fence=batch_fence,
                          previous_boundaries=boundaries, cache_contexts=contexts)


def upload_compact(db: Database, source: Path) -> dict[str, int]:
    value = _read(source)
    if value.get("kind") != "compact_facts" or value.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("upload requires compact facts from the current algorithm")
    if value.get("source_identity") != db.identity:
        raise ValueError("compact regulation facts belong to another source database")
    if not isinstance(value.get("source_fence"), dict):
        raise ValueError("compact upload requires an explicit export source fence")
    items = value["items"]
    if len(items) > MAX_DAYS * 16:
        raise ValueError("compact upload exceeds one bounded batch")
    repository = RegulationRepository(db.storage)
    changed = 0
    for item in items:
        changed += repository.save(item["facts"], source_fingerprint=item["source_fingerprint"],
                                   source_fence=value["source_fence"], publish=True)
    return {"circuit_days": len(items), "changed": changed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--start", type=datetime.fromisoformat, required=True)
    export.add_argument("--end", type=datetime.fromisoformat, required=True)
    export.add_argument("--timezone", required=True)
    export.add_argument("--output", type=Path, required=True)
    calculate = commands.add_parser("calculate")
    calculate.add_argument("--input", type=Path, required=True)
    calculate.add_argument("--output", type=Path, required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("--input", type=Path, required=True)
    upload.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "calculate":
        result = calculate_export(args.input, args.output)
    else:
        database = Database(YdbConfig.from_environment())
        try:
            result = (export_selected_period(database, args.start, args.end, timezone=args.timezone, output=args.output)
                      if args.command == "export" else upload_compact(database, args.input))
        finally:
            database.close()
    print(_canonical(result))


if __name__ == "__main__":
    main()

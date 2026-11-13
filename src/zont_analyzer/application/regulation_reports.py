"""Validate every compact day; calculate at most two days before a paid AI call."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.analytics.regulation import (
    ALGORITHM_VERSION,
    RegulationInputs,
    aggregate_regulation_facts,
    build_regulation_facts,
)
from zont_analyzer.analytics.regulation_settings import apply_settings_history
from zont_analyzer.analytics.settings_history import settings_timeline
from zont_analyzer.domain.periods import midnight

from .regulation_context import RegulationFactStore, regulation_fingerprint
from .regulation_sources import (
    SELECTION_VERSION,
    SOURCE_LOOKAHEAD_SECONDS,
    load_regulation_sources,
    regulation_scopes,
    regulation_source_selections,
)

CACHE_CONTRACT = "regulation-day-cache-v2"


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _days(start: datetime, end: datetime, timezone: str) -> list[tuple[datetime, datetime]]:
    day = start.astimezone(ZoneInfo(timezone)).date()
    result = []
    while midnight(day, timezone) < end:
        left, right = midnight(day, timezone), midnight(day + timedelta(days=1), timezone)
        result.append((left, right))
        day += timedelta(days=1)
        if len(result) > 400:
            raise ValueError("regulation report is limited to 400 local days")
    return result


def day_history(history: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    return settings_timeline(history.get("versions", []), device_id=history["device_id"],
                             circuit_id=history["circuit_id"], start=start, end=end,
                             incomplete=history.get("incomplete", False))


def _markers(fence: dict[str, Any]) -> dict[str, Any]:
    # Current metadata revisions fence races, not historical cache semantics.
    return {key: value for key, value in fence.items() if key.startswith("meta:")}


def _dependency_bounds(facts: list[dict[str, Any]], start: datetime, end: datetime) -> tuple[datetime, datetime]:
    lower, upper = start, end
    for fact in facts:
        dependency = fact.get("cache_dependency", {})
        for key in dependency.get("markers", {}):
            if key.startswith("meta:telemetry-day:"):
                timestamp = datetime.fromisoformat(key.removeprefix("meta:telemetry-day:")).replace(tzinfo=UTC)
                lower, upper = min(lower, timestamp), max(upper, timestamp + timedelta(days=1))
        for left, setting in dependency.get("settings", {}).items():
            lower, upper = min(lower, datetime.fromisoformat(left)), max(upper, datetime.fromisoformat(setting["end"]))
        if dependency.get("negative_inputs"):
            # A newly arriving first input anywhere before its cutoff can
            # invalidate previously observed absence, including legacy history.
            lower = min(lower, datetime(1970, 1, 1, tzinfo=UTC))
    return lower, upper


def _empty_dependency(selection: dict[str, Any]) -> dict[str, Any]:
    return {"markers": {}, "settings": {}, "negative_inputs": {},
            "selection": _hash(selection), "rules": CACHE_CONTRACT}


def _dependency_valid(dependency: dict[str, Any], actual: dict[str, Any], history: dict[str, Any],
                      selection: dict[str, Any], settings_cache: dict[tuple[str, str], str] | None = None) -> bool:
    if dependency.get("rules") != CACHE_CONTRACT or dependency.get("selection") != _hash(selection):
        return False
    if any(actual.get(key) != value for key, value in dependency.get("markers", {}).items()):
        return False
    for key, cutoff in dependency.get("negative_inputs", {}).items():
        first = actual.get("meta:" + key, "")
        if first != "" and int(first) < cutoff:
            return False
    for left, setting in dependency.get("settings", {}).items():
        key = (left, setting["end"])
        fingerprint = settings_cache.get(key) if settings_cache is not None else None
        if fingerprint is None:
            current = day_history(history, datetime.fromisoformat(left), datetime.fromisoformat(setting["end"]))
            fingerprint = current["fingerprint"]
            if settings_cache is not None:
                settings_cache[key] = fingerprint
        if fingerprint != setting["fingerprint"]:
            return False
    return True


def prepare_regulation_day(
    db: Database, left: datetime, right: datetime, *, devices: list[dict[str, Any]],
    series: list[dict[str, Any]], identifiers: tuple[str, ...], source_fence: dict[str, int | str],
    history: dict[str, Any], scope: str,
) -> tuple[RegulationInputs | None, dict[str, Any], dict[str, int | str]]:
    """Shared normal/offline source selection and historical-settings enrichment."""
    sources = load_regulation_sources(db, left, right, device_ids=identifiers, devices=devices,
                                      series=series, source_fence=source_fence, extend_setpoint_fence=True)
    selected = next((item for item in sources.inputs if f"{item.device_id}/{item.circuit_id}" == scope), None)
    selection = sources.selections.get(scope, {})
    if selected is not None:
        settings = day_history(history, left, right)
        lookback = left - timedelta(hours=6)
        selected = apply_settings_history(
            selected, day_history(history, lookback, right), start=lookback, end=right,
            flow_association_verified=selection.get("boiler_association")
            == "single_adapter_and_single_heating_consumer_on_device",
        )
        selected = replace(selected, settings_version=settings["fingerprint"])
    return selected, selection, sources.source_fence


def _lineage(boundary: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = boundary.get("lineage", _empty_dependency(selection))
    return result


def _bootstrap(selected: RegulationInputs, left: datetime, seed: dict[str, Any] | None) -> dict[str, Any]:
    """Refresh the immediate tail from the already fetched six hours."""
    start = left - timedelta(hours=6)
    boundary = deepcopy(seed) if seed else None
    if boundary:
        boundary.update(end=start.isoformat(), last_values={}, last_flame=None, command_last_sample_at=None)
        if boundary.get("recovery"):
            boundary["recovery"]["stable_intervals"] = 0
        anchor = boundary.get("request_anchor")
        if anchor and datetime.fromisoformat(anchor.get("started_at", left.isoformat())) >= start:
            boundary["request_anchor"] = None
    result: dict[str, Any] = build_regulation_facts(start=start, end=left, inputs=selected,
                                                  previous_boundary=boundary)["boundary"]
    return result


def attach_regulation_cache(
    facts: dict[str, Any], *, source_fence: dict[str, int | str], selection: dict[str, Any],
    settings: dict[str, Any], previous: dict[str, Any] | None,
) -> str:
    """Shared normal/offline compact-fact provenance and dependency closure."""
    dependency = deepcopy(_lineage(previous, selection)) if previous else _empty_dependency(selection)
    dependency["markers"].update(_markers(source_fence))
    dependency["settings"][settings["period_start"]] = {"end": settings["period_end"],
                                                        "fingerprint": settings["fingerprint"]}
    dependency.update(selection=_hash(selection), rules=CACHE_CONTRACT)
    negative = dependency.setdefault("negative_inputs", {})
    for key, cutoff in facts.get("negative_input_guards", []):
        negative[key] = max(cutoff, negative.get(key, cutoff))
    write_fence = dict(source_fence)
    for key, value in dependency["markers"].items():
        if key in write_fence and write_fence[key] != value:
            raise ValueError("regulation boundary source changed during calculation")
        write_fence[key] = value
    boundary = facts["boundary"]
    anchor = boundary.get("request_anchor")
    carry = boundary.get("recovery") or (anchor and anchor["key"][0] is True) or boundary.get("mode_kind") == "warmup"
    boundary["lineage"] = deepcopy(dependency) if carry else _empty_dependency(selection)
    fingerprint = regulation_fingerprint(
        source_revision=_hash({"markers": _markers(source_fence), "selection": selection}),
        settings_version=facts["settings_version"], previous_boundary=previous,
        rules={"sources": SELECTION_VERSION, "cache": CACHE_CONTRACT},
    )
    facts.update(cache_contract=CACHE_CONTRACT, cache_dependency=dependency, source_fence=write_fence,
                 source_selection=selection, source_fingerprint=fingerprint, input_boundary=previous,
                 previous_boundary_fingerprint=_hash(previous))
    return fingerprint


def validated_regulation_days(
    db: Database, start: datetime, end: datetime, *, timezone: str, calculate_daily: bool = False,
) -> dict[str, Any]:
    """Return full validated facts per scope and explicit missing/stale days.

    Comparisons and reports share this reader. Dependency closures are checked
    using metadata only. A stale inherited recovery is never silently forgotten.
    """
    store = RegulationFactStore(db)
    metadata_fence = store.repository.source_fence()
    devices, series = db.list_devices(), db.list_series()
    scopes = regulation_scopes(devices, series)
    selections = regulation_source_selections(devices, series)
    windows = _days(start, end, timezone)
    if not windows or not scopes:
        return {"circuits": [], "unknowns": ["heating_scope_unresolved"], "built_days": 0, "hit_days": 0}
    prior_date = (windows[0][0] - timedelta(hours=1)).astimezone(ZoneInfo(timezone)).date()
    predecessor = (midnight(prior_date, timezone), windows[0][0])
    all_windows = [predecessor, *windows]
    source_windows = [(left - timedelta(hours=6), right + timedelta(seconds=SOURCE_LOOKAHEAD_SECONDS))
                      for left, right in all_windows]
    identifiers = tuple(sorted({device for device, _circuit in scopes}))
    fences = store.repository.source_fences(source_windows, device_ids=identifiers)
    for fence in fences.values():
        for device in identifiers:
            for prefix in ("device:", "series:"):
                if fence["revision:" + prefix + device] != metadata_fence.get(prefix + device, 0):
                    raise ValueError("regulation source metadata changed during selection")
    circuits = []
    built = hits = 0
    dependency_start, dependency_end = start, end
    for device, circuit in scopes:
        scope = f"{device}/{circuit}"
        selection = selections[scope]
        stored = {item["period_start"]: item for item in store.read(scope, predecessor[0], windows[-1][1])}
        bounds = _dependency_bounds(list(stored.values()), start, end)
        dependency_start, dependency_end = min(dependency_start, bounds[0]), max(dependency_end, bounds[1])
        dependency_keys = {key for fact in stored.values()
                           for key in fact.get("cache_dependency", {}).get("markers", {})}
        dependency_keys.update("meta:" + key for fact in stored.values()
                               for key in fact.get("cache_dependency", {}).get("negative_inputs", {}))
        lineage_starts = [datetime.fromisoformat(left) for fact in stored.values()
                          for left in fact.get("cache_dependency", {}).get("settings", {})]
        history_start = min([predecessor[0] - timedelta(hours=6), *lineage_starts])
        history = db.control_settings_history(device, circuit, history_start, windows[-1][1])
        actual = store.repository.read_fence(sorted(dependency_keys))
        accepted = []
        missing = []
        previous: dict[str, Any] | None = None
        blocked = False
        settings_cache: dict[tuple[str, str], str] = {}
        for (left, right), source_window in zip(all_windows, source_windows, strict=True):
            fence = fences[source_window]
            settings = day_history(history, left, right)
            enriched_settings = day_history(history, left - timedelta(hours=6), right)
            existing = stored.get(left.isoformat())
            valid = bool(existing and existing.get("algorithm_version") == ALGORITHM_VERSION
                         and existing.get("cache_contract") == CACHE_CONTRACT
                         and all(existing.get("source_fence", {}).get(key) == value
                                 for key, value in _markers(fence).items())
                         and existing.get("source_selection") == selection
                         and existing.get("settings_version") == settings["fingerprint"]
                         and existing.get("period_end") == right.isoformat()
                         and _dependency_valid(existing.get("cache_dependency", {}), actual, history,
                                               selection, settings_cache)
                         and (previous is None or existing.get("previous_boundary_fingerprint") == _hash(previous)))
            facts = existing if valid else None
            if valid:
                hits += 1
            complete = right <= end
            seed = previous
            if left == predecessor[0] and existing:
                seed = existing.get("input_boundary")
                if seed and not _dependency_valid(_lineage(seed, selection), actual, history,
                                                  selection, settings_cache):
                    blocked = True
            if facts is None and calculate_daily and len(windows) == 1 and complete and not blocked:
                selected, current_selection, read_fence = prepare_regulation_day(
                    db, left, right, devices=devices, series=series, identifiers=identifiers,
                    source_fence=fence, history=history, scope=scope,
                )
                if selected is not None:
                    if current_selection != selection:
                        raise ValueError("regulation source selection changed during calculation")
                    boundary = previous if previous is not None else _bootstrap(selected, left, seed)
                    if seed and previous is None:
                        boundary["lineage"] = _lineage(seed, selection)
                    facts = build_regulation_facts(start=left, end=right, inputs=selected,
                                                   previous_boundary=boundary)
                    fingerprint = attach_regulation_cache(facts, source_fence=read_fence, selection=selection,
                                                          settings=enriched_settings, previous=boundary)
                    store.save(facts, fingerprint, source_fence=facts["source_fence"])
                    bounds = _dependency_bounds([facts], start, end)
                    dependency_start, dependency_end = min(dependency_start, bounds[0]), max(dependency_end, bounds[1])
                    built += 1
            previous = facts.get("boundary") if facts is not None else None
            if facts is None and left == predecessor[0] and existing:
                blocked = True
            if left == predecessor[0]:
                continue
            if facts is not None and left >= start and right <= end:
                accepted.append(facts)
            else:
                missing.append({"start": left.isoformat(), "end": right.isoformat(),
                                "reason": "partial_local_day" if not complete or left < start else
                                "stale_boundary_ancestor_requires_local_backfill" if blocked else
                                "missing_or_stale_facts"})
        circuits.append({"scope": scope, "device_id": device, "circuit_id": circuit, "facts": accepted,
                         "expected_days": len(windows), "missing_days": missing,
                         "settings_history_incomplete": history.get("incomplete", False)})
    return {"circuits": circuits, "built_days": built, "hit_days": hits, "unknowns": [],
            "dependency_start": dependency_start.isoformat(), "dependency_end": dependency_end.isoformat()}


def build_regulation_context(
    db: Database, start: datetime, end: datetime, *, timezone: str, calculate_daily: bool,
) -> dict[str, Any]:
    selected = validated_regulation_days(db, start, end, timezone=timezone, calculate_daily=calculate_daily)
    circuits = []
    for item in selected["circuits"]:
        summary = aggregate_regulation_facts(item["facts"], start=start, end=end, expected_days=item["expected_days"])
        summary.update({key: value for key, value in item.items() if key not in {"facts", "expected_days"}})
        if item["settings_history_incomplete"]:
            summary["incomplete"] = True
        circuits.append(summary)
    return {"regulation_facts": {"algorithm_version": ALGORITHM_VERSION, "circuits": circuits,
                                 "incomplete": not circuits or any(item["incomplete"] for item in circuits),
                                 "unknowns": selected["unknowns"],
                                 "dependency_start": selected.get("dependency_start", start.isoformat()),
                                 "dependency_end": selected.get("dependency_end", end.isoformat()),
                                 "cache": {"built_days": selected["built_days"], "hit_days": selected["hit_days"]},
                                 "source_binding": "current circuit/sensor association; historical rewiring unverified",
                                 "historical_rebuild": "bounded local export and compact upload; no AI replay"}}

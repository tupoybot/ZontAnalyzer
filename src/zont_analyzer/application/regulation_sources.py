"""Conservative circuit selection shared by report calculation and offline export.

Current configuration identifies sources; it does not establish historical control
settings. Missing/ambiguous measurements stay absent, including physical boiler
activity when only a circuit's heat request is available.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.adapters.ydb.regulation_markers import EVENT_FAMILIES
from zont_analyzer.analytics.dhw import parse_opentherm_flags
from zont_analyzer.analytics.evidence import NumericSample, SignalMetadata, SignalSeries, StateSample
from zont_analyzer.analytics.regulation import RegulationInputs
from zont_analyzer.analytics.series_semantics import is_setpoint_series
from zont_analyzer.application.ingestion import _object_names, heating_circuit_sensor_links
from zont_analyzer.application.regulation_reliability import apply_reliability_context

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.application import Database

SELECTION_VERSION = "regulation-sources-v2-family-projection"
SOURCE_LOOKAHEAD_SECONDS = 61


@dataclass(frozen=True)
class RegulationSources:
    inputs: tuple[RegulationInputs, ...]
    source_fence: dict[str, int | str]
    source_revision: str
    selections: dict[str, dict[str, Any]]


def _identity(row: dict[str, Any]) -> str:
    return "/".join(str(row[key]) for key in ("device_id", "source_type", "entity_id", "metric_key"))


def _external(row: dict[str, Any]) -> str:
    return str(row["entity_id"]).rsplit(":", 1)[-1]


def _one(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if len(rows) == 1 else None


def _circuits(device: dict[str, Any]) -> list[dict[str, Any]]:
    config = device.get("raw", {}).get("z3k_config", {})
    circuits = config.get("heating_circuits", []) if isinstance(config, dict) else []
    return [item for item in circuits if isinstance(item, dict) and item.get("id") is not None
            ] if isinstance(circuits, list) else []


def _targets(device: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured = {str(item["id"]): item for item in _circuits(device)}
    # Role metadata supports older controllers without a complete snapshot.
    return [row for row in rows if row["source_type"] == "z3k_heating_circuit"
            and row.get("role") == "target_temperature"
            and row["metric_key"] in {"target_temp", "setpoint_temp"}
            and configured.get(_external(row), {}).get("type", 3) == 3]


def regulation_scopes(
    devices: list[dict[str, Any]], series: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Discover cache scopes from metadata only, using the loader's exact rules."""
    result: set[tuple[str, str]] = set()
    for device in devices:
        device_id = str(device["id"])
        targets = _targets(device, [row for row in series if str(row["device_id"]) == device_id])
        for circuit_id in {_external(row) for row in targets}:
            if _one([row for row in targets if _external(row) == circuit_id]) is not None:
                result.add((device_id, circuit_id))
    return sorted(result)


def regulation_source_selections(
    devices: list[dict[str, Any]], series: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Run the exact loader selection rules without fetching any observations."""
    start = datetime(2000, 1, 1, tzinfo=UTC)
    return load_regulation_sources(None, start, start + timedelta(seconds=1), devices=devices, series=series,
                                   source_fence={}, _describe_only=True).selections


def load_regulation_sources(
    db: Database | None, start: datetime, end: datetime, *, device_ids: tuple[str, ...] | None = None,
    devices: list[dict[str, Any]] | None = None, series: list[dict[str, Any]] | None = None,
    source_fence: dict[str, int | str] | None = None,
    extend_setpoint_fence: bool = False, _describe_only: bool = False,
    _negative_recheck: bool = False,
) -> RegulationSources:
    """Read at most 31 days, six prior hours and one minute after the endpoint.

    A caller without device_ids uses the conservative global fence.
    A caller providing devices/series must also provide the fence captured before
    reading that metadata. The returned fence must be checked atomically on save.
    Settings remain unknown: a present-day snapshot cannot populate historical
    limits, operating mode or PID/PZA applicability. A settings-history consumer
    can enrich/split these inputs using its verified interval contract.
    """
    if end <= start or end - start > timedelta(days=31):
        raise ValueError("regulation source reads require a positive period of at most 31 days")
    if (devices is not None or series is not None) and source_fence is None:
        raise ValueError("preloaded source metadata requires a fence captured before its read")
    lookback = start - timedelta(hours=6)
    # A following preceding-minute aggregate can overlap the last minute of
    # this day. Include that observation to localize ambiguity across midnight.
    # The final second retains the endpoint in the half-open storage query.
    fetch_end = end + timedelta(seconds=SOURCE_LOOKAHEAD_SECONDS)
    if source_fence is None:
        assert db is not None
        repository = RegulationRepository(db.storage)
        source_fence = (repository.source_fence(start=lookback, end=fetch_end, device_ids=device_ids)
                        if device_ids is not None else repository.source_fence())
    source_fence = dict(source_fence)
    if devices is None:
        assert db is not None
        devices = db.list_devices()
    if series is None:
        assert db is not None
        series = db.list_series()
    hold_repository = RegulationRepository(db.storage) if db is not None and extend_setpoint_fence else None
    if device_ids is not None:
        devices = [row for row in devices if str(row["id"]) in device_ids]
        series = [row for row in series if str(row["device_id"]) in device_ids]
    links = heating_circuit_sensor_links(devices, _object_names(devices), series)
    numeric_cache: dict[int, tuple[tuple[datetime, float | None], ...]] = {}
    text_cache: dict[int, tuple[tuple[datetime, str], ...]] = {}
    hold_starts: list[datetime] = []
    held_series: dict[str, int] = {}
    event_contexts: dict[str, dict[str, Any]] = {}

    def signal(key: str, row: dict[str, Any]) -> SignalSeries:
        assert db is not None
        identifier = int(row["id"])
        setpoint = is_setpoint_series(str(row["source_type"]), str(row["metric_key"]))
        if identifier not in numeric_cache:
            numeric_cache[identifier] = tuple(db.fetch_numeric_observations(
                identifier, lookback, fetch_end, include_previous=setpoint,
            ))
        points = numeric_cache[identifier]
        if setpoint:
            hold_starts.extend(ts for ts, _value in points if ts < lookback)
            held_series[_identity(row)] = identifier
        return SignalSeries(
            key=key, metadata=SignalMetadata(
                identity=_identity(row), display_name=str(row.get("display_name") or row["entity_id"]),
                unit=str(row.get("unit") or "state"),
                provenance=f"{SELECTION_VERSION}; {row.get('provenance', row['source_type'])}",
                value_kind="setpoint" if setpoint else "measurement",
            ), samples=tuple(NumericSample(ts, value) for ts, value in points if value is not None),
            unknown_at=tuple(ts for ts, value in points if value is None),
        )

    inputs: list[RegulationInputs] = []
    selections: dict[str, dict[str, Any]] = {}
    for device in devices:
        device_id = str(device["id"])
        if not _describe_only:
            assert db is not None
            event_contexts[device_id] = RegulationRepository(db.storage).source_events(device_id, lookback, fetch_end)
            anchor = event_contexts[device_id].get("anchor")
            if isinstance(anchor, datetime) and anchor < lookback:
                hold_starts.append(anchor)
        rows = [row for row in series if str(row["device_id"]) == device_id]
        circuits = _circuits(device)
        targets = _targets(device, rows)
        circuit_ids = sorted({_external(row) for row in targets})
        consumers = set(circuit_ids) | {str(item["id"]) for item in circuits if item.get("type") == 3}
        boiler_ids = sorted({str(row["entity_id"]) for row in rows
                             if row["source_type"] == "z3k_boiler_adapter"})
        for circuit_id in circuit_ids:
            circuit_rows = [row for row in rows if row["source_type"] == "z3k_heating_circuit"
                            and _external(row) == circuit_id]
            target = _one([row for row in targets if _external(row) == circuit_id])
            if target is None:
                continue
            linked = {link.sensor_external_id for link in links
                      if link.device_id == device_id and link.circuit_external_id == circuit_id}
            room = _one([row for row in rows if _external(row) in linked
                         and row.get("role") in {"control_indoor_temperature", "room_temperature",
                                                 "indoor_temperature"}
                         and row["metric_key"] in {"temperature", "curr_temp", "temp"}])
            chosen: dict[str, dict[str, Any] | None] = {
                "target": target, "room": room,
                "request": _one([row for row in circuit_rows if row["metric_key"] == "worktime"]),
                "mode": _one([row for row in circuit_rows if row["metric_key"] == "mode_id"]),
                "controller_status": _one([row for row in rows if row["source_type"] == "ztc_state"
                                            and row["metric_key"] == "status_flags"]),
            }
            chosen["humidity"] = _one([row for row in rows if room is not None
                and row["entity_id"] == room["entity_id"] and row["source_type"] == room["source_type"]
                and row.get("role") == "humidity"])
            # One outside measurement on this controller is descriptive weather,
            # never evidence of room association or a PZA curve's active limit.
            chosen["outdoor"] = _one([row for row in rows if row.get("role") == "outdoor_temperature"])
            boiler: str | None = None
            association = "unknown"
            if len(boiler_ids) == 1 and len(consumers) == 1:
                boiler = boiler_ids[0]
                association = "single_adapter_and_single_heating_consumer_on_device"
            if boiler is not None:
                boiler_rows = [row for row in rows if row["source_type"] == "z3k_boiler_adapter"
                               and str(row["entity_id"]) == boiler]
                chosen["command"] = _one([row for row in boiler_rows if row["metric_key"] == "cs"])
                chosen["actual"] = _one([row for row in boiler_rows if row["metric_key"] == "bt"])
                state_row = _one([row for row in boiler_rows if row["metric_key"] == "s"])
            else:
                state_row = None
            states: tuple[StateSample, ...] = ()
            state_unknown_at: tuple[datetime, ...] = ()
            if state_row is not None and not _describe_only:
                assert db is not None
                identifier = int(state_row["id"])
                if identifier not in text_cache:
                    text_cache[identifier] = tuple(db.fetch_text_samples(identifier, lookback, fetch_end))
                valid_states = []
                for ts, value in text_cache[identifier]:
                    try:
                        parsed = ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        continue
                    if isinstance(parsed, (list, tuple, set, frozenset)) and all(
                            isinstance(flag, str) for flag in parsed):
                        valid_states.append(StateSample(ts, parse_opentherm_flags(value), _identity(state_row)))
                states = tuple(valid_states)
                valid_at = {sample.timestamp for sample in states}
                state_unknown_at = tuple(ts for ts, _value in db.fetch_numeric_observations(
                    identifier, lookback, fetch_end,
                ) if ts not in valid_at)
            selected_signals = {} if _describe_only else {
                key: signal(key, row) for key, row in chosen.items() if row is not None}
            scope = f"{device_id}/{circuit_id}"
            selections[scope] = {
                "version": SELECTION_VERSION,
                "signals": {key: _identity(row) for key, row in chosen.items() if row is not None},
                "boiler_association": association, "settings_applicability": "unknown",
                "room_link_applicability": "current_discovered_binding; historical_binding_unverified",
                "room_link": next((link.provenance for link in links
                                   if link.device_id == device_id
                                   and link.circuit_external_id == circuit_id), "unknown"),
            }
            selected_inputs = RegulationInputs(
                circuit_id=circuit_id, device_id=device_id, signals=selected_signals,
                request_semantics="preceding_minute", states=states, state_unknown_at=state_unknown_at,
                humidity_room_identity=_identity(room) if room is not None and chosen["humidity"] else None,
            )
            inputs.append(apply_reliability_context(selected_inputs, event_contexts[device_id], start=lookback, end=end)
                          if not _describe_only else selected_inputs)
    if hold_repository is not None:
        keys = []
        if hold_starts:
            day, last = min(hold_starts).astimezone(UTC).date(), lookback.astimezone(UTC).date()
            while day <= last:
                keys.append("meta:telemetry-day:" + day.isoformat())
                day += timedelta(days=1)
                if len(keys) > 8192:
                    raise ValueError("setpoint hold exceeds the bounded regulation metadata fence")
        added = hold_repository.read_fence([key for key in keys if key not in source_fence])
        source_fence.update(added)
        # Anchor days were discovered by the initial predecessor query. Re-read
        # held setpoints AFTER fencing those days so an intervening late insert
        # cannot leave stale values under a newly captured marker. Existing day
        # markers remain unchanged and are checked atomically on save.
        if added:
            assert db is not None
            refreshed = {identity: tuple(db.fetch_numeric_observations(
                identifier, lookback, fetch_end, include_previous=True,
            )) for identity, identifier in held_series.items()}
            inputs = [replace(item, signals={key: replace(
                series, samples=tuple(NumericSample(ts, value) for ts, value in refreshed[series.metadata.identity]
                                      if value is not None),
                unknown_at=tuple(ts for ts, value in refreshed[series.metadata.identity] if value is None),
            ) if series.metadata.identity in refreshed else series for key, series in item.signals.items()})
                      for item in inputs]
            for device_id in event_contexts:
                event_contexts[device_id] = hold_repository.source_events(device_id, lookback, fetch_end)
            # Replace initial event-derived masks, rather than retaining stale
            # exclusions when a late restore appeared during fence discovery.
            inputs = [apply_reliability_context(
                replace(item, exclusions=(), source_unknowns=(), reliability_context={}),
                event_contexts[item.device_id], start=lookback, end=end,
            ) for item in inputs]
    guarded_inputs = []
    for item in inputs:
        guards = []
        for signal_series in item.signals.values():
            if signal_series.metadata.value_kind != "setpoint":
                continue
            if not any(sample.timestamp < lookback for sample in signal_series.samples) and not any(
                    timestamp < lookback for timestamp in signal_series.unknown_at):
                guards.append((f"regulation-first-setpoint:{held_series[signal_series.metadata.identity]}",
                               int(lookback.timestamp())))
        for family, types in EVENT_FAMILIES.items():
            if not any(event.event_type in types and event.timestamp_utc < lookback
                       for event in event_contexts.get(item.device_id, {}).get("events", [])):
                guards.append((f"regulation-first-event:{item.device_id}:{family}", int(lookback.timestamp())))
        guarded_inputs.append(replace(item, negative_input_guards=tuple(guards)))
    inputs = guarded_inputs
    if hold_repository is not None and any(item.negative_input_guards for item in inputs):
        guard_keys = sorted({"meta:" + key for item in inputs for key, _cutoff in item.negative_input_guards})
        first_arrivals = hold_repository.read_fence(guard_keys)
        stale_minima = {key: int(first_arrivals["meta:" + key]) for item in inputs
                        for key, cutoff in item.negative_input_guards
                        if first_arrivals["meta:" + key] != "" and int(first_arrivals["meta:" + key]) < cutoff}
        if stale_minima:
            # Monotone minima may outlive a corrected/deleted old observation.
            # Retain that floor plus day markers through the negative interval;
            # a fresh negative proof must not become permanently uncacheable.
            day = datetime.fromtimestamp(min(stale_minima.values()), UTC).date()
            last = lookback.astimezone(UTC).date()
            extra = []
            while day <= last:
                key = "meta:telemetry-day:" + day.isoformat()
                if key not in source_fence:
                    extra.append(key)
                day += timedelta(days=1)
                if len(extra) > 8192:
                    raise ValueError("negative input interval exceeds the bounded regulation fence")
            source_fence.update(hold_repository.read_fence(extra))
            if extra and not _negative_recheck:
                return load_regulation_sources(
                    db, start, end, device_ids=device_ids, devices=devices, series=series,
                    source_fence=source_fence, extend_setpoint_fence=True, _negative_recheck=True,
                )
            inputs = [replace(item, negative_input_guards=tuple(
                (key, min(cutoff, stale_minima.get(key, cutoff))) for key, cutoff in item.negative_input_guards))
                      for item in inputs]
    revision = hashlib.sha256(json.dumps(source_fence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return RegulationSources(tuple(inputs), source_fence, revision, selections)

"""Compact control observations supplement immutable legacy configuration snapshots."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.analytics.settings_history import fingerprint, observed_controls, settings_timeline

from .database import Transaction, YdbDatabase


def observe(
    tx: Transaction, device: dict[str, Any], device_id: str, at: int, *, snapshot_id: int | None = None,
) -> tuple[int, int, list[str]] | None:
    from .telemetry import encode, next_id

    rows = tx.execute(
        "DECLARE $device AS Utf8; SELECT circuit_id,payload FROM control_setting_heads WHERE device_id=$device;",
        {"$device": device_id},
    )[0].rows
    heads = {row.circuit_id: json.loads(row.payload) for row in rows}
    legacy: dict[str, dict[str, Any]] = {}
    prior = tx.execute(
        "DECLARE $device AS Utf8; SELECT id,payload,captured_at FROM config_snapshots VIEW by_observed "
        "WHERE device_id=$device ORDER BY captured_at DESC,id DESC LIMIT 1;", {"$device": device_id},
    )[0].rows
    if not heads and prior:
        legacy = {circuit: {"state": state, "observed_at": prior[0].captured_at,
                            "last_confirmed_at": None}
                  for circuit, state in observed_controls(json.loads(prior[0].payload)).items()}
    states = observed_controls(device, known=set(heads) | set(legacy))
    earliest: int | None = None
    changed_circuits = []
    for circuit, state in states.items():
        previous = heads.get(circuit)
        baseline = previous or legacy.get(circuit)
        last_known = (baseline.get("last_confirmed_at") or baseline["observed_at"]) if baseline else at
        observed_at = max(at, last_known)
        version = fingerprint(state)
        writer_gap = bool(previous and prior and (
            prior[0].captured_at > previous["last_confirmed_at"]
            or prior[0].id > previous.get("last_seen_snapshot_id", previous.get("ordering_snapshot_id", 0))
        ))
        latest_snapshot_id = snapshot_id or (prior[0].id if prior else 0)
        if previous and previous["version"] == version and not writer_gap:
            event = {**previous, "last_confirmed_at": observed_at, "last_seen_snapshot_id": latest_snapshot_id}
        else:
            event = {"event_id": next_id(tx, "control_settings"), "observed_at": observed_at,
                     "configuration_snapshot_id": snapshot_id,
                     "ordering_snapshot_id": latest_snapshot_id, "last_seen_snapshot_id": latest_snapshot_id,
                     "last_confirmed_at": observed_at, "version": version, "state": state,
                     "kind": "change" if baseline and baseline["state"] != state else "first_observation",
                     "boundary": {"after": last_known if baseline and baseline["state"] != state else None,
                                  "no_later_than": observed_at, "exact_effective_at": None},
                     "changed_fields": sorted(
                         key for key in state.get("fields", {}).keys() | (
                             baseline["state"].get("fields", {}).keys() if baseline else set()
                         ) if state.get("fields", {}).get(key) != (
                             baseline["state"].get("fields", {}).get(key) if baseline else None
                         )
                     )}
            lower = last_known if writer_gap or baseline and baseline["state"] != state else at
            earliest = min(earliest, lower) if earliest is not None else lower
            changed_circuits.append(circuit)
        payload = encode(event)
        params = {"$device": device_id, "$circuit": circuit, "$at": event["observed_at"],
                  "$event": event["event_id"], "$payload": payload}
        tx.execute(
            "DECLARE $device AS Utf8; DECLARE $circuit AS Utf8; DECLARE $at AS Int64; "
            "DECLARE $event AS Int64; DECLARE $payload AS Utf8; "
            "UPSERT INTO control_setting_observations (device_id,circuit_id,observed_at,event_id,payload) "
            "VALUES ($device,$circuit,$at,$event,$payload); "
            "UPSERT INTO control_setting_heads (device_id,circuit_id,payload) VALUES ($device,$circuit,$payload);",
            params,
        )
    return (earliest, at, changed_circuits) if earliest is not None else None


def period_history(
    db: YdbDatabase, device_id: str, circuit_id: str, start: datetime, end: datetime, *, limit: int = 256,
) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("control history limit must be between 1 and 1000")
    left, right = int(start.timestamp() * 1_000_000), int(end.timestamp() * 1_000_000)
    declarations = ("DECLARE $device AS Utf8; DECLARE $circuit AS Utf8; "
                    "DECLARE $left AS Int64; DECLARE $right AS Int64; ")
    params = {"$device": device_id, "$circuit": circuit_id, "$left": left, "$right": right}
    predicate = "device_id=$device AND circuit_id=$circuit"
    results = db.execute(
        declarations + "DECLARE $limit AS Uint64; "
        f"SELECT payload FROM control_setting_observations WHERE {predicate} AND observed_at<$left "
        "ORDER BY observed_at DESC,event_id DESC LIMIT 1; "
        f"SELECT payload FROM control_setting_observations WHERE {predicate} "
        "AND observed_at>=$left AND observed_at<$right "
        "ORDER BY observed_at,event_id LIMIT $limit; "
        f"SELECT payload FROM control_setting_observations WHERE {predicate} AND observed_at>=$right "
        "ORDER BY observed_at,event_id LIMIT 1;",
        {**params, "$limit": ydb.TypedValue(limit + 1, ydb.PrimitiveType.Uint64)},
    )
    events = [json.loads(row.payload) for result in results for row in result.rows]
    incomplete = len(results[1].rows) > limit
    # Base snapshots remain read-only, including observations made during an
    # older-image rollback after compact history was first introduced.
    # Window + predecessor/successor are index bounded, never latest-N globally.
    legacy = db.execute(
        "DECLARE $device AS Utf8; DECLARE $left AS Int64; DECLARE $right AS Int64; DECLARE $limit AS Uint64; "
        "SELECT id,payload,captured_at FROM config_snapshots VIEW by_observed "
        "WHERE device_id=$device AND captured_at<$left ORDER BY captured_at DESC,id DESC LIMIT 1; "
        "SELECT id,payload,captured_at FROM config_snapshots VIEW by_observed "
        "WHERE device_id=$device AND captured_at>=$left AND captured_at<$right "
        "ORDER BY captured_at,id LIMIT $limit; "
        "SELECT id,payload,captured_at FROM config_snapshots VIEW by_observed "
        "WHERE device_id=$device AND captured_at>=$right ORDER BY captured_at,id LIMIT 1;",
        {"$device": device_id, "$left": left, "$right": right,
         "$limit": ydb.TypedValue(limit + 1, ydb.PrimitiveType.Uint64)},
    )
    incomplete = incomplete or len(legacy[1].rows) > limit
    represented = {event.get("configuration_snapshot_id") for event in events}
    for event in events:
        event["sequence"] = event.get("ordering_snapshot_id", event["event_id"])
        event["ordering_rank"] = 1
        event["event_id"] = f"control:{event['event_id']}"
    for result in legacy:
        for row in result.rows:
            if row.id in represented:
                continue
            states = observed_controls(json.loads(row.payload), known={circuit_id})
            events.append({"event_id": f"snapshot:{row.id}", "sequence": row.id,
                           "observed_at": row.captured_at,
                           "last_confirmed_at": None, "state": states[circuit_id]})
    # A bounded read must say it is incomplete, rather than presenting its final
    # row as the last known version when more intermediate changes may exist.
    return settings_timeline(events, device_id=device_id, circuit_id=circuit_id,
                             start=start, end=end, incomplete=incomplete)

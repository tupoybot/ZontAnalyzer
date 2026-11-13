"""Complete generation-based reliability-event projection, separate from telemetry.

Readers use one snapshot transaction. A generation is usable only after a fenced
full event-only import; subsequent canonical event corrections update it in the
same transaction. An uncovered writer revision invalidates readiness.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.analytics.reliability import CONNECTION_LOSS_TYPES
from zont_analyzer.domain import SourceEvent

from .database import Transaction, YdbDatabase
from .regulation_markers import EVENT_FAMILIES, observe_first_inputs

CONTRACT = "regulation-event-projection-v1:" + hashlib.sha256(json.dumps(
    {key: sorted(values) for key, values in EVENT_FAMILIES.items()}, sort_keys=True,
).encode()).hexdigest()
TABLE = "regulation_event_projection"
PAGE_SIZE = 500


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _head_key(device: str) -> str:
    return "regulation-event-projection:" + device


def _read_head(database: Transaction | YdbDatabase, device: str) -> dict[str, Any] | None:
    rows = database.execute("DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;",
                            {"$key": _head_key(device)})[0].rows
    value: dict[str, Any] | None = json.loads(rows[0].value) if rows else None
    return value


def _write_head(tx: Transaction, device: str, head: dict[str, Any]) -> None:
    tx.execute("DECLARE $key AS Utf8; DECLARE $value AS Utf8; UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
               {"$key": _head_key(device), "$value": _encode(head)})


def _revision(database: Transaction | YdbDatabase, device: str) -> int:
    rows = database.execute("DECLARE $scope AS Utf8; SELECT revision FROM revisions WHERE scope=$scope;",
                            {"$scope": "telemetry:" + device})[0].rows
    return int(rows[0].revision) if rows else 0


def projection_states(database: Transaction | YdbDatabase, devices: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not devices:
        return {}
    result = database.execute(
        "DECLARE $keys AS List<Utf8>; DECLARE $scopes AS List<Utf8>; "
        "SELECT key,value FROM app_meta WHERE key IN $keys; "
        "SELECT scope,revision FROM revisions WHERE scope IN $scopes;",
        {"$keys": ydb.TypedValue([_head_key(device) for device in devices], ydb.ListType(ydb.PrimitiveType.Utf8)),
         "$scopes": ydb.TypedValue(["telemetry:" + device for device in devices],
                                    ydb.ListType(ydb.PrimitiveType.Utf8))},
    )
    heads = {row.key: json.loads(row.value) for row in result[0].rows}
    revisions = {row.scope: int(row.revision) for row in result[1].rows}
    states = {}
    for device in devices:
        head = heads.get(_head_key(device), {})
        ready = bool(head.get("contract") == CONTRACT and head.get("ready") is True
                     and head.get("covered_telemetry_revision") == revisions.get("telemetry:" + device, 0))
        states[device] = {"contract": CONTRACT, "generation": head.get("generation"), "ready": ready}
    return states


def _micros(timestamp: datetime) -> int:
    delta = timestamp.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _family(event: SourceEvent) -> str | None:
    return next((name for name, types in EVENT_FAMILIES.items() if event.event_type in types), None)


def projected_row(device: str, generation: str, event: SourceEvent) -> dict[str, Any] | None:
    family = _family(event)
    if family is None:
        return None
    if event.device_id != device:
        raise ValueError("projection event belongs to another device")
    return {"device_id": device, "generation": generation, "family": family,
            "timestamp_us": _micros(event.timestamp_utc),
            "order_rank": int(event.event_type not in CONNECTION_LOSS_TYPES),
            "id": event.id, "payload": event.model_dump_json()}


def _row_type(*, payload: bool) -> Any:
    result = (ydb.StructType().add_member("device_id", ydb.PrimitiveType.Utf8)
              .add_member("generation", ydb.PrimitiveType.Utf8).add_member("family", ydb.PrimitiveType.Utf8)
              .add_member("timestamp_us", ydb.PrimitiveType.Int64).add_member("order_rank", ydb.PrimitiveType.Int64)
              .add_member("id", ydb.PrimitiveType.Utf8))
    return result.add_member("payload", ydb.PrimitiveType.Utf8) if payload else result


def _upsert(tx: Transaction, rows: list[dict[str, Any]]) -> None:
    if rows:
        tx.execute(
            "DECLARE $rows AS List<Struct<device_id:Utf8,generation:Utf8,family:Utf8,timestamp_us:Int64,"
            "order_rank:Int64,id:Utf8,payload:Utf8>>; "
            "UPSERT INTO regulation_event_projection SELECT * FROM AS_TABLE($rows);",
            {"$rows": ydb.TypedValue(rows, ydb.ListType(_row_type(payload=True)))},
        )


def _delete(tx: Transaction, rows: list[dict[str, Any]]) -> None:
    if rows:
        keys = [{key: value for key, value in row.items() if key != "payload"} for row in rows]
        tx.execute(
            "DECLARE $rows AS List<Struct<device_id:Utf8,generation:Utf8,family:Utf8,timestamp_us:Int64,"
            "order_rank:Int64,id:Utf8>>; DELETE FROM regulation_event_projection ON SELECT * FROM AS_TABLE($rows);",
            {"$rows": ydb.TypedValue(keys, ydb.ListType(_row_type(payload=False)))},
        )


def advance_projection(tx: Transaction, device: str, previous_revision: int, new_revision: int,
                       previous_events: list[SourceEvent], events: list[SourceEvent]) -> None:
    """Called for every changed telemetry transaction, including numeric-only writes."""
    head = _read_head(tx, device)
    for moved_device in sorted({event.device_id for event in previous_events if event.device_id != device}):
        # The canonical source changed for the old device too. Fence exports
        # and readiness there even though the ingest request targets a new one.
        moved_revision = _revision(tx, moved_device) + 1
        tx.execute("DECLARE $scope AS Utf8; DECLARE $revision AS Int64; "
                   "UPSERT INTO revisions (scope,revision) VALUES ($scope,$revision);",
                   {"$scope": "telemetry:" + moved_device, "$revision": moved_revision})
        moved = _read_head(tx, moved_device)
        if moved is not None:
            _write_head(tx, moved_device, {**moved, "ready": False, "reason": "canonical_event_moved_device",
                                           "invalidated_at_revision": moved_revision})
    if head is None or not head.get("ready"):
        return
    if head.get("contract") != CONTRACT or head.get("covered_telemetry_revision") != previous_revision:
        _write_head(tx, device, {**head, "ready": False, "reason": "uncovered_writer_revision",
                                 "invalidated_at_revision": new_revision})
        return
    generation = head["generation"]
    old = [row for event in previous_events if event.device_id == device
           if (row := projected_row(device, generation, event)) is not None]
    new = [row for event in events if (row := projected_row(device, generation, event)) is not None]
    _delete(tx, old)
    _upsert(tx, new)
    _write_head(tx, device, {**head, "covered_telemetry_revision": new_revision})


class EventProjectionRepository:
    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def revision(self, device: str) -> int:
        return _revision(self.db, device)

    def source_page(self, device: str, *, after: tuple[int, str] | None = None) -> list[dict[str, Any]]:
        timestamp, identifier = after or (-(1 << 63), "")
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $after AS Int64; DECLARE $id AS Utf8; "
            "SELECT timestamp_utc,id,payload FROM source_events WHERE device_id=$device "
            "AND (timestamp_utc > $after OR (timestamp_utc=$after AND id > $id)) "
            "ORDER BY timestamp_utc,id LIMIT 500;",
            {"$device": device, "$after": timestamp, "$id": identifier},
        )[0].rows
        return [dict(row) for row in rows]

    def source_context(self, device: str, start: datetime, end: datetime) -> dict[str, Any]:
        def read(tx: Transaction) -> dict[str, Any]:
            state = projection_states(tx, (device,))[device]
            if not state["ready"]:
                return {"events": [], "incomplete": True, "prior_state_complete": False,
                        "window_truncated": False, "projection_ready": False,
                        "reason": "reliability_event_projection_requires_fenced_backfill", "anchor": start,
                        "event_projection": state}
            query = ("DECLARE $device AS Utf8; DECLARE $generation AS Utf8; "
                     "DECLARE $start AS Int64; DECLARE $end AS Int64; ")
            query += " ".join(f"DECLARE $family{index} AS Utf8;" for index in range(len(EVENT_FAMILIES))) + " "
            parameters: dict[str, Any] = {"$device": device, "$generation": state["generation"],
                                          "$start": _micros(start), "$end": _micros(end)}
            for index, family in enumerate(sorted(EVENT_FAMILIES)):
                parameters[f"$family{index}"] = family
                prefix = ("SELECT payload FROM regulation_event_projection WHERE device_id=$device "
                          f"AND generation=$generation AND family=$family{index} ")
                query += prefix + ("AND timestamp_us < $start "
                                   "ORDER BY timestamp_us DESC,order_rank DESC,id DESC LIMIT 1; ")
                query += prefix + ("AND timestamp_us >= $start AND timestamp_us < $end "
                                   "ORDER BY timestamp_us,order_rank,id LIMIT 257; ")
            results = tx.execute(query, parameters)
            events = []
            prior = []
            truncated = False
            for index, result in enumerate(results):
                selected = result.rows[:256] if index % 2 else result.rows
                decoded = [SourceEvent.model_validate_json(row.payload) for row in selected]
                events.extend(decoded)
                if index % 2:
                    truncated = truncated or len(result.rows) > 256
                else:
                    prior.extend(decoded)
            return {"events": sorted(events, key=lambda event: (
                        event.timestamp_utc, event.event_type not in CONNECTION_LOSS_TYPES, event.id)),
                    "incomplete": truncated, "prior_state_complete": True, "window_truncated": truncated,
                    "projection_ready": True, "projection_contract": CONTRACT,
                    "event_projection": state,
                    "anchor": min((event.timestamp_utc for event in prior), default=start)}
        return self.db.transaction(read)

    def write_generation(self, device: str, generation: str, revision: int,
                         events: list[SourceEvent]) -> bool:
        if len(events) > PAGE_SIZE:
            raise ValueError("projection upload batch exceeds 500 events")
        rows = [row for event in events if (row := projected_row(device, generation, event)) is not None]

        def write(tx: Transaction) -> bool:
            head = _read_head(tx, device)
            if head and head.get("generation") == generation:
                if (head.get("ready") and head.get("contract") == CONTRACT
                        and head.get("covered_telemetry_revision") == _revision(tx, device)):
                    return False
                raise ValueError("cannot write an active or invalidated projection generation")
            if _revision(tx, device) != revision:
                raise ValueError("canonical source changed during projection upload")
            _upsert(tx, rows)
            return True
        return self.db.transaction(write)

    def publish_generation(self, device: str, generation: str, revision: int,
                           count: int, first_events: list[SourceEvent]) -> bool:
        def publish(tx: Transaction) -> bool:
            head = _read_head(tx, device)
            current = _revision(tx, device)
            if head and head.get("generation") == generation:
                if (head.get("ready") and head.get("contract") == CONTRACT
                        and head.get("covered_telemetry_revision") == current):
                    return False
                raise ValueError("cannot republish an invalidated active generation")
            if current != revision:
                raise ValueError("canonical source changed before projection activation")
            rows = tx.execute(
                "DECLARE $device AS Utf8; DECLARE $generation AS Utf8; "
                "SELECT COUNT(*) AS count FROM regulation_event_projection "
                "WHERE device_id=$device AND generation=$generation;",
                {"$device": device, "$generation": generation},
            )[0].rows
            if int(rows[0].count) != count:
                raise ValueError("projection generation is incomplete")
            observe_first_inputs(tx, {}, first_events)
            _write_head(tx, device, {"contract": CONTRACT, "generation": generation, "ready": True,
                                     "covered_telemetry_revision": revision, "initialized_event_count": count})
            return True
        return self.db.transaction(publish)

    def cleanup_generation(self, device: str, generation: str, *, max_batches: int = 20) -> int:
        if not 1 <= max_batches <= 200:
            raise ValueError("cleanup requires one to 200 bounded batches")
        removed = 0
        for _ in range(max_batches):
            def clean(tx: Transaction) -> int:
                head = _read_head(tx, device)
                if head and head.get("generation") == generation:
                    raise ValueError("cannot delete the active projection generation")
                rows = tx.execute(
                    "DECLARE $device AS Utf8; DECLARE $generation AS Utf8; "
                    "SELECT device_id,generation,family,timestamp_us,order_rank,id FROM regulation_event_projection "
                    "WHERE device_id=$device AND generation=$generation "
                    "ORDER BY family,timestamp_us,order_rank,id LIMIT 500;",
                    {"$device": device, "$generation": generation},
                )[0].rows
                _delete(tx, [dict(row) for row in rows])
                return len(rows)
            count = self.db.transaction(clean)
            removed += count
            if count < PAGE_SIZE:
                break
        return removed

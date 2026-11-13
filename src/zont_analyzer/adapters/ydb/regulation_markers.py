"""Monotone first-arrival metadata for negative historical input dependencies."""
from __future__ import annotations

import ydb  # type: ignore[import-untyped]

from zont_analyzer.analytics.reliability import (
    BOILER_LOSS_TYPES,
    BOILER_RESTORE_TYPES,
    CONNECTION_LOSS_TYPES,
    CONNECTION_RESTORE_TYPES,
    CONTROLLER_OFF_TYPES,
    CONTROLLER_ON_TYPES,
    MAIN_POWER_LOSS_TYPES,
    MAIN_POWER_RESTORE_TYPES,
)
from zont_analyzer.domain import SourceEvent

from .database import Transaction

EVENT_FAMILIES = {
    "boiler": BOILER_LOSS_TYPES | BOILER_RESTORE_TYPES,
    "power": MAIN_POWER_LOSS_TYPES | MAIN_POWER_RESTORE_TYPES,
    "controller": CONTROLLER_OFF_TYPES | CONTROLLER_ON_TYPES,
    "cloud": CONNECTION_LOSS_TYPES | CONNECTION_RESTORE_TYPES,
}


def observe_first_inputs(tx: Transaction, setpoints: dict[int, int], events: list[SourceEvent]) -> None:
    candidates = {f"regulation-first-setpoint:{identifier}": timestamp for identifier, timestamp in setpoints.items()}
    for event in events:
        for family, types in EVENT_FAMILIES.items():
            if event.event_type in types:
                key = f"regulation-first-event:{event.device_id}:{family}"
                timestamp = int(event.timestamp_utc.timestamp())
                candidates[key] = min(timestamp, candidates.get(key, timestamp))
    if not candidates:
        return
    rows = tx.execute(
        "DECLARE $keys AS List<Utf8>; SELECT key,value FROM app_meta WHERE key IN $keys;",
        {"$keys": ydb.TypedValue(list(candidates), ydb.ListType(ydb.PrimitiveType.Utf8))},
    )[0].rows
    existing = {row.key: int(row.value) for row in rows}
    changed = [{"key": key, "value": str(value)} for key, value in candidates.items()
               if key not in existing or value < existing[key]]
    if changed:
        row_type = (ydb.StructType().add_member("key", ydb.PrimitiveType.Utf8)
                    .add_member("value", ydb.PrimitiveType.Utf8))
        tx.execute("DECLARE $rows AS List<Struct<key:Utf8,value:Utf8>>; "
                   "UPSERT INTO app_meta SELECT * FROM AS_TABLE($rows);",
                   {"$rows": ydb.TypedValue(changed, ydb.ListType(row_type))})

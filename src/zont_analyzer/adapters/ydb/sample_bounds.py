"""Durable global sample boundaries, maintained with telemetry transactions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from .database import Transaction, YdbDatabase

KEY = "telemetry-bounds:v1"
Bounds = tuple[int | None, int | None]


@dataclass(frozen=True)
class _Snapshot:
    bounds: Bounds | None
    recorded: dict[str, int] | None
    actual: dict[str, int]


def _read(reader: Transaction | YdbDatabase) -> _Snapshot:
    # One Query API execution observes metadata and the small per-device
    # revision range together. Older releases already advance these revisions.
    metadata, revisions = reader.execute(
        "DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key; "
        "SELECT scope,revision FROM revisions WHERE scope>='telemetry:' AND scope<'telemetry;';",
        {"$key": KEY},
    )
    actual = {str(row.scope): int(row.revision) for row in revisions.rows}
    if not metadata.rows:
        return _Snapshot(None, None, actual)
    value = json.loads(metadata.rows[0].value)
    first, last = value["first"], value["last"]
    if not ((first is None and last is None)
            or (type(first) is int and type(last) is int and first <= last)):
        raise ValueError("invalid persisted telemetry bounds")
    recorded = value.get("revisions")
    if recorded is not None and (not isinstance(recorded, dict) or any(
        not isinstance(key, str) or type(revision) is not int for key, revision in recorded.items()
    )):
        raise ValueError("invalid persisted telemetry revisions")
    return _Snapshot((first, last), recorded, actual)


def _store(tx: Transaction, bounds: Bounds, revisions: dict[str, int]) -> None:
    tx.execute(
        "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
        "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
        {"$key": KEY, "$value": json.dumps(
            {"first": bounds[0], "last": bounds[1], "revisions": revisions}, separators=(",", ":"),
        )},
    )


def ensure(tx: Transaction) -> Bounds:
    """Initialize legacy data once, under the same serializable lock as writers."""
    current = _read(tx)
    if current.bounds is not None and current.recorded == current.actual:
        return current.bounds
    return _bootstrap(tx, current.actual)


def _bootstrap(tx: Transaction, revisions: dict[str, int]) -> Bounds:
    first: int | None = None
    last: int | None = None
    after: int | None = None
    while True:
        # Page the catalogue index, never the sample table. Each sample lookup
        # reads one end of a primary-key range regardless of history length.
        series = tx.execute(
            ("DECLARE $after AS Int64; " if after is not None else "")
            + "SELECT id FROM telemetry_series VIEW by_id "
            + ("WHERE id>$after " if after is not None else "")
            + "ORDER BY id LIMIT 100;",
            {"$after": after} if after is not None else None,
        )[0].rows
        if not series:
            break
        for row in series:
            results = tx.execute(
                "DECLARE $id AS Int64; "
                "SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id "
                "ORDER BY timestamp_utc ASC LIMIT 1; "
                "SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id "
                "ORDER BY timestamp_utc DESC LIMIT 1;", {"$id": int(row.id)},
            )
            if results[0].rows:
                low, high = int(results[0].rows[0].timestamp_utc), int(results[1].rows[0].timestamp_utc)
                first = low if first is None else min(first, low)
                last = high if last is None else max(last, high)
        after = int(series[-1].id)
    bounds = first, last
    _store(tx, bounds, revisions)
    return bounds


def extend(tx: Transaction, timestamps: list[int], device_id: str, revision: int) -> None:
    """Call after samples and their revision change, in the same transaction."""
    current = _read(tx)
    expected = dict(current.actual)
    scope = "telemetry:" + device_id
    if revision == 1:
        expected.pop(scope, None)
    else:
        expected[scope] = revision - 1
    if current.bounds is None or current.recorded not in (expected, current.actual):
        # A previous release may have written samples without maintaining this
        # metadata. Include those changes and this transaction's samples once.
        _bootstrap(tx, current.actual)
        return
    first, last = current.bounds
    if timestamps:
        first = min(timestamps) if first is None else min(first, min(timestamps))
        last = max(timestamps) if last is None else max(last, max(timestamps))
    _store(tx, (first, last), current.actual)


def invalidate(tx: Transaction) -> None:
    """Bulk import or deletion must invalidate within its sample transaction."""
    tx.execute("DECLARE $key AS Utf8; DELETE FROM app_meta WHERE key=$key;", {"$key": KEY})


def sample_bounds(db: YdbDatabase) -> tuple[datetime | None, datetime | None]:
    current = _read(db)
    bounds = current.bounds
    if bounds is None or current.recorded != current.actual:
        bounds = db.transaction(ensure)
    first, last = bounds
    return (datetime.fromtimestamp(first, UTC) if first is not None else None,
            datetime.fromtimestamp(last, UTC) if last is not None else None)

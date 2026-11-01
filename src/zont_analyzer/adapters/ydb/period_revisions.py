"""Batch exact period fingerprints without changing their persisted format."""
from __future__ import annotations

import hashlib
import heapq
import json
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase
from .telemetry import encode, utc_seconds

if TYPE_CHECKING:
    from .application import Database

Window = tuple[datetime, datetime]


def _telemetry_revision(database: Transaction | YdbDatabase) -> str:
    rows = database.execute(
        "SELECT scope,revision FROM revisions WHERE "
        "(scope >= 'telemetry:' AND scope < 'telemetry;') OR "
        "(scope >= 'series:' AND scope < 'series;') ORDER BY scope;",
    )[0].rows
    return hashlib.sha256(encode([[row.scope, row.revision] for row in rows]).encode()).hexdigest()


def telemetry_day_revisions(db: Database, windows: list[Window]) -> dict[Window, str]:
    """Cheap conservative cache dependencies, not content fingerprints.

    Every changed ingestion transaction writes its affected UTC day markers.
    A missing marker is also a dependency: the first subsequent write creates it.
    Callers must fence their reads and cache writes with the input revision.
    """
    windows = list(dict.fromkeys(windows))
    if not windows:
        return {}
    if any(end <= start for start, end in windows):
        raise ValueError("period end must be after its start")
    first = min(start for start, _ in windows)
    last = max(end for _, end in windows)
    lower_marker = f"telemetry-day:{first.astimezone(UTC).date().isoformat()}"
    upper_marker = f"telemetry-day:{(last - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()}"
    rows = []
    after = ""
    while True:
        page = db.storage.execute(
            "DECLARE $first AS Utf8; DECLARE $last AS Utf8; DECLARE $after AS Utf8; "
            "SELECT key,value FROM app_meta WHERE key >= $first AND key <= $last "
            "AND key > $after ORDER BY key LIMIT 1000;",
            {"$first": lower_marker, "$last": upper_marker, "$after": after},
        )[0].rows
        rows.extend(page)
        if len(page) < 1000:
            break
        after = page[-1].key
    marker_keys = [row.key for row in rows]
    markers: dict[Window, str] = {}
    for window in windows:
        start, end = window
        lower = f"telemetry-day:{start.astimezone(UTC).date().isoformat()}"
        upper = f"telemetry-day:{(end - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()}"
        markers[window] = hashlib.sha256(json.dumps(
            [[row.key, row.value] for row in rows[bisect_left(marker_keys, lower):bisect_right(marker_keys, upper)]],
        ).encode()).hexdigest()
    return markers


def period_data_revisions(db: Database, windows: list[Window]) -> dict[Window, str]:
    windows = list(dict.fromkeys(windows))
    if not windows:
        return {}
    source_revision = _telemetry_revision(db.storage)
    markers = telemetry_day_revisions(db, windows)
    keys: dict[Window, str] = {}
    for window in windows:
        start, end = window
        keys[window] = (
            f"telemetry-period-revision:v2:{start.astimezone(UTC).isoformat(timespec='microseconds')}:"
            f"{end.astimezone(UTC).isoformat(timespec='microseconds')}"
        )
    cached: dict[str, str] = {}
    for offset in range(0, len(windows), 128):
        selected = windows[offset:offset + 128]
        result = db.storage.execute(
            "DECLARE $keys AS List<Utf8>; SELECT key,value FROM app_meta WHERE key IN $keys;",
            {"$keys": ydb.TypedValue([keys[window] for window in selected],
                                    ydb.ListType(ydb.PrimitiveType.Utf8))},
        )[0].rows
        cached.update({row.key: row.value for row in result})
    revisions: dict[Window, str] = {}
    for window in windows:
        try:
            value = json.loads(cached.get(keys[window], "null"))
            if (isinstance(value, dict) and value.get("markers") == markers[window]
                    and isinstance(value.get("revision"), str)):
                revisions[window] = value["revision"]
        except (ValueError, TypeError):
            pass
    missing = [window for window in windows if window not in revisions]
    digests = [hashlib.sha256() for _ in missing]
    counts = [0 for _ in missing]
    ordered = sorted((utc_seconds(start), utc_seconds(end), index)
                     for index, (start, end) in enumerate(missing))
    # Merge overlapping ranges, so a single observation feeds every affected
    # fingerprint while unrelated gaps are never read.
    ranges: list[Window] = []
    for start, end in sorted(missing):
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    for series in db.list_series() if missing else []:
        active: dict[int, int] = {}
        endings: list[tuple[int, int]] = []
        cursor = 0
        for start, end in ranges:
            for row in db._samples(series["id"], start, end):
                timestamp = row["timestamp_utc"]
                while cursor < len(ordered) and ordered[cursor][0] <= timestamp:
                    _, ending, index = ordered[cursor]
                    active[index] = ending
                    heapq.heappush(endings, (ending, index))
                    cursor += 1
                while endings and endings[0][0] <= timestamp:
                    _, index = heapq.heappop(endings)
                    active.pop(index, None)
                if not active:
                    continue
                encoded = json.dumps(
                    [series["id"], timestamp, row["value_num"], row["value_text"], row["quality"]],
                    ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                ).encode() + b"\n"
                for index in active:
                    digests[index].update(encoded)
                    counts[index] += 1
    empty = hashlib.sha256(b"[]").hexdigest()
    for index, window in enumerate(missing):
        revisions[window] = f"telemetry-v2:{digests[index].hexdigest()}" if counts[index] else empty

    def check(tx: Transaction) -> None:
        if _telemetry_revision(tx) != source_revision:
            raise ValueError("inputs changed during telemetry fingerprint calculation")

    row_type = ydb.StructType().add_member("key", ydb.PrimitiveType.Utf8).add_member(
        "value", ydb.PrimitiveType.Utf8,
    )
    for offset in range(0, len(missing), 128):
        batch: list[dict[str, Any]] = [
            {"key": keys[window], "value": encode({"markers": markers[window], "revision": revisions[window]})}
            for window in missing[offset:offset + 128]
        ]

        def save(tx: Transaction, batch: list[dict[str, Any]] = batch) -> None:
            check(tx)
            tx.execute(
                "DECLARE $rows AS List<Struct<key:Utf8,value:Utf8>>; "
                "UPSERT INTO app_meta SELECT * FROM AS_TABLE($rows);",
                {"$rows": ydb.TypedValue(batch, ydb.ListType(row_type))},
            )
        db.storage.transaction(save)
    if _telemetry_revision(db.storage) != source_revision:
        raise ValueError("inputs changed during telemetry fingerprint calculation")
    return revisions

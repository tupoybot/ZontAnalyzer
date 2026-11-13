"""Read-only bounded YDB inventory; output contains aggregates, never source payloads.

Run with ``python -m tools.ydb_storage_audit`` and private YDB environment settings.
The tool does not initialize tables, write metadata, or scan telemetry rows.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import ALL_TABLES as TABLES
from zont_analyzer.adapters.ydb.telemetry import encode
from zont_analyzer.adapters.zont_readonly.configuration import configuration_snapshot

# Only known application namespaces may become report labels. Arbitrary key
# fragments can contain account identifiers or other private information.
SAFE_PREFIXES = (
    "telemetry-day:", "telemetry-period-revision:", "telemetry-bounds:",
    "chart-data:", "gas-report-revision:", "gas-statistic:", "gas-model-pointer:", "gas-model-active:",
    "gas-model:", "gas-exposure:", "latest-report:", "intervention-prediction:",
    "source-event-report-baseline:", "source-event-report-baselines:",
    "report-marker-upgrade:",
)
PAGE_SIZE = 256


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _pages(db: Any, table: str, budget: list[int]) -> tuple[list[Any], bool, str | None]:
    """Keyset pagination uses the primary key and a shared total row budget.

    Reaching the exact cap is incomplete too: completeness cannot be inferred
    without reading another row. No unbounded COUNT/SUM or offset is used.
    """
    result: list[Any] = []
    cursor: tuple[str, ...] | None = None
    keys = ("key",) if table == "app_meta" else ("device_id", "content_hash")
    columns = "key,value" if table == "app_meta" else "device_id,content_hash,id,payload,captured_at"
    while budget[0] > 0:
        limit = min(PAGE_SIZE, budget[0])
        declarations = "DECLARE $limit AS Uint64; "
        parameters: dict[str, Any] = {"$limit": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64)}
        where = ""
        if cursor is not None:
            for index, value in enumerate(cursor):
                declarations += f"DECLARE $after{index} AS Utf8; "
                parameters[f"$after{index}"] = value
            if len(keys) == 1:
                where = " WHERE key > $after0"
            else:
                where = (" WHERE device_id > $after0 OR "
                         "(device_id = $after0 AND content_hash > $after1)")
        try:
            parts = db.execute(
                declarations + f"SELECT {columns} FROM {table}{where} "
                f"ORDER BY {','.join(keys)} LIMIT $limit;",
                parameters, timeout_seconds=20,
            )
            page = [row for part in parts for row in part.rows]
            if len(page) > limit:
                return result, False, "page_limit_exceeded"
            budget[0] -= len(page)
            result.extend(page)
            if len(page) < limit:
                return result, True, None
            next_cursor = tuple(_text(getattr(page[-1], key)) for key in keys)
            if cursor is not None and next_cursor <= cursor:
                return result, False, "non_advancing_cursor"
            cursor = next_cursor
        except Exception as exc:
            # Exception messages can contain query parameters and private paths.
            return result, False, type(exc).__name__
    return result, False, "row_cap_reached"


def _inventory(db: Any) -> list[dict[str, Any]]:
    rows = []
    for table in TABLES:
        item: dict[str, Any] = {
            "table": table, "physical_rows": None, "physical_bytes": None,
            "statistics_timestamp": None, "error": None,
        }
        try:
            description = db.driver.table_client.describe_table(
                db.path + "/" + table,
                settings=ydb.DescribeTableSettings().with_include_table_stats(True).with_timeout(10),
            )
            stats = getattr(description, "table_stats", None)
            if stats is None:
                item["error"] = "table_statistics_unavailable"
            else:
                item["physical_rows"] = getattr(stats, "rows_estimate", None)
                item["physical_bytes"] = getattr(stats, "store_size", None)
                stamp = getattr(stats, "modification_time", None)
                item["statistics_timestamp"] = (
                    stamp.isoformat() if isinstance(stamp, datetime) and stamp.timestamp() > 0 else None
                )
                if any(item[key] is None for key in ("physical_rows", "physical_bytes", "statistics_timestamp")):
                    item["error"] = "table_statistics_partially_unavailable"
        except Exception as exc:
            item["error"] = type(exc).__name__
        rows.append(item)
    return rows


def _metadata(rows: list[Any], complete: bool, error: str | None) -> dict[str, Any]:
    groups: dict[str, dict[str, int]] = {}
    for row in rows:
        key = _text(row.key)
        prefix = next((value for value in SAFE_PREFIXES if key.startswith(value)), "other")
        group = groups.setdefault(prefix, {"rows": 0, "key_utf8_bytes": 0, "value_utf8_bytes": 0})
        group["rows"] += 1
        group["key_utf8_bytes"] += len(key.encode("utf-8"))
        group["value_utf8_bytes"] += len(_text(row.value).encode("utf-8"))
    return {"complete": complete, "error": error, "observed_rows": len(rows), "prefixes": groups}


def _snapshots(rows: list[Any], complete: bool, error: str | None) -> dict[str, Any]:
    raw_bytes = sum(len(_text(row.payload).encode("utf-8")) for row in rows)
    output: dict[str, Any] = {
        "complete": complete, "error": error, "observed_rows": len(rows),
        "observed_raw_payload_bytes": raw_bytes, "projected_payload_bytes": None,
        "projected_event_count": None, "observed_span_days": None,
        "observed_raw_payload_bytes_per_day": None, "projected_payload_bytes_per_day": None,
    }
    if not complete:
        return output
    try:
        # PK order is hash order, not event order. Compare only adjacent events
        # of the same device; A -> B -> A must retain all three events.
        ordered = sorted(rows, key=lambda row: (_text(row.device_id), row.captured_at, row.id))
        previous: dict[str, str] = {}
        projected_bytes = 0
        events = 0
        times = []
        valid_times = True
        for row in ordered:
            device = json.loads(_text(row.payload))
            if not isinstance(device, dict):
                raise ValueError("snapshot_payload_not_object")
            payload = encode(configuration_snapshot(device))
            identifier = _text(row.device_id)
            if previous.get(identifier) != payload:
                events += 1
                projected_bytes += len(payload.encode("utf-8"))
                previous[identifier] = payload
            timestamp = row.captured_at
            if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp > 0:
                times.append(timestamp)
            else:
                valid_times = False
        output.update(projected_payload_bytes=projected_bytes, projected_event_count=events)
        if valid_times and len(times) > 1 and max(times) > min(times):
            days = (max(times) - min(times)) / 86_400_000_000
            output.update(observed_span_days=days,
                          observed_raw_payload_bytes_per_day=raw_bytes / days,
                          projected_payload_bytes_per_day=projected_bytes / days)
    except Exception as exc:
        output.update(complete=False, error=type(exc).__name__)
    return output


def inspect_storage(db: Any, max_rows: int = 10_000) -> dict[str, Any]:
    """Inspect schema statistics and at most max_rows application payload rows."""
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
        raise ValueError("max_rows must be a positive integer")
    budget = [max_rows]
    inventory = _inventory(db)
    metadata = _metadata(*_pages(db, "app_meta", budget))
    snapshots = _snapshots(*_pages(db, "config_snapshots", budget))
    return {
        "generated_at": datetime.now(UTC).isoformat(), "max_rows": max_rows,
        "complete": (metadata["complete"] and snapshots["complete"]
                     and all(item["error"] is None for item in inventory)),
        "tables": inventory, "app_meta": metadata, "config_snapshots": snapshots,
        "limitations": [
            "Table statistics are asynchronous estimates; their timestamp is the table modification time.",
            "Logical UTF-8 payload bytes exclude indexes, replicas, keys, compression and storage overhead.",
            "Daily rates describe the observed snapshot interval, not a physical storage forecast.",
            "Pages are not one database snapshot; concurrent writes can change the observed inventory.",
            "Existing hash-keyed history may already have lost recurring identical configurations.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-rows", type=int, default=10_000)
    args = parser.parse_args()
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        result = inspect_storage(db, max_rows=args.max_rows)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

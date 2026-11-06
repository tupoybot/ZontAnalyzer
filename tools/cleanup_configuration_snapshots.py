"""Byte-fenced configuration compaction from a private, immutable export plan.

Only config_snapshots is written. Plans contain private source payloads and must
remain outside the public repository. Callers own backup, export and acceptance.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import ydb  # type: ignore[import-untyped]

from tools.compact_configuration_export import compact
from zont_analyzer.adapters.ydb.database import Transaction, YdbDatabase
from zont_analyzer.adapters.ydb.telemetry import encode
from zont_analyzer.adapters.zont_readonly.configuration import configuration_snapshot

_FIELDS = ("device_id", "content_hash", "id", "payload", "captured_at")
_MAX_BYTES = 2 * 1024 * 1024
_KEYS = "DECLARE $keys AS List<Struct<device_id:Utf8,content_hash:Utf8>>; "


def _identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return row["device_id"], row["content_hash"]


def _same(left: Any, right: Any) -> bool:
    """Strict manifest comparison without serializing private payloads again."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def plan_configuration_cleanup(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Create a detached private plan preserving the exact compact() timeline."""
    import json

    source = []
    identities = set()
    for row in rows:
        if set(row) != set(_FIELDS):
            raise ValueError("source row fields differ from snapshot schema")
        item = {field: row[field] for field in _FIELDS}
        if not isinstance(item["content_hash"], str) or not isinstance(item["payload"], str):
            raise ValueError("source keys and payload must be strings")
        if len(item["payload"].encode("utf-8")) > _MAX_BYTES:
            raise ValueError("one source payload exceeds the transaction byte limit")
        identity = _identity(item)
        if identity in identities:
            raise ValueError("duplicate source primary key")
        identities.add(identity)
        source.append(item)
    targets, summary = compact(source)
    source.sort(key=lambda row: (row["device_id"], row["captured_at"], row["id"]))
    events: list[dict[str, Any]] = []
    index = 0
    for row in source:
        projected = encode(configuration_snapshot(json.loads(row["payload"])))
        if (not events or events[-1]["target"]["device_id"] != row["device_id"]
                or events[-1]["target"]["payload"] != projected):
            events.append({"target": dict(targets[index]), "sources": []})
            index += 1
        events[-1]["sources"].append(dict(row))
    return {"version": 1, "source_rows": source, "events": events, "summary": summary}


def _typed_keys(rows: Iterable[Mapping[str, Any]]) -> Any:
    typ = ydb.StructType().add_member("device_id", ydb.PrimitiveType.Utf8)
    typ.add_member("content_hash", ydb.PrimitiveType.Utf8)
    return ydb.TypedValue(
        [{"device_id": row["device_id"], "content_hash": row["content_hash"]} for row in rows],
        ydb.ListType(typ),
    )


def _chunks(rows: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        added = len(row["payload"].encode("utf-8"))
        if batch and (len(batch) == batch_size or size + added > _MAX_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += added
    if batch:
        yield batch


def _apply_batch(
    db: YdbDatabase, target: dict[str, Any], first: dict[str, Any], batch: list[dict[str, Any]],
) -> dict[str, int | bool]:
    def write(tx: Transaction) -> dict[str, int | bool]:
        # A missing target can only be created in the batch containing its
        # byte-identical first source observation. Later batches require it.
        requested = {_identity(row): row for row in [*batch, target]}
        result = tx.execute(
            _KEYS + "SELECT s.* "
            "FROM AS_TABLE($keys) AS k INNER JOIN config_snapshots AS s "
            "ON s.device_id=k.device_id AND s.content_hash=k.content_hash;",
            {"$keys": _typed_keys(requested.values())},
        )
        current = {_identity(row): {field: row[field] for field in _FIELDS}
                   for part in result for row in part.rows}
        target_key = _identity(target)
        existing = current.get(target_key)
        conflict: dict[str, int | bool] = {"conflicted": True, "deleted_rows": 0, "upserted_rows": 0}
        if existing is not None and existing != target:
            return conflict
        complete = existing == target
        if not complete and current.get(_identity(first)) != first:
            return conflict
        present = []
        for row in batch:
            actual = current.get(_identity(row))
            if actual is not None:
                if actual != row:
                    return conflict
                if _identity(row) != target_key:
                    present.append(row)
            elif not complete:
                return conflict
        if not complete:
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $hash AS Utf8; DECLARE $id AS Int64; "
                "DECLARE $payload AS Utf8; DECLARE $at AS Int64; "
                "UPSERT INTO config_snapshots (device_id,content_hash,id,payload,captured_at) "
                "VALUES ($device,$hash,$id,$payload,$at);",
                {"$device": target["device_id"], "$hash": target["content_hash"], "$id": target["id"],
                 "$payload": target["payload"], "$at": target["captured_at"]},
            )
        if present:
            tx.execute(_KEYS + "DELETE FROM config_snapshots ON SELECT * FROM AS_TABLE($keys);",
                       {"$keys": _typed_keys(present)})
        return {"conflicted": False, "deleted_rows": len(present), "upserted_rows": int(not complete)}

    return db.transaction(write)


def apply_configuration_cleanup(
    db: YdbDatabase, plan: Mapping[str, Any], batch_size: int = 50,
) -> dict[str, Any]:
    """Apply validated bounded transactions; stop on the first byte-fence conflict.

    A completed target permits replay after an interrupted run. No row outside
    the immutable manifest is selected for deletion, including newly appended
    settings. Callback results are counted only after a committed transaction.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    if not isinstance(plan, Mapping) or "source_rows" not in plan:
        raise ValueError("invalid configuration cleanup plan")
    rebuilt = plan_configuration_cleanup(plan["source_rows"])
    if not _same(rebuilt, dict(plan)):
        raise ValueError("configuration cleanup plan does not match its source manifest")
    summary: dict[str, Any] = {
        "status": "complete", "committed_batches": 0, "deleted_rows": 0, "upserted_rows": 0,
        "planned_source_rows": len(rebuilt["source_rows"]), "retained_events": len(rebuilt["events"]),
    }
    for event in rebuilt["events"]:
        for batch in _chunks(event["sources"], batch_size):
            result = _apply_batch(db, event["target"], event["sources"][0], batch)
            if result["conflicted"]:
                summary["status"] = "conflicted"
                return summary
            summary["committed_batches"] += 1
            summary["deleted_rows"] += result["deleted_rows"]
            summary["upserted_rows"] += result["upserted_rows"]
    return summary

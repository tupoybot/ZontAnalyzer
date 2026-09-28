"""Resume a verified private YDB snapshot into an empty initialized namespace.

The artifact and target must be private. This uploader does not assert equality;
run the independent transfer_ydb_snapshot verify command after it completes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import ydb  # type: ignore[import-untyped]

from tools import transfer_ydb_snapshot as snapshot
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES

STATE = snapshot.MARKER_PREFIX + "state"
TABLE_MARKER = snapshot.MARKER_PREFIX + "table:"
BATCH_ROWS = 500
BATCH_BYTES = 128 * 1024


def _json(value: Any) -> str:
    return snapshot._canonical(value).decode("utf-8").rstrip("\n")


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _artifact_preflight(directory: Path) -> tuple[dict[str, Any], str]:
    manifest = snapshot._artifact_manifest(directory)
    source = manifest.get("source_import_sha256")
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{64}", source):
        raise ValueError("invalid source backup hash")
    with (directory / "manifest.json").open("rb") as stream:
        artifact_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    metadata: dict[str, str] = {}
    for table in snapshot.TABLE_NAMES:
        entry = manifest["tables"][table]
        if type(entry["rows"]) is not int or entry["rows"] < 0 or type(entry["bytes"]) is not int or entry["bytes"] < 0:
            raise ValueError(f"invalid artifact counts: {table}")
        count = 0
        for row in snapshot._file_rows(directory, table):
            count += 1
            if table == "metadata":
                metadata[row["name"]] = row["value"]
                if row["name"].startswith(snapshot.MARKER_PREFIX):
                    raise ValueError("source artifact contains transfer markers")
        if count != entry["rows"]:
            raise ValueError(f"artifact row count mismatch: {table}")
    if metadata.get("schema_version") != "2" or metadata.get("schema_hash") != snapshot.SCHEMA_HASH:
        raise ValueError("source artifact schema metadata mismatch")
    if metadata.get("sqlite_import_sha256") != source:
        raise ValueError("source artifact backup hash mismatch")
    try:
        state = json.loads(metadata["sqlite_import_state"])
    except (KeyError, ValueError):
        raise ValueError("source artifact import state missing") from None
    if state != {"state": "complete", "source_sha256": source}:
        raise ValueError("source artifact import not complete")
    return manifest, artifact_hash


def _marker(db: YdbDatabase, name: str) -> str | None:
    rows = db.execute(
        "DECLARE $name AS Utf8; SELECT value FROM metadata WHERE name=$name;", {"$name": name},
    )[0].rows
    return _text(rows[0].value) if rows else None


def _write_marker(db: YdbDatabase, name: str, value: dict[str, Any]) -> None:
    encoded = _json(value)
    db.transaction(lambda tx: tx.execute(
        "DECLARE $name AS Utf8; DECLARE $value AS Utf8; "
        "UPSERT INTO metadata (name,value) VALUES ($name,$value);",
        {"$name": name, "$value": encoded},
    ))


def _load_progress(db: YdbDatabase, manifest: dict[str, Any], artifact_hash: str) -> dict[str, int]:
    source = manifest["source_import_sha256"]
    state_raw = _marker(db, STATE)
    if state_raw is None:
        # Schema initialization creates exactly these two metadata rows. Every
        # other row, including migration_records, indicates an occupied target.
        for table in TABLES:
            if table == "metadata":
                rows = db.execute("SELECT name FROM metadata LIMIT 3;")[0].rows
                if {str(row.name) for row in rows} != {"schema_version", "schema_hash"}:
                    raise ValueError("target namespace is not empty")
            elif db.execute(f"SELECT * FROM {table} LIMIT 1;")[0].rows:
                raise ValueError(f"target namespace is not empty: {table}")
        _write_marker(db, STATE, {"manifest_sha256": artifact_hash,
                                  "source_import_sha256": source, "status": "loading"})
        return {table: 0 for table in snapshot.TABLE_NAMES}
    try:
        state = json.loads(state_raw)
    except ValueError:
        raise ValueError("invalid target transfer state") from None
    if (not isinstance(state, dict) or set(state) != {"manifest_sha256", "source_import_sha256", "status"}
            or state["manifest_sha256"] != artifact_hash or state["source_import_sha256"] != source
            or state["status"] not in {"loading", "complete"}):
        raise ValueError("target belongs to another artifact or has invalid transfer state")
    progress: dict[str, int] = {}
    for table in snapshot.TABLE_NAMES:
        raw = _marker(db, TABLE_MARKER + table)
        if raw is None:
            progress[table] = 0
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            raise ValueError(f"invalid transfer progress: {table}") from None
        if (not isinstance(item, dict) or set(item) != {"manifest_sha256", "rows"}
                or item["manifest_sha256"] != artifact_hash or type(item["rows"]) is not int
                or not 0 <= item["rows"] <= manifest["tables"][table]["rows"]):
            raise ValueError(f"target transfer progress mismatch: {table}")
        progress[table] = item["rows"]
    if state["status"] == "complete" and any(
        progress[table] != manifest["tables"][table]["rows"] for table in snapshot.TABLE_NAMES
    ):
        raise ValueError("complete marker has incomplete table progress")
    return progress


def _types(table: str) -> Any:
    struct = ydb.StructType()
    for name, typ, required in snapshot._columns(table):
        primitive = getattr(ydb.PrimitiveType, typ)
        struct = struct.add_member(name, primitive if required else ydb.OptionalType(primitive))
    return struct


def _batch(db: YdbDatabase, table: str, rows: list[dict[str, Any]], done: int,
           artifact_hash: str, *, bulk: Callable[..., Any] | None = None) -> None:
    if not rows:
        return
    progress = {"manifest_sha256": artifact_hash, "rows": done}
    if snapshot._expected_indexes(table):
        columns = ",".join(f"{name}:{typ}{'' if required else '?'}"
                           for name, typ, required in snapshot._columns(table))
        statement = (f"DECLARE $rows AS List<Struct<{columns}>>; "
                     f"UPSERT INTO {table} SELECT * FROM AS_TABLE($rows);")
        def commit(tx: Any) -> None:
            tx.execute(statement, {"$rows": ydb.TypedValue(rows, ydb.ListType(_types(table)))})
            tx.execute(
                "DECLARE $name AS Utf8; DECLARE $value AS Utf8; "
                "UPSERT INTO metadata (name,value) VALUES ($name,$value);",
                {"$name": TABLE_MARKER + table, "$value": _json(progress)},
            )
        db.transaction(commit)
    else:
        writer = bulk or db.driver.table_client.bulk_upsert
        ydb.retry_operation_sync(
            lambda: writer(f"{db.path}/{table}", rows, _types(table)),
            ydb.RetrySettings(max_retries=3, idempotent=True),
        )
        # If the process dies between BulkUpsert and this marker, replaying the
        # same full-key rows is safe. No increment or external call is repeated.
        _write_marker(db, TABLE_MARKER + table, progress)


class _Pacer:
    def __init__(self, ru_per_second: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rate, self.clock, self.sleep = ru_per_second, clock, sleep
        self.next_at = clock()

    def wait(self, sizes: list[int], indexed: bool) -> None:
        # Charge a minimum for each row, rather than amortizing tiny rows into
        # one KiB. Include the progress-marker transaction for every batch.
        units = sum(max(1, math.ceil(size / 1024)) for size in sizes)
        estimated = units * (8 if indexed else 1) + 10
        now = self.clock()
        self.next_at = max(now, self.next_at) + estimated / self.rate
        self.sleep(max(0.0, self.next_at - now))


def upload_snapshot(db: YdbDatabase, directory: Path, *, ru_per_second: int = 100,
                    batch_rows: int = BATCH_ROWS, bulk: Callable[..., Any] | None = None,
                    pacer: _Pacer | None = None,
                    progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    if not 1 <= ru_per_second <= 1000 or not 1 <= batch_rows <= BATCH_ROWS:
        raise ValueError("invalid upload limit")
    manifest, artifact_hash = _artifact_preflight(directory)
    snapshot._check_schema(db)
    completed = _load_progress(db, manifest, artifact_hash)
    pace = pacer or _Pacer(ru_per_second)
    for table in snapshot.TABLE_NAMES:
        indexed = bool(snapshot._expected_indexes(table))
        done = completed[table]
        batch: list[dict[str, Any]] = []
        sizes: list[int] = []
        size = 0
        batches = 0
        for index, row in enumerate(snapshot._file_rows(directory, table)):
            if index < done:
                continue
            encoded_size = len(snapshot._canonical(row))
            if batch and (len(batch) >= batch_rows or size + encoded_size > BATCH_BYTES):
                pace.wait(sizes, indexed)
                _batch(db, table, batch, done + len(batch), artifact_hash, bulk=bulk)
                done += len(batch)
                batches += 1
                if progress is not None and batches % 100 == 0:
                    progress(table, done, manifest["tables"][table]["rows"])
                batch, sizes, size = [], [], 0
            batch.append(row)
            sizes.append(encoded_size)
            size += encoded_size
        if batch:
            pace.wait(sizes, indexed)
            _batch(db, table, batch, done + len(batch), artifact_hash, bulk=bulk)
            done += len(batch)
        if done != manifest["tables"][table]["rows"]:
            raise ValueError(f"artifact progress mismatch: {table}")
        if completed[table] == 0 and done == 0:
            _write_marker(db, TABLE_MARKER + table, {"manifest_sha256": artifact_hash, "rows": 0})
        completed[table] = done
        if progress is not None:
            progress(table, done, manifest["tables"][table]["rows"])
    if any(completed[table] != manifest["tables"][table]["rows"] for table in snapshot.TABLE_NAMES):
        raise RuntimeError("cannot complete partial upload")
    _write_marker(db, STATE, {"manifest_sha256": artifact_hash,
                              "source_import_sha256": manifest["source_import_sha256"], "status": "complete"})
    return {"status": "uploaded_unverified", "manifest_sha256": artifact_hash,
            "tables": {table: completed[table] for table in snapshot.TABLE_NAMES}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--target-namespace", required=True)
    parser.add_argument("--ru-per-second", type=int, default=100)
    args = parser.parse_args()
    config = YdbConfig.from_environment(namespace=args.target_namespace)
    if config.namespace != args.target_namespace:
        raise ValueError("YDB_NAMESPACE disagrees with --target-namespace")
    db = YdbDatabase(config)
    try:
        result = upload_snapshot(
            db, args.artifact, ru_per_second=args.ru_per_second,
            progress=lambda table, done, total: print(
                f"upload {table}: {done}/{total} rows", file=sys.stderr, flush=True,
            ),
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        db.close()


if __name__ == "__main__":
    main()

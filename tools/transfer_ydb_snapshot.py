"""Export and independently compare a verified YDB snapshot using ordered ReadTable.

The artifact contains private application data. Keep it outside the public checkout.
All operations stream one table and one row at a time. Writers must remain stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from tools.verify_sqlite_import import verify_backup
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES

EXCLUDED = frozenset({"migration_records"})
TABLE_NAMES = tuple(name for name in TABLES if name not in EXCLUDED)
SCHEMA_HASH = hashlib.sha256(json.dumps(TABLES, sort_keys=True).encode()).hexdigest()
MARKER_PREFIX = "__snapshot_transfer__"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _columns(table: str) -> tuple[tuple[str, str, bool], ...]:
    """Parse the intentionally small primitive-only application schema."""
    result = []
    definition = re.sub(r"INDEX\s+\w+\s+GLOBAL SYNC ON\s*\([^)]*\)", "", TABLES[table])
    definition = re.sub(r"PRIMARY KEY\s*\([^)]*\)", "", definition)
    for fragment in definition.split(","):
        match = re.fullmatch(r"\s*(\w+)\s+(Utf8|Int64|Double|Bool)(\s+NOT NULL)?\s*", fragment)
        if match:
            result.append((match[1], match[2], bool(match[3])))
        elif fragment.strip():
            raise ValueError(f"unsupported schema fragment in {table}")
    if not result:
        raise ValueError(f"no supported columns in {table}")
    return tuple(result)


def _primary_key(table: str) -> tuple[str, ...]:
    match = re.search(r"PRIMARY KEY\s*\(([^)]*)\)", TABLES[table])
    if match is None:
        raise ValueError(f"table {table} has no primary key")
    return tuple(part.strip() for part in match[1].split(","))


def _expected_indexes(table: str) -> dict[str, tuple[str, ...]]:
    return {name: tuple(part.strip() for part in columns.split(","))
            for name, columns in re.findall(r"INDEX\s+(\w+)\s+GLOBAL SYNC ON\s*\(([^)]*)\)",
                                            TABLES[table])}


def _check_schema(db: YdbDatabase) -> None:
    actual_names = {item.name for item in cast(Any, db.driver.scheme_client).list_directory(db.path).children}
    if actual_names != set(TABLES):
        raise ValueError("YDB table inventory differs from application schema")
    for table in TABLES:
        expected_columns = {name: (typ, required) for name, typ, required in _columns(table)}
        description = db.driver.table_client.describe_table(f"{db.path}/{table}")
        actual_columns = {col.name: (str(col.type).rstrip("?"), not str(col.type).endswith("?"))
                          for col in description.columns}
        if actual_columns != expected_columns or tuple(description.primary_key) != _primary_key(table):
            raise ValueError(f"YDB schema mismatch: {table}")
        # SDK TableSchemeEntry discards the index kind while decoding DescribeTable:
        # it retains only name, columns, and status. We can compare the actual
        # columns here; schema metadata cannot independently prove the index kind.
        actual_indexes = {index.name: tuple(index.index_columns) for index in description.indexes}
        if actual_indexes != _expected_indexes(table) or description.ttl_settings is not None:
            raise ValueError(f"YDB index or TTL mismatch: {table}")
    rows = db.execute("SELECT name,value FROM metadata WHERE name IN ('schema_version','schema_hash');")[0].rows
    values = {row.name: row.value for row in rows}
    if values != {"schema_version": "2", "schema_hash": SCHEMA_HASH}:
        raise ValueError("YDB schema metadata mismatch")


def _row_dict(row: Any, table: str) -> dict[str, Any]:
    result = {}
    for name, typ, required in _columns(table):
        value = row[name] if isinstance(row, Mapping) else getattr(row, name)
        if isinstance(value, bytes) and typ == "Utf8":
            value = value.decode("utf-8")
        if value is None:
            if required:
                raise ValueError(f"null required column in {table}")
        elif typ == "Utf8" and not isinstance(value, str):
            raise ValueError(f"invalid text column in {table}")
        elif typ == "Bool" and type(value) is not bool:
            raise ValueError(f"invalid bool column in {table}")
        elif typ == "Int64" and (type(value) is not int or not -(1 << 63) <= value < (1 << 63)):
            raise ValueError(f"invalid integer column in {table}")
        elif typ == "Double" and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f"invalid double column in {table}")
        result[name] = value
    return result


def _ordered_rows(db: YdbDatabase, table: str) -> Iterator[dict[str, Any]]:
    session = cast(Any, db.driver.table_client.session()).create()
    try:
        for part in session.read_table(f"{db.path}/{table}", ordered=True, use_snapshot=True):
            for row in part.rows:
                yield _row_dict(row, table)
    finally:
        session.delete()


def _key(row: Mapping[str, Any], table: str) -> tuple[Any, ...]:
    return tuple(row[name] for name in _primary_key(table))


def _artifact_manifest(directory: Path) -> dict[str, Any]:
    raw = (directory / "manifest.json").read_bytes()
    manifest = cast(dict[str, Any], json.loads(raw))
    if (manifest.get("format") != 1 or manifest.get("schema_sha256") != SCHEMA_HASH
            or set(manifest.get("tables", {})) != set(TABLE_NAMES)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_import_sha256", ""))
            or _canonical(manifest) != raw):
        raise ValueError("unsupported or incomplete artifact manifest")
    for table in TABLE_NAMES:
        entry = manifest["tables"][table]
        if (set(entry) != {"rows", "bytes", "sha256"}
                or type(entry["rows"]) is not int or entry["rows"] < 0
                or type(entry["bytes"]) is not int or entry["bytes"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("invalid artifact table entry")
        path = directory / f"{table}.jsonl"
        if path.stat().st_size != entry["bytes"] or _digest(path) != entry["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {table}")
    return manifest


def _file_rows(directory: Path, table: str) -> Iterator[dict[str, Any]]:
    last: tuple[Any, ...] | None = None
    with (directory / f"{table}.jsonl").open("rb") as source:
        for line in source:
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {name for name, _, _ in _columns(table)}:
                raise ValueError(f"invalid artifact row in {table}")
            row = _row_dict(row, table)
            if table == "metadata" and row["name"].startswith(MARKER_PREFIX):
                raise ValueError("transfer marker reserved in source metadata")
            if _canonical(row) != line:
                raise ValueError(f"noncanonical artifact row in {table}")
            key = _key(row, table)
            if last is not None and not last < key:
                raise ValueError(f"artifact order or duplicate key in {table}")
            last = key
            yield row


def export_snapshot(db: YdbDatabase, directory: Path, sqlite_backup: Path, *,
                    ai_ledger: Path | None = None, chart_cache: Path | None = None,
                    require_sidecars: bool = True,
                    require_complete_schema: bool = True,
                    expected_schema_revision: str | None = None) -> dict[str, Any]:
    """Fail closed if the local import does not match its closed SQLite backup."""
    if directory.exists():
        raise ValueError("artifact directory must not exist")
    if require_sidecars and (ai_ledger is None or chart_cache is None):
        raise ValueError("AI ledger and chart cache are required")
    proof = verify_backup(sqlite_backup, db, require_complete_schema=require_complete_schema,
                          expected_schema_revision=expected_schema_revision,
                          ai_ledger=ai_ledger, chart_cache=chart_cache)
    if not proof["ok"]:
        raise ValueError("local SQLite import verification failed")
    _check_schema(db)
    source_digest = _digest(sqlite_backup)
    directory.mkdir(mode=0o700, parents=True)
    tables = {}
    try:
        for table in TABLE_NAMES:
            path = directory / f"{table}.jsonl"
            digest = hashlib.sha256()
            count = size = 0
            previous: tuple[Any, ...] | None = None
            with path.open("xb") as output:
                os.chmod(path, 0o600)
                for row in _ordered_rows(db, table):
                    if table == "metadata" and row["name"].startswith(MARKER_PREFIX):
                        raise ValueError("transfer marker reserved in source metadata")
                    key = _key(row, table)
                    if previous is not None and not previous < key:
                        raise ValueError(f"unordered YDB ReadTable: {table}")
                    previous = key
                    chunk = _canonical(row)
                    output.write(chunk)
                    digest.update(chunk)
                    count += 1
                    size += len(chunk)
            tables[table] = {"rows": count, "bytes": size, "sha256": digest.hexdigest()}
        manifest = {"format": 1, "schema_sha256": SCHEMA_HASH,
                    "source_import_sha256": source_digest, "tables": tables}
        path = directory / "manifest.json"
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            output.write(_canonical(manifest))
        if _digest(sqlite_backup) != source_digest:
            raise ValueError("SQLite backup changed during export")
        compared = verify_snapshot(db, directory)
        if not compared["ok"]:
            raise ValueError("local YDB changed during export")
        return {"tables": {name: entry["rows"] for name, entry in tables.items()},
                "total_rows": sum(entry["rows"] for entry in tables.values()),
                "total_jsonl_bytes": sum(entry["bytes"] for entry in tables.values()),
                "source_import_sha256": source_digest}
    except BaseException:
        # A post-write proof can fail. Remove only our manifest so the artifact
        # remains unusable; the private data files stay available for diagnosis.
        (directory / "manifest.json").unlink(missing_ok=True)
        raise


def _expected_markers(directory: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    digest = _digest(directory / "manifest.json")
    expected = {MARKER_PREFIX + "state": {
        "manifest_sha256": digest,
        "source_import_sha256": manifest["source_import_sha256"],
        "status": "complete",
    }}
    for table in TABLE_NAMES:
        expected[MARKER_PREFIX + "table:" + table] = {
            "manifest_sha256": digest, "rows": manifest["tables"][table]["rows"],
        }
    return expected


def verify_snapshot(db: YdbDatabase, directory: Path, *,
                    require_upload_complete: bool = False) -> dict[str, Any]:
    """Read-only full comparison, including missing, changed, and cloud-only rows."""
    manifest = _artifact_manifest(directory)
    _check_schema(db)
    # The target deliberately omits the source's row-by-row migration journal.
    # A single indexed/limited read rejects accidental journal import cheaply.
    journal_empty = True
    if require_upload_complete:
        journal_empty = not bool(db.execute(
            "SELECT source_table FROM migration_records LIMIT 1;"
        )[0].rows)
    results: dict[str, dict[str, int]] = {}
    total_bad = 0
    markers: dict[str, Any] = {}
    for table in TABLE_NAMES:
        expected = _file_rows(directory, table)
        actual = _ordered_rows(db, table)
        left = next(expected, None)
        right = next(actual, None)
        counts = {"expected_rows": 0, "actual_rows": 0, "missing": 0,
                  "extra": 0, "changed": 0}
        while left is not None or right is not None:
            if right is None or (left is not None and _key(left, table) < _key(right, table)):
                counts["expected_rows"] += 1
                counts["missing"] += 1
                left = next(expected, None)
            elif left is None or _key(right, table) < _key(left, table):
                counts["actual_rows"] += 1
                if table == "metadata" and str(right["name"]).startswith(MARKER_PREFIX):
                    try:
                        markers[right["name"]] = json.loads(right["value"])
                    except (TypeError, ValueError):
                        counts["extra"] += 1
                else:
                    counts["extra"] += 1
                right = next(actual, None)
            else:
                counts["expected_rows"] += 1
                counts["actual_rows"] += 1
                counts["changed"] += int(_canonical(left) != _canonical(right))
                left, right = next(expected, None), next(actual, None)
        if counts["expected_rows"] != manifest["tables"][table]["rows"]:
            raise ValueError(f"artifact count mismatch: {table}")
        total_bad += counts["missing"] + counts["extra"] + counts["changed"]
        results[table] = counts
    expected_markers = _expected_markers(directory, manifest)
    markers_ok = (markers == expected_markers if markers or require_upload_complete else True)
    total_bad += int(not markers_ok) + int(not journal_empty)
    return {"ok": total_bad == 0, "mismatches": total_bad, "tables": results,
            "upload_markers_complete": markers == expected_markers,
            "migration_records_empty": journal_empty}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("artifact", type=Path)
    export.add_argument("--sqlite-backup", type=Path, required=True)
    export.add_argument("--ai-ledger", type=Path)
    export.add_argument("--chart-cache", type=Path)
    export.add_argument("--allow-absent-sidecars", action="store_true")
    export.add_argument("--expected-schema-revision")
    export.add_argument("--allow-minimal-schema", action="store_true")
    export.add_argument("--writers-stopped", action="store_true", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("artifact", type=Path)
    verify.add_argument("--allow-unuploaded-source", action="store_true")
    args = parser.parse_args()
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        if args.command == "export":
            result = export_snapshot(db, args.artifact, args.sqlite_backup,
                                     ai_ledger=args.ai_ledger, chart_cache=args.chart_cache,
                                     require_sidecars=not args.allow_absent_sidecars,
                                     require_complete_schema=not args.allow_minimal_schema,
                                     expected_schema_revision=args.expected_schema_revision)
        else:
            result = verify_snapshot(db, args.artifact,
                                     require_upload_complete=not args.allow_unuploaded_source)
        print(json.dumps(result, sort_keys=True))
        if result.get("ok") is False:
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

"""Read-only comparison of a closed SQLite backup with its YDB import.

Output contains table names and aggregate mismatch counts, never row payloads.
Run while target writers are stopped; a concurrent database is not a snapshot.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ydb  # type: ignore[import-untyped]

from tools.import_sqlite import (
    _chart_cache_entries,
    _checksum,
    _columns,
    _enrich,
    _file_sha256,
    _identity,
    _inventory,
    _json,
    _ledger_cache_matches,
    _ledger_entries,
    _ledger_targets,
    _manifest_row,
    _matches,
    _primary_key,
    _source_rows,
    _target,
    _target_key,
    _text,
)
from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase

# These tables contain owner decisions/history; a rehearsal must not silently
# merge independent cloud edits. Other runtime/ledger tables may legitimately
# contain cloud metadata, scheduling, publication, cache and budget state.
OWNER_TABLES = frozenset({
    "owner_profile_revisions", "gas_readings", "gas_meter_boundaries", "gas_reading_audit",
    "gas_tariffs", "gas_tariff_audit", "ai_settings_revisions", "recommendations",
    "interventions", "intervention_experiments", "model_review_state", "model_review_runs",
    "model_review_proposals", "gas_meter_boundary_audit", "recommendation_audit",
})

BATCHED_TABLES = frozenset({
    "app_meta", "devices", "entities", "config_snapshots", "telemetry_series", "telemetry_samples",
    "source_events", "ingestion_cursors", "data_gaps", "analysis_periods", "metric_values",
    "detected_events", "interventions", "intervention_experiments", "publication_changes",
})


def _bounded_pages(connection: sqlite3.Connection, table: str) -> Iterator[list[dict[str, Any]]]:
    """Limit expected manifest+target result payload to 1 MiB or 500 rows.

    A single larger row is checked alone. Unexpectedly enlarged target data
    remains subject to YDB's query result limit and fails verification closed.
    Reports and mappings needing surrogate IDs use the existing single-row path.
    """
    page: list[dict[str, Any]] = []
    size = 0
    cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
    for raw in cursor:
        row = dict(raw)
        _, target = _target(table, row)
        row_size = len(_json(row).encode()) + len(_json(target).encode()) + 512
        if page and (len(page) >= 500 or size + row_size > 1024 * 1024):
            yield page
            page, size = [], 0
        page.append(row)
        size += row_size
    if page:
        yield page


def _sidecar_stats() -> dict[str, int]:
    return {"source_rows": 0, "manifest_mismatches": 0, "target_mismatches": 0,
            "stale_manifest_rows": 0, "source_changed": 0}


def _sidecar_manifest_matches(tx: Transaction, source: str, key: str, checksum: str,
                              table: str, target: dict[str, Any], payload: str) -> bool:
    manifest = _manifest_row(tx, source, key)
    return bool(manifest is not None and _text(manifest.checksum) == checksum
                and _text(manifest.target_table) == table
                and _text(manifest.target_key) == _json(_target_key(table, target))
                and _text(manifest.payload) == payload)


def _stale_sidecar_rows(db: YdbDatabase, source: str, present: set[str]) -> int:
    after = ""
    stale = 0
    while True:
        rows = db.execute(
            "DECLARE $source AS Utf8; DECLARE $after AS Utf8; "
            "SELECT source_key FROM migration_records WHERE source_table=$source "
            "AND source_key>$after ORDER BY source_key LIMIT 100;",
            {"$source": source, "$after": after},
        )[0].rows
        if not rows:
            return stale
        for row in rows:
            after = _text(row.source_key)
            stale += int(after not in present)


def _verify_ledger(db: YdbDatabase, path: Path) -> dict[str, int]:
    digest, entries = _ledger_entries(path)
    stats = _sidecar_stats()
    stats["source_rows"] = len(entries)
    for key, entry in entries.items():
        call, cache = _ledger_targets(key, entry)

        def check(tx: Transaction, key: str = key, entry: dict[str, Any] = entry,
                  call: dict[str, Any] = call,
                  cache: dict[str, Any] | None = cache) -> tuple[bool, bool]:
            return (_sidecar_manifest_matches(tx, "ai_ledger", _json([key]), _checksum(entry),
                                              "llm_calls", call, _json(entry)),
                    _matches(tx, "llm_calls", call) and _ledger_cache_matches(tx, key, cache))

        manifest_ok, target_ok = db.transaction(check)
        stats["manifest_mismatches"] += int(not manifest_ok)
        stats["target_mismatches"] += int(not target_ok)
    stats["stale_manifest_rows"] = _stale_sidecar_rows(db, "ai_ledger", {_json([key]) for key in entries})
    stats["source_changed"] = int(_file_sha256(path) != digest)
    return stats


def _verify_charts(connection: sqlite3.Connection, db: YdbDatabase, path: Path) -> dict[str, int]:
    entries = _chart_cache_entries(connection, path)
    stats = _sidecar_stats()
    stats["source_rows"] = len(entries)
    for member, digest, key, value in entries:
        target = {"key": key, "value": value}

        def check(tx: Transaction, source_key: str = _json([member.name]), digest: str = digest,
                  target: dict[str, Any] = target, value: str = value) -> tuple[bool, bool]:
            return (_sidecar_manifest_matches(tx, "chart_cache", source_key, digest, "app_meta", target, value),
                    _matches(tx, "app_meta", target))

        manifest_ok, target_ok = db.transaction(check)
        stats["manifest_mismatches"] += int(not manifest_ok)
        stats["target_mismatches"] += int(not target_ok)
    stats["stale_manifest_rows"] = _stale_sidecar_rows(
        db, "chart_cache", {_json([member.name]) for member, *_ in entries},
    )
    stats["source_changed"] = int(
        {member.name for member in path.iterdir()} != {member.name for member, *_ in entries}
        or any(not member.is_file() or _file_sha256(member) != digest for member, digest, *_ in entries)
    )
    return stats


def _verify_mapped_page(db: YdbDatabase, source: str, page: list[dict[str, Any]],
                        pk: tuple[str, ...]) -> tuple[int, int]:
    """Compare a bounded page with two indexed reads in one transaction."""
    prepared = []
    for row in page:
        table, target = _target(source, row)
        prepared.append((_identity(row, pk), _checksum(row), _json(row),
                         _json(_target_key(table, target)), target))
    target_columns = _columns(table)
    target_pk = tuple(_target_key(table, prepared[0][4]))
    lookup_type = ydb.StructType()
    for name in target_pk:
        lookup_type.add_member(name, getattr(ydb.PrimitiveType, target_columns[name]))
    lookup = [_target_key(table, target) for _, _, _, _, target in prepared]
    parameters = {"$keys": ydb.TypedValue(lookup, ydb.ListType(lookup_type))}
    fields = ",".join(f"{name}:{target_columns[name]}" for name in target_pk)
    declaration = f"DECLARE $keys AS List<Struct<{fields}>>; "
    joins = " AND ".join(f"t.{name}=k.{name}" for name in target_pk)
    manifest_type = (ydb.StructType().add_member("source_table", ydb.PrimitiveType.Utf8)
                     .add_member("source_key", ydb.PrimitiveType.Utf8))
    manifest_keys = [{"source_table": source, "source_key": item[0]} for item in prepared]

    def check(tx: Transaction) -> tuple[int, int]:
        manifest_rows = tx.execute(
            "DECLARE $keys AS List<Struct<source_table:Utf8,source_key:Utf8>>; "
            "SELECT m.source_key AS source_key,m.checksum AS checksum,"
            "m.payload AS payload,m.target_key AS target_key,m.target_table AS target_table "
            "FROM AS_TABLE($keys) AS k INNER JOIN migration_records AS m "
            "ON m.source_key=k.source_key AND m.source_table=k.source_table;",
            {"$keys": ydb.TypedValue(manifest_keys, ydb.ListType(manifest_type))},
        )[0].rows
        target_rows = tx.execute(
            declaration + f"SELECT t.* FROM AS_TABLE($keys) AS k INNER JOIN `{table}` AS t "
            f"ON {joins};", parameters,
        )[0].rows
        manifests = {_text(row.source_key): row for row in manifest_rows}
        targets = {_json({name: _text(row[name]) if isinstance(row[name], bytes) else row[name]
                          for name in target_pk}): row for row in target_rows}
        manifest_mismatches = target_mismatches = 0
        for key, checksum, raw, target_key, target in prepared:
            manifest = manifests.get(key)
            manifest_mismatches += int(manifest is None or not (
                _text(manifest.checksum) == checksum and _text(manifest.payload) == raw
                and _text(manifest.target_key) == target_key
                and _text(manifest.target_table) == table
            ))
            found = targets.get(target_key)
            target_mismatches += int(found is None or not all(
                (_text(found[name]) if isinstance(found[name], bytes) else found[name]) == value
                for name, value in target.items()
            ))
        return manifest_mismatches, target_mismatches

    return db.transaction(check)


def verify_backup(path: Path, db: YdbDatabase, *, require_complete_schema: bool = False,
                  expected_schema_revision: str | None = None, ai_ledger: Path | None = None,
                  chart_cache: Path | None = None) -> dict[str, Any]:
    source = path.resolve(strict=True)
    digest = _file_sha256(source)
    connection = sqlite3.connect(f"file:{quote(str(source))}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite backup failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite backup failed foreign_key_check")
        tables = _inventory(connection, require_complete_schema=require_complete_schema,
                            expected_schema_revision=expected_schema_revision)
        counts: dict[str, dict[str, int]] = {}
        for table in tables:
            stats = {"source_rows": 0, "manifest_mismatches": 0, "target_mismatches": 0,
                     "stale_manifest_rows": 0}
            counts[table] = stats
            pk = _primary_key(connection, table)
            pages = (_bounded_pages(connection, table) if table in BATCHED_TABLES
                     else _source_rows(connection, table, batch_size=1))
            for page in pages:
                if table in BATCHED_TABLES:
                    manifest_bad, target_bad = _verify_mapped_page(db, table, page, pk)
                    stats["source_rows"] += len(page)
                    stats["manifest_mismatches"] += manifest_bad
                    stats["target_mismatches"] += target_bad
                    continue
                for row in page:
                    stats["source_rows"] += 1
                    key = _identity(row, pk)
                    enriched = _enrich(connection, table, row, tables)

                    def check(tx: Transaction, table: str = table, key: str = key,
                              enriched: dict[str, Any] = enriched,
                              row: dict[str, Any] = row) -> tuple[bool, bool]:
                        manifest = _manifest_row(tx, table, key)
                        if manifest is None:
                            return False, False
                        target_key = json.loads(_text(manifest.target_key))
                        ordinal = (int(target_key["revision"]) if table == "owner_profile_revisions"
                                   else int(target_key["id"]) if table in {
                                       "gas_reading_audit", "gas_tariff_audit"} else 0)
                        target_table, target = _target(table, enriched, ordinal)
                        valid = (_text(manifest.checksum) == _checksum(enriched)
                                 and _text(manifest.payload) == _json(row)
                                 and _text(manifest.target_table) == target_table
                                 and target_key == _target_key(target_table, target))
                        return valid, _matches(tx, target_table, target)

                    manifest_ok, target_ok = db.transaction(check)
                    stats["manifest_mismatches"] += int(not manifest_ok)
                    stats["target_mismatches"] += int(not target_ok)
            after = ""
            while True:
                manifests = db.execute(
                    "DECLARE $table AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
                    "SELECT source_key FROM migration_records WHERE source_table=$table "
                    "AND source_key>$after ORDER BY source_key LIMIT $limit;",
                    {"$table": table, "$after": after,
                     "$limit": ydb.TypedValue(500, ydb.PrimitiveType.Uint64)},
                )[0].rows
                if not manifests:
                    break
                for manifest in manifests:
                    after = _text(manifest.source_key)
                    predicate = " AND ".join(f'"{name}"=?' for name in pk)
                    if connection.execute(f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1',
                                          json.loads(after)).fetchone() is None:
                        stats["stale_manifest_rows"] += 1
        sidecars = {}
        if ai_ledger is not None:
            sidecars["ai_ledger"] = _verify_ledger(db, ai_ledger)
        if chart_cache is not None:
            if "reports" not in tables:
                raise ValueError("chart cache requires source reports")
            sidecars["chart_cache"] = _verify_charts(connection, db, chart_cache)
        extra_owner_rows = {}
        for table in sorted(OWNER_TABLES):
            actual = int(db.execute(f"SELECT COUNT(*) AS n FROM `{table}`;")[0].rows[0].n)
            expected = counts.get(table, {}).get("source_rows", 0)
            extra_owner_rows[table] = max(0, actual - expected)
        metadata = db.execute("SELECT value FROM metadata WHERE name='sqlite_import_state';")[0].rows
        state = json.loads(_text(metadata[0].value)) if metadata else {}
        metadata_ok = state == {"state": "complete", "source_sha256": digest}
        unchanged = _file_sha256(source) == digest
        mismatches = sum(value for stats in counts.values() for key, value in stats.items()
                         if key != "source_rows") + int(not metadata_ok) + int(not unchanged)
        mismatches += sum(extra_owner_rows.values())
        mismatches += sum(value for stats in sidecars.values() for key, value in stats.items()
                          if key != "source_rows")
        return {"ok": mismatches == 0, "mismatches": mismatches,
                "import_metadata_matches": metadata_ok, "source_unchanged": unchanged, "tables": counts,
                "extra_owner_rows": extra_owner_rows, "sidecars": sidecars}
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--require-complete-schema", action="store_true")
    parser.add_argument("--expected-schema-revision")
    parser.add_argument("--ai-ledger", type=Path)
    parser.add_argument("--chart-cache", type=Path)
    args = parser.parse_args()
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        result = verify_backup(args.backup, db, require_complete_schema=args.require_complete_schema,
                               expected_schema_revision=args.expected_schema_revision,
                               ai_ledger=args.ai_ledger, chart_cache=args.chart_cache)
        print(_json(result))
        raise SystemExit(0 if result["ok"] else 1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

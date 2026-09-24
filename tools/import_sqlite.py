"""One-way, resumable import of an application-created SQLite online backup.

This operator tool is intentionally outside the installed application package.
The input must be a closed backup file; no source database or WAL is copied here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ydb  # type: ignore[import-untyped]

from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES
from zont_analyzer.adapters.ydb.telemetry import next_id
from zont_analyzer.domain import AnalysisResult, Report
from zont_analyzer.reports.chart_data import CHART_DATA_SCHEMA_VERSION, _cache_key

# Every durable table in the final SQLite migration is explicit. Unexpected
# tables abort before the first write, including tables created by later code.
SOURCE_TABLES = frozenset({
    "devices", "entities", "config_snapshots", "telemetry_series", "telemetry_samples",
    "ingestion_cursors", "data_gaps", "source_events", "analysis_periods", "metric_values",
    "detected_events", "reports", "recommendations", "interventions",
    "intervention_experiments", "jobs", "llm_calls", "notification_outbox", "app_meta",
    "owner_profile_revisions", "gas_readings", "gas_meter_boundaries", "gas_reading_audit",
    "gas_tariffs", "gas_tariff_audit", "ai_settings_revisions", "model_review_state",
    "model_review_runs", "model_review_proposals", "publication_changes", "alembic_version",
})

# Parent rows precede children. Deletions run in the reverse order.
TABLE_ORDER = (
    "devices", "entities", "config_snapshots", "telemetry_series", "telemetry_samples",
    "ingestion_cursors", "data_gaps", "source_events", "analysis_periods", "metric_values",
    "detected_events", "reports", "recommendations", "interventions",
    "intervention_experiments", "jobs", "llm_calls", "notification_outbox", "app_meta",
    "owner_profile_revisions", "gas_readings", "gas_meter_boundaries", "gas_reading_audit",
    "gas_tariffs", "gas_tariff_audit", "ai_settings_revisions", "model_review_state",
    "model_review_runs", "model_review_proposals", "publication_changes", "alembic_version",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _checksum(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _micros(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)  # SQLAlchemy stored UTC without the offset.
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _row_payload(row: Mapping[str, Any], *, timestamps: tuple[str, ...] = ()) -> dict[str, Any]:
    result = dict(row)
    for name in timestamps:
        if name in result:
            result[name] = _micros(result[name])
    return result


def _iso(value: Any) -> str:
    """Keep device discovery time in the same form as the SQLite facade."""
    moment = datetime.fromtimestamp(value / 1_000_000, UTC) if isinstance(value, int) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return (moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)).isoformat()


def _iso_or_none(value: Any) -> str | None:
    return _iso(value) if value is not None else None


def _columns(table: str) -> dict[str, str]:
    if table not in TABLES:
        raise ValueError(f"YDB table missing for SQLite source: {table}")
    result = {}
    for fragment in TABLES[table].split(","):
        parts = fragment.strip().split()
        if len(parts) >= 2 and parts[0] not in {"PRIMARY", "INDEX"}:
            result[parts[0]] = parts[1]
    return result


def _target(source: str, row: Mapping[str, Any], ordinal: int = 0) -> tuple[str, dict[str, Any]]:
    """Convert a legacy row into the fields read by the cloud repositories."""
    d = dict(row)
    payload = _row_payload(d, timestamps=tuple(k for k in d if k.endswith(("_at", "_from", "_until"))))
    if source == "alembic_version":
        return "metadata", {"name": "sqlite_schema_version", "value": d["version_num"]}
    if source == "app_meta":
        return "app_meta", {"key": d["key"], "value": d["value"]}
    if source == "devices":
        return source, {"id": d["id"], "payload": _json({
            "id": d["id"], "name": d["name"], "model": d["model"],
            "raw": json.loads(d["raw_json"]), "discovered_at": _iso(d["discovered_at"]),
        })}
    if source == "entities":
        return source, {"device_id": d["device_id"], "id": d["id"], "payload": _json(payload)}
    if source == "config_snapshots":
        return source, {"device_id": d["device_id"], "content_hash": d["content_hash"],
                        "id": d["id"], "payload": d["payload_json"], "captured_at": _micros(d["captured_at"])}
    if source == "telemetry_series":
        return source, {"device_id": d["device_id"], "source_type": d["source_type"],
                        "entity_id": d["entity_id"], "metric_key": d["metric_key"],
                        "id": d["id"], "payload": _json(payload)}
    if source == "telemetry_samples":
        return source, {"series_id": d["series_id"], "timestamp_utc": d["timestamp_utc"],
                        "value_num": d["value_num"], "value_text": d["value_text"],
                        "quality": d["quality"], "ingested_at": _micros(d["ingested_at"])}
    if source == "ingestion_cursors":
        return source, {"device_id": d["device_id"], "data_type": d["data_type"],
                        "timestamp_utc": d["timestamp_utc"]}
    if source == "source_events":
        event = {"id": d["id"], "device_id": d["device_id"], "event_type": d["event_type"],
                 "timestamp_utc": datetime.fromtimestamp(d["timestamp_utc"], UTC).isoformat(),
                 "duration_seconds": d["duration_seconds"], "details": json.loads(d["details_json"]),
                 "important": bool(d["important"])}
        return source, {"device_id": d["device_id"], "timestamp_utc": d["timestamp_utc"],
                        "id": d["id"], "payload": _json(event)}
    if source == "reports":
        report = json.loads(d["canonical_json"])
        rendered_text = d.get("rendered_text") or report["summary"]
        return source, {"kind": d["kind"], "period_start": d["period_start"],
                        "period_end": d["period_end"], "algorithm_version": d["algorithm_version"],
                        "id": d["id"], "payload": _json({"report": report, "rendered_text": rendered_text}),
                        "revision": 1}
    if source == "recommendations":
        return source, {"id": d["id"], "report_id": d["report_id"],
                        "payload": d["payload_json"], "status": d["status"],
                        "note": d["rejection_reason"], "experiment": None,
                        "created_at": _micros(d["created_at"]),
                        "updated_at": _micros(d["updated_at"])}
    if source == "interventions":
        return source, {"id": d["id"], "recommendation_id": d["recommendation_id"],
                        "applied_at": _micros(d["applied_at"]), "payload": _json(payload)}
    if source == "owner_profile_revisions":
        payload.update({"value": json.loads(d["value_json"]), "effective_at": _micros(d["effective_from"]),
                        "revision": ordinal})
        return source, {"device_id": d["device_id"], "field": d["field"],
                        "revision": ordinal, "effective_at": _micros(d["effective_from"]),
                        "payload": _json(payload)}
    if source in {"gas_readings", "gas_meter_boundaries"}:
        key = "reading_day" if source == "gas_readings" else "boundary_day"
        return source, {"device_id": d["device_id"], key: d[key], "payload": _json(payload)}
    if source in {"gas_tariffs"}:
        return source, {"scope": d["scope"], "effective_month": d["effective_month"],
                        "payload": _json(payload)}
    if source in {"gas_reading_audit", "gas_tariff_audit"}:
        if ordinal <= 0:
            raise ValueError("audit surrogate ID must be assigned inside a transaction")
        key = "reading_day" if source == "gas_reading_audit" else "effective_month"
        scope = "device_id" if source == "gas_reading_audit" else "scope"
        return source, {"id": ordinal, scope: d.get(scope), key: d.get(key),
                        "at": _micros(d["created_at"]), "payload": _json(payload)}
    if source == "ai_settings_revisions":
        return source, {"scope": "default", "version": d["id"],
                        "effective_at": _micros(d["created_at"]),
                        "payload": d["values_json"]}
    if source == "model_review_state":
        for name in ("last_success_at", "next_due_at", "last_attempt_at", "lease_until", "updated_at"):
            payload[name] = _iso_or_none(d[name])
        return source, {"scope": d["scope"], "version": d["attempts"], "payload": _json(payload)}
    if source == "model_review_runs":
        payload["started_at_us"] = _micros(d["started_at"])
        payload["started_at"] = _iso(d["started_at"])
        payload["finished_at"] = _iso_or_none(d["finished_at"])
        for name in ("settings", "sources", "catalog", "result"):
            payload[name] = json.loads(d[f"{name}_json"])
        return source, {"id": d["id"], "scope": d["scope"],
                        "started_at": _micros(d["started_at"]), "status": d["status"],
                        "payload": _json(payload)}
    if source == "model_review_proposals":
        payload["recommendation"] = json.loads(d["recommendation_json"])
        payload["created_at"] = _iso(d["created_at"])
        payload["decided_at"] = _iso_or_none(d["decided_at"])
        return source, {"id": d["id"], "payload": _json(payload)}
    if source == "publication_changes":
        return source, {"scope": d["scope"], "identifier": d["identifier"],
                        "revision": d["revision"], "payload": _json(payload)}
    if source == "notification_outbox":
        return source, {"id": d["id"], "report_id": d["report_id"],
                        "channel": d["channel"], "payload": d["payload"],
                        "state": d["status"], "attempts": d["attempts"]}
    if source == "jobs":
        return source, {"job_key": d["idempotency_key"], "owner": "legacy",
                        "attempt": 1, "lease_until": 0, "state": d["status"],
                        "checkpoint": _json(payload)}
    if source == "llm_calls":
        return source, {"call_key": d["id"], "job_key": d["report_id"],
                        "state": d["status"], "payload": _json(payload),
                        "created_at": _micros(d["created_at"]),
                        "updated_at": _micros(d["created_at"]),
                        "sent_at": _micros(d["created_at"])}
    if source in {"data_gaps", "analysis_periods", "metric_values", "detected_events",
                  "intervention_experiments"}:
        # These tables retain every source field and are directly addressable by
        # the corresponding YDB read adapters; the raw snapshot remains in the manifest.
        columns = _columns(source)
        target = {k: payload[k] for k in columns if k in payload}
        if "payload" in columns:
            target["payload"] = _json(payload)
        return source, target
    raise ValueError(f"unmapped SQLite source table: {source}")


def _primary_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    key = tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])
    if not key:
        raise ValueError(f"source table has no primary key: {table}")
    return key


def _identity(row: Mapping[str, Any], columns: tuple[str, ...]) -> str:
    return _json([row[name] for name in columns])


def _target_key(table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    definition = TABLES[table]
    match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", definition)
    if match is None:
        raise ValueError(f"YDB table has no primary key: {table}")
    key = match.group(1)
    return {name.strip(): row[name.strip()] for name in key.split(",")}


def _upsert(tx: Transaction, table: str, row: Mapping[str, Any]) -> None:
    allowed = _columns(table)
    unknown = set(row) - set(allowed)
    if unknown:
        raise ValueError(f"unsupported target columns for {table}: {sorted(unknown)}")
    for name in _target_key(table, row):
        if row[name] is None:
            raise ValueError(f"null target identity in {table}.{name}")
    columns = list(row)
    declarations = " ".join(f"DECLARE ${name} AS {allowed[name]}{'?' if row[name] is None else ''};"
                            for name in columns)
    values = ",".join("$" + name for name in columns)
    parameters = {}
    for name in columns:
        value = row[name]
        if value is None:
            primitive = getattr(ydb.PrimitiveType, allowed[name])
            value = ydb.TypedValue(None, ydb.OptionalType(primitive))
        parameters["$" + name] = value
    tx.execute(f"{declarations} UPSERT INTO `{table}` ({','.join(columns)}) VALUES ({values});",
               parameters)


def _delete(tx: Transaction, table: str, key: Mapping[str, Any]) -> None:
    columns = _columns(table)
    where = " AND ".join(f"{name}=${name}" for name in key)
    declarations = " ".join(f"DECLARE ${name} AS {columns[name]};" for name in key)
    tx.execute(f"{declarations} DELETE FROM `{table}` WHERE {where};",
               {"$" + name: value for name, value in key.items()})


def _matches(tx: Transaction, table: str, expected: Mapping[str, Any]) -> bool:
    key = _target_key(table, expected)
    column_types = _columns(table)
    declarations = " ".join(f"DECLARE ${name} AS {column_types[name]};" for name in key)
    where = " AND ".join(f"{name}=${name}" for name in key)
    fields = ",".join(expected)
    rows = tx.execute(
        f"{declarations} SELECT {fields} FROM `{table}` WHERE {where};",
        {"$" + name: value for name, value in key.items()},
    )[0].rows
    if len(rows) != 1:
        return False
    actual = rows[0]
    return all((_text(actual[name]) if isinstance(actual[name], bytes) else actual[name]) == value
               for name, value in expected.items())


def _manifest_row(tx: Transaction, table: str, key: str) -> Any | None:
    result = tx.execute(
        "DECLARE $table AS Utf8; DECLARE $key AS Utf8; "
        "SELECT checksum,target_table,target_key,payload FROM migration_records "
        "WHERE source_table=$table AND source_key=$key;",
        {"$table": table, "$key": key},
    )
    return result[0].rows[0] if result[0].rows else None


def _import_samples_page(db: YdbDatabase, page: list[dict[str, Any]],
                         pk: tuple[str, ...]) -> int:
    """Commit a bounded telemetry page and its resume manifest atomically."""
    prepared = []
    for row in page:
        target_table, target = _target("telemetry_samples", row)
        prepared.append((
            _identity(row, pk), _checksum(row), target,
            _json(_target_key(target_table, target)), _json(row),
        ))
    keys = [item[0] for item in prepared]
    key_type = ydb.ListType(ydb.PrimitiveType.Utf8)

    def write(tx: Transaction) -> int:
        old_rows = tx.execute(
            "DECLARE $keys AS List<Utf8>; SELECT source_key,checksum,target_key "
            "FROM migration_records WHERE source_table='telemetry_samples' AND source_key IN $keys;",
            {"$keys": ydb.TypedValue(keys, key_type)},
        )[0].rows
        old = {_text(item.source_key): item for item in old_rows}
        unchanged = [item for item in prepared if item[0] in old
                     and _text(old[item[0]].checksum) == item[1]
                     and _text(old[item[0]].target_key) == item[3]]
        existing: dict[tuple[int, int], Any] = {}
        if unchanged:
            lookup = [(item[2]["series_id"], item[2]["timestamp_utc"]) for item in unchanged]
            lookup_type = ydb.ListType(ydb.TupleType()
                                       .add_element(ydb.PrimitiveType.Int64)
                                       .add_element(ydb.PrimitiveType.Int64))
            target_rows = tx.execute(
                "DECLARE $keys AS List<Tuple<Int64,Int64>>; "
                "SELECT * FROM telemetry_samples WHERE (series_id,timestamp_utc) IN $keys;",
                {"$keys": ydb.TypedValue(lookup, lookup_type)},
            )[0].rows
            existing = {(int(item.series_id), int(item.timestamp_utc)): item
                        for item in target_rows}
        changed = []
        for item in prepared:
            key, checksum, target, target_key, raw = item
            found = existing.get((target["series_id"], target["timestamp_utc"]))
            if item in unchanged and found is not None and all(
                found[name] == value or (isinstance(found[name], bytes) and _text(found[name]) == value)
                for name, value in target.items()
            ):
                continue
            changed.append(item)
        if not changed:
            return 0
        row_type = (ydb.StructType().add_member("series_id", ydb.PrimitiveType.Int64)
                    .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
                    .add_member("value_num", ydb.OptionalType(ydb.PrimitiveType.Double))
                    .add_member("value_text", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                    .add_member("quality", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                    .add_member("ingested_at", ydb.OptionalType(ydb.PrimitiveType.Int64)))
        tx.execute("UPSERT INTO telemetry_samples SELECT * FROM AS_TABLE($rows);", {
            "$rows": ydb.TypedValue([item[2] for item in changed], ydb.ListType(row_type)),
        })
        manifest_type = (ydb.StructType().add_member("source_table", ydb.PrimitiveType.Utf8)
                         .add_member("source_key", ydb.PrimitiveType.Utf8)
                         .add_member("target_table", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                         .add_member("target_key", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                         .add_member("checksum", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                         .add_member("payload", ydb.OptionalType(ydb.PrimitiveType.Utf8)))
        manifest = [{"source_table": "telemetry_samples", "source_key": item[0],
                     "target_table": "telemetry_samples", "target_key": item[3],
                     "checksum": item[1], "payload": item[4]} for item in changed]
        tx.execute("UPSERT INTO migration_records SELECT * FROM AS_TABLE($rows);", {
            "$rows": ydb.TypedValue(manifest, ydb.ListType(manifest_type)),
        })
        return len(changed)

    return int(db.transaction(write))


def _legacy_usage(row: Mapping[str, Any] | None) -> tuple[str, int] | None:
    if row is None:
        return None
    count = int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    if count <= 0:
        return None
    timestamp = _micros(row["created_at"])
    if timestamp is None:
        raise ValueError("legacy LLM usage has no creation timestamp")
    month = datetime.fromtimestamp(timestamp // 1_000_000, UTC).strftime("%Y-%m")
    return month, count


def _adjust_legacy_budget(
    tx: Transaction, old: Mapping[str, Any] | None, new: Mapping[str, Any] | None,
) -> None:
    changes: dict[str, int] = {}
    for row, sign in ((old, -1), (new, 1)):
        usage = _legacy_usage(row)
        if usage:
            month, count = usage
            changes[month] = changes.get(month, 0) + sign * count
    for month, delta in changes.items():
        if not delta:
            continue
        rows = tx.execute(
            "DECLARE $month AS Utf8; SELECT reserved_tokens,charged_tokens FROM ai_budget_months "
            "WHERE month=$month;", {"$month": month},
        )[0].rows
        reserved = int(rows[0].reserved_tokens or 0) if rows else 0
        charged = int(rows[0].charged_tokens or 0) if rows else 0
        if charged + delta < 0:
            raise ValueError("legacy LLM charge exceeds recorded monthly budget")
        _upsert(tx, "ai_budget_months", {
            "month": month, "reserved_tokens": reserved, "charged_tokens": charged + delta,
        })


def _raise_sequence(db: YdbDatabase, name: str, minimum: int) -> None:
    if minimum <= 0:
        return

    def write(tx: Transaction) -> None:
        rows = tx.execute(
            "DECLARE $name AS Utf8; SELECT value FROM sequences WHERE name=$name;", {"$name": name}
        )[0].rows
        if rows and int(rows[0].value) >= minimum:
            return
        tx.execute(
            "DECLARE $name AS Utf8; DECLARE $value AS Int64; "
            "UPSERT INTO sequences (name,value) VALUES ($name,$value);",
            {"$name": name, "$value": minimum},
        )

    db.transaction(write)


def _source_rows(connection: sqlite3.Connection, table: str, *, batch_size: int) -> Iterator[list[dict[str, Any]]]:
    cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
    while page := cursor.fetchmany(batch_size):
        yield [dict(row) for row in page]


def _enrich(connection: sqlite3.Connection, table: str, row: dict[str, Any],
            tables: tuple[str, ...]) -> dict[str, Any]:
    if table == "reports" and "notification_outbox" in tables:
        rendered = connection.execute(
            "SELECT payload FROM notification_outbox WHERE report_id=? AND channel='log' "
            "ORDER BY id LIMIT 1", (row["id"],)
        ).fetchone()
        return {**row, "rendered_text": rendered["payload"] if rendered else None}
    if table == "gas_tariffs":
        audits = connection.execute(
            "SELECT * FROM gas_tariff_audit WHERE tariff_id=? ORDER BY created_at,id", (row["id"],)
        )
        return {**row, "corrections": [
            {
                "id": item["id"], "action": item["action"],
                "before": json.loads(item["before_json"]) if item["before_json"] else None,
                "after": json.loads(item["after_json"]), "reason": item["reason"],
                "created_at": _micros(item["created_at"]),
            }
            for item in audits
        ]}
    if table == "gas_tariff_audit":
        tariff = connection.execute(
            "SELECT scope,effective_month FROM gas_tariffs WHERE id=?", (row["tariff_id"],)
        ).fetchone()
        if tariff is None:
            raise ValueError("orphan gas tariff audit")
        return {**row, "scope": tariff["scope"], "effective_month": tariff["effective_month"]}
    return row


def _inventory(connection: sqlite3.Connection) -> tuple[str, ...]:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    unexpected = tables - SOURCE_TABLES
    if unexpected:
        raise ValueError(f"unmapped SQLite tables: {sorted(unexpected)}")
    missing_target = sorted((tables - {"alembic_version"}) - set(TABLES))
    if missing_target or "migration_records" not in TABLES:
        missing = missing_target + ([] if "migration_records" in TABLES else ["migration_records"])
        raise ValueError(f"YDB schema is missing importer tables: {missing}")
    return tuple(table for table in TABLE_ORDER if table in tables)


def _import_chart_cache(connection: sqlite3.Connection, db: YdbDatabase, directory: Path) -> int:
    """Validate old report-bound packets and move their data to YDB app_meta."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("chart cache bundle must be a directory, not a symlink")
    expected: dict[str, tuple[str, str, str]] = {}
    for page in _source_rows(connection, "reports", batch_size=100):
        for row in page:
            report = Report.model_validate_json(row["canonical_json"])
            key, digest = _cache_key(report)
            filename = hashlib.sha256(row["id"].encode("utf-8")).hexdigest() + ".json"
            expected[filename] = (hashlib.sha256(row["canonical_json"].encode("utf-8")).hexdigest(),
                                  key, digest)
    pending: list[tuple[Path, str, str, str]] = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or path.name not in expected:
            raise ValueError("unexpected chart cache bundle member")
        original_digest, target_key, target_digest = expected[path.name]
        raw = path.read_bytes()
        packet = json.loads(raw)
        if (not isinstance(packet, dict) or packet.get("schema_version") != CHART_DATA_SCHEMA_VERSION
                or not isinstance(packet.get("data"), dict) or packet.get("report_digest") != original_digest):
            raise ValueError(f"chart cache does not match accepted report: {path.name}")
        pending.append((path, hashlib.sha256(raw).hexdigest(), target_key,
                        _json({"schema_version": CHART_DATA_SCHEMA_VERSION,
                               "report_digest": target_digest, "data": packet["data"]})))
    changed = 0
    for path, digest, key, value in pending:
        if _file_sha256(path) != digest:
            raise ValueError("chart cache bundle changed during import")
        source_key = _json([path.name])

        def write(tx: Transaction, key: str = key, value: str = value,
                  source_key: str = source_key, digest: str = digest) -> bool:
            old = _manifest_row(tx, "chart_cache", source_key)
            rows = tx.execute(
                "DECLARE $key AS Utf8; SELECT value FROM app_meta WHERE key=$key;", {"$key": key}
            )[0].rows
            if rows:
                if _text(rows[0].value) != value:
                    raise ValueError("chart cache conflicts with an existing accepted packet")
                if old and old.checksum == digest and json.loads(_text(old.target_key)) == {"key": key}:
                    return False
            if old and json.loads(_text(old.target_key)) != {"key": key}:
                _delete(tx, "app_meta", json.loads(_text(old.target_key)))
            _upsert(tx, "app_meta", {"key": key, "value": value})
            _upsert(tx, "migration_records", {
                "source_table": "chart_cache", "source_key": source_key,
                "target_table": "app_meta", "target_key": _json({"key": key}),
                "checksum": digest, "payload": value,
            })
            return True

        changed += int(db.transaction(write))
    if any(_file_sha256(path) != digest for path, digest, *_ in pending):
        raise ValueError("chart cache bundle changed during import; deletion pass refused")
    present = {_json([path.name]) for path, *_ in pending}
    after = ""
    while True:
        rows = db.execute(
            "DECLARE $after AS Utf8; SELECT source_key,target_key FROM migration_records "
            "WHERE source_table='chart_cache' AND source_key>$after ORDER BY source_key LIMIT 100;",
            {"$after": after},
        )[0].rows
        if not rows:
            break
        for manifest in rows:
            after = _text(manifest.source_key)
            if after in present:
                continue

            def remove(tx: Transaction, manifest: Any = manifest, after: str = after) -> None:
                _delete(tx, "app_meta", json.loads(_text(manifest.target_key)))
                _delete(tx, "migration_records", {"source_table": "chart_cache", "source_key": after})

            db.transaction(remove)
            changed += 1
    return changed


def _ledger_amounts(entry: Mapping[str, Any] | None) -> tuple[str, int, int] | None:
    if entry is None:
        return None
    month = entry["billing_month"]
    reserved = entry["reserved_tokens"] if entry["status"] == "pending" else 0
    return month, reserved, entry.get("charged_tokens", 0)


def _adjust_ledger_budget(
    tx: Transaction, old: Mapping[str, Any] | None, new: Mapping[str, Any] | None,
) -> None:
    changes: dict[str, tuple[int, int]] = {}
    for entry, sign in ((old, -1), (new, 1)):
        amounts = _ledger_amounts(entry)
        if amounts is None:
            continue
        month, reserved, charged = amounts
        previous = changes.get(month, (0, 0))
        changes[month] = (previous[0] + sign * reserved, previous[1] + sign * charged)
    for month, (reserved_delta, charged_delta) in changes.items():
        if not reserved_delta and not charged_delta:
            continue
        rows = tx.execute(
            "DECLARE $month AS Utf8; SELECT reserved_tokens,charged_tokens FROM ai_budget_months "
            "WHERE month=$month;", {"$month": month},
        )[0].rows
        reserved = int(rows[0].reserved_tokens or 0) if rows else 0
        charged = int(rows[0].charged_tokens or 0) if rows else 0
        if reserved + reserved_delta < 0 or charged + charged_delta < 0:
            raise ValueError("AI ledger adjustment exceeds recorded monthly budget")
        _upsert(tx, "ai_budget_months", {
            "month": month, "reserved_tokens": reserved + reserved_delta,
            "charged_tokens": charged + charged_delta,
        })


def _valid_ledger_entry(key: str, entry: Any) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(entry, dict):
        raise ValueError("invalid AI ledger entry identity")
    if entry.get("status") not in {"pending", "success", "failure"}:
        raise ValueError("invalid AI ledger entry status")
    created = entry.get("created_at")
    if not isinstance(created, (int, float)) or isinstance(created, bool) or not math.isfinite(created) or created < 0:
        raise ValueError("invalid AI ledger creation time")
    month = entry.get("billing_month")
    if month is None:
        # Early ledgers predate explicit billing-month storage. Their creation
        # instant is the only retained month evidence.
        month = datetime.fromtimestamp(created, UTC).strftime("%Y-%m")
        entry = {**entry, "billing_month": month}
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("invalid AI ledger billing month")
    for name in ("input_tokens", "output_tokens", "reserved_tokens", "charged_tokens"):
        value = entry.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"invalid AI ledger {name}")
    if entry["status"] == "pending" and entry.get("reserved_tokens", 0) <= 0:
        raise ValueError("pending AI ledger entry has no reservation")
    if entry["status"] == "success" and not isinstance(entry.get("result"), dict):
        raise ValueError("successful AI ledger entry has no cached result")
    if entry["status"] == "success":
        AnalysisResult.model_validate(entry["result"])
    if entry.get("charged_tokens", 0) and (entry.get("input_tokens", 0) or entry.get("output_tokens", 0)):
        raise ValueError("AI ledger has both actual and reservation-based charges")
    return dict(entry)


def _import_ai_ledger(db: YdbDatabase, path: Path) -> int:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("AI ledger must be a closed regular file of at most 16 MiB")
    digest = _file_sha256(path)
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or set(state) != {"entries"} or not isinstance(state["entries"], dict):
        raise ValueError("invalid AI ledger structure")
    entries = {key: _valid_ledger_entry(key, entry) for key, entry in state["entries"].items()}
    changed = 0
    for key, entry in entries.items():
        source_key = _json([key])
        checksum = _checksum(entry)

        def put(
            tx: Transaction, key: str = key, entry: dict[str, Any] = entry,
            source_key: str = source_key, checksum: str = checksum,
        ) -> bool:
            old = _manifest_row(tx, "ai_ledger", source_key)
            previous = json.loads(_text(old.payload)) if old else None
            if old and old.checksum == checksum:
                return False
            created_at = int(entry["created_at"] * 1_000_000)
            state_name = "unknown" if entry["status"] == "pending" else entry["status"]
            _upsert(tx, "llm_calls", {
                "call_key": key, "job_key": "legacy-ai-ledger", "state": state_name,
                "payload": _json(entry), "created_at": created_at, "updated_at": created_at,
                "sent_at": None if entry["status"] == "pending" else created_at,
            })
            if entry["status"] == "success":
                result = entry["result"]
                provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
                _upsert(tx, "ai_response_cache", {
                    "fingerprint": key, "payload": _json(result), "provenance": _json(provenance),
                    "settings_version": str(provenance.get("settings_version") or "legacy-unrecorded"),
                    "model": str(provenance.get("requested_model") or "legacy-unrecorded"),
                    "created_at": created_at,
                })
            elif previous and previous["status"] == "success":
                _delete(tx, "ai_response_cache", {"fingerprint": key})
            _adjust_ledger_budget(tx, previous, entry)
            _upsert(tx, "migration_records", {
                "source_table": "ai_ledger", "source_key": source_key,
                "target_table": "llm_calls", "target_key": _json({"call_key": key}),
                "checksum": checksum, "payload": _json(entry),
            })
            return True

        changed += int(db.transaction(put))
    if _file_sha256(path) != digest:
        raise ValueError("AI ledger changed during import; deletion pass refused")
    after = ""
    while True:
        rows = db.execute(
            "DECLARE $after AS Utf8; SELECT source_key,payload FROM migration_records "
            "WHERE source_table='ai_ledger' AND source_key>$after ORDER BY source_key LIMIT 100;",
            {"$after": after},
        )[0].rows
        if not rows:
            break
        for manifest in rows:
            after = _text(manifest.source_key)
            key = json.loads(after)[0]
            if key in entries:
                continue

            def remove(
                tx: Transaction, manifest: Any = manifest, key: str = key, after: str = after,
            ) -> None:
                previous = json.loads(_text(manifest.payload))
                _delete(tx, "llm_calls", {"call_key": key})
                if previous["status"] == "success":
                    _delete(tx, "ai_response_cache", {"fingerprint": key})
                _adjust_ledger_budget(tx, previous, None)
                _delete(tx, "migration_records", {"source_table": "ai_ledger", "source_key": after})

            db.transaction(remove)
            changed += 1
    return changed


def import_backup(path: Path, db: YdbDatabase, *, batch_size: int = 100,
                  pause_seconds: float = 0.0, chart_cache: Path | None = None,
                  ai_ledger: Path | None = None) -> dict[str, int]:
    """Import a closed SQLite online backup; repeat safely for changed backups.

    A committed row and its manifest checksum share one YDB transaction. A
    restarted scan begins at the first source row; unchanged rows are skipped.
    Memory use is bounded by ``batch_size``. The second pass removes source
    deletions after a full successful scan, never on a partial run.
    """
    if not 1 <= batch_size <= 500 or not 0 <= pause_seconds <= 60:
        raise ValueError("invalid batch size or pause")
    source = path.resolve(strict=True)
    if not source.is_file():
        raise ValueError("source must be a regular online backup file")
    source_digest = _file_sha256(source)
    uri = f"file:{quote(str(source))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite backup failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("SQLite backup failed foreign_key_check")
        tables = _inventory(connection)
        db.transaction(lambda tx: _upsert(tx, "metadata", {
            "name": "sqlite_import_state",
            "value": _json({"state": "running", "source_sha256": source_digest}),
        }))
        counts: dict[str, int] = {}
        for table in tables:
            pk = _primary_key(connection, table)
            count = 0
            for page in _source_rows(connection, table, batch_size=batch_size):
                if table == "telemetry_samples":
                    count += _import_samples_page(db, page, pk)
                    if pause_seconds:
                        time.sleep(pause_seconds)
                    continue
                for row in page:
                    source_key = _identity(row, pk)
                    enriched = _enrich(connection, table, row, tables)
                    checksum = _checksum(enriched)
                    raw = _json(row)

                    def put(
                        tx: Transaction, table: str = table, source_key: str = source_key,
                        row: dict[str, Any] = row, enriched: dict[str, Any] = enriched,
                        checksum: str = checksum, raw: str = raw,
                    ) -> bool:
                        old = _manifest_row(tx, table, source_key)
                        audit = table in {"gas_reading_audit", "gas_tariff_audit"}
                        if audit:
                            surrogate = (int(json.loads(_text(old.target_key))["id"]) if old
                                         else next_id(tx, table))
                        elif table == "owner_profile_revisions":
                            if old:
                                surrogate = int(json.loads(_text(old.target_key))["revision"])
                            else:
                                rows = tx.execute(
                                    "DECLARE $device AS Utf8; DECLARE $field AS Utf8; "
                                    "SELECT MAX(revision) AS latest FROM owner_profile_revisions "
                                    "WHERE device_id=$device AND field=$field;",
                                    {"$device": enriched["device_id"], "$field": enriched["field"]},
                                )[0].rows
                                surrogate = int(rows[0].latest or 0) + 1
                        else:
                            surrogate = 0
                        target_table, target_row = _target(table, enriched, surrogate)
                        target_key = _json(_target_key(target_table, target_row))
                        if (old and old.checksum == checksum and old.target_table == target_table
                                and old.target_key == target_key and _matches(tx, target_table, target_row)):
                            return False
                        if old and (old.target_table != target_table or old.target_key != target_key):
                            _delete(tx, old.target_table, json.loads(old.target_key))
                        _upsert(tx, target_table, target_row)
                        if table == "llm_calls":
                            previous = json.loads(_text(old.payload)) if old else None
                            _adjust_legacy_budget(tx, previous, row)
                        _upsert(tx, "migration_records", {
                            "source_table": table, "source_key": source_key, "target_table": target_table,
                            "target_key": target_key, "checksum": checksum, "payload": raw,
                        })
                        return True

                    count += int(db.transaction(put))
                if pause_seconds:
                    time.sleep(pause_seconds)
            counts[table] = count
            if table in {"config_snapshots", "telemetry_series", "data_gaps"}:
                maximum = connection.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()[0]
                _raise_sequence(db, table, int(maximum or 0))
            if table == "publication_changes":
                maximum = connection.execute("SELECT MAX(revision) FROM publication_changes").fetchone()[0]
                if maximum:
                    def raise_revision(tx: Transaction, maximum: int = int(maximum)) -> None:
                        rows = tx.execute(
                            "SELECT revision FROM revisions WHERE scope='publication';"
                        )[0].rows
                        if not rows or int(rows[0].revision) < int(maximum):
                            _upsert(tx, "revisions", {"scope": "publication", "revision": int(maximum)})

                    db.transaction(raise_revision)
        if _file_sha256(source) != source_digest:
            raise ValueError("SQLite backup changed during import; deletion pass refused")
        # Source deletions are checked only after every table has scanned.
        for table in reversed(tables):
            pk = _primary_key(connection, table)
            after = ""
            while True:
                rows = db.execute(
                    "DECLARE $table AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
                    "SELECT source_key,target_table,target_key FROM migration_records "
                    "WHERE source_table=$table AND source_key>$after ORDER BY source_key LIMIT $limit;",
                    {"$table": table, "$after": after,
                     "$limit": ydb.TypedValue(batch_size, ydb.PrimitiveType.Uint64)},
                )[0].rows
                if not rows:
                    break
                for manifest in rows:
                    after = _text(manifest.source_key)
                    values = json.loads(after)
                    predicate = " AND ".join(f'"{name}"=?' for name in pk)
                    if connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1', values
                    ).fetchone():
                        continue

                    def remove(
                        tx: Transaction, table: str = table, after: str = after,
                        manifest: Any = manifest,
                    ) -> None:
                        _delete(tx, _text(manifest.target_table), json.loads(_text(manifest.target_key)))
                        if table == "llm_calls":
                            previous = _manifest_row(tx, table, after)
                            if previous:
                                _adjust_legacy_budget(tx, json.loads(_text(previous.payload)), None)
                        _delete(tx, "migration_records", {"source_table": table, "source_key": after})

                    db.transaction(remove)
                    counts[table] += 1
        if chart_cache is not None:
            if "reports" not in tables:
                raise ValueError("chart cache requires source reports")
            counts["chart_cache"] = _import_chart_cache(connection, db, chart_cache)
        if ai_ledger is not None:
            counts["ai_ledger"] = _import_ai_ledger(db, ai_ledger)
        if _file_sha256(source) != source_digest:
            raise ValueError("SQLite backup changed during import; completion refused")

        def finish(tx: Transaction) -> None:
            _upsert(tx, "metadata", {"name": "sqlite_import_sha256", "value": source_digest})
            _upsert(tx, "metadata", {
                "name": "sqlite_import_state",
                "value": _json({"state": "complete", "source_sha256": source_digest}),
            })

        db.transaction(finish)
        return counts
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path, help="closed application-created SQLite online backup")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--pause-seconds", type=float, default=0.0)
    parser.add_argument("--chart-cache", type=Path, help="closed legacy chart-data-cache bundle")
    parser.add_argument("--ai-ledger", type=Path, help="closed legacy .ai-ledger.json snapshot")
    args = parser.parse_args()
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        db.initialize()
        counts = import_backup(args.backup, db, batch_size=args.batch_size,
                               pause_seconds=args.pause_seconds, chart_cache=args.chart_cache,
                               ai_ledger=args.ai_ledger)
        print(_json({"changed_or_removed_rows": counts}))
    finally:
        db.close()


if __name__ == "__main__":
    main()

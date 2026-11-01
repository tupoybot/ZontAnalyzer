"""Independent snapshot transfer proof against disposable local YDB."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import ydb  # type: ignore[import-untyped]

from tests.integration.test_ydb_import import _source
from tests.ydb_support import make_ydb_database
from tools.import_sqlite import import_backup
from tools.transfer_ydb_snapshot import (
    _artifact_manifest,
    _check_schema,
    _expected_indexes,
    _expected_markers,
    export_snapshot,
    verify_snapshot,
)
from tools.upload_ydb_snapshot import upload_snapshot
from zont_analyzer.adapters.ydb.database import YdbDatabase


@pytest.mark.ydb
@pytest.mark.parametrize("name,columns", [
    ("by_kind_end", ("kind", "period_start")),
    ("unknown_read_index", ("kind", "period_end")),
])
def test_snapshot_schema_accepts_compatible_old_source_and_rejects_unexpected_indexes(
    name: str, columns: tuple[str, ...],
) -> None:
    ydb_database = make_ydb_database(fresh_schema=True)
    _check_schema(ydb_database)
    assert _expected_indexes("reports")["by_kind_end"] == ("kind", "period_end")
    session = ydb_database.driver.table_client.session().create()

    def alter(**changes) -> None:
        operation = session.alter_table(ydb_database.path + "/reports", **changes)
        deadline = time.monotonic() + 30
        while not operation.ready and time.monotonic() < deadline:
            time.sleep(0.1)
            operation = operation.get()
        assert operation.ready, "Disposable test index change did not finish"

    try:
        alter(drop_indexes=["by_kind_end"])
        _check_schema(ydb_database)
        alter(add_indexes=[ydb.TableIndex(name).with_global_index().with_index_columns(*columns)])
        with pytest.raises(ValueError, match="YDB index or TTL mismatch: reports"):
            _check_schema(ydb_database)
    finally:
        session.delete()
        ydb_database.close()


@pytest.mark.ydb
def test_export_stream_and_independent_target_comparison(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    backup = tmp_path / "closed.sqlite"
    _source(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT)")
        connection.executemany("INSERT INTO app_meta VALUES(?,?)",
                               [(f"entry-{index:04d}", None if index % 2 else str(index))
                                for index in range(601)])
        connection.executemany(
            "INSERT INTO telemetry_samples VALUES(?,?,?,?,?,?)",
            [(47, 1735689600 + offset, float(offset), None, "valid", "2025-01-01 00:00:00")
             for offset in range(1, 602)],
        )
    import_backup(backup, ydb_database, batch_size=500)
    artifact = tmp_path / "private-artifact"
    exported = export_snapshot(ydb_database, artifact, backup, require_complete_schema=False,
                               require_sidecars=False)
    assert exported["tables"]["telemetry_samples"] == 602
    assert exported["tables"]["app_meta"] == 601
    assert exported["total_rows"] >= 602
    assert exported["total_jsonl_bytes"] > 0
    assert verify_snapshot(ydb_database, artifact)["ok"] is True

    target = make_ydb_database(fresh_schema=True)
    try:
        uploaded = upload_snapshot(target, artifact, ru_per_second=1000)
        assert uploaded["status"] == "uploaded_unverified"
        compared = verify_snapshot(target, artifact, require_upload_complete=True)
        assert compared["ok"] is True
        assert compared["tables"]["telemetry_samples"]["actual_rows"] == 602
        assert compared["migration_records_empty"] is True
        target.execute("DELETE FROM telemetry_samples WHERE series_id=47 AND timestamp_utc=1735689601;")
        compared = verify_snapshot(target, artifact, require_upload_complete=True)
        assert compared["ok"] is False
        assert compared["tables"]["telemetry_samples"]["missing"] == 1
    finally:
        target.close()

    ydb_database.execute(
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num) "
        "VALUES (47,1735689601,-1.0);"
    )
    compared = verify_snapshot(ydb_database, artifact)
    assert compared["ok"] is False
    assert compared["tables"]["telemetry_samples"]["changed"] == 1
    ydb_database.execute(
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num) "
        "VALUES (47,1735699999,11.0);"
    )
    compared = verify_snapshot(ydb_database, artifact)
    assert compared["tables"]["telemetry_samples"]["extra"] == 1
    with pytest.raises(ValueError, match='"target_mismatches": 1'):
        export_snapshot(ydb_database, tmp_path / "rejected-artifact", backup,
                        require_complete_schema=False, require_sidecars=False)


@pytest.mark.ydb
def test_artifact_tampering_rejected_before_target_read(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    backup = tmp_path / "closed.sqlite"
    _source(backup)
    import_backup(backup, ydb_database)
    artifact = tmp_path / "private-artifact"
    export_snapshot(ydb_database, artifact, backup, require_complete_schema=False,
                    require_sidecars=False)
    with (artifact / "telemetry_samples.jsonl").open("ab") as output:
        output.write(b"{}\n")
    with pytest.raises(ValueError, match="artifact checksum mismatch"):
        verify_snapshot(ydb_database, artifact)


@pytest.mark.ydb
def test_verifier_requires_complete_exact_upload_markers(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    backup = tmp_path / "closed.sqlite"
    _source(backup)
    import_backup(backup, ydb_database)
    artifact = tmp_path / "private-artifact"
    export_snapshot(ydb_database, artifact, backup, require_complete_schema=False,
                    require_sidecars=False)
    # The verified local source keeps its import journal; upload verification
    # allows no such rows in the new target.
    assert verify_snapshot(ydb_database, artifact)["ok"] is True
    assert verify_snapshot(ydb_database, artifact, require_upload_complete=True)["ok"] is False
    ydb_database.execute("DELETE FROM migration_records;")
    expected = _expected_markers(artifact, _artifact_manifest(artifact))
    rows = [{"name": name, "value": json.dumps(value, sort_keys=True, separators=(",", ":"))}
            for name, value in expected.items()]
    row_type = ydb.StructType().add_member("name", ydb.PrimitiveType.Utf8).add_member(
        "value", ydb.PrimitiveType.Utf8)
    ydb_database.execute(
        "DECLARE $rows AS List<Struct<name:Utf8,value:Utf8>>; "
        "UPSERT INTO metadata SELECT * FROM AS_TABLE($rows);",
        {"$rows": ydb.TypedValue(rows, ydb.ListType(row_type))},
    )
    complete = verify_snapshot(ydb_database, artifact, require_upload_complete=True)
    assert complete["ok"] is True and complete["upload_markers_complete"] is True
    ydb_database.execute(
        "UPSERT INTO migration_records (source_table,source_key) "
        "VALUES ('telemetry_samples','unexpected');"
    )
    journal = verify_snapshot(ydb_database, artifact, require_upload_complete=True)
    assert journal["ok"] is False and journal["migration_records_empty"] is False
    ydb_database.execute("DELETE FROM migration_records;")
    ydb_database.execute(
        "UPSERT INTO metadata (name,value) VALUES ('__snapshot_transfer__unknown','{}');"
    )
    assert verify_snapshot(ydb_database, artifact, require_upload_complete=True)["ok"] is False

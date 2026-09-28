"""Upload paths use isolated local YDB namespaces, never managed YDB."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import tools.upload_ydb_snapshot as upload_module
from tests.ydb_support import make_ydb_database
from tools import transfer_ydb_snapshot as transfer
from tools.upload_ydb_snapshot import STATE, TABLE_MARKER, _artifact_preflight, upload_snapshot
from zont_analyzer.adapters.ydb.database import YdbDatabase


def _artifact(path: Path, *, device_count: int = 2) -> Path:
    path.mkdir()
    source_hash = hashlib.sha256(b"synthetic stopped backup").hexdigest()
    metadata = {
        "schema_version": "2", "schema_hash": transfer.SCHEMA_HASH,
        "sqlite_import_sha256": source_hash,
        "sqlite_import_state": json.dumps({"state": "complete", "source_sha256": source_hash},
                                          sort_keys=True, separators=(",", ":")),
    }
    contents: dict[str, list[dict[str, Any]]] = {
        "metadata": [{"name": key, "value": value} for key, value in sorted(metadata.items())],
        "devices": [{"id": f"device-{i}", "payload": "{}"} for i in range(device_count)],
        "interventions": [{"id": "intervention-1", "recommendation_id": None,
                           "applied_at": 1, "payload": "{}"}],
    }
    entries = {}
    for table in transfer.TABLE_NAMES:
        rows = sorted(contents.get(table, []), key=lambda row: transfer._key(row, table))
        raw = b"".join(transfer._canonical(row) for row in rows)
        (path / f"{table}.jsonl").write_bytes(raw)
        entries[table] = {"rows": len(rows), "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()}
    (path / "manifest.json").write_bytes(transfer._canonical({
        "format": 1, "schema_sha256": transfer.SCHEMA_HASH,
        "source_import_sha256": source_hash, "tables": entries,
    }))
    return path


class NoWait:
    def wait(self, _sizes: list[int], _indexed: bool) -> None:
        return


@pytest.mark.ydb
def test_upload_nonindexed_bulk_indexed_transaction_and_resume(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    artifact = _artifact(tmp_path / "artifact", device_count=501)
    client = ydb_database.driver.table_client
    calls: list[str] = []

    def interrupt(path: str, rows: list[dict[str, Any]], types: Any) -> None:
        calls.append(path)
        if path.endswith("/devices") and sum(call.endswith("/devices") for call in calls) == 2:
            raise RuntimeError("synthetic interruption")
        client.bulk_upsert(path, rows, types)

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        upload_snapshot(ydb_database, artifact, batch_rows=500, bulk=interrupt, pacer=NoWait())
    assert ydb_database.execute("SELECT id FROM devices;")[0].rows
    assert len(ydb_database.execute("SELECT id FROM interventions;")[0].rows) == 1
    state = ydb_database.execute(
        "DECLARE $name AS Utf8; SELECT value FROM metadata WHERE name=$name;", {"$name": STATE},
    )[0].rows[0]
    assert json.loads(state.value)["status"] == "loading"
    progress = ydb_database.execute(
        "DECLARE $name AS Utf8; SELECT value FROM metadata WHERE name=$name;",
        {"$name": TABLE_MARKER + "devices"},
    )[0].rows[0]
    assert json.loads(progress.value)["rows"] == 500

    result = upload_snapshot(ydb_database, artifact, batch_rows=500, pacer=NoWait())
    assert result["status"] == "uploaded_unverified"
    assert len(ydb_database.execute("SELECT id FROM devices;")[0].rows) == 501
    assert len(ydb_database.execute("SELECT id FROM interventions;")[0].rows) == 1
    assert transfer.verify_snapshot(ydb_database, artifact)["ok"]
    again = upload_snapshot(ydb_database, artifact, batch_rows=500, pacer=NoWait())
    assert again["tables"] == result["tables"]


@pytest.mark.ydb
def test_upload_rejects_nonempty_target_and_other_artifact(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    artifact = _artifact(tmp_path / "artifact")
    ydb_database.execute("UPSERT INTO devices (id,payload) VALUES ('existing','{}');")
    with pytest.raises(ValueError, match="not empty"):
        upload_snapshot(ydb_database, artifact, pacer=NoWait())
    assert not ydb_database.execute(
        "DECLARE $name AS Utf8; SELECT name FROM metadata WHERE name=$name;", {"$name": STATE},
    )[0].rows

    fresh = make_ydb_database(fresh_schema=True)
    try:
        upload_snapshot(fresh, artifact, pacer=NoWait())
        other = _artifact(tmp_path / "other", device_count=3)
        with pytest.raises(ValueError, match="another artifact"):
            upload_snapshot(fresh, other, pacer=NoWait())
    finally:
        fresh.close()


@pytest.mark.ydb
def test_upload_rejects_tampering_before_first_write(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    artifact = _artifact(tmp_path / "artifact")
    (artifact / "devices.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="checksum"):
        upload_snapshot(ydb_database, artifact, pacer=NoWait())
    assert not ydb_database.execute(
        "DECLARE $name AS Utf8; SELECT name FROM metadata WHERE name=$name;", {"$name": STATE},
    )[0].rows
    artifact = _artifact(tmp_path / "bad-source")
    manifest = json.loads((artifact / "manifest.json").read_text())
    manifest["source_import_sha256"] = "0" * 64
    (artifact / "manifest.json").write_bytes(transfer._canonical(manifest))
    with pytest.raises(ValueError, match="backup hash mismatch"):
        _artifact_preflight(artifact)


@pytest.mark.ydb
def test_resume_replays_bulk_upsert_after_unmarked_commit(
    tmp_path: Path, ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact(tmp_path / "artifact", device_count=1)
    original = upload_module._write_marker
    interrupted = False

    def lose_progress(db: YdbDatabase, name: str, value: dict[str, Any]) -> None:
        nonlocal interrupted
        if name == TABLE_MARKER + "devices" and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic lost progress")
        original(db, name, value)

    with monkeypatch.context() as patch:
        patch.setattr(upload_module, "_write_marker", lose_progress)
        with pytest.raises(RuntimeError, match="synthetic lost progress"):
            upload_snapshot(ydb_database, artifact, pacer=NoWait())
    assert len(ydb_database.execute("SELECT id FROM devices;")[0].rows) == 1
    assert not ydb_database.execute(
        "DECLARE $name AS Utf8; SELECT name FROM metadata WHERE name=$name;",
        {"$name": TABLE_MARKER + "devices"},
    )[0].rows
    upload_snapshot(ydb_database, artifact, pacer=NoWait())
    assert len(ydb_database.execute("SELECT id FROM devices;")[0].rows) == 1
    assert transfer.verify_snapshot(ydb_database, artifact)["ok"]

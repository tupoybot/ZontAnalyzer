"""Contract tests for the standalone legacy SQLite artifact installer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


def importer(name: str = "import_payload"):
    path = Path(__file__).parents[2] / "deploy/import_analysis.py"
    spec = importlib.util.spec_from_file_location("import_analysis", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def _legacy_database(tmp_path: Path) -> Path:
    """Supply only the tables the old installer reads; the new runtime is YDB."""
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE reports(id TEXT PRIMARY KEY, canonical_json TEXT, generated_at TEXT);
            CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE recommendations(id TEXT PRIMARY KEY, status TEXT, owner_note TEXT);
            CREATE TABLE llm_calls(id TEXT PRIMARY KEY);
            INSERT INTO reports VALUES('daily:1',
                '{"id":"daily:1","context":{"gas":{"model_version":"old"}}}',
                '2026-01-02T00:00:00+00:00');
            INSERT INTO reports VALUES('daily:2',
                '{"id":"daily:2","context":{"gas":{"model_version":"old"}}}',
                '2026-01-03T00:00:00+00:00');
            INSERT INTO recommendations VALUES('rec:owner','rejected','keep');
        """)
    return database


def test_derived_import_is_atomic_idempotent_and_preserves_owner_state(tmp_path: Path) -> None:
    database = _legacy_database(tmp_path)
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT id,canonical_json,generated_at FROM reports ORDER BY id").fetchall()
        owner_before = connection.execute("SELECT * FROM recommendations").fetchall()
    payload = {"reports": [], "app_meta": {"gas-model:hash": "{}"}, "new_rows": {}}
    for identifier, canonical, generated_at in rows:
        value = json.loads(canonical)
        value["context"]["gas"] = {"model_version": "new"}
        payload["reports"].append({
            "id": identifier, "previous_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "canonical_json": json.dumps(value), "generated_at": generated_at,
        })
    good_hash = payload["reports"][1]["previous_sha256"]
    payload["reports"][1]["previous_sha256"] = "wrong"
    apply = importer()
    with pytest.raises(ValueError, match="changed since backup"):
        apply(database, payload)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT canonical_json FROM reports WHERE id='daily:1'").fetchone()[0] == rows[0][1]
        assert connection.execute("SELECT * FROM app_meta").fetchall() == []
    payload["reports"][1]["previous_sha256"] = good_hash
    assert apply(database, payload)["reports"] == 2
    assert apply(database, payload) == {"reports": 0, "app_meta": 0, "new_rows": 0}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM recommendations").fetchall() == owner_before
    payload["app_meta"] = {"owner-profile:forbidden": "{}"}
    with pytest.raises(ValueError, match="unsupported metadata"):
        apply(database, payload)


def test_prebuilt_chart_cache_is_guarded_and_idempotent(tmp_path: Path) -> None:
    database = _legacy_database(tmp_path)
    with sqlite3.connect(database) as connection:
        canonical = connection.execute("SELECT canonical_json FROM reports WHERE id='daily:1'").fetchone()[0]
    source = tmp_path / "prepared"
    source.mkdir()
    name = hashlib.sha256(b"daily:1").hexdigest() + ".json"
    packet = {"schema_version": 2, "report_digest": hashlib.sha256(canonical.encode()).hexdigest(),
              "data": {"series": {}, "timezone": "UTC"}}
    path = source / name
    path.write_text(json.dumps(packet), encoding="utf-8")
    install = importer("install_chart_cache")
    assert install(database, source) == 1
    assert install(database, source) == 0
    destination = database.parent / "chart-data-cache" / name
    assert destination.read_bytes() == path.read_bytes()
    packet["report_digest"] = "stale"
    path.write_text(json.dumps(packet), encoding="utf-8")
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        install(database, source)
    assert destination.read_bytes() == before


def test_chart_cache_bundle_rejects_non_cache_paths(tmp_path: Path) -> None:
    database = _legacy_database(tmp_path)
    source = tmp_path / "prepared"
    source.mkdir()
    (source / "unexpected.txt").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        importer("install_chart_cache")(database, source)

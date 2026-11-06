import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import cleanup_ydb_storage as cleanup
from tools.compact_configuration_export import compact
from zont_analyzer.adapters.ydb.database import YdbConfig


def fake_db(monkeypatch, snapshots=None, metadata=None):
    tables = {name: [] for name in cleanup.EXPORT_COLUMNS}
    tables["config_snapshots"] = snapshots or []
    tables["app_meta"] = metadata or []
    monkeypatch.setattr(cleanup, "_inventory", lambda db: [])
    return SimpleNamespace(config=YdbConfig("grpc://fixture:2136", "/local", "fixture", True),
                           read_table=lambda table, **kw: kw["consume"](iter(tables[table])))


def snapshot(identity=1, setting=20):
    return {"device_id": "fixture", "content_hash": f"old-{identity}", "id": identity,
            "captured_at": identity * 1000, "payload": json.dumps({"setting": setting, "online": True})}


def test_reviewed_plan_rejects_modified_source_and_wrong_digest(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    cleanup.export_storage(fake_db(monkeypatch, [snapshot()]), directory)
    summary = cleanup.create_plan(directory)
    assert cleanup.load_plan(directory, summary["plan_sha256"])["configuration"]["summary"]["retained_events"] == 1
    with pytest.raises(ValueError, match="reviewed plan checksum"):
        cleanup.load_plan(directory, "0" * 64)
    with (directory / "config_snapshots.jsonl").open("a") as stream:
        stream.write(json.dumps(snapshot(2)) + "\n")
    with pytest.raises(ValueError, match="export checksum"):
        cleanup.load_plan(directory, summary["plan_sha256"])


def test_apply_rejects_another_database_before_connecting(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    cleanup.export_storage(fake_db(monkeypatch, [snapshot()]), directory)
    summary = cleanup.create_plan(directory)
    monkeypatch.setenv("YDB_ENDPOINT", "grpc://different:2136")
    monkeypatch.setenv("YDB_DATABASE", "/local")
    monkeypatch.setenv("YDB_NAMESPACE", "fixture")
    monkeypatch.setenv("YDB_ANONYMOUS_CREDENTIALS", "1")
    monkeypatch.setattr("sys.argv", ["cleanup", "apply", str(directory), "--plan-sha256", summary["plan_sha256"]])
    monkeypatch.setattr(cleanup, "YdbDatabase", lambda config: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="database scope mismatch"):
        cleanup.main()


def test_reviewed_but_altered_plan_must_still_match_source(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    cleanup.export_storage(fake_db(monkeypatch, [snapshot()]), directory)
    cleanup.create_plan(directory)
    path = directory / "plan.json"
    plan = json.loads(path.read_text())
    plan["configuration"]["events"][0]["target"]["payload"] = "{}"
    path.write_text(json.dumps(plan, ensure_ascii=False))
    with pytest.raises(ValueError, match="immutable source export"):
        cleanup.load_plan(directory, cleanup._sha(path))


def test_incomplete_export_cannot_be_planned_and_evidence_is_not_overwritten(tmp_path, monkeypatch):
    directory = tmp_path / "source"
    with pytest.raises(ValueError, match="row budget"):
        cleanup.export_storage(fake_db(monkeypatch, [snapshot(), snapshot(2)]), directory, max_rows=1)
    assert not (directory / "manifest.json").exists()
    with pytest.raises(FileExistsError):
        cleanup.export_storage(fake_db(monkeypatch), directory)
    assert directory.stat().st_mode & 0o777 == 0o700


def test_verify_detects_lost_history_and_protected_data(tmp_path, monkeypatch):
    source = [snapshot(), snapshot(2), snapshot(3, 21), snapshot(4)]
    targets, _ = compact(source)
    before, after = tmp_path / "before", tmp_path / "after"
    protected = [{"key": "gas-report-revision:fixture", "value": "preserve"}]
    cleanup.export_storage(fake_db(monkeypatch, source, protected), before)
    cleanup.export_storage(fake_db(monkeypatch, targets, protected), after)
    assert cleanup.verify_exports(before, after)["strict_comparison_passed"]
    assert (before / "manifest.json").stat().st_mode & 0o777 == 0o600
    missing = tmp_path / "missing"
    cleanup.export_storage(fake_db(monkeypatch, targets[:-1]), missing)
    result = cleanup.verify_exports(before, missing)
    assert not result["strict_comparison_passed"]
    assert not result["configuration"]["source_timeline_equal"]
    assert result["protected"]["metadata_missing"] == 1


def test_private_export_rejects_repository_path(monkeypatch):
    with pytest.raises(ValueError, match="outside the repository"):
        cleanup.export_storage(fake_db(monkeypatch), Path(cleanup.__file__).parent / "private-data")

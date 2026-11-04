"""Offline adversarial acceptance for exact-byte migration and exact-version cleanup."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from types import SimpleNamespace
from xml.sax.saxutils import escape

import httpx
import pytest

from tools.cleanup_publication_storage import (
    VersionInventory,
    apply_cleanup,
    candidates,
    prepare_cleanup,
    read_versions,
)
from tools.migrate_publication_storage import migrate, rollback
from tools.publication_storage_admin import (
    MaintenanceError,
    PrivateArtifacts,
    digest,
    encoded,
    parse_json,
    read_iam_token,
    storage_target,
    validate_manifest,
)
from zont_analyzer.cloud.object_storage import ObjectStorage, StoredObject
from zont_analyzer.cloud.publication import CloudPublication


class MemoryStorage:
    bucket = "test-publications"
    prefix = "private/publication"

    def __init__(self):
        self.objects = {}
        self.writes = []
        self.fail_on = None
        self.race_on = None

    def get(self, key):
        return self.objects.get(key)

    def seed(self, key, body):
        value = StoredObject(body, "application/octet-stream", '"' + digest(body) + '"')
        self.objects[key] = value
        return value.etag

    def conditional_put(self, key, body, content_type, *, expected_etag):
        if self.fail_on == key:
            self.fail_on = None
            raise MaintenanceError("interrupted fixture")
        if self.race_on == key:
            self.seed(key, b"competing bytes")
            self.race_on = None
        current = self.get(key)
        if (current.etag if current else None) != expected_etag:
            return None
        self.writes.append(key)
        return self.seed(key, body)

    def compare_and_swap(self, key, body, content_type, *, expected_etag):
        return self.conditional_put(key, body, content_type, expected_etag=expected_etag) is not None


class Repository:
    def __init__(self, items, meta):
        self.items, self.meta = items, meta
        self.saved = []
        self.lose_response = False

    def load(self):
        return copy.deepcopy((self.items, self.meta))

    def load_meta(self):
        return dict(self.meta)

    def save(self, items, previous, meta, old_meta, **kwargs):
        assert previous == self.items and old_meta == self.meta
        assert kwargs["expected_checkpoint"] == int(self.meta["checkpoint"])
        self.saved.append(copy.deepcopy((items, meta, kwargs)))
        self.items, self.meta = copy.deepcopy((items, meta))
        if self.lose_response:
            self.lose_response = False
            raise MaintenanceError("commit response lost")
        return True


class Jobs:
    def __init__(self):
        self.attempt = 0
        self.busy = False
        self.lost = False
        self.released = 0

    def acquire(self, key, owner, seconds):
        assert key == "publication"
        if self.busy:
            return None
        self.attempt += 1
        return SimpleNamespace(attempt=self.attempt)

    def renew(self, key, owner, attempt, seconds):
        return None if self.lost else SimpleNamespace(attempt=attempt)

    def release(self, key, owner, attempt):
        self.released += 1


def legacy(storage, category, suffix, body):
    key = f"publication/{category}/{hashlib.sha256(body).hexdigest()}.{suffix}"
    return key, storage.seed(key, body)


@pytest.fixture
def setup(tmp_path):
    storage = MemoryStorage()
    entries, items = [], {}
    for href in ("daily/2024-01-01.html", "monthly/2024-01-01.html"):
        html_key, html_stamp = legacy(storage, "reports", "html", (href + "\nhtml\x00").encode())
        json_key, json_stamp = legacy(storage, "reports", "json", (href + "\njson\r\n").encode())
        entry = {"href": href, "kind": href.split("/")[0], "start": "2024-01-01",
                 "html_key": html_key, "json_key": json_key, "complete": True}
        entries.append(entry)
        items[href] = {"entry": json.dumps(entry), "html_stamp": html_stamp, "json_stamp": json_stamp,
                       "dirty": 5, "digest": "unchanged", "generated": 123, "report_id": "unchanged"}
    manifest_key, _ = legacy(storage, "manifests", "json", encoded({
        "version": 1, "updated_at": "2024-02-01T00:00:00+00:00", "reports": entries,
    }))
    latest_key, _ = legacy(storage, "latest", "html", b"latest\r\n\x00")
    repo = Repository(items, {"manifest_key": manifest_key, "latest_key": latest_key,
                              "storage_target": storage_target(storage), "checkpoint": "42",
                              "latest_report_id": "unchanged", "queue_metrics": "unchanged"})
    CloudPublication(storage).update_site_index(repo)
    storage.writes.clear()
    artifacts = PrivateArtifacts(tmp_path / "private")
    return repo, Jobs(), storage, artifacts


@pytest.mark.parametrize("entry_patch", [
    {"href": "../2024-01-01.html"}, {"href": "daily/2024-02-31.html"},
    {"href": "daily/2024-01-01.html?download=1"}, {"html_key": "other/report.html"},
    {"start": "2024-01-02"}, {"kind": "weekly"}, {"json_key": "publication/reports/" + "0" * 64 + ".html"},
])
def test_rejects_malformed_manifest_paths(setup, entry_patch):
    repo, _, storage, _ = setup
    value = parse_json(storage.get(repo.meta["manifest_key"]).body)
    value["reports"][0].update(entry_patch)
    with pytest.raises(MaintenanceError):
        validate_manifest(encoded(value))


def test_duplicate_period_path_rejected(setup):
    repo, _, storage, _ = setup
    value = parse_json(storage.get(repo.meta["manifest_key"]).body)
    value["reports"].append(value["reports"][0])
    with pytest.raises(MaintenanceError, match="duplicate"):
        validate_manifest(encoded(value))


def test_inventory_default_does_not_mutate_storage_or_jobs(setup):
    repo, jobs, storage, artifacts = setup
    before = repo.load()
    result = migrate(repo, jobs, storage, artifacts)
    assert result["applied"] is False
    assert not storage.writes and jobs.attempt == 0 and repo.load() == before
    assert artifacts.read("migration-inventory.json")
    assert all(path.stat().st_mode & 0o077 == 0 for path in artifacts.path.iterdir())


def test_migration_copies_exact_bytes_and_preserves_semantic_state(setup):
    repo, jobs, storage, artifacts = setup
    before_items, before_meta = repo.load()
    before_index = storage.get("site-index.json").body
    result = migrate(repo, jobs, storage, artifacts, apply=True)
    assert result["reports"] == 2
    assert repo.meta["storage_layout"] == "period-v1"
    assert repo.meta["checkpoint"] == before_meta["checkpoint"]
    for href, item in repo.items.items():
        old = parse_json(before_items[href]["entry"])
        assert storage.get(href).body == storage.get(old["html_key"]).body
        assert storage.get(href[:-5] + ".json").body == storage.get(old["json_key"]).body
        assert {k: v for k, v in item.items() if k not in {"entry", "html_stamp", "json_stamp"}} == {
            k: v for k, v in before_items[href].items() if k not in {"entry", "html_stamp", "json_stamp"}}
    assert storage.get("latest.html").body == storage.get(before_meta["latest_key"]).body
    assert storage.get("site-index.json").body != before_index
    assert artifacts.read("sha256-" + digest(before_index)) == before_index
    assert jobs.released == 1
    writes = list(storage.writes)
    assert migrate(repo, jobs, storage, artifacts, apply=True)["already_migrated"]
    assert storage.writes == writes


@pytest.mark.parametrize("damage", ["missing", "checksum", "etag"])
def test_invalid_source_aborts_before_writes(setup, damage):
    repo, jobs, storage, artifacts = setup
    key = parse_json(next(iter(repo.items.values()))["entry"])["html_key"]
    if damage == "missing":
        del storage.objects[key]
    elif damage == "checksum":
        item = next(iter(repo.items.values()))
        item["html_stamp"] = storage.seed(key, b"wrong checksum")
    else:
        storage.seed(key, b"wrong etag")
    with pytest.raises(MaintenanceError):
        migrate(repo, jobs, storage, artifacts, apply=True)
    assert not storage.writes and not repo.saved


def test_existing_conflicting_target_stops_entire_migration(setup):
    repo, jobs, storage, artifacts = setup
    storage.seed("monthly/2024-01-01.json", b"other")
    with pytest.raises(MaintenanceError, match="destination conflicts"):
        migrate(repo, jobs, storage, artifacts, apply=True)
    assert not storage.writes and not repo.saved


def test_racing_conditional_create_cannot_overwrite(setup):
    repo, jobs, storage, artifacts = setup
    storage.race_on = "daily/2024-01-01.html"
    with pytest.raises(MaintenanceError, match="create conflicted"):
        migrate(repo, jobs, storage, artifacts, apply=True)
    assert storage.get("daily/2024-01-01.html").body == b"competing bytes"
    assert not repo.saved


def test_interrupted_copy_resumes_from_private_prepared_manifest(setup):
    repo, jobs, storage, artifacts = setup
    storage.fail_on = "reports.json"
    with pytest.raises(MaintenanceError):
        migrate(repo, jobs, storage, artifacts, apply=True)
    journal = artifacts.read("migration.json")
    assert not repo.saved
    migrate(repo, jobs, storage, artifacts, apply=True)
    assert artifacts.read("migration.json") == journal
    assert len(repo.saved) == 1


def test_lost_commit_response_repairs_browser_projection_without_copy(setup):
    repo, jobs, storage, artifacts = setup
    repo.lose_response = True
    with pytest.raises(MaintenanceError):
        migrate(repo, jobs, storage, artifacts, apply=True)
    before = list(storage.writes)
    assert "reports.json" in before and repo.meta["storage_layout"] == "period-v1"
    migrate(repo, jobs, storage, artifacts, apply=True)
    assert storage.writes == before + ["site-index.json"]
    assert len(repo.saved) == 1


def test_rollback_restores_exact_original_state_and_can_remigrate(setup):
    repo, jobs, storage, artifacts = setup
    original = repo.load()
    old_index = storage.get("site-index.json").body
    migrate(repo, jobs, storage, artifacts, apply=True)
    stable_bytes = storage.get("daily/2024-01-01.html").body
    assert rollback(repo, jobs, storage, artifacts)["rolled_back"]
    assert repo.load() == original
    assert storage.get("site-index.json").body == old_index
    assert storage.get("daily/2024-01-01.html").body == stable_bytes
    assert rollback(repo, jobs, storage, artifacts)["already_restored"]
    migrate(repo, jobs, storage, artifacts, apply=True)
    assert repo.meta["storage_layout"] == "period-v1"
    assert storage.get("daily/2024-01-01.html").body == stable_bytes


@pytest.mark.parametrize("change", ["metadata", "item", "deleted_legacy", "backup"])
def test_rollback_refuses_new_work_or_missing_legacy_rollback_material(setup, change):
    repo, jobs, storage, artifacts = setup
    migrate(repo, jobs, storage, artifacts, apply=True)
    inventory = parse_json(artifacts.read("migration-inventory.json"))
    if change == "metadata":
        repo.meta["checkpoint"] = "43"
    elif change == "item":
        next(iter(repo.items.values()))["dirty"] = 7
    elif change == "deleted_legacy":
        del storage.objects[inventory["objects"][0]["source"]]
    else:
        artifacts.write(inventory["objects"][0]["backup"], b"corrupted backup")
    before = repo.load()
    saved = len(repo.saved)
    with pytest.raises(MaintenanceError):
        rollback(repo, jobs, storage, artifacts)
    assert repo.load() == before and len(repo.saved) == saved


def test_busy_or_lost_publication_lease_does_not_write(setup):
    repo, jobs, storage, artifacts = setup
    jobs.busy = True
    with pytest.raises(MaintenanceError, match="busy"):
        migrate(repo, jobs, storage, artifacts, apply=True)
    jobs.busy = False
    jobs.lost = True
    with pytest.raises(MaintenanceError, match="lost"):
        migrate(repo, jobs, storage, artifacts, apply=True)
    assert not storage.writes and not repo.saved


def test_private_artifacts_refuse_checkout_and_symlinks(tmp_path):
    with pytest.raises(MaintenanceError):
        PrivateArtifacts("tools/private-maintenance")
    (tmp_path / "actual").mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(tmp_path / "actual")
    with pytest.raises(MaintenanceError):
        PrivateArtifacts(tmp_path / "link")


def test_explicit_iam_file_drives_both_dependencies_without_metadata_fallback(tmp_path, monkeypatch):
    from tools.publication_storage_admin import open_dependencies

    token_file = tmp_path / "token"
    token_file.write_text("test-private-token\n")
    token_file.chmod(0o600)
    monkeypatch.setenv("YDB_ENDPOINT", "grpcs://database.example:2135")
    monkeypatch.setenv("YDB_DATABASE", "/database")
    monkeypatch.setenv("YDB_NAMESPACE", "application")
    monkeypatch.setenv("YDB_METADATA_CREDENTIALS", "1")
    monkeypatch.setenv("YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS", "unwanted-fallback")
    monkeypatch.setenv("CLOUD_PUBLICATION_BUCKET", "test-publications")
    database = SimpleNamespace(close=lambda: None)

    def connect(config):
        assert os.environ["YDB_ACCESS_TOKEN_CREDENTIALS"] == "test-private-token"
        assert "YDB_METADATA_CREDENTIALS" not in os.environ
        assert "YDB_SERVICE_ACCOUNT_KEY_FILE_CREDENTIALS" not in os.environ
        return database

    monkeypatch.setattr("zont_analyzer.adapters.ydb.database.YdbDatabase", connect)
    db, _, _, storage = open_dependencies("application", token_file)
    assert db is database and storage.token() == "test-private-token"


def test_iam_file_requires_private_permissions_and_rejects_embedded_newline(tmp_path):
    path = tmp_path / "token"
    path.write_text("private-token")
    path.chmod(0o644)
    with pytest.raises(MaintenanceError, match="owner-only"):
        read_iam_token(path)
    path.chmod(0o600)
    path.write_text("private\ntoken")
    with pytest.raises(MaintenanceError, match="invalid IAM"):
        read_iam_token(path)


@pytest.mark.parametrize("module", ["tools.migrate_publication_storage", "tools.cleanup_publication_storage"])
def test_cli_failures_do_not_print_secret_or_private_path(tmp_path, monkeypatch, capsys, module):
    import importlib

    tool = importlib.import_module(module)
    artifact_path = tmp_path / "private-cli-artifacts"
    token_path = tmp_path / "private-token-file"
    monkeypatch.setattr("sys.argv", [module, "--target-namespace", "application", "--artifacts", str(artifact_path),
                                     "--iam-token-file", str(token_path)])

    def failure(namespace, token_file):
        assert token_file == token_path
        raise RuntimeError("private-token-value " + str(token_path))

    monkeypatch.setattr(tool, "open_dependencies", failure)
    with pytest.raises(SystemExit) as exc:
        tool.main()
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert not output.out and "private-token-value" not in output.err and str(token_path) not in output.err


class Versions:
    def __init__(self, storage):
        self.rows = []
        self.bodies = {}
        self.deleted = []
        self.fail_after = None
        for key, value in storage.objects.items():
            self.add(key, value.body, "current", latest=True)

    def add(self, key, body, version_id, *, latest=False, marker=False):
        row = {"key": key, "etag": '"' + digest(body) + '"' if not marker else "", "size": len(body),
               "version_id": version_id, "delete_marker": marker, "is_latest": latest}
        self.rows.append(row)
        self.bodies[(key, version_id)] = StoredObject(body, "application/octet-stream", row["etag"])
        return row

    def inventory(self, *, fence=lambda: None):
        fence()
        versions = sorted(copy.deepcopy(self.rows), key=lambda r: (r["key"], r["version_id"]))
        objects = [{k: v for k, v in row.items() if k != "is_latest"} for row in versions
                   if row["is_latest"] and not row["delete_marker"]]
        return {"objects": objects, "versions": versions}

    def get_version(self, row):
        return self.bodies[(row["key"], row["version_id"])]

    def delete_version(self, row):
        if self.fail_after is not None and len(self.deleted) >= self.fail_after:
            raise MaintenanceError("injected deletion interruption")
        self.rows.remove(row)
        self.deleted.append(row)


def stable_setup(setup):
    repo, jobs, storage, artifacts = setup
    migrate(repo, jobs, storage, artifacts, apply=True)
    storage.seed("seasonal/1999-01-01.html", b"archive outside current manifest")
    storage.seed("seasonal/1999-01-01.json", b"archive JSON")
    storage.seed("unrelated/keep.txt", b"unrelated current")
    versions = Versions(storage)
    versions.add("daily/2024-01-01.html", b"prior stable page", "old")
    versions.add("unrelated/keep.txt", b"prior unrelated", "old")
    versions.add("reports.json", b"", "old-marker", marker=True)
    return repo, jobs, storage, artifacts, versions


def test_cleanup_retains_all_current_stable_archive_pairs_and_unrelated_keys(setup):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    result = prepare_cleanup(repo, storage, versions, artifacts)
    assert not result["applied"] and not versions.deleted
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    assert any(r["key"] == "daily/2024-01-01.html" and r["version_id"] == "old" for r in plan["deletions"])
    assert all(r["key"] != "unrelated/keep.txt" for r in plan["deletions"])
    assert all(not r["key"].startswith("seasonal/") for r in plan["deletions"])
    result = apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert result["deleted_versions"] == len(plan["deletions"])
    assert all(row["version_id"] for row in versions.deleted)


def test_cleanup_without_deleted_backups_never_reads_obsolete_bodies_and_keeps_current_files(setup, monkeypatch):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    current = [copy.deepcopy(row) for row in versions.rows
               if row["is_latest"] and not row["key"].startswith("publication/")]
    calls = []

    def forbidden_get(row):
        calls.append(row)
        raise AssertionError("obsolete body must not be downloaded")

    monkeypatch.setattr(versions, "get_version", forbidden_get)
    result = prepare_cleanup(repo, storage, versions, artifacts, backup_deleted_objects=False)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    assert plan["backup_deleted_objects"] is False and plan["backups"] == {}
    assert result["backup_objects"] == 0 and result["backup_deleted_objects"] is False
    result = apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert result["deleted_versions"] == len(plan["deletions"]) and not calls
    assert result["backup_deleted_objects"] is False
    assert all(row in versions.rows for row in current)


@pytest.mark.parametrize("change", ["inventory", "unsafe_deletion", "current_pair"])
def test_no_backup_cleanup_still_rejects_changed_or_unsafe_state(setup, change):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    prepare_cleanup(repo, storage, versions, artifacts, backup_deleted_objects=False)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    if change == "inventory":
        versions.add("daily/2000-01-01.html", b"changed inventory", "newer")
    elif change == "unsafe_deletion":
        plan["deletions"].append(next(r for r in versions.rows if r["key"] == "reports.json" and r["is_latest"]))
        artifacts.write("cleanup-plan.json", encoded(plan))
    else:
        storage.seed("daily/2024-01-01.json", b"uncommitted current bytes")
    with pytest.raises(MaintenanceError):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert not versions.deleted


@pytest.mark.parametrize("policy", [None, 0, 1, "false", [], {}])
def test_cleanup_rejects_non_boolean_saved_backup_policy(setup, policy):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    prepare_cleanup(repo, storage, versions, artifacts, backup_deleted_objects=False)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    plan["backup_deleted_objects"] = policy
    artifacts.write("cleanup-plan.json", encoded(plan))
    with pytest.raises(MaintenanceError, match="backup policy"):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert not versions.deleted


@pytest.mark.parametrize("policy,backups", [(False, {"extra": "sha256-" + "0" * 64}),
                                          (False, []), (True, {}), (True, {"extra": "../unsafe"})])
def test_cleanup_rejects_backup_map_inconsistent_with_saved_policy(setup, policy, backups):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    prepare_cleanup(repo, storage, versions, artifacts, backup_deleted_objects=False)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    plan.update(backup_deleted_objects=policy, backups=backups)
    artifacts.write("cleanup-plan.json", encoded(plan))
    with pytest.raises(MaintenanceError, match="backup"):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert not versions.deleted


def test_cleanup_rejects_non_boolean_prepare_backup_policy(setup):
    repo, _, storage, artifacts, versions = stable_setup(setup)
    with pytest.raises(MaintenanceError, match="backup policy"):
        prepare_cleanup(repo, storage, versions, artifacts, backup_deleted_objects="false")


def test_cleanup_cli_rejects_apply_backup_override_before_opening_dependencies(tmp_path, monkeypatch):
    import tools.cleanup_publication_storage as cleanup

    artifacts = tmp_path / "private-cli"
    monkeypatch.setattr("sys.argv", ["cleanup", "--target-namespace", "application", "--artifacts", str(artifacts),
                                     "--plan", str(artifacts / "cleanup-plan.json"),
                                     "--apply", "--no-deleted-object-backup"])

    def forbidden_open(*args):
        raise AssertionError("invalid flags must fail before opening dependencies")

    monkeypatch.setattr(cleanup, "open_dependencies", forbidden_open)
    with pytest.raises(SystemExit) as exc:
        cleanup.main()
    assert exc.value.code == 1


def test_cleanup_cli_prepares_explicit_no_backup_plan_without_obsolete_reads(setup, monkeypatch, capsys):
    import tools.cleanup_publication_storage as cleanup

    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    monkeypatch.setattr("sys.argv", ["cleanup", "--target-namespace", "application", "--artifacts", str(artifacts.path),
                                     "--no-deleted-object-backup"])
    monkeypatch.setattr(cleanup, "open_dependencies", lambda *args: (SimpleNamespace(close=lambda: None),
                                                                   repo, jobs, storage))
    monkeypatch.setattr(cleanup, "VersionInventory", lambda value: versions)

    def forbidden_get(row):
        raise AssertionError("CLI opt-out must not download obsolete bodies")

    monkeypatch.setattr(versions, "get_version", forbidden_get)
    cleanup.main()
    result = json.loads(capsys.readouterr().out)
    assert result["backup_deleted_objects"] is False and result["backup_objects"] == 0
    assert parse_json(artifacts.read("cleanup-plan.json"))["backup_deleted_objects"] is False


@pytest.mark.parametrize("change", ["index", "inventory", "target", "plan", "backup"])
def test_cleanup_apply_rejects_stale_or_tampered_plan(setup, change):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    prepare_cleanup(repo, storage, versions, artifacts)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    if change == "index":
        storage.seed("site-index.json", b"{}")
    elif change == "inventory":
        versions.add("daily/2000-01-01.html", b"new old version", "later")
    elif change == "target":
        plan["target"] = "different"
        artifacts.write("cleanup-plan.json", encoded(plan))
    elif change == "plan":
        plan["deletions"].append(next(r for r in versions.rows if r["key"] == "reports.json" and r["is_latest"]))
        artifacts.write("cleanup-plan.json", encoded(plan))
    else:
        artifacts.write(next(iter(plan["backups"].values())), b"bad backup")
    with pytest.raises(MaintenanceError):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert not versions.deleted


def test_cleanup_partial_failure_records_exact_completed_deletions(setup):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    prepare_cleanup(repo, storage, versions, artifacts)
    versions.fail_after = 1
    with pytest.raises(MaintenanceError, match="partial progress"):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    journal = parse_json(artifacts.read("cleanup-apply.json"))
    assert journal["deleted"] == versions.deleted and len(versions.deleted) == 1
    chunks = [parse_json(line) for line in artifacts.read(journal["journal"]).splitlines()]
    assert [row for chunk in chunks for row in chunk["deleted"]] == versions.deleted


def test_cleanup_uses_bounded_snapshots_and_append_only_chunks(setup, monkeypatch):
    import tools.cleanup_publication_storage as cleanup

    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    for number in range(70):
        versions.add("daily/2024-01-01.html", b"old report", f"obsolete-{number}")
    prepare_cleanup(repo, storage, versions, artifacts)
    expected = parse_json(artifacts.read("cleanup-plan.json"))["deletions"]
    snapshot_calls, summary_writes, renewals = [], [], []
    original_snapshot, original_write = cleanup.snapshot, artifacts.write
    original_renew = jobs.renew

    def snapshot(*args, **kwargs):
        snapshot_calls.append(True)
        return original_snapshot(*args, **kwargs)

    def write(name, body):
        if name == "cleanup-apply.json":
            summary_writes.append(len(body))
        return original_write(name, body)

    def renew(*args, **kwargs):
        renewals.append(True)
        return original_renew(*args, **kwargs)

    monkeypatch.setattr(cleanup, "snapshot", snapshot)
    monkeypatch.setattr(artifacts, "write", write)
    monkeypatch.setattr(jobs, "renew", renew)
    result = apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    chunks_expected = (len(expected) + cleanup.DELETE_BATCH_SIZE - 1) // cleanup.DELETE_BATCH_SIZE
    assert len(snapshot_calls) == 1 + 2 * chunks_expected
    assert len(summary_writes) == 2
    assert len(renewals) < len(expected)
    summary = parse_json(artifacts.read("cleanup-apply.json"))
    chunks = [parse_json(line) for line in artifacts.read(summary["journal"]).splitlines()]
    assert len(chunks) == chunks_expected
    assert all(0 < len(chunk["deleted"]) <= cleanup.DELETE_BATCH_SIZE for chunk in chunks)
    assert [row for chunk in chunks for row in chunk["deleted"]] == expected == versions.deleted
    assert result["deleted_versions"] == len(expected)


def test_cleanup_partial_failure_requires_new_plan_and_keeps_current_versions(setup):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    current_stable = [copy.deepcopy(row) for row in versions.rows
                      if row["is_latest"] and not row["key"].startswith("publication/")]
    prepare_cleanup(repo, storage, versions, artifacts)
    versions.fail_after = 2
    with pytest.raises(MaintenanceError, match="partial progress"):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    with pytest.raises(MaintenanceError, match="stale"):
        apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    versions.fail_after = None
    prepare_cleanup(repo, storage, versions, artifacts)
    apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert all(row in versions.rows for row in current_stable)
    assert len({(row["key"], row["version_id"]) for row in versions.deleted}) == len(versions.deleted)


def test_version_inventory_reuses_http_client_and_closes_it(monkeypatch):
    clients = []
    original_client = httpx.Client

    def client(*args, **kwargs):
        instance = original_client(*args, **kwargs)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "Client", client)
    storage = ObjectStorage("test-publications", token=lambda: "token", transport=httpx.MockTransport(
        lambda request: httpx.Response(204)
    ))
    versions = VersionInventory(storage)
    row = {"key": "daily/2000-01-01.html", "version_id": "old"}
    versions.get_version(row)
    versions.delete_version(row)
    assert len(clients) == 1 and not clients[0].is_closed
    versions.close()
    assert clients[0].is_closed


def test_cleanup_backup_reads_are_bounded_and_artifact_writes_stay_serial(setup, monkeypatch):
    repo, _, storage, artifacts, versions = stable_setup(setup)
    for number in range(30):
        versions.add("daily/2024-01-01.html", b"old report", f"parallel-{number}")
    original_get, original_backup = versions.get_version, artifacts.backup
    active = maximum = 0
    lock = threading.Lock()
    caller = threading.get_ident()

    def get_version(row):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.005)
            return original_get(row)
        finally:
            with lock:
                active -= 1

    def backup(body):
        assert threading.get_ident() == caller
        return original_backup(body)

    monkeypatch.setattr(versions, "get_version", get_version)
    monkeypatch.setattr(artifacts, "backup", backup)
    prepare_cleanup(repo, storage, versions, artifacts)
    assert 1 < maximum <= 4
    assert not versions.deleted


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_parallel_backup_read_failure_prevents_all_deletes(setup, monkeypatch, phase):
    repo, jobs, storage, artifacts, versions = stable_setup(setup)
    if phase == "apply":
        prepare_cleanup(repo, storage, versions, artifacts)

    def failure(row):
        raise MaintenanceError("injected backup read failure")

    monkeypatch.setattr(versions, "get_version", failure)
    with pytest.raises(MaintenanceError, match="backup read failure"):
        if phase == "prepare":
            prepare_cleanup(repo, storage, versions, artifacts)
        else:
            apply_cleanup(repo, jobs, storage, versions, artifacts, "cleanup-plan.json")
    assert not versions.deleted


def test_parallel_reads_do_not_queue_the_whole_inventory():
    consumed = []

    def rows():
        for number in range(1000):
            consumed.append(number)
            yield {"number": number}

    versions = SimpleNamespace(get_version=lambda row: row["number"])
    results = read_versions(rows(), versions)
    try:
        assert next(results) == ({"number": 0}, 0)
        assert len(consumed) == 25
    finally:
        results.close()


def test_cleanup_refuses_inventory_disagreement_and_missing_version_id(setup):
    _, _, storage, _, versions = stable_setup(setup)
    inventory = versions.inventory()
    inventory["versions"] = []
    with pytest.raises(MaintenanceError, match="inventories disagree"):
        candidates(inventory, [])
    assert storage.get("reports.json") is not None


def test_cleanup_retains_stable_null_versions_even_when_noncurrent(setup):
    repo, _, storage, artifacts, versions = stable_setup(setup)
    versions.add("daily/2024-01-01.html", b"prior null page", "null")
    versions.add("reports.json", b"prior null manifest", "null")
    result = prepare_cleanup(repo, storage, versions, artifacts)
    plan = parse_json(artifacts.read("cleanup-plan.json"))
    assert result["retained_stable_null_versions"] == 2
    assert not any(r["version_id"] == "null" for r in plan["deletions"])


def test_cleanup_refuses_missing_reachable_current_report_even_with_consistent_inventories(setup):
    repo, _, storage, artifacts, versions = stable_setup(setup)
    versions.rows = [r for r in versions.rows if r["key"] != "daily/2024-01-01.html"]
    with pytest.raises(MaintenanceError, match="reachable.*missing"):
        prepare_cleanup(repo, storage, versions, artifacts)
    assert not versions.deleted


@pytest.mark.parametrize("damage", ["missing", "etag"])
def test_cleanup_refuses_stable_report_without_verified_committed_pair(setup, damage):
    repo, _, storage, artifacts, versions = stable_setup(setup)
    if damage == "missing":
        del storage.objects["daily/2024-01-01.json"]
    else:
        storage.seed("daily/2024-01-01.json", b"uncommitted report bytes")
    with pytest.raises(MaintenanceError):
        prepare_cleanup(repo, storage, versions, artifacts)
    assert not versions.deleted


def xml_page(prefix, rows, *, versions=False, truncated=False, marker=""):
    root = "ListVersionsResult" if versions else "ListBucketResult"
    body = f'<{root} xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Prefix>{prefix}/</Prefix>'
    body += f'<IsTruncated>{str(truncated).lower()}</IsTruncated>'
    for key, version in rows:
        tag = "Version" if versions else "Contents"
        body += f'<{tag}><Key>{escape(key)}</Key><ETag>"etag"</ETag><Size>3</Size>'
        if versions:
            body += f'<VersionId>{version}</VersionId><IsLatest>true</IsLatest>'
        body += f'</{tag}>'
    if truncated:
        if versions:
            body += f'<NextKeyMarker>{escape(marker)}</NextKeyMarker><NextVersionIdMarker>v1</NextVersionIdMarker>'
        else:
            body += f'<NextContinuationToken>{marker}</NextContinuationToken>'
    return (body + f'</{root}>').encode()


@pytest.mark.parametrize("versions", [False, True])
def test_s3_xml_inventory_pagination_is_scoped_and_bearer_authenticated(versions):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.url.params["prefix"] == "private/publication/"
        marker = "key-marker" if versions else "continuation-token"
        key = "reports.json" if marker not in request.url.params else "latest.html"
        page = xml_page("private/publication", [("private/publication/" + key, "v1")],
                        versions=versions, truncated=marker not in request.url.params,
                        marker="private/publication/reports.json" if versions else "opaque-token")
        return httpx.Response(200, content=page)

    storage = ObjectStorage("test-publications", "private/publication", token=lambda: "test-token",
                            transport=httpx.MockTransport(handler))
    rows = VersionInventory(storage).listing(versions=versions)
    assert len(rows) == 2 and len(requests) == 2
    assert requests[1].url.params["key-marker" if versions else "continuation-token"] == (
        "private/publication/reports.json" if versions else "opaque-token")


@pytest.mark.parametrize("body", [
    b'<!DOCTYPE x [<!ENTITY x "boom">]><ListBucketResult/>',
    xml_page("private/publication", [("private/publication-other/reports.json", "")]),
    xml_page("private/publication", [], truncated=True),
    b"<bad>",
])
def test_s3_xml_inventory_rejects_unsafe_pages(body):
    storage = ObjectStorage("test-publications", "private/publication", token=lambda: "test-token",
                            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    with pytest.raises(MaintenanceError):
        VersionInventory(storage).listing()


def test_s3_inventory_response_is_bounded(monkeypatch):
    monkeypatch.setattr("tools.cleanup_publication_storage.MAX_LIST_BYTES", 10)
    storage = ObjectStorage("test-publications", "private/publication", token=lambda: "test-token",
                            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 11)))
    with pytest.raises(MaintenanceError, match="byte limit"):
        VersionInventory(storage).listing()


def test_s3_delete_uses_exact_version_query_without_prefix_delete():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(204)

    storage = ObjectStorage("test-publications", "private/publication", token=lambda: "test-token",
                            transport=httpx.MockTransport(handler))
    versions = VersionInventory(storage)
    versions.delete_version({"key": "daily/2000-01-01.html", "version_id": "opaque+/version="})
    assert requests[0].method == "DELETE"
    assert requests[0].url.path.endswith("/private/publication/daily/2000-01-01.html")
    assert requests[0].url.params["versionId"] == "opaque+/version="
    with pytest.raises(MaintenanceError):
        versions.delete_version({"key": "reports.json", "version_id": ""})

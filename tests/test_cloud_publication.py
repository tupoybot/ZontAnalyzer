"""Cloud publication projects only committed YDB state into private Object Storage."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application import publication
from zont_analyzer.cloud.object_storage import ObjectStorage, StorageError, StoredObject
from zont_analyzer.cloud.publication import SITE_INDEX_KEY, CloudPublication
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.runtime import Runtime


class MemoryStorage:
    bucket = "private-publication-bucket"
    prefix = "test"

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.fail_category = ""

    def get(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def head(self, key: str) -> str:
        return self.objects[key].etag if key in self.objects else ""

    def put(self, key: str, body: bytes, content_type: str) -> str:
        if self.fail_category and f"/{self.fail_category}/" in key:
            self.fail_category = ""
            raise OSError("interrupted upload")
        etag = hashlib.sha256(body).hexdigest()
        existing = self.objects.get(key)
        if existing and existing.body != body:
            raise AssertionError("immutable key overwritten")
        self.objects[key] = StoredObject(body, content_type, etag)
        return etag

    def compare_and_swap(self, key: str, body: bytes, content_type: str, *, expected_etag: str | None) -> bool:
        current = self.objects.get(key)
        if expected_etag != (current.etag if current else None):
            return False
        self.objects[key] = StoredObject(body, content_type, hashlib.sha256(body).hexdigest())
        return True


def _runtime(tmp_path: Path) -> Runtime:
    loaded = LoadedConfig(config=AppConfig(), secrets=Secrets(), config_path=None,
                          data_dir=tmp_path, sources={})
    return Runtime(loaded, make_database(tmp_path))


def _activate(monkeypatch: pytest.MonkeyPatch, storage: MemoryStorage) -> None:
    monkeypatch.setenv("CLOUD_PUBLICATION_BUCKET", storage.bucket)
    monkeypatch.setattr(ObjectStorage, "from_environment", classmethod(lambda cls: storage))
    monkeypatch.setattr(publication, "reports_directory", lambda *_: pytest.fail("cloud used local publication"))


def _active(runtime: Runtime, storage: MemoryStorage) -> tuple[dict, dict, str]:
    meta = PublicationRepository(runtime.db.storage).load_meta()
    manifest = json.loads(storage.objects[meta["manifest_key"]].body)
    return meta, manifest, storage.objects[meta["latest_key"]].body.decode()


@pytest.mark.ydb
def test_cloud_archive_latest_and_idle_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    daily = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    weekly = runtime.analysis(no_ai=True).analyze_week(2026, 31, use_ai=False)
    changed = daily.model_copy(update={"summary": '<script>alert("unsafe")</script>',
                                       "generated_at": daily.generated_at + timedelta(seconds=1)})
    runtime.db.save_report(changed, "changed")
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    first = publication.publish_reports(runtime)
    assert first["rendered_reports"] == 2
    meta, manifest, latest = _active(runtime, storage)
    assert json.loads(storage.objects[SITE_INDEX_KEY].body) == {
        **manifest, "manifest_key": meta["manifest_key"], "latest_key": meta["latest_key"],
    }
    entries = {entry["href"]: entry for entry in manifest["reports"]}
    assert set(entries) == {"daily/2026-08-01.html", "weekly/2026-07-27.html"}
    assert all(storage.get(entry["html_key"]) and storage.get(entry["json_key"])
               for entry in entries.values())
    assert storage.objects[entries["daily/2026-08-01.html"]["json_key"]].content_type.startswith("application/json")
    assert '&lt;script&gt;alert(' in latest
    assert '<script>alert(' not in latest
    assert changed.id in latest and weekly.id not in latest
    assert "latest.html" in latest
    keys = set(storage.objects)
    monkeypatch.setattr(PublicationRepository, "load", lambda *_: pytest.fail("idle full index load"))
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    assert storage.objects.keys() == keys
    assert PublicationRepository(runtime.db.storage).load_meta()["manifest_key"] == meta["manifest_key"]


@pytest.mark.ydb
def test_interrupted_latest_upload_keeps_old_pointer_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    publication.publish_reports(runtime)
    old_meta, _, old_latest = _active(runtime, storage)
    old_index = storage.objects[SITE_INDEX_KEY]
    newer = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    storage.fail_category = "latest"
    with pytest.raises(OSError, match="interrupted upload"):
        publication.publish_reports(runtime)
    failed_meta, _, failed_latest = _active(runtime, storage)
    assert failed_meta["manifest_key"] == old_meta["manifest_key"]
    assert failed_meta["latest_key"] == old_meta["latest_key"]
    assert failed_latest == old_latest
    assert storage.objects[SITE_INDEX_KEY] == old_index
    assert any("/manifests/" in key and key != old_meta["manifest_key"] for key in storage.objects)
    result = publication.publish_reports(runtime)
    meta, manifest, latest = _active(runtime, storage)
    assert result["pending_reports"] == 0
    assert meta["manifest_key"] != old_meta["manifest_key"]
    assert newer.id in latest
    assert len(manifest["reports"]) == 2


@pytest.mark.ydb
def test_replaced_lease_cannot_activate_uploaded_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    publication.publish_reports(runtime)
    old_meta, _, old_latest = _active(runtime, storage)
    old_index = storage.objects[SITE_INDEX_KEY]
    newer = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    original_save = PublicationRepository.save
    replacement_attempt = 0

    def replace_before_save(self, *args, **kwargs):
        nonlocal replacement_attempt
        lease = runtime.db.jobs.get("publication")
        assert lease and runtime.db.jobs.release("publication", lease.owner, lease.attempt)
        replacement = runtime.db.jobs.acquire("publication", "replacement", 3600)
        assert replacement
        replacement_attempt = replacement.attempt
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(PublicationRepository, "save", replace_before_save)
    with pytest.raises(RuntimeError, match="lost its YDB checkpoint or lease"):
        publication.publish_reports(runtime)
    meta, _, latest = _active(runtime, storage)
    assert meta["manifest_key"] == old_meta["manifest_key"]
    assert meta["latest_key"] == old_meta["latest_key"]
    assert latest == old_latest
    assert storage.objects[SITE_INDEX_KEY] == old_index
    monkeypatch.setattr(PublicationRepository, "save", original_save)
    assert runtime.db.jobs.release("publication", "replacement", replacement_attempt)
    publication.publish_reports(runtime)
    assert newer.id in _active(runtime, storage)[2]


def test_content_addressed_put_is_idempotent() -> None:
    storage = MemoryStorage()
    publisher = CloudPublication(storage)  # type: ignore[arg-type]
    first = publisher.put("reports", "html", b"<p>report</p>", "text/html")
    second = publisher.put("reports", "html", b"<p>report</p>", "text/html")
    assert first == second
    assert len(storage.objects) == 1


@pytest.mark.ydb
def test_new_storage_target_rebuilds_all_links_in_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    analysis = runtime.analysis(no_ai=True)
    for day in range(9):
        analysis.analyze_daily(date(2026, 8, 1) + timedelta(days=day), use_ai=False)
    first_storage = MemoryStorage()
    _activate(monkeypatch, first_storage)
    first = publication.publish_reports(runtime, batch_size=100)
    assert first["rendered_reports"] == 8
    assert first["pending_reports"] == 1
    assert publication.publish_reports(runtime)["pending_reports"] == 0

    replacement = MemoryStorage()
    replacement.bucket = "private-publication-bucket-2"
    _activate(monkeypatch, replacement)
    moved = publication.publish_reports(runtime, batch_size=100)
    assert moved["rendered_reports"] == 8
    assert moved["pending_reports"] == 1
    assert publication.publish_reports(runtime)["pending_reports"] == 0
    _, manifest, _ = _active(runtime, replacement)
    assert len(manifest["reports"]) == 9
    assert all(entry["html_key"] in replacement.objects and entry["json_key"] in replacement.objects
               for entry in manifest["reports"])


@pytest.mark.ydb
def test_bounded_audit_restores_missing_archived_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    publication.publish_reports(runtime)
    old_meta, manifest, _ = _active(runtime, storage)
    missing = manifest["reports"][0]["html_key"]
    del storage.objects[missing]
    result = publication.publish_reports(runtime)
    assert result["rendered_reports"] == 1
    assert missing in storage.objects
    assert PublicationRepository(runtime.db.storage).load_meta()["manifest_key"] != old_meta["manifest_key"]


@pytest.mark.ydb
def test_rebuild_without_daily_clears_active_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    weekly = runtime.analysis(no_ai=True).analyze_week(2026, 31, use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    publication.publish_reports(runtime)
    assert PublicationRepository(runtime.db.storage).load_meta()["latest_key"]
    monkeypatch.setattr(PublicationRepository, "canonical_reports", lambda *_: [weekly])
    publication.publish_reports(runtime, rebuild=True)
    meta = PublicationRepository(runtime.db.storage).load_meta()
    assert meta["latest_key"] == ""
    assert len(json.loads(storage.objects[meta["manifest_key"]].body)["reports"]) == 1
    assert json.loads(storage.objects[SITE_INDEX_KEY].body)["latest_key"] == ""


@pytest.mark.ydb
def test_index_failure_after_ydb_commit_is_repaired_by_idle_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    storage = MemoryStorage()
    _activate(monkeypatch, storage)
    publication.publish_reports(runtime)
    old_index = storage.objects[SITE_INDEX_KEY]
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    original_swap = storage.compare_and_swap

    def unavailable(*args, **kwargs):
        raise OSError("index upload interrupted")

    monkeypatch.setattr(storage, "compare_and_swap", unavailable)
    with pytest.raises(OSError, match="index upload interrupted"):
        publication.publish_reports(runtime)
    committed = PublicationRepository(runtime.db.storage).load_meta()
    assert committed["manifest_key"] != json.loads(old_index.body)["manifest_key"]
    assert not PublicationRepository(runtime.db.storage).has_dirty()
    assert storage.objects[SITE_INDEX_KEY] == old_index

    monkeypatch.setattr(storage, "compare_and_swap", original_swap)
    monkeypatch.setattr(PublicationRepository, "load", lambda *_: pytest.fail("idle full index load"))
    monkeypatch.setattr(publication, "render_html", lambda *args, **kwargs: pytest.fail("idle render"))
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    index = json.loads(storage.objects[SITE_INDEX_KEY].body)
    assert index["manifest_key"] == committed["manifest_key"]
    assert index["latest_key"] == committed["latest_key"]
    assert len(index["reports"]) == 2
    del storage.objects[SITE_INDEX_KEY]
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    assert json.loads(storage.objects[SITE_INDEX_KEY].body) == index


class CommittedPublication:
    def __init__(self, publisher: CloudPublication) -> None:
        self.meta = {"storage_target": publisher.storage_target}
        self.loads = 0

    def load_meta(self) -> dict[str, str]:
        self.loads += 1
        return dict(self.meta)


def _committed_snapshot(storage: MemoryStorage, revision: str) -> dict[str, str]:
    manifest_key = f"publication/manifests/{revision}.json"
    latest_key = f"publication/latest/{revision}.html"
    storage.put(latest_key, revision.encode(), "text/html")
    storage.put(manifest_key, json.dumps({
        "version": 1, "updated_at": revision, "reports": [{"href": f"daily/{revision}.html"}],
    }).encode(), "application/json")
    return {"manifest_key": manifest_key, "latest_key": latest_key}


@pytest.mark.parametrize("existing_index", [False, True])
def test_stale_index_writer_retries_fresh_committed_snapshot(
    monkeypatch: pytest.MonkeyPatch, existing_index: bool,
) -> None:
    storage = MemoryStorage()
    publisher = CloudPublication(storage)  # type: ignore[arg-type]
    repository = CommittedPublication(publisher)
    if existing_index:
        repository.meta.update(_committed_snapshot(storage, "initial"))
        publisher.update_site_index(repository)  # type: ignore[arg-type]
    repository.meta.update(_committed_snapshot(storage, "older"))
    newer = _committed_snapshot(storage, "newer")
    original_swap = storage.compare_and_swap
    swapped = False
    rejected = []

    def concurrent_swap(key, body, content_type, *, expected_etag):
        nonlocal swapped
        if not swapped:
            swapped = True
            repository.meta.update(newer)
            publisher.update_site_index(repository)
            accepted = original_swap(key, body, content_type, expected_etag=expected_etag)
            rejected.append(not accepted)
            return accepted
        return original_swap(key, body, content_type, expected_etag=expected_etag)

    monkeypatch.setattr(storage, "compare_and_swap", concurrent_swap)
    publisher.update_site_index(repository)  # type: ignore[arg-type]
    assert rejected == [True]
    assert json.loads(storage.objects[SITE_INDEX_KEY].body)["manifest_key"] == newer["manifest_key"]


def test_index_get_precedes_metadata_and_conflicts_have_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = MemoryStorage()
    publisher = CloudPublication(storage)  # type: ignore[arg-type]
    repository = CommittedPublication(publisher)
    repository.meta.update(_committed_snapshot(storage, "committed"))
    original_get, original_load = storage.get, repository.load_meta
    order = []

    def get(key):
        if key == SITE_INDEX_KEY:
            order.append("index")
        return original_get(key)

    def load():
        order.append("meta")
        return original_load()

    monkeypatch.setattr(storage, "get", get)
    monkeypatch.setattr(repository, "load_meta", load)
    monkeypatch.setattr(storage, "compare_and_swap", lambda *args, **kwargs: False)
    with pytest.raises(StorageError, match="conflicted"):
        publisher.update_site_index(repository)  # type: ignore[arg-type]
    assert order == ["index", "meta"] * 3
    assert SITE_INDEX_KEY not in storage.objects


def test_index_refuses_pointers_from_another_storage_target() -> None:
    storage = MemoryStorage()
    publisher = CloudPublication(storage)  # type: ignore[arg-type]
    repository = CommittedPublication(publisher)
    repository.meta.update(_committed_snapshot(storage, "committed"))
    repository.meta["storage_target"] = "another-target"
    with pytest.raises(StorageError, match="target changed"):
        publisher.update_site_index(repository)  # type: ignore[arg-type]
    assert SITE_INDEX_KEY not in storage.objects

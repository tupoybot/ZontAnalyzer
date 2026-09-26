"""Cloud publication uses YDB as the only active pointer and index."""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application import publication
from zont_analyzer.cloud.object_storage import ObjectStorage, StoredObject
from zont_analyzer.cloud.publication import CloudPublication
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
    newer = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 2), use_ai=False)
    storage.fail_category = "latest"
    with pytest.raises(OSError, match="interrupted upload"):
        publication.publish_reports(runtime)
    failed_meta, _, failed_latest = _active(runtime, storage)
    assert failed_meta["manifest_key"] == old_meta["manifest_key"]
    assert failed_meta["latest_key"] == old_meta["latest_key"]
    assert failed_latest == old_latest
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

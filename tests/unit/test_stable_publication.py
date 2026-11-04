from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from zont_analyzer.cloud.object_storage import StorageError, StoredObject
from zont_analyzer.cloud.publication import CloudPublication
from zont_analyzer.domain import Report


class Storage:
    bucket = "test-bucket"
    prefix = "reports"

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.fail_key = ""
        self.after_get: Any = None
        self.before_put: Any = None

    def get(self, key: str) -> StoredObject | None:
        current = self.objects.get(key)
        if self.after_get:
            callback, self.after_get = self.after_get, None
            callback()
        return current

    def conditional_put(self, key: str, body: bytes, content_type: str, *, expected_etag: str | None) -> str | None:
        if self.before_put:
            callback, self.before_put = self.before_put, None
            callback()
        if key == self.fail_key:
            raise StorageError("interrupted upload")
        current = self.objects.get(key)
        if expected_etag != (current.etag if current else None):
            return None
        etag = hashlib.sha256(body).hexdigest()
        self.objects[key] = StoredObject(body, content_type, etag)
        return etag


def publisher(storage: Storage) -> CloudPublication:
    return CloudPublication(storage)  # type: ignore[arg-type]


def noop() -> None:
    pass


def report() -> Report:
    return Report.model_validate({
        "id": "daily-report", "kind": "daily", "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-08-02T00:00:00Z", "generated_at": "2026-08-02T00:00:01Z",
        "summary": "Report", "context": {"period": {"complete": True}},
        "quality": {"score": 1, "coverage_pct": 100, "max_gap_seconds": 0, "stuck_pct": 0,
                    "implausible_jumps": 0, "sample_count": 1},
    })


def test_stable_report_pair_preserves_canonical_data_and_changes_generation() -> None:
    storage = Storage()
    cloud = publisher(storage)
    canonical = report()
    before = canonical.model_dump(mode="json")
    entry, html_etag, json_etag = cloud.report(canonical, "daily/2026-08-01.html", "<p>Report</p>",
                                              datetime.now(UTC), fence=noop)
    assert entry["html_key"] == "daily/2026-08-01.html"
    assert entry["json_key"] == "daily/2026-08-01.json"
    payload = json.loads(storage.objects[entry["json_key"]].body)
    assert payload["context"].pop("_publication")["generation"]
    assert payload == before == canonical.model_dump(mode="json")
    _, html_again, json_again = cloud.report(canonical, entry["href"], "<p>Report</p>",
                                            datetime.now(UTC), fence=noop)
    assert html_again != html_etag and json_again != json_etag
    assert len(storage.objects) == 2


def test_lost_lease_after_get_prevents_write() -> None:
    storage = Storage()
    owned = True

    def lose_lease() -> None:
        nonlocal owned
        owned = False

    def fence() -> None:
        if not owned:
            raise RuntimeError("lost lease")

    storage.after_get = lose_lease
    with pytest.raises(RuntimeError, match="lost lease"):
        publisher(storage).latest("old", fence=fence)
    assert not storage.objects


@pytest.mark.parametrize("same_payload", [False, True])
def test_paused_writer_cannot_overwrite_replacement_even_for_identical_payload(same_payload: bool) -> None:
    storage = Storage()
    cloud = publisher(storage)
    accepted = "old" if same_payload else "new"
    cloud.latest(accepted if same_payload else "initial", fence=noop)
    storage.before_put = lambda: cloud.latest(accepted, fence=noop)
    with pytest.raises(StorageError, match="conflicted"):
        cloud.latest("old", fence=noop)
    assert storage.objects["latest.html"].body.startswith(accepted.encode())


def test_json_failure_leaves_html_written_and_retry_repairs_pair() -> None:
    storage = Storage()
    cloud = publisher(storage)
    storage.fail_key = "daily/2026-08-01.json"
    with pytest.raises(StorageError, match="interrupted"):
        cloud.report(report(), "daily/2026-08-01.html", "complete HTML", datetime.now(UTC), fence=noop)
    assert set(storage.objects) == {"daily/2026-08-01.html"}
    first_html = storage.objects["daily/2026-08-01.html"]
    storage.fail_key = ""
    cloud.report(report(), "daily/2026-08-01.html", "complete HTML", datetime.now(UTC), fence=noop)
    assert len(storage.objects) == 2
    assert storage.objects["daily/2026-08-01.html"].etag != first_html.etag


def test_manifest_uses_stable_key_and_fresh_generation_on_each_selected_write() -> None:
    storage = Storage()
    cloud = publisher(storage)
    now = datetime.now(UTC)
    key, etag = cloud.manifest([], now, fence=noop)
    first = json.loads(storage.objects[key].body)
    next_key, next_etag = cloud.manifest([], now, fence=noop)
    assert key == next_key == "reports.json"
    assert etag != next_etag
    assert first["generation"] != json.loads(storage.objects[key].body)["generation"]


def test_index_rejects_uncommitted_stable_manifest() -> None:
    storage = Storage()
    cloud = publisher(storage)
    key, etag = cloud.manifest([], datetime.now(UTC), fence=noop)
    cloud.manifest([], datetime.now(UTC), fence=noop)

    class Repository:
        def load_meta(self) -> dict[str, str]:
            return {"storage_target": cloud.storage_target, "manifest_key": key, "manifest_etag": etag}

    with pytest.raises(StorageError, match="differs from committed"):
        cloud.update_site_index(Repository())  # type: ignore[arg-type]

"""Immutable report artifacts and a browser index derived from committed YDB state."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.cloud.object_storage import StorageError, valid_key
from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.publication import PublicationRepository
    from zont_analyzer.cloud.object_storage import ObjectStorage

SITE_INDEX_KEY = "site-index.json"
SITE_INDEX_ATTEMPTS = 3


class CloudPublication:
    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

    @property
    def storage_target(self) -> str:
        target = json.dumps((self.storage.bucket, self.storage.prefix), sort_keys=True).encode()
        return hashlib.sha256(target).hexdigest()

    def update_site_index(self, repository: PublicationRepository) -> None:
        """Project committed YDB pointers into the stable private browser index.

        Read the object's ETag before the committed metadata on every attempt.
        A publisher paused after reading older metadata cannot replace an index
        already written by a newer publisher. A failed CAS restarts both reads.
        """
        for _ in range(SITE_INDEX_ATTEMPTS):
            current = self.storage.get(SITE_INDEX_KEY)
            if current is not None and not current.etag:
                raise StorageError("browser index has no ETag")
            meta = repository.load_meta()
            if meta.get("storage_target") != self.storage_target:
                raise StorageError("committed publication target changed")
            manifest_key = meta.get("manifest_key", "")
            latest_key = meta.get("latest_key", "")
            if not valid_key(manifest_key) or (latest_key and not valid_key(latest_key)):
                raise StorageError("invalid committed publication key")
            manifest_object = self.storage.get(manifest_key)
            if manifest_object is None:
                raise StorageError("committed publication manifest missing")
            try:
                manifest = json.loads(manifest_object.body)
                if (not isinstance(manifest, dict) or manifest.get("version") != 1
                        or not isinstance(manifest.get("reports"), list)
                        or not isinstance(manifest.get("updated_at"), str)):
                    raise ValueError("invalid manifest")
            except (ValueError, UnicodeError):
                raise StorageError("invalid committed publication manifest") from None
            body = json.dumps({
                "version": 1, "updated_at": manifest["updated_at"], "manifest_key": manifest_key,
                "latest_key": latest_key, "reports": manifest["reports"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            if current is not None and current.body == body:
                return
            if self.storage.compare_and_swap(
                SITE_INDEX_KEY, body, "application/json; charset=utf-8",
                expected_etag=current.etag if current else None,
            ):
                return
        raise StorageError("browser index update conflicted; retry publication")

    def put(self, category: str, suffix: str, body: bytes, content_type: str) -> tuple[str, str]:
        """The digest makes a key immutable and makes an interrupted retry safe."""
        digest = hashlib.sha256(body).hexdigest()
        key = f"publication/{category}/{digest}.{suffix}"
        etag = self.storage.head(key)
        if not etag:
            etag = self.storage.put(key, body, content_type) or self.storage.head(key)
        if not etag:
            raise RuntimeError("published object has no ETag")
        return key, etag

    def report(self, report: Report, href: str, html: str, now: datetime) -> tuple[dict[str, Any], str, str]:
        html_key, html_etag = self.put("reports", "html", html.encode("utf-8"), "text/html; charset=utf-8")
        json_key, json_etag = self.put(
            "reports", "json", (report.model_dump_json(indent=2) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )
        zone = ZoneInfo(report.timezone)
        entry = {
            "kind": report.kind,
            "start": report.period_start.astimezone(zone).date().isoformat(),
            "end": report.period_end.astimezone(zone).date().isoformat(),
            "href": href,
            "published_at": now.astimezone(UTC).isoformat(),
            "timezone": report.timezone,
            "complete": report.context.get("period", {}).get("complete", True),
            "nominal_end": report.context.get("period", {}).get("end", report.period_end.isoformat()),
            "season": report.context.get("period", {}).get("season"),
            "html_key": html_key,
            "json_key": json_key,
        }
        return entry, html_etag, json_etag

    def manifest(self, entries: list[dict[str, Any]], now: datetime) -> str:
        body = json.dumps({
            "version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries,
        }, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        return self.put("manifests", "json", body, "application/json; charset=utf-8")[0]

    def latest(self, html: str) -> str:
        return self.put("latest", "html", html.encode("utf-8"), "text/html; charset=utf-8")[0]

    def valid_entry(self, entry: dict[str, Any], html_etag: str, json_etag: str) -> bool:
        return bool(entry.get("html_key") and entry.get("json_key") and html_etag and json_etag
                    and self.storage.head(entry["html_key"]) == html_etag
                    and self.storage.head(entry["json_key"]) == json_etag)

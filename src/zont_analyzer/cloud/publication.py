"""Fenced period artifacts and a browser index derived from committed YDB state."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from zont_analyzer.cloud.object_storage import StorageError, valid_key
from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.publication import PublicationRepository
    from zont_analyzer.cloud.object_storage import ObjectStorage

SITE_INDEX_KEY = "site-index.json"
SITE_INDEX_ATTEMPTS = 3
REPORT_HREF = re.compile(r"(?:daily|weekly|monthly|seasonal)/\d{4}-\d{2}-\d{2}\.html\Z")


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
            if meta.get("manifest_etag") and manifest_object.etag != meta["manifest_etag"]:
                raise StorageError("publication manifest differs from committed version")
            try:
                manifest = json.loads(manifest_object.body)
                if (not isinstance(manifest, dict) or manifest.get("version") != 1
                        or not isinstance(manifest.get("reports"), list)
                        or not isinstance(manifest.get("updated_at"), str)):
                    raise ValueError("invalid manifest")
            except (ValueError, UnicodeError):
                raise StorageError("invalid committed publication manifest") from None
            index = {
                "version": 1, "updated_at": manifest["updated_at"], "manifest_key": manifest_key,
                "latest_key": latest_key, "reports": manifest["reports"],
            }
            if "generation" in manifest:
                index["generation"] = manifest["generation"]
            body = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
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

    def _replace(self, key: str, body: bytes, content_type: str, fence: Callable[[], None]) -> tuple[str, str]:
        """Never retry a prepared payload against a newly read object version."""
        current = self.storage.get(key)
        if current is not None and not current.etag:
            raise StorageError("publication object has no ETag")
        fence()
        etag = self.storage.conditional_put(key, body, content_type,
                                            expected_etag=current.etag if current else None)
        if etag is None:
            raise StorageError("publication object update conflicted; retry publication")
        return key, etag

    def _html(self, key: str, html: str, fence: Callable[[], None]) -> tuple[str, str]:
        body = (html + f"\n<!-- zont-publication-generation:{uuid4()} -->\n").encode("utf-8")
        return self._replace(key, body, "text/html; charset=utf-8", fence)

    def report(self, report: Report, href: str, html: str, now: datetime, *,
               fence: Callable[[], None]) -> tuple[dict[str, Any], str, str]:
        if not REPORT_HREF.fullmatch(href):
            raise ValueError("invalid report publication path")
        html_key, html_etag = self._html(href, html, fence)
        payload = report.model_dump(mode="json")
        payload["context"]["_publication"] = {"generation": str(uuid4())}
        json_key, json_etag = self._replace(
            href.removesuffix(".html") + ".json",
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            "application/json; charset=utf-8", fence,
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

    def manifest(self, entries: list[dict[str, Any]], now: datetime, *,
                 fence: Callable[[], None]) -> tuple[str, str]:
        body = json.dumps({
            "version": 1, "updated_at": now.astimezone(UTC).isoformat(), "reports": entries,
            "generation": str(uuid4()),
        }, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        return self._replace("reports.json", body, "application/json; charset=utf-8", fence)

    def latest(self, html: str, *, fence: Callable[[], None]) -> tuple[str, str]:
        return self._html("latest.html", html, fence)

    def valid_entry(self, entry: dict[str, Any], html_etag: str, json_etag: str) -> bool:
        return bool(entry.get("html_key") and entry.get("json_key") and html_etag and json_etag
                    and self.storage.head(entry["html_key"]) == html_etag
                    and self.storage.head(entry["json_key"]) == json_etag)

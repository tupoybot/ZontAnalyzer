"""Immutable Object Storage artifacts for the YDB publication index."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report

if TYPE_CHECKING:
    from zont_analyzer.cloud.object_storage import ObjectStorage


class CloudPublication:
    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

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

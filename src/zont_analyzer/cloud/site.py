"""Resolve the committed presentation snapshot after the HTTP perimeter authenticates."""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.cloud.object_storage import ObjectStorage, StorageError, valid_key

if TYPE_CHECKING:
    from zont_analyzer.runtime import Runtime

_ARCHIVE_PATH = re.compile(r"(?:daily|weekly|monthly|seasonal)/\d{4}-\d{2}-\d{2}\.(?:html|json)\Z")


def serve(runtime: Runtime, path: str) -> tuple[int, bytes, str]:
    """GET never renders, recalculates, saves user data or invokes an AI provider."""
    parsed = urlsplit(path)
    name = parsed.path.removeprefix("/")
    if name == "za":
        name = ""
    elif name.startswith("za/"):
        name = name[3:]
    if name in {"", "index.html"}:
        name = "latest.html"
    if parsed.scheme or parsed.netloc or not (name in {"latest.html", "reports.json"} or _ARCHIVE_PATH.fullmatch(name)):
        return 404, b'{"error":"not_found"}', "application/json"
    storage = ObjectStorage.from_environment()
    # Read both pointers together: publication commits them in the same YDB transaction.
    meta = PublicationRepository(runtime.db.storage).load_meta()
    manifest_key = meta.get("manifest_key", "")
    if not manifest_key:
        return 404, b'{"error":"not_published"}', "application/json"
    if name == "latest.html":
        key = meta.get("latest_key", "")
    elif name == "reports.json":
        key = manifest_key
    else:
        manifest = storage.get(manifest_key)
        if manifest is None:
            raise StorageError("committed manifest missing")
        try:
            entries = json.loads(manifest.body)["reports"]
            html_name = name.removesuffix(".json") + ".html" if name.endswith(".json") else name
            entry = next((row for row in entries if row["href"] == html_name), None)
            key = entry["json_key" if name.endswith(".json") else "html_key"] if entry else ""
        except (ValueError, KeyError, TypeError):
            raise StorageError("invalid committed manifest") from None
    if not key:
        return 404, b'{"error":"not_found"}', "application/json"
    if not valid_key(key):
        raise StorageError("invalid committed object key")
    value = storage.get(key)
    if value is None:
        raise StorageError("committed publication object missing")
    content_type = "text/html; charset=utf-8" if name.endswith(".html") else "application/json; charset=utf-8"
    return 200, value.body, content_type

"""Plan and separately apply exact-version cleanup under one publication prefix.

The default only inventories storage and writes an owner-only --plan and byte
backups under --artifacts, outside the checkout. --apply --plan revalidates the
complete inventory, committed YDB pointers and browser index under the publication
lease. Every delete names an exact VersionId; missing version support fails closed.
All current period report pairs, including archive dates absent from the manifest,
are retained. Stable null versions are retained because a suspended bucket can
reuse their identifiers. Stop legacy writers before cleanup. There is no age rule
and no recursive or prefix delete.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from itertools import islice
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from tools.publication_storage_admin import (
    LEGACY_KEY,
    MaintenanceError,
    PrivateArtifacts,
    allowed_key,
    committed_objects,
    digest,
    encoded,
    fingerprint,
    open_dependencies,
    parse_json,
    publication_lease,
    required_object,
    storage_target,
    validate_manifest,
)
from zont_analyzer.cloud.object_storage import MAX_OBJECT_BYTES, StoredObject, valid_key

MAX_LIST_BYTES = 4 * 1024 * 1024
MAX_PAGES = 10_000
MAX_ENTRIES = 1_000_000
DELETE_BATCH_SIZE = 25
LEASE_RENEW_INTERVAL = 30
READ_WORKERS = 4


class VersionInventory:
    """Bearer-authenticated S3 XML listing with explicit continuation markers."""

    def __init__(self, storage):
        self.storage = storage
        self.client = httpx.Client(transport=storage.transport, trust_env=False,
                                   follow_redirects=False, timeout=10)

    def close(self):
        self.client.close()

    def request(self, method, *, key=None, params=None):
        storage = self.storage
        if key is not None and not allowed_key(key):
            raise MaintenanceError("version request outside publication namespace")
        url = "https://storage.yandexcloud.net/" + storage.bucket
        if key is not None:
            url += "/" + quote(storage.prefix + "/" + key, safe="/")
        headers = {"Authorization": "Bearer " + storage.token(), "Accept-Encoding": "identity"}
        limit = MAX_OBJECT_BYTES if key is not None else MAX_LIST_BYTES
        try:
            with self.client.stream(method, url, params=params, headers=headers) as response:
                if response.status_code not in {200, 204}:
                    raise MaintenanceError("publication version request failed")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise MaintenanceError("publication version response exceeds byte limit")
                return StoredObject(bytes(data), response.headers.get("content-type", "application/octet-stream"),
                                    response.headers.get("etag", ""))
        except httpx.HTTPError:
            raise MaintenanceError("publication version request failed") from None

    def _relative(self, key):
        prefix = self.storage.prefix + "/"
        if not key.startswith(prefix) or not valid_key(key):
            raise MaintenanceError("listing returned a key outside exact publication prefix")
        return key[len(prefix):]

    def listing(self, *, versions=False, fence=lambda: None):
        params = {"prefix": self.storage.prefix + "/", "max-keys": "1000"}
        params.update({"versions": ""} if versions else {"list-type": "2"})
        rows, markers = [], set()
        for _ in range(MAX_PAGES):
            fence()
            raw = self.request("GET", params=params).body
            # ElementTree does not resolve external entities; reject declarations
            # too, so entity expansion and alternate parser behavior are excluded.
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise MaintenanceError("unsafe publication listing XML")
            try:
                root = ET.fromstring(raw)
                for node in root.iter():
                    node.tag = node.tag.rsplit("}", 1)[-1]
                expected_root = "ListVersionsResult" if versions else "ListBucketResult"
                if root.tag != expected_root:
                    raise ValueError("unexpected listing")
                if root.findtext("Prefix") != self.storage.prefix + "/":
                    raise ValueError("listing prefix mismatch")
                truncated = root.findtext("IsTruncated")
                if truncated not in {"true", "false"}:
                    raise ValueError("invalid listing truncation")
                for node in root:
                    if node.tag not in ({"Version", "DeleteMarker"} if versions else {"Contents"}):
                        continue
                    key = self._relative(node.findtext("Key", ""))
                    row = {"key": key, "etag": node.findtext("ETag", ""),
                           "size": int(node.findtext("Size", "0")),
                           "version_id": node.findtext("VersionId", ""),
                           "delete_marker": node.tag == "DeleteMarker"}
                    if versions:
                        latest = node.findtext("IsLatest")
                        if latest not in {"true", "false"} or not row["version_id"]:
                            raise ValueError("invalid version record")
                        row["is_latest"] = latest == "true"
                    if row["size"] < 0 or (not row["delete_marker"] and not row["etag"]):
                        raise ValueError("invalid object record")
                    rows.append(row)
                    if len(rows) > MAX_ENTRIES:
                        raise MaintenanceError("publication inventory exceeds entry limit")
                if truncated == "false":
                    identity = [(r["key"], r["version_id"]) for r in rows]
                    if len(set(identity)) != len(identity):
                        raise ValueError("duplicate listing record")
                    return sorted(rows, key=lambda r: (r["key"], r["version_id"]))
                if versions:
                    marker = (root.findtext("NextKeyMarker", ""), root.findtext("NextVersionIdMarker", ""))
                    if not marker[0]:
                        raise ValueError("missing key marker")
                    self._relative(marker[0])
                    params.update({"key-marker": marker[0], "version-id-marker": marker[1]})
                else:
                    marker = (root.findtext("NextContinuationToken", ""),)
                    if not marker[0]:
                        raise ValueError("missing continuation token")
                    params["continuation-token"] = marker[0]
                if marker in markers:
                    raise ValueError("repeated listing marker")
                markers.add(marker)
            except (ET.ParseError, ValueError, TypeError):
                raise MaintenanceError("invalid publication listing XML") from None
        raise MaintenanceError("publication listing exceeds page limit")

    def inventory(self, *, fence=lambda: None):
        return {"objects": self.listing(fence=fence), "versions": self.listing(versions=True, fence=fence)}

    def get_version(self, row):
        return self.request("GET", key=row["key"], params={"versionId": row["version_id"]})

    def delete_version(self, row):
        if not row.get("version_id"):
            raise MaintenanceError("cleanup requires exact version identifiers")
        self.request("DELETE", key=row["key"], params={"versionId": row["version_id"]})


def snapshot(repository, storage, *, verify_reports=False, fence=lambda: None):
    meta = repository.load_meta()
    if (meta.get("storage_target") != storage_target(storage) or meta.get("storage_layout") != "period-v1"
            or meta.get("manifest_key") != "reports.json" or meta.get("latest_key", "") not in {"", "latest.html"}
            or not meta.get("manifest_etag")):
        raise MaintenanceError("cleanup requires committed stable publication")
    manifest = required_object(storage, "reports.json", expected_etag=meta["manifest_etag"])
    value = validate_manifest(manifest.body, stable=True)
    if verify_reports:
        items, current_meta = repository.load()
        if current_meta != meta:
            raise MaintenanceError("publication changed during report verification")
        committed_objects(storage, items, value, fence=fence)
    index = required_object(storage, "site-index.json")
    projected = dict(value, manifest_key="reports.json", latest_key=meta.get("latest_key", ""))
    # The browser projection deliberately has only these manifest properties.
    projected = {k: projected[k] for k in ("version", "updated_at", "manifest_key", "latest_key", "reports")}
    if "generation" in value:
        projected["generation"] = value["generation"]
    if parse_json(index.body) != projected:
        raise MaintenanceError("browser index differs from committed stable publication")
    latest = None
    if meta.get("latest_key"):
        if not meta.get("latest_etag"):
            raise MaintenanceError("committed latest page has no ETag")
        latest = required_object(storage, "latest.html", expected_etag=meta["latest_etag"])
    reachable = {"site-index.json", "reports.json"}
    if latest:
        reachable.add("latest.html")
    for entry in value["reports"]:
        reachable.update((entry["html_key"], entry["json_key"]))
    return {"meta": meta, "index_etag": index.etag, "index_sha256": digest(index.body),
            "manifest_sha256": digest(manifest.body), "reachable": sorted(reachable)}, (index, manifest, latest)


def candidates(inventory, reachable):
    """Only old hash objects and noncurrent versions of known publication keys."""
    reachable = set(reachable)
    objects = {r["key"]: r for r in inventory["objects"]}
    versions = inventory["versions"]
    latest = {}
    for row in versions:
        if row["is_latest"]:
            if row["key"] in latest:
                raise MaintenanceError("ambiguous latest publication version")
            latest[row["key"]] = row
    # Require complete agreement between current and version listings. Unknown
    # keys remain in the inventory fingerprint, but are never candidates.
    for key, row in objects.items():
        version = latest.get(key)
        if (version is None or version["delete_marker"]
                or (version["etag"], version["size"]) != (row["etag"], row["size"])):
            raise MaintenanceError("current and version inventories disagree")
    for key, row in latest.items():
        if not row["delete_marker"] and key not in objects:
            raise MaintenanceError("version inventory has an unlisted current object")
    if not reachable.issubset(objects):
        raise MaintenanceError("reachable stable publication object is missing")
    result = []
    for row in versions:
        key = row["key"]
        if not allowed_key(key):
            continue
        # A suspended bucket can reuse the null version identifier on PUT.
        # A paused writer might therefore turn a formerly noncurrent null
        # version into the current report after inventory. Keep every stable
        # null version, even when the listing currently calls it noncurrent.
        if row["version_id"] == "null" and not LEGACY_KEY.fullmatch(key):
            continue
        if row["delete_marker"]:
            # Removing a latest marker could expose an old unversioned object.
            # Delete noncurrent versions first, then markers; stable markers
            # remain untouched to avoid exposing a previous report as current.
            if (LEGACY_KEY.fullmatch(key) and key not in reachable) or not row["is_latest"]:
                result.append(row)
        elif not row["is_latest"] or (LEGACY_KEY.fullmatch(key) and key not in reachable):
            result.append(row)
    return sorted(result, key=lambda row: (row["delete_marker"], row["key"], row["version_id"]))


def read_versions(rows, versions, *, fence=lambda: None):
    """Bound pending reads and body memory to one chunk; yield on the caller thread."""
    rows = iter(rows)
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as executor:
        while chunk := list(islice(rows, DELETE_BATCH_SIZE)):
            fence()
            futures = [executor.submit(versions.get_version, row) for row in chunk]
            for row, future in zip(chunk, futures, strict=True):
                yield row, future.result()
            fence()


def prepare_cleanup(repository, storage, versions, artifacts, plan_name="cleanup-plan.json"):
    state, values = snapshot(repository, storage, verify_reports=True)
    inventory = versions.inventory()
    selected = candidates(inventory, state["reachable"])
    backups = {}
    for row, value in read_versions((row for row in selected if not row["delete_marker"]), versions):
        if value.etag != row["etag"] or len(value.body) != row["size"]:
            raise MaintenanceError("cleanup backup differs from listed version")
        backups[fingerprint(row)] = artifacts.backup(value.body)
    for value in values:
        if value is not None:
            artifacts.backup(value.body)
    if snapshot(repository, storage)[0] != state or versions.inventory() != inventory:
        raise MaintenanceError("publication changed while preparing cleanup backup")
    plan = {"version": 1, "target": storage_target(storage), "state": state, "inventory": inventory,
            "deletions": selected, "backups": backups}
    artifacts.write(plan_name, encoded(plan))
    retained_null = sum(r["version_id"] == "null" and allowed_key(r["key"])
                        and not LEGACY_KEY.fullmatch(r["key"]) for r in inventory["versions"])
    return {"delete_versions": len(selected), "backup_objects": len(backups), "applied": False,
            "retained_stable_null_versions": retained_null,
            "plan_sha256": fingerprint(plan)}


def append_deletions(artifacts, name, rows):
    """Append one confirmed chunk without rewriting the growing deletion history."""
    if not rows:
        return
    fd = os.open(artifacts.path / name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "ab") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise MaintenanceError("private deletion journal must be owner-only")
        stream.write(encoded({"deleted": rows}))
        stream.flush()
        os.fsync(stream.fileno())


def apply_cleanup(repository, jobs, storage, versions, artifacts, plan_name):
    raw = artifacts.read(plan_name)
    if raw is None:
        raise MaintenanceError("cleanup plan does not exist")
    plan = parse_json(raw)
    if (not isinstance(plan, dict) or plan.get("version") != 1
            or plan.get("target") != storage_target(storage)):
        raise MaintenanceError("cleanup plan target differs")
    deleted = []
    with publication_lease(jobs) as (_, _, fence):
        state, _ = snapshot(repository, storage, verify_reports=True, fence=fence)
        inventory = versions.inventory(fence=fence)
        if state != plan.get("state") or inventory != plan.get("inventory"):
            raise MaintenanceError("cleanup plan is stale; prepare a new plan")
        selected = candidates(inventory, state["reachable"])
        if selected != plan.get("deletions"):
            raise MaintenanceError("cleanup plan contains unsafe deletions")
        for row, value in read_versions((row for row in selected if not row["delete_marker"]),
                                        versions, fence=fence):
            backup = artifacts.read(plan["backups"].get(fingerprint(row), "missing"))
            if backup is None or "sha256-" + digest(backup) != plan["backups"].get(fingerprint(row)):
                raise MaintenanceError("cleanup rollback backup is missing or changed")
            if value.etag != row["etag"] or value.body != backup:
                raise MaintenanceError("cleanup version changed after planning")
        # The plan names every exact version. Keep bounded append-only chunks
        # during execution and write the full summary only on exit. A process
        # killed mid-chunk has an ambiguous tail and requires a fresh plan.
        journal_name = "cleanup-deletions-" + str(uuid4()) + ".jsonl"
        artifacts.write(journal_name, b"")

        def summary():
            artifacts.write("cleanup-apply.json", encoded({
                "plan_sha256": digest(raw), "journal": journal_name, "deleted": deleted,
            }))

        summary()
        try:
            for offset in range(0, len(selected), DELETE_BATCH_SIZE):
                fence()
                if snapshot(repository, storage)[0] != state:
                    raise MaintenanceError("publication changed during cleanup")
                fence()
                renewed_at = time.monotonic()
                confirmed = []
                try:
                    for row in selected[offset:offset + DELETE_BATCH_SIZE]:
                        # Slow storage responses must not exhaust the lease
                        # inside a chunk. Fast chunks need no per-row DB call.
                        if time.monotonic() - renewed_at >= LEASE_RENEW_INTERVAL:
                            fence()
                            renewed_at = time.monotonic()
                        versions.delete_version(row)
                        deleted.append(row)
                        confirmed.append(row)
                finally:
                    append_deletions(artifacts, journal_name, confirmed)
                fence()
                if snapshot(repository, storage)[0] != state:
                    raise MaintenanceError("publication changed during cleanup")
        except Exception:
            summary()
            raise MaintenanceError("cleanup interrupted; private deletion journal records partial progress") from None
        summary()
    retained_null = sum(r["version_id"] == "null" and allowed_key(r["key"])
                        and not LEGACY_KEY.fullmatch(r["key"]) for r in inventory["versions"])
    return {"deleted_versions": len(deleted), "applied": True, "plan_sha256": digest(raw),
            "retained_stable_null_versions": retained_null}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-namespace", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--iam-token-file", type=Path,
                        help="owner-only mounted IAM token for YDB and Object Storage; otherwise metadata credentials")
    parser.add_argument("--plan", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db = versions = None
    try:
        artifacts = PrivateArtifacts(args.artifacts)
        if args.apply and args.plan is None:
            raise MaintenanceError("cleanup apply requires an explicit plan")
        plan_path = args.plan.absolute() if args.plan else artifacts.path / "cleanup-plan.json"
        if plan_path.parent != artifacts.path:
            raise MaintenanceError("cleanup plan must be inside private artifacts")
        db, repository, jobs, storage = open_dependencies(args.target_namespace, args.iam_token_file)
        versions = VersionInventory(storage)
        if args.apply:
            result = apply_cleanup(repository, jobs, storage, versions, artifacts, plan_path.name)
        else:
            result = prepare_cleanup(repository, storage, versions, artifacts, plan_path.name)
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("publication cleanup failed; inspect private plan and deletion journal", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if versions is not None:
            with suppress(Exception):
                versions.close()
        if db is not None:
            with suppress(Exception):
                db.close()


if __name__ == "__main__":
    main()

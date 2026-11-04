"""Copy committed publication bytes to period keys without report regeneration.

Default is read-only storage inventory. --apply acquires the publication lease,
conditionally creates absent destinations and commits pointers through YDB fencing.
Keep --artifacts outside the repository: it contains private rollback material.
Run with the regular YDB/Object Storage environment in a Docker container.
--rollback --apply restores the original fenced YDB pointers only while no
publication state has advanced and all original immutable bytes still exist.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from contextlib import nullcontext, suppress
from pathlib import Path
from uuid import uuid4

from tools.publication_storage_admin import (
    MaintenanceError,
    PrivateArtifacts,
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
from zont_analyzer.cloud.publication import CloudPublication


def migrate(repository, jobs, storage, artifacts, *, apply=False):
    target = storage_target(storage)
    context = publication_lease(jobs) if apply else nullcontext(("", 0, lambda: None))
    with context as (owner, attempt, fence):
        items, meta = repository.load()
        if meta.get("storage_target") != target:
            raise MaintenanceError("committed publication storage target differs")
        if meta.get("storage_layout") == "period-v1":
            if not meta.get("manifest_etag") or (meta.get("latest_key") and not meta.get("latest_etag")):
                raise MaintenanceError("stable committed publication has no ETag")
            manifest = required_object(storage, "reports.json", expected_etag=meta.get("manifest_etag", ""))
            validate_manifest(manifest.body, stable=True)
            if meta.get("manifest_key") != "reports.json" or meta.get("latest_key", "") not in {"", "latest.html"}:
                raise MaintenanceError("inconsistent stable publication pointers")
            committed_objects(storage, items, parse_json(manifest.body), fence=fence)
            if meta.get("latest_key"):
                required_object(storage, "latest.html", expected_etag=meta.get("latest_etag", ""))
            if apply:
                current_index = storage.get("site-index.json")
                if current_index is not None:
                    artifacts.backup(current_index.body)
                fence()
                CloudPublication(storage).update_site_index(repository)
            return {"reports": len(parse_json(manifest.body)["reports"]), "copied": 0,
                    "already_migrated": True, "manifest_sha256": digest(manifest.body)}
        source = fingerprint({"items": items, "meta": meta})
        manifest_object = required_object(storage, meta.get("manifest_key", ""),
                                          expected_etag=meta.get("manifest_etag", ""))
        manifest = validate_manifest(manifest_object.body)
        current_index = required_object(storage, "site-index.json")
        expected_index = {"version": manifest["version"], "updated_at": manifest["updated_at"],
                          "manifest_key": meta["manifest_key"], "latest_key": meta.get("latest_key", ""),
                          "reports": manifest["reports"]}
        if "generation" in manifest:
            expected_index["generation"] = manifest["generation"]
        if parse_json(current_index.body) != expected_index:
            raise MaintenanceError("browser index differs from committed migration source")
        objects = committed_objects(storage, items, manifest, fence=fence)
        if meta.get("latest_key"):
            if not meta["latest_key"].startswith("publication/latest/"):
                raise MaintenanceError("source latest page is not legacy publication")
            objects["latest.html"] = (meta["latest_key"], required_object(
                storage, meta["latest_key"], expected_etag=meta.get("latest_etag", "")))
        if meta.get("manifest_key", "").split("/")[:2] != ["publication", "manifests"]:
            raise MaintenanceError("source manifest is not legacy publication")
        journal_body = artifacts.read("migration.json")
        journal = parse_json(journal_body) if journal_body else None
        if journal is not None and (not isinstance(journal, dict) or journal.get("version") != 1
                                    or journal.get("target") != target or journal.get("source") != source):
            raise MaintenanceError("migration journal disagrees with committed source")
        prepared = copy.deepcopy(manifest)
        prepared["generation"] = journal["manifest"]["generation"] if journal is not None else str(uuid4())
        if not isinstance(prepared["generation"], str) or not prepared["generation"]:
            raise MaintenanceError("migration journal has no publication generation")
        for entry in prepared["reports"]:
            entry["html_key"] = entry["href"]
            entry["json_key"] = entry["href"][:-5] + ".json"
        if journal is not None and journal.get("manifest") != prepared:
            raise MaintenanceError("migration journal changes more than publication links")
        if journal is None:
            journal = {"version": 1, "target": target, "source": source, "manifest": prepared}
        prepared_body = encoded(journal["manifest"])
        # Preflight every destination before the first storage write. Existing
        # identical bytes are the only acceptable interrupted-run remnants.
        desired = {key: value.body for key, (_, value) in objects.items()}
        desired["reports.json"] = prepared_body
        for key, body in desired.items():
            fence()
            existing = storage.get(key)
            if existing is not None and (existing.body != body or not existing.etag):
                raise MaintenanceError("stable publication destination conflicts")
        inventory = {"version": 1, "target": target, "source": source,
                     "items": items, "meta": meta, "manifest_backup": artifacts.backup(manifest_object.body),
                     "manifest_etag": manifest_object.etag,
                     "site_index_backup": artifacts.backup(current_index.body), "site_index_etag": current_index.etag,
                     "objects": [{"source": old_key, "destination": key, "etag": value.etag,
                                  "backup": artifacts.backup(value.body)}
                                 for key, (old_key, value) in sorted(objects.items())]}
        artifacts.write("migration-inventory.json", encoded(inventory))
        artifacts.write("migration.json", encoded(journal))
        if repository.load() != (items, meta):
            raise MaintenanceError("publication changed during migration inventory")
        if not apply:
            return {"reports": len(manifest["reports"]), "objects": len(desired), "applied": False,
                    "inventory_sha256": fingerprint(inventory)}
        stamps = {}
        for key, body in desired.items():
            fence()
            existing = storage.get(key)
            if existing is None:
                content_type = ("text/html; charset=utf-8" if key.endswith(".html")
                                else "application/json; charset=utf-8")
                etag = storage.conditional_put(key, body, content_type, expected_etag=None)
                existing = storage.get(key)
                if etag is not None and (existing is None or existing.etag != etag):
                    raise MaintenanceError("created publication ETag verification failed")
            if existing is None or existing.body != body or not existing.etag:
                raise MaintenanceError("stable publication create conflicted")
            stamps[key] = existing.etag
        updated_items = copy.deepcopy(items)
        for entry in journal["manifest"]["reports"]:
            item = updated_items[entry["href"]]
            item.update(entry=json.dumps(entry, sort_keys=True), html_stamp=stamps[entry["html_key"]],
                        json_stamp=stamps[entry["json_key"]])
        updated_meta = dict(meta, manifest_key="reports.json", manifest_etag=stamps["reports.json"],
                            latest_key="latest.html" if "latest.html" in objects else "",
                            latest_etag=stamps.get("latest.html", ""), storage_layout="period-v1")
        fence()
        if not repository.save(updated_items, items, updated_meta, meta,
                               expected_checkpoint=int(meta.get("checkpoint", "0")),
                               lease_owner=owner, lease_attempt=attempt):
            raise MaintenanceError("migration commit lost its checkpoint or publication lease")
        artifacts.write("migration-committed.json", encoded({"items": updated_items, "meta": updated_meta}))
        fence()
        CloudPublication(storage).update_site_index(repository)
        result = {"reports": len(manifest["reports"]), "copied": len(desired), "applied": True,
                  "inventory_sha256": fingerprint(inventory)}
        artifacts.write("migration-result.json", encoded(result))
        return result


def rollback(repository, jobs, storage, artifacts):
    inventory_body = artifacts.read("migration-inventory.json")
    committed_body = artifacts.read("migration-committed.json")
    if inventory_body is None or committed_body is None:
        raise MaintenanceError("rollback requires original inventory and a confirmed migration commit")
    inventory, committed = parse_json(inventory_body), parse_json(committed_body)
    target = storage_target(storage)
    if (inventory.get("version") != 1 or inventory.get("target") != target
            or fingerprint({"items": inventory.get("items"), "meta": inventory.get("meta")})
            != inventory.get("source")):
        raise MaintenanceError("rollback inventory target or source fingerprint differs")
    original = (inventory["items"], inventory["meta"])
    expected = (committed["items"], committed["meta"])
    with publication_lease(jobs) as (owner, attempt, fence):
        current = repository.load()
        if current != expected and current != original:
            raise MaintenanceError("publication advanced after migration; rollback is refused")
        if original[1].get("storage_target") != target:
            raise MaintenanceError("rollback publication target differs")
        fence()
        manifest = required_object(storage, original[1]["manifest_key"], expected_etag=inventory["manifest_etag"])
        manifest_backup = artifacts.read(inventory["manifest_backup"])
        if manifest.body != manifest_backup:
            raise MaintenanceError("legacy rollback manifest differs from private backup")
        value = validate_manifest(manifest.body)
        verified = committed_objects(storage, original[0], value, fence=fence)
        for row in inventory["objects"]:
            fence()
            obj = required_object(storage, row["source"], expected_etag=row["etag"])
            backup = artifacts.read(row["backup"])
            if backup is None or obj.body != backup:
                raise MaintenanceError("legacy rollback object differs from private backup")
        if original[1].get("latest_key"):
            required_object(storage, original[1]["latest_key"])
        already_original = current == original
        if not already_original:
            fence()
            if not repository.save(copy.deepcopy(original[0]), current[0], copy.deepcopy(original[1]), current[1],
                                   expected_checkpoint=int(current[1].get("checkpoint", "0")),
                                   lease_owner=owner, lease_attempt=attempt):
                raise MaintenanceError("rollback commit lost its checkpoint or publication lease")
        if repository.load() != original:
            raise MaintenanceError("rollback did not restore the exact original publication state")
        artifacts.write("migration-rollback.json", encoded({"target": target, "source": inventory["source"],
                                                            "restored": True}))
        fence()
        CloudPublication(storage).update_site_index(repository)
        return {"restored_reports": len(verified) // 2, "rolled_back": True,
                "already_restored": already_original, "source_sha256": inventory["source"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-namespace", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--iam-token-file", type=Path,
                        help="owner-only mounted IAM token for YDB and Object Storage; otherwise metadata credentials")
    parser.add_argument("--rollback", action="store_true", help="restore unchanged migration state; requires --apply")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db = None
    try:
        artifacts = PrivateArtifacts(args.artifacts)
        if args.rollback and not args.apply:
            raise MaintenanceError("rollback requires explicit --apply")
        db, repository, jobs, storage = open_dependencies(args.target_namespace, args.iam_token_file)
        result = rollback(repository, jobs, storage, artifacts) if args.rollback else migrate(
            repository, jobs, storage, artifacts, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
    except Exception:
        print("publication migration failed; inspect private artifacts and retry safely", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if db is not None:
            with suppress(Exception):
                db.close()


if __name__ == "__main__":
    main()

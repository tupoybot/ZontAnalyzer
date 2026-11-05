"""Rehearse configuration compaction on a private JSONL export, never a database.

Input rows have device_id, content_hash, id, payload and captured_at. Preserve
the original export for recovery. This tool creates a new directory and refuses
to overwrite evidence; it has no database credentials, network or apply mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.ydb.telemetry import encode
from zont_analyzer.adapters.zont_readonly.configuration import configuration_snapshot


def compact(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (row["device_id"], row["captured_at"], row["id"]))
    previous: dict[str, str] = {}
    retained = []
    before_bytes = after_bytes = 0
    identities = set()
    for row in ordered:
        if (not isinstance(row["device_id"], str) or type(row["id"]) is not int
                or type(row["captured_at"]) is not int or row["captured_at"] <= 0):
            raise ValueError("invalid configuration observation")
        identity = (row["device_id"], row["id"])
        if identity in identities:
            raise ValueError("duplicate configuration event identity")
        identities.add(identity)
        raw = json.loads(row["payload"])
        if not isinstance(raw, dict):
            raise ValueError("configuration payload must be an object")
        payload = encode(configuration_snapshot(raw))
        before_bytes += len(row["payload"].encode())
        if previous.get(row["device_id"]) != payload:
            digest = hashlib.sha256(payload.encode()).hexdigest()
            retained.append({**row, "content_hash": f"v2:{row['id']:020d}:{digest}", "payload": payload})
            after_bytes += len(payload.encode())
            previous[row["device_id"]] = payload
        # Every source observation maps to the same settings in the compacted
        # timeline. A recurring A after B must therefore have its own event.
        if previous[row["device_id"]] != payload:
            raise AssertionError("configuration timeline changed")
    return retained, {
        "source_rows": len(rows), "retained_events": len(retained),
        "source_payload_bytes": before_bytes, "compacted_payload_bytes": after_bytes,
        "configuration_timeline_preserved": True,
        "runtime_values_removed_from_snapshot_only": True,
        "production_modified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.source.read_bytes()
    rows = [json.loads(line) for line in source.splitlines() if line.strip()]
    retained, summary = compact(rows)
    args.output.mkdir(mode=0o700)
    target = args.output / "config_snapshots.jsonl"
    target.write_text("".join(encode(row) + "\n" for row in retained))
    target.chmod(0o600)
    summary.update(source_sha256=hashlib.sha256(source).hexdigest(),
                   compacted_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    manifest = args.output / "summary.json"
    manifest.write_text(json.dumps(summary, indent=2) + "\n")
    manifest.chmod(0o600)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()

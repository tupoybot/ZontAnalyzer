"""Private bounded event-only export, offline projection build and fenced upload.

No telemetry samples are scanned. Source events are paged by their canonical
device/time key; only a completely exported, unchanged device may publish READY.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.backfill_regulation import MAX_BYTES, _write_private
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.adapters.ydb.regulation_events import (
    CONTRACT,
    PAGE_SIZE,
    EventProjectionRepository,
    projected_row,
)
from zont_analyzer.domain import SourceEvent

FORMAT = "regulation-event-projection-export-v1"
MAX_ROWS = 100_000


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("event projection artifact exceeds 64 MiB")
    value: dict[str, Any] = json.loads(path.read_text())
    if value.get("format") != FORMAT or value.get("contract") != CONTRACT:
        raise ValueError("unsupported event projection artifact or event-family contract")
    return value


def generation_key(value: dict[str, Any]) -> str:
    return _digest({key: value[key] for key in ("contract", "source_identity", "device_id",
                                                "source_revision", "source_digest")})


def export_events(db: Database, device: str, output: Path, *, max_rows: int = MAX_ROWS) -> dict[str, int]:
    if not 1 <= max_rows <= MAX_ROWS:
        raise ValueError("event export requires a positive cap of at most 100000 rows")
    repository = EventProjectionRepository(db.storage)
    revision = repository.revision(device)
    events = []
    after = None
    pages = 0
    while True:
        page = repository.source_page(device, after=after)
        pages += 1
        for row in page:
            event = SourceEvent.model_validate_json(row["payload"])
            if event.device_id != device:
                raise ValueError("canonical event export contains another device")
            events.append(event.model_dump(mode="json"))
        if len(events) > max_rows:
            raise ValueError("bounded event export cap exceeded; no complete projection was exported")
        if len(page) < PAGE_SIZE:
            break
        after = (int(page[-1]["timestamp_utc"]), str(page[-1]["id"]))
    if repository.revision(device) != revision:
        raise ValueError("canonical source changed during event export; export again")
    _write_private(output, {"format": FORMAT, "contract": CONTRACT, "kind": "canonical_events",
                            "source_identity": db.identity, "device_id": device, "source_revision": revision,
                            "source_digest": _digest(events), "events": events})
    return {"canonical_events": len(events), "source_pages": pages, "bytes": output.stat().st_size}


def calculate_export(source: Path, output: Path) -> dict[str, int]:
    value = _read(source)
    if value.get("kind") != "canonical_events" or _digest(value["events"]) != value.get("source_digest"):
        raise ValueError("canonical event export checksum mismatch")
    if len(value["events"]) > MAX_ROWS:
        raise ValueError("event export cap exceeded")
    device = value["device_id"]
    events = [SourceEvent.model_validate(item) for item in value["events"]]
    if any(event.device_id != device for event in events) or len({event.id for event in events}) != len(events):
        raise ValueError("event export has conflicting identities")
    generation = generation_key(value)
    selected = [event.model_dump(mode="json") for event in events
                if projected_row(device, generation, event) is not None]
    compact = {key: value[key] for key in ("format", "contract", "source_identity", "device_id",
                                          "source_revision", "source_digest")}
    compact.update(kind="generation", generation=generation, events=selected,
                   projected_digest=_digest(selected), canonical_event_count=len(events))
    _write_private(output, compact)
    return {"canonical_events": len(events), "projected_events": len(selected), "bytes": output.stat().st_size}


def upload_generation(db: Database, source: Path) -> dict[str, Any]:
    value = _read(source)
    if (value.get("kind") != "generation" or value.get("generation") != generation_key(value)
            or value.get("projected_digest") != _digest(value["events"])):
        raise ValueError("projection generation checksum mismatch")
    if value.get("source_identity") != db.identity:
        raise ValueError("projection belongs to another canonical source database")
    device, generation, revision = value["device_id"], value["generation"], value["source_revision"]
    events = [SourceEvent.model_validate(item) for item in value["events"]]
    if len(events) > MAX_ROWS or len({event.id for event in events}) != len(events):
        raise ValueError("projection event count/identity mismatch")
    if any(projected_row(device, generation, event) is None for event in events):
        raise ValueError("projection contains an unsupported event family")
    repository = EventProjectionRepository(db.storage)
    for offset in range(0, len(events), PAGE_SIZE):
        repository.write_generation(device, generation, revision, events[offset:offset + PAGE_SIZE])
    first: dict[str, SourceEvent] = {}
    for event in events:
        row = projected_row(device, generation, event)
        assert row is not None
        family = row["family"]
        if family not in first or event.timestamp_utc < first[family].timestamp_utc:
            first[family] = event
    changed = repository.publish_generation(device, generation, revision, len(events), list(first.values()))
    return {"projected_events": len(events), "activated": changed, "generation": generation}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--device", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--max-rows", type=int, default=MAX_ROWS)
    calculate = commands.add_parser("calculate")
    calculate.add_argument("--input", type=Path, required=True)
    calculate.add_argument("--output", type=Path, required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("--input", type=Path, required=True)
    upload.add_argument("--apply", action="store_true", required=True)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--device", required=True)
    cleanup.add_argument("--generation", required=True)
    cleanup.add_argument("--max-batches", type=int, default=20)
    cleanup.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    if args.command == "calculate":
        result: dict[str, Any] = calculate_export(args.input, args.output)
    else:
        db = Database(YdbConfig.from_environment())
        try:
            if args.command == "export":
                result = export_events(db, args.device, args.output, max_rows=args.max_rows)
            elif args.command == "upload":
                result = upload_generation(db, args.input)
            else:
                result = {"removed_rows": EventProjectionRepository(db.storage).cleanup_generation(
                    args.device, args.generation, max_batches=args.max_batches)}
        finally:
            db.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()

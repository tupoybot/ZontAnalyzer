"""Exercise the real private export/offline calculation/upload projection path."""
from pathlib import Path
from typing import Any

from tools.backfill_regulation_events import calculate_export, export_events, upload_generation
from zont_analyzer.adapters.ydb.application import Database


def initialize_projection(db: Database, directory: Path, device: str = "device") -> dict[str, Any]:
    source, compact = directory / f"events-{device}-raw.json", directory / f"events-{device}-compact.json"
    exported = export_events(db, device, source)
    calculated = calculate_export(source, compact)
    activated = upload_generation(db, compact)
    return {**activated, "exported": exported, "calculated": calculated, "source": source, "compact": compact}

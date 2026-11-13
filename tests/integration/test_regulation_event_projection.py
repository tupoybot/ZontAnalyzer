from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.regulation_projection_support import initialize_projection
from tests.unit.test_regulation import START, inputs
from tests.ydb_support import make_database
from tools.backfill_regulation_events import calculate_export, export_events, upload_generation
from zont_analyzer.adapters.ydb.database import Transaction
from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.adapters.ydb.regulation_events import EventProjectionRepository
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.analytics.regulation import build_regulation_facts
from zont_analyzer.application.regulation_reliability import apply_reliability_context
from zont_analyzer.domain import SourceEvent, TelemetryPoint

pytestmark = pytest.mark.ydb


def event(identifier: str, kind: str, minutes: int) -> SourceEvent:
    return SourceEvent(id=identifier, device_id="device", event_type=kind,
                       timestamp_utc=START + timedelta(minutes=minutes))


def write(db: Any, events: list[SourceEvent]) -> None:
    left = min(item.timestamp_utc for item in events)
    right = max(item.timestamp_utc for item in events) + timedelta(minutes=1)
    db.telemetry.write_window(device_id="device", data_type="events", start=left, end=right, points=[], events=events)


def context(db: Any) -> dict[str, Any]:
    return RegulationRepository(db.storage).source_events("device", START, START + timedelta(hours=1))


def test_complete_projection_proves_absence_despite_over_128_unrelated_prior_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    noise = [event(f"noise:{index}", "Info", -1000 + index) for index in range(1001)]
    write(db, [event("on", "PowerOn", -3000), event("loss", "LossConnectionBoiler", -2900),
               event("restore", "ReconnectingBoiler", -2800), *noise])
    assert not context(db)["projection_ready"]
    initialized = initialize_projection(db, tmp_path)
    assert initialized["exported"]["source_pages"] == 3
    assert initialized["calculated"]["projected_events"] == 3
    queries = []
    original = Transaction.execute

    def counted(tx: Transaction, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        queries.append(query)
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", counted)
    selected = context(db)
    assert selected["projection_ready"] and selected["prior_state_complete"] and not selected["incomplete"]
    assert {item.event_type for item in selected["events"]} == {"PowerOn", "ReconnectingBoiler"}
    assert len(queries) == 2
    assert "FROM source_events" not in " ".join(queries)
    assert queries[-1].count("SELECT payload FROM regulation_event_projection") == 8
    enriched = apply_reliability_context(inputs(), selected, start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=enriched)
    assert facts["coverage"]["eligible_seconds"] == 3600


def test_old_unclosed_power_fault_remains_excluded_behind_noise(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    write(db, [event("old-power-loss", "MainPowerLost", -10_000),
               *[event(f"noise:{index}", "Info", -1000 + index) for index in range(500)]])
    initialize_projection(db, tmp_path)
    selected = context(db)
    assert not selected["incomplete"]
    assert [item.event_type for item in selected["events"]] == ["MainPowerLost"]
    enriched = apply_reliability_context(inputs(), selected, start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=enriched)
    assert facts["coverage"]["eligible_seconds"] == 0
    assert facts["coverage"]["exclusion_seconds"]["reliability"] == 3600


def test_simultaneous_cloud_restore_follows_loss_in_prior_and_window_context(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    write(db, [event("z-prior-loss", "disconnected", -20), event("a-prior-restore", "connected", -20),
               event("z-window-loss", "disconnected", 20), event("a-window-restore", "connected", 20)])
    initialize_projection(db, tmp_path)
    selected = context(db)
    assert [item.id for item in selected["events"]] == ["a-prior-restore", "z-window-loss", "a-window-restore"]
    enriched = apply_reliability_context(inputs(), selected, start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=enriched)
    assert facts["coverage"]["eligible_seconds"] == 3600


def test_late_and_corrected_event_keys_change_atomically_without_orphans(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    write(db, [event("initial-on", "PowerOn", -1440)])
    initialize_projection(db, tmp_path)
    write(db, [event("mutable", "PowerOff", 10)])
    assert {item.event_type for item in context(db)["events"]} == {"PowerOn", "PowerOff"}
    write(db, [event("mutable", "PowerOn", 20)])
    selected = context(db)
    assert selected["projection_ready"]
    assert [item.event_type for item in selected["events"]].count("PowerOff") == 0
    write(db, [event("mutable", "MainPowerLost", -2880)])
    selected = context(db)
    assert selected["projection_ready"]
    assert {item.event_type for item in selected["events"]} == {"MainPowerLost", "PowerOn"}
    row, = db.storage.execute("SELECT COUNT(*) AS count FROM regulation_event_projection;")[0].rows
    assert row.count == 2


def test_numeric_writer_advances_epoch_and_legacy_gap_requires_new_complete_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    initialize_projection(db, tmp_path)
    point = TelemetryPoint(device_id="device", source_type="temperature", entity_id="room", metric_key="temperature",
                           timestamp_utc=START, value_num=20)
    queries = []
    original = Transaction.execute

    def counted(tx: Transaction, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        if parameters and parameters.get("$key") == "regulation-event-projection:device":
            queries.append(query)
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", counted)
    db.telemetry.write_window(device_id="device", data_type="history", start=START,
                              end=START + timedelta(hours=1), points=[point])
    assert len(queries) == 2  # One readiness read and one epoch update per changed numeric batch.
    assert context(db)["projection_ready"]
    db.storage.transaction(lambda tx: bump_revision(tx, "telemetry:device", publish=False))
    assert not context(db)["projection_ready"]
    point = point.model_copy(update={"timestamp_utc": START + timedelta(minutes=1)})
    db.telemetry.write_window(device_id="device", data_type="history", start=START,
                              end=START + timedelta(hours=1), points=[point])
    assert not context(db)["projection_ready"]
    initialize_projection(db, tmp_path)
    assert context(db)["projection_ready"]


def test_correction_moving_event_to_another_device_fences_both_scopes(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    write(db, [event("moved", "PowerOff", -100)])
    initialize_projection(db, tmp_path)
    initialize_projection(db, tmp_path, "other")
    repository = EventProjectionRepository(db.storage)
    before = repository.revision("device")
    moved = SourceEvent(id="moved", device_id="other", event_type="PowerOn",
                        timestamp_utc=START - timedelta(minutes=50))
    db.telemetry.write_window(device_id="other", data_type="events", start=START - timedelta(hours=1),
                              end=START, points=[], events=[moved])
    assert repository.revision("device") == before + 1
    assert not context(db)["projection_ready"]
    target = repository.source_context("other", START, START + timedelta(hours=1))
    assert target["projection_ready"] and [item.id for item in target["events"]] == ["moved"]
    initialize_projection(db, tmp_path)
    assert context(db)["projection_ready"] and context(db)["events"] == []


def test_export_race_never_activates_partial_generation_and_cleanup_is_bounded(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    events = [event(f"state:{index}", "PowerOn" if index % 2 else "PowerOff", -1000 + index)
              for index in range(600)]
    write(db, events)
    raw, compact = tmp_path / "raw.json", tmp_path / "compact.json"
    export_events(db, "device", raw)
    calculate_export(raw, compact)
    import json

    manifest = json.loads(compact.read_text())
    repository = EventProjectionRepository(db.storage)
    repository.write_generation("device", manifest["generation"], manifest["source_revision"], events[:500])
    write(db, [event("late", "MainPowerLost", -2000)])
    with pytest.raises(ValueError, match="source changed"):
        upload_generation(db, compact)
    assert not context(db)["projection_ready"]
    assert repository.cleanup_generation("device", manifest["generation"], max_batches=1) == 500


def test_idempotent_upload_never_rewrites_active_generation_and_retired_cleanup_preserves_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    events = [event("on", "PowerOn", -100)]
    write(db, events)
    initialized = initialize_projection(db, tmp_path)
    assert not upload_generation(db, initialized["compact"])["activated"]
    repository = EventProjectionRepository(db.storage)
    with pytest.raises(ValueError, match="active"):
        repository.cleanup_generation("device", initialized["generation"])
    revision = repository.revision("device")
    repository.write_generation("device", "replacement", revision, events)
    original = Transaction.execute
    retired = False

    def retire_during_read(tx: Transaction, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        nonlocal retired
        if "SELECT payload FROM regulation_event_projection" in query and not retired:
            retired = True
            repository.publish_generation("device", "replacement", revision, 1, events)
            repository.cleanup_generation("device", initialized["generation"])
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", retire_during_read)
    selected = context(db)
    assert retired
    assert selected["projection_ready"] and not selected["incomplete"]
    assert [item.id for item in selected["events"]] == ["on"]

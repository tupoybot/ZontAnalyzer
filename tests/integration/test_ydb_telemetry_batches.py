"""Bounded canonical event writes against a real disposable YDB namespace."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.database import Transaction
from zont_analyzer.domain import SourceEvent, TelemetryPoint


def test_sparse_sample_replay_reads_only_requested_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    points = [TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room",
                             metric_key="temperature", timestamp_utc=start + timedelta(seconds=index),
                             value_num=20.0) for index in range(2000)]
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end, points=points)
    original_execute = Transaction.execute
    fetched: list[int] = []

    def count_rows(self: Transaction, query: str, parameters=None):
        result = original_execute(self, query, parameters)
        if "SELECT * FROM telemetry_samples" in query:
            fetched.append(len(result[0].rows))
        return result

    monkeypatch.setattr(Transaction, "execute", count_rows)
    before = db.get_app_meta("telemetry-day:2026-01-01")
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end,
                              points=[points[0], points[-1]])
    assert fetched == [2]
    assert db.get_app_meta("telemetry-day:2026-01-01") == before


def test_event_batch_is_atomic_idempotent_and_rekeys_changed_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_database(tmp_path)
    db.save_devices([{"device_id": "fixture"}])
    old_start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    new_start = old_start + timedelta(days=1)
    old_events = [SourceEvent(id=f"event-{index}", device_id="fixture", event_type="PowerOn",
                              timestamp_utc=old_start + timedelta(seconds=index))
                  for index in range(2000)]
    new_events = [event.model_copy(update={"timestamp_utc": new_start + timedelta(seconds=index)})
                  for index, event in enumerate(old_events)]
    original_execute = Transaction.execute
    event_statements: list[str] = []

    def count_event_statements(self: Transaction, query: str, parameters=None):
        if "source_events" in query:
            event_statements.append(query)
        return original_execute(self, query, parameters)

    monkeypatch.setattr(Transaction, "execute", count_event_statements)
    old_end = old_start + timedelta(minutes=40)
    new_end = new_start + timedelta(minutes=40)
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=old_start,
                              end=old_end, events=old_events)
    assert len(event_statements) == 2  # One indexed lookup, one bounded table UPSERT.
    old_marker = db.get_app_meta("telemetry-day:2026-01-01")
    event_statements.clear()
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=old_start,
                              end=old_end, events=old_events)
    assert len(event_statements) == 1  # Unchanged events never issue row writes.
    assert db.get_app_meta("telemetry-day:2026-01-01") == old_marker
    event_statements.clear()
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=new_start,
                              end=new_end, events=new_events)
    assert len(event_statements) == 3  # Lookup, old-key DELETE, new-key UPSERT.
    assert db.list_source_events(old_start, old_end) == []
    assert len(db.list_source_events(new_start, new_end)) == 2000
    assert db.get_app_meta("telemetry-day:2026-01-01") != old_marker
    new_marker = db.get_app_meta("telemetry-day:2026-01-02")
    assert new_marker is not None
    event_statements.clear()
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=new_start,
                              end=new_end, events=new_events)
    assert len(event_statements) == 1
    assert db.get_app_meta("telemetry-day:2026-01-02") == new_marker

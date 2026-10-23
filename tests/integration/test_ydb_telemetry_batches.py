"""Bounded canonical event writes against a real disposable YDB namespace."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.database import Transaction
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.domain import SourceEvent, TelemetryPoint


def _boiler_point(at: datetime, *, value: str = "['fl']") -> TelemetryPoint:
    return TelemetryPoint(device_id="fixture", source_type="z3k_boiler_adapter",
                          entity_id="boiler", metric_key="s", timestamp_utc=at,
                          value_text=value)


def _publication_marker(db, scope: str, hour: str) -> tuple[int, str]:
    rows = db.storage.execute(
        "DECLARE $scope AS Utf8; DECLARE $hour AS Utf8; "
        "SELECT revision,payload FROM publication_changes "
        "WHERE scope=$scope AND identifier=$hour;",
        {"$scope": scope, "$hour": hour},
    )[0].rows
    assert len(rows) == 1
    return int(rows[0].revision), str(rows[0].payload)


def _publication_ack(db, revision: int) -> None:
    db.storage.execute(
        "DECLARE $value AS Utf8; UPSERT INTO metadata (name,value) "
        "VALUES ('publication:checkpoint',$value);",
        {"$value": str(revision)},
    )


@pytest.mark.ydb
def test_calibration_marker_coalesces_exact_changed_times_until_ack(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    hour = "2026-01-01T10"
    first = start + timedelta(minutes=5)
    second = start + timedelta(minutes=45)
    third = start + timedelta(minutes=55)
    for at in (first, second):
        db.telemetry.write_window(device_id="fixture", data_type="history",
                                  start=start, end=end, points=[_boiler_point(at)])
        revision, payload = _publication_marker(db, "telemetry-gas", hour)
        assert json.loads(payload) == {
            "calibration": True, "change_start": int(first.timestamp()),
            "change_end": int(at.timestamp()),
        }
        assert json.loads(_publication_marker(db, "telemetry", hour)[1]) == {"calibration": False}
        if at == first:
            publisher_upper = revision
    # An in-flight publisher captured the first revision; a later same-hour
    # write must remain visible beyond that captured high-water mark.
    _publication_ack(db, publisher_upper)
    unconsumed = PublicationRepository(db.storage).changes_since(publisher_upper, db.source_revision())
    assert any(change["scope"] == "telemetry-gas" and
               json.loads(change["payload"])["change_start"] == int(first.timestamp())
               for change in unconsumed)
    _publication_ack(db, db.source_revision())
    db.telemetry.write_window(device_id="fixture", data_type="history",
                              start=start, end=end, points=[_boiler_point(third)])
    revision, payload = _publication_marker(db, "telemetry-gas", hour)
    assert json.loads(payload) == {
        "calibration": True, "change_start": int(third.timestamp()),
        "change_end": int(third.timestamp()),
    }
    db.telemetry.write_window(device_id="fixture", data_type="history",
                              start=start, end=end, points=[_boiler_point(third)])
    assert _publication_marker(db, "telemetry-gas", hour) == (revision, payload)


@pytest.mark.ydb
@pytest.mark.parametrize("old_payload", ["{}", "not-json", '{"calibration":true,"change_start":"bad"}'])
def test_unconsumed_unknown_calibration_marker_remains_broad(
    tmp_path: Path, old_payload: str,
) -> None:
    db = make_database(tmp_path)
    start = datetime(2026, 1, 1, 10, tzinfo=UTC)
    end = start + timedelta(hours=1)
    hour = "2026-01-01T10"
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end,
                              points=[_boiler_point(start + timedelta(minutes=5))])
    db.storage.execute(
        "DECLARE $payload AS Utf8; UPDATE publication_changes SET payload=$payload "
        "WHERE scope='telemetry-gas' AND identifier='2026-01-01T10';",
        {"$payload": old_payload},
    )
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end,
                              points=[_boiler_point(start + timedelta(minutes=45))])
    assert _publication_marker(db, "telemetry-gas", hour)[1] == "{}"
    # Existing ordinary telemetry compatibility must also retain an
    # unconsumed legacy marker rather than narrow it to calibration=false.
    db.storage.execute(
        "UPDATE publication_changes SET payload='{}' "
        "WHERE scope='telemetry' AND identifier='2026-01-01T10';"
    )
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end,
                              points=[_boiler_point(start + timedelta(minutes=50))])
    assert _publication_marker(db, "telemetry", hour)[1] == "{}"
    _publication_ack(db, db.source_revision())
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end,
                              points=[_boiler_point(start + timedelta(minutes=55))])
    assert json.loads(_publication_marker(db, "telemetry-gas", hour)[1]) == {
        "calibration": True,
        "change_start": int((start + timedelta(minutes=55)).timestamp()),
        "change_end": int((start + timedelta(minutes=55)).timestamp()),
    }


@pytest.mark.ydb
def test_imported_event_replay_ignores_json_order_and_equivalent_utc_spelling(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture"}])
    start = datetime(2026, 1, 1, tzinfo=UTC)
    event = SourceEvent(id="imported", device_id="fixture", event_type="PowerOn", timestamp_utc=start,
                        details={"second": 2, "first": 1})
    source = event.model_dump(mode="json")
    source["timestamp_utc"] = start.isoformat()
    db.storage.execute(
        "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
        "UPSERT INTO source_events (device_id,timestamp_utc,id,payload) VALUES ('fixture',$at,'imported',$payload);",
        {"$at": int(start.timestamp()), "$payload": json.dumps(source, sort_keys=True)},
    )
    db.telemetry.write_window(device_id="fixture", data_type="raw_events", start=start,
                              end=start + timedelta(minutes=30), events=[event])
    assert db.get_app_meta("telemetry-day:2026-01-01") is None
    assert db.list_source_events(start, start + timedelta(days=1)) == [event]


@pytest.mark.ydb
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


@pytest.mark.ydb
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

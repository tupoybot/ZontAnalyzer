from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.adapters.ydb.database import YdbDatabase
from zont_analyzer.adapters.ydb.telemetry import TelemetryRepository
from zont_analyzer.domain import SourceEvent, TelemetryPoint

START = datetime(2026, 1, 1, tzinfo=UTC)


def point(seconds: int = 0, value: float | None = 20, text: str | None = None) -> TelemetryPoint:
    return TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room", metric_key="temperature",
                          timestamp_utc=START + timedelta(seconds=seconds), value_num=value, value_text=text)


@pytest.mark.ydb
def test_window_repeat_boundaries_and_precision(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    args = dict(device_id="fixture", data_type="history", start=START, end=START + timedelta(hours=1))
    points = [point(), point(1, None, ""), point(2, 0), point(3600, 21.123456789)]
    assert repo.write_window(**args, points=points) == 4
    assert repo.write_window(**args, points=points) == 4
    assert len(repo.list_series()) == 1
    series = repo.list_series()[0]["id"]
    records = repo.read_samples(series, START, START + timedelta(hours=1))
    assert len(records) == 3
    assert records[1]["value_num"] is None and records[1]["value_text"] == ""
    assert records[2]["value_num"] == 0
    boundary = repo.read_samples(series, START + timedelta(hours=1), START + timedelta(hours=2))
    assert boundary[0]["value_num"] == 21.123456789
    assert repo.get_cursor("fixture", "history") == START + timedelta(hours=1)
    assert repo.get_cursor("fixture", "events") is None
    snapshot = repo.read_period("fixture", START, START + timedelta(hours=1))
    repo.write_window(**args, points=points)
    assert repo.read_period("fixture", START, START + timedelta(hours=1))["revision"] == snapshot["revision"]
    # An invalid response cannot advance coverage/cursor or commit some of its points.
    with pytest.raises(ValueError):
        repo.write_window(**args, points=[point(2), point(3601)])
    assert len(repo.read_samples(series, START, START + timedelta(hours=1))) == 3


@pytest.mark.ydb
def test_coverage_empty_errors_and_source_horizon(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    for hour, state in [(0, "empty"), (1, "failed"), (2, "complete")]:
        repo.write_window(device_id="fixture", data_type="history", start=START + timedelta(hours=hour),
                          end=START + timedelta(hours=hour + 1), state=state)
    repo.write_window(device_id="fixture", data_type="history", start=START,
                      end=START + timedelta(hours=1), state="failed")
    missing = repo.missing_intervals("fixture", "history", START, START + timedelta(hours=4),
                                     now=START + timedelta(days=90, hours=2))
    assert missing == [(int((START + timedelta(hours=1)).timestamp()),
                        int((START + timedelta(hours=2)).timestamp()), "unavailable"),
                       (int((START + timedelta(hours=3)).timestamp()),
                        int((START + timedelta(hours=4)).timestamp()), "fetch")]
    assert repo.coverage("fixture", "history", START, START + timedelta(hours=1))[0]["state"] == "empty"


@pytest.mark.ydb
def test_concurrent_series_identity_and_late_data(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)

    def write(n: int) -> int:
        return repo.write_window(device_id="fixture", data_type="history", start=START,
                                 end=START + timedelta(hours=1), points=[point(n)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(write, range(4))) == [1] * 4
    assert len(repo.list_series()) == 1
    series = repo.list_series()[0]["id"]
    assert len(repo.read_samples(series, START, START + timedelta(hours=1))) == 4
    repo.write_window(device_id="fixture", data_type="history", start=START,
                      end=START + timedelta(hours=1), points=[point(1, 25)])
    assert repo.read_samples(series, START, START + timedelta(hours=1))[1]["value_num"] == 25


@pytest.mark.ydb
def test_catalogue_snapshot_deduplication(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    device = {"id": "fixture", "name": "Обезличенное устройство", "configured": False}
    repo.save_devices([device])
    repo.save_devices([device])
    assert repo.list_devices() == [device]
    assert len(ydb_database.execute("SELECT * FROM config_snapshots;")[0].rows) == 1
    entity = dict(entity_id="entity", device_id="fixture", source_type="sensor", external_id="1",
                  display_name="Room", role="room_temperature", unit="C", confidence=1)
    repo.upsert_entity(**entity)
    with pytest.raises(ValueError, match="identity"):
        repo.upsert_entity(**{**entity, "external_id": "2"})
    assert repo.list_entities("fixture")[0]["external_id"] == "1"


@pytest.mark.ydb
def test_late_event_revision_and_archive_survive_source_horizon(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    event = SourceEvent(id="source-event", device_id="fixture", event_type="restart", timestamp_utc=START)
    args = dict(device_id="fixture", data_type="events", start=START, end=START + timedelta(days=1))
    repo.write_window(**args, events=[event])
    first = repo.read_period("fixture", START, START + timedelta(days=7))
    event = event.model_copy(update={"timestamp_utc": START + timedelta(seconds=5), "duration_seconds": 15})
    repo.write_window(**args, events=[event])
    changed = repo.read_period("fixture", START, START + timedelta(days=30))
    assert changed["events"] == [event]
    assert changed["revision"] > first["revision"]
    assert not repo.missing_intervals("fixture", "events", START, START + timedelta(days=1),
                                     now=START + timedelta(days=180))


@pytest.mark.ydb
def test_period_snapshot_never_mixes_concurrent_windows(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)

    def write() -> None:
        for value in range(8):
            repo.write_window(device_id="fixture", data_type="history", start=START,
                              end=START + timedelta(hours=1), points=[point(0, value), point(1, value)])

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        for _ in range(8):
            snapshot = repo.read_period("fixture", START, START + timedelta(hours=1))
            rows = snapshot["samples"]
            if rows:
                assert len(rows) == 2
                assert rows[0]["value_num"] == rows[1]["value_num"]
        writer.result()


@pytest.mark.ydb
def test_long_period_pages_reuse_archive_and_reject_mid_scan_change(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    args = dict(device_id="fixture", data_type="history", start=START, end=START + timedelta(hours=1))
    repo.write_window(**args, points=[point(0), point(1), point(2)])
    pages = list(repo.period_pages("fixture", START, START + timedelta(days=100), page_size=2))
    assert [len(page["samples"]) for page in pages] == [2, 1]
    scan = repo.period_pages("fixture", START, START + timedelta(days=100), page_size=2)
    next(scan)
    repo.write_window(**args, points=[point(1, 22)])
    with pytest.raises(ValueError, match="changed"):
        next(scan)


@pytest.mark.ydb
def test_coverage_pagination_keeps_same_start_intervals(ydb_database: YdbDatabase) -> None:
    repo = TelemetryRepository(ydb_database)
    for seconds in (10, 20, 30):
        repo.write_window(device_id="fixture", data_type="history", start=START,
                          end=START + timedelta(seconds=seconds), state="empty")
    first = repo.coverage("fixture", "history", START, START + timedelta(hours=1), limit=2)
    rest = repo.coverage("fixture", "history", START, START + timedelta(hours=1), limit=2,
                         after=(first[-1]["started_at"], first[-1]["ended_at"]))
    assert len(first) == 2 and len(rest) == 1
    assert rest[0]["ended_at"] == int(START.timestamp()) + 30

"""Real YDB coverage of durable boundaries and their transaction contract."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tools.import_sqlite import _delete, _import_samples_page
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import Transaction, YdbDatabase
from zont_analyzer.adapters.ydb.sample_bounds import KEY, invalidate
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.domain import TelemetryPoint

START = datetime(2026, 1, 1, tzinfo=UTC)
pytestmark = pytest.mark.ydb


def write(db: Database, seconds: int, *, entity: str = "room", value: float = 20) -> None:
    db.telemetry.write_window(
        device_id="fixture", data_type="history", start=START - timedelta(days=1),
        end=START + timedelta(days=1), points=[TelemetryPoint(
            device_id="fixture", source_type="temperature", entity_id=entity, metric_key="temperature",
            timestamp_utc=START + timedelta(seconds=seconds), value_num=value,
        )],
    )


def test_empty_bounds_are_initialized_and_subsequent_reads_are_key_only(
    ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(ydb_database)
    assert db.get_app_meta(KEY) is None
    assert db.sample_bounds() == (None, None)
    assert db.get_app_meta(KEY) is not None

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("initialized bounds must not bootstrap again")

    monkeypatch.setattr(ydb_database, "transaction", forbidden)
    assert db.sample_bounds() == (None, None)


def test_late_new_and_identical_samples_preserve_global_bounds(
    ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(ydb_database)
    write(db, 10)
    write(db, -10, entity="outside")
    write(db, 30, entity="outside")
    expected = START - timedelta(seconds=10), START + timedelta(seconds=30)
    assert db.sample_bounds() == expected
    original = Transaction.execute
    bounds_writes = []

    def capture(tx: Transaction, query: str, parameters: Any = None) -> Any:
        if "UPSERT INTO app_meta" in query and parameters.get("$key") == KEY:
            bounds_writes.append(query)
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", capture)
    write(db, 30, entity="outside")
    assert not bounds_writes
    write(db, 10, value=21)
    assert len(bounds_writes) == 1
    assert db.earliest_sample_time() == expected[0]
    assert db.latest_sample_time() == expected[1]


@pytest.mark.parametrize("initialize_by_write", [False, True])
def test_legacy_bootstrap_includes_all_series_once(
    ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch, initialize_by_write: bool,
) -> None:
    db = Database(ydb_database)
    write(db, -100, entity="legacy-first")
    write(db, 100, entity="legacy-last")
    ydb_database.transaction(invalidate)
    queries = []
    original = Transaction.execute

    def capture(tx: Transaction, query: str, parameters: Any = None) -> Any:
        if "FROM telemetry_samples WHERE series_id=$id" in query:
            assert "LIMIT 1" in query
            queries.append(query)
        return original(tx, query, parameters)

    monkeypatch.setattr(Transaction, "execute", capture)
    if initialize_by_write:
        write(db, 0, entity="new")
    expected = START - timedelta(seconds=100), START + timedelta(seconds=100)
    assert db.sample_bounds() == expected
    assert len(queries) == (3 if initialize_by_write else 2)
    count = len(queries)
    assert db.sample_bounds() == expected
    assert len(queries) == count


def test_failed_window_rolls_back_bounds_and_samples(
    ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(ydb_database)
    write(db, 0)
    original = Transaction.execute

    def fail(tx: Transaction, query: str, parameters: Any = None) -> Any:
        if "UPSERT INTO publication_changes" in query:
            raise ValueError("injected failure after samples and bounds")
        return original(tx, query, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(Transaction, "execute", fail)
        with pytest.raises(ValueError, match="injected failure"):
            write(db, 100)
    assert db.sample_bounds() == (START, START)
    rows = ydb_database.execute("SELECT timestamp_utc FROM telemetry_samples;")[0].rows
    assert [row.timestamp_utc for row in rows] == [int(START.timestamp())]


def test_concurrent_first_read_and_writers_cannot_lose_extrema(ydb_database: YdbDatabase) -> None:
    db = Database(ydb_database)
    write(db, 0)
    ydb_database.transaction(invalidate)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(write, db, seconds, entity=entity)
                   for seconds, entity in [(-100, "first"), (100, "last")]]
        futures.append(pool.submit(db.sample_bounds))
        for future in futures:
            future.result()
    assert db.sample_bounds() == (START - timedelta(seconds=100), START + timedelta(seconds=100))


def test_bulk_import_and_deletion_invalidate_but_unchanged_import_does_not(
    ydb_database: YdbDatabase,
) -> None:
    db = Database(ydb_database)
    write(db, 0)
    series_id = db.list_series()[0]["id"]
    timestamp = int((START + timedelta(seconds=100)).timestamp())
    page = [{"series_id": series_id, "timestamp_utc": timestamp, "value_num": 21.,
             "value_text": None, "quality": "valid", "ingested_at": START.isoformat()}]
    pk = ("series_id", "timestamp_utc")
    assert _import_samples_page(ydb_database, page, pk) == 1
    assert db.get_app_meta(KEY) is None
    assert db.sample_bounds() == (START, START + timedelta(seconds=100))
    persisted = db.get_app_meta(KEY)
    assert _import_samples_page(ydb_database, page, pk) == 0
    assert db.get_app_meta(KEY) == persisted
    ydb_database.transaction(lambda tx: _delete(
        tx, "telemetry_samples", {"series_id": series_id, "timestamp_utc": timestamp},
    ))
    assert db.get_app_meta(KEY) is None
    assert db.sample_bounds() == (START, START)


def test_old_release_writes_are_repaired_by_reads_and_new_writers(
    ydb_database: YdbDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(ydb_database)
    write(db, 0)
    series_id = db.list_series()[0]["id"]

    def old_write(seconds: int) -> None:
        def save(tx: Transaction) -> None:
            tx.execute(
                "DECLARE $id AS Int64; DECLARE $at AS Int64; "
                "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num,quality) "
                "VALUES ($id,$at,20.,'valid');",
                {"$id": series_id, "$at": int((START + timedelta(seconds=seconds)).timestamp())},
            )
            bump_revision(tx, "telemetry:fixture", publish=False)

        ydb_database.transaction(save)

    old_write(-100)
    assert db.sample_bounds() == (START - timedelta(seconds=100), START)
    old_write(100)
    write(db, 10)
    expected = START - timedelta(seconds=100), START + timedelta(seconds=100)
    assert db.sample_bounds() == expected

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("fresh revision metadata must not bootstrap")

    monkeypatch.setattr(ydb_database, "transaction", forbidden)
    assert db.sample_bounds() == expected

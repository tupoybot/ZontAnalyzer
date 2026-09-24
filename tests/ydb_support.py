"""Isolated real YDB application databases for local and CI tests."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.domain import SourceEvent, TelemetryPoint
from zont_analyzer.runtime import Runtime


@dataclass(eq=False)
class _Slot:
    config: YdbConfig
    pooled: bool
    leased: bool = True
    quarantined: bool = False


_slots: list[_Slot] = []
_leases: list[_Slot] = []
_clients: list[YdbDatabase] = []
_closed_clients: set[int] = set()
_tracking_clients = False
_runtime_databases: dict[Path, Database] = {}
_active_test: str | None = None
_ydb_allowed = False
_pool_counts = {"created": 0, "reused": 0, "reset": 0, "quarantined": 0}
_SCHEMA_HASH = hashlib.sha256(json.dumps(TABLES, sort_keys=True).encode()).hexdigest()
_CANONICAL_METADATA = {"schema_version": "2", "schema_hash": _SCHEMA_HASH}
_DRAIN_SECONDS = 10.0


def begin_test(nodeid: str, *, ydb_allowed: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    global _active_test, _ydb_allowed, _tracking_clients
    if _active_test is not None:
        raise RuntimeError(f"YDB fixture still active for {_active_test}")
    _active_test = nodeid
    _ydb_allowed = ydb_allowed
    _tracking_clients = ydb_allowed
    if ydb_allowed:
        original_init = YdbDatabase.__init__
        original_close = YdbDatabase.close

        def tracked_init(client: YdbDatabase, config: YdbConfig) -> None:
            original_init(client, config)
            if _tracking_clients:
                _clients.append(client)

        def tracked_close(client: YdbDatabase) -> None:
            original_close(client)
            _closed_clients.add(id(client))

        monkeypatch.setattr(YdbDatabase, "__init__", tracked_init)
        monkeypatch.setattr(YdbDatabase, "close", tracked_close)


def _require_marked() -> None:
    if _active_test is None or not _ydb_allowed:
        raise AssertionError("YDB fixture access requires an explicit pytest.mark.ydb on the test")


def _test_config(namespace: str) -> YdbConfig:
    endpoint = os.environ.get("YDB_TEST_ENDPOINT")
    if not endpoint:
        if os.environ.get("ZONT_SKIP_YDB_TESTS") == "1":
            pytest.skip("isolated YDB is unavailable in this Codex Cloud fixture check")
        raise RuntimeError("YDB_TEST_ENDPOINT is required: run the Docker test workflow")
    return YdbConfig(endpoint, os.environ.get("YDB_TEST_DATABASE", "/local"), namespace, True)


def _lease(*, fresh_schema: bool = False, prefix: str = "app_test_") -> YdbDatabase:
    _require_marked()
    slot = next((item for item in _slots if item.pooled and not item.leased and not item.quarantined), None)
    if fresh_schema or slot is None:
        slot = _Slot(_test_config(prefix + uuid.uuid4().hex), pooled=not fresh_schema)
        _slots.append(slot)
        _pool_counts["created"] += 1
        initialize = True
    else:
        slot.leased = True
        _pool_counts["reused"] += 1
        initialize = False
    _leases.append(slot)
    try:
        client = YdbDatabase(slot.config)
        if initialize:
            client.initialize()
        return client
    except BaseException:
        _quarantine(slot)
        raise


def make_ydb_database(*, fresh_schema: bool = False) -> YdbDatabase:
    """Raw storage fixture using the same exclusive namespace pool."""
    return _lease(fresh_schema=fresh_schema, prefix="test_")


def make_runtime(data_dir: Path) -> Runtime:
    """Application fixture; repeat calls for the same path share this test's DB."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir not in _runtime_databases:
        _runtime_databases[data_dir] = make_database(data_dir)
    return Runtime(LoadedConfig(config=AppConfig(), secrets=Secrets(), config_path=None,
                                data_dir=data_dir, sources={}), _runtime_databases[data_dir])


def make_database(tmp_path: Path, *, fresh_schema: bool = False) -> Database:
    del tmp_path
    return Database(_lease(fresh_schema=fresh_schema))


def pool_statistics() -> dict[str, int]:
    """JSON-safe counters, including in workers without a YDB endpoint."""
    return {
        "schema_creations": _pool_counts["created"],
        "schema_reuses": _pool_counts["reused"],
        "schema_resets": _pool_counts["reset"],
        "schema_quarantines": _pool_counts["quarantined"],
    }


def _quarantine(slot: _Slot) -> None:
    if not slot.quarantined:
        slot.quarantined = True
        _pool_counts["quarantined"] += 1


def _test_thread(thread: threading.Thread) -> bool:
    name = thread.name
    return (
        name == "ai-model-review"
        or name.startswith("regenerate-")
        or name == "feedback-http"
        or name.endswith("(serve_forever)")
        or name.endswith("(process_request_thread)")
    )


def _drain_background_threads() -> None:
    deadline = time.monotonic() + _DRAIN_SECONDS
    while True:
        threads = [thread for thread in threading.enumerate()
                   if thread is not threading.current_thread() and _test_thread(thread)]
        if not threads:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("YDB fixture background threads did not stop before reset: "
                               + ", ".join(sorted({thread.name for thread in threads})))
        for thread in threads:
            thread.join(min(remaining, 0.2))


def _metadata(client: YdbDatabase) -> dict[str, str]:
    rows = client.execute("SELECT name,value FROM metadata;")[0].rows
    return {str(row.name): str(row.value) for row in rows}


def _reset_slot(slot: _Slot) -> None:
    client = YdbDatabase(slot.config)
    try:
        actual = {entry.name for entry in client.driver.scheme_client.list_directory(client.path).children}
        if actual != set(TABLES):
            raise AssertionError(f"YDB test namespace table set changed: {slot.config.namespace}")
        current = _metadata(client)
        for key, value in _CANONICAL_METADATA.items():
            if current.get(key) != value:
                raise AssertionError(f"YDB test namespace {key} changed: {slot.config.namespace}")

        def clear(tx) -> None:
            deletes = " ".join(f"DELETE FROM `{name}`;" for name in TABLES)
            query = (
                "DECLARE $hash AS Utf8; " + deletes
                + " UPSERT INTO metadata (name,value) VALUES ('schema_version','2'),('schema_hash',$hash);"
            )
            tx.execute(query, {"$hash": _SCHEMA_HASH})

        client.transaction(clear)
        if _metadata(client) != _CANONICAL_METADATA:
            raise AssertionError(f"YDB test namespace metadata reset failed: {slot.config.namespace}")
    finally:
        client.close()


def cleanup_databases() -> None:
    """Release this test's leases, or quarantine them on any cleanup failure."""
    global _tracking_clients
    errors: list[BaseException] = []
    try:
        if _leases:
            try:
                _drain_background_threads()
            except BaseException as exc:
                errors.append(exc)
                for slot in _leases:
                    _quarantine(slot)
        _tracking_clients = False
        for client in reversed(_clients):
            if id(client) in _closed_clients:
                continue
            try:
                client.close()
            except BaseException as exc:
                errors.append(exc)
                for slot in _leases:
                    _quarantine(slot)
        if not errors:
            for slot in _leases:
                if slot.pooled and not slot.quarantined:
                    try:
                        _reset_slot(slot)
                        _pool_counts["reset"] += 1
                    except BaseException as exc:
                        errors.append(exc)
                        _quarantine(slot)
        for slot in _leases:
            slot.leased = False
        if errors:
            raise RuntimeError("YDB test namespace cleanup failed; leased slots quarantined") from errors[0]
    finally:
        _clients.clear()
        _closed_clients.clear()
        _leases.clear()
        _runtime_databases.clear()
        # Manual cleanup within an isolation regression keeps tracking active.
        _tracking_clients = _active_test is not None and _ydb_allowed


def end_test() -> None:
    global _active_test, _ydb_allowed, _tracking_clients
    try:
        cleanup_databases()
    finally:
        _active_test = None
        _ydb_allowed = False
        _tracking_clients = False


def _drop_slot(slot: _Slot) -> None:
    client = YdbDatabase(slot.config)
    try:
        for name in reversed(TABLES):
            client.execute(f"DROP TABLE IF EXISTS `{name}`;")
        client.driver.scheme_client.remove_directory(client.path)
    finally:
        client.close()


def cleanup_session() -> None:
    errors: list[BaseException] = []
    while _slots:
        slot = _slots.pop()
        try:
            _drop_slot(slot)
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(f"failed to remove {len(errors)} YDB test namespaces") from errors[0]


def seed_samples(db: Database, points: list[TelemetryPoint], roles: dict[str, str] | None = None) -> int:
    """Insert bounded synthetic fixture windows without claiming ZONT coverage."""
    by_device: dict[str, list[TelemetryPoint]] = defaultdict(list)
    for point in points:
        by_device[point.device_id].append(point)
    for device_id, rows in by_device.items():
        for offset in range(0, len(rows), 2000):
            page = rows[offset:offset + 2000]
            times = [point.timestamp_utc for point in page]
            db.telemetry.write_window(
                device_id=device_id, data_type="fixture-samples",
                start=min(times) - timedelta(seconds=1), end=max(times) + timedelta(seconds=1),
                points=page, roles=roles,
            )
    return len(points)


def seed_events(db: Database, events: list[SourceEvent]) -> int:
    by_device: dict[str, list[SourceEvent]] = defaultdict(list)
    for event in events:
        by_device[event.device_id].append(event)
    for device_id, rows in by_device.items():
        db.telemetry.save_devices([{"device_id": device_id}])
        for offset in range(0, len(rows), 2000):
            page = rows[offset:offset + 2000]
            times = [event.timestamp_utc for event in page]
            db.telemetry.write_window(
                device_id=device_id, data_type="fixture-events",
                start=min(times) - timedelta(seconds=1), end=max(times) + timedelta(seconds=1),
                events=page,
            )
    return len(events)


def delete_samples(db: Database, start: datetime, end: datetime) -> int:
    """Delete fixture samples using native keys and invalidate touched days."""
    deleted = 0
    for series in db.list_series():
        series_id = int(series["id"])
        device_id = str(series["device_id"])

        def remove(tx, series_id: int = series_id, device_id: str = device_id):
            rows = tx.execute(
                "DECLARE $id AS Int64; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                "SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id "
                "AND timestamp_utc >= $start AND timestamp_utc < $end "
                "ORDER BY timestamp_utc LIMIT 1000;",
                {"$id": series_id, "$start": int(start.timestamp()), "$end": int(end.timestamp())},
            )[0].rows
            if not rows:
                return 0
            tx.execute(
                "DECLARE $id AS Int64; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                "DELETE FROM telemetry_samples WHERE series_id=$id "
                "AND timestamp_utc >= $start AND timestamp_utc <= $end;",
                {"$id": series_id, "$start": int(start.timestamp()), "$end": int(rows[-1].timestamp_utc)},
            )
            revision = bump_revision(tx, "telemetry:" + device_id)
            days = {datetime.fromtimestamp(row.timestamp_utc, UTC).date().isoformat() for row in rows}
            for day in days:
                tx.execute(
                    "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                    "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                    {"$key": f"telemetry-day:{day}", "$value": f"fixture-delete:{device_id}:{revision}"},
                )
            return len(rows)

        while count := db.storage.transaction(remove):
            deleted += count
    return deleted

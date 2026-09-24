"""Isolated real YDB application databases for local and CI tests."""
from __future__ import annotations

import os
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.adapters.ydb.schema import TABLES
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.domain import SourceEvent, TelemetryPoint
from zont_analyzer.runtime import Runtime

_databases: list[Database] = []
_runtime_databases: dict[Path, Database] = {}


def make_runtime(data_dir: Path) -> Runtime:
    """Application fixture with a real YDB namespace and no developer secrets."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if data_dir not in _runtime_databases:
        _runtime_databases[data_dir] = make_database(data_dir)
    return Runtime(LoadedConfig(config=AppConfig(), secrets=Secrets(), config_path=None,
                                data_dir=data_dir, sources={}), _runtime_databases[data_dir])


def make_database(tmp_path: Path) -> Database:
    del tmp_path
    endpoint = os.environ.get("YDB_TEST_ENDPOINT")
    if not endpoint:
        if os.environ.get("ZONT_SKIP_YDB_TESTS") == "1":
            pytest.skip("isolated YDB is unavailable in this Codex Cloud fixture check")
        raise RuntimeError("YDB_TEST_ENDPOINT is required: run the Docker test workflow")
    db = Database(YdbConfig(endpoint, os.environ.get("YDB_TEST_DATABASE", "/local"),
                            "app_test_" + uuid.uuid4().hex, True))
    db.initialize()
    _databases.append(db)
    return db


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


def cleanup_databases() -> None:
    _runtime_databases.clear()
    while _databases:
        db = _databases.pop()
        try:
            for name in reversed(TABLES):
                db.storage.execute(f"DROP TABLE IF EXISTS `{name}`;")
            db.storage.driver.scheme_client.remove_directory(db.storage.path)
        finally:
            db.close()

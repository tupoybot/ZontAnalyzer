from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.sqlite.database import ReportRow
from zont_analyzer.domain import TelemetryPoint

EMPTY_REVISION = hashlib.sha256(b"[]").hexdigest()


def _point(timestamp: datetime, *, entity: str = "room", value: float = 21.0) -> TelemetryPoint:
    return TelemetryPoint(
        device_id="1",
        source_type="synthetic",
        entity_id=entity,
        metric_key="temperature",
        timestamp_utc=timestamp,
        value_num=value,
        unit="°C",
    )


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.sqlite3")
    database.initialize()
    return database


def test_period_revision_is_exact_at_utc_boundaries_and_keeps_empty_sentinel(tmp_path: Path) -> None:
    database = _database(tmp_path)
    # September 7 local day in Samara (UTC+4).
    start = datetime(2026, 9, 6, 20, tzinfo=UTC)
    end = start + timedelta(days=1)

    assert database.period_data_revision(start, end) == EMPTY_REVISION

    database.upsert_samples([_point(start + timedelta(hours=1))])
    revision = database.period_data_revision(start, end)
    assert revision.startswith("telemetry-v2:")

    # It shares a UTC day with the period, but is outside [start, end).
    database.upsert_samples([
        _point(start - timedelta(seconds=1), entity="before", value=5.0),
        _point(end, entity="boundary", value=5.0),
        _point(end + timedelta(hours=1), entity="outside", value=5.0),
    ])
    assert database.period_data_revision(start, end) == revision
    database.upsert_samples([_point(end - timedelta(seconds=1), entity="inside", value=5.0)])
    assert database.period_data_revision(start, end) != revision


def test_warm_revision_cache_does_not_scan_telemetry_and_ignores_identical_upsert(tmp_path: Path) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 7, tzinfo=UTC)
    end = start + timedelta(days=1)
    point = _point(start)
    database.upsert_samples([point])
    expected = database.period_data_revision(start, end)
    database.upsert_samples([point])
    statements = []
    event.listen(database.engine, 'before_cursor_execute',
                 lambda connection, cursor, statement, parameters, context, executemany: statements.append(statement))
    assert database.period_data_revision(start, end) == expected
    assert not any('telemetry_samples' in statement for statement in statements)


def test_period_revision_includes_all_sample_content_and_deletions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    point = _point(start + timedelta(hours=1))
    database.upsert_samples([point])
    original = database.period_data_revision(start, end)

    database.upsert_samples([point.model_copy(update={"value_num": 22.0})])
    corrected = database.period_data_revision(start, end)
    assert corrected != original

    database.upsert_samples([
        point.model_copy(update={"entity_id": "state", "value_num": None, "value_text": "on", "quality": "invalid"})
    ])
    with_text_and_quality = database.period_data_revision(start, end)
    assert with_text_and_quality != corrected

    assert database.delete_samples(start, end) == 2
    assert database.period_data_revision(start, end) != with_text_and_quality


def test_legacy_period_revision_remains_available_for_lazy_rollout(tmp_path: Path) -> None:
    database = _database(tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    end = start + timedelta(days=1)

    before = database.legacy_period_data_revision(start, end)
    database.upsert_samples([_point(start)])
    assert database.legacy_period_data_revision(start, end) != before


def test_upgrade_report_telemetry_revision_is_compare_and_swap_metadata_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    generated_at = datetime(2026, 9, 2, tzinfo=UTC)
    original = {
        "id": "report-1",
        "context": {
            "input_revision": {"telemetry": "legacy", "weather": "unchanged"},
            "ai": {"response": "preserve"},
        },
        "metrics": [{"name": "preserve"}],
    }
    with database.session() as session:
        session.add(ReportRow(
            id="report-1", kind="daily", period_start=0, period_end=1,
            canonical_json=json.dumps(original), generated_at=generated_at,
            algorithm_version="test",
        ))

    assert database.upgrade_report_telemetry_revision("report-1", "legacy", "telemetry-v2:exact")
    with database.session() as session:
        row = session.get(ReportRow, "report-1")
        assert row is not None
        stored_generated_at = row.generated_at
        stored = json.loads(row.canonical_json)
    assert stored["context"]["input_revision"] == {"telemetry": "telemetry-v2:exact", "weather": "unchanged"}
    assert stored["context"]["ai"] == original["context"]["ai"]
    assert stored["metrics"] == original["metrics"]
    assert not database.upgrade_report_telemetry_revision("report-1", "legacy", "other")
    with database.session() as session:
        row = session.get(ReportRow, "report-1")
        assert row is not None
        assert row.generated_at == stored_generated_at

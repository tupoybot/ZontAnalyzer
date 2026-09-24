from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.ydb_support import make_database
from zont_analyzer.application.collection import CollectionService
from zont_analyzer.config import AppConfig


@pytest.mark.ydb
def test_empty_coverage_resume_and_source_failure_are_independent(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture"}])
    config = AppConfig()
    config.zont.history_data_types = ["temperature"]
    client = Mock()
    client.load_history.return_value = [{"device_id": "fixture", "ok": True}]
    client.normalize_history.return_value = ([], {})
    client.load_events.return_value = []
    client.normalize_events.return_value = []
    service = CollectionService(db, client, config)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    start = end - timedelta(hours=1)
    first = service.ensure_period(start, end, now=end, max_requests=2)
    assert first["pending"] and not first["complete"]
    second = service.ensure_period(start, end, now=end, max_requests=2)
    assert second["complete"]
    third = service.ensure_period(start, end, now=end)
    assert third["complete"] and third["requests"] == 0
    assert client.load_history.call_count == 2
    assert client.load_events.call_count == 2
    client.load_history.side_effect = RuntimeError("source unavailable")
    fourth = service.ensure_period(end, end + timedelta(minutes=30), now=end + timedelta(minutes=30))
    assert fourth["failed_windows"] == 1 and not fourth["complete"]
    assert db.get_cursor("fixture", "temperature") == end
    assert db.get_cursor("fixture", "raw_events") == end + timedelta(minutes=30)


@pytest.mark.ydb
def test_unavailable_archive_gap_does_not_call_source(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture"}])
    config = AppConfig()
    config.zont.history_data_types = ["temperature"]
    client = Mock()
    service = CollectionService(db, client, config)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    result = service.ensure_period(end - timedelta(days=1), end, now=end + timedelta(days=100))
    assert result["requests"] == 0 and result["unavailable_intervals"] == 2
    client.load_history.assert_not_called()
    client.load_events.assert_not_called()


@pytest.mark.ydb
def test_oversized_response_learns_bound_across_invocations(tmp_path: Path) -> None:
    from zont_analyzer.domain import TelemetryPoint

    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture"}])
    config = AppConfig()
    config.zont.history_data_types = ["temperature"]
    client = Mock()
    client.load_history.side_effect = lambda **kw: [{"device_id": "fixture", "ok": True,
                                                   "start": kw["start"], "end": kw["end"]}]
    client.normalize_history.side_effect = lambda response: ([
        TelemetryPoint(device_id="fixture", source_type="temperature", entity_id=entity,
                       metric_key="temperature", timestamp_utc=response["start"] + timedelta(seconds=second),
                       value_num=20)
        for second in range(int((response["end"] - response["start"]).total_seconds()))
        for entity in ("room", "outdoor")
    ], {})
    service = CollectionService(db, client, config)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    start = end - timedelta(minutes=30)
    first = service.ensure_period(start, end, now=end, max_requests=1)
    assert first["pending"] and first["samples"] == 0
    assert db.get_app_meta("collection-window-seconds:fixture:temperature") == "900"
    second = service.ensure_period(start, end, now=end, max_requests=1)
    assert second["requests"] == 1  # Events get a turn before retrying telemetry.
    third = service.ensure_period(start, end, now=end, max_requests=1)
    assert third["samples"] == 1800
    assert db.get_cursor("fixture", "temperature") == start + timedelta(minutes=15)


@pytest.mark.ydb
def test_small_budget_preserves_source_fairness_across_restart(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture"}])
    config = AppConfig()
    config.zont.history_data_types = ["temperature", "voltage"]
    client = Mock()
    client.load_history.side_effect = RuntimeError("unavailable")
    client.load_events.return_value = []
    client.normalize_events.return_value = []
    end = datetime(2026, 1, 2, tzinfo=UTC)
    start = end - timedelta(minutes=30)
    for _ in range(3):
        result = CollectionService(db, client, config).ensure_period(
            start, end, now=end, max_requests=1,
        )
        assert result["requests"] == 1
    assert client.load_history.call_count == 2
    assert client.load_events.call_count == 1
    assert db.get_cursor("fixture", "temperature") is None
    assert db.get_cursor("fixture", "raw_events") == end

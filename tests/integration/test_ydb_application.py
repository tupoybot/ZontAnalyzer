from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.ydb_support import make_database
from zont_analyzer.domain import TelemetryPoint


def test_daily_comparison_preserves_historical_algorithm_versions(tmp_path: Path) -> None:
    from tests.integration.test_ydb_reports import _report
    from zont_analyzer.application.comparison_context import daily_history

    db = make_database(tmp_path)
    original = _report(report_id="historical-v1")
    original.algorithm_version = "report-v1"
    recent = original.model_copy(deep=True, update={
        "id": "historical-v2", "algorithm_version": "report-v2",
        "generated_at": original.generated_at + timedelta(hours=1),
    })
    db.save_report(original, "original")
    db.save_report(recent, "recent")
    assert [item.id for item in db.daily_reports(original.period_start, original.period_end)] == [recent.id]
    assert [item.id for item in daily_history(db, original.period_start, original.period_end)] == [
        original.id, recent.id,
    ]


def test_application_observations_preserve_unknowns_and_previous(tmp_path: Path) -> None:
    db = make_database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    db.save_devices([{"device_id": "fixture", "name": "Fixture"}])
    points = [TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room",
                             metric_key="temperature", timestamp_utc=start + timedelta(seconds=i),
                             value_num=value, quality=quality)
              for i, value, quality in [(0, 20.0, "valid"), (1, None, "invalid"), (2, 0.0, "valid")]]
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start,
                              end=start + timedelta(hours=1), points=points)
    series = db.list_series()[0]["id"]
    observations = db.fetch_numeric_observations(series, start + timedelta(seconds=1),
                                                 start + timedelta(hours=1), include_previous=True)
    assert [value for _, value in observations] == [20.0, None, 0.0]
    assert db.latest_sample_time() == start + timedelta(seconds=2)
    assert db.earliest_sample_time() == start
    assert db.list_devices()[0]["raw"]["name"] == "Fixture"
    revision = db.period_data_revision(start, start + timedelta(hours=1))
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start,
                              end=start + timedelta(hours=1), points=points)
    assert db.period_data_revision(start, start + timedelta(hours=1)) == revision


def test_imported_markers_only_change_with_semantic_data(tmp_path: Path) -> None:
    from tests.integration.test_ydb_reports import _report

    db = make_database(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    db.set_app_meta("telemetry-day:2026-01-01", "imported-marker")
    original = db.legacy_period_data_revision(start, end)
    point = TelemetryPoint(device_id="fixture", source_type="temperature", entity_id="room",
                           metric_key="temperature", timestamp_utc=start, value_num=20)
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end, points=[point])
    changed = db.legacy_period_data_revision(start, end)
    assert changed != original
    db.telemetry.write_window(device_id="fixture", data_type="history", start=start, end=end, points=[point])
    assert db.legacy_period_data_revision(start, end) == changed
    report = _report()
    report.context["input_revision"] = {"telemetry": original}
    db.save_report(report, "original rendered text")
    assert db.upgrade_report_telemetry_revision(report.id, original, changed)
    assert not db.upgrade_report_telemetry_revision(report.id, original, "stale")
    assert db.report(report.id).context["input_revision"]["telemetry"] == changed
    assert db.seed_source_event_report_baselines() == 1
    assert db.seed_source_event_report_baselines() == 0


def test_incompatible_schema_version_is_rejected_before_creation(tmp_path: Path) -> None:
    import pytest

    db = make_database(tmp_path)
    db.storage.execute("DROP TABLE `data_gaps`;")
    db.storage.execute("UPSERT INTO metadata (name,value) VALUES ('schema_version','1');")
    with pytest.raises(ValueError, match="unsupported YDB schema version"):
        db.initialize()
    tables = {entry.name for entry in db.storage.driver.scheme_client.list_directory(db.storage.path).children}
    assert "data_gaps" not in tables


def test_incompatible_schema_hash_is_rejected_before_creation(tmp_path: Path) -> None:
    import pytest

    db = make_database(tmp_path)
    db.storage.execute("DROP TABLE `data_gaps`;")
    db.storage.execute("UPSERT INTO metadata (name,value) VALUES ('schema_hash','incompatible');")
    with pytest.raises(ValueError, match="explicit migration"):
        db.initialize()
    tables = {entry.name for entry in db.storage.driver.scheme_client.list_directory(db.storage.path).children}
    assert "data_gaps" not in tables

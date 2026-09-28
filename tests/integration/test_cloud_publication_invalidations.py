"""Publication invalidation regressions driven by real YDB adapter writes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.test_incremental_publication import _daily_reports, _runtime
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application import publication
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import SourceEvent, TelemetryPoint


def _write_point(runtime, at: datetime, value: float) -> None:
    point = TelemetryPoint(
        device_id="device", source_type="temperature", entity_id="room",
        metric_key="temperature", timestamp_utc=at, value_num=value,
    )
    runtime.db.telemetry.write_window(
        device_id="device", data_type="history", start=at, end=at + timedelta(hours=1),
        points=[point],
    )


@pytest.mark.ydb
def test_discovery_volatility_is_ignored_but_equipment_profile_change_renders(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    first = {"boiler_model": {"value": "Model A", "source": "fixture:equipment"}}
    runtime.db.save_devices([{"device_id": "device", "online": True, "_equipment": first}])
    _daily_reports(runtime, 1)
    publication.publish_reports(runtime)

    # Each call gets a new discovered_at, and online state is volatile. Neither
    # changes the equipment facts rendered into the reports.
    runtime.db.save_devices([{"device_id": "device", "online": False, "_equipment": first}])
    assert publication.publish_reports(runtime)["rendered_reports"] == 0

    changed = {"boiler_model": {"value": "Model B", "source": "fixture:equipment"}}
    runtime.db.save_devices([{"device_id": "device", "online": False, "_equipment": changed}])
    assert publication.publish_reports(runtime)["rendered_reports"] == 1


@pytest.mark.ydb
def test_real_telemetry_targets_changed_report_hour_and_ignores_outside_or_replay(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 3)
    outside = reports[-1].period_end + timedelta(days=2)
    # Establish the series before the clean publication checkpoint.
    _write_point(runtime, outside, 18.0)
    publication.publish_reports(runtime)

    _write_point(runtime, outside + timedelta(hours=2), 19.0)
    assert publication.publish_reports(runtime)["rendered_reports"] == 0

    target = reports[0].period_start + timedelta(hours=2)
    _write_point(runtime, target, 20.0)
    assert publication.publish_reports(runtime)["rendered_reports"] == 1

    _write_point(runtime, target, 20.0)
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_moved_source_event_journals_both_old_and_new_utc_hours(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    old = datetime(2026, 9, 1, 1, 20, tzinfo=UTC)
    new = datetime(2026, 9, 1, 3, 40, tzinfo=UTC)
    event = SourceEvent(id="event-1", device_id="device", event_type="burner", timestamp_utc=old)
    runtime.db.telemetry.write_window(
        device_id="device", data_type="raw_events", start=old.replace(hour=0),
        end=old.replace(hour=5), events=[event],
    )
    checkpoint = runtime.db.source_revision()

    moved = event.model_copy(update={"timestamp_utc": new})
    runtime.db.telemetry.write_window(
        device_id="device", data_type="raw_events", start=old.replace(hour=0),
        end=old.replace(hour=5), events=[moved],
    )
    changes = PublicationRepository(runtime.db.storage).changes_since(checkpoint, runtime.db.source_revision())
    hours = {str(change["identifier"]) for change in changes if change["scope"] == "telemetry"}
    assert hours == {"2026-09-01T01", "2026-09-01T03"}


@pytest.mark.ydb
def test_real_calibration_telemetry_still_queues_archive_in_batches(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    owner = OwnerContextStore(runtime.db)
    owner.update_gas(reports[0].id, {"value_m3": "100"})
    owner.update_gas(reports[1].id, {"value_m3": "102"})

    outside_hour = reports[0].period_start - timedelta(days=2)
    _write_point(runtime, outside_hour, 17.0)
    publication.publish_reports(runtime)

    # The changed hour affects only the first report directly. The meter
    # calibration span should still queue the whole archive.
    calibration_hour = reports[0].period_start + timedelta(hours=12)
    _write_point(runtime, calibration_hour, 18.0)
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == len(reports) - 1

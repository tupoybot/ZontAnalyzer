from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.application import publication
from zont_analyzer.application.gas import GasService
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.runtime import Runtime


def _runtime(tmp_path: Path):
    loaded = LoadedConfig(config=AppConfig(), secrets=Secrets(), config_path=None,
                          data_dir=tmp_path, sources={})
    return Runtime(loaded, make_database(tmp_path))


def _daily_reports(runtime, count: int, *, start: date = date(2026, 8, 1)):
    analysis = runtime.analysis(no_ai=True)
    return [analysis.analyze_daily(start + timedelta(days=day), use_ai=False) for day in range(count)]


def _record_change(db, scope: str, identifier: str, payload: dict | None = None) -> None:
    def write(tx) -> None:
        revision = bump_revision(tx, "publication")
        tx.execute(
            "DECLARE $scope AS Utf8; DECLARE $identifier AS Utf8; DECLARE $revision AS Int64; "
            "DECLARE $payload AS Utf8; "
            "UPSERT INTO publication_changes (scope,identifier,revision,payload) "
            "VALUES ($scope,$identifier,$revision,$payload);",
            {"$scope": scope, "$identifier": identifier, "$revision": revision,
             "$payload": json.dumps(payload or {})},
        )

    db.storage.transaction(write)


@pytest.mark.ydb
def test_queue_events_survive_drain_and_count_transient_enqueue_render_cycles(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    first = publication.publish_reports(runtime, batch_size=1)
    assert first["queue_metrics"]["added"] == 4
    assert first["queue_metrics"]["completed"] == 1
    assert first["queue_metrics"]["requeued"] == 0
    started = first["queue_metrics"]["started_timestamp_seconds"]
    drained = publication.publish_reports(runtime, batch_size=8)
    assert drained["queue_metrics"] == {"started_timestamp_seconds": started, "added": 4,
                                         "completed": 4, "requeued": 0}
    _record_change(runtime.db, "render", reports[0].id)
    same_batch = publication.publish_reports(runtime)
    assert same_batch["pending_reports"] == 0
    assert same_batch["queue_metrics"] == {"started_timestamp_seconds": started, "added": 4,
                                            "completed": 5, "requeued": 1}
    state = PublicationRepository(runtime.db.storage).load_meta()["queue_metrics"]
    assert publication.publish_reports(runtime)["queue_metrics"] == same_batch["queue_metrics"]
    assert PublicationRepository(runtime.db.storage).load_meta()["queue_metrics"] == state


@pytest.mark.ydb
def test_tracking_activation_does_not_invent_historical_additions(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 3)
    publication.publish_reports(runtime, batch_size=1)
    runtime.db.storage.execute("DELETE FROM metadata WHERE name='publication:queue_metrics';")
    resumed = publication.publish_reports(runtime, batch_size=1)
    assert resumed["queue_metrics"]["added"] == 0
    assert resumed["queue_metrics"]["completed"] == 1
    assert resumed["queue_metrics"]["requeued"] == 0
    assert resumed["queue_metrics"]["started_timestamp_seconds"] > 0


@pytest.mark.ydb
def test_failed_render_does_not_commit_queue_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 1)
    original = publication.render_html

    def failed_render(*args, **kwargs):
        raise RuntimeError("render")

    monkeypatch.setattr(publication, "render_html", failed_render)
    with pytest.raises(RuntimeError, match="render"):
        publication.publish_reports(runtime)
    assert "queue_metrics" not in PublicationRepository(runtime.db.storage).load_meta()
    monkeypatch.setattr(publication, "render_html", original)
    retried = publication.publish_reports(runtime)
    assert retried["queue_metrics"]["added"] == retried["queue_metrics"]["completed"] == 1


@pytest.mark.ydb
def test_committed_metrics_recover_when_publisher_loses_its_result(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 1)
    original = PublicationRepository.save

    def lost_result(*args, **kwargs):
        assert original(*args, **kwargs)
        raise RuntimeError("publisher result lost")

    with monkeypatch.context() as patch:
        patch.setattr(PublicationRepository, "save", lost_result)
        with pytest.raises(RuntimeError, match="publisher result lost"):
            publication.publish_reports(runtime)
    recovered = publication.publish_reports(runtime)
    assert recovered["rendered_reports"] == 0
    assert recovered["queue_metrics"]["added"] == recovered["queue_metrics"]["completed"] == 1


@pytest.mark.ydb
def test_damaged_metrics_start_new_epoch_without_blocking_publication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 1)[0]
    first = publication.publish_reports(runtime)["queue_metrics"]
    runtime.db.storage.execute(
        "UPSERT INTO metadata(name,value) VALUES ('publication:queue_metrics','invalid-json');",
    )
    _record_change(runtime.db, "render", report.id)
    repaired = publication.publish_reports(runtime)["queue_metrics"]
    assert repaired["started_timestamp_seconds"] > first["started_timestamp_seconds"]
    assert repaired["added"] == 0
    assert repaired["completed"] == repaired["requeued"] == 1


@pytest.mark.ydb
def test_noop_does_not_recalculate_or_walk_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 1)
    publication.publish_reports(runtime)

    monkeypatch.setattr(GasService, "refresh", lambda *args: pytest.fail("refresh on no-op"))
    monkeypatch.setattr(publication, "render_html", lambda *args, **kwargs: pytest.fail("render on no-op"))
    monkeypatch.setattr(PublicationRepository, "load",
                        lambda *args, **kwargs: pytest.fail("full index load on no-op"))
    monkeypatch.setattr(PublicationRepository, "canonical_reports",
                        lambda *args, **kwargs: pytest.fail("canonical report walk on no-op"))

    result = publication.publish_reports(runtime)
    assert result["rendered_reports"] == 0
    assert result["pending_reports"] == 0


@pytest.mark.ydb
@pytest.mark.parametrize("view", ["gas_validation", "renderers"])
def test_view_update_republishes_in_batches_without_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, view: str,
) -> None:
    from zont_analyzer.reports import gas_validation, renderers

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 3)
    publication.publish_reports(runtime)
    before = {report.id: runtime.db.report(report.id).model_dump_json() for report in reports}
    monkeypatch.setattr({"gas_validation": gas_validation, "renderers": renderers}[view],
                        "VIEW_VERSION", "test-view-update")
    monkeypatch.setattr(GasService, "refresh", lambda *args: pytest.fail("view update recalibrated gas"))
    monkeypatch.setattr(GasService, "refresh_cost", lambda *args: pytest.fail("view update recalculated cost"))

    results = [publication.publish_reports(runtime, batch_size=1) for _ in range(3)]

    assert [result["rendered_reports"] for result in results] == [1, 1, 1]
    assert [result["pending_reports"] for result in results] == [2, 1, 0]
    assert {report.id: runtime.db.report(report.id).model_dump_json() for report in reports} == before
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_changed_stored_summary_refreshes_one_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 2)
    publication.publish_reports(runtime)
    changed = report[0].model_copy(update={
        "summary": "changed", "generated_at": report[0].generated_at + timedelta(seconds=1),
    })
    runtime.db.save_report(changed, "changed")

    refreshed: list[str] = []
    original_refresh = GasService.refresh

    def record_refresh(self: GasService, value):
        refreshed.append(value.id)
        return original_refresh(self, value)

    monkeypatch.setattr(GasService, "refresh", record_refresh)
    result = publication.publish_reports(runtime)
    assert refreshed == [report[0].id]
    assert result["rendered_reports"] == 1


@pytest.mark.ydb
def test_initial_publish_is_bounded_and_restart_can_drain_with_daily_latest(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    daily = _daily_reports(runtime, 3)
    analysis = runtime.analysis(no_ai=True)
    analysis.analyze_week(2026, 31, use_ai=False)
    first = publication.publish_reports(runtime, batch_size=2)
    assert first["pending_reports"] > 0

    restarted_db = Database(runtime.db.storage.config)
    try:
        restarted = Runtime(runtime.loaded, restarted_db)
        results = [first]
        while results[-1]["pending_reports"]:
            results.append(publication.publish_reports(restarted, batch_size=2))
    finally:
        restarted_db.close()
    assert results[-1]["pending_reports"] == 0
    assert results[-1]["latest_report_id"] == daily[-1].id
    assert sum(item["rendered_reports"] for item in results) >= 4


@pytest.mark.ydb
def test_publication_failure_leaves_pending_work_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 1)
    original = publication.render_html
    failed = True

    def fail_once(*args, **kwargs):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("render failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "render_html", fail_once)
    with pytest.raises(OSError, match="render failed"):
        publication.publish_reports(runtime, batch_size=2)
    result = publication.publish_reports(runtime, batch_size=2)
    assert result["pending_reports"] == 0
    assert result["rendered_reports"] == 1


@pytest.mark.ydb
def test_new_week_is_published_before_old_archive_maintenance(tmp_path: Path) -> None:
    import json

    runtime = _runtime(tmp_path)
    daily = _daily_reports(runtime, 12, start=date(2026, 9, 9))
    publication.publish_reports(runtime, batch_size=100)
    _record_change(runtime.db, "global", "gas")
    # Leave an older backlog before the newly completed week enters the queue.
    publication.publish_reports(runtime, batch_size=1)
    weekly = runtime.analysis(no_ai=True).analyze_week(2026, 38, use_ai=False)
    result = publication.publish_reports(runtime, batch_size=2)
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert any(item["href"] == "weekly/2026-09-14.html" for item in manifest["reports"])
    html, exported = publication.archive_paths(Path(result["manifest"]).parent, weekly)
    assert html.is_file() and exported.is_file()
    assert result["rendered_reports"] == 2
    assert result["pending_reports"] > 0
    assert result["latest_report_id"] == daily[-1].id


@pytest.mark.ydb
def test_global_gas_change_is_bounded_and_latest_is_first(tmp_path: Path) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore
    from zont_analyzer.application.pilot import reports_directory

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    publication.publish_reports(runtime)
    OwnerContextStore(runtime.db).update_gas(reports[-1].id, {"value_m3": "123"})
    result = publication.publish_reports(runtime, batch_size=2)
    assert result["rendered_reports"] == 2
    assert result["pending_reports"] == 2
    assert "Текущее показание: 123 м³" in (reports_directory(runtime) / "latest.html").read_text()
    assert publication.publish_reports(runtime, batch_size=2)["pending_reports"] == 0
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_tariff_change_recalculates_cost_without_gas_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from zont_analyzer.application.gas_tariffs import GasTariffStore

    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 2)
    publication.publish_reports(runtime)
    GasTariffStore(runtime.db).save({"price": "8.01", "currency": "RUB", "effective_month": "2026-08"})
    monkeypatch.setattr(GasService, "refresh", lambda *args: pytest.fail("gas model recalculated for tariff"))
    assert publication.publish_reports(runtime)["rendered_reports"] == 2
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_exact_tariff_marker_recalculates_only_affected_period(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 2)
    publication.publish_reports(runtime)
    _record_change(runtime.db, "tariff", "2026-08-02T00:00:00+00:00")
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_telemetry_outside_archive_is_idle_and_late_hour_is_targeted(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 3)
    publication.publish_reports(runtime)
    _record_change(runtime.db, "telemetry", "2026-09-13T12")
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    _record_change(runtime.db, "telemetry", "2026-08-01T12")
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    assert reports[0].id != reports[-1].id


@pytest.mark.ydb
def test_calibration_telemetry_invalidates_reports_outside_sample_window(tmp_path: Path) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    store = OwnerContextStore(runtime.db)
    store.update_gas(reports[0].id, {"value_m3": "100"})
    store.update_gas(reports[1].id, {"value_m3": "102"})
    publication.publish_reports(runtime)
    _record_change(runtime.db, "telemetry", "2026-08-01T12")
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == 3


@pytest.mark.ydb
def test_post_midnight_calibration_change_targets_only_adjacent_reports(tmp_path: Path) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    store = OwnerContextStore(runtime.db)
    store.update_gas(reports[0].id, {"value_m3": "100"})
    store.update_gas(reports[1].id, {"value_m3": "102"})
    publication.publish_reports(runtime)

    # The last reading is Aug 2 in UTC+4. Its final boundary window ends at
    # Aug 3 00:00 local (Aug 2 20:00 UTC); the old hour marker overlapped the
    # 15-minute post-window cache fingerprint and invalidated the whole archive.
    changed = int(datetime(2026, 8, 2, 20, 30, tzinfo=UTC).timestamp())
    _record_change(runtime.db, "telemetry-gas", "2026-08-02T20",
                   {"calibration": True, "change_start": changed, "change_end": changed})
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == 1  # Report windows include a 15-minute edge.
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 0

    # An exact marker inside calibration does not change the effective model
    # when its underlying telemetry is unchanged.
    inside = int(datetime(2026, 8, 2, 19, 30, tzinfo=UTC).timestamp())
    _record_change(runtime.db, "telemetry-gas", "2026-08-02T19",
                   {"calibration": True, "change_start": inside, "change_end": inside})
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == 1


@pytest.mark.ydb
def test_unchanged_calibration_model_uses_partial_index_and_one_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    owner = OwnerContextStore(runtime.db)
    owner.update_gas(reports[0].id, {"value_m3": "100"})
    owner.update_gas(reports[1].id, {"value_m3": "102"})
    publication.publish_reports(runtime)
    previous = PublicationRepository(runtime.db.storage).load_meta()["gas_model_version"]
    at = reports[0].period_start + timedelta(hours=12)
    changed = int(at.timestamp())
    _record_change(runtime.db, "telemetry-gas", at.strftime("%Y-%m-%dT%H"),
                   {"calibration": True, "change_start": changed, "change_end": changed})
    original = GasService.__init__
    services = []

    def init(self, *args, **kwargs):
        services.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(GasService, "__init__", init)
    monkeypatch.setattr(PublicationRepository, "load", lambda *args: pytest.fail("full archive load"))
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["rendered_reports"] == 1
    assert result["pending_reports"] == 0
    assert len(services) == 1
    assert PublicationRepository(runtime.db.storage).load_meta()["gas_model_version"] == previous
    monkeypatch.setattr(GasService, "model", lambda *args: pytest.fail("model on idle poll"))
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_missing_model_checkpoint_stays_broad_and_does_not_swallow_newer_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    owner = OwnerContextStore(runtime.db)
    owner.update_gas(reports[0].id, {"value_m3": "100"})
    owner.update_gas(reports[1].id, {"value_m3": "102"})
    publication.publish_reports(runtime)
    runtime.db.storage.execute("DELETE FROM metadata WHERE name='publication:gas_model_version';")
    at = reports[0].period_start + timedelta(hours=12)

    def marker(moment):
        changed = int(moment.timestamp())
        _record_change(runtime.db, "telemetry-gas", moment.strftime("%Y-%m-%dT%H"),
                       {"calibration": True, "change_start": changed, "change_end": changed})

    marker(at)
    upper = runtime.db.source_revision()
    original = GasService.model
    injected = False

    def model(self, *args, **kwargs):
        nonlocal injected
        result = original(self, *args, **kwargs)
        if not injected:
            injected = True
            marker(at + timedelta(hours=1))
        return result

    monkeypatch.setattr(GasService, "model", model)
    result = publication.publish_reports(runtime, batch_size=1)
    assert result["pending_reports"] == 3
    meta = PublicationRepository(runtime.db.storage).load_meta()
    assert meta.get("gas_model_version", "") == ""
    assert meta["gas_model_previous_version"] == ""
    assert meta["gas_model_refresh_reason"] == "calibration_model_checkpoint_missing"
    assert int(meta["checkpoint"]) == upper < runtime.db.source_revision()
    assert publication.publish_reports(runtime, batch_size=8)["rendered_reports"] == 4
    assert PublicationRepository(runtime.db.storage).load_meta()["gas_model_version"]
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


def test_exact_calibration_range_and_legacy_hour_with_fractional_timezone() -> None:
    from zont_analyzer.application.incremental_publication import (
        _calibration_bounds,
        _calibration_change_overlaps,
    )

    class Readings:
        def reading_span(self) -> tuple[str, str]:
            return "2026-08-01", "2026-08-02"

    config = AppConfig()
    config.home.timezone = "Asia/Kolkata"
    runtime = type("RuntimeStub", (), {"config": config})()
    bounds = _calibration_bounds(Readings(), runtime)
    assert bounds is not None
    assert bounds[1] == datetime(2026, 8, 2, 18, 30, tzinfo=UTC).timestamp()
    hour = "2026-08-02T18"
    outside = int(datetime(2026, 8, 2, 18, 45, tzinfo=UTC).timestamp())
    inside = int(datetime(2026, 8, 2, 18, 15, tzinfo=UTC).timestamp())
    change = {"scope": "telemetry-gas", "identifier": hour,
              "payload": json.dumps({"calibration": True, "change_start": outside, "change_end": outside})}
    assert not _calibration_change_overlaps(change, bounds)
    change["payload"] = json.dumps({"calibration": True, "change_start": inside, "change_end": inside})
    assert _calibration_change_overlaps(change, bounds)
    change["payload"] = "{}"  # Pre-upgrade hourly markers stay conservative.
    assert _calibration_change_overlaps(change, bounds)
    change["payload"] = json.dumps({"change_start": outside, "change_end": outside})
    assert _calibration_change_overlaps(change, bounds)  # Unknown schema is also conservative.


@pytest.mark.ydb
def test_change_during_publication_remains_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from zont_analyzer.application.owner_context import OwnerContextStore
    from zont_analyzer.application.pilot import reports_directory

    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 1)[0]
    original = publication.render_html
    changed = False

    def concurrent_write(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            OwnerContextStore(runtime.db).update_gas(report.id, {"value_m3": "321"})
        return original(*args, **kwargs)

    monkeypatch.setattr(publication, "render_html", concurrent_write)
    publication.publish_reports(runtime)
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert "Текущее показание: 321 м³" in (reports_directory(runtime) / "latest.html").read_text()
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.parametrize("damage", ["missing_manifest", "missing_html", "corrupt_json", "missing_latest", "rebuild"])
@pytest.mark.ydb
def test_durable_index_and_artifact_recovery(tmp_path: Path, damage: str) -> None:
    from zont_analyzer.application.pilot import reports_directory

    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 1)[0]
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    if damage == "missing_manifest":
        (output / "reports.json").unlink()
    elif damage == "missing_html":
        (output / "daily/2026-08-01.html").unlink()
    elif damage == "corrupt_json":
        (output / "daily/2026-08-01.json").write_text("broken json")
    elif damage == "missing_latest":
        (output / "latest.html").unlink()
    result = publication.publish_reports(runtime, rebuild=damage == "rebuild")
    assert result["rendered_reports"] == (0 if damage == "missing_manifest" else 1)
    assert report.id in (output / "latest.html").read_text()
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
def test_local_artifact_loss_recovers_from_canonical_ydb(tmp_path: Path) -> None:
    import shutil

    from zont_analyzer.application.pilot import reports_directory

    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 1)[0]
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    shutil.rmtree(output / "daily")
    (output / "reports.json").unlink()
    (output / "latest.html").unlink()
    result = publication.publish_reports(runtime)
    assert result["reports"] == 1
    assert report.id in (output / "latest.html").read_text()
    assert (output / "daily/2026-08-01.json").is_file()
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


@pytest.mark.ydb
@pytest.mark.parametrize("batch_size", [1, 2])
def test_comparison_is_requeued_when_its_source_finishes_in_later_batch(
    tmp_path: Path, monkeypatch, batch_size,
) -> None:

    runtime = _runtime(tmp_path)
    older, latest = _daily_reports(runtime, 2)
    latest.context["period_comparisons"] = [{
        "baseline_period": {"start": older.period_start.isoformat(), "end": older.period_end.isoformat()},
        "current_period": {"start": latest.period_start.isoformat(), "end": latest.period_end.isoformat()},
    }]
    runtime.db.save_report(latest, "comparison")
    for _ in range(5):
        if not publication.publish_reports(runtime)["pending_reports"]:
            break
    baseline = publication.publish_reports(runtime)["queue_metrics"]
    original_refresh = GasService.refresh

    def revised_volume(self, report):
        result = original_refresh(self, report)
        if report.id == older.id:
            result.context["gas"]["volume_m3"] = 42
        return result

    monkeypatch.setattr(GasService, "refresh", revised_volume)
    _record_change(runtime.db, "global", "gas")
    # Latest is rendered first. Its older source changes only in the next batch.
    first = publication.publish_reports(runtime, batch_size=batch_size)
    assert first["pending_reports"] == 1
    assert first["queue_metrics"]["completed"] == baseline["completed"] + batch_size
    if batch_size == 1:
        assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 1
    drained = publication.publish_reports(runtime, batch_size=1)
    assert drained["pending_reports"] == 0
    assert drained["queue_metrics"]["completed"] == baseline["completed"] + 3
    assert drained["queue_metrics"]["requeued"] == baseline["requeued"] + 3
    assert publication.publish_reports(runtime)["rendered_reports"] == 0

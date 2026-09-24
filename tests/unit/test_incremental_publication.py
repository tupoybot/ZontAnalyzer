from __future__ import annotations

from datetime import date, timedelta
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


def _record_change(db, scope: str, identifier: str) -> None:
    def write(tx) -> None:
        revision = bump_revision(tx, "publication")
        tx.execute(
            "DECLARE $scope AS Utf8; DECLARE $identifier AS Utf8; DECLARE $revision AS Int64; "
            "UPSERT INTO publication_changes (scope,identifier,revision,payload) "
            "VALUES ($scope,$identifier,$revision,'{}');",
            {"$scope": scope, "$identifier": identifier, "$revision": revision},
        )

    db.storage.transaction(write)


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


def test_tariff_change_recalculates_cost_without_gas_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from zont_analyzer.application.gas_tariffs import GasTariffStore

    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 2)
    publication.publish_reports(runtime)
    GasTariffStore(runtime.db).save({"price": "8.01", "currency": "RUB", "effective_month": "2026-08"})
    monkeypatch.setattr(GasService, "refresh", lambda *args: pytest.fail("gas model recalculated for tariff"))
    assert publication.publish_reports(runtime)["rendered_reports"] == 2
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


def test_exact_tariff_marker_recalculates_only_affected_period(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 2)
    publication.publish_reports(runtime)
    _record_change(runtime.db, "tariff", "2026-08-02T00:00:00+00:00")
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


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


def test_comparison_is_requeued_when_its_source_finishes_in_later_batch(tmp_path: Path, monkeypatch) -> None:

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
    publication.publish_reports(runtime)
    original_refresh = GasService.refresh

    def revised_volume(self, report):
        result = original_refresh(self, report)
        if report.id == older.id:
            result.context["gas"]["volume_m3"] = 42
        return result

    monkeypatch.setattr(GasService, "refresh", revised_volume)
    _record_change(runtime.db, "global", "gas")
    # Latest is rendered first. Its older source changes only in the next batch.
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 1
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 1
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 0
    assert publication.publish_reports(runtime)["rendered_reports"] == 0

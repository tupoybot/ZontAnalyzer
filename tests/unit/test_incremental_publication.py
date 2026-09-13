from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from zont_analyzer.application import publication
from zont_analyzer.application.gas import GasService
from zont_analyzer.runtime import build_runtime


def _runtime(tmp_path: Path):
    return build_runtime(None, tmp_path)


def _daily_reports(runtime, count: int, *, start: date = date(2026, 8, 1)):
    analysis = runtime.analysis(no_ai=True)
    return [analysis.analyze_daily(start + timedelta(days=day), use_ai=False) for day in range(count)]


def test_noop_does_not_recalculate_or_walk_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    _daily_reports(runtime, 1)
    publication.publish_reports(runtime)

    monkeypatch.setattr(GasService, "refresh", lambda *args: pytest.fail("refresh on no-op"))
    monkeypatch.setattr(publication, "render_html", lambda *args, **kwargs: pytest.fail("render on no-op"))
    monkeypatch.setattr(runtime.db, "completed_reports", lambda *args, **kwargs: pytest.fail("DB walk on no-op"))

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

    restarted = _runtime(tmp_path)
    results = [first]
    while results[-1]["pending_reports"]:
        results.append(publication.publish_reports(restarted, batch_size=2))
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


def test_telemetry_outside_archive_is_idle_and_late_hour_is_targeted(tmp_path: Path) -> None:
    from zont_analyzer.adapters.sqlite.publication_journal import record_change

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 3)
    publication.publish_reports(runtime)
    record_change(runtime.db, "telemetry", "2026-09-13T12")
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    record_change(runtime.db, "telemetry", "2026-08-01T12")
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert publication.publish_reports(runtime)["rendered_reports"] == 0
    assert reports[0].id != reports[-1].id


def test_calibration_telemetry_invalidates_reports_outside_sample_window(tmp_path: Path) -> None:
    from zont_analyzer.adapters.sqlite.publication_journal import record_change
    from zont_analyzer.application.owner_context import OwnerContextStore

    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 4)
    store = OwnerContextStore(runtime.db)
    store.update_gas(reports[0].id, {"value_m3": "100"})
    store.update_gas(reports[1].id, {"value_m3": "102"})
    publication.publish_reports(runtime)
    record_change(runtime.db, "telemetry", "2026-08-01T12")
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


@pytest.mark.parametrize("damage", ["missing_cache", "corrupt_cache", "old_schema", "missing_html", "corrupt_json"])
def test_disposable_cache_and_artifact_recovery(tmp_path: Path, damage: str) -> None:
    import sqlite3

    from zont_analyzer.application.pilot import reports_directory

    runtime = _runtime(tmp_path)
    report = _daily_reports(runtime, 1)[0]
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    cache = output / ".publication-cache.sqlite3"
    assert cache.stat().st_mode & 0o777 == 0o600
    if damage == "missing_cache":
        cache.unlink()
    elif damage == "corrupt_cache":
        cache.write_text("broken sqlite")
    elif damage == "old_schema":
        cache.unlink()
        with sqlite3.connect(cache) as connection:
            connection.execute("CREATE TABLE items(href TEXT PRIMARY KEY)")
    elif damage == "missing_html":
        (output / "daily/2026-08-01.html").unlink()
    else:
        (output / "daily/2026-08-01.json").write_text("broken json")
    assert publication.publish_reports(runtime)["rendered_reports"] == 1
    assert report.id in (output / "latest.html").read_text()
    assert publication.publish_reports(runtime)["rendered_reports"] == 0


def test_recovery_retains_file_only_exports(tmp_path: Path) -> None:
    import shutil

    from zont_analyzer.application.pilot import reports_directory

    source = _runtime(tmp_path / "source")
    report = _daily_reports(source, 1)[0]
    publication.publish_reports(source)
    target = _runtime(tmp_path / "target")
    shutil.copytree(reports_directory(source) / "daily", reports_directory(target) / "daily")
    assert target.db.report(report.id) is None
    result = publication.publish_reports(target)
    assert result["reports"] == 1
    assert report.id in (reports_directory(target) / "latest.html").read_text()
    assert publication.publish_reports(target)["rendered_reports"] == 0


def test_comparison_is_requeued_when_its_source_finishes_in_later_batch(tmp_path: Path, monkeypatch) -> None:
    from zont_analyzer.adapters.sqlite.publication_journal import record_change

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
    record_change(runtime.db, "global", "gas")
    # Latest is rendered first. Its older source changes only in the next batch.
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 1
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 1
    assert publication.publish_reports(runtime, batch_size=1)["pending_reports"] == 0
    assert publication.publish_reports(runtime)["rendered_reports"] == 0

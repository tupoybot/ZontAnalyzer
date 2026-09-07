from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.application import feedback, publication
from zont_analyzer.application.feedback import publish_feedback_report
from zont_analyzer.application.pilot import reports_directory
from zont_analyzer.runtime import build_runtime


def test_only_completed_existing_periods_are_published_and_latest_stays_daily(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = runtime.analysis(no_ai=True)
    day = analysis.analyze_daily(date(2026, 8, 3), use_ai=False)
    analysis.analyze_daily(date(2026, 8, 1), use_ai=False)
    week = analysis.analyze_week(2026, 31, use_ai=False)
    month = analysis.analyze_month(2026, 7, use_ai=False)
    analysis.analyze_month(2099, 1, use_ai=False)

    result = publication.publish_reports(runtime)
    output = reports_directory(runtime)
    manifest = json.loads((output / "reports.json").read_text())
    assert result["latest_report_id"] == day.id
    assert {(entry["kind"], entry["start"], entry["end"]) for entry in manifest["reports"]} == {
        ("daily", "2026-08-01", "2026-08-02"),
        ("daily", "2026-08-03", "2026-08-04"),
        ("weekly", "2026-07-27", "2026-08-03"),
        ("monthly", "2026-07-01", "2026-08-01"),
    }
    for entry in manifest["reports"]:
        assert (output / entry["href"]).is_file()
        assert (output / entry["href"]).with_suffix(".json").is_file()
    assert not (output / "daily/2026-08-02.html").exists()
    assert not (output / "monthly/2099-01-01.html").exists()
    before = (output / "latest.html").read_text()
    recommendation = week.recommendations[0]
    assert recommendation.id
    runtime.db.set_recommendation_feedback(recommendation.id, "applied", "Archive feedback")
    publish_feedback_report(runtime, week.id)
    assert (output / "latest.html").read_text() == before
    assert "Archive feedback" in (output / "weekly/2026-07-27.html").read_text()
    assert month.id != day.id


def test_empty_archive_and_incomplete_or_corrupt_exports_do_not_get_links(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    output = reports_directory(runtime)
    (output / "daily").mkdir(parents=True)
    (output / "daily/2026-08-01.json").write_text('{"partial":')
    (output / "daily/2026-08-02.html").write_text("orphan HTML")
    result = publication.publish_reports(runtime)
    assert result["reports"] == 0
    assert json.loads((output / "reports.json").read_text())["reports"] == []
    assert not (output / "latest.html").exists()


def test_failed_publication_keeps_manifest_and_latest_complete(tmp_path: Path, monkeypatch) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    old_manifest = (output / "reports.json").read_bytes()
    old_latest = (output / "latest.html").read_bytes()
    runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 3), use_ai=False)
    original_write = publication.atomic_write_text

    def fail_json(path: Path, content: str, *, mode: int = 0o600) -> None:
        if path.name == "2026-08-03.json":
            raise OSError("simulated disk failure")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(publication, "atomic_write_text", fail_json)
    with pytest.raises(OSError, match="disk failure"):
        publication.publish_reports(runtime)
    assert (output / "reports.json").read_bytes() == old_manifest
    assert (output / "latest.html").read_bytes() == old_latest
    monkeypatch.setattr(publication, "atomic_write_text", original_write)
    publication.publish_reports(runtime)
    assert len(json.loads((output / "reports.json").read_text())["reports"]) == 2
    assert not list(output.rglob("*.tmp"))


def test_report_calculated_before_period_end_is_not_completed(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    runtime.db.save_report(report.model_copy(update={"generated_at": report.period_start}), "partial")
    assert runtime.db.completed_reports(datetime(2026, 9, 1, tzinfo=UTC)) == []


def test_feedback_publication_refreshes_only_requested_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = runtime.analysis(no_ai=True)
    daily = analysis.analyze_daily(date(2026, 8, 3), use_ai=False)
    weekly = analysis.analyze_week(2026, 31, use_ai=False)
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    weekly_path = output / "weekly/2026-07-27.html"
    weekly_before = weekly_path.read_text(encoding="utf-8")
    recommendation = weekly.recommendations[0]
    runtime.db.set_recommendation_feedback(recommendation.id, "applied", "Targeted feedback")

    def fail_full_publish(*args: object, **kwargs: object) -> None:
        raise AssertionError("feedback must not publish the complete archive")

    monkeypatch.setattr(publication, "publish_reports", fail_full_publish)
    monkeypatch.setattr(feedback, "publish_reports", fail_full_publish)
    publish_feedback_report(runtime, weekly.id)

    assert weekly_path.read_text(encoding="utf-8") != weekly_before
    assert "Targeted feedback" in weekly_path.read_text(encoding="utf-8")
    assert "Targeted feedback" not in (output / "daily/2026-08-03.html").read_text(encoding="utf-8")
    assert daily.id != weekly.id


def test_feedback_can_create_first_calendar_publication(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = runtime.analysis(no_ai=True)
    old = analysis.analyze_daily(date(2026, 8, 1), use_ai=False)
    latest = analysis.analyze_daily(date(2026, 8, 3), use_ai=False)
    runtime.db.set_recommendation_feedback(old.recommendations[0].id, "applied", "Old day")
    publish_feedback_report(runtime, old.id)
    output = reports_directory(runtime)
    assert (output / "daily/2026-08-01.html").is_file()
    assert "Old day" in (output / "daily/2026-08-01.html").read_text(encoding="utf-8")
    assert latest.recommendations[0].id not in (output / "latest.html").read_text(encoding="utf-8")


def test_feedback_publication_does_not_overwrite_newer_same_period_snapshot(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 3), use_ai=False)
    publication.publish_reports(runtime)
    newer = report.model_copy(update={
        "summary": "Newer snapshot", "generated_at": report.generated_at.replace(microsecond=0),
    })
    newer = newer.model_copy(update={"generated_at": newer.generated_at + timedelta(seconds=1)})
    runtime.db.save_report(newer, "newer")
    runtime.db.set_recommendation_feedback(report.recommendations[0].id, "applied", "Keep new snapshot")
    publish_feedback_report(runtime, report.id)
    html = (reports_directory(runtime) / "daily/2026-08-03.html").read_text(encoding="utf-8")
    assert "Newer snapshot" in html


@pytest.mark.parametrize("manifest_state", ["valid", "missing", "corrupt"])
def test_old_day_feedback_preserves_latest_and_archive_index(tmp_path: Path, manifest_state: str) -> None:
    runtime = build_runtime(None, tmp_path)
    old = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    latest = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 3), use_ai=False)
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    latest_bytes = (output / "latest.html").read_bytes()
    manifest = output / "reports.json"
    if manifest_state == "missing":
        manifest.unlink()
    elif manifest_state == "corrupt":
        manifest.write_text('{broken')
    runtime.db.set_recommendation_feedback(old.recommendations[0].id, "rejected", "Old comment")
    publish_feedback_report(runtime, old.id)
    assert (output / "latest.html").read_bytes() == latest_bytes
    assert "Old comment" in (output / "daily/2026-08-01.html").read_text()
    if manifest_state == "valid":
        assert len(json.loads(manifest.read_text())["reports"]) == 2
    elif manifest_state == "missing":
        assert not manifest.exists()
    else:
        assert manifest.read_text() == '{broken'
    runtime.db.set_recommendation_feedback(latest.recommendations[0].id, "rejected", "Latest comment")
    publish_feedback_report(runtime, latest.id)
    assert "Latest comment" in (output / "latest.html").read_text()


def test_feedback_on_superseded_report_or_initial_does_not_replace_archive(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    old = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 3), use_ai=False)
    newer = old.model_copy(update={
        "id": "new-report", "generated_at": old.generated_at + timedelta(seconds=1),
        "algorithm_version": "new-version", "recommendations": [],
    })
    runtime.db.save_report(newer, "new-version")
    publication.publish_reports(runtime)
    output = reports_directory(runtime)
    before = {p: p.read_bytes() for p in output.rglob('*') if p.is_file()}
    publish_feedback_report(runtime, old.id)
    initial = old.model_copy(update={"id": "initial-report", "kind": "initial", "recommendations": []})
    runtime.db.save_report(initial, "initial")
    publish_feedback_report(runtime, initial.id)
    assert all(p.read_bytes() == content for p, content in before.items())

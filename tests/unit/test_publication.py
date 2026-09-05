from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from zont_analyzer.application import publication
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

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.ydb_support import make_runtime
from zont_analyzer.application import publication, regeneration
from zont_analyzer.application.pilot import reports_directory


@pytest.mark.ydb
def test_regeneration_snapshot_reads_only_candidate_and_index_files(tmp_path: Path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path)
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    output = reports_directory(runtime)
    candidate_html, candidate_json = publication.archive_paths(output, report)
    candidate_html.parent.mkdir(parents=True, exist_ok=True)
    candidate_html.write_text("html")
    candidate_json.write_text("json")
    (output / "reports.json").write_text("manifest")
    (output / "latest.html").write_text("latest")
    unrelated = output / "daily" / "unrelated.json"
    unrelated.write_text("must not be touched")

    def fail_walk(self):
        raise AssertionError("regeneration must not walk the archive")

    monkeypatch.setattr(Path, "rglob", fail_walk)
    snapshot = regeneration._publication_snapshot(runtime, report.id)
    assert set(snapshot) == {
        candidate_html.relative_to(output), candidate_json.relative_to(output),
        Path("reports.json"), Path("latest.html"),
    }
    assert unrelated.read_text() == "must not be touched"


@pytest.mark.ydb
def test_regeneration_restore_restores_candidate_and_preserves_unrelated_files(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    output = reports_directory(runtime)
    html_path, json_path = publication.archive_paths(output, report)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text("old html")
    json_path.write_text("old json")
    (output / "reports.json").write_text("old manifest")
    (output / "latest.html").write_text("old latest")
    unrelated = output / "daily" / "unrelated.json"
    unrelated.write_text("keep")
    snapshot = regeneration._publication_snapshot(runtime, report.id)

    html_path.write_text("new html")
    json_path.unlink()
    (output / "reports.json").write_text("new manifest")
    regeneration._restore_publication(runtime, snapshot)

    assert html_path.read_text() == "old html"
    assert json_path.read_text() == "old json"
    assert (output / "reports.json").read_text() == "old manifest"
    assert (output / "latest.html").read_text() == "old latest"
    assert unrelated.read_text() == "keep"

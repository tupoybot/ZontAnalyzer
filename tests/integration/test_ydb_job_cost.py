"""Completed report work survives a failure after its atomic save."""
from __future__ import annotations

import json
from datetime import date

import pytest

from tests.integration.test_cloud_report_jobs import _runner
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.cloud import monitoring
from zont_analyzer.domain.periods import calendar_period


@pytest.mark.ydb
def test_scheduled_job_retries_after_metrics_failure_without_reanalysis(tmp_path, monkeypatch) -> None:
    db, runner, client = _runner(tmp_path)
    runner.publish = False
    period = calendar_period("daily", date(2026, 9, 23), "UTC")
    db.save_devices([{"id": "fixture"}])
    for source in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="fixture", data_type=source,
                                  start=period.start, end=period.observed_end, state="empty")
    assert runner.run_scheduled(period, use_ai=False)["phase"] == "analyze"

    def interrupted(_report):
        raise RuntimeError("metrics interrupted")

    monkeypatch.setattr(monitoring, "report_metrics", interrupted)
    with pytest.raises(RuntimeError, match="metrics interrupted"):
        runner.run_scheduled(period, use_ai=False)
    job_key = f"scheduled:daily:{int(period.start.timestamp())}:{int(period.observed_end.timestamp())}:v1"
    lease = db.jobs.get(job_key)
    assert lease is not None
    assert json.loads(lease.checkpoint)["phase"] == "saved"
    report = db.report(runner.runtime.analysis(no_ai=True).report_id_for("daily", period.start))
    assert report is not None
    revision = db.storage.execute(
        "DECLARE $id AS Utf8; SELECT revision FROM reports VIEW by_id WHERE id=$id;",
        {"$id": report.id},
    )[0].rows[0].revision
    calls = client.history_calls, client.event_calls

    monkeypatch.setattr(AnalysisService, "analyze_period", lambda *_args, **_kwargs:
                        pytest.fail("completed analysis repeated"))
    monkeypatch.setattr(monitoring, "report_metrics", lambda _report: None)
    result = runner.run_scheduled(period, use_ai=False)
    assert result["status"] == "done" and result["report_id"] == report.id
    assert (client.history_calls, client.event_calls) == calls
    assert db.storage.execute(
        "DECLARE $id AS Utf8; SELECT revision FROM reports VIEW by_id WHERE id=$id;",
        {"$id": report.id},
    )[0].rows[0].revision == revision

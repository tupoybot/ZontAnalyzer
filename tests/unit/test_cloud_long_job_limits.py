from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer.cloud import report_jobs
from zont_analyzer.cloud.report_jobs import Period, ReportJobRunner, ReportRequest


def test_report_runner_accepts_570_and_rejects_571_before_work() -> None:
    runner = ReportJobRunner.__new__(ReportJobRunner)
    now = datetime(2026, 9, 28, tzinfo=UTC)
    runner.now = lambda: now
    runner.monotonic = lambda: 0
    period = Period("weekly", now + timedelta(days=1), now + timedelta(days=8), "test-job")
    request = ReportRequest(kind="weekly", year=2026, week=40, use_ai=False)

    result = runner._run(request, period, timeout_seconds=570)
    assert result == {
        "status": "not_due", "job_key": "test-job",
        "due_at": period.end.isoformat(),
    }
    with pytest.raises(ValueError, match="invalid report timeout"):
        runner._run(request, period, timeout_seconds=571)


def test_report_execute_subtracts_runtime_startup_from_570_second_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(db=SimpleNamespace(close=Mock()))
    build = Mock(return_value=runtime)
    runner = Mock()
    runner.run.return_value = {"status": "pending"}
    runner_factory = Mock(return_value=runner)
    monkeypatch.setattr(report_jobs, "build_runtime", build)
    monkeypatch.setattr(report_jobs, "ReportJobRunner", runner_factory)
    monkeypatch.setattr(report_jobs.HeavyWorkLease, "acquire", Mock(return_value=Mock()))
    monkeypatch.setattr(report_jobs.time, "monotonic", Mock(side_effect=[80.0, 90.0, 100.0, 120.0, 130.0]))

    result = report_jobs.execute({"kind": "weekly"}, timeout_seconds=570)

    assert result == {"status": "pending"}
    runner.run.assert_called_once_with({"kind": "weekly"}, timeout_seconds=540.0)
    runtime.db.close.assert_called_once()


def test_report_execute_closes_runtime_when_startup_exhausts_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(db=SimpleNamespace(close=Mock()))
    monkeypatch.setattr(report_jobs, "build_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(report_jobs, "ReportJobRunner", Mock())
    monkeypatch.setattr(report_jobs.HeavyWorkLease, "acquire", Mock(return_value=Mock()))
    monkeypatch.setattr(report_jobs.time, "monotonic", Mock(side_effect=[0.0, 0.0, 570.0, 570.0]))

    with pytest.raises(TimeoutError, match="report startup exhausted"):
        report_jobs.execute({"kind": "weekly"}, timeout_seconds=570)

    runtime.db.close.assert_called_once()


def test_report_execute_rejects_571_before_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = Mock()
    monkeypatch.setattr(report_jobs, "build_runtime", build)
    with pytest.raises(ValueError, match="invalid report timeout"):
        report_jobs.execute({"kind": "weekly"}, timeout_seconds=571)
    build.assert_not_called()

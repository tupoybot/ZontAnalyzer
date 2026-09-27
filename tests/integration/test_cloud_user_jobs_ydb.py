"""Real YDB contract for queued cloud regeneration and its saved checkpoint."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tests.ydb_support import make_runtime
from zont_analyzer.cloud import user_jobs


def _seed(tmp_path: Path):
    runtime = make_runtime(tmp_path / "data")
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    return runtime, report


@pytest.mark.ydb
def test_cloud_regeneration_queues_and_commits_fenced_report(tmp_path: Path) -> None:
    runtime, report = _seed(tmp_path)
    first = user_jobs.enqueue_regeneration(runtime, report.id)
    assert first["status"] == "queued"
    assert user_jobs.enqueue_regeneration(runtime, report.id) == first
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert result["processed"] == 1, result
    assert result["jobs"][0]["status"] == "success"
    saved = runtime.db.report(report.id)
    assert saved is not None
    assert saved.generated_at >= report.generated_at
    status = user_jobs.regeneration_status(runtime, report.id)
    assert status["status"] == "success"
    assert status["generated_at"] == saved.generated_at.isoformat()
    assert "request_nonce" not in status


class _Killed(BaseException):
    pass


@pytest.mark.ydb
def test_saved_checkpoint_resumes_after_worker_death_without_reanalysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, report = _seed(tmp_path)
    user_jobs.enqueue_regeneration(runtime, report.id)
    run = user_jobs._run_regeneration

    def killed_after_commit(*args):
        outcome = run(*args)
        assert outcome["status"] == "success"
        raise _Killed

    monkeypatch.setattr(user_jobs, "_run_regeneration", killed_after_commit)
    with pytest.raises(_Killed):
        user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)

    key = "m5:regenerate:" + report.id
    interrupted = runtime.db.jobs.get(key)
    assert interrupted is not None
    assert interrupted.state == "active"
    assert json.loads(interrupted.checkpoint or "{}")["phase"] == "saved"
    saved = runtime.db.report(report.id)
    assert saved is not None

    # Simulate lease expiry without waiting for a live timeout.
    runtime.db.storage.execute(
        "DECLARE $key AS Utf8; UPDATE jobs SET lease_until=0 WHERE job_key=$key;",
        {"$key": key},
    )
    monkeypatch.setattr(user_jobs, "_run_regeneration", run)
    monkeypatch.setattr(runtime, "analysis", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("saved report must not be analyzed again"),
    ))
    recovered = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert recovered["jobs"][0]["status"] == "success"
    assert runtime.db.report(report.id).generated_at == saved.generated_at
    assert user_jobs.regeneration_status(runtime, report.id)["status"] == "success"

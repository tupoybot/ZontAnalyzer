from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from zont_analyzer.adapters.ydb.jobs import JobLease
from zont_analyzer.cloud import report_jobs, user_jobs
from zont_analyzer.cloud.heavy_work import KEY, HeavyWorkLease


class SharedJobs:
    """Small shared-table stand-in for two independently built runtimes."""

    def __init__(self) -> None:
        self.now = 1_000
        self.lease: JobLease | None = None

    def acquire(self, job_key: str, owner: str, lease_seconds: int) -> JobLease | None:
        assert job_key == KEY
        if self.lease is not None and self.lease.lease_until > self.now and self.lease.state == "active":
            return None
        attempt = self.lease.attempt + 1 if self.lease else 1
        self.lease = JobLease(job_key, owner, attempt, self.now + lease_seconds, "active", None)
        return self.lease

    def release(self, job_key: str, owner: str, attempt: int) -> bool:
        if (self.lease is None or self.lease.job_key != job_key or self.lease.owner != owner
                or self.lease.attempt != attempt or self.lease.state != "active"):
            return False
        self.lease = JobLease(job_key, owner, attempt, self.now, "released", None)
        return True


def _runtime(jobs: SharedJobs) -> SimpleNamespace:
    return SimpleNamespace(db=SimpleNamespace(jobs=jobs, close=Mock()))


def test_heavy_work_lease_serializes_two_runtimes_and_expiry_allows_recovery() -> None:
    jobs = SharedJobs()
    first_runtime, second_runtime = _runtime(jobs), _runtime(jobs)

    first = HeavyWorkLease.acquire(first_runtime, deadline=20.1, monotonic=lambda: 10.0)
    assert first is not None
    assert jobs.lease is not None and jobs.lease.lease_until == 1_041  # ceil(10.1) + 30.
    assert HeavyWorkLease.acquire(second_runtime, deadline=20.0, monotonic=lambda: 10.0) is None

    jobs.now = jobs.lease.lease_until
    recovered = HeavyWorkLease.acquire(second_runtime, deadline=20.0, monotonic=lambda: 10.0)
    assert recovered is not None
    assert recovered.lease.attempt == first.lease.attempt + 1
    first.release()  # A stale owner cannot release the recovered fencing token.
    assert jobs.lease.state == "active"
    assert jobs.lease.owner == recovered.lease.owner
    recovered.release()
    assert jobs.lease.state == "released"


def test_maintenance_busy_leaves_queue_untouched_and_closes_runtime(monkeypatch) -> None:
    jobs = SharedJobs()
    first = HeavyWorkLease.acquire(_runtime(jobs), deadline=20.0, monotonic=lambda: 10.0)
    assert first is not None
    runtime = _runtime(jobs)
    runtime.db.storage = Mock()
    monkeypatch.setattr(user_jobs, "open_runtime", Mock(return_value=runtime))
    drain = Mock()
    monkeypatch.setattr(user_jobs, "drain", drain)

    assert user_jobs.execute({}) == {
        "status": "busy", "processed": 0, "jobs": [],
        "publication": {"status": "busy"},
    }

    drain.assert_not_called()
    runtime.db.storage.execute.assert_not_called()
    runtime.db.storage.transaction.assert_not_called()
    runtime.db.close.assert_called_once()
    first.release()


def test_report_execution_releases_shared_lease_and_closes_runtime_on_failure(monkeypatch) -> None:
    jobs = SharedJobs()
    runtime = _runtime(jobs)
    monkeypatch.setattr(report_jobs, "build_runtime", Mock(return_value=runtime))
    runner = Mock()
    runner.run.side_effect = RuntimeError("test failure")
    monkeypatch.setattr(report_jobs, "ReportJobRunner", Mock(return_value=runner))

    try:
        report_jobs.execute({"kind": "weekly"}, timeout_seconds=570)
    except RuntimeError as exc:
        assert str(exc) == "test failure"
    else:
        raise AssertionError("expected runner failure")

    assert jobs.lease is not None and jobs.lease.state == "released"
    runtime.db.close.assert_called_once()

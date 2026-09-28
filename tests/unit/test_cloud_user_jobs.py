from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from zont_analyzer.adapters.ydb.jobs import JobLease, JobLeaseRepository
from zont_analyzer.cloud import user_jobs


@dataclass
class _Result:
    rows: list[dict[str, Any]]


class _Storage:
    def __init__(self) -> None:
        self.rows: dict[str, JobLease] = {}

    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[_Result]:
        if parameters and "$job_key" in parameters:
            lease = self.rows.get(str(parameters["$job_key"]))
            return [_Result([vars(lease)] if lease else [])]
        if parameters and "$key" in parameters:
            lease = self.rows.get(str(parameters["$key"]))
            return [_Result([vars(lease)] if lease else [])]
        if "SELECT job_key FROM jobs" in query:
            return [_Result([SimpleNamespace(job_key=key) for key, lease in sorted(self.rows.items())
                             if key.startswith("m5:") and lease.state != "done"][:8])]
        raise AssertionError(query)

    def transaction(self, callback: Any) -> Any:
        return callback(self)


class _Database:
    def __init__(self, storage: _Storage) -> None:
        self.storage = storage
        self.jobs = JobLeaseRepository(storage)

    def report(self, report_id: str) -> Any:
        return type("Report", (), {"id": report_id, "kind": "daily"})() if report_id == "daily-1" else None


class _Runtime:
    def __init__(self, storage: _Storage) -> None:
        self.db = _Database(storage)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _Storage:
    value = _Storage()

    def put(tx: _Storage, lease: JobLease) -> None:
        tx.rows[lease.job_key] = lease

    monkeypatch.setattr(JobLeaseRepository, "_put", staticmethod(put))
    return value


def test_regeneration_request_survives_new_runtime_and_duplicate_clicks(storage: _Storage) -> None:
    first = user_jobs.enqueue_regeneration(_Runtime(storage), "daily-1", " Почему? ")
    assert first["status"] == "queued"
    assert first["question"] == "Почему?"
    second = user_jobs.enqueue_regeneration(_Runtime(storage), "daily-1", "Новый вопрос")
    assert second == first
    checkpoint = json.loads(storage.rows["m5:regenerate:daily-1"].checkpoint or "{}")
    assert checkpoint["question"] == "Почему?"
    assert checkpoint["request_nonce"]
    assert user_jobs.regeneration_status(_Runtime(storage), "daily-1") == first
    assert user_jobs.regeneration_status(_Runtime(storage), "missing") == {
        "report_id": "missing", "status": "idle",
    }


def test_expired_worker_is_reclaimed_and_result_remains_durable(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    user_jobs.enqueue_regeneration(runtime, "daily-1")
    lease = runtime.db.jobs.acquire("m5:regenerate:daily-1", "dead-worker", 1)
    assert lease is not None
    storage.rows[lease.job_key] = JobLease(
        lease.job_key, lease.owner, lease.attempt, 0, "active", lease.checkpoint,
    )
    monkeypatch.setattr(user_jobs, "_run_regeneration", lambda *_args, **_kwargs: {
        "report_id": "daily-1", "status": "success", "updated_at": "2026-09-25T00:00:00+00:00",
    })
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert result["processed"] == 1
    assert user_jobs.regeneration_status(_Runtime(storage), "daily-1")["status"] == "success"
    again = user_jobs.enqueue_regeneration(_Runtime(storage), "daily-1")
    assert again["status"] == "queued"


def test_reconciliation_required_job_does_not_starve_next_pending_key(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    unresolved_key = "m5:regenerate:a"
    ready_key = "m5:regenerate:b"
    storage.rows[unresolved_key] = JobLease(
        unresolved_key, "", 1, 0, "released",
        json.dumps({"report_id": "a", "status": "reconciliation_required"}),
    )
    storage.rows[ready_key] = JobLease(
        ready_key, "", 1, 0, "released", json.dumps({"report_id": "b", "status": "queued"}),
    )
    processed: list[str] = []

    def run(_runtime: Any, lease: JobLease, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        processed.append(lease.job_key)
        return {"report_id": payload["report_id"], "status": "success"}

    monkeypatch.setattr(user_jobs, "_run_regeneration", run)
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)

    assert result["processed"] == 1
    assert processed == [ready_key]


def test_failed_work_exposes_generic_status_and_can_be_requeued(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    user_jobs.enqueue_regeneration(runtime, "daily-1")
    def fail(*_args: Any) -> Any:
        raise RuntimeError("private provider credential")
    monkeypatch.setattr(user_jobs, "_run_regeneration", fail)
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert result["processed"] == 1
    status = user_jobs.regeneration_status(_Runtime(storage), "daily-1")
    assert status["status"] == "error"
    assert "private provider credential" not in str(status)
    assert user_jobs.enqueue_regeneration(_Runtime(storage), "daily-1")["status"] == "queued"


def test_ai_time_deferral_keeps_original_request_queued(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    user_jobs.enqueue_regeneration(runtime, "daily-1")
    key = "m5:regenerate:daily-1"
    nonce = json.loads(storage.rows[key].checkpoint or "{}")["request_nonce"]

    def defer(*_args: Any, **_kwargs: Any) -> Any:
        raise user_jobs.AIRequestDeferred("too little time")

    monkeypatch.setattr(user_jobs, "_run_regeneration", defer)
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    checkpoint = json.loads(storage.rows[key].checkpoint or "{}")

    assert result["jobs"][0]["status"] == "pending"
    assert storage.rows[key].state == "released"
    assert checkpoint["status"] == "queued"
    assert checkpoint["request_nonce"] == nonce


def test_busy_model_review_stays_queued_for_later_timer_retry(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    assert user_jobs.enqueue_review(runtime)["status"] == "queued"
    monkeypatch.setattr(user_jobs, "_run_review",
                        lambda *_args: (_ for _ in ()).throw(user_jobs.ReviewLeaseBusy()))
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert result["jobs"][0]["status"] == "pending"
    assert runtime.db.jobs.get("m5:review").state == "released"
    assert user_jobs.review_status(_Runtime(storage))["status"] == "queued"


def test_saved_report_checkpoint_completes_without_reanalysis(storage: _Storage) -> None:
    runtime = _Runtime(storage)
    report_id = "daily-1"
    lease = JobLease("m5:regenerate:" + report_id, "worker", 2, 2**62, "active",
                     json.dumps({"phase": "saved", "report_id": report_id,
                                 "input_fingerprint": "known"}))
    storage.rows[lease.job_key] = lease
    runtime.db.report = lambda _id: SimpleNamespace(
        id=report_id, generated_at=datetime(2026, 9, 25, tzinfo=UTC),
    )
    assert user_jobs._run_regeneration(runtime, lease, json.loads(lease.checkpoint or "{}"))[
        "status"
    ] == "success"


def test_regeneration_ai_uses_proxy_and_stable_request_nonce(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    user_jobs.enqueue_regeneration(runtime, "daily-1")
    key = "m5:regenerate:daily-1"
    queued = json.loads(storage.rows[key].checkpoint or "{}")
    lease = runtime.db.jobs.acquire(key, "worker", 180)
    assert lease is not None
    observed: dict[str, Any] = {}

    class Analyst:
        client: Any = None
        ledger: Any = SimpleNamespace(pending_for_job=lambda _report_id: None)

    class Service:
        config = SimpleNamespace(openai=SimpleNamespace(enabled=True))
        analyst = Analyst()

        def regenerate(self, _old: Any, **kwargs: Any) -> Any:
            observed["nonce"] = kwargs["request_nonce"]
            observed["client"] = self.analyst.client
            return SimpleNamespace(generated_at=datetime(2026, 9, 25, tzinfo=UTC))

    runtime.analysis = lambda **_kwargs: Service()
    runtime.loaded = SimpleNamespace(secrets=SimpleNamespace(
        openai_api_key=SimpleNamespace(get_secret_value=lambda: "test-key"),
    ))
    runtime.db.source_revision = lambda: 0
    runtime.db.save_report = lambda *_args, **kwargs: observed.update({"fence": kwargs["job_fence"]})
    monkeypatch.setattr(user_jobs, "render_text", lambda _report: "rendered")
    monkeypatch.setattr(user_jobs, "OpenAIAnalyst", Analyst)
    monkeypatch.setattr(user_jobs, "ReportTransport", lambda: httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"{}"),
    ))

    def openai_client(**kwargs: Any) -> object:
        observed["transport"] = kwargs["http_client"]._transport
        observed["retries"] = kwargs["max_retries"]
        return object()

    monkeypatch.setattr(user_jobs, "OpenAI", openai_client)
    monkeypatch.setenv("CLOUD_OPENAI_ACCESS_CONFIRMED", "true")
    result = user_jobs._run_regeneration(runtime, lease, queued)
    assert result["status"] == "success"
    assert observed["nonce"] == queued["request_nonce"]
    assert isinstance(observed["transport"], httpx.MockTransport)
    assert observed["retries"] == 0
    assert observed["fence"] == (key, "worker", lease.attempt)


def test_drain_accepts_570_and_lease_covers_tail_and_grace(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    user_jobs.enqueue_regeneration(runtime, "daily-1")
    acquired: list[tuple[str, int]] = []
    original_acquire = runtime.db.jobs.acquire

    def acquire(key: str, owner: str, lease_seconds: int) -> JobLease | None:
        acquired.append((key, lease_seconds))
        return original_acquire(key, owner, lease_seconds)

    monkeypatch.setattr(runtime.db.jobs, "acquire", acquire)
    monkeypatch.setattr(user_jobs.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(user_jobs, "_run_regeneration", lambda *_args, **_kwargs: {"status": "success"})

    result = user_jobs.drain(runtime, timeout_seconds=570, max_jobs=1)
    assert result["processed"] == 1
    assert acquired[0][0] == "m5:regenerate:daily-1"
    assert acquired[0][1] == 645  # 570 seconds plus publication tail and expiry grace.
    with pytest.raises(ValueError, match="invalid maintenance bounds"):
        user_jobs.drain(runtime, timeout_seconds=571, max_jobs=1)


def test_report_write_lease_tracks_remaining_parent_lease(
    storage: _Storage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(storage)
    report_id = "daily-1"
    key = "m5:regenerate:" + report_id
    checkpoint = json.dumps({"phase": "saved", "report_id": report_id})
    parent = JobLease(key, "worker", 1, time.time_ns() // 1_000 + 300_500_000,
                      "active", checkpoint)
    runtime.db.report = lambda _report_id: SimpleNamespace(
        id=report_id, generated_at=datetime(2026, 9, 25, tzinfo=UTC),
    )
    acquired_ttls: list[int] = []
    original_acquire = runtime.db.jobs.acquire

    def acquire(job_key: str, owner: str, lease_seconds: int) -> JobLease | None:
        acquired_ttls.append(lease_seconds)
        return original_acquire(job_key, owner, lease_seconds)

    monkeypatch.setattr(runtime.db.jobs, "acquire", acquire)
    fixed_now_ns = time.time_ns()
    monkeypatch.setattr(user_jobs.time, "time_ns", lambda: fixed_now_ns)
    result = user_jobs._run_regeneration(runtime, parent, json.loads(checkpoint))

    assert result["status"] == "success"
    assert acquired_ttls == [316]  # ceil(300.5s remaining) plus 15s.


def test_execute_counts_startup_reserves_publication_and_defaults_to_one_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(db=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(user_jobs, "build_runtime", lambda *_args: runtime)
    monkeypatch.setattr(user_jobs.HeavyWorkLease, "acquire", Mock(return_value=Mock()))
    monkeypatch.setattr(user_jobs.time, "monotonic", Mock(side_effect=[0.0, 10.0, 511.0]))
    drain = Mock(return_value={"processed": 1, "jobs": [{"status": "success"}]})
    monkeypatch.setattr(user_jobs, "drain", drain)
    publish = Mock(return_value={"pending_reports": 0})
    monkeypatch.setattr("zont_analyzer.application.publication.publish_reports", publish)

    result = user_jobs.execute({})

    drain.assert_called_once_with(runtime, timeout_seconds=500.0, max_jobs=1)
    publish.assert_not_called()
    assert result["publication"] == {"status": "deferred"}


def test_execute_defers_when_startup_leaves_less_than_publication_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    runtime = SimpleNamespace(db=SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr(user_jobs, "build_runtime", lambda *_args: runtime)
    monkeypatch.setattr(user_jobs.HeavyWorkLease, "acquire", Mock(return_value=Mock()))
    monkeypatch.setattr(user_jobs.time, "monotonic", Mock(side_effect=[0.0, 511.0]))
    drain = Mock()
    monkeypatch.setattr(user_jobs, "drain", drain)
    publish = Mock()
    monkeypatch.setattr("zont_analyzer.application.publication.publish_reports", publish)

    result = user_jobs.execute({"_runtime_timeout_seconds": 570, "max_jobs": 2})

    assert result == {"processed": 0, "jobs": [], "publication": {"status": "deferred"}}
    drain.assert_not_called()
    publish.assert_not_called()
    assert closed == [True]


def test_execute_publishes_batch_when_exactly_60_seconds_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(db=SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(user_jobs, "build_runtime", lambda *_args: runtime)
    monkeypatch.setattr(user_jobs.HeavyWorkLease, "acquire", Mock(return_value=Mock()))
    monkeypatch.setattr(user_jobs.time, "monotonic", Mock(side_effect=[100.0, 110.0, 610.0]))
    drain = Mock(return_value={"processed": 1, "jobs": [{"status": "success"}]})
    monkeypatch.setattr(user_jobs, "drain", drain)
    publish = Mock(return_value={"pending_reports": 0})
    monkeypatch.setattr("zont_analyzer.application.publication.publish_reports", publish)

    result = user_jobs.execute({"_runtime_timeout_seconds": 570, "max_jobs": 2})

    drain.assert_called_once_with(runtime, timeout_seconds=500.0, max_jobs=2)
    publish.assert_called_once_with(runtime, batch_size=8)
    assert result["publication"] == {"pending_reports": 0}


def test_execute_rejects_571_before_opening_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = Mock()
    monkeypatch.setattr(user_jobs, "build_runtime", build)
    with pytest.raises(ValueError, match="invalid maintenance timeout"):
        user_jobs.execute({"_runtime_timeout_seconds": 571})
    build.assert_not_called()


@pytest.mark.parametrize("max_jobs", [0, 9])
def test_execute_rejects_invalid_job_count_before_opening_runtime(
    monkeypatch: pytest.MonkeyPatch, max_jobs: int,
) -> None:
    build = Mock()
    monkeypatch.setattr(user_jobs, "build_runtime", build)
    with pytest.raises(ValueError, match="invalid maintenance bounds"):
        user_jobs.execute({"max_jobs": max_jobs})
    build.assert_not_called()

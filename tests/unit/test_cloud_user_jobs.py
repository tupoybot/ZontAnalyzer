from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

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
    monkeypatch.setattr(user_jobs, "_run_regeneration", lambda *_args: {
        "report_id": "daily-1", "status": "success", "updated_at": "2026-09-25T00:00:00+00:00",
    })
    result = user_jobs.drain(runtime, timeout_seconds=145, max_jobs=1)
    assert result["processed"] == 1
    assert user_jobs.regeneration_status(_Runtime(storage), "daily-1")["status"] == "success"
    again = user_jobs.enqueue_regeneration(_Runtime(storage), "daily-1")
    assert again["status"] == "queued"


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

"""Durable user-triggered cloud work using the existing YDB jobs table."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import httpx
from openai import OpenAI

from zont_analyzer.adapters.openai.model_catalog import OpenAIModelCatalog
from zont_analyzer.adapters.openai.provider import AIRequestPending, OpenAIAnalyst
from zont_analyzer.adapters.ydb.jobs import JobLease, JobLeaseRepository, _job
from zont_analyzer.application.ai_maintenance import local_assessments
from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.application.model_review import ModelReviewStore
from zont_analyzer.application.regeneration import normalize_counterfactual_question
from zont_analyzer.cloud.egress import ReportTransport
from zont_analyzer.reports import render_text
from zont_analyzer.runtime import Runtime, build_runtime

_PREFIX = "m5:"
_REVIEW_KEY = _PREFIX + "review"
_MAX_SCAN = 8


class ReviewLeaseBusy(RuntimeError):
    """A previous review invocation still owns the durable review lease."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_checkpoint(lease: JobLease | None) -> dict[str, Any]:
    if lease is None or not lease.checkpoint:
        return {}
    try:
        value = json.loads(lease.checkpoint)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _queue(runtime: Runtime, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically register a request before returning 202.

    Duplicate clicks while queued or running keep their first payload. A
    completed request may be explicitly started again.
    """
    queued = {**payload, "status": "queued", "updated_at": _now()}

    def put(tx: Any) -> dict[str, Any]:
        rows = tx.execute(
            "DECLARE $key AS Utf8; SELECT job_key,owner,attempt,lease_until,state,checkpoint "
            "FROM jobs WHERE job_key=$key;", {"$key": key},
        )[0].rows
        old = _job(rows[0]) if rows else None
        old_payload = _read_checkpoint(old)
        if old and old.state != "done":
            # An expired owner must resume its original request. It may have
            # dispatched an LLM call or committed the report before dying.
            return _status(old, old_payload)
        if old and old_payload.get("status") == "error":
            queued_value = {**old_payload, "status": "queued", "updated_at": _now()}
        else:
            queued_value = queued
        lease = JobLease(
            key, "", (old.attempt + 1) if old else 1, 0, "released",
            json.dumps(queued_value, ensure_ascii=False, sort_keys=True),
        )
        JobLeaseRepository._put(tx, lease)
        return _status(lease, queued_value)

    return runtime.db.storage.transaction(put)


def _status(lease: JobLease | None, payload: dict[str, Any]) -> dict[str, Any]:
    if lease is None:
        return payload
    result = dict(payload)
    result.pop("phase", None)
    result.pop("input_fingerprint", None)
    result.pop("source_revision", None)
    result.pop("request_nonce", None)
    if payload.get("phase") == "saved":
        result["status"] = "success"
    elif lease.state == "active" and lease.lease_until > time.time_ns() // 1_000:
        result["status"] = "running"
    elif lease.state == "active":
        result["status"] = "queued"
    return result


def enqueue_regeneration(runtime: Runtime, report_id: str, question: str | None = None) -> dict[str, Any]:
    question = normalize_counterfactual_question(question)
    report = runtime.db.report(report_id)
    if report is None:
        raise KeyError(report_id)
    if report.kind == "initial":
        raise ValueError("Перегенерация доступна для дневного, недельного, месячного и сезонного отчёта.")
    payload: dict[str, Any] = {"report_id": report_id, "request_nonce": str(uuid.uuid4())}
    if question is not None:
        payload["question"] = question
    return _queue(runtime, _PREFIX + "regenerate:" + report_id, payload)


def regeneration_status(runtime: Runtime, report_id: str) -> dict[str, Any]:
    lease = runtime.db.jobs.get(_PREFIX + "regenerate:" + report_id)
    if lease is None:
        return {"report_id": report_id, "status": "idle"}
    return _status(lease, _read_checkpoint(lease))


def enqueue_review(runtime: Runtime) -> dict[str, Any]:
    return _queue(runtime, _REVIEW_KEY, {"kind": "review"})


def review_status(runtime: Runtime) -> dict[str, Any]:
    lease = runtime.db.jobs.get(_REVIEW_KEY)
    return _status(lease, _read_checkpoint(lease))


def _run_regeneration(runtime: Runtime, lease: JobLease, payload: dict[str, Any]) -> dict[str, Any]:
    report_id = str(payload["report_id"])
    old = runtime.db.report(report_id)
    if old is None:
        raise KeyError(report_id)
    if payload.get("phase") == "saved":
        return {"report_id": report_id, "status": "success", "updated_at": _now(),
                "generated_at": old.generated_at.isoformat()}
    source_revision = runtime.db.source_revision()
    fingerprint = hashlib.sha256(
        json.dumps({"report_id": report_id, "question": payload.get("question"),
                    "source_revision": source_revision}, sort_keys=True).encode()
    ).hexdigest()
    checkpoint = {**payload, "status": "running", "phase": "analyze",
                  "source_revision": source_revision, "input_fingerprint": fingerprint,
                  "updated_at": _now()}
    if not runtime.db.jobs.checkpoint(lease.job_key, lease.owner, lease.attempt,
                                      json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)):
        raise RuntimeError("job ownership expired")
    with ExitStack() as stack:
        service = runtime.analysis(job_fence=(lease.job_key, lease.owner, lease.attempt))
        if service.config.openai.enabled:
            if not isinstance(service.analyst, OpenAIAnalyst):
                raise RuntimeError("OpenAI is enabled but no API key is configured")
            if os.environ.get("CLOUD_OPENAI_ACCESS_CONFIRMED") != "true":
                raise PermissionError("OpenAI access conditions are not confirmed")
            http_client = stack.enter_context(httpx.Client(
                transport=ReportTransport(), follow_redirects=False,
                trust_env=False, timeout=120.0,
            ))
            api_key = runtime.loaded.secrets.openai_api_key
            assert api_key is not None
            service.analyst.client = OpenAI(
                api_key=api_key.get_secret_value(), max_retries=0,
                timeout=120.0, http_client=http_client,
            )
        candidate = service.regenerate(
            old, request_nonce=str(payload["request_nonce"]), question=payload.get("question"),
        )
    # save_report verifies the live lease, attempt and source revision in the
    # same YDB transaction as the report write.
    runtime.db.save_report(candidate, render_text(candidate),
                           source_revision=source_revision,
                           job_fence=(lease.job_key, lease.owner, lease.attempt))
    return {"report_id": report_id, "status": "success", "updated_at": _now(),
            "generated_at": candidate.generated_at.isoformat(),
            **({"question": payload["question"]} if "question" in payload else {})}


def _run_review(runtime: Runtime, _lease: JobLease, payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(transport=ReportTransport(), follow_redirects=False,
                      trust_env=False, timeout=15.0) as client:
        catalog = OpenAIModelCatalog(client=client)
        settings = AISettingsStore(runtime.db, runtime.config).snapshot()
        store = ModelReviewStore(runtime.db, catalog, assessments=local_assessments(runtime))
        requested_at = datetime.fromisoformat(str(payload["updated_at"]))
        for run in store.state(settings)["runs"]:
            if (run.get("trigger") == "manual" and run.get("finished_at")
                    and datetime.fromisoformat(str(run["started_at"])) >= requested_at):
                return {"kind": "review", "status": "success", "updated_at": _now()}
        result = store.run_if_due(settings, trigger="manual")
    if result is None:
        raise ReviewLeaseBusy()
    return {"kind": "review", "status": "success", "updated_at": _now()}


def _pending_keys(runtime: Runtime) -> list[str]:
    rows = runtime.db.storage.execute(
        "DECLARE $now AS Int64; "
        "SELECT job_key FROM jobs WHERE job_key >= 'm5:' AND job_key < 'm5;' "
        "AND (state='released' OR (state='active' AND lease_until <= $now)) "
        "ORDER BY job_key LIMIT 8;",
        {"$now": time.time_ns() // 1_000},
    )[0].rows
    return [value.decode() if isinstance((value := row.job_key), bytes) else str(value)
            for row in rows]


def drain(runtime: Runtime, *, timeout_seconds: float = 145, max_jobs: int = 2) -> dict[str, Any]:
    """Run a bounded number of queued jobs; expired leases can be retried."""
    if not 1 <= max_jobs <= _MAX_SCAN or not 1 <= timeout_seconds <= 180:
        raise ValueError("invalid maintenance bounds")
    deadline = time.monotonic() + timeout_seconds
    results: list[dict[str, Any]] = []
    for key in _pending_keys(runtime):
        if len(results) >= max_jobs or deadline - time.monotonic() < 135:
            break
        owner = str(uuid.uuid4())
        lease = runtime.db.jobs.acquire(key, owner, math.ceil(deadline - time.monotonic()) + 15)
        if lease is None:
            continue
        payload = _read_checkpoint(lease)
        if not payload:
            runtime.db.jobs.release(key, owner, lease.attempt)
            continue
        try:
            outcome = (_run_review(runtime, lease, payload) if key == _REVIEW_KEY
                       else _run_regeneration(runtime, lease, payload))
        except AIRequestPending:
            # A provider response may have been lost after dispatch. Keep the
            # original request identity so later reconciliation cannot send
            # a second request with a new nonce.
            pending = {field: value for field, value in payload.items()
                       if field in {"report_id", "question", "request_nonce", "kind"}}
            pending.update({"status": "reconciliation_required", "updated_at": _now()})
            if not runtime.db.jobs.checkpoint(key, owner, lease.attempt,
                                              json.dumps(pending, ensure_ascii=False, sort_keys=True)):
                raise RuntimeError("job ownership expired") from None
            runtime.db.jobs.release(key, owner, lease.attempt)
            results.append({"job_key": key, "status": "reconciliation_required"})
            continue
        except ReviewLeaseBusy:
            runtime.db.jobs.release(key, owner, lease.attempt)
            results.append({"job_key": key, "status": "pending"})
            continue
        except Exception as exc:
            # Do not expose provider/DB failure text through status responses.
            outcome = {key: value for key, value in payload.items()
                       if key in {"report_id", "question", "request_nonce", "kind"}}
            outcome.update({"status": "error", "updated_at": _now(),
                            "error": "Задание не завершилось. Повторите запрос."})
            results.append({"job_key": key, "status": "error", "error_type": type(exc).__name__})
        else:
            results.append({"job_key": key, "status": "success"})
        if not runtime.db.jobs.checkpoint(key, owner, lease.attempt,
                                          json.dumps(outcome, ensure_ascii=False, sort_keys=True)):
            raise RuntimeError("job ownership expired")
        if not runtime.db.jobs.complete(key, owner, lease.attempt):
            raise RuntimeError("job ownership expired")
    return {"processed": len(results), "jobs": results}


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"max_jobs", "_runtime_timeout_seconds"}
    if set(payload) - allowed:
        raise ValueError("unknown maintenance fields")
    timeout = float(payload.get("_runtime_timeout_seconds", 150))
    max_jobs = payload.get("max_jobs", 2)
    if type(max_jobs) is not int:
        raise ValueError("max_jobs must be integer")
    runtime = build_runtime(None, None)
    try:
        result = drain(runtime, timeout_seconds=max(1, min(timeout - 30, 145)), max_jobs=max_jobs)
        from zont_analyzer.application.publication import publish_reports

        result["publication"] = publish_reports(runtime, batch_size=8)
        return result
    finally:
        runtime.db.close()

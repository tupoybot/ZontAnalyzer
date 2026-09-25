"""Fenced, resumable cloud report jobs with bounded archive collection."""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as calendar_date
from typing import Any, Literal

import httpx
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zont_analyzer.adapters.openai.provider import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    AIRequestPending,
    OpenAIAnalyst,
)
from zont_analyzer.adapters.ydb.jobs import JobLease, JobLeaseRepository
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.application.collection import CollectionService
from zont_analyzer.cloud.egress import ReportTransport
from zont_analyzer.domain import Report
from zont_analyzer.runtime import Runtime, build_runtime

MIN_AI_SECONDS = 135
MIN_COLLECTION_SECONDS = 20


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _canonical_digest(value: Any) -> bytes:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).digest()


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["daily", "weekly", "monthly"]
    date: str | None = None
    year: int | None = Field(default=None, ge=2000, le=2100)
    week: int | None = Field(default=None, ge=1, le=53)
    month: int | None = Field(default=None, ge=1, le=12)
    max_requests: int = Field(default=4, ge=1, le=8)
    use_ai: bool = True
    refresh: bool = False

    @model_validator(mode="after")
    def period_fields(self) -> ReportRequest:
        if self.kind == "daily":
            if self.date is None or any(value is not None for value in (self.year, self.week, self.month)):
                raise ValueError("daily requires only date")
            if len(self.date) != 10 or calendar_date.fromisoformat(self.date).isoformat() != self.date:
                raise ValueError("daily date must be YYYY-MM-DD")
        elif self.kind == "weekly":
            if self.year is None or self.week is None or self.date is not None or self.month is not None:
                raise ValueError("weekly requires only year and week")
            calendar_date.fromisocalendar(self.year, self.week, 1)
        elif (self.year is None or self.month is None or self.date is not None or self.week is not None):
            raise ValueError("monthly requires only year and month")
        return self

    def selected_date(self) -> calendar_date:
        if self.kind == "daily":
            assert self.date is not None
            return calendar_date.fromisoformat(self.date)
        assert self.year is not None
        if self.kind == "weekly":
            assert self.week is not None
            return calendar_date.fromisocalendar(self.year, self.week, 1)
        assert self.month is not None
        return calendar_date(self.year, self.month, 1)


@dataclass(frozen=True)
class Period:
    kind: str
    start: datetime
    end: datetime
    job_key: str


class ReportJobRunner:
    """One invocation owns at most one phase and never starts work after its deadline."""

    def __init__(
        self, runtime: Runtime, *,
        client_factory: Callable[[], ZontReadOnlyClient] | None = None,
        jobs: JobLeaseRepository | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runtime = runtime
        self.client_factory = client_factory or self._live_client
        self.jobs = jobs or runtime.db.jobs
        self.now, self.monotonic = now, monotonic

    def period(self, request: ReportRequest) -> Period:
        service = self.runtime.analysis(no_ai=True)
        selected = request.selected_date()
        start, _ = service.local_day_window(selected)
        if request.kind == "daily":
            _, end = service.local_day_window(selected)
        elif request.kind == "weekly":
            _, end = service.local_day_window(selected + timedelta(days=6))
        else:
            assert request.month is not None and request.year is not None
            last_day = calendar.monthrange(request.year, request.month)[1]
            _, end = service.local_day_window(calendar_date(request.year, request.month, last_day))
        mode = "ai" if request.use_ai else "deterministic"
        key = f"report:{request.kind}:{int(start.timestamp())}:{int(end.timestamp())}:{mode}:report-v2"
        return Period(request.kind, start, end, key)

    def run(self, payload: dict[str, Any], *, timeout_seconds: float = 180) -> dict[str, Any]:
        request = ReportRequest.model_validate(payload)
        if not 1 <= timeout_seconds <= 180:
            raise ValueError("invalid report timeout")
        period = self.period(request)
        reference = self.now().astimezone(UTC)
        due = period.end + (timedelta(minutes=self.runtime.config.pilot.daily_report_delay_minutes)
                            if request.kind == "daily" else timedelta())
        if reference < due:
            return {"status": "not_due", "job_key": period.job_key, "due_at": due.isoformat()}
        imported = self._imported_artifact(period, refresh=request.refresh)
        if imported is not None:
            if imported.get("status") == "stored_imported":
                self._publish(imported)
            return imported
        deadline = self.monotonic() + timeout_seconds - 5
        owner = str(uuid.uuid4())
        lease = self.jobs.acquire(period.job_key, owner, math.ceil(timeout_seconds) + 30)
        if lease is None:
            existing = self.jobs.get(period.job_key)
            if existing is not None and existing.state == "done":
                checkpoint = self._checkpoint(existing)
                fingerprint, _revision = self._input_state()
                report_id = checkpoint.get("report_id")
                report_exists = isinstance(report_id, str) and self.runtime.db.report(report_id) is not None
                if (checkpoint.get("input_fingerprint") == fingerprint
                        and report_exists):
                    result = {"status": "done", "job_key": period.job_key,
                              "report_id": report_id, "reused": True}
                    self._publish(result)
                    return result
                lease = self.jobs.reopen_completed(
                    period.job_key, owner, math.ceil(timeout_seconds) + 30,
                    existing.checkpoint, fingerprint, force=not report_exists,
                )
            if lease is None:
                return {"status": "busy", "job_key": period.job_key}
        try:
            try:
                return self._advance(request, period, lease, deadline)
            except AIRequestPending as exc:
                return {"status": "reconciliation_required", "phase": "analyze",
                        "job_key": period.job_key, "request_key": exc.request_key,
                        "request_status": exc.status}
        finally:
            # A killed child cannot run this; the lease then expires and a later
            # invocation resumes from durable coverage and the last checkpoint.
            self.jobs.release(period.job_key, owner, lease.attempt)

    @staticmethod
    def _checkpoint(lease: JobLease) -> dict[str, Any]:
        if not lease.checkpoint:
            return {"phase": "collect"}
        value = json.loads(lease.checkpoint)
        if not isinstance(value, dict) or value.get("phase") not in {"collect", "analyze", "saved"}:
            raise ValueError("invalid report job checkpoint")
        return value

    def _save_checkpoint(self, lease: JobLease, value: dict[str, Any]) -> None:
        if not self.jobs.checkpoint(lease.job_key, lease.owner, lease.attempt, json.dumps(value, sort_keys=True)):
            raise RuntimeError("report job ownership expired")

    def _imported_artifact(self, period: Period, *, refresh: bool) -> dict[str, Any] | None:
        """Return an exact imported artifact; it makes no claim about current inputs."""
        def read(tx: Any) -> dict[str, Any] | None:
            metadata = tx.execute(
                "SELECT name,value FROM metadata WHERE name IN ('sqlite_import_state','sqlite_import_sha256');"
            )[0].rows
            values = {_text(row.name): _text(row.value) for row in metadata}
            try:
                state = json.loads(values.get("sqlite_import_state", "null"))
            except (TypeError, ValueError):
                return None
            if not isinstance(state, dict):
                return None
            if state.get("state") == "running":
                return {"status": "import_in_progress", "job_key": period.job_key}
            digest = state.get("source_sha256")
            if (refresh or state.get("state") != "complete" or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or digest != values.get("sqlite_import_sha256")):
                return None
            rows = tx.execute(
                "DECLARE $kind AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                "SELECT kind,period_start,period_end,algorithm_version,id,payload FROM reports "
                "WHERE kind=$kind AND period_start=$start AND period_end=$end LIMIT 2;",
                {"$kind": period.kind, "$start": int(period.start.timestamp()),
                 "$end": int(period.end.timestamp())},
            )[0].rows
            if len(rows) != 1:
                return None
            row = rows[0]
            report_id = _text(row.id)
            manifest_rows = tx.execute(
                "DECLARE $key AS Utf8; SELECT target_table,target_key,checksum,payload "
                "FROM migration_records WHERE source_table='reports' AND source_key=$key;",
                {"$key": json.dumps([report_id], separators=(",", ":"))},
            )[0].rows
            if len(manifest_rows) != 1:
                return None
            manifest = manifest_rows[0]
            target_key = {"kind": _text(row.kind), "period_start": int(row.period_start),
                          "period_end": int(row.period_end), "algorithm_version": _text(row.algorithm_version)}
            if _text(manifest.target_table) != "reports":
                return None
            try:
                source = json.loads(_text(manifest.payload))
                target = json.loads(_text(manifest.target_key))
                saved = json.loads(_text(row.payload))
                if not isinstance(source, dict) or not isinstance(saved, dict):
                    return None
                source_report = json.loads(source["canonical_json"])
                stored_report = saved["report"]
                report = Report.model_validate(stored_report)
                same_content = _canonical_digest(source_report) == _canonical_digest(stored_report)
                valid_source = _canonical_digest(source).hex() == _text(manifest.checksum)
            except (TypeError, ValueError, KeyError):
                return None
            if (source.get("id") != report_id or target != target_key
                    or report.id != report_id or report.kind != period.kind
                    or report.period_start.astimezone(UTC) != period.start
                    or report.period_end.astimezone(UTC) != period.end
                    or not re.fullmatch(r"[0-9a-f]{64}", _text(manifest.checksum))):
                return None
            if not same_content or not valid_source:
                return None
            return {"status": "stored_imported", "job_key": period.job_key,
                    "report_id": report_id, "ai_used": report.ai_used,
                    "reused": True, "freshness": "unverified"}

        return self.runtime.db.storage.transaction(read)

    def _input_state(self) -> tuple[str, int]:
        """Snapshot source revisions without output-only publication revisions."""
        def read(tx: Any) -> tuple[list[tuple[str, int]], int]:
            rows = tx.execute("SELECT scope,revision FROM revisions ORDER BY scope;")[0].rows
            scopes = [(str(row.scope), int(row.revision)) for row in rows]
            publication = next((revision for scope, revision in scopes if scope == "publication"), 0)
            return [(scope, revision) for scope, revision in scopes if scope != "publication"], publication

        sources, publication = self.runtime.db.storage.transaction(read)
        config = self.runtime.config.model_dump(mode="json")
        encoded = json.dumps({"sources": sources, "config": config,
                              "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION},
                             sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest(), publication

    def _advance(self, request: ReportRequest, period: Period, lease: JobLease, deadline: float) -> dict[str, Any]:
        checkpoint = self._checkpoint(lease)
        phase = checkpoint["phase"]
        if phase == "saved":
            report_id = checkpoint.get("report_id")
            fingerprint, _revision = self._input_state()
            if (checkpoint.get("input_fingerprint") == fingerprint and isinstance(report_id, str)
                    and self.runtime.db.report(report_id) is not None):
                return self._complete(lease, report_id, checkpoint.get("ai_used"))
            phase = "analyze"
        if phase == "collect":
            if deadline - self.monotonic() < MIN_COLLECTION_SECONDS:
                return {"status": "pending", "phase": "collect", "job_key": period.job_key}
            with contextlib.closing(self.client_factory()) as client:
                if not self.runtime.db.list_devices():
                    self.runtime.db.save_devices(client.discover_devices())
                result = CollectionService(self.runtime.db, client, self.runtime.config).ensure_period(
                    period.start, period.end, now=self.now(), max_requests=request.max_requests,
                )
            if not result["complete"]:
                self._save_checkpoint(lease, {"phase": "collect", "last_collection": result})
                return {"status": "pending", "phase": "collect", "job_key": period.job_key,
                        "collection": result}
            self._save_checkpoint(lease, {"phase": "analyze", "last_collection": result})
            return {"status": "pending", "phase": "analyze", "job_key": period.job_key,
                    "collection": result}
        if deadline - self.monotonic() < (MIN_AI_SECONDS if request.use_ai else MIN_COLLECTION_SECONDS):
            return {"status": "pending", "phase": "analyze", "job_key": period.job_key}
        fingerprint, source_revision = self._input_state()
        self._save_checkpoint(lease, {"phase": "analyze", "input_fingerprint": fingerprint,
                                      "source_revision": source_revision})
        with contextlib.ExitStack() as stack:
            service = self.runtime.analysis(no_ai=not request.use_ai,
                                            job_fence=(lease.job_key, lease.owner, lease.attempt))
            if request.use_ai and service.config.openai.enabled:
                if not isinstance(service.analyst, OpenAIAnalyst):
                    raise RuntimeError("OpenAI is enabled but no API key is configured")
                if os.environ.get("CLOUD_OPENAI_ACCESS_CONFIRMED") != "true":
                    raise PermissionError("OpenAI access conditions are not confirmed")
                transport = ReportTransport()
                http_client = stack.enter_context(httpx.Client(
                    transport=transport, follow_redirects=False, trust_env=False, timeout=120.0,
                ))
                api_key = self.runtime.loaded.secrets.openai_api_key
                assert api_key is not None
                service.analyst.client = OpenAI(
                    api_key=api_key.get_secret_value(), max_retries=0, timeout=120.0,
                    http_client=http_client,
                )
            if request.kind == "daily":
                report = service.analyze_daily(request.selected_date(), use_ai=request.use_ai)
            elif request.kind == "weekly":
                assert request.year is not None and request.week is not None
                report = service.analyze_week(request.year, request.week, use_ai=request.use_ai)
            else:
                assert request.year is not None and request.month is not None
                report = service.analyze_month(request.year, request.month, use_ai=request.use_ai)
        # ReportRepository committed this checkpoint atomically with the report.
        return self._complete(lease, report.id, report.ai_used)

    def _complete(self, lease: JobLease, report_id: str, ai_used: bool | None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "done", "job_key": lease.job_key, "report_id": report_id,
                                  "ai_used": ai_used, "reused": False}
        # The report and checkpoint already exist; retries resume publication.
        self._publish(result)
        if not self.jobs.complete(lease.job_key, lease.owner, lease.attempt):
            raise RuntimeError("report job ownership expired")
        return result

    def _publish(self, result: dict[str, Any]) -> None:
        if os.environ.get("CLOUD_PUBLICATION_BUCKET"):
            from zont_analyzer.application.publication import publish_reports

            result["publication"] = publish_reports(self.runtime)

    def _live_client(self) -> ZontReadOnlyClient:
        token = self.runtime.loaded.secrets.zont_token
        email = self.runtime.config.zont.client_email
        if token is None or not email:
            raise RuntimeError("ZONT read credentials are required")
        return ZontReadOnlyClient(
            token=token.get_secret_value(), client_email=email,
            base_url=self.runtime.config.zont.base_url,
            timeout=min(self.runtime.config.zont.request_timeout_seconds, 10),
            transport=ReportTransport(),
        )


def execute(payload: dict[str, Any], *, timeout_seconds: float = 180) -> dict[str, Any]:
    runtime = build_runtime(None, None)
    try:
        return ReportJobRunner(runtime).run(payload, timeout_seconds=timeout_seconds)
    finally:
        runtime.db.close()

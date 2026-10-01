"""Read-only, bounded operational snapshots; no provider calls or model decisions."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.application.model_review import ModelReviewStore
from zont_analyzer.observability import observe, span

if TYPE_CHECKING:
    from zont_analyzer.domain import Report
    from zont_analyzer.runtime import Runtime

_QUEUE_LIMIT = 1000


def queues(
    runtime: Runtime, publication: dict[str, Any] | None = None, *, deadline: float | None = None,
) -> None:
    """Sample durable materialized queues without changing completed work.

    Publication results reuse the publisher's committed in-memory index. Other
    reads return at most 1001 rows; truncated counts are explicitly lower bounds.
    Neither queue includes future scheduler work or unconsumed change records.
    """
    for kind in ("publication", "manual"):
        try:
            if deadline is not None and deadline - time.monotonic() < 8:
                raise TimeoutError("insufficient queue sampling time")
            if kind == "publication":
                counts, oldest, truncated = _publication_queue(runtime, publication)
            else:
                counts, oldest, truncated = _manual_queue(runtime)
        except Exception:  # noqa: BLE001 - queue health must not affect application work
            observe("zont_queue_snapshot_success", 0, kind=kind)
        else:
            for status in ("pending", "running", "blocked"):
                observe("zont_queue_items", counts.get(status, 0), kind=kind, status=status)
            observe("zont_queue_truncated", float(truncated), kind=kind)
            # Clear a previous nonempty sample when oldest is unavailable.
            observe("zont_queue_oldest_timestamp_seconds", oldest or 0, kind=kind)
            observe("zont_queue_snapshot_success", 1, kind=kind)
        finally:
            observe("zont_queue_observed_timestamp_seconds", time.time(), kind=kind)


def _publication_queue(
    runtime: Runtime, publication: dict[str, Any] | None,
) -> tuple[dict[str, int], float | None, bool]:
    if publication is not None and "pending_reports" in publication:
        pending = int(publication["pending_reports"])
        oldest = publication.get("pending_oldest_timestamp_seconds") if pending else None
        return {"pending": pending}, oldest, False
    rows = runtime.db.storage.execute(
        "SELECT queued_at FROM publication_items VIEW by_queue WHERE dirty>0 "
        "ORDER BY dirty,queued_at LIMIT 1001;",
        timeout_seconds=2,
    )[0].rows
    truncated = len(rows) > _QUEUE_LIMIT
    oldest = min((int(row.queued_at) / 1_000_000 for row in rows), default=None)
    return {"pending": min(len(rows), _QUEUE_LIMIT)}, None if truncated else oldest, truncated


def _manual_queue(runtime: Runtime) -> tuple[dict[str, int], float | None, bool]:
    rows = runtime.db.storage.execute(
        "SELECT state,lease_until,checkpoint FROM jobs "
        "WHERE job_key >= 'm5:' AND job_key < 'm5;' AND (state='active' OR state='released') "
        "ORDER BY job_key LIMIT 1001;",
        timeout_seconds=2,
    )[0].rows
    counts = {"pending": 0, "running": 0, "blocked": 0}
    oldest: float | None = None
    now_us = time.time_ns() // 1_000
    for row in rows[:_QUEUE_LIMIT]:
        payload = json.loads(row.checkpoint)
        if not isinstance(payload, dict):
            raise ValueError("invalid manual job checkpoint")
        if payload.get("status") == "reconciliation_required":
            status = "blocked"
        elif row.state == "active" and int(row.lease_until) > now_us:
            status = "running"
        else:
            status = "pending"
        counts[status] += 1
        # updated_at is the durable last transition, not the original enqueue
        # time (which the current jobs contract does not retain).
        if payload.get("updated_at"):
            timestamp = datetime.fromisoformat(payload["updated_at"]).timestamp()
            oldest = timestamp if oldest is None else min(oldest, timestamp)
    truncated = len(rows) > _QUEUE_LIMIT
    return counts, None if truncated else oldest, truncated


def snapshot(runtime: Runtime) -> None:
    """Small aggregate/key reads, isolated from the result of completed work."""
    try:
        with span("zont_snapshot"):
            _snapshot(runtime)
    except Exception:  # noqa: BLE001 - snapshot health reports failure separately
        pass


def _snapshot(runtime: Runtime) -> None:
    series = runtime.db.storage.execute("SELECT id FROM telemetry_series LIMIT 65;")[0].rows
    if len(series) > 64:
        raise ValueError("telemetry snapshot exceeds series bound")
    latest = 0
    if series:
        # Keep each primary-key seek and LIMIT; batching reduces round trips
        # without scanning the histories of all series to compute MAX().
        query = " ".join(f"DECLARE $id{i} AS Int64;" for i in range(len(series))) + " " + " ".join(
            f"SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id{i} "
            "ORDER BY timestamp_utc DESC LIMIT 1;" for i in range(len(series))
        )
        results = runtime.db.storage.execute(query, {f"$id{i}": int(row.id) for i, row in enumerate(series)})
        timestamps = (int(row.timestamp_utc) for result in results for row in result.rows)
        latest = max(0, max(timestamps, default=0))
    observe("zont_telemetry_present", float(latest > 0))
    if latest:
        observe("zont_telemetry_timestamp_seconds", latest)
    month = datetime.now(UTC).strftime("%Y-%m")
    rows = runtime.db.storage.execute(
        "DECLARE $month AS Utf8; SELECT charged_tokens,reserved_tokens "
        "FROM ai_budget_months WHERE month=$month;", {"$month": month},
    )[0].rows
    observe("zont_monthly_ai_tokens", int(rows[0].charged_tokens) if rows else 0)
    observe("zont_monthly_ai_reserved_tokens", int(rows[0].reserved_tokens) if rows else 0)
    observe("zont_monthly_ai_token_budget", runtime.config.openai.monthly_token_budget)
    rows = runtime.db.storage.execute(
        "SELECT payload FROM model_review_state WHERE scope='installation';",
    )[0].rows
    state: dict[str, Any] = json.loads(rows[0].payload) if rows else {}
    observe("zont_model_review_initialized", float(bool(rows)))
    observe("zont_model_review_attempts", state.get("attempts", 0))
    observe("zont_model_review_error", float(bool(state.get("last_error"))))
    effective = AISettingsStore(runtime.db, runtime.config).snapshot()["effective"]
    review_enabled = bool(effective.get("review_enabled", True))
    observe("zont_model_review_enabled", float(review_enabled))
    if state.get("last_success_at"):
        observe("zont_model_review_last_success_timestamp_seconds",
                datetime.fromisoformat(state["last_success_at"]).timestamp())
    # Reuse scheduling rules without state(), which can supersede proposals.
    if rows and review_enabled:
        next_due = ModelReviewStore._next_due(state, effective)
        if next_due is not None:
            observe("zont_model_review_next_due_timestamp_seconds", next_due.timestamp())
    rows = runtime.db.storage.execute(
        "SELECT payload FROM model_review_proposals LIMIT 1001;",
    )[0].rows
    if len(rows) > 1000:
        raise ValueError("model proposal snapshot exceeds bound")
    counts: dict[str, int] = {}
    for row in rows:
        status = json.loads(row.payload).get("status", "")
        counts[status] = counts.get(status, 0) + 1
    for status in ("open", "deferred", "accepted", "rejected", "superseded"):
        observe("zont_model_review_proposals", counts.get(status, 0), status=status)


def report_metrics(report: Report) -> None:
    """Export existing reliability facts with evidence quality, never raw events."""
    observe("zont_report_generated_timestamp_seconds", report.generated_at.timestamp())
    for metric in report.metrics:
        if metric.name in {"boiler_mtbf_hours", "boiler_mttr_hours"}:
            observe("zont_reliability_" + metric.name, metric.value)
            continue
        if metric.name not in {"zont_uptime_seconds", "boiler_uptime_seconds"}:
            continue
        component = "zont" if metric.name.startswith("zont") else "boiler"
        if isinstance(metric.value, (int, float)):
            observe("zont_reliability_uptime_seconds", float(metric.value), component=component)
        for key in ("data_fresh", "lower_bound", "continuity_uncertain"):
            if isinstance(metric.context.get(key), bool):
                observe("zont_reliability_evidence", float(metric.context[key]), component=component, quality=key)

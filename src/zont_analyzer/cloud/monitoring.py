"""Read-only, bounded operational snapshots; no provider calls or model decisions."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zont_analyzer.observability import observe, span

if TYPE_CHECKING:
    from zont_analyzer.domain import Report
    from zont_analyzer.runtime import Runtime


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
    for series_row in series:
        samples = runtime.db.storage.execute(
            "DECLARE $id AS Int64; SELECT timestamp_utc FROM telemetry_samples WHERE series_id=$id "
            "ORDER BY timestamp_utc DESC LIMIT 1;", {"$id": int(series_row.id)},
        )[0].rows
        if samples:
            latest = max(latest, int(samples[0].timestamp_utc))
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
    for key, metric in (
        ("next_due_at", "zont_model_review_next_due_timestamp_seconds"),
        ("last_success_at", "zont_model_review_last_success_timestamp_seconds"),
    ):
        if state.get(key):
            observe(metric, datetime.fromisoformat(state[key]).timestamp())
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

"""Durable production scheduling, one bounded phase per private timer call.

The timer is only a wake-up signal. Calendar boundaries, polling intervals,
AI policy and durable progress come from the application and YDB, never from
the timer payload. User jobs and publication retain their separate timer.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import math
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.application.analysis import CALCULATION_VERSION, AnalysisService
from zont_analyzer.application.period_schedule import (
    _already_current,
    schedule_signature,
    scheduled_periods,
    seasonal_daily_signature,
)
from zont_analyzer.application.pilot import _report_source_event_revision
from zont_analyzer.cloud.report_jobs import MIN_COLLECTION_SECONDS, ReportJobRunner
from zont_analyzer.domain import Report
from zont_analyzer.domain.periods import Period, calendar_period, midnight
from zont_analyzer.observability import observe
from zont_analyzer.runtime import Runtime, build_runtime

_KEY = "production-scheduler:v1"
_LANES = ("sync", "daily", "weekly", "monthly", "seasonal", "review")
_BASELINES = "source-event-report-baselines:v1:complete"
_SCAN_LIMIT = 4


def _observe_lane(lane: str, result: dict[str, Any]) -> None:
    """Separate successful timer delivery from the outcome of its work."""
    if result.get("status") in {"idle", "busy", "not_due", "import_in_progress"}:
        return
    failed = result.get("status") == "error" or any(
        bool(result.get(key, {}).get("failed_windows", 0)) for key in ("sync", "collection")
    )
    operation = "scheduler_" + lane
    observe("zont_cloud_job_observed_timestamp_seconds", time.time(), operation=operation)
    observe("zont_cloud_job_success", float(not failed), operation=operation)
    observe("zont_invocations_total", operation=operation, outcome="failure" if failed else "success")


def daily_needs_report(analysis: AnalysisService, selected: date, yesterday: date) -> bool:
    """The pilot's content-based repair rule, including imported legacy markers."""
    db = analysis.db
    start, end = analysis.local_day_window(selected)
    report = db.report(analysis.report_id_for("daily", start))
    if report is None:
        return True
    data_revision = db.period_data_revision(start, end)
    empty_revision = hashlib.sha256(b"[]").hexdigest()
    stored_revision = report.context.get("input_revision", {}).get("telemetry")
    unchanged_import = (stored_revision is None and data_revision != empty_revision
                        and db.legacy_period_data_revision(start, end) == empty_revision)
    if (isinstance(stored_revision, str) and not stored_revision.startswith("telemetry-v2:")
            and stored_revision != data_revision
            and stored_revision == db.legacy_period_data_revision(start, end)):
        if not db.upgrade_report_telemetry_revision(report.id, stored_revision, data_revision):
            raise RuntimeError("report changed during revision upgrade")
        report = report.model_copy(deep=True)
        report.context["input_revision"]["telemetry"] = data_revision
    return bool(
        (selected == yesterday and report.context.get("calculation_version") != CALCULATION_VERSION)
        or _report_source_event_revision(db, report) != db.source_event_revision(end)
        or (not unchanged_import and (data_revision != empty_revision or (
            isinstance(stored_revision, str) and stored_revision.startswith("telemetry-v2:")
        )) and report.context.get("input_revision", {}).get("telemetry") != data_revision)
    )


def period_needs_report(analysis: AnalysisService, period: Period) -> bool:
    identifier = analysis.report_id_for(period.kind, period.start)
    previous = analysis.db.report(identifier)
    signature = schedule_signature(analysis, period)
    if previous is not None and previous.period_end == period.observed_end:
        stored_daily = previous.context.get("scheduler_daily_signature")
        if stored_daily is not None and stored_daily != seasonal_daily_signature(analysis, period):
            return True
    if _already_current(previous, period, signature):
        return False
    if previous is not None and (
        _report_source_event_revision(analysis.db, previous) == analysis.db.source_event_revision(period.observed_end)
    ):
        # Exact upgrades of old signatures do not represent new facts and must
        # not purchase another model interpretation.
        for old_revision, old_ai, old_events in itertools.product((False, True), repeat=3):
            if not (old_revision or old_ai or old_events):
                continue
            old_signature = schedule_signature(
                analysis, period, legacy_revision=old_revision,
                legacy_ai_config=old_ai, legacy_source_events=old_events,
            )
            if previous.context.get("schedule_signature") == old_signature:
                if not analysis.db.upgrade_report_schedule_signature(identifier, old_signature, signature):
                    raise RuntimeError("report changed during schedule signature upgrade")
                return False
    return True


class ProductionScheduler:
    def __init__(
        self, runtime: Runtime, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic, runner: ReportJobRunner | None = None,
    ) -> None:
        self.runtime, self.now, self.monotonic = runtime, now, monotonic
        self.runner = runner or ReportJobRunner(runtime, now=now, monotonic=monotonic, publish=False)

    def run(self, *, timeout_seconds: float = 180) -> dict[str, Any]:
        if not 1 <= timeout_seconds <= 180:
            raise ValueError("invalid scheduler timeout")
        deadline = self.monotonic() + timeout_seconds - 5
        owner = str(uuid.uuid4())
        lease = self.runtime.db.jobs.acquire(_KEY, owner, math.ceil(timeout_seconds) + 30)
        if lease is None:
            return {"status": "busy"}
        state = json.loads(lease.checkpoint) if lease.checkpoint else {}

        def save() -> None:
            if not self.runtime.db.jobs.checkpoint(_KEY, owner, lease.attempt, json.dumps(state, sort_keys=True)):
                raise RuntimeError("scheduler ownership expired")

        try:
            # Import remains read-only until all of its completion checks passed.
            imported = self.runtime.db.storage.execute(
                "SELECT value FROM metadata WHERE name='sqlite_import_state';",
            )[0].rows
            importing = json.loads(imported[0].value) if imported else None
            if isinstance(importing, dict) and importing.get("state") == "running":
                return {"status": "import_in_progress"}
            if self.runtime.db.get_app_meta(_BASELINES) != "1":
                count = self.runtime.db.seed_source_event_report_baselines(batch_size=8)
                return {"status": "initializing", "baselines": count}
            reference = self.now().astimezone(UTC)
            first = int(state.get("next_lane", 0)) % len(_LANES)
            for offset in range(len(_LANES)):
                index = (first + offset) % len(_LANES)
                lane = _LANES[index]
                if deadline - self.monotonic() < 25:
                    break
                # Advance before external work: timeout/failure in one lane must
                # not starve the others on every subsequent timer delivery.
                state["next_lane"] = (index + 1) % len(_LANES)
                save()
                try:
                    if lane == "sync":
                        result = self._sync(state, reference, deadline, save)
                    elif lane in {"daily", "weekly", "monthly", "seasonal"}:
                        result = self._reports(lane, state, reference, deadline, save)
                    else:
                        from zont_analyzer.cloud.user_jobs import scheduled_review

                        result = scheduled_review(self.runtime, deadline=deadline)
                        if result["status"] == "not_due":
                            result = None
                except Exception as exc:
                    # Payloads and provider errors can contain private data.
                    state["last_error"] = {"lane": lane, "type": type(exc).__name__, "at": reference.isoformat()}
                    save()
                    failure = {"status": "error", **state["last_error"]}
                    _observe_lane(lane, failure)
                    return failure
                save()
                if result is not None:
                    _observe_lane(lane, result)
                    return {"lane": lane, **result}
            return {"status": "idle"}
        finally:
            self.runtime.db.jobs.release(_KEY, owner, lease.attempt)

    def _sync(
        self, state: dict[str, Any], reference: datetime, deadline: float, save: Callable[[], None],
    ) -> dict[str, Any] | None:
        slot = state.get("sync")
        if slot is None:
            due = datetime.fromisoformat(state["next_sync"]) if state.get("next_sync") else reference
            if reference < due:
                return None
            # Preserve the reference and replay freshness threshold until this
            # slot completes. Never reset them after a short invocation.
            slot = {"reference": reference.replace(microsecond=0).isoformat(),
                    "checked_after": reference.isoformat()}
            cursors = [self.runtime.db.get_cursor(str(device["id"]), source) or reference - timedelta(days=1)
                       for device in self.runtime.db.list_devices()
                       for source in [*self.runtime.config.zont.history_data_types, "raw_events"]]
            start = min(cursors) if cursors else reference - timedelta(days=1)
            slot["start"] = (start - timedelta(minutes=self.runtime.config.scheduler.overlap_minutes)).isoformat()
            state["sync"] = slot
            save()
        selected = datetime.fromisoformat(slot["reference"])
        with contextlib.closing(self.runner.client_factory()) as client:
            result = self.runtime.ingestion(client).sync(
                now=selected, max_requests=4, deadline=deadline,
                replay_checked_after=datetime.fromisoformat(slot["checked_after"]),
                start_at=datetime.fromisoformat(slot["start"]),
            )
        if result["complete"]:
            state.pop("sync", None)
            state["last_sync"] = selected.isoformat()
            # Missed wakeups coalesce into one new slot; no unbounded catch-up
            # list, while independent source cursors retain all missing work.
            interval = timedelta(minutes=self.runtime.config.scheduler.sync_every_minutes)
            state["next_sync"] = (selected + interval).isoformat()
            self.runtime.db.set_app_meta("cloud-sync-last-success", reference.isoformat())
            self.runtime.db.set_app_meta("cloud-worker-last-success", reference.isoformat())
        return {"status": "done" if result["complete"] else "pending", "sync": result}

    def _reports(
        self, lane: str, state: dict[str, Any], reference: datetime, deadline: float,
        save: Callable[[], None],
    ) -> dict[str, Any] | None:
        if not state.get("last_sync") or state.get("sync"):
            return None
        remaining = deadline - self.monotonic()
        if remaining < MIN_COLLECTION_SECONDS + 5:
            return None
        pending = state.get(lane + "_pending")
        deferred = state.setdefault(lane + "_deferred", [])
        if not pending:
            due_retry = next((item for item in deferred
                              if datetime.fromisoformat(item["retry_at"]) <= reference), None)
            if due_retry is not None:
                deferred.remove(due_retry)
                pending = {key: value for key, value in due_retry.items() if key != "retry_at"}
                state[lane + "_pending"] = pending
                save()
        if pending:
            period = Period.model_validate(pending["period"])
            use_ai = bool(pending["use_ai"])
        else:
            analysis = self.runtime.analysis(no_ai=True)
            timezone = self.runtime.config.home.effective_timezone
            today = reference.astimezone(ZoneInfo(timezone)).date()
            yesterday = today - timedelta(days=1)
            ready = reference >= midnight(today, timezone) + timedelta(
                minutes=self.runtime.config.pilot.daily_report_delay_minutes,
            )
            earliest = self.runtime.db.earliest_sample_time()
            first = earliest.astimezone(ZoneInfo(timezone)).date() if earliest else yesterday
            first = max(min(first, yesterday), yesterday - timedelta(
                days=self.runtime.config.pilot.max_catchup_days - 1,
            ))
            if lane == "daily":
                periods = [calendar_period("daily", first + timedelta(days=offset), timezone)
                           for offset in range((yesterday - first).days + 1)
                           if ready or first + timedelta(days=offset) != yesterday]
                # Latest daily gets first consideration, then history rotates.
                periods.reverse()
            else:
                if not ready:
                    return None
                periods = [item for item in scheduled_periods(analysis, first, today) if item.kind == lane]
            if not periods:
                return None
            # Always inspect the most recent period. The remaining scan budget
            # rotates through history instead of delaying tomorrow's daily
            # report until a possibly years-long archive scan wraps around.
            cursor = max(1, int(state.get(lane + "_cursor", 1)))
            indices = [0]
            if len(periods) > 1:
                indices.extend(1 + ((cursor - 1 + offset) % (len(periods) - 1))
                               for offset in range(min(len(periods) - 1, _SCAN_LIMIT - 1)))
            period = None
            for index in indices:
                selected_period = periods[index]
                if index:
                    state[lane + "_cursor"] = index + 1
                if any(item["period"] == selected_period.model_dump(mode="json") for item in deferred):
                    continue
                if (selected_period.kind == "seasonal"
                        and selected_period.observed_end - selected_period.start > timedelta(days=31)):
                    # Long seasons consume daily facts. Do not freeze a partial
                    # aggregate while the automatic daily catch-up is pending.
                    start_day = max(first, selected_period.start.astimezone(ZoneInfo(timezone)).date())
                    end_day = selected_period.observed_end.astimezone(ZoneInfo(timezone)).date()
                    available = {report.period_start.astimezone(ZoneInfo(timezone)).date()
                                 for report in self.runtime.db.daily_reports(
                                     selected_period.start, selected_period.observed_end,
                                 ) if report.generated_at >= report.period_end}
                    if any(start_day + timedelta(days=offset) not in available
                           for offset in range((end_day - start_day).days)):
                        continue
                if deadline - self.monotonic() < MIN_COLLECTION_SECONDS + 5:
                    return {"status": "pending", "phase": "scan"}
                selected_day = selected_period.start.astimezone(ZoneInfo(timezone)).date()
                needed = (daily_needs_report(analysis, selected_day, yesterday)
                          if lane == "daily" else period_needs_report(analysis, selected_period))
                if needed:
                    period = selected_period
                    break
            if period is None:
                return None
            previous: Report | None = self.runtime.db.report(analysis.report_id_for(period.kind, period.start))
            use_ai = ((previous is None and period.start.astimezone(ZoneInfo(timezone)).date() == yesterday)
                      if lane == "daily" else (previous is None or (
                          period.kind == "seasonal" and previous.period_end < period.observed_end
                      )))
            pending = {"period": period.model_dump(mode="json"), "use_ai": use_ai}
            state[lane + "_pending"] = pending
            save()
        assert period is not None
        result = self.runner.run_scheduled(
            period, use_ai=use_ai, timeout_seconds=max(1, min(180, deadline - self.monotonic())),
        )
        if result["status"] in {"done", "stored_imported"}:
            state.pop(lane + "_pending", None)
        elif result["status"] == "reconciliation_required":
            # A provider response lost after dispatch must retain its request
            # identity, without blocking all later calendar periods forever.
            deferred.append({**pending, "retry_at": (reference + timedelta(hours=1)).isoformat()})
            state.pop(lane + "_pending", None)
        return result


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    if set(payload) - {"_runtime_timeout_seconds"}:
        raise ValueError("scheduler payload must be empty")
    timeout = float(payload.get("_runtime_timeout_seconds", 180))
    if not 1 <= timeout <= 180:
        raise ValueError("invalid scheduler timeout")
    runtime = build_runtime(None, None)
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining < 1:
            raise TimeoutError("scheduler startup exhausted invocation budget")
        return ProductionScheduler(runtime).run(timeout_seconds=remaining)
    finally:
        runtime.db.close()

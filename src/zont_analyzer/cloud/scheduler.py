"""Durable production scheduling, one bounded phase per private timer call.

The timer is only a wake-up signal. Calendar boundaries, polling intervals,
AI policy and durable progress come from the application and YDB, never from
the timer payload. User jobs and publication retain their separate timer.
"""

from __future__ import annotations

import contextlib
import json
import math
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import scheduled_periods
from zont_analyzer.cloud.heavy_work import HeavyWorkLease
from zont_analyzer.cloud.limits import DEFAULT_LONG_JOB_SECONDS, MAX_LONG_JOB_SECONDS
from zont_analyzer.cloud.report_jobs import MIN_COLLECTION_SECONDS, ReportJobRunner
from zont_analyzer.domain import Report
from zont_analyzer.domain.periods import Period, calendar_period, midnight
from zont_analyzer.observability import observe
from zont_analyzer.runtime import Runtime, open_runtime

_KEY = "production-scheduler:v1"
_LANES = ("daily", "weekly", "monthly", "seasonal", "review")
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
    """Closed daily reports are regenerated only by an explicit request."""
    start, _end = analysis.local_day_window(selected)
    return analysis.db.reports.observed_end(analysis.report_id_for("daily", start)) is None


def period_needs_report(analysis: AnalysisService, period: Period) -> bool:
    """Schedule missing reports and the next seasonal observation boundary."""
    observed_end = analysis.db.reports.observed_end(analysis.report_id_for(period.kind, period.start))
    return observed_end is None or (period.kind == "seasonal" and observed_end < period.observed_end)


class ProductionScheduler:
    def __init__(
        self, runtime: Runtime, *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic, runner: ReportJobRunner | None = None,
    ) -> None:
        self.runtime, self.now, self.monotonic = runtime, now, monotonic
        self.runner = runner or ReportJobRunner(runtime, now=now, monotonic=monotonic, publish=False)

    def run(
        self, *, timeout_seconds: float = DEFAULT_LONG_JOB_SECONDS,
    ) -> dict[str, Any]:
        if not 1 <= timeout_seconds <= MAX_LONG_JOB_SECONDS:
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
            due = state.get("next_recommendation_maintenance")
            if due is None or reference >= datetime.fromisoformat(due):
                try:
                    self.runtime.maintain_recommendation_lifecycle(now=reference)
                except Exception as exc:
                    # A failed expiry pass must not prevent collection/reports.
                    # Leave the due time unchanged so the next timer retries.
                    state["recommendation_maintenance_error"] = type(exc).__name__
                else:
                    state.pop("recommendation_maintenance_error", None)
                    state["next_recommendation_maintenance"] = (reference + timedelta(hours=1)).isoformat()
                save()
            # Collection is the freshness gate for every report lane. With a
            # production timer matching the configured polling interval, always
            # advance it first so report work cannot postpone telemetry by another
            # timer period. A completed sync may share the same invocation with one
            # report lane; an incomplete sync resumes on the next timer delivery.
            sync_result: dict[str, Any] | None = None
            try:
                sync_result = self._sync(state, reference, deadline, save)
            except Exception as exc:
                state["last_error"] = {
                    "lane": "sync", "type": type(exc).__name__, "at": reference.isoformat(),
                }
                save()
                failure = {"status": "error", **state["last_error"]}
                _observe_lane("sync", failure)
                return failure
            save()
            if sync_result is not None:
                _observe_lane("sync", sync_result)
                if sync_result.get("status") != "done" or deadline - self.monotonic() < 25:
                    return {"lane": "sync", **sync_result}

            missing_daily = (self._has_recent_missing_daily(reference, state.get("daily_deferred", []))
                             if state.get("last_sync") else False)
            first = 0 if missing_daily else int(state.get("next_lane", 0)) % len(_LANES)
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
                    if lane in {"daily", "weekly", "monthly", "seasonal"}:
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
            if sync_result is not None:
                return {"lane": "sync", **sync_result}
            return {"status": "idle"}
        finally:
            self.runtime.db.jobs.release(_KEY, owner, lease.attempt)

    def _sync(
        self, state: dict[str, Any], reference: datetime, deadline: float, save: Callable[[], None],
    ) -> dict[str, Any] | None:
        slot = state.get("sync")
        interval = timedelta(minutes=self.runtime.config.scheduler.sync_every_minutes)

        def boundary(moment: datetime) -> datetime:
            # Anchor all polling slots to UTC, including legacy rolling due
            # times saved a few seconds after a timer's actual boundary.
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            return epoch + ((moment.astimezone(UTC) - epoch) // interval) * interval

        if slot is None:
            due = datetime.fromisoformat(state["next_sync"]) if state.get("next_sync") else reference
            if reference < boundary(due):
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
                now=selected, max_requests=self.runtime.config.scheduler.max_requests_per_sync, deadline=deadline,
                replay_checked_after=datetime.fromisoformat(slot["checked_after"]),
                start_at=datetime.fromisoformat(slot["start"]),
            )
        if result["complete"]:
            state.pop("sync", None)
            state["last_sync"] = selected.isoformat()
            # Missed wakeups coalesce into one new slot; no unbounded catch-up
            # list, while independent source cursors retain all missing work.
            state["next_sync"] = (boundary(selected) + interval).isoformat()
            self.runtime.db.set_app_meta("cloud-sync-last-success", reference.isoformat())
            self.runtime.db.set_app_meta("cloud-worker-last-success", reference.isoformat())
        return {"status": "done" if result["complete"] else "pending", "sync": result}

    def _has_recent_missing_daily(self, reference: datetime, deferred: list[dict[str, Any]]) -> bool:
        timezone = self.runtime.config.home.effective_timezone
        today = reference.astimezone(ZoneInfo(timezone)).date()
        if reference < midnight(today, timezone) + timedelta(
            minutes=self.runtime.config.pilot.daily_report_delay_minutes,
        ):
            return False
        days = min(_SCAN_LIMIT, self.runtime.config.pilot.max_catchup_days)
        periods = [calendar_period("daily", today - timedelta(days=offset + 1), timezone)
                   for offset in range(days)]
        available = {start for _identifier, start in self.runtime.db.daily_report_catalogue(
            periods[-1].start, periods[0].end,
        )}
        return any(period.start not in available and not any(
            item["period"] == period.model_dump(mode="json") for item in deferred
        ) for period in periods)

    def _missing_recent_daily(self, reference: datetime, deferred: list[dict[str, Any]]) -> Period | None:
        timezone = self.runtime.config.home.effective_timezone
        today = reference.astimezone(ZoneInfo(timezone)).date()
        if reference < midnight(today, timezone) + timedelta(
            minutes=self.runtime.config.pilot.daily_report_delay_minutes,
        ):
            return None
        yesterday = today - timedelta(days=1)
        analysis = self.runtime.analysis(no_ai=True)
        earliest = self.runtime.db.earliest_sample_time()
        first = earliest.astimezone(ZoneInfo(timezone)).date() if earliest else yesterday
        first = max(min(first, yesterday), yesterday - timedelta(
            days=self.runtime.config.pilot.max_catchup_days - 1,
        ))
        for offset in range(min(_SCAN_LIMIT, (yesterday - first).days + 1)):
            period = calendar_period("daily", yesterday - timedelta(days=offset), timezone)
            if any(item["period"] == period.model_dump(mode="json") for item in deferred):
                continue
            if self.runtime.db.reports.observed_end(analysis.report_id_for("daily", period.start)) is None:
                return period
        return None

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
        if lane == "daily" and (pending or deferred):
            urgent_daily = self._missing_recent_daily(reference, deferred)
            if urgent_daily is not None and (
                pending is None or Period.model_validate(pending["period"]).start < urgent_daily.start
            ):
                if pending is not None:
                    deferred.append({**pending, "retry_at": reference.isoformat()})
                yesterday = reference.astimezone(ZoneInfo(urgent_daily.timezone)).date() - timedelta(days=1)
                pending = {"period": urgent_daily.model_dump(mode="json"),
                           "use_ai": urgent_daily.start.astimezone(ZoneInfo(urgent_daily.timezone)).date() == yesterday}
                state[lane + "_pending"] = pending
                save()
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
                if deadline - self.monotonic() < MIN_COLLECTION_SECONDS + 5:
                    return {"status": "pending", "phase": "scan"}
                selected_day = selected_period.start.astimezone(ZoneInfo(timezone)).date()
                needed = (daily_needs_report(analysis, selected_day, yesterday)
                          if lane == "daily" else period_needs_report(analysis, selected_period))
                if not needed:
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
            period, use_ai=use_ai,
            timeout_seconds=max(1, min(MAX_LONG_JOB_SECONDS, deadline - self.monotonic())),
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
    timeout = float(payload.get("_runtime_timeout_seconds", DEFAULT_LONG_JOB_SECONDS))
    if not 1 <= timeout <= MAX_LONG_JOB_SECONDS:
        raise ValueError("invalid scheduler timeout")
    deadline = started + timeout
    runtime = open_runtime()
    heavy = None
    try:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            raise TimeoutError("scheduler startup exhausted invocation budget")
        heavy = HeavyWorkLease.acquire(runtime, deadline=deadline)
        if heavy is None:
            return {"status": "busy"}
        remaining = deadline - time.monotonic()
        if remaining < 1:
            return {"status": "busy"}
        return ProductionScheduler(runtime).run(timeout_seconds=remaining)
    finally:
        try:
            if heavy is not None:
                heavy.release()
        finally:
            runtime.db.close()

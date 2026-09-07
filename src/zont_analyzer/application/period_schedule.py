"""Bounded catch-up of independently analysed calendar periods."""

from __future__ import annotations

import fcntl
import hashlib
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain.periods import SEASONS, Period, active_season, calendar_period, midnight

if TYPE_CHECKING:
    from zont_analyzer.application.analysis import AnalysisService
    from zont_analyzer.domain import Report
    from zont_analyzer.runtime import Runtime


def scheduled_periods(analysis: AnalysisService, first: date, today: date) -> list[Period]:
    timezone = analysis.config.home.timezone
    cutoff = midnight(today, timezone)
    weekly_cutoff = midnight(today - timedelta(days=today.weekday()), timezone)
    periods: dict[tuple[str, str], Period] = {}
    for kind in ("weekly", "monthly"):
        selected = first
        while selected < today:
            period = calendar_period(kind, selected, timezone)
            if period.end <= cutoff:
                periods[(kind, period.start.isoformat())] = period
            selected = period.end.astimezone(ZoneInfo(timezone)).date()
    boundaries, _source = analysis.season_boundaries()
    active_year, active_name = active_season(today - timedelta(days=1), boundaries)
    for year in range(first.year, today.year + 2):
        for name in SEASONS:
            try:
                period = analysis.seasonal_period(year, name, as_of=cutoff)
                if not period.complete:
                    # Refresh the running season with the completed week's data.
                    # A season ending midweek still receives its final report immediately.
                    period = analysis.seasonal_period(year, name, as_of=weekly_cutoff)
            except ValueError:
                continue  # Future season has no completed observations yet.
            if period.end > midnight(first, timezone):
                periods[("seasonal", period.start.isoformat())] = period
    # Give the latest week/month/current season precedence over historical catch-up.
    ordered = sorted(periods.values(), key=lambda item: item.start, reverse=True)
    priorities = []
    for kind in ("weekly", "monthly", "seasonal"):
        selected_period = next(
            (
                item
                for item in ordered
                if item.kind == kind
                and (kind != "seasonal" or (item.year == active_year and item.season == active_name))
            ),
            None,
        )
        if selected_period:
            priorities.append(selected_period)
    return priorities + [item for item in ordered if item not in priorities]


def schedule_signature(analysis: AnalysisService, period: Period) -> str:
    payload = {
        "version": "stage6-v1",
        "period": period.model_dump(mode="json"),
        "config": analysis.config.model_dump(mode="json", include={"home", "preferences", "analysis", "dhw", "openai"}),
        "boundaries": analysis.season_boundaries()[0].model_dump(),
        "data_revision": analysis.db.period_data_revision(period.start, period.observed_end),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _already_current(previous: Report | None, period: Period, signature: str) -> bool:
    if previous is None:
        return False
    if previous.context.get("schedule_signature") == signature:
        return True
    # Do not buy another seasonal analysis within the same weekly checkpoint,
    # including after manual regeneration or corrections to older telemetry.
    # The former daily policy may also have already included newer days.
    return period.kind == "seasonal" and not period.complete and previous.period_end >= period.observed_end


def run_period_schedule(
    runtime: Runtime, analysis: AnalysisService, today: date, *, limit: int = 1
) -> list[dict[str, Any]]:
    from zont_analyzer.application.regeneration import _lock_path

    first_sample = runtime.db.earliest_sample_time()
    timezone = ZoneInfo(runtime.config.home.timezone)
    first = first_sample.astimezone(timezone).date() if first_sample else today - timedelta(days=1)
    first = max(first, today - timedelta(days=runtime.config.pilot.max_catchup_days))
    results: list[dict[str, Any]] = []
    for period in scheduled_periods(analysis, first, today):
        identifier = analysis.report_id_for(period.kind, period.start)
        signature = schedule_signature(analysis, period)
        previous = runtime.db.report(identifier)
        if _already_current(previous, period, signature):
            continue
        path = _lock_path(runtime, identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            previous = runtime.db.report(identifier)
            if _already_current(previous, period, signature):
                continue
            report = analysis.analyze_period(period)
            report.context["schedule_signature"] = signature
            from zont_analyzer.reports import render_text

            runtime.db.save_report(report, render_text(report))
            results.append(
                {
                    "id": report.id,
                    "kind": period.kind,
                    "complete": period.complete,
                    "observed_end": period.observed_end.isoformat(),
                    "ai_used": report.ai_used,
                }
            )
        if len(results) >= limit:
            break
    return results

"""One validated compact history shared by period facts and setting comparisons."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.owner_experiments import OwnerExperimentsRepository
from zont_analyzer.domain.periods import midnight

from .regulation_comparison import build_regulation_comparison_context


def _manual_for_scope(records: list[dict[str, Any]], device: str, circuit: str) -> list[dict[str, Any]]:
    """Discard only actions explicitly scoped exclusively to other consumers."""
    selected = []
    for record in records:
        experiment = record.get("experiment") or {}
        changes = experiment.get("changes") or [experiment]
        if any(not change.get("device_id") or (
            str(change["device_id"]) == device
            and (not change.get("circuit_id") or str(change["circuit_id"]) == circuit)
        ) for change in changes):
            selected.append(record)
    return selected


def build_regulation_report_context(
    db: Database, start: datetime, end: datetime, *, timezone: str, calculate_daily: bool = False,
) -> dict[str, Any]:
    from .regulation_reports import _days, build_regulation_context, validated_regulation_days

    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("regulation report requires a positive timezone-aware period")
    windows = _days(start, end, timezone)
    history_start = min(start.astimezone(UTC), end.astimezone(UTC) - timedelta(days=28)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if ((end.astimezone(UTC) - timedelta(microseconds=1)).date() - history_start.date()).days + 1 > 400:
        raise ValueError("regulation report is limited to 400 UTC days")
    first_day = midnight(history_start.astimezone(ZoneInfo(timezone)).date(), timezone)
    _days(first_day, end, timezone)  # Validate the complete local-day range before any raw calculation.
    built = 0
    if calculate_daily and len(windows) == 1:
        # Only the requested daily calculation may read raw observations. The
        # comparison lookback and publication refresh always use validated days.
        daily = validated_regulation_days(db, start, end, timezone=timezone, calculate_daily=True)
        built = daily["built_days"]
    selected = validated_regulation_days(db, first_day, end, timezone=timezone, calculate_daily=False)
    period_circuits = []
    by_scope = {item["scope"]: item["facts"] for item in selected["circuits"]}
    for item in selected["circuits"]:
        facts = [fact for fact in item["facts"]
                 if start <= datetime.fromisoformat(fact["period_start"])
                 and datetime.fromisoformat(fact["period_end"]) <= end]
        present = {fact["period_start"] for fact in facts}
        missing = {day["start"]: day for day in item["missing_days"]}
        period_missing = []
        for left, right in windows:
            if left.isoformat() in present:
                continue
            source = missing.get(left.isoformat(), {})
            period_missing.append({
                **source, "start": left.isoformat(), "end": right.isoformat(),
                "reason": "partial_local_day" if left < start or right > end else source.get(
                    "reason", "missing_or_stale_facts"),
            })
        period_circuits.append({**item, "facts": facts, "expected_days": len(windows),
                                "missing_days": period_missing})
    period_selection = {**selected, "circuits": period_circuits, "built_days": built,
                        "hit_days": sum(len(item["facts"]) for item in period_circuits)}
    context = build_regulation_context(db, start, end, timezone=timezone, calculate_daily=False,
                                       selected=period_selection)
    undated = OwnerExperimentsRepository(db.storage).uncertain_history(limit=101) if selected["circuits"] else []
    dated = db.intervention_history(limit=101, before=end) if selected["circuits"] else []
    manual = [*undated, *dated]
    manual_feed_incomplete = len(undated) > 100 or len(dated) > 100

    def read_facts(scope: str, left: datetime, right: datetime) -> list[dict[str, Any]]:
        return [fact for fact in by_scope.get(scope, [])
                if datetime.fromisoformat(fact["period_start"]) < right
                and datetime.fromisoformat(fact["period_end"]) > left]

    comparisons = [build_regulation_comparison_context(
        db, device_id=item["device_id"], circuit_id=item["circuit_id"], start=start, end=end,
        read_facts=read_facts,
        manual_history=_manual_for_scope(manual, str(item["device_id"]), str(item["circuit_id"])),
    )["regulation_comparisons"] for item in selected["circuits"]]
    for block in comparisons:
        if manual_feed_incomplete:
            block["manual_history_incomplete"] = True
            for result in block["results"]:
                result["next_observation"].append("manual_history_incomplete; source_read_limit_reached")
                if result["stage"] == "available":
                    result.update(stage="insufficient", outcome=None)
                    for assessment in result.get("forecast_assessments", []):
                        assessment.update(assessment="awaiting_complete_manual_history", observed_outcome=None)
    results = [{**result, "scope": block["scope"]} for block in comparisons for result in block["results"]]
    results.sort(key=lambda result: (result["event"]["boundary"]["no_later_than"], result["scope"]), reverse=True)
    context["regulation_comparisons"] = {
        "algorithm_version": comparisons[0]["algorithm_version"] if comparisons else "regulation-comparison-v2",
        "results": results, "scopes": [block["scope"] for block in comparisons],
        "history_start": history_start.isoformat(), "validated_history_start": first_day.isoformat(),
        "history_end": end.isoformat(),
        "report_start": start.isoformat(), "report_end": end.isoformat(),
        "total_changes_in_history": sum(block["total_changes_in_history"] for block in comparisons),
        "events_truncated": any(block["events_truncated"] for block in comparisons),
        "history_incomplete": not comparisons or any(block["history_incomplete"] for block in comparisons)
        or any(item["missing_days"] for item in selected["circuits"]),
        "manual_history_incomplete": any(block["manual_history_incomplete"] for block in comparisons),
        "unmatched_manual_count": sum(block["unmatched_manual_count"] for block in comparisons),
        "fact_days_read": sum(block["fact_days_read"] for block in comparisons),
        "unknowns": selected["unknowns"],
        "history_missing_days": [{"scope": item["scope"], **day}
                                 for item in selected["circuits"] for day in item["missing_days"]],
        "manual_source_counts": {"undated": len(undated), "dated": len(dated)},
        "limits": "Full period; up to 400 days and 256 observed changes per circuit; four cache slots per circuit",
        "ai_requests": 0,
    }
    return context

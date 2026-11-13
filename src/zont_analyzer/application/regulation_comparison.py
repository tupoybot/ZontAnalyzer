"""Bounded cached regulation comparisons, independently of paid AI/report history."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from zont_analyzer.analytics.regulation_comparison import (
    ALGORITHM_VERSION,
    ComparisonRules,
    compare_change,
    fingerprint,
    observed_changes,
    when,
)


class ComparisonDatabase(Protocol):
    def get_app_meta(self, key: str) -> str | None: ...
    def set_app_meta(self, key: str, value: str) -> None: ...
    def control_settings_history(self, device_id: str, circuit_id: str,
                                 start: datetime, end: datetime) -> dict[str, Any]: ...
    def intervention_history(self, limit: int = 10, *, before: datetime | None = None) -> list[dict[str, Any]]: ...


def build_regulation_comparison_context(
    db: ComparisonDatabase, *, device_id: str, circuit_id: str, start: datetime, end: datetime,
    read_facts: Callable[[str, datetime, datetime], list[dict[str, Any]]],
    timeline: dict[str, Any] | None = None, manual_history: list[dict[str, Any]] | None = None,
    rules: ComparisonRules | None = None, maximum_events: int = 256, lookback_days: int = 28,
    diagnostics: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One complete fact-range read per circuit; four reusable persistent slots.

    ``read_facts`` is RegulationFactStore.read, not aggregate/display cycles.
    The caller refreshes stale daily facts using their cheap source markers
    before calling this function. No raw telemetry or analysis/AI calls occur.
    A result slot is overwritten only when its full dependency identity changes.
    """
    if not 1 <= maximum_events <= 256 or not 1 <= lookback_days <= 400:
        raise ValueError("comparison context is limited to 256 observed changes and 400 UTC days")
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError("comparison context requires a positive aware period")
    rules = rules or ComparisonRules()
    scope = f"{device_id}/{circuit_id}"
    history_start = min(start.astimezone(UTC), end.astimezone(UTC) - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if ((end.astimezone(UTC) - timedelta(microseconds=1)).date() - history_start.date()).days + 1 > 400:
        raise ValueError("comparison context is limited to 400 UTC days; requested period is too long")
    history = timeline if timeline is not None else db.control_settings_history(
        device_id, circuit_id, history_start, end)
    if str(history.get("device_id")) != device_id or str(history.get("circuit_id")) != circuit_id:
        raise ValueError("comparison settings history belongs to another circuit")
    manual = manual_history if manual_history is not None else db.intervention_history(limit=101, before=end)
    manual_incomplete = len(manual) > 100
    manual = manual[:100]
    joined = observed_changes(history, manual)
    events = [event for event in joined["events"]
              if history_start <= when(event["boundary"]["no_later_than"]) < end]
    chosen = events[-maximum_events:]
    facts = read_facts(scope, history_start, end) if chosen else []
    unique: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if fact.get("scope") != scope:
            raise ValueError("comparison facts belong to another circuit")
        if fact.get("algorithm_version") != "regulation-v1":
            raise ValueError("comparison requires current complete regulation census")
        key = fact["period_start"]
        if key in unique and unique[key] != fact:
            raise ValueError("conflicting daily comparison facts")
        unique[key] = fact
    facts = sorted(unique.values(), key=lambda fact: when(fact["period_start"]))
    results = []
    hits = 0
    for slot, event in enumerate(reversed(chosen)):
        # Only facts in this event's bounded dependency interval participate.
        # Updating an unrelated distant day leaves another event's cache valid.
        upper = when(event["boundary"]["no_later_than"])
        previous = [when(other["boundary"]["no_later_than"]) for other in joined["events"]
                    if when(other["boundary"]["no_later_than"]) < upper]
        following = [when(other["boundary"]["after"] or other["boundary"]["no_later_than"])
                     for other in joined["events"] if when(other["boundary"]["no_later_than"]) > upper]
        left, right = max([history_start, *previous]), min([end, *following])
        dependent = [fact for fact in facts if when(fact["period_start"]) < right
                     and when(fact["period_end"]) > left]
        identity = fingerprint({"algorithm": ALGORITHM_VERSION, "rules": asdict(rules), "event": event,
                                "adjacent_events": [{"event_id": value["event_id"], "boundary": value["boundary"]}
                                                    for value in joined["events"]
                                                    if when(value["boundary"]["no_later_than"]) == left
                                                    or when(value["boundary"]["after"]
                                                            or value["boundary"]["no_later_than"]) == right],
                                "facts": dependent, "manual": joined["unmatched_manual_records"],
                                "history_incomplete": joined["incomplete"],
                                "history_contract": history.get("contract"),
                                "history_start": history_start.isoformat(), "end": right.isoformat(),
                                "manual_incomplete": manual_incomplete})
        cache_key = f"regulation-comparison:{fingerprint(scope)}:{slot}"
        raw = db.get_app_meta(cache_key) if slot < 4 else None
        try:
            cached = json.loads(raw) if raw else None
        except (ValueError, TypeError):
            cached = None
        if (isinstance(cached, dict) and cached.get("fingerprint") == identity
                and isinstance(cached.get("result"), dict)):
            result = cached["result"]
            hits += 1
        else:
            result = compare_change(
                event, events=joined["events"], facts=dependent, start=history_start, end=end,
                unmatched_manual=joined["unmatched_manual_records"],
                incomplete_history=joined["incomplete"] or manual_incomplete, rules=rules,
            )
            result["dependency_fingerprint"] = identity
            if slot < 4:
                db.set_app_meta(cache_key, json.dumps({"fingerprint": identity, "result": result},
                                                     ensure_ascii=False, sort_keys=True, allow_nan=False))
        results.append(result)
    if diagnostics is not None:
        diagnostics.update(cache_hits=hits, fact_range_reads=1 if chosen else 0, fact_days_read=len(facts))
    return {"regulation_comparisons": {
        "algorithm_version": ALGORITHM_VERSION, "scope": scope, "results": results,
        "selection": "all bounded observed changes over complete report period; full run census; shared conditions",
        "history_start": history_start.isoformat(), "history_end": end.isoformat(),
        "report_start": start.isoformat(), "report_end": end.isoformat(),
        "maximum_events": maximum_events, "total_changes_in_history": len(events),
        "events_truncated": len(events) > maximum_events,
        "history_incomplete": joined["incomplete"], "manual_history_incomplete": manual_incomplete,
        "unmatched_manual_count": len(joined["unmatched_manual_records"]),
        "fact_days_read": len(facts),
        "limits": "Full report period with at least requested lookback; at most 400 UTC days and 256 observed changes",
        "ai_requests": 0,
    }}

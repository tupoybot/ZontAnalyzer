"""Refresh deterministic regulation evidence without rebuilding raw history or AI."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from zont_analyzer.domain import Report

REGULATION_KEYS = ("regulation_facts", "regulation_comparisons")


def semantic_regulation_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep evidence and completeness; omit typed cache/provenance bookkeeping.

    Names are removed only from their contract locations, never from arbitrary
    nested facts. A metric or an exclusion called ``cache`` remains evidence.
    """
    result = {key: deepcopy(context[key]) for key in REGULATION_KEYS if key in context}
    for key, block in result.items():
        if not isinstance(block, dict):
            continue
        block.pop("cache", None)
        block.pop("diagnostics", None)
        if key == "regulation_comparisons":
            containers = [block, *(value for value in block.get("circuits", []) if isinstance(value, dict))]
            for container in containers:
                container.pop("cache", None)
                container.pop("diagnostics", None)
                for comparison in container.get("results", []):
                    if not isinstance(comparison, dict):
                        continue
                    comparison.pop("dependency_fingerprint", None)
                    for source in comparison.get("source_facts", []):
                        if isinstance(source, dict):
                            source.pop("fingerprint", None)
    return result


def regulation_context_fingerprint(context: dict[str, Any]) -> str:
    value = semantic_regulation_context(context)
    return "regulation-context-v1:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def _build_context(db: Any, report: Report) -> dict[str, Any]:
    from .regulation_comparison_reports import build_regulation_report_context

    return build_regulation_report_context(
        db, report.period_start, report.period_end, timezone=report.timezone, calculate_daily=False,
    )


def refresh_regulation_context(db: Any, report: Report) -> Report:
    """Replace both evidence blocks, including explicit missing/stale results."""
    from .reasoning_context import report_facts_fingerprint

    current = _build_context(db, report)
    if any(key not in current for key in REGULATION_KEYS):
        raise ValueError("regulation refresh must return facts and comparisons, including missing evidence")
    if regulation_context_fingerprint(current) == regulation_context_fingerprint(report.context):
        return report
    result = report.model_copy(deep=True)
    for key in REGULATION_KEYS:
        result.context[key] = current[key]
    if report.ai_used:
        baseline = report.context.get("ai_facts_fingerprint")
        reused = report.context.get("pilot_ai_reuse") or report.context.get("ai_interpretation_reuse")
        stale = any(report.context.get(key) for key in (
            "gas_interpretation_stale", "control_settings_interpretation_stale", "regulation_interpretation_stale",
        )) or report.context.get("gas", {}).get("ai_stale")
        if not baseline and not reused and not stale:
            baseline = report_facts_fingerprint(report)
        if baseline:
            result.context["ai_facts_fingerprint"] = baseline
        # Unknown historical evidence must remain unknown across repeated refreshes.
        if not baseline or report_facts_fingerprint(result) != baseline:
            result.context["regulation_interpretation_stale"] = True
    return result


def report_depends_on_regulation_scope(report: Report, scope: str) -> bool:
    """Use saved report bindings; unknown legacy scope stays conservative."""
    device_id, separator, circuit_id = scope.partition("/")
    if not separator or not device_id:
        return True
    known: set[str] = set()
    history = report.context.get("control_settings_history", {})
    if history.get("device_id") and history.get("circuit_id"):
        known.add(f"{history['device_id']}/{history['circuit_id']}")
    for key in REGULATION_KEYS:
        block = report.context.get(key, {})
        if not isinstance(block, dict):
            continue
        for item in [block, *block.get("circuits", [])]:
            if isinstance(item, dict) and item.get("scope"):
                known.add(str(item["scope"]))
    if known:
        return any(candidate.partition("/")[0] == device_id and (
            circuit_id == "*" or candidate == scope) for candidate in known)
    profiles = report.context.get("equipment_profiles", [])
    devices = {str(item["device_id"]) for item in profiles if isinstance(item, dict) and item.get("device_id")}
    return device_id in devices if devices else True

"""Bounded historical context; interpretations never become independent facts."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from zont_analyzer.domain import DetectedEvent, Report

REASONING_FIELDS = ("observed_patterns", "hypotheses", "predictions", "unknowns", "recommended_experiment")


def report_facts_fingerprint(report: Report) -> str:
    """Compare report evidence, not bookkeeping or the interpretation itself."""
    payload = report.model_dump(mode="json", include={
        "kind", "period_start", "period_end", "timezone", "quality", "metrics", "events", "context",
    })
    context = payload["context"]
    for key in ("input_revision", "calculation_version", "pilot_ai_reuse", "ai_interpretation_reuse",
                "ai_facts_fingerprint", "gas_interpretation_stale", "timezone_provenance"):
        context.pop(key, None)
    gas = context.get("gas")
    if isinstance(gas, dict):
        for key in ("ai_stale", "updated", "previous_model_version"):
            gas.pop(key, None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reuse_ai_interpretation(previous: Report, current: Report) -> Report:
    """Retain AI with an explicit comparison against its original evidence."""
    context = dict(current.context)
    baseline = previous.context.get("ai_facts_fingerprint")
    prior_reuse = previous.context.get("pilot_ai_reuse") or previous.context.get("ai_interpretation_reuse")
    prior_gas = previous.context.get("gas", {})
    if not baseline and not prior_reuse and not prior_gas.get("ai_stale"):
        baseline = report_facts_fingerprint(previous)
    changed = report_facts_fingerprint(current) != baseline if baseline else None
    if baseline:
        context["ai_facts_fingerprint"] = baseline
    context["pilot_ai_reuse"] = {
        "source_generated_at": original_ai_generated_at(previous),
        "reason": "daily facts recomputed without a duplicate OpenAI call",
        "facts_changed": changed,
    }
    if isinstance(context.get("gas"), dict):
        context["gas"] = {**context["gas"], "ai_stale": changed is not False}
    if changed is not False:
        context["gas_interpretation_stale"] = True
    else:
        context.pop("gas_interpretation_stale", None)
    return current.model_copy(update={
        "context": context, "summary": previous.summary, "recommendations": previous.recommendations,
        "ai_used": True, **reasoning_payload(previous),
    })


def original_ai_generated_at(report: Report) -> str:
    """Keep the original AI timestamp across repeated deterministic refreshes."""
    for key in ("pilot_ai_reuse", "ai_interpretation_reuse"):
        reuse = report.context.get(key)
        if isinstance(reuse, dict) and isinstance(reuse.get("source_generated_at"), str):
            return str(reuse["source_generated_at"])
    return report.generated_at.isoformat()


def reasoning_payload(value: Any) -> dict[str, Any]:
    return {name: getattr(value, name) for name in REASONING_FIELDS}


def reasoning_context(
    events: list[DetectedEvent], prior: list[Report], timezone: str,
    intervention_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def bounded_interventions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep a reproducible fingerprint while bounding verbose raw discovery fields."""
        result: list[dict[str, Any]] = []
        for item in items[:10]:
            value = dict(item)
            experiment = value.get("experiment")
            if not isinstance(experiment, dict):
                result.append(value)
                continue
            copied = dict(experiment)
            snapshot = copied.get("control_snapshot")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("value"), dict):
                snapshot_copy = dict(snapshot)
                raw = snapshot_copy.get("value")
                fields = raw.get("fields") if isinstance(raw, dict) else None
                if isinstance(fields, dict):
                    selected_keys = sorted(fields)[:24]
                    snapshot_copy["value"] = {"fields": {key: fields[key] for key in selected_keys}}
                    snapshot_copy["field_selection"] = {
                        "included": len(selected_keys), "total": len(fields),
                        "reason": "AI context limit; fingerprint identifies the full discovery snapshot",
                    }
                copied["control_snapshot"] = snapshot_copy
            value["experiment"] = copied
            result.append(value)
        return result

    def episodes(items: list[DetectedEvent]) -> list[dict[str, Any]]:
        return [
            {
                "id": event.id, "started_at": event.started_at.isoformat(),
                "ended_at": event.ended_at.isoformat() if event.ended_at else None,
                "timezone": timezone, "facts": {
                    key: value for key, value in event.details.get("facts", {}).items() if key in {
                        "selected_system_mode_name", "dhw_enabled_by_selected_mode", "dhw_temperature_start_c",
                        "dhw_temperature_end_c", "dhw_target_c", "dhw_peak_temperature_c", "duration_minutes",
                        "recovery_minutes", "dhw_status", "episode_observation_continuous", "data_quality_score",
                        "pre_episode_temperature_drop_c_per_hour", "contains_concurrent_or_ambiguous_flags",
                    }
                },
                "inference": {"heating_demand": event.details.get("inference", {}).get("heating_demand", "unknown")},
            }
            for event in items if event.kind == "dhw_reheat_episode"
        ][:2]

    firmware_timeline = [
        {
            "category": experiment["category"],
            "parameter": experiment.get("parameter"),
            "before": experiment.get("before"),
            "after": experiment.get("after"),
            "performed_at": experiment.get("performed_at"),
            "source": "owner_recorded_manual_intervention",
            "epistemic_level": "owner_confirmed",
        }
        for item in (intervention_history or [])
        if isinstance(item.get("experiment"), dict)
        for experiment in [item["experiment"]]
        if experiment.get("category") in {"firmware_update", "firmware_rollback"}
    ][:10]

    return {
        "dhw_profiles": {
            "current": episodes(events),
            "current_episode_total": sum(event.kind == "dhw_reheat_episode" for event in events),
            "selection": "First two episodes per period; selected evidence is not a complete event census",
            "history": [
                {
                    "report_id": report.id,
                    "period_start": report.period_start.isoformat(),
                    "period_end": report.period_end.isoformat(),
                    "timezone": report.timezone,
                    "quality": report.quality.model_dump(mode="json"),
                    "episodes": episodes(report.events),
                    "episode_total": sum(event.kind == "dhw_reheat_episode" for event in report.events),
                    "equipment_profiles": report.context.get("equipment_profiles", []),
                }
                for report in prior[:3]
            ],
            "firmware": {
                "status": "unknown",
                "reason": "No verified telemetry firmware signal",
                "owner_recorded_timeline": firmware_timeline,
                "timeline_limit": "Owner-reported changes are manual context, not controller telemetry",
            },
            "comparison": "Compare only compatible targets, modes, quality and equipment; water draw is not measured",
        },
        "prior_interpretations": [
            {
                "report_id": report.id, "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(), "generated_at": report.generated_at.isoformat(),
                "epistemic_level": "prior_interpretation", "summary": report.summary,
                "observed_patterns": [item.model_dump(mode="json") for item in report.observed_patterns],
                "hypotheses": [item.model_dump(mode="json") for item in report.hypotheses],
                "predictions": [item.model_dump(mode="json") for item in report.predictions],
                "recommended_experiment": (
                    report.recommended_experiment.model_dump(mode="json")
                    if report.recommended_experiment is not None else None
                ),
                "recommendation_titles": [item.title for item in report.recommendations],
            }
            for report in prior[:3] if report.ai_used and not report.context.get("gas_interpretation_stale")
        ],
        "intervention_history": bounded_interventions(intervention_history or []),
        "noise_history": [
            {
                "report_id": report.id, "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(), "coverage_pct": report.quality.coverage_pct,
                "selection": "At most two matching events; absence here is not proof of no incidents",
                "events": [event.model_dump(mode="json") for event in report.events
                           if "pulse" in event.kind or "loss" in event.kind or "restart" in event.kind][:2],
            }
            for report in prior[:3]
        ],
    }

"""Bounded historical context; interpretations never become independent facts."""
from __future__ import annotations

from typing import Any

from zont_analyzer.domain import DetectedEvent, Report

REASONING_FIELDS = ("observed_patterns", "hypotheses", "predictions", "unknowns", "recommended_experiment")


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
            for report in prior[:3] if report.ai_used
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

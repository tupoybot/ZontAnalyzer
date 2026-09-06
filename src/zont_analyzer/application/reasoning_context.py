"""Bounded historical context; interpretations never become independent facts."""
from __future__ import annotations

from typing import Any

from zont_analyzer.domain import DetectedEvent, Report

REASONING_FIELDS = ("observed_patterns", "hypotheses", "predictions", "unknowns", "recommended_experiment")


def reasoning_payload(value: Any) -> dict[str, Any]:
    return {name: getattr(value, name) for name in REASONING_FIELDS}


def reasoning_context(
    events: list[DetectedEvent], prior: list[Report], timezone: str,
) -> dict[str, Any]:
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
            "firmware": {"status": "unknown", "reason": "No verified historical firmware signal"},
            "comparison": "Compare only compatible targets, modes, quality and equipment; water draw is not measured",
        },
        "prior_interpretations": [
            {
                "report_id": report.id, "period_start": report.period_start.isoformat(),
                "period_end": report.period_end.isoformat(), "generated_at": report.generated_at.isoformat(),
                "epistemic_level": "prior_interpretation", "summary": report.summary,
                "observed_patterns": [item.model_dump(mode="json") for item in report.observed_patterns],
                "hypotheses": [item.model_dump(mode="json") for item in report.hypotheses],
                "recommendation_titles": [item.title for item in report.recommendations],
            }
            for report in prior[:3] if report.ai_used
        ],
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

"""Stable provenance for the AI portion of a report.

The deterministic metrics and facts in a report have their own provenance.  This
module records only the request which produced the optional AI interpretation.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from zont_analyzer.domain import Report

CONTEXT_KEY = "ai_provenance"
UNKNOWN_LABEL = "Модель не сохранена"
NO_AI_LABEL = "Без AI-анализа"


def normalize(value: Any) -> dict[str, Any] | None:
    """Accept only complete, persisted successful-response metadata."""
    if not isinstance(value, Mapping):
        return None
    requested_model = value.get("requested_model")
    generated_at = value.get("generated_at")
    if not isinstance(requested_model, str) or not requested_model.strip():
        return None
    if not isinstance(generated_at, str) or not generated_at.strip():
        return None
    parameters = value.get("parameters")
    return {
        "requested_model": requested_model,
        "response_model": value.get("response_model") if isinstance(value.get("response_model"), str) else None,
        "parameters": dict(parameters) if isinstance(parameters, Mapping) else {},
        "generated_at": generated_at,
        "prompt_version": value.get("prompt_version") if isinstance(value.get("prompt_version"), str) else None,
        "schema_version": value.get("schema_version") if isinstance(value.get("schema_version"), str) else None,
        "settings_version": value.get("settings_version") if isinstance(value.get("settings_version"), str) else None,
        "ai_log_id": value.get("ai_log_id") if isinstance(value.get("ai_log_id"), str) else None,
    }


def report_provenance(report: Report) -> dict[str, Any] | None:
    return normalize(report.context.get(CONTEXT_KEY))


def label(report: Report) -> str:
    provenance = report_provenance(report)
    if provenance is not None:
        return "AI-анализ: " + str(provenance["response_model"] or provenance["requested_model"])
    return NO_AI_LABEL if not report.ai_used else UNKNOWN_LABEL


def details(report: Report) -> list[str]:
    provenance = report_provenance(report)
    if provenance is None:
        return []
    parameters = provenance["parameters"]
    result = [f"Сгенерирован: {provenance['generated_at']}"]
    if parameters:
        result.append("Параметры: " + ", ".join(f"{key}={value}" for key, value in sorted(parameters.items())))
    for key, title in (("prompt_version", "Prompt"), ("schema_version", "Схема"),
                       ("settings_version", "Версия настроек"), ("ai_log_id", "AI-журнал")):
        if provenance.get(key):
            result.append(f"{title}: {provenance[key]}")
    return result


def recover_historical_provenance(report: Report, calls: Iterable[Any]) -> Report:
    """Recover only a single, successful call explicitly linked to this report."""
    if report_provenance(report) is not None or not report.ai_used:
        return report
    cutoff = _original_ai_timestamp(report)
    successful = []
    for row in calls:
        timestamp = _row_timestamp(row)
        if (_value(row, "status") == "success" and _value(row, "report_id") == report.id
                and timestamp is not None and timestamp <= cutoff):
            successful.append(row)
    if len(successful) != 1:
        return report
    row = successful[0]
    created_at = _row_timestamp(row)
    if isinstance(created_at, datetime):
        generated_at = created_at.astimezone(UTC).isoformat()
    elif isinstance(created_at, str):
        generated_at = created_at
    else:
        return report
    model = _value(row, "model")
    if not isinstance(model, str) or not model:
        return report
    context = dict(report.context)
    context[CONTEXT_KEY] = {
        "requested_model": model,
        "response_model": None,
        "parameters": {"reasoning_effort": _value(row, "reasoning_effort")},
        "generated_at": generated_at,
        "prompt_version": _value(row, "prompt_version"),
        "schema_version": None,
        "settings_version": None,
        "ai_log_id": _value(row, "id"),
    }
    return report.model_copy(update={"context": context})


def _value(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _original_ai_timestamp(report: Report) -> datetime:
    for key in ("pilot_ai_reuse", "ai_interpretation_reuse"):
        reuse = report.context.get(key)
        value = reuse.get("source_generated_at") if isinstance(reuse, Mapping) else None
        parsed = _parse_timestamp(value)
        if parsed is not None:
            return parsed
    return report.generated_at.astimezone(UTC)


def _row_timestamp(row: Any) -> datetime | None:
    return _parse_timestamp(_value(row, "created_at"))


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else None
        except ValueError:
            return None
    return None

"""Deterministic cleanup of normal-state wording in owner-facing text."""

from __future__ import annotations

import re
from typing import Any

from zont_analyzer.domain import Report
from zont_analyzer.reports.language import normalize_user_text

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^явная неисправность регулирования не подтверждена$", re.I),
     "Работу регулирования можно оценить по приведённым наблюдениям"),
    (re.compile(r"^это объясняет часть отсутствия отопительного запроса без признака неисправности$", re.I),
     "Это объясняет часть периода без отопительного запроса при текущем режиме работы системы"),
    (re.compile(r"^данных достаточного качества; значимых локальных аномалий не обнаружено$", re.I),
     "Доступных данных достаточно для оценки текущего режима работы системы"),
    (re.compile(
        r"^(?:явной неисправности нет|неисправность не обнаружена|"
        r"аномалий не выявлено|критических проблем нет)$",
        re.I,
    ),
     "Оценка текущего режима работы представлена в наблюдениях отчёта"),
)


def normalize_text(value: str) -> str:
    """Rewrite only complete known assertions; retain warnings and uncertainty."""
    value = normalize_user_text(value)
    # A legacy summary joins a positive observation to an unsupported diagnostic
    # negative. Keep the observation verbatim and remove only that redundant clause.
    value = re.sub(
        r"(?<=система оставалась наблюдаемой) и явная неисправность регулирования не подтверждена(?=[.!]|\s*$)",
        "", value, flags=re.I,
    )
    chunks = re.split(r"(?<=[.!?])(?=\s+|$)", value)
    result: list[str] = []
    for chunk in chunks:
        match = re.fullmatch(r"(?P<body>.*?)(?P<punct>[.!?]*)", chunk.strip(), re.S)
        if not match:
            result.append(chunk)
            continue
        body = match.group("body").strip()
        replacement = next((new for pattern, new in _REPLACEMENTS if pattern.fullmatch(body)), None)
        if replacement is None:
            result.append(chunk)
            continue
        prefix = chunk[: len(chunk) - len(chunk.lstrip())]
        trailing = chunk[len(chunk.rstrip()):]
        suffix = match.group("punct")
        result.append(prefix + replacement + suffix + trailing)
    return "".join(result)


def normalize_report_for_display(report: Report) -> Report:
    """Return a display copy while preserving the canonical report object."""
    data = report.model_dump(mode="python")
    fields = {
        "summary", "title", "hypothesis", "suggested_manual_action", "expected_effect",
        "success_criteria", "risks", "stop_conditions", "alternatives", "statement",
        "scenario", "confidence_basis", "rationale", "assumptions", "verification",
        "variable", "current_value", "proposed_change", "observation_period",
    }

    def visit(value: Any, key: str = "") -> Any:
        if isinstance(value, str) and key in fields:
            return normalize_text(value)
        if isinstance(value, list):
            return [visit(item, key) for item in value]
        if isinstance(value, dict):
            return {name: visit(item, name) for name, item in value.items()}
        return value

    return type(report).model_validate(visit(data))

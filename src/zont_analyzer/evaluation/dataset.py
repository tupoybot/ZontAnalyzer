"""Build the single anonymized evaluation packet set from the real packet builder."""
# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.openai.provider import analysis_packet
from zont_analyzer.domain import DetectedEvent, MetricValue

DATASET_VERSION = "evaluation-v1"
Case = tuple[str, str, dict[str, Any], list[MetricValue], list[DetectedEvent], dict[str, Any], list[dict[str, Any]], list[str], list[str]]


def _packet(case: str, kind: str, quality: dict[str, Any], metrics: list[MetricValue], events: list[DetectedEvent], context: dict[str, Any] | None = None, feedback: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    end = {"daily": "2026-01-02", "weekly": "2026-01-08", "monthly": "2026-02-01"}.get(kind, "2026-01-02")
    return analysis_packet(quality=quality, metrics=metrics, events=events, period={"kind": kind, "start": "2026-01-01", "end": end}, context={"evaluation_case": case, **(context or {})}, recommendation_feedback=feedback)


def _metric(identifier: str, name: str, value: float = 1.0, unit: str = "count") -> MetricValue:
    return MetricValue(id=identifier, name=name, value=value, unit=unit, context={"source": "synthetic_fixture"})


def _event(identifier: str, kind: str) -> DetectedEvent:
    from datetime import UTC, datetime

    return DetectedEvent(id=identifier, kind=kind, started_at=datetime(2026, 1, 1, tzinfo=UTC), details={"source": "synthetic_fixture"})


def build_dataset() -> list[dict[str, Any]]:
    """Return eight packets using the production ``analysis_packet`` contract."""
    good = {"score": 0.95, "coverage_pct": 99, "max_gap_seconds": 30, "stuck_pct": 0, "implausible_jumps": 0, "sample_count": 1440}
    poor = {"score": 0.2, "coverage_pct": 18, "max_gap_seconds": 7200, "stuck_pct": 25, "implausible_jumps": 2, "sample_count": 4, "flags": ["insufficient_data"]}
    cases: list[Case] = [
        ("normal", "daily", good, [_metric("comfort-avg", "Средняя температура", 21.4, "°C")], [_event("heating-cycle-1", "heating_cycle")], {}, [], ["comfort-avg", "heating-cycle-1"], ["точно доказано"]),
        ("insufficient", "daily", poor, [], [], {}, [], [], ["точно доказано", "единственная причина"]),
        ("dhw-heating", "daily", good, [_metric("dhw-runtime", "Работа ГВС", 42, "min"), _metric("heat-runtime", "Отопление", 310, "min")], [_event("dhw-cycle-1", "dhw_cycle")], {"dhw": {"source": "derived"}}, [], ["dhw-runtime", "heat-runtime", "dhw-cycle-1"], ["ГВС не работала"]),
        ("competing-causes", "daily", good, [_metric("room-drop", "Падение температуры", 2.1, "°C")], [_event("ventilation-1", "ventilation_possible"), _event("setback-1", "schedule_setback")], {"temporal_evidence": {"windows": [{"id": "window-ventilation-1", "started_at": "2026-01-01T10:00:00Z", "ended_at": "2026-01-01T12:00:00Z", "signals": {"room_temperature": {"value": 19.3, "source": "observed"}}, "facts": {"ventilation": {"source": "derived"}}}, {"id": "window-setback-1", "started_at": "2026-01-01T10:00:00Z", "ended_at": "2026-01-01T12:00:00Z", "signals": {"setback": {"value": True, "source": "observed"}}, "facts": {"schedule": {"source": "derived"}}}], "exclusion_windows": [{"id": "counterevidence-1", "started_at": "2026-01-01T06:00:00Z", "ended_at": "2026-01-01T08:00:00Z", "reason": "compatible weather window absent"}]}}, [], ["room-drop", "ventilation-1", "setback-1", "window-ventilation-1", "window-setback-1", "counterevidence-1"], ["единственная причина"]),
        ("rejected-advice", "daily", good, [_metric("comfort-avg", "Средняя температура", 20.9, "°C")], [_event("feedback-1", "owner_feedback")], {}, [{"recommendation_id": "rec-old", "report_id": "daily-2025-12-31", "status": "rejected", "title": "Изменить уставку", "category": "safe_user_setting", "hypothesis": "Датчик мог ошибаться", "owner_note": "Датчик исправен; гипотезу закрыть на период наблюдения", "updated_at": "2026-01-01T09:00:00+00:00"}], ["comfort-avg", "feedback-1"], ["применить отклонённую рекомендацию"]),
        ("experiment", "weekly", good, [_metric("before-after", "Сравнение до и после", 0.1, "°C")], [_event("experiment-1", "manual_experiment")], {"experiment": {"id": "experiment-1", "status": "planned", "variable": "температура подачи", "current_value": "55 °C", "proposed_change": "57 °C", "observation_period": "7 дней", "confounders": ["погода", "ГВС", "занятость"], "outcome": "unknown", "source": "owner_confirmed"}}, [], ["before-after", "experiment-1"], ["эксперимент доказал причинность"]),
        ("gas", "daily", good, [_metric("gas-consumption", "Расход газа", 4.2, "m³")], [_event("gas-reading-1", "gas_reading")], {"gas": {"value_m3": "120.4", "source": "owner_input"}}, [], ["gas-consumption", "gas-reading-1"], ["снижение газа доказано"]),
        ("long-review", "monthly", good, [_metric("comfort-avg", "Средняя температура", 21.1, "°C"), _metric("gas-consumption", "Расход газа", 110, "m³"), _metric("coverage", "Покрытие", 98, "%")], [_event("heating-cycle-1", "heating_cycle"), _event("dhw-cycle-1", "dhw_cycle")], {"review": {"window_days": 30}}, [], ["comfort-avg", "gas-consumption", "coverage", "heating-cycle-1", "dhw-cycle-1"], ["тренд доказывает причинность"]),
    ]
    result = []
    for case, kind, quality, metrics, events, context, feedback, expected, forbidden in cases:
        result.append({"id": case, "dataset_version": DATASET_VERSION, "packet": _packet(case, kind, quality, metrics, events, context, feedback), "expected_evidence_ids": expected, "forbidden_claims": forbidden, "rubric": {"schema": 1, "evidence": 1, "factual": 1, "advice": 1, "uncertainty": 1}})
    return result


def dataset_sha(dataset: list[dict[str, Any]]) -> str:
    encoded = json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def materialize_dataset(directory: str | Path) -> dict[str, Any]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    dataset = build_dataset()
    for item in dataset:
        (target / f"{item['id']}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"version": DATASET_VERSION, "sha256": dataset_sha(dataset), "cases": len(dataset), "directory": str(target)}

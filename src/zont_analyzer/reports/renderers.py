from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report

METRIC_LABELS = {
    "mean_temperature_c": "Средняя температура помещения",
    "temperature_range_c": "Диапазон температуры помещения",
    "outdoor_mean_temperature_c": "Средняя уличная температура",
    "outdoor_temperature_range_c": "Диапазон уличной температуры",
    "heating_target_evaluation_time_pct": "Доля времени с активным контролем уставки отопления",
    "time_in_target_band_pct": "Время в диапазоне уставки отопления",
    "time_above_target_band_pct": "Время выше диапазона уставки отопления",
    "time_below_target_band_pct": "Время ниже диапазона уставки отопления",
    "mean_absolute_target_error_c": "Среднее абсолютное отклонение от уставки отопления",
    "degree_hours_above_target": "Накопленное превышение температуры комнаты над уставкой",
    "degree_hours_below_target": "Накопленный дефицит температуры комнаты относительно уставки",
    "mean_error_while_above_target_c": "Среднее превышение температуры над уставкой",
    "mean_error_while_below_target_c": "Средний дефицит температуры относительно уставки",
    "burner_starts": "Запуски горелки",
    "burner_starts_per_hour": "Запуски горелки в час",
    "burner_duty_cycle_pct": "Доля работы горелки",
    "short_cycle_share_pct": "Доля коротких циклов",
    "median_burner_cycle_minutes": "Медианная длительность цикла горелки",
    "dhw_burner_starts": "Запуски горелки на ГВС",
    "dhw_burner_starts_per_hour": "Запуски горелки на ГВС в час",
    "dhw_burner_duty_cycle_pct": "Доля работы горелки на ГВС",
    "dhw_short_cycle_share_pct": "Доля коротких циклов ГВС",
    "dhw_median_burner_cycle_minutes": "Медианная длительность цикла ГВС",
}

SPACE_HEATING_METRIC_LABELS = {
    "burner_starts": "Запуски горелки на отопление",
    "burner_starts_per_hour": "Запуски горелки на отопление в час",
    "burner_duty_cycle_pct": "Доля работы горелки на отопление",
    "short_cycle_share_pct": "Доля коротких циклов отопления",
    "median_burner_cycle_minutes": "Медианная длительность цикла отопления",
}

EVENT_LABELS = {
    "temperature_above_heating_setpoint": "Температура комнаты выше диапазона уставки отопления",
    "temperature_below_heating_setpoint": "Температура комнаты ниже диапазона уставки отопления",
    "heating_mode_change": "Изменение режима отопления",
    "target_temperature_change": "Изменение уставки отопления",
    "burner_cycle_after_control_change": "Цикл горелки после изменения управления",
    "short_burner_cycle": "Короткий цикл горелки",
    "burner_cycle": "Цикл горелки",
    "automatic_summer_mode_entered": "Контур автоматически перешёл в летнее состояние",
    "automatic_summer_mode_exited": "Контур автоматически вышел из летнего состояния",
}


def _metric_label(name: str, context: dict[str, Any] | None = None) -> str:
    if context and context.get("activity_scope") == "space_heating_only":
        return SPACE_HEATING_METRIC_LABELS.get(name, METRIC_LABELS.get(name, name.replace("_", " ")))
    return METRIC_LABELS.get(name, name.replace("_", " "))


def _event_label(kind: str) -> str:
    return EVENT_LABELS.get(kind, kind.replace("_", " "))


def _local(value: datetime, timezone: str) -> str:
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


def render_text(report: Report) -> str:
    lines = [
        f"ZontAnalyzer — {report.kind}",
        f"ID отчёта: {report.id}",
        f"Период: {_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}",
        f"AI-интерпретация: {'да' if report.ai_used else 'нет'}",
        f"Качество данных: {report.quality.score:.0%} (покрытие {report.quality.coverage_pct:.1f}%)",
        report.summary,
    ]
    current_mode = report.context.get("current_mode")
    if isinstance(current_mode, dict):
        lines.append(
            f"Текущий режим: {current_mode.get('name', current_mode.get('id'))} "
            f"({current_mode.get('intent', 'unknown')}, политика цели: {current_mode.get('target_policy', 'unknown')})"
        )
    if report.context.get("current_target_c") is not None:
        lines.append(f"Текущая целевая температура: {report.context['current_target_c']:g} °C")
    heating_circuit = report.context.get("heating_circuit")
    if isinstance(heating_circuit, dict):
        auto_enabled = heating_circuit.get("automatic_summer_mode_enabled")
        auto_active = heating_circuit.get("automatic_summer_mode_active")
        threshold = heating_circuit.get("summer_threshold_c")
        lines.append(
            "Автоматический летний переход контура: "
            f"{'включён' if auto_enabled else 'выключен'}"
            + (f", порог {threshold:g} °C" if isinstance(threshold, (int, float)) else "")
            + (f", текущее летнее состояние: {'активно' if auto_active else 'неактивно'}" if auto_enabled else "")
        )
        active_time_pct = 100 - float(heating_circuit.get("inactive_time_pct", 0))
        lines.append(
            f"Контроль отопительной уставки был активен {active_time_pct:g}% периода."
        )
    if report.context.get("mode_change_count") or report.context.get("target_change_count"):
        lines.append(
            f"Изменения контекста: режим — {report.context.get('mode_change_count', 0)}, "
            f"цель — {report.context.get('target_change_count', 0)}"
        )
    if report.quality.flags:
        lines.append("Флаги качества: " + ", ".join(report.quality.flags))
    if report.metrics:
        lines.append("Метрики:")
        lines.extend(
            f"- {_metric_label(metric.name, metric.context)}: {metric.value:g} {metric.unit}"
            for metric in report.metrics
        )
    if report.events:
        lines.append(f"События (показано до 20 из {len(report.events)}):")
        lines.extend(
            f"- [{event.severity}] {_local(event.started_at, report.timezone)} — {_event_label(event.kind)}: "
            f"{json.dumps(event.details, ensure_ascii=False, sort_keys=True)}"
            for event in report.events[:20]
        )
    if report.recommendations:
        lines.append("Рекомендации:")
        for item in report.recommendations:
            lines.extend(
                [
                    f"- [{item.priority}] {item.title} (уверенность {item.confidence:.0%})",
                    f"  Гипотеза: {item.hypothesis}",
                    f"  Действие: {item.suggested_manual_action}",
                    f"  Ожидаемый эффект: {item.expected_effect}",
                    "  Evidence: "
                    + ", ".join((*item.evidence_metric_ids, *item.evidence_event_ids)),
                ]
            )
            if item.risks:
                lines.append("  Риски:")
                lines.extend(f"    - {risk}" for risk in item.risks)
            if item.stop_conditions:
                lines.append("  Когда остановиться:")
                lines.extend(f"    - {condition}" for condition in item.stop_conditions)
    return "\n".join(lines)


def render_html(report: Report) -> str:
    title = html.escape(f"ZontAnalyzer — {report.kind}")
    period = html.escape(
        f"{_local(report.period_start, report.timezone)} — {_local(report.period_end, report.timezone)}"
    )
    current_mode = report.context.get("current_mode")
    mode_name = current_mode.get("name") if isinstance(current_mode, dict) else None
    mode_intent = current_mode.get("intent") if isinstance(current_mode, dict) else None
    mode_context = (
        f"<p><strong>Текущий режим:</strong> {html.escape(str(mode_name))} "
        f"({html.escape(str(mode_intent))}); <strong>цель:</strong> "
        f"{html.escape(str(report.context.get('current_target_c')))} °C</p>"
        if mode_name is not None
        else ""
    )
    heating_circuit = report.context.get("heating_circuit")
    summer_context = ""
    if isinstance(heating_circuit, dict):
        auto_enabled = heating_circuit.get("automatic_summer_mode_enabled")
        auto_active = heating_circuit.get("automatic_summer_mode_active")
        threshold = heating_circuit.get("summer_threshold_c")
        threshold_text = f"; порог {threshold:g} °C" if isinstance(threshold, (int, float)) else ""
        active_text = f"; сейчас {'активно' if auto_active else 'неактивно'}" if auto_enabled else ""
        summer_context = (
            "<p><strong>Автоматический летний переход контура:</strong> "
            f"{'включён' if auto_enabled else 'выключен'}{threshold_text}{active_text}. "
            f"Контроль отопительной уставки активен "
            f"{100 - float(heating_circuit.get('inactive_time_pct', 0)):g}% периода.</p>"
        )
    metrics = "".join(
        f"<tr><td>{html.escape(_metric_label(metric.name, metric.context))}</td><td>{metric.value:g}</td>"
        f"<td>{html.escape(metric.unit)}</td></tr>"
        for metric in report.metrics
    )
    events = "".join(
        f"<tr><td>{html.escape(_local(item.started_at, report.timezone))}</td>"
        f"<td>{html.escape(item.severity)}</td><td>{html.escape(_event_label(item.kind))}</td>"
        f"<td><code>{html.escape(json.dumps(item.details, ensure_ascii=False, sort_keys=True))}</code></td></tr>"
        for item in report.events[:50]
    )

    def html_list(values: list[str], empty: str) -> str:
        return "<ul>" + "".join(f"<li>{html.escape(value)}</li>" for value in values) + "</ul>" if values else empty

    recommendations = "".join(
        (
            f"<article><h3>{html.escape(item.title)}</h3>"
            f"<p><strong>Гипотеза:</strong> {html.escape(item.hypothesis)}</p>"
            f"<p><strong>Действие:</strong> {html.escape(item.suggested_manual_action)}</p>"
            f"<p><strong>Ожидаемый эффект:</strong> {html.escape(item.expected_effect)}</p>"
            f"<p><strong>Evidence:</strong> "
            f"{html.escape(', '.join((*item.evidence_metric_ids, *item.evidence_event_ids)))}</p>"
            f"<p><small>Приоритет: {html.escape(item.priority)}; уверенность: {item.confidence:.0%}</small></p>"
            f"<p><strong>Риски:</strong></p>{html_list(item.risks, '<p>Не указаны.</p>')}"
            f"<p><strong>Когда остановиться:</strong></p>"
            f"{html_list(item.stop_conditions, '<p>Не указано.</p>')}</article>"
        )
        for item in report.recommendations
    )
    canonical = html.escape(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title><style>
body{{font:16px system-ui;max-width:960px;margin:2rem auto;padding:0 1rem;color:#20242a}}
h1{{font-size:1.6rem}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:.55rem;border-bottom:1px solid #ddd;text-align:left}}
.quality{{padding:.8rem;background:#eef6ff;border-radius:.5rem}}
article{{border-left:4px solid #568;padding:0 1rem;margin:1rem 0}}
</style></head><body><h1>{title}</h1>
<p><strong>ID:</strong> <code>{html.escape(report.id)}</code></p>
<p><strong>Период:</strong> {period}</p>
<p><strong>AI-интерпретация:</strong> {'да' if report.ai_used else 'нет'}</p>
{mode_context}
{summer_context}
<p class="quality">Качество данных: {report.quality.score:.0%}; покрытие {report.quality.coverage_pct:.1f}%</p>
<p>{html.escape(report.summary)}</p><h2>Метрики</h2><table><tr><th>Метрика</th><th>Значение</th><th>Единица</th></tr>{metrics}</table>
<h2>События</h2><p>Показано до 50 из {len(report.events)}.</p>
<table><tr><th>Начало</th><th>Уровень</th><th>Тип</th><th>Детали</th></tr>{events}</table>
<h2>Рекомендации</h2>{recommendations or '<p>Нет рекомендаций.</p>'}
<details><summary>Канонический JSON</summary><pre>{canonical}</pre></details></body></html>"""

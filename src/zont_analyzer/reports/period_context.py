"""Compact presentation of measured comparisons, separate from AI opinions."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report
from zont_analyzer.reports.timezone_labels import timezone_label

_LIMITS = {
    "second_intervention_between_windows": "Есть другое вмешательство: изолированный эффект не определён",
    "weather_not_comparable": "Различается погода",
    "dhw_influence_not_comparable": "Различается влияние ГВС",
    "operating_mode_not_comparable": "Различается профиль режима или уставки",
    "before_low_coverage": "Недостаточное покрытие до изменения",
    "after_low_coverage": "Недостаточное покрытие после изменения",
    "before_low_quality": "Низкое качество данных до изменения",
    "after_low_quality": "Низкое качество данных после изменения",
    "weather_context_unavailable": "Недостаточно данных о погоде",
    "dhw_context_unavailable": "Недостаточно данных о ГВС",
    "mode_context_unavailable": "Исторический профиль режима или уставки неизвестен",
    "comparison_not_isolated": "Причину изменения нельзя отделить от других факторов",
}
_UNITS = {"celsius": "°C", "seconds": "с", "minutes": "мин", "vendor_percent": "% шкалы котла",
          "ratio": "доля", "count/hour": "в час", "count": "шт.", "percent": "%"}


def _number(value: Any) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, (float, int)) else "н/д"


def _label(name: str) -> str:
    from zont_analyzer.reports.renderers import EVIDENCE_LABELS, _metric_label

    return EVIDENCE_LABELS.get(name, _metric_label(name))


def _local(value: Any, timezone: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(value)


def _limits(comparison: dict[str, Any]) -> str:
    limitations = [*comparison.get("confounders", []), *comparison.get("unknowns", []),
                   *comparison.get("quality", {}).get("flags", [])]
    return "; ".join(_LIMITS.get(str(value), str(value)) for value in limitations)


def _pairs(item: dict[str, Any]) -> list[dict[str, Any]]:
    return item.get("matched_windows") or [item.get("comparison", item)]


def _bounds(pair: dict[str, Any], timezone: str) -> str:
    return (f"До: {_local(pair.get('before_start'), timezone)} — {_local(pair.get('before_end'), timezone)}; "
            f"после: {_local(pair.get('after_start'), timezone)} — {_local(pair.get('after_end'), timezone)}")


def period_text(report: Report) -> list[str]:
    period = report.context.get("period", {})
    result: list[str] = []
    if period:
        result.append(f"Границы периода: {_local(period['start'], report.timezone)} — "
                      f"{_local(period['end'], report.timezone)}; часовой пояс {timezone_label(report.timezone)}.")
        if not period.get("complete", True):
            result.append("Промежуточный результат: обработаны данные до "
                          f"{_local(period['observed_end'], report.timezone)}.")
    for item in [*report.context.get("intervention_outcomes", []), *report.context.get("period_comparisons", [])]:
        result.append(str(item.get("label", "Сравнение")))
        if item.get("unavailable_reason"):
            result.append(str(item["unavailable_reason"]))
            continue
        for pair in _pairs(item):
            result.extend([_bounds(pair, report.timezone), _limits(pair)])
            for metric in pair.get("metrics", []):
                result.append(f"{_label(metric['name'])}: {_number(metric.get('before'))} → "
                              f"{_number(metric.get('after'))}; Δ {_number(metric.get('absolute_change'))} "
                              f"{_UNITS.get(metric['unit'], metric['unit'])}; "
                              f"изменение {_number(metric.get('relative_change_pct'))}%")
    return result


def render_period_context(report: Report) -> str:
    period = report.context.get("period", {})
    if not period:
        return ""
    body = f"<p>{escape(period_text(report)[0])}</p>"
    if not period.get("complete", True):
        body += f"<p><strong>{escape(period_text(report)[1])}</strong></p>"
    house = report.context.get("house_context", {})
    if house.get("days"):
        body += f"<details><summary>Обычное поведение дома: {int(house['days'])} наблюдаемых дней</summary>"
        body += "<p>Расчёт по доступным дневным свидетельствам. Медианы описывают наблюдавшиеся дни.</p><ul>"
        typical = house.get("typical_daily_metrics", {})
        for name in ("burner_starts_per_active_request_hour", "burner_cycle_median_seconds", "flame_modulation_mean",
                     "delta_t_c", "room_error_c", "dhw_mean_recovery_minutes"):
            if name in typical:
                value = typical[name]
                body += (f"<li>{escape(_label(name))}: {_number(value['median'])}; "
                         f"диапазон {_number(value['minimum'])}…{_number(value['maximum'])}</li>")
        body += "</ul><p>Тепловая инерция: точная постоянная времени пока не определена; "
        body += "темп остывания зависит также от погоды и теплопритоков.</p></details>"
    for item in [*report.context.get("intervention_outcomes", []), *report.context.get("period_comparisons", [])]:
        body += f"<details><summary>{escape(str(item.get('label', 'Сравнение')))}</summary>"
        if item.get("unavailable_reason"):
            body += f"<p>{escape(str(item['unavailable_reason']))}</p></details>"
            continue
        body += f"<p>{escape(str(item.get('selection', '')))}</p>"
        for pair in _pairs(item):
            status = "сопоставимые окна" if pair.get("status") == "comparable" else "сравнение с ограничениями"
            body += f"<p>{escape(_bounds(pair, report.timezone))}</p><p>{status}. {escape(_limits(pair))}</p>"
            body += ('<div class="table-scroll"><table><thead><tr><th>Показатель</th><th>До</th><th>После</th>'
                     '<th>Δ</th><th>Δ, %</th></tr></thead><tbody>')
            for metric in pair.get("metrics", []):
                body += "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in (
                    _label(metric["name"]) + ", " + _UNITS.get(metric["unit"], metric["unit"]),
                    _number(metric.get("before")), _number(metric.get("after")),
                    _number(metric.get("absolute_change")), _number(metric.get("relative_change_pct")),
                )) + "</tr>"
            body += "</tbody></table></div>"
        body += "</details>"
    return '<section class="full-width period-context"><h2>Период и сравнения</h2>' + body + "</section>"

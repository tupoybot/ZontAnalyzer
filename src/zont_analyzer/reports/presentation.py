"""Presentation of existing report facts; no analysis or persistence."""

from __future__ import annotations

import html
import json
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report

from .wording import normalize_report_for_display


def esc(value: Any) -> str:
    return html.escape(str(value))


def number(value: Any, unit: str = "") -> str:
    if not isinstance(value, (int, float)):
        return "Нет данных"
    return f"{value:,.1f}".replace(",", " ").replace(".", ",") + (f" {unit}" if unit else "")


def debug(value: Any, label: str = "Технические данные") -> str:
    return (
        '<details class="debug-only"><summary>'
        + esc(label)
        + "</summary><pre>"
        + esc(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        + "</pre></details>"
    )


def hero(report: Report) -> str:
    report = normalize_report_for_display(report)
    insufficient = report.quality.score < 0.6 or not report.quality.sample_count
    alerts = [e for e in report.events if e.severity in {"warning", "critical"}]
    urgent = [r for r in report.recommendations if r.priority in {"high", "critical"}]
    if urgent:
        title, state = urgent[0].title, "warning"
    elif insufficient:
        title, state = "Недостаточно данных для оценки", "warning"
    elif any(e.severity == "critical" for e in alerts):
        title, state = "Есть события, требующие внимания", "warning"
    elif not report.metrics:
        title, state = "Недостаточно данных для оценки", "warning"
    else:
        title, state = "Работа системы за период", "info"
        # Only use an affirmative healthy headline when the report itself says so.
        normal = (
            "штатно",
            "в норме",
            "не требуют вмешательства",
            "неисправности нет",
            "неисправность не обнаружена",
            "критических проблем нет",
            "аномалий не выявлено",
        )
        if any(phrase in report.summary.lower() for phrase in normal):
            title, state = "Система работает штатно", "success"
    summary = report.summary
    return (
        f'<section class="hero {state}"><span class="eyebrow">СОСТОЯНИЕ СИСТЕМЫ</span>'
        f'<h1>{esc(title)}</h1><p class="lead">{esc(summary)}</p></section>'
    )


def kpis(report: Report) -> str:
    metrics = {m.name: m for m in report.metrics}

    def metric(name: str, unit: str) -> str:
        if name not in metrics:
            return "Нет данных"
        value = metrics[name].value
        return str(int(value)) if not unit and value.is_integer() else number(value, unit)

    values = [
        ("Комната · средняя", metric("mean_temperature_c", "°C")),
        ("Цель · на конец периода", number(report.context.get("current_target_c"), "°C")),
        ("Улица · средняя", metric("outdoor_mean_temperature_c", "°C")),
        ("Качество данных", number(report.quality.score * 100, "%")),
        ("Отопление · запуски", metric("burner_starts", "")),
        ("ГВС · догревы", metric("dhw_episode_count", "")),
    ]
    return (
        '<section class="kpi-grid" aria-label="Ключевые показатели">'
        + "".join(
            f'<div class="kpi"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>' for label, value in values
        )
        + reliability(report)
        + "</section>"
    )


def reliability(report: Report) -> str:
    cards = []
    by_name = {metric.name: metric for metric in report.metrics}
    for key in ("zont_uptime_seconds", "boiler_uptime_seconds"):
        metric = by_name.get(key)
        if metric is None:
            label = "ZONT" if key.startswith("zont") else "Котёл"
            cards.append(f'<div class="kpi"><span>Аптайм {label}</span><strong>Нет данных</strong></div>')
            continue
        name = "ZONT" if metric.name.startswith("zont") else "Котёл"
        online = metric.context.get("online")
        status = "● На связи" if online is True else "○ Не на связи" if online is False else "Статус неизвестен"
        seconds = metric.value
        duration = (
            f"{int(seconds // 86400)} дн."
            if seconds >= 86400
            else f"{int(seconds // 3600)} ч"
            if seconds >= 3600
            else f"{int(seconds // 60)} мин"
        )
        cards.append(
            f'<div class="kpi"><span>Аптайм {name}</span><strong>{duration}</strong><small>{status}</small>'
            + debug(metric.model_dump(), "Основание аптайма")
            + "</div>"
        )
    return '<div class="kpi-uptime-row" aria-label="Надёжность">' + "".join(cards) + "</div>"


def metric_groups(report: Report, missing_mttr: str | None) -> str:
    from .renderers import _metric_display, _metric_label

    groups: dict[str, list[str]] = {k: [] for k in ("Комфорт", "Отопление", "ГВС", "Надёжность", "Качество данных")}
    for m in report.metrics:
        group = (
            "ГВС"
            if m.name.startswith("dhw_")
            else "Надёжность"
            if any(t in m.name for t in ("uptime", "mtbf", "mttr", "mtbr"))
            else "Качество данных"
            if any(t in m.name for t in ("quality", "coverage", "noise", "unconfirmed"))
            else "Комфорт"
            if any(t in m.name for t in ("temperature", "target", "degree_hours"))
            else "Отопление"
        )
        value, unit = _metric_display(m)
        if unit != "дд:чч:мм":
            value, unit = number(m.value), {"celsius": "°C", "minutes": "мин", "percent": "%"}.get(unit, unit)
        groups[group].append(
            f'<tr><th scope="row">{esc(_metric_label(m.name, m.context))}'
            + debug(m.model_dump(), "Метрика / evidence")
            + f"</th><td>{esc(value)} {esc(unit)}</td></tr>"
        )
    if missing_mttr:
        groups["Надёжность"].append(
            f'<tr><th scope="row">MTTR котельного сервиса</th><td>Нет достоверных данных. {esc(missing_mttr)}</td></tr>'
        )
    groups["Качество данных"].append(
        f'<tr><th scope="row">Покрытие периода</th><td>{number(report.quality.coverage_pct, "%")}</td></tr>'
    )
    return (
        '<section id="metrics"><h2>Подробные метрики</h2>'
        + "".join(
            f'<details class="metric-group"><summary>{name}</summary>'
            f'<table><tbody>{"".join(rows)}</tbody></table></details>'
            for name, rows in groups.items()
            if rows
        )
        + "</section>"
    )


def timeline(report: Report) -> str:
    from .renderers import _event_label

    significant: list[str] = []
    routine: list[str] = []
    for e in sorted(report.events, key=lambda e: e.started_at):
        time = e.started_at.astimezone(ZoneInfo(report.timezone)).strftime(
            "%H:%M" if report.kind == "daily" else "%d.%m %H:%M"
        )
        duration = f" · {number((e.ended_at - e.started_at).total_seconds() / 60, 'мин')}" if e.ended_at else ""
        row = (
            f"<li><time>{time}</time><div><strong>{esc(_event_label(e.kind))}</strong>"
            f'<span>{esc(duration)}</span><span class="event-severity">'
            f"{ {'info': '', 'warning': 'Требует внимания', 'critical': 'Критическое событие'}[e.severity] }</span>"
            + debug(e.model_dump(), "Событие / evidence")
            + "</div></li>"
        )
        (
            routine if e.kind in {"burner_cycle", "unconfirmed_burner_pulse"} and e.severity == "info" else significant
        ).append(row)
    visible = "".join(significant[:8])
    more = significant[8:]
    return (
        '<section id="events"><h2>Значимые события</h2><ol class="timeline">'
        + (visible or "<li>Значимые события за период не зарегистрированы.</li>")
        + "</ol>"
        + (
            f'<details><summary>Ещё {len(more)} значимых событий</summary>'
            f'<ol class="timeline">{"".join(more)}</ol></details>'
            if more
            else ""
        )
        + (
            f'<details class="technical-events"><summary>Показать ещё {len(routine)} технических событий</summary>'
            f'<ol class="timeline">{"".join(routine)}</ol></details>'
            if routine
            else ""
        )
        + "</section>"
    )


def sensors(report: Report) -> str:
    context = report.context.get("sensors", {})
    rows = []
    for key, value in context.items() if isinstance(context, dict) else []:
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, dict) or not item.get("display_name"):
                continue
            role = (
                "контроль отопления"
                if key == "control_temperature"
                else {
                    "humidity": "влажность",
                    "return_temperatures": "обратка",
                    "room_temperatures": "комната",
                    "technical_temperatures": "техническое помещение",
                }.get(key, "")
            )
            rows.append(
                f"<li><strong>{esc(item['display_name'])}</strong> <span>{role}</span>"
                + debug(item, "Источник датчика")
                + "</li>"
            )
    return (
        '<details class="sensor-list"><summary>Датчики и источники</summary><p>Доступные датчики; '
        'текущие измерения не входят в этот список источников.</p><ul>'
        + "".join(rows)
        + "</ul></details>"
        if rows
        else ""
    )


def quality(report: Report) -> str:
    q = report.quality
    title = "Данные подходят для оценки" if q.score >= 0.8 else "Качество измерений ограничено"
    reasons = []
    if q.coverage_pct < 95:
        reasons.append("В периоде есть пропуски измерений; часть динамики неизвестна.")
    if q.stuck_pct > 0:
        reasons.append("Температура менялась редко; оценка динамики может быть менее уверенной.")
    if q.implausible_jumps:
        reasons.append("Есть подозрительные скачки температуры.")
    if q.score < 0.8 and not reasons:
        reasons.append("Полноты и достоверности измерений недостаточно для уверенной оценки всей динамики.")
    dhw = report.context.get("dhw_interaction", {})
    dhw_q = dhw.get("data_quality", {}) if isinstance(dhw, dict) else {}
    dhw_warning = ""
    if isinstance(dhw_q, dict) and isinstance(dhw_q.get("score"), (int, float)) and dhw_q["score"] < .8:
        dhw_warning = ('<h3>Качество измерения ГВС ограничено</h3>'
                       '<p>Температурный ряд ГВС имеет ограничения; выводы о динамике нагрева '
                       'менее уверенные. Качество: ' + number(dhw_q["score"] * 100, "%") + '.</p>'
                       + debug(dhw_q, "Качество ГВС / исходные показатели"))
    return (
        f'<section class="quality"><h2>{title}</h2><p>{esc(" ".join(reasons))}</p>'
        f"<p>Качество: {number(q.score * 100, '%')} · покрытие: {number(q.coverage_pct, '%')}</p>"
        + dhw_warning
        + debug(q.model_dump(), "Качество / исходные показатели")
        + "</section>"
    )

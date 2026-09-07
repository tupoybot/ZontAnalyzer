"""Presentation of existing report facts; no analysis or persistence."""

from __future__ import annotations

import html
import json
import math
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


def period_target(report: Report) -> tuple[float | None, float | None]:
    """Use historical means for period cards, retaining old canonical evidence."""
    if report.kind not in {"weekly", "monthly", "seasonal"}:
        value = report.context.get("current_target_c")
        return (float(value), None) if isinstance(value, (int, float)) else (None, None)
    value = report.context.get("period_target_mean_c")
    coverage = report.context.get("period_target_coverage_pct")
    if value is None:
        temporal = report.context.get("temporal_evidence", {})
        quality = temporal.get("quality", {}) if isinstance(temporal, dict) else {}
        signal = quality.get("target_temperature", {}) if isinstance(quality, dict) else {}
        value = signal.get("mean") if isinstance(signal, dict) else None
        coverage = signal.get("coverage_pct") if isinstance(signal, dict) else None
    if (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        and isinstance(coverage, (int, float)) and math.isfinite(coverage) and 0 < coverage <= 100
    ):
        return float(value), float(coverage)
    return None, 0.0


def _gas_value(value: Any, unit: str = "") -> str:
    return number(value, unit) if isinstance(value, (int, float)) and not isinstance(value, bool) else "Нет данных"


_GAS_REASONS = {
    "incomplete_passport_range": "Паспортный диапазон расхода указан не полностью",
    "invalid_meter_interval": "Некорректный интервал показаний исключён",
    "invalid_feature_shape": "Неполные признаки режима исключены",
    "telemetry_gaps_excluded": "Интервалы с большими пропусками исключены из калибровки",
    "positive_meter_volume_without_flame": "Расход счётчика не объясняется наблюдаемым горением",
    "shared_meter_gas_stove_unseparated": "Расход плиты не отделён от общего счётчика",
    "meter_scope_unconfirmed": "Другие потребители общего счётчика не подтверждены",
    "passport_prior_without_meter_readings": "Предварительная оценка по паспорту без калибровки счётчиком",
    "no_valid_meter_intervals": "Нет пригодных интервалов для калибровки",
    "mean_model_selected": "Использован средний расход во время горения",
    "insufficient_interval_diversity": "Разнообразия режимов недостаточно для таблицы модуляции",
    "unobserved_modulation_bins_use_mean_or_prior": "Ненаблюдавшиеся режимы используют среднее или паспорт",
    "low_support_modulation_bins": "Мало наблюдений в части диапазонов модуляции",
    "no_heldout_validation": "Нет независимой отложенной проверки точности",
    "extrapolated_unknown_modulation": "Модуляция части наблюдаемого горения неизвестна",
    "extrapolated_modulation_range": "Есть режимы вне поддержанной калибровки",
    "telemetry_gap_estimated_from_observed_mix": "Короткие пропуски оценены по наблюдаемым режимам",
    "calibration_is_stale": "Калибровка устарела",
    "observed_burner_off": "На наблюдаемом интервале горения не было",
    "insufficient_telemetry_coverage": "Покрытия телеметрии недостаточно для итога периода",
}


def gas_period_card(report: Report) -> str:
    """Render the local gas estimate contract without inventing unavailable values."""
    gas = report.context.get("gas")
    if not isinstance(gas, dict):
        gas = {}
    status = str(gas.get("status") or "unknown")
    status_labels = {
        "unknown": "нет данных", "measured": "измерено", "estimated": "оценено",
        "extrapolated": "экстраполировано",
    }
    status_label = status_labels.get(status, "нет данных")
    volume = _gas_value(gas.get("volume_m3"), "м³") if status != "unknown" else "Нет данных"
    bounds = []
    lower, upper = gas.get("lower_m3"), gas.get("upper_m3")
    if isinstance(lower, (int, float)) and not isinstance(lower, bool):
        bounds.append(f"от {_gas_value(lower, 'м³')}")
    if isinstance(upper, (int, float)) and not isinstance(upper, bool):
        bounds.append(f"до {_gas_value(upper, 'м³')}")
    details: list[str] = [f"Статус: {status_label}"]
    if bounds:
        details.append("Диапазон: " + " — ".join(bounds))
    if status == "unknown" and isinstance(gas.get("observed_volume_m3"), (int, float)):
        details.append("Оценка только наблюдаемой части: " + _gas_value(gas["observed_volume_m3"], "м³"))
    reliability_index = gas.get("reliability_index_pct")
    if isinstance(reliability_index, (int, float)) and not isinstance(reliability_index, bool):
        details.append(f"Индекс надёжности: {_gas_value(reliability_index, '%')}")
    coverage = gas.get("coverage_pct")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        details.append(f"Покрытие: {_gas_value(coverage, '%')}")
    observed_days = gas.get("observed_days")
    if isinstance(observed_days, (int, float)) and not isinstance(observed_days, bool):
        details.append(f"Знаменатель: {observed_days:g} календарных суток обработанной части периода")
    for key, label, unit in (("average_daily_m3", "Среднее за сутки", "м³/сутки"),
                             ("average_weekly_m3", "Среднее за 7 суток (не итог конкретной недели)", "м³/неделю")):
        if isinstance(gas.get(key), (int, float)) and not isinstance(gas.get(key), bool):
            details.append(f"{label}: {_gas_value(gas[key], unit)}")
    if gas.get("complete") is False:
        details.append("Период неполный; итог не представляет полный сезон")
    for key in ("source", "uncertainty_method"):
        if gas.get(key):
            details.append(str(gas[key]))
    details.append("Индекс надёжности не является вероятностью точности.")
    version = gas.get("model_version")
    if version:
        details.append(f"Модель: {version}")
    reasons = gas.get("reasons")
    if isinstance(reasons, list) and reasons:
        details.append("Ограничения: " + "; ".join(_GAS_REASONS.get(str(item), str(item)) for item in reasons))
    scope = gas.get("scope")
    if scope:
        details.append("Охват: " + {
            "boiler": "котёл", "shared_meter": "приближение по общему счётчику",
            "whole_meter": "весь счётчик",
        }.get(str(scope), str(scope)))
    stale = (
        '<p class="gas-period-stale" role="status"><strong>Объяснение AI устарело:</strong> '
        'оно относится к предыдущей версии модели расхода.</p>'
        if gas.get("ai_stale") is True else ""
    )
    intervals = gas.get("measured_intervals")
    interval_rows = []
    if isinstance(intervals, list):
        for item in intervals:
            if not isinstance(item, dict):
                continue
            start = str(item.get("start", item.get("before_start", "неизвестно")))[:10]
            end = str(item.get("end", item.get("after_end", "неизвестно")))[:10]
            volume_text = _gas_value(item.get("volume_m3"), "м³")
            residual = item.get("predicted_residual_m3", item.get("residual_m3"))
            residual_text = (
                f"; невязка модели {_gas_value(residual, 'м³')}"
                if isinstance(residual, (int, float)) else ""
            )
            interval_rows.append(f"<li>{esc(start)} — {esc(end)}: {esc(volume_text)}{esc(residual_text)}</li>")
    interval_details = (
        '<details class="gas-measured-intervals"><summary>Измеренные интервалы</summary>'
        "<p>Объём измерен за весь интервал между показаниями; даты имеют точность до дня.</p><ul>"
        + "".join(interval_rows) + "</ul></details>"
        if interval_rows else ""
    )
    return (
        '<details class="gas-period-card"><summary id="gas-period-title">'
        f'Расход газа за период: {esc(volume)} · подробнее</summary>'
        f'<p class="gas-period-details">{esc(" · ".join(details))}</p>'
        + stale
        + interval_details
        + debug(gas, "Расход газа / происхождение")
        + "</details>"
    )


def gas_savings_text(report: Report) -> list[str]:
    savings = report.context.get("gas_savings")
    if not isinstance(savings, dict):
        return []
    if savings.get("status") != "available":
        return ["Экономия газа: " + str(savings.get("reason") or "Недостаточно данных.")]
    lines = ["Экономия газа"]
    labels = {"effect_indistinguishable": "эффект неразличим на фоне неопределённости",
              "estimated": "оценка", "confounded": "эффект смешан с другими изменениями",
              "extrapolated": "экстраполяция", "model_only": "модельная оценка",
              "measured": "проверено последующими показаниями"}
    for item in savings.get("comparisons", []):
        if not isinstance(item, dict):
            continue
        before = str(item.get("before_start", "неизвестно"))[:10]
        after = str(item.get("after_start", "неизвестно"))[:10]
        raw, normalized = item.get("raw_savings", {}), item.get("normalized_savings", {})
        value, spread = normalized.get("m3"), item.get("uncertainty_m3")
        lines.append(f"Сравнение: {before} и {after}")
        lines.append(f"Исходная разница: {_gas_value(raw.get('m3'), 'м³')} "
                     f"({_gas_value(raw.get('pct'), '%')})")
        lines.append(f"Нормализованное изменение: {_gas_value(value, 'м³')} "
                     f"({_gas_value(normalized.get('pct'), '%')})")
        if isinstance(value, (int, float)) and isinstance(spread, (int, float)):
            lines.append(f"Диапазон эффекта: {_gas_value(value-spread, 'м³')} — "
                         f"{_gas_value(value+spread, 'м³')}; не вероятностный интервал.")
        effect = str(item.get("effect_status", "unknown"))
        lines.append("Статус эффекта: " + labels.get(effect, effect))
        provenance = item.get("provenance", {})
        lines.append("Расход после изменения: " + ("оценён замороженной моделью" if provenance.get('model_only')
                     else "проверен независимым показанием счётчика"))
        if provenance.get('extrapolated'):
            lines.append("Условия вне диапазона обучения; экономия не подтверждена.")
        if provenance.get('base_temperature_c') is not None:
            lines.append(f"База градусо-часов: {_gas_value(provenance['base_temperature_c'], '°C')}")
        for key, label in (("assumptions", "Допущения"), ("confounders", "Смешивающие факторы"),
                           ("diagnostics", "Ограничения проверки")):
            if item.get(key):
                lines.append(label + ": " + "; ".join(str(v) for v in item[key]))
        lines.append("Причинность по одному сравнению не доказана.")
    return lines


def gas_savings_section(report: Report) -> str:
    lines = gas_savings_text(report)
    if not lines:
        return ""
    return ('<section class="full-width gas-savings"><h2>Экономия газа</h2>'
            + "".join(f"<p>{esc(line)}</p>" for line in lines if line != "Экономия газа") + "</section>")


def kpis(report: Report) -> str:
    metrics = {m.name: m for m in report.metrics}

    target_value, target_coverage = period_target(report)
    is_period = report.kind in {"weekly", "monthly", "seasonal"}

    def metric(name: str, unit: str) -> str:
        if name not in metrics:
            return "Нет данных"
        value = metrics[name].value
        return str(int(value)) if not unit and value.is_integer() else number(value, unit)

    values = [
        ("Комната · средняя", metric("mean_temperature_c", "°C")),
        (
            "Цель · средняя за период" if is_period else "Цель · на конец периода",
            number(target_value, "°C"),
        ),
        ("Улица · средняя", metric("outdoor_mean_temperature_c", "°C")),
        ("Качество данных", number(report.quality.score * 100, "%")),
        ("Отопление · запуски", metric("burner_starts", "")),
        ("ГВС · догревы", metric("dhw_episode_count", "")),
    ]
    return (
        '<section class="kpi-grid" aria-label="Ключевые показатели">'
        + "".join(
            f'<div class="kpi"><span>{esc(label)}</span><strong>{esc(value)}</strong>'
            + (f'<small>По {target_coverage:g}% периода</small>'
               if index == 1 and is_period and target_value is not None
               and target_coverage is not None and target_coverage < 99 else "")
            + "</div>" for index, (label, value) in enumerate(values)
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
    gas = report.context.get("gas", {})
    status = {"measured": "Измерено по датам показаний", "estimated": "Оценка",
              "extrapolated": "Экстраполяция"}.get(gas.get("status"), "Нет данных")
    volume = _gas_value(gas.get("volume_m3"), "м³") if gas.get("status") != "unknown" else "Нет данных"
    cards.append('<div class="kpi gas-kpi"><span>Расход газа за период</span>'
                 f'<strong>{esc(volume)}</strong><small>{esc(status)}</small></div>')
    return '<div class="kpi-uptime-row" aria-label="Надёжность и газ">' + "".join(cards) + "</div>"


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
    gas = report.context.get("gas")
    if isinstance(gas, dict):
        flame_hours = gas.get("flame_hours")
        observed_hours = gas.get("observed_hours")
        flame_pct = gas.get("flame_pct")
        if isinstance(flame_hours, (int, float)) and not isinstance(flame_hours, bool):
            denominator = (
                f" за {observed_hours:g} ч наблюдений"
                if isinstance(observed_hours, (int, float)) and not isinstance(observed_hours, bool)
                else ""
            )
            percent = (
                f"; {_gas_value(flame_pct, '%')} от наблюдаемого времени"
                if isinstance(flame_pct, (int, float)) and not isinstance(flame_pct, bool)
                else ""
            )
            groups["Отопление"].append(
                '<tr><th scope="row">Время работы горелки</th>'
                f'<td>{esc(_gas_value(flame_hours, "ч"))}{esc(denominator)}{esc(percent)}</td></tr>'
            )
            for key, label in (("heating_flame_hours", "Горение в режиме отопления"),
                               ("dhw_flame_hours", "Горение в режиме ГВС"),
                               ("purpose_unknown_flame_hours", "Горение с неопределённым назначением")):
                if isinstance(gas.get(key), (int, float)):
                    groups["Отопление"].append(
                        f'<tr><th scope="row">{label}</th><td>{esc(_gas_value(gas[key], "ч"))}</td></tr>'
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

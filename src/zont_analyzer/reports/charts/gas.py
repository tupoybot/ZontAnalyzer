"""Daily gas bars from explicit calendar-day calculations, without interpolation."""
from __future__ import annotations

import math
from datetime import timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report


def _volume(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _number(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def render_daily_gas(report: Report) -> str:
    if report.kind not in {"weekly", "monthly"}:
        return ""
    gas = report.context.get("gas")
    raw = gas.get("daily", []) if isinstance(gas, dict) else []
    by_day = {item.get("day"): item for item in raw if isinstance(item, dict)} if isinstance(raw, list) else {}
    zone = ZoneInfo(report.timezone)
    day = report.period_start.astimezone(zone).date()
    last = (report.period_end - timedelta(microseconds=1)).astimezone(zone).date()
    rows: list[tuple[str, str, float | None, str]] = []
    while day <= last:
        item = by_day.get(day.isoformat(), {})
        status = item.get("status", "unknown")
        value = _volume(item.get("volume_m3")) if status in {"measured", "estimated", "extrapolated"} else None
        label = "По счётчику" if status == "measured" else "Оценка"
        if status == "extrapolated":
            label = "Оценка с экстраполяцией"
        if value is None:
            label = "Нет данных"
            status = "unknown"
        elif item.get("complete") is False:
            label += " · часть суток"
        rows.append((day.strftime("%d.%m"), status, value, label))
        day += timedelta(days=1)
    heading = '<section class="gas-daily full-width" id="gas-daily"><h2>Расход газа по дням</h2>'
    if not rows or not any(value is not None for _, _, value, _ in rows):
        return heading + '<p class="chart-note">Нет дневных данных о расходе газа за этот период.</p></section>'
    width, height = max(680, len(rows) * 38 + 64), 270
    left, top, bottom = 52, 30, 220
    step = (width - left - 16) / len(rows)
    maximum = max(value or 0 for _, _, value, _ in rows)
    ceiling = max(1.0, maximum * 1.18)
    svg = [f'<svg viewBox="0 0 {width} {height}" style="min-width:{width}px" role="img" '
           'aria-labelledby="gas-daily-title gas-daily-desc">'
           '<title id="gas-daily-title">Расход газа по дням, м³</title>'
           '<desc id="gas-daily-desc">Зелёные столбцы — показания счётчика, синие — оценка. '
           'Пропуски отмечены прочерком. Точные значения приведены в таблице ниже.</desc>']
    for tick in range(5):
        value = ceiling * tick / 4
        y = bottom - (bottom - top) * tick / 4
        svg.append(f'<path class="gas-grid" d="M{left} {y:.1f}H{width - 16}"/>'
                   f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{_number(value)}</text>')
    table = []
    for index, (date_label, status, value, label) in enumerate(rows):
        x = left + step * (index + .5)
        formatted = _number(value) + " м³" if value is not None else "—"
        title = escape(f"{date_label}: {formatted} · {label}")
        if value is None:
            svg.append(f'<text class="gas-missing" x="{x:.1f}" y="{bottom - 8}" '
                       f'text-anchor="middle"><title>{title}</title>—</text>')
        else:
            bar_height = (bottom - top) * value / ceiling
            # Zero remains a zero-height value; a baseline marker makes it visible.
            style = "measured" if status == "measured" else "estimated"
            svg.append(f'<rect class="gas-day-bar gas-day-{style}" x="{x - step * .32:.1f}" '
                       f'y="{bottom - max(2, bar_height):.1f}" width="{step * .64:.1f}" '
                       f'height="{max(2, bar_height):.1f}" rx="3"><title>{title}</title></rect>')
            if len(rows) <= 7:
                svg.append(f'<text x="{x:.1f}" y="{bottom - bar_height - 9:.1f}" '
                           f'text-anchor="middle">{_number(value)}</text>')
        svg.append(f'<text x="{x:.1f}" y="{bottom + 24}" text-anchor="middle">{date_label}</text>')
        table.append(f'<tr><th scope="row">{date_label}</th><td>{formatted}</td><td>{escape(label)}</td></tr>')
    svg.append('</svg>')
    return (heading + '<p class="chart-note">м³ за календарные сутки · ' + escape(report.timezone)
            + '</p><div class="gas-daily-scroll" tabindex="0" role="region" '
            'aria-label="Диаграмма расхода газа; можно прокручивать по горизонтали">'
            + ''.join(svg) + '</div><div class="gas-daily-legend">'
            '<span><i class="gas-day-measured"></i>По счётчику</span>'
            '<span><i class="gas-day-estimated"></i>Оценка</span><span>— Нет данных</span></div>'
            '<p class="chart-note">Показания счётчика известны с точностью до дня. '
            'Оценки рассчитаны по работе горелки; их сумма может отличаться от итога по счётчику. '
            'Дни без данных не считаются нулевым расходом.</p>'
            '<details><summary>Значения по дням</summary><table><thead><tr><th>Дата</th>'
            '<th>Расход</th><th>Источник</th></tr></thead><tbody>' + ''.join(table)
            + '</tbody></table></details></section>')

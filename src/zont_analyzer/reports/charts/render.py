from __future__ import annotations

import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report

from .theme import state_color, style_for


@dataclass(frozen=True)
class _Point:
    at: datetime
    value: float
    gap_before: bool = False


@dataclass(frozen=True)
class _Series:
    role: str
    label: str
    unit: str
    points: tuple[_Point, ...]
    explicit_gaps: bool = False
    step: bool = False


_PANELS = (
    ("climate", "Температура дома", ("control_temperature", "target_temperature", "outdoor_temperature")),
    ("thermal", "Тепловая система", (
        "flow_temperature", "target_flow_temperature", "return_temperature", "dhw_temperature",
    )),
)
_FALLBACK_LABELS = {
    "control_temperature": "Комната",
    "target_temperature": "Цель",
    "outdoor_temperature": "Улица",
    "flow_temperature": "Подача",
    "target_flow_temperature": "Цель подачи (cs)",
    "return_temperature": "Обратка",
    "dhw_temperature": "ГВС",
}
_STATE_LABELS = {
    "ch": "Пламя CH", "ch_flame": "Пламя CH",
    "dhw": "Пламя ГВС", "dhw_flame": "Пламя ГВС",
    "concurrent_unknown": "Одновременные флаги: не интерпретируется",
    "unknown": "Неизвестное состояние", "missing": "Нет наблюдения состояния",
}


def render_charts(
    report: Report, chart_data: dict[str, Any] | None = None, *, panel_ids: Sequence[str] | None = None,
) -> str:
    """Render two embedded SVG panels from raw report-bound observations.

    A missing chart packet is a normal state for existing report archives.  It
    remains visible as an explicit unavailable message rather than inventing a
    curve from metric values or hourly evidence summaries.
    """

    # ``chart_data`` is intentionally a rendering-only adjunct: reports remain
    # canonical, compact and backward-compatible.  The context fallback helps
    # inspect an explicitly embedded fixture, but normal publication passes the
    # adjunct read from the telemetry store.
    packet: Any = chart_data if chart_data is not None else report.context.get("chart_series")
    timezone = _timezone(packet, report.timezone)
    series, bands = _parse_packet(packet)
    period = (report.period_start, report.period_end)
    wanted = set(panel_ids) if panel_ids is not None else {item[0] for item in _PANELS}
    panels = "".join(
        _render_panel(panel_id, title, roles, series, bands, timezone, period)
        for panel_id, title, roles in _PANELS
        if panel_id in wanted
    )
    return '<section class="report-charts" aria-label="Графики наблюдений">' + panels + "</section>"


def _timezone(packet: Any, fallback: str) -> ZoneInfo:
    value = packet.get("timezone") if isinstance(packet, Mapping) else fallback
    try:
        return ZoneInfo(str(value or fallback))
    except Exception:
        return ZoneInfo("UTC")


def _parse_packet(packet: Any) -> tuple[dict[str, _Series], list[tuple[datetime, datetime, str, str]]]:
    if not isinstance(packet, Mapping):
        return {}, []
    raw_series = packet.get("series")
    explicit_gaps = packet.get("gap_policy") == "explicit"
    parsed: dict[str, _Series] = {}
    if isinstance(raw_series, Mapping):
        for role, raw in raw_series.items():
            if not isinstance(role, str) or not isinstance(raw, Mapping):
                continue
            points = _points(raw.get("points"))
            if points:
                parsed[role] = _Series(
                    role, str(raw.get("label") or _FALLBACK_LABELS.get(role, role)),
                    str(raw.get("unit") or ""), tuple(points), explicit_gaps=explicit_gaps,
                    step=raw.get("interpolation") == "step",
                )
    bands: list[tuple[datetime, datetime, str, str]] = []
    raw_bands = packet.get("state_bands")
    if isinstance(raw_bands, Sequence) and not isinstance(raw_bands, (str, bytes)):
        for raw in raw_bands:
            if not isinstance(raw, Mapping):
                continue
            start, end = _datetime(raw.get("started_at")), _datetime(raw.get("ended_at"))
            if start is not None and end is not None and start < end:
                bands.append((start, end, str(raw.get("label") or "Состояние"), str(raw.get("state") or "unknown")))
    return parsed, sorted(bands, key=lambda item: item[:2])


def _points(value: Any) -> list[_Point]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    unique: dict[datetime, tuple[float, bool]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        at = _datetime(raw.get("timestamp"))
        number = raw.get("value")
        if at is None or not isinstance(number, (int, float)) or isinstance(number, bool):
            continue
        numeric = float(number)
        if math.isfinite(numeric):
            unique[at] = (numeric, raw.get("gap_before") is True)
    return [_Point(at, *unique[at]) for at in sorted(unique)]


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _render_panel(
    panel_id: str, title: str, roles: tuple[str, ...], all_series: Mapping[str, _Series],
    bands: list[tuple[datetime, datetime, str, str]], timezone: ZoneInfo,
    period: tuple[datetime, datetime],
) -> str:
    selected = [all_series[role] for role in roles if role in all_series and len(all_series[role].points) >= 2]
    missing = [_FALLBACK_LABELS[role] for role in roles if role not in {item.role for item in selected}]
    if not selected:
        return (
            f'<figure class="report-chart report-chart--unavailable" data-chart="{panel_id}">'
            f"<figcaption><strong>{html.escape(title)}</strong></figcaption>"
            '<p class="chart-unavailable">Нет временного ряда для графика. '
            "Сводные метрики и почасовые агрегаты не отображаются как кривая.</p></figure>"
        )
    observed = [point for item in selected for point in item.points]
    observed_left, observed_right = min(point.at for point in observed), max(point.at for point in observed)
    left, right = period
    if right <= left:
        left, right = observed_left, observed_right
    low, high = min(point.value for point in observed), max(point.value for point in observed)
    padding = max((high - low) * 0.08, 0.5)
    low, high = low - padding, high + padding
    width, height, inset = 760, 180, 0
    plot_width, plot_height = width, height
    span = max((right - left).total_seconds(), 1.0)
    def scale_x(at: datetime) -> float:
        return inset + ((at - left).total_seconds() / span) * plot_width

    def scale_y(value: float) -> float:
        return (high - value) / (high - low) * plot_height

    description = html.escape(", ".join(item.label for item in selected))
    svg_parts = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-labelledby="{panel_id}-title {panel_id}-desc">',
        f'<title id="{panel_id}-title">{html.escape(title)}</title>',
        f'<desc id="{panel_id}-desc">Временные наблюдения: {description}.</desc>',
        f'<rect x="{inset}" y="0" width="{plot_width:.2f}" height="{plot_height:.2f}" '
        'fill="#fbfcfe" stroke="#d9e1ea"/>',
    ]
    y_labels: list[str] = []
    for fraction in (0, 0.5, 1):
        y = fraction * plot_height
        value = high - fraction * (high - low)
        svg_parts.append(f'<path d="M {inset} {y:.2f} H {width}" stroke="#e6ebf0"/>')
        label = f"{value:.1f}".replace(".", ",")
        y_labels.append(f'<span style="top:{fraction * 100:g}%">{label}</span>')
    visible_bands: list[tuple[str, str]] = []
    # State bands are rendered only when the input explicitly contains intervals.
    for start, end, label, state in bands if panel_id == "thermal" else ():
        clipped_start, clipped_end = max(start, left), min(end, right)
        if clipped_start >= clipped_end:
            continue
        x, band_width = scale_x(clipped_start), scale_x(clipped_end) - scale_x(clipped_start)
        svg_parts.append(
            f'<rect x="{x:.2f}" y="0" width="{band_width:.2f}" height="{plot_height:.2f}" '
            f'fill="{state_color(state)}" fill-opacity=".24"><title>{html.escape(label)}: '
            f'{html.escape(state)}</title></rect>'
        )
        visible_bands.append((label, state))
    gap_counts: dict[str, int] = {}
    for item in selected:
        paths, gap_count = _paths(item.points, scale_x, scale_y, explicit_gaps=item.explicit_gaps, step=item.step)
        role_style = style_for(item.role)
        dash = f' stroke-dasharray="{role_style.dasharray}"' if role_style.dasharray else ""
        for path in paths:
            svg_parts.append(
                f'<path data-role="{html.escape(item.role, quote=True)}" d="{path}" fill="none" '
                f'stroke="{role_style.color}"{dash} stroke-width="2.25" vector-effect="non-scaling-stroke"/>'
            )
        gap_counts[item.role] = gap_count
    x_labels: list[str] = []
    for fraction in (0, 0.25, 0.5, 0.75, 1):
        at = left + (right - left) * fraction
        tick_class = ("chart-first-tick" if fraction == 0 else "chart-last-tick" if fraction == 1
                      else "chart-minor-tick" if fraction in (0.25, 0.75) else "")
        x_labels.append(
            f'<span class="{tick_class}" style="left:{fraction * 100:g}%">'
            f'{html.escape(_axis_time(at, timezone, right - left))}</span>'
        )
    svg_parts.append("</svg>")
    legend = "".join(
        '<li data-role="' + html.escape(item.role, quote=True)
        + '"><span style="background:' + style_for(item.role).color
        + '" aria-hidden="true"></span>' + html.escape(item.label)
        + html.escape(f" · разрывы: {gap_counts[item.role]}" if gap_counts[item.role] else "") + "</li>"
        for item in selected
    )
    unavailable = f'<p class="chart-note">Нет ряда: {html.escape(", ".join(missing))}.</p>' if missing else ""
    state_legend = _state_legend(visible_bands)
    return (
        f'<figure class="report-chart" data-chart="{panel_id}"><figcaption><strong>{html.escape(title)}</strong>'
        f'</figcaption><div class="chart-unit">{html.escape(_unit(selected))}</div>'
        f'<div class="chart-plot-grid"><div class="chart-y-axis">{"".join(y_labels)}</div>'
        f'{"".join(svg_parts)}<div class="chart-x-axis">{"".join(x_labels)}</div></div>'
        f'<ul class="chart-legend">{legend}</ul>{state_legend}{unavailable}</figure>'
    )


def _paths(
    points: tuple[_Point, ...], scale_x: Any, scale_y: Any, *, explicit_gaps: bool = False, step: bool = False,
) -> tuple[list[str], int]:
    gaps = [
        (right.at - left.at).total_seconds()
        for left, right in zip(points, points[1:], strict=False)
        if right.at > left.at
    ]
    threshold = median(gaps) * 3 if gaps else math.inf
    result: list[str] = []
    current: list[str] = []
    gap_count = 0
    previous: _Point | None = None
    for point in points:
        inferred_gap = (point.at - previous.at).total_seconds() > threshold if previous is not None else False
        if previous is None or point.gap_before or (not explicit_gaps and inferred_gap):
            if current:
                result.append(" ".join(current))
            current = [f"M {scale_x(point.at):.2f} {scale_y(point.value):.2f}"]
            if previous is not None:
                gap_count += 1
        elif step:
            current.append(f"L {scale_x(point.at):.2f} {scale_y(previous.value):.2f}")
            current.append(f"L {scale_x(point.at):.2f} {scale_y(point.value):.2f}")
        else:
            current.append(f"L {scale_x(point.at):.2f} {scale_y(point.value):.2f}")
        previous = point
    if current:
        result.append(" ".join(current))
    return result, gap_count


def _unit(series: list[_Series]) -> str:
    units = {item.unit for item in series if item.unit}
    return next(iter(units)) if len(units) == 1 else "значение"


def _axis_time(value: datetime, timezone: ZoneInfo, span: Any) -> str:
    local = value.astimezone(timezone)
    return local.strftime("%d.%m %H:%M" if span.days >= 2 else "%H:%M")


def _state_legend(bands: list[tuple[str, str]]) -> str:
    seen: set[tuple[str, str]] = set()
    items: list[str] = []
    for label, state in bands:
        key = (label, state)
        if key in seen:
            continue
        seen.add(key)
        semantic = _STATE_LABELS.get(state.casefold(), "Неизвестное состояние")
        text = label if label and label != semantic else semantic
        if label and label != semantic:
            text = f"{semantic}: {label}"
        items.append(
            f'<li class="chart-state" data-state="{html.escape(state, quote=True)}"><span '
            f'style="background:{state_color(state)}" aria-hidden="true"></span>{html.escape(text)}</li>'
        )
    return f'<ul class="chart-state-legend" aria-label="Состояния котла">{"".join(items)}</ul>' if items else ""

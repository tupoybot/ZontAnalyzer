"""Read-only presentation of the controller settings snapshot."""
from __future__ import annotations

import html
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "не указано"
    return f"{value:g}".replace(".", ",")


def _snapshot_time(value: Any, timezone: str) -> str:
    if not isinstance(value, str):
        return "время снимка не указано"
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            return "время снимка не указано"
        moment = moment.astimezone(ZoneInfo(timezone))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return "время снимка не указано"
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z")


def _parameter(settings: Mapping[str, Any], field: str) -> Any:
    parameters = settings.get("parameters")
    if not isinstance(parameters, list):
        return None
    for item in parameters:
        if isinstance(item, Mapping) and item.get("field") == field:
            return item.get("value")
    return None


def _rows(settings: Mapping[str, Any], timezone: str) -> list[str]:
    regulation = settings.get("regulation")
    regulation = regulation if isinstance(regulation, Mapping) else {}
    status = regulation.get("status")
    label = regulation.get("label")
    if status == "decoded" and isinstance(label, str) and label.strip():
        regulation_text = f"Настроенное регулирование: {label}"
    elif status == "not_applicable":
        regulation_text = "Настроенное регулирование: неприменимо"
    else:
        regulation_text = "Настроенное регулирование: неизвестно"
    rows = [regulation_text]
    if regulation.get("enabled") is False:
        rows.append("Контур отключён в настройках")
    proportional = _parameter(settings, "pid_prop_koef")
    integral = _parameter(settings, "pid_integral_koef")
    if _number(proportional) != "не указано":
        rows.append(f"P: {_number(proportional)}")
    if _number(integral) != "не указано":
        rows.append(f"I: {_number(integral)}")
    role = regulation.get("pza_role_label") or regulation.get("pza_role")
    if isinstance(role, str) and role.strip():
        rows.append(f"Роль ПЗА: {role}")
    curve = settings.get("pza_curve")
    points = curve.get("points_c") if isinstance(curve, Mapping) else None
    if isinstance(points, list) and points:
        rows.append("Кривая ПЗА:")
        for point in points:
            if (isinstance(point, Mapping) and _number(point.get("outdoor_c")) != "не указано"
                    and _number(point.get("flow_c")) != "не указано"):
                rows.append(f"  {_number(point['outdoor_c'])} → {_number(point['flow_c'])}")
    rows.append(f"Время снимка: {_snapshot_time(settings.get('captured_at'), timezone)}")
    rows.append("Снимок настроек не подтверждает их неизменность за весь период.")
    return rows


def control_settings_text(settings: Any, timezone: str) -> list[str]:
    if not isinstance(settings, Mapping):
        return []
    return ["Настройки регулирования отопления:", *[f"- {row}" for row in _rows(settings, timezone)]]


def control_settings_html(settings: Any, timezone: str) -> str:
    if not isinstance(settings, Mapping):
        return ""
    rows = _rows(settings, timezone)
    body_rows = "".join(
        f"<p>{html.escape(row)}</p>"
        for row in rows
        if row != "Кривая ПЗА:" and not row.startswith("  ")
    )
    curve = settings.get("pza_curve")
    points = curve.get("points_c") if isinstance(curve, Mapping) else None
    curve_rows = ""
    if isinstance(points, list) and points:
        valid_points = [
            point for point in points
            if isinstance(point, Mapping)
            and isinstance(point.get("outdoor_c"), (int, float))
            and not isinstance(point.get("outdoor_c"), bool)
            and math.isfinite(point["outdoor_c"])
            and isinstance(point.get("flow_c"), (int, float))
            and not isinstance(point.get("flow_c"), bool)
            and math.isfinite(point["flow_c"])
        ]
        curve_rows = (
            '<table class="control-settings-curve"><thead><tr>'
            "<th>На улице, °C</th><th>Теплоноситель, °C</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{html.escape(_number(point['outdoor_c']))}</td>"
                f"<td>{html.escape(_number(point['flow_c']))}</td></tr>"
                for point in valid_points
            )
            + "</tbody></table>"
            if valid_points
            else ""
        )
    return (
        '<details class="control-settings"><summary>Настройки регулирования (снимок)</summary>'
        f"{body_rows}{curve_rows}</details>"
    )

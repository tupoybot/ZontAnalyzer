"""Optional owner-recorded experiment fields shared by report cards."""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .timezone_labels import timezone_label

CATEGORIES = {
    "": "Не указано", "settings": "Настройки ZONT",
    "firmware_update": "Обновление прошивки", "firmware_rollback": "Откат прошивки",
    "other": "Другое изменение",
}


def experiment_form(value: Any, disabled: str = "", timezone: str = "UTC") -> str:
    experiment = value if isinstance(value, dict) else {}
    category = experiment.get("category") or ""
    options = "".join(
        f'<option value="{key}"{" selected" if key == category else ""}>{label}</option>'
        for key, label in CATEGORIES.items()
    )
    values = dict(experiment)
    if values.get("performed_at"):
        values["performed_at"] = datetime.fromisoformat(values["performed_at"]).astimezone(
            ZoneInfo(timezone)
        ).strftime("%Y-%m-%dT%H:%M:%S")
    def display(value: Any) -> str:
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)

    fields = "".join(
        f'<label>{label}<input data-experiment-field="{key}" '
        f'type="{"datetime-local" if key == "performed_at" else "text"}" '
        f'maxlength="{limit}" value="{html.escape(display(values.get(key)), quote=True)}"{disabled}></label>'
        for key, label, limit in (
            ("parameter", "Параметр", 200), ("before", "До / прежняя версия", 500),
            ("after", "После / новая версия", 500),
            ("performed_at", "Время действия", 50),
        )
    )
    return (
        f'<details class="feedback-experiment" data-timezone="{html.escape(timezone, quote=True)}">'
        '<summary>Подробности изменения…</summary>'
        '<p>Заполняйте по желанию. Укажите известные значения; остальные поля можно оставить пустыми.</p>'
        '<label>Тип изменения<select data-experiment-field="category"'
        f'{disabled}>{options}</select></label>{fields}'
        f'<small>Время: {html.escape(timezone_label(timezone))}. Сведения сохраняются кнопкой «Выполнено». '
        'Примечание можно добавить в комментарий. '
        'Для оценки результата меняйте один параметр за раз.</small></details>'
    )

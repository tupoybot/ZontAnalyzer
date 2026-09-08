"""Standalone owner forms for equipment context and daily gas readings."""
# ruff: noqa: E501
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.domain import Report
from zont_analyzer.reports.owner_script import OWNER_SCRIPT

_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("auto_adapt", "AutoAdapt", "tristate"),
    ("boiler_model", "Модель котла", "text"),
    ("has_gas_stove", "Есть газовая плита", "tristate"),
    ("installation_notes", "Примечания к установке", "text"),
    ("auto_adapt_node", "Узел AutoAdapt", "text"),
    ("auto_adapt_pump_model", "Модель насоса AutoAdapt", "text"),
    ("dhw_type", "Тип ГВС", "text"),
    ("hydraulic_separator", "Гидравлический разделитель", "tristate"),
    ("nominal_power_kw", "Номинальная мощность котла, кВт", "number"),
    ("gas_min_m3h", "Минимальный расход газа, м³/ч", "number"),
    ("gas_max_m3h", "Максимальный расход газа, м³/ч", "number"),
    ("gas_type", "Вид газа", "text"),
    ("coordinates", "Координаты (переопределение)", "coordinates"),
    ("season_boundaries", "Начало сезонов, ММ-ДД", "seasons"),
)


def _device_id(report: Report) -> str:
    context = report.context
    for key in ("device_id", "target_device_id", "reliability_device_id"):
        if context.get(key) is not None:
            return str(context[key])
    for key in ("target_series", "boiler_state_series", "burner_series"):
        value = context.get(key)
        if isinstance(value, dict) and value.get("device_id") is not None:
            return str(value["device_id"])
    return "installation"


def _json_for_script(value: Any) -> str:
    """Serialize data safely enough for a script element (including hostile text)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def render_owner_forms(report: Report, owner_data: dict[str, Any] | None = None) -> str:
    """Return a dependency-free equipment/gas form fragment with its own styles and script."""
    owner_data = owner_data if isinstance(owner_data, dict) else {}
    profiles = owner_data.get("profiles", [])
    if isinstance(profiles, dict):
        profiles = [profiles]
    profiles = [item for item in profiles if isinstance(item, dict)]
    device_id = _device_id(report)
    if device_id == "installation" and profiles:
        device_id = str(profiles[0].get("device_id") or device_id)
    profile: dict[str, Any] = next((item for item in profiles if str(item.get("device_id")) == device_id), {})
    initial = {
        "profiles": profiles,
        "field_labels": {name: label for name, label, _ in _FIELDS},
        "gas": owner_data.get("gas"),
        "device_id": device_id,
        "report_id": report.id,
        "daily": report.kind == "daily",
        "tariffs": owner_data.get("tariffs", []),
        "timezone": report.timezone,
    }
    initial_json = _json_for_script(initial)
    fields: list[str] = []
    for name, label, kind in _FIELDS:
        field = profile.get("fields", {}).get(name, {}) if isinstance(profile.get("fields"), dict) else {}
        value = field.get("value", "") if isinstance(field, dict) else field
        source = field.get("source", "") if isinstance(field, dict) else ""
        value_text = html.escape(str(value) if value is not None else "", quote=True)
        source_text = html.escape(str(source), quote=True)
        if kind == "tristate":
            unknown = "yes" if value is True else "no" if value is False else "unknown"
            fields.append(
                f'<div class="owner-field" data-field="{name}"><label for="profile-{name}">{label}</label>'
                f'<select id="profile-{name}" class="owner-unknown" aria-label="{label}, состояние"><option value="unknown"{" selected" if unknown == "unknown" else ""}>'
                f'неизвестно</option><option value="no"{" selected" if unknown == "no" else ""}>нет</option><option value="yes"{" selected" if unknown == "yes" else ""}>да</option></select>'
                f'<small class="owner-source">Источник: {source_text or "нет"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
        elif kind == "seasons":
            from zont_analyzer.domain.periods import SeasonBoundaries

            defaults = report.context.get("season_boundaries", SeasonBoundaries().model_dump())
            boundaries = value if isinstance(value, dict) else defaults
            inputs = "".join(
                f'<label>{title}<input data-season="{key}" type="text" pattern="[0-9]{{2}}-[0-9]{{2}}" '
                f'data-default="{html.escape(str(defaults[key]), quote=True)}" '
                f'value="{html.escape(str(boundaries[key]), quote=True)}" placeholder="ММ-ДД"></label>'
                for key, title in (("spring", "Весна"), ("summer", "Лето"), ("autumn", "Осень"), ("winter", "Зима"))
            )
            fields.append(
                f'<div class="owner-field" data-field="{name}"><p>{label}</p>{inputs}'
                '<small>Границы календарных отчётов. После изменения можно перегенерировать отчёт.</small>'
                f'<small class="owner-source">Источник: {source_text or "настройки дома"}</small>'
                '<button type="button" class="owner-reset">Вернуть границы из настроек</button></div>'
            )
        elif kind == "coordinates":
            coordinates = value if isinstance(value, dict) else {}
            latitude = html.escape(str(coordinates.get("latitude", "")), quote=True)
            longitude = html.escape(str(coordinates.get("longitude", "")), quote=True)
            fields.append(
                f'<div class="owner-field" data-field="{name}"><details><summary>Уточнить координаты вручную</summary>'
                f'<label>Широта<input type="text" inputmode="decimal" class="owner-coordinate" data-coordinate="latitude" maxlength="64" value="{latitude}"></label>'
                f'<label>Долгота<input type="text" inputmode="decimal" class="owner-coordinate" data-coordinate="longitude" maxlength="64" value="{longitude}"></label></details>'
                f'<small class="owner-source">Источник: {source_text or "нет"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
        elif name == "auto_adapt_node":
            node_options = ["Рециркуляция ГВС", "Радиаторное отопление", "Тёплый пол", "Котловой контур", "Другой узел"]
            selected = str(value or node_options[0])
            if selected not in node_options:
                node_options.append(selected)
            options_html = "".join(
                f'<option value="{html.escape(item, quote=True)}"{" selected" if item == selected else ""}>{html.escape(item)}</option>'
                for item in node_options
            )
            fields.append(
                f'<div class="owner-field" data-field="{name}"><label>{label}'
                f'<select class="owner-value" data-default="Рециркуляция ГВС">{options_html}</select></label>'
                f'<small class="owner-source">Источник: {source_text or "не сохранено"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
        elif name == "gas_type":
            gas_options = ["", "Природный газ (метан)", "Сжиженный газ (пропан-бутан)", "Пропан", "Бутан"]
            selected = str(value or "")
            if selected not in gas_options:
                gas_options.append(selected)
            options_html = "".join(
                f'<option value="{html.escape(item, quote=True)}"{" selected" if item == selected else ""}>{html.escape(item or "Не указано")}</option>'
                for item in gas_options
            )
            fields.append(
                f'<div class="owner-field" data-field="{name}"><label>{label}'
                f'<select class="owner-value" data-preserve-legacy="true">{options_html}</select></label>'
                f'<small class="owner-source">Источник: {source_text or "нет"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
        elif name == "dhw_type":
            options = (("", "Не указано"), ("tank", "БКН"), ("combi", "Двухконтурный котёл"), ("none", "Нет ГВС"))
            selected = str(value or "")
            options_html = "".join(
                f'<option value="{item}"{" selected" if item == selected else ""}>{label}</option>'
                for item, label in options
            )
            fields.append(
                f'<div class="owner-field" data-field="{name}"><label>{label}'
                f'<select class="owner-value">{options_html}</select></label>'
                f'<small class="owner-source">Источник: {source_text or "нет"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
        else:
            input_type = "text"
            step = ' inputmode="decimal" data-owner-number="true" maxlength="64"' if kind == "number" else ' maxlength="500"'
            fields.append(
                f'<div class="owner-field" data-field="{name}"><label>{label}'
                f'<input type="{input_type}" class="owner-value" value="{value_text}"{step}></label>'
                f'<small class="owner-source">Источник: {source_text or "нет"}</small>'
                '<button type="button" class="owner-reset">Сбросить к авто</button></div>'
            )
    gas = owner_data.get("gas") if isinstance(owner_data.get("gas"), dict) else {}
    reading = gas.get("reading") if isinstance(gas, dict) else None
    reading_value = reading.get("value_m3", "") if isinstance(reading, dict) else ""
    reading_summary = (
        f"Текущее показание: {html.escape(str(reading_value), quote=True)} м³"
        if reading_value not in (None, "")
        else "Показание не задано"
    )
    field_groups = {
        "Оборудование": {"boiler_model", "nominal_power_kw", "gas_type", "has_gas_stove", "installation_notes"},
        "Тепловая система": {"auto_adapt", "auto_adapt_node", "auto_adapt_pump_model", "dhw_type", "hydraulic_separator"},
        "Расход газа": {"gas_min_m3h", "gas_max_m3h"},
        "Расположение": {"coordinates"},
        "Сезоны дома": {"season_boundaries"},
    }
    grouped_fields = []
    for title, names in field_groups.items():
        grouped_fields.append(
            f'<fieldset class="owner-field-group"><legend>{title}</legend>'
            + "".join(item for item, (name, _, _) in zip(fields, _FIELDS, strict=True) if name in names)
            + "</fieldset>"
        )
    gas_form = "" if report.kind != "daily" else f"""
<section class="owner-form owner-gas" data-owner-gas>
  <h2>Показание газа</h2>
  <p class="owner-help">Накопленное показание счётчика, м³. День берётся из этого дневного отчёта; время снятия неизвестно.</p>
  <div class="owner-gas-summary"><p data-gas-current>{reading_summary}</p><button type="button" class="owner-secondary" data-gas-edit aria-controls="gas-editor" aria-expanded="false">Изменить показание</button></div>
  <details id="gas-editor" class="owner-gas-editor"><summary>Редактирование показания</summary>
    <label>Накопленное показание, м³ <input name="gas-value" type="text" inputmode="decimal" maxlength="64" value="{html.escape(str(reading_value), quote=True)}"></label>
    <p class="owner-help">Дробные значения можно вводить через точку или запятую.</p>
    <label class="owner-inline"><input name="gas-reset" type="checkbox"> Явная замена, сброс или переполнение счётчика</label>
    <div class="owner-actions"><button type="button" data-gas-save>Сохранить</button><button type="button" data-gas-delete>Удалить</button></div>
  </details>
  <p class="owner-help">Расход ещё не рассчитан. Показание сохраняется для дальнейшего анализа.</p>
  <p class="owner-help" data-gas-plausibility></p>
  <p data-meter-boundary></p><details class="owner-gas-history"><summary>История показания</summary><pre data-gas-history></pre></details>
  <p class="owner-message" data-gas-message role="status" aria-live="polite"></p>
</section>"""
    from zont_analyzer.application.gas_tariffs import CURRENCIES

    local_now = datetime.now(ZoneInfo(report.timezone))
    next_month = f"{local_now.year + (local_now.month == 12):04d}-{local_now.month % 12 + 1:02d}"
    currencies = "".join(
        f'<option value="{code}"{" selected" if code == "RUB" else ""}>{code}</option>' for code in CURRENCIES
    )
    tariff_form = f"""<div class="owner-tariffs">
<div class="owner-gas-summary"><p data-tariff-current>Цена за м³ не задана</p><button type="button" class="owner-secondary" data-tariff-edit aria-controls="tariff-editor" aria-expanded="false">Изменить цену</button></div>
<p class="owner-help" data-tariff-planned></p>
<details id="tariff-editor" class="owner-gas-editor"><summary>Цена газа за м³</summary>
<p class="owner-help">Тариф за м³ действует с начала месяца до следующего изменения.
Новая цена — со следующего месяца. Для внесения истории выберите нужный месяц.</p>
<p class="owner-help">Дробные значения можно вводить через точку или запятую.</p>
<div class="owner-fields">
<div class="owner-field"><label>Цена за м³<input data-tariff-price type="text" inputmode="decimal" maxlength="64" autocomplete="off"></label></div>
<div class="owner-field"><label>Валюта<select data-tariff-currency>{currencies}</select></label></div>
<div class="owner-field"><label>Месяц начала действия<input data-tariff-month type="month" value="{next_month}"></label></div>
</div><div class="owner-actions"><button type="button" data-tariff-save>Сохранить тариф</button></div>
<details><summary>История тарифов и исправления</summary><div data-tariff-history></div>
<div class="owner-field"><label>Исправить ошибку в тарифе<select data-tariff-correction><option value="">Выберите тариф</option></select></label>
<label>Причина исправления<input data-tariff-reason type="text" maxlength="500"></label>
<p class="owner-help">Укажите верную цену и валюту выше. Исправление сохраняет месяц действия и остаётся в истории.</p>
<div class="owner-actions"><button type="button" data-tariff-correct>Исправить выбранный тариф</button></div></div></details>
<p class="owner-message" data-tariff-message role="status" aria-live="polite"></p></details></div>"""
    if gas_form:
        gas_form = gas_form.replace('  <details id="gas-editor"', tariff_form + '\n  <details id="gas-editor"', 1)
    return f"""<style>
.owner-forms{{margin:1.2rem 0;font:inherit}}.owner-form{{padding:1rem;margin:.8rem 0;background:#f6f8fa;border-radius:.6rem}}
.owner-form summary{{cursor:pointer;font-weight:700;font-size:1.1rem}}.owner-fields{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:1rem;margin-top:1rem}}
.owner-field-group{{min-width:0;margin:1rem 0 0;padding:.7rem;border:1px solid #dfe5eb;border-radius:.45rem;display:grid;grid-template-columns:1fr;align-content:start;gap:.7rem}}.owner-field-group legend{{padding:0 .35rem;font-weight:700;color:#334155;grid-column:1/-1}}
.owner-field{{min-width:0;display:grid;align-content:start;align-items:start;gap:.5rem;padding:.65rem;background:white;border:1px solid #dfe5eb;border-radius:.45rem}}
.owner-field label,.owner-gas label{{display:grid;gap:.3rem;font-weight:600}}.owner-inline{{display:flex!important;align-items:center;margin:.7rem 0;font-weight:400!important}}
.owner-field input,.owner-gas input,.owner-field select{{box-sizing:border-box;width:100%;font:inherit;padding:.4rem;border:1px solid #aeb9c4;border-radius:.3rem}}
.owner-inline input,.owner-tristate{{width:auto!important}}.owner-source,.owner-help{{overflow-wrap:anywhere;color:#536579;font-size:.9rem}}.owner-actions{{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.7rem}}
.owner-actions button,.owner-reset,.owner-secondary{{font:inherit;padding:.4rem .7rem;border:0;border-radius:.35rem;background:#287943;color:#fff;cursor:pointer}}
.owner-secondary{{background:#516275}}
.owner-forms .owner-secondary:hover,.owner-forms .owner-reset:hover{{background:#394b60;color:#fff}}
.owner-forms .owner-actions button:hover{{background:#1b6033;color:#fff}}
.owner-forms .owner-reset{{justify-self:start;min-height:40px}}.owner-gas-summary{{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}}.owner-gas-summary p{{margin:.2rem 0;font-weight:700}}
.owner-gas-editor{{margin-top:.8rem;padding:.7rem;background:#fff;border:1px solid #dfe5eb;border-radius:.45rem}}.owner-gas-editor summary{{font-size:1rem}}
.owner-reset{{background:#687789;font-size:.85rem}}.owner-message.error{{color:#9b251d}}.owner-message.ok{{color:#185c2d}}
.owner-forms pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
@media(max-width:560px){{.owner-fields,.owner-field-group{{grid-template-columns:1fr}}.owner-form{{padding:.7rem}}}}
</style><section class="owner-forms" data-owner-forms data-device-id="{html.escape(device_id, quote=True)}" data-report-id="{html.escape(report.id, quote=True)}">
<details id="system-profile" class="owner-form owner-equipment" aria-label="Профиль оборудования"><summary>⚙ Профиль системы</summary>
<p class="owner-help">Значения из ZONT помечены как автоматические. Координаты и модель котла уже найденные можно уточнить вручную. Пустые поля остаются неизвестными. Новые сведения действуют с момента сохранения, если дата ниже не указана.</p>
<p class="owner-help">Дробные значения можно вводить через точку или запятую. Расход и мощность должны быть больше нуля; минимум расхода не должен превышать максимум.</p>
<p data-coordinates-summary></p><label hidden>Устройство <select data-device-select></select></label>
<div class="owner-fields">{"".join(grouped_fields)}</div>
<details class="owner-history"><summary>История изменений</summary><pre data-profile-history></pre></details>
<details class="debug-only"><summary>Технические данные профиля</summary><pre data-profile-debug></pre></details>
<label class="owner-effective">Дата применимости (необязательно, только профиль) <input type="date" data-effective-from></label>
<div class="owner-actions"><button type="button" data-profile-save>Сохранить изменения</button></div><p class="owner-message" data-profile-message role="status" aria-live="polite"></p>
</details>{gas_form}</section>
<script id="owner-initial" type="application/json">{initial_json}</script>
<script>{OWNER_SCRIPT}</script>"""

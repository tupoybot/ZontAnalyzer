"""Optional owner-recorded experiment fields shared by report cards."""
# ruff: noqa: E501 -- Embedded browser JavaScript and HTML attributes.
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


def owner_experiment_journal(report_id: str, timezone: str) -> str:
    return (
        f'<section class="full-width" data-experiment-journal data-report-id="{html.escape(report_id, quote=True)}">'
        '<details><summary>Начать эксперимент / Зафиксировать изменение</summary>'
        '<p>Это журнал ручных действий. Приложение не меняет настройки ZONT. '
        'Время сохранения не подтверждает время действия; пустое время означает «неизвестно».</p>'
        f'<p>Время действий: {html.escape(timezone_label(timezone))}.</p>'
        '<form data-owner-experiment><label>Состояние<select name="state">'
        '<option value="planned">План</option><option value="performed">Выполнено владельцем</option>'
        '<option value="completed">Наблюдение завершено</option><option value="stopped">Досрочно остановлено</option>'
        '<option value="cancelled">Отменено до выполнения</option>'
        '<option value="rollback_planned">Откат запланирован</option>'
        '<option value="rolled_back">Откат выполнен владельцем</option></select></label>'
        '<label>Тип изменения<select name="category"><option value="settings">Настройки</option>'
        '<option value="firmware_update">Обновление прошивки</option>'
        '<option value="firmware_rollback">Откат прошивки</option><option value="other">Другое</option></select></label>'
        '<div data-change-rows></div><button type="button" data-add-change>Добавить параметр</button>'
        '<label>Время изменения (можно оставить неизвестным)<input type="datetime-local" name="performed_at"></label>'
        '<label>Время выполненного отката (если известно)<input type="datetime-local" name="rollback_at"></label>'
        '<label>Цель<textarea name="goal" required maxlength="2000"></textarea></label>'
        '<label>Ожидаемый эффект<textarea name="expected_effect" maxlength="2000"></textarea></label>'
        '<label>Срок наблюдения<input name="observation_window" required maxlength="500"></label>'
        '<label>Комментарий<textarea name="comment" maxlength="2000"></textarea></label>'
        '<button type="submit">Сохранить эксперимент</button><button type="button" data-new-experiment>Новая запись</button>'
        '</form><p data-experiment-message role="status"></p><div data-experiment-list></div></details></section>'
    )


OWNER_EXPERIMENT_SCRIPT = r"""
(() => {
  const root = document.querySelector('[data-experiment-journal]');
  if (!root) return;
  const form = root.querySelector('form'), message = root.querySelector('[data-experiment-message]');
  const url = (document.body.dataset.feedbackApiBase || '/api').replace(/\/$/, '') + '/reports/' +
    encodeURIComponent(root.dataset.reportId) + '/experiments';
  let devices = [], current = null, pending = null;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function row(change = {}) {
    const element = document.createElement('fieldset');
    element.innerHTML = '<legend>Параметр</legend><label>Устройство<select data-change="device_id">' +
      devices.map(d => '<option value="' + escape(d.id) + '">' + escape(d.id) + '</option>').join('') +
      '</select></label><label>Контур<select data-change="circuit_id"></select></label>' +
      ['parameter','before','after'].map((k,i) => '<label>' + ['Параметр','До','После'][i] +
        '<input data-change="'+k+'" maxlength="'+(i ? 500 : 200)+'" required></label>').join('') +
      '<button type="button" data-remove>Удалить параметр</button>';
    const device = element.querySelector('[data-change="device_id"]');
    function circuits() {
      element.querySelector('[data-change="circuit_id"]').innerHTML = '<option value="">Всё устройство</option>' +
        (devices.find(d => d.id === device.value)?.circuits || []).map(c => '<option>'+escape(c)+'</option>').join('');
    }
    device.addEventListener('change', circuits);
    if (change.device_id) device.value = change.device_id;
    circuits();
    for (const input of element.querySelectorAll('[data-change]')) if (change[input.dataset.change] != null) input.value = change[input.dataset.change];
    element.querySelector('[data-remove]').onclick = () => { element.remove(); pending = null; };
    root.querySelector('[data-change-rows]').append(element);
  }
  function fresh() { current = null; pending = null; form.reset(); root.querySelector('[data-change-rows]').replaceChildren(); row(); }
  function localTime(value, zone) {
    if (!value) return '';
    const parts = new Intl.DateTimeFormat('sv-SE', {timeZone:zone, year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(value));
    return parts.replace(' ', 'T');
  }
  function render(data) {
    devices = data.devices;
    const list = root.querySelector('[data-experiment-list]'); list.replaceChildren();
    for (const item of data.experiments) {
      const card = document.createElement('article'), text = document.createElement('p');
      const labels = {planned:'План',performed:'Выполнено владельцем',completed:'Завершено',stopped:'Досрочно остановлено',cancelled:'Отменено',rollback_planned:'Откат запланирован',rolled_back:'Откат выполнен владельцем'};
      text.textContent = labels[item.state] + ' · ' + item.goal + ' · время действия: ' + (item.performed_at ? localTime(item.performed_at,data.timezone) : 'неизвестно') +
        ' · сохранено: ' + localTime(item.recorded_at,data.timezone) + ' · подтверждение телеметрией отсутствует';
      card.append(text);
      const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
      summary.textContent = 'Исходная цель, прогноз и история изменений';
      pre.textContent = 'Исходная цель: ' + item.original.goal + '\nОжидаемый эффект: ' + item.original.expected_effect +
        '\nСрок наблюдения: ' + item.original.observation_window + '\n\n' + item.history.map(event => {
          const value = event.request;
          return localTime(event.recorded_at,data.timezone) + ' · ' + labels[value.state] + '\nЦель: ' + value.goal +
            '\nКомментарий: ' + value.comment + '\n' + value.changes.map(change => change.parameter + ': ' + change.before + ' → ' + change.after).join('\n');
        }).join('\n\n');
      details.append(summary,pre); card.append(details);
      const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Изменить / завершить / откатить';
      button.onclick = () => {
        current = item; pending = null; form.reset();
        for (const input of form.elements) if (input.name && item[input.name] != null) input.value = input.type === 'datetime-local' ? localTime(item[input.name],data.timezone) : item[input.name];
        root.querySelector('[data-change-rows]').replaceChildren(); item.changes.forEach(row);
      };
      card.append(button); list.append(card);
    }
  }
  async function load() {
    try { const response = await fetch(url,{credentials:'same-origin',cache:'no-store'}); const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить журнал.'); render(data); fresh();
    } catch(error) { message.textContent = error.message; }
  }
  form.addEventListener('input', () => { pending = null; });
  root.querySelector('[data-add-change]').onclick = () => { row(); pending = null; };
  root.querySelector('[data-new-experiment]').onclick = fresh;
  form.onsubmit = async event => {
    event.preventDefault();
    message.textContent = 'Сохранение…';
    if (!pending) {
      pending = {request_id:crypto.randomUUID(),experiment_id:current?.id || null,expected_revision:current?.revision || 0,
        recommendation_id:current?.recommendation_id || null};
      for (const input of form.elements) if (input.name) pending[input.name] = input.value || (input.type === 'datetime-local' ? null : '');
      pending.changes = [...root.querySelectorAll('fieldset')].map(element => Object.fromEntries([...element.querySelectorAll('[data-change]')].map(input => [input.dataset.change,input.value || (input.dataset.change === 'circuit_id' ? null : '')])));
    }
    const button = form.querySelector('[type="submit"]'); button.disabled = true;
    try { const response = await fetch(url,{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});
      const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Не удалось сохранить.');
      render(data); fresh(); message.textContent = 'Сохранено. Исходная цель и прогноз сохранены в истории.';
    } catch(error) { message.textContent = error.message; } finally { button.disabled = false; }
  };
  let loaded = false;
  root.querySelector('details').addEventListener('toggle', async event => {
    if (event.target.open && !loaded) { loaded = true; await load(); }
  });
})();
"""

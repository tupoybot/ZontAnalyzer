"""Same-origin owner-input interactions embedded in standalone reports."""
# ruff: noqa: E501

OWNER_SCRIPT = r"""
(() => {
  const form = document.querySelector('[data-owner-forms]');
  if (!form) return;
  const initial = JSON.parse(document.querySelector('#owner-initial').textContent);
  const root = location.pathname.startsWith('/za/') ? '/za/' : '/';
  const configured = document.body.dataset.feedbackApiBase || '/api';
  const api = ['/api','/za/api'].includes(configured) ? root + 'api' : configured;
  const reportId = form.dataset.reportId;
  let deviceId = form.dataset.deviceId;
  let profiles = initial.profiles || [];
  const changed = new Set();
  const fieldNodes = [...form.querySelectorAll('[data-field]')];
  const profileMessage = form.querySelector('[data-profile-message]');
  const gasMessage = form.querySelector('[data-gas-message]');
  const message = (node, text, error=false) => {
    if (!node) return;
    node.textContent = text;
    node.className = 'owner-message ' + (error ? 'error' : 'ok');
  };
  async function request(path, payload) {
    const response = await fetch(api + path, {
      method: payload === undefined ? 'GET' : 'PUT', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
    });
    let value;
    try {
      value = await response.json();
    } catch (_error) {
      if (!response.ok) throw new Error(`Ошибка HTTP ${response.status}: сервер вернул некорректный ответ.`);
      throw new Error('Не удалось подтвердить сохранение или перезагрузить данные.');
    }
    if (!response.ok) throw new Error(value?.error || `Ошибка HTTP ${response.status}`);
    return value;
  }
  function decimalInput(input, label, minimum = null, maximum = null, strictlyPositive = false) {
    const raw = input.value.trim();
    const normalized = raw.replace(',', '.');
    const number = Number(normalized);
    let error = '';
    if (!/^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)$/.test(raw) || !Number.isFinite(number)) {
      error = `${label}: введите число; десятичный разделитель — точка или запятая.`;
    } else if (strictlyPositive && number <= 0) {
      error = `${label}: значение должно быть больше нуля.`;
    } else if ((minimum !== null && number < minimum) || (maximum !== null && number > maximum)) {
      error = `${label}: значение вне допустимого диапазона${maximum === null ? ' (не меньше ' + minimum + ')' : ' (' + minimum + '…' + maximum + ')'}.`;
    }
    input.setCustomValidity(error);
    if (error) { input.reportValidity(); input.focus(); throw new Error(error); }
    return normalized;
  }
  form.querySelectorAll('input').forEach(input => input.addEventListener('input', () => input.setCustomValidity('')));
  function applyProfile(profile) {
    for (const node of fieldNodes) {
      const item = profile?.fields?.[node.dataset.field];
      const value = item?.value;
      node.querySelectorAll('[data-season]').forEach(input => {
        input.value = value?.[input.dataset.season] ?? input.dataset.default;
      });
      node.querySelectorAll('[data-coordinate]').forEach(input => {
        input.value = value?.[input.dataset.coordinate] ?? '';
      });
      const input = node.querySelector('.owner-value');
      if (input) {
        const selected = value ?? input.dataset.default ?? '';
        if (input.tagName === 'SELECT' && (input.dataset.default || input.dataset.preserveLegacy)) {
          input.querySelectorAll('[data-legacy]').forEach(option => option.remove());
          if (![...input.options].some(option => option.value === selected)) {
            const option = new Option(selected, selected);
            option.dataset.legacy = 'true'; input.append(option);
          }
        }
        input.value = selected;
      }
      const check = node.querySelector('.owner-tristate');
      const state = node.querySelector('.owner-unknown');
      if (check) { check.checked = value === true; check.indeterminate = value == null; }
      if (state) state.value = value == null ? 'unknown' : value ? 'yes' : 'no';
      const source = node.querySelector('.owner-source');
      if (source) source.textContent = item
        ? `Источник: ${item.source === 'manual' ? 'владелец' : 'ZONT'}`
        : 'Не указано';
    }
    const coords = profile?.fields?.coordinates?.value;
    form.querySelector('[data-coordinates-summary]').textContent = coords
      ? `Широта ${coords.latitude}, долгота ${coords.longitude}` : 'Координаты недоступны';
    form.querySelector('[data-profile-history]').textContent = (profile?.history || []).map(item =>
      `${new Date(item.recorded_at).toLocaleString('ru-RU')}: ${initial.field_labels?.[item.field] || 'Поле профиля'} = ` +
      `${JSON.stringify(item.value)}; ${item.reset ? 'возврат к авто' : item.source === 'manual' ? 'владелец' : 'ZONT'}`
    ).join('\n') || 'Нет изменений.';
    form.querySelector('[data-profile-debug]').textContent = JSON.stringify(profile || {}, null, 2);
  }
  function selectProfiles(values) {
    profiles = values;
    const selector = form.querySelector('[data-device-select]');
    selector.replaceChildren();
    for (const profile of profiles) {
      const option = document.createElement('option');
      option.value = profile.device_id; option.textContent = profile.name || profile.device_id;
      selector.append(option);
    }
    if (!profiles.some(p => p.device_id === deviceId)) deviceId = profiles[0]?.device_id || '';
    selector.value = deviceId;
    selector.parentElement.hidden = profiles.length < 2;
    form.dataset.deviceId = deviceId;
    form.querySelectorAll('[data-profile-save],.owner-reset').forEach(button => button.disabled = !deviceId);
    applyProfile(profiles.find(p => p.device_id === deviceId));
  }
  form.querySelector('[data-device-select]').addEventListener('change', event => {
    deviceId = event.target.value; form.dataset.deviceId = deviceId; changed.clear();
    applyProfile(profiles.find(p => p.device_id === deviceId));
  });
  const gasDate = form.querySelector('[name=gas-date]');
  const gasValue = form.querySelector('[name=gas-value]');
  const gasReset = form.querySelector('[name=gas-reset]');
  const localToday = () => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  };
  let isLatestReport = false;
  const gasDefaultDay = () => isLatestReport ? localToday() : initial.report_day;
  let gasState = {reading: null, loading: false, sequence: 0};
  const gasControls = () => form.querySelectorAll('[data-gas-save],[data-gas-delete],[data-gas-edit],[data-gas-new]');
  function setGasLoading(value) {
    gasState.loading = value;
    gasControls().forEach(button => { button.disabled = value; });
    [gasDate, gasValue, gasReset].forEach(input => { if (input) input.disabled = value; });
  }
  function gasSummary(reading, day) {
    const summary = form.querySelector('[data-gas-current]');
    summary.textContent = reading && reading.day !== day
      ? `Переносится показание ${reading.value_m3} м³ с ${reading.day} на ${day}; сохраните изменение.`
      : reading ? `Показание за ${day}: ${reading.value_m3} м³` : `Показание за ${day} не задано`;
  }
  function renderGasHistory(readings) {
    const history = form.querySelector('[data-gas-history]');
    if (!history) return;
    history.replaceChildren();
    if (!readings?.length) { history.textContent = 'Нет сохранённых показаний.'; return; }
    const list = document.createElement('ul');
    for (const item of readings) {
      const row = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button'; button.dataset.gasReading = item.id; button.dataset.gasDay = item.day;
      button.textContent = `Редактировать ${item.day}: ${item.value_m3} м³`;
      row.append(button); list.append(row);
    }
    history.append(list);
  }
  function applyGas(data, preserveInputs = false) {
    if (!initial.daily || !data) return;
    isLatestReport = data.is_latest_report === true;
    const selectedDay = data.selected_day || gasDate.value || initial.report_day;
    if (!preserveInputs) {
      gasDate.value = selectedDay;
      gasValue.value = data.reading?.value_m3 ?? '';
      gasReset.checked = false;
      gasState.reading = data.reading || null;
    }
    gasSummary(preserveInputs ? gasState.reading : (data.reading || null), preserveInputs ? gasDate.value : selectedDay);
    form.querySelector('[data-gas-plausibility]').textContent =
      [data?.plausibility?.reason, ...(data?.plausibility?.warnings || [])].filter(Boolean).join(' ');
    renderGasHistory(data.readings || []);
    const audit = data?.audit || [];
    const history = form.querySelector('[data-gas-history]');
    if (audit.length && history) {
      const auditNote = document.createElement('p');
      auditNote.className = 'owner-help';
      auditNote.textContent = audit.map(item => `${item.created_at}: ${item.action}; ${item.before?.day ?? '—'} ${item.before?.value_m3 ?? '—'} → ${item.after?.day ?? '—'} ${item.after?.value_m3 ?? '—'} м³`).join(' | ');
      history.append(auditNote);
    }
    const boundary = data?.reading?.meter_segment;
    form.querySelector('[data-meter-boundary]').textContent = boundary && boundary !== 'default'
      ? 'Показание относится к новому участку учёта после замены/сброса. Разность через границу не рассчитывается.' : '';
  }
  async function loadGas(day, preserveInputs = false, discoverLatest = false) {
    if (!initial.daily) return;
    const sequence = ++gasState.sequence;
    setGasLoading(true);
    try {
      const data = await request('/reports/' + encodeURIComponent(reportId) + '/gas?day=' + encodeURIComponent(day));
      if (sequence !== gasState.sequence) return;
      applyGas(data, preserveInputs);
      if (discoverLatest && data.is_latest_report && day !== localToday()) {
        // The first response identifies the newest report.  Only then choose
        // browser-local today; never derive it from a UTC ISO string.
        gasDate.value = localToday(); gasValue.value = ''; gasState.reading = null;
        gasSummary(null, gasDate.value);
        loadGas(gasDate.value);
      }
    } catch (error) {
      if (sequence === gasState.sequence) message(gasMessage, error.message, true);
    } finally {
      if (sequence === gasState.sequence) setGasLoading(false);
    }
  }
  for (const node of fieldNodes) {
    node.querySelectorAll('input,select').forEach(input => input.addEventListener('change', () => {
      changed.add(node.dataset.field);
      const check = node.querySelector('.owner-tristate');
      const state = node.querySelector('.owner-unknown');
      if (check && state) {
        if (input === check) state.value = check.checked ? 'yes' : 'no';
        check.checked = state.value === 'yes'; check.indeterminate = state.value === 'unknown';
      }
    }));
  }
  function effectivePayload() {
    const day = form.querySelector('[data-effective-from]').value;
    return day ? {effective_from: day} : {};
  }
  async function saveProfile(fields, node) {
    message(profileMessage, 'Сохраняем профиль…');
    const updated = await request('/equipment/' + encodeURIComponent(deviceId), {fields, ...effectivePayload()});
    profiles = profiles.map(p => p.device_id === deviceId ? updated : p);
    applyProfile(updated); changed.clear();
    message(profileMessage, node || 'Профиль сохранён.');
    if (initial.daily) loadGas(gasDate.value, true);
  }
  form.querySelector('[data-profile-save]').addEventListener('click', async () => {
    try {
      const fields = {};
      for (const node of fieldNodes) {
        const name = node.dataset.field;
        const defaultInput = node.querySelector('.owner-value[data-default]');
        const current = profiles.find(p => p.device_id === deviceId)?.fields?.[name]?.value;
        if (!changed.has(name) && !(defaultInput && current == null)) continue;
        const coordinates = [...node.querySelectorAll('[data-coordinate]')];
        const state = node.querySelector('.owner-unknown');
        const input = node.querySelector('.owner-value');
        let value;
        const seasons = [...node.querySelectorAll('[data-season]')];
        if (seasons.length) {
          value = Object.fromEntries(seasons.map(i => [i.dataset.season, i.value]));
        } else if (coordinates.length) {
          if (coordinates.every(i => !i.value)) value = null;
          else {
            if (coordinates.some(i => !i.value)) throw new Error('Укажите обе координаты или очистите обе.');
            value = Object.fromEntries(coordinates.map(i => [i.dataset.coordinate, Number(decimalInput(
              i, i.dataset.coordinate === 'latitude' ? 'Широта' : 'Долгота',
              i.dataset.coordinate === 'latitude' ? -90 : -180, i.dataset.coordinate === 'latitude' ? 90 : 180
            ))]));
          }
        } else if (state) value = state.value === 'unknown' ? null : state.value === 'yes';
        else value = !input.value.trim() ? null : input.dataset.ownerNumber === 'true'
          ? Number(decimalInput(input, initial.field_labels[name] || name, null, null, true)) : input.value.trim();
        fields[name] = {value};
      }
      const existing = profiles.find(p => p.device_id === deviceId)?.fields || {};
      const gasMin = fields.gas_min_m3h?.value ?? (fields.gas_min_m3h ? null : existing.gas_min_m3h?.value);
      const gasMax = fields.gas_max_m3h?.value ?? (fields.gas_max_m3h ? null : existing.gas_max_m3h?.value);
      if (gasMin != null && gasMax != null && gasMin > gasMax) throw new Error('Минимальный расход газа не должен превышать максимальный.');
      await saveProfile(fields);
    } catch (error) { message(profileMessage, error.message, true); }
  });
  form.querySelectorAll('.owner-reset').forEach(button => button.addEventListener('click', async () => {
    try { await saveProfile({[button.closest('[data-field]').dataset.field]: {reset:true}}, 'Поле сброшено к авто.'); }
    catch (error) { message(profileMessage, error.message, true); }
  }));
  form.querySelector('[data-gas-save]')?.addEventListener('click', async () => {
    if (gasState.loading) return;
    try {
      if (!gasDate.value || !gasDate.reportValidity()) throw new Error('Укажите допустимую дату показания.');
      ++gasState.sequence; setGasLoading(true);
      message(gasMessage, 'Сохраняем показание…');
      const saved = await request('/reports/' + encodeURIComponent(reportId) + '/gas', {
        value_m3: decimalInput(gasValue, 'Показание газа', 0), reset: gasReset.checked,
        day: gasDate.value, reading_id: gasState.reading?.id ?? null,
      });
      applyGas(saved);
      form.querySelector('#gas-editor')?.removeAttribute('open');
      form.querySelector('[data-gas-edit]')?.setAttribute('aria-expanded', 'false');
      message(gasMessage, 'Показание сохранено. Отчёты обновятся при ближайшем фоновом обновлении.');
    } catch (error) { message(gasMessage, error.message, true); }
    finally { setGasLoading(false); }
  });
  form.querySelector('[data-gas-delete]')?.addEventListener('click', async () => {
    if (gasState.loading) return;
    try {
      if (!gasState.reading?.id) throw new Error('Нет сохранённого показания для удаления.');
      ++gasState.sequence; setGasLoading(true);
      message(gasMessage, 'Удаляем показание…');
      // A changed date is a pending move.  Deletion must still target the row
      // that was selected before editing, never an unrelated date.
      applyGas(await request('/reports/' + encodeURIComponent(reportId) + '/gas', {
        delete:true, day: gasState.reading.day, reading_id: gasState.reading.id,
      }));
      form.querySelector('#gas-editor')?.removeAttribute('open');
      form.querySelector('[data-gas-edit]')?.setAttribute('aria-expanded', 'false');
      message(gasMessage, 'Показание удалено. Отчёты обновятся при ближайшем фоновом обновлении.');
    } catch (error) { message(gasMessage, error.message, true); }
    finally { setGasLoading(false); }
  });
  form.querySelector('[data-gas-edit]')?.addEventListener('click', () => {
    const editor = form.querySelector('#gas-editor');
    if (!editor) return;
    editor.open = !editor.open;
    form.querySelector('[data-gas-edit]').setAttribute('aria-expanded', String(editor.open));
    if (editor.open) form.querySelector('[name=gas-value]')?.focus();
  });
  form.querySelector('[data-gas-new]')?.addEventListener('click', () => {
    gasState.reading = null;
    gasDate.value = gasDefaultDay(); gasValue.value = ''; gasReset.checked = false;
    gasSummary(null, gasDate.value);
    const editor = form.querySelector('#gas-editor');
    if (editor) editor.open = true;
    form.querySelector('[data-gas-edit]')?.setAttribute('aria-expanded', 'true');
    gasValue.focus();
  });
  form.querySelector('[data-gas-history]')?.addEventListener('click', event => {
    const button = event.target.closest('[data-gas-reading]');
    if (!button || gasState.loading) return;
    // The selected id is resolved from the server so its current value and
    // audit trail cannot be confused with a stale history rendering.
    if (button.dataset.gasDay) {
      const editor = form.querySelector('#gas-editor');
      if (editor) editor.open = true;
      form.querySelector('[data-gas-edit]')?.setAttribute('aria-expanded', 'true');
      loadGas(button.dataset.gasDay);
    }
  });
  if (gasDate) {
    gasDate.max = localToday();
    gasDate.addEventListener('change', () => {
      // Changing a date is an intentional move/create target.  Do not fetch
      // and silently replace the value or id the owner is editing.
      gasSummary(gasState.reading, gasDate.value);
    });
  }
  selectProfiles(profiles);
  const offline = window.location.protocol === "file:";
  if (initial.daily && offline) {
    applyGas({...initial.gas, selected_day: initial.report_day});
  } else if (initial.daily) {
    // Rendered data belongs to the report day and must not briefly impersonate
    // a new reading for today's browser date on the newest report.
    gasDate.value = initial.report_day; gasValue.value = ''; gasState.reading = null;
    gasSummary(null, initial.report_day);
    loadGas(initial.report_day, false, true);
  }
  const tariffMessage = form.querySelector('[data-tariff-message]');
  let tariffItems = [];
  const tariffMonth = form.querySelector('[data-tariff-month]');
  if (tariffMonth) {
    const parts = new Intl.DateTimeFormat('en', {timeZone:initial.timezone || 'UTC', year:'numeric',month:'numeric'}).formatToParts(new Date());
    const year = Number(parts.find(p => p.type === 'year').value);
    const month = Number(parts.find(p => p.type === 'month').value);
    tariffMonth.value = `${year + (month === 12 ? 1 : 0)}-${String(month % 12 + 1).padStart(2, '0')}`;
  }
  const monthLabel = item => item.effective_month || new Intl.DateTimeFormat('sv-SE', {
    timeZone: initial.timezone || 'UTC', year:'numeric', month:'2-digit'
  }).format(new Date(item.effective_from));
  function applyTariffs(items) {
    const history = form.querySelector('[data-tariff-history]');
    const selector = form.querySelector('[data-tariff-correction]');
    if (!history || !selector) return;
    tariffItems = items;
    history.replaceChildren(); selector.replaceChildren(new Option('Выберите тариф', ''));
    const now = Date.now();
    const current = items.filter(item => Date.parse(item.effective_from) <= now).at(-1);
    const planned = items.filter(item => Date.parse(item.effective_from) > now);
    const label = item => `${monthLabel(item)}: ${item.price.replace('.', ',')} ${item.currency}/м³`;
    form.querySelector('[data-tariff-current]').textContent = current ? `Цена за м³ — ${current.price.replace('.', ',')} ${current.currency} (с ${monthLabel(current)})` : 'Цена за м³ не задана';
    form.querySelector('[data-tariff-planned]').textContent = planned.map(item => `С ${label(item)}`).join('; ');
    const selected = planned.find(item => monthLabel(item) === form.querySelector('[data-tariff-month]').value) || current;
    if (selected) {
      form.querySelector('[data-tariff-price]').value = selected.price.replace('.', ',');
      form.querySelector('[data-tariff-currency]').value = selected.currency;
    }
    for (const item of items) {
      const row = document.createElement('p'); row.textContent = label(item); history.append(row);
      selector.append(new Option(label(item), item.id));
      for (const correction of item.corrections || []) {
        const audit = document.createElement('p');
        audit.className = 'owner-help';
        const before = correction.before;
        const after = correction.after;
        audit.textContent = `${before?.price ?? '—'} ${before?.currency ?? ''} → ${after?.price ?? '—'} ${after?.currency ?? ''}; ${correction.correction_reason || correction.reason || 'Изменение цены'}; ${correction.recorded_at || correction.created_at || ''}`;
        history.append(audit);
      }
    }
    if (!items.length) history.textContent = 'История пуста.';
  }
  async function saveTariff(correct) {
    const buttons = [...form.querySelectorAll('[data-tariff-save],[data-tariff-correct]')];
    buttons.forEach(button => button.disabled = true);
    try {
      const payload = {action: correct ? 'correct' : 'create',
        price: decimalInput(form.querySelector('[data-tariff-price]'), 'Цена за м³', 0),
        currency: form.querySelector('[data-tariff-currency]').value};
      if (correct) {
        payload.id = form.querySelector('[data-tariff-correction]').value;
        payload.correction_reason = form.querySelector('[data-tariff-reason]').value;
        if (!payload.id) throw new Error('Выберите тариф для исправления.');
      } else {
        payload.effective_month = form.querySelector('[data-tariff-month]').value;
        if (!payload.effective_month) throw new Error('Укажите месяц начала действия.');
      }
      message(tariffMessage, 'Сохраняем тариф и обновляем стоимость…');
      const saved = await request('/gas-tariffs', payload);
      applyTariffs(saved.history || []);
      message(tariffMessage, ['Тариф сохранён.', saved.publish_warning || 'Обновите страницу, чтобы увидеть стоимость.'].join(' '));
    } catch (error) { message(tariffMessage, error.message, true); }
    finally { buttons.forEach(button => button.disabled = false); }
  }
  form.querySelector('[data-tariff-save]')?.addEventListener('click', () => saveTariff(false));
  form.querySelector('[data-tariff-correct]')?.addEventListener('click', () => saveTariff(true));
  form.querySelector('[data-tariff-correction]')?.addEventListener('change', event => {
    const item = tariffItems.find(item => item.id === event.target.value);
    if (item) {
      form.querySelector('[data-tariff-price]').value = item.price.replace('.', ',');
      form.querySelector('[data-tariff-currency]').value = item.currency;
    }
  });
  form.querySelector('[data-tariff-edit]')?.addEventListener('click', () => {
    const editor = form.querySelector('#tariff-editor');
    editor.open = !editor.open;
    form.querySelector('[data-tariff-edit]').setAttribute('aria-expanded', String(editor.open));
    if (editor.open) form.querySelector('[data-tariff-price]').focus();
  });
  applyTariffs(initial.tariffs || []);
  if (offline) return;
  if (initial.daily) request('/gas-tariffs').then(data => applyTariffs(data.history || []))
    .catch(error => message(tariffMessage, error.message, true));
  request('/equipment').then(data => selectProfiles(data.profiles || []))
    .catch(error => message(profileMessage, error.message, true));
})();
"""

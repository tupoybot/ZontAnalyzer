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
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || `Ошибка HTTP ${response.status}`);
    return value;
  }
  function applyProfile(profile) {
    for (const node of fieldNodes) {
      const item = profile?.fields?.[node.dataset.field];
      const value = item?.value;
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
        ? `Источник: ${item.source === 'manual' ? 'владелец' : 'ZONT'}; ${item.provenance || ''}; действует с ${item.effective_from}`
        : 'Не указано';
    }
    const coords = profile?.fields?.coordinates?.value;
    form.querySelector('[data-coordinates-summary]').textContent = coords
      ? `Широта ${coords.latitude}, долгота ${coords.longitude}` : 'Координаты недоступны';
    form.querySelector('[data-profile-history]').textContent = (profile?.history || []).map(item =>
      `${item.recorded_at}: ${item.field} = ${JSON.stringify(item.value)}; ${item.reset ? 'возврат к авто' : item.source}; ` +
      `${item.provenance || ''}; действует с ${item.effective_from}`
    ).join('\n') || 'Нет изменений.';
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
  function applyGas(data) {
    if (!initial.daily) return;
    form.querySelector('[name=gas-value]').value = data?.reading?.value_m3 ?? '';
    form.querySelector('[name=gas-reset]').checked = false;
    form.querySelector('[data-gas-plausibility]').textContent =
      [data?.plausibility?.reason, ...(data?.plausibility?.warnings || [])].filter(Boolean).join(' ');
    form.querySelector('[data-gas-history]').textContent = (data?.audit || []).map(item =>
      `${item.created_at}: ${item.action}; ${item.before?.value_m3 ?? '—'} → ${item.after?.value_m3 ?? '—'} м³`
    ).join('\n') || 'Нет изменений.';
    const boundary = data?.reading?.meter_segment;
    form.querySelector('[data-meter-boundary]').textContent = boundary && boundary !== 'default'
      ? 'Показание относится к новому участку учёта после замены/сброса. Разность через границу не рассчитывается.' : '';
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
    const updated = await request('/equipment/' + encodeURIComponent(deviceId), {fields, ...effectivePayload()});
    profiles = profiles.map(p => p.device_id === deviceId ? updated : p);
    applyProfile(updated); changed.clear();
    message(profileMessage, node || 'Профиль сохранён.');
  }
  form.querySelector('[data-profile-save]').addEventListener('click', async () => {
    try {
      const fields = {};
      for (const node of fieldNodes) {
        const name = node.dataset.field;
        const defaultInput = node.querySelector('[data-default]');
        const current = profiles.find(p => p.device_id === deviceId)?.fields?.[name]?.value;
        if (!changed.has(name) && !(defaultInput && current == null)) continue;
        const coordinates = [...node.querySelectorAll('[data-coordinate]')];
        const state = node.querySelector('.owner-unknown');
        const input = node.querySelector('.owner-value');
        let value;
        if (coordinates.length) {
          if (coordinates.every(i => !i.value)) value = null;
          else {
            if (coordinates.some(i => !i.value)) throw new Error('Укажите обе координаты или очистите обе.');
            value = Object.fromEntries(coordinates.map(i => [i.dataset.coordinate, Number(i.value)]));
          }
        } else if (state) value = state.value === 'unknown' ? null : state.value === 'yes';
        else value = !input.value.trim() ? null : input.type === 'number' ? Number(input.value) : input.value.trim();
        fields[name] = {value};
      }
      await saveProfile(fields);
    } catch (error) { message(profileMessage, error.message, true); }
  });
  form.querySelectorAll('.owner-reset').forEach(button => button.addEventListener('click', async () => {
    try { await saveProfile({[button.closest('[data-field]').dataset.field]: {reset:true}}, 'Поле сброшено к авто.'); }
    catch (error) { message(profileMessage, error.message, true); }
  }));
  form.querySelector('[data-gas-save]')?.addEventListener('click', async () => {
    const input = form.querySelector('[name=gas-value]');
    if (!input.value || !input.checkValidity()) { message(gasMessage, 'Введите неотрицательное показание.', true); return; }
    try {
      const saved = await request('/reports/' + encodeURIComponent(reportId) + '/gas', {
        value_m3: input.value, reset: form.querySelector('[name=gas-reset]').checked,
      });
      applyGas(saved);
      message(gasMessage, ['Показание сохранено.', saved.publish_warning].filter(Boolean).join(' '));
    } catch (error) { message(gasMessage, error.message, true); }
  });
  form.querySelector('[data-gas-delete]')?.addEventListener('click', async () => {
    try {
      applyGas(await request('/reports/' + encodeURIComponent(reportId) + '/gas', {delete:true}));
      message(gasMessage, 'Показание удалено.');
    } catch (error) { message(gasMessage, error.message, true); }
  });
  selectProfiles(profiles); applyGas(initial.gas);
  request('/equipment').then(data => selectProfiles(data.profiles || []))
    .catch(error => message(profileMessage, error.message, true));
  if (initial.daily) request('/reports/' + encodeURIComponent(reportId) + '/gas').then(applyGas)
    .catch(error => message(gasMessage, error.message, true));
})();
"""

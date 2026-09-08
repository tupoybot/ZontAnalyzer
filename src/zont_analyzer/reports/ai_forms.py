"""Protected AI settings and model review controls for owner reports."""
# ruff: noqa: E501
from __future__ import annotations

import html
from typing import Any


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_ai_forms(owner_data: dict[str, Any] | None = None) -> str:
    """Render the lazy, accessible AI settings subsection.

    The endpoint is deliberately read only until the owner explicitly opens this
    subsection. Dynamic API values are written with DOM text APIs in the embedded
    script so account supplied text cannot become markup.
    """
    owner_data = owner_data if isinstance(owner_data, dict) else {}
    review_hint = owner_data.get("ai_review")
    hint_status = ""
    if isinstance(review_hint, dict):
        hint_status = str(review_hint.get("status") or review_hint.get("state") or "")
    hint = _text(hint_status)
    return f'''<details id="ai-settings" class="owner-form owner-ai" data-ai-settings data-ai-hint="{hint}">
<summary>AI <span class="owner-ai-summary-status" data-ai-summary-status></span></summary>
<div data-ai-panel>
<p class="owner-help">Настройки применяются только к новым AI-анализам. Каталог модели не гарантирует доступность в вашем аккаунте: доступ и совместимость проверяются при фактическом запуске.</p>
<p class="owner-help">Открытие этого раздела не запускает генерацию и не обращается к официальным страницам моделей.</p>
<p class="owner-message" data-ai-message role="status" aria-live="polite">Откройте раздел, чтобы загрузить актуальные настройки.</p>
<div data-ai-content hidden>
<div class="owner-fields">
<div class="owner-field"><label class="owner-inline"><input data-ai-enabled type="checkbox"> Включать AI-анализ</label></div>
<div class="owner-field"><label>Модель дневного анализа<select data-ai-daily-model></select></label><label>Глубина рассуждения<select data-ai-daily-effort></select></label></div>
<div class="owner-field"><label>Модель обзоров<select data-ai-review-model></select></label><label>Глубина рассуждения<select data-ai-review-effort></select></label></div>
<div class="owner-field"><label class="owner-inline"><input data-ai-review-enabled type="checkbox"> Периодический пересмотр моделей</label><label>Интервал, дней<input data-ai-review-interval type="number" min="1" max="365" inputmode="numeric"></label><p class="owner-help">По умолчанию — 60 дней. Можно запустить проверку вручную.</p></div>
</div>
<p class="owner-help" data-ai-overridden></p>
<div class="owner-actions"><button type="button" data-ai-save>Сохранить AI-настройки</button><button type="button" class="owner-secondary" data-ai-reset>Сбросить переопределение YAML</button></div>
<details><summary>Аудит изменений</summary><div data-ai-history></div></details>
<section class="owner-ai-review" data-ai-review-section aria-label="Пересмотр моделей">
<h3>Пересмотр моделей</h3><p data-ai-review-links></p><p data-ai-review-status>Проверка ещё не загружена.</p><p data-ai-review-schedule></p><p data-ai-review-latest></p>
<div class="owner-actions"><button type="button" data-ai-check>Проверить сейчас</button></div>
<div data-ai-review-proposal hidden><label>Предложение<select data-ai-proposal-select></select></label><div data-ai-review-list></div><p data-ai-review-summary></p><p data-ai-review-reason></p><p data-ai-review-tradeoff></p><p data-ai-review-evidence></p>
<div class="owner-actions"><button type="button" data-ai-accept>Принять</button><button type="button" data-ai-reject>Отклонить</button><button type="button" data-ai-defer>Отложить</button></div></div>
</section>
</div></div>
</details>
<style>
.owner-ai .owner-fields{{margin-top:.7rem}}.owner-ai h3{{margin-bottom:.35rem}}.owner-ai-review{{margin-top:1rem;padding:.7rem;border:1px solid #dfe5eb;border-radius:.45rem;background:#fff}}.owner-ai-review [data-ai-review-links] a{{overflow-wrap:anywhere}}.owner-ai-review button[disabled]{{opacity:.55;cursor:wait}}
</style>
<script>
(() => {{
  const root = document.querySelector('[data-ai-settings]');
  if (!root || root.dataset.aiBound === 'true') return;
  root.dataset.aiBound = 'true';
  const content = root.querySelector('[data-ai-content]');
  const message = root.querySelector('[data-ai-message]');
  const enabled = root.querySelector('[data-ai-enabled]');
  const dailyModel = root.querySelector('[data-ai-daily-model]');
  const reviewModel = root.querySelector('[data-ai-review-model]');
  const dailyEffort = root.querySelector('[data-ai-daily-effort]');
  const reviewEffort = root.querySelector('[data-ai-review-effort]');
  const reviewEnabled = root.querySelector('[data-ai-review-enabled]');
  const interval = root.querySelector('[data-ai-review-interval]');
  const overridden = root.querySelector('[data-ai-overridden]');
  const history = root.querySelector('[data-ai-history]');
  const reviewStatus = root.querySelector('[data-ai-review-status]');
  const summaryStatus = root.querySelector('[data-ai-summary-status]');
  const proposalBox = root.querySelector('[data-ai-review-proposal]');
  const proposalSelect = root.querySelector('[data-ai-proposal-select]');
  let state = null;
  let initialEffective = null;
  summaryStatus.textContent = root.dataset.aiHint ? ' · ' + (root.dataset.aiHint === 'no_change' ? 'проверено' : root.dataset.aiHint) : '';
  let loaded = false;
  let busy = false;

  const archiveRoot = window.location.pathname.startsWith('/za/') ? '/za/' : '/';
  const configuredApi = document.body.dataset.feedbackApiBase || '/api';
  const apiBase = ['/api', '/za/api'].includes(configuredApi) ? archiveRoot + 'api' : configuredApi;
  const api = (path, options) => fetch(apiBase + path, {{ credentials: 'same-origin', headers: {{'Accept': 'application/json', ...(options && options.body ? {{'Content-Type': 'application/json'}} : {{}})}}, ...options }});
  const setMessage = (value, error = false) => {{ message.textContent = value || ''; message.classList.toggle('error', error); message.classList.toggle('ok', !error && Boolean(value)); }};
  const text = (node, value) => {{ if (node) node.textContent = value == null ? '' : String(value); }};
  const json = async response => {{ const data = await response.json().catch(() => ({{}})); if (!response.ok) throw new Error(data.detail || data.error || ('HTTP ' + response.status)); return data; }};
  const options = (select, values, selected) => {{ select.replaceChildren(); (Array.isArray(values) ? values : []).forEach(value => {{ const item = document.createElement('option'); item.value = String(value); item.textContent = String(value); select.append(item); }}); if (selected != null) select.value = String(selected); }};
  const modelOptions = (select, models, selected) => {{ select.replaceChildren(); (Array.isArray(models) ? models : []).forEach(model => {{ if (!model || typeof model.id !== 'string') return; const item = document.createElement('option'); item.value = model.id; item.textContent = model.id; select.append(item); }}); if (selected != null) select.value = String(selected); }};
  const model = select => (Array.isArray(state && state.models) ? state.models : []).find(item => item && item.id === select.value) || {{}};
  const updateEfforts = (select, effortSelect, selected) => options(effortSelect, model(select).efforts, selected);
  const showHistory = items => {{ history.replaceChildren(); (Array.isArray(items) ? items : []).forEach(item => {{ const row = document.createElement('p'); row.textContent = [item.at || item.created_at, item.action || item.event, item.summary || item.reason || JSON.stringify(item.values || {{}})].filter(Boolean).join(' — '); history.append(row); }}); if (!history.childNodes.length) text(history, 'Изменений ещё нет.'); }};
  const linkList = links => {{ const box = root.querySelector('[data-ai-review-links]'); box.replaceChildren(); (Array.isArray(links) ? links : []).forEach(value => {{ const raw = String(value.url || value.href || value); let url; try {{ url = new URL(raw, window.location.href); }} catch (_) {{ return; }} if (!['http:', 'https:'].includes(url.protocol)) return; const a = document.createElement('a'); a.href = url.href; a.textContent = String(value.title || value.label || raw); a.target = '_blank'; a.rel = 'noopener noreferrer'; box.append(a, document.createTextNode(' ')); }}); }};
  const reviewText = value => {{ if (!value || typeof value !== 'object') return value || ''; const price = value.price || {{}}; const current = price.current || {{}}; const candidate = price.candidate || {{}}; return [value.quality && 'Качество: ' + value.quality, value.latency && 'Задержка: ' + value.latency, (current.input_per_mtok_usd != null || candidate.input_per_mtok_usd != null) && 'Входные токены, USD/млн: ' + (current.input_per_mtok_usd ?? 'неизвестно') + ' → ' + (candidate.input_per_mtok_usd ?? 'неизвестно'), (current.output_per_mtok_usd != null || candidate.output_per_mtok_usd != null) && 'Выходные токены, USD/млн: ' + (current.output_per_mtok_usd ?? 'неизвестно') + ' → ' + (candidate.output_per_mtok_usd ?? 'неизвестно')].filter(Boolean).join('; '); }};
  const renderReview = review => {{ review = review && typeof review === 'object' ? review : {{}}; const proposals = Array.isArray(review.proposals) ? review.proposals : []; const latestRun = Array.isArray(review.runs) && review.runs[0];
    const result = latestRun && latestRun.result || {{}};
    const messages = {{no_change:'Оснований для замены не найдено.', unverified:'Не удалось проверить модели.', proposal:'Есть предложение по моделям.', interrupted:'Проверка прервана.', running:'Проверка выполняется…'}};
    text(root.querySelector('[data-ai-review-schedule]'), review.next_due_at ? 'Следующая проверка: ' + new Date(review.next_due_at).toLocaleString('ru-RU') : 'Первая проверка ещё не выполнена.');
    const notices = [(review.last_success_at ? 'Последняя успешная проверка: ' + new Date(review.last_success_at).toLocaleString('ru-RU') : ''), review.last_error || '', result.message || '', review.settings_changed ? 'Последняя проверка относится к прежним настройкам.' : ''];
    (result.deprecations || []).forEach(item => notices.push('Прекращение поддержки ' + item.model + ': ' + (item.shutdown_date || 'дата не указана')));
    text(root.querySelector('[data-ai-review-latest]'), notices.filter(Boolean).join(' '));
    text(summaryStatus, review.running ? ' · выполняется' : proposals.some(item => item.status === 'open') ? ' · есть предложение' : '');
     const selected = proposalBox.dataset.selectedProposal; proposalSelect.replaceChildren(); proposals.forEach(item => {{ const o = new Option([item.profile, item.current_model, item.candidate_model].filter(Boolean).join(' — '), String(item.id)); proposalSelect.append(o); }}); if (selected) proposalSelect.value = selected; const proposal = proposals.find(item => String(item.id) === proposalSelect.value) || proposals[0]; const recommendation = proposal && proposal.recommendation || {{}}; const proposalId = proposal && proposal.id; const hasProposal = Boolean(proposalId && proposal.candidate_model); const status = review.running ? 'Проверка выполняется' : review.last_error ? 'Ошибка проверки' : (latestRun && latestRun.status === 'no_change' ? 'Изменение модели не обосновано' : 'Проверка завершена'); text(reviewStatus, review.running ? messages.running : messages[latestRun && latestRun.status] || status); text(summaryStatus, review.running ? ' · выполняется' : hasProposal ? ' · есть предложение' : ' · проверено'); text(root.querySelector('[data-ai-review-schedule]'), review.next_due_at ? 'Следующая проверка: ' + review.next_due_at : ''); text(root.querySelector('[data-ai-review-latest]'), review.last_success_at ? 'Последняя успешная проверка: ' + review.last_success_at : review.last_error || (latestRun && latestRun.result && latestRun.result.message) || ''); const list = root.querySelector('[data-ai-review-list]'); list.replaceChildren(); proposals.forEach(item => {{ const row = document.createElement('p'); row.textContent = [item.profile, item.current_model, item.candidate_model].filter(Boolean).join(' — '); list.append(row); }}); proposalBox.hidden = !hasProposal; if (!hasProposal) {{ linkList((latestRun && latestRun.sources) || (latestRun && latestRun.result && latestRun.result.sources) || []); return; }} proposalBox.dataset.proposalId = String(proposalId); proposalBox.dataset.proposalVersion = String(Number(proposal.version)); root.querySelector('[data-ai-accept]').hidden = !proposal.candidate_model || recommendation.can_apply === false; text(root.querySelector('[data-ai-review-summary]'), proposal.current_model + (proposal.candidate_model ? ' → ' + proposal.candidate_model : ': замена пока не подтверждена'));  text(root.querySelector('[data-ai-review-reason]'), ({{current_model_deprecated:'Подтверждено прекращение поддержки текущей модели.', published_candidate_requires_evaluation:'Есть совместимый кандидат с меньшей опубликованной ценой или подтверждённой локальной оценкой.'}})[recommendation.reason] || 'Требуется решение владельца.'); text(root.querySelector('[data-ai-review-tradeoff]'), reviewText(recommendation.tradeoffs)); text(root.querySelector('[data-ai-review-evidence]'), recommendation.requires_evaluation ? 'Перед применением нужна локальная оценка.' : ''); linkList(recommendation.sources || []); }};
  proposalSelect.addEventListener('change', () => {{ proposalBox.dataset.selectedProposal = proposalSelect.value; state && renderReview(state.review); }});
  const render = data => {{ state = data || {{}}; const effective = state.effective || {{}}; initialEffective = {{...effective}}; enabled.checked = Boolean(effective.enabled); modelOptions(dailyModel, state.models, effective.daily_model); modelOptions(reviewModel, state.models, effective.review_model); if (!dailyModel.value && effective.daily_model != null) {{ const item = new Option(String(effective.daily_model), String(effective.daily_model)); dailyModel.append(item); dailyModel.value = String(effective.daily_model); }} if (!reviewModel.value && effective.review_model != null) {{ const item = new Option(String(effective.review_model), String(effective.review_model)); reviewModel.append(item); reviewModel.value = String(effective.review_model); }} updateEfforts(dailyModel, dailyEffort, effective.daily_reasoning_effort); updateEfforts(reviewModel, reviewEffort, effective.review_reasoning_effort); if (!dailyEffort.value && effective.daily_reasoning_effort != null) dailyEffort.append(new Option(String(effective.daily_reasoning_effort), String(effective.daily_reasoning_effort))); if (!reviewEffort.value && effective.review_reasoning_effort != null) reviewEffort.append(new Option(String(effective.review_reasoning_effort), String(effective.review_reasoning_effort))); dailyEffort.value = String(effective.daily_reasoning_effort || dailyEffort.value); reviewEffort.value = String(effective.review_reasoning_effort || reviewEffort.value); reviewEnabled.checked = Boolean(effective.review_enabled); interval.value = effective.review_interval_days == null ? 60 : effective.review_interval_days; text(overridden, state.overridden ? 'Сохранено веб-переопределение YAML.' : 'Действуют значения YAML/defaults.'); showHistory(state.history); renderReview(state.review); content.hidden = false; }};
  const load = async () => {{ if (busy) return; busy = true; setMessage('Загрузка AI-настроек…'); try {{ render(await json(await api('/ai'))); loaded = true; setMessage('AI-настройки загружены.', false); }} catch (error) {{ setMessage('Не удалось загрузить AI-настройки: ' + error.message, true); }} finally {{ busy = false; }} }};
  root.addEventListener('toggle', () => {{ if (root.open) load(); }});
  dailyModel.addEventListener('change', () => updateEfforts(dailyModel, dailyEffort));
  reviewModel.addEventListener('change', () => updateEfforts(reviewModel, reviewEffort));
  root.querySelector('[data-ai-save]').addEventListener('click', async () => {{ if (!state) return; try {{ const candidate = {{enabled: enabled.checked, daily_model: dailyModel.value, review_model: reviewModel.value, daily_reasoning_effort: dailyEffort.value, review_reasoning_effort: reviewEffort.value, review_enabled: reviewEnabled.checked, review_interval_days: Number(interval.value)}}; const values = Object.fromEntries(Object.entries(candidate).filter(([key, value]) => String(value) !== String(initialEffective && initialEffective[key]))); if (!Object.keys(values).length) {{ setMessage('Изменений нет.'); return; }} render(await json(await api('/ai', {{method: 'PUT', body: JSON.stringify({{expected_version: state.version, values}})}}))); setMessage('AI-настройки сохранены.'); }} catch (error) {{ setMessage('Не удалось сохранить: ' + error.message, true); }} }});
  root.querySelector('[data-ai-reset]').addEventListener('click', async () => {{ if (!state) return; try {{ render(await json(await api('/ai', {{method: 'PUT', body: JSON.stringify({{expected_version: state.version, reset: true}})}}))); setMessage('Веб-переопределение сброшено к YAML.'); }} catch (error) {{ setMessage('Не удалось сбросить настройки: ' + error.message, true); }} }});
  const reviewAction = async action => {{ if (!state || busy) return; const proposalId = proposalBox.dataset.proposalId || ''; if (!proposalId && action !== 'check') return; const buttons = [...root.querySelectorAll('[data-ai-check],[data-ai-accept],[data-ai-reject],[data-ai-defer]')]; busy = true; buttons.forEach(button => button.disabled = true); try {{ const http = await api('/ai/review', {{method: 'PUT', body: JSON.stringify({{action, proposal_id: proposalId, expected_version: Number(proposalBox.dataset.proposalVersion)}})}}); const response = await json(http); if (http.status === 202) {{ text(reviewStatus, 'Проверка выполняется…'); let finished = false; for (let attempt = 0; attempt < 8; attempt++) {{ await new Promise(resolve => setTimeout(resolve, 1000)); const next = await json(await api('/ai')); const review = next.review || {{}}; if (!review.running) {{ render(next); finished = true; break; }} }} if (!finished) text(reviewStatus, 'Проверка всё ещё выполняется. Откройте раздел позже, чтобы увидеть результат.'); }} else render(response); }} catch (error) {{ setMessage('Не удалось выполнить действие: ' + error.message, true); }} finally {{ busy = false; buttons.forEach(button => button.disabled = false); }} }};
  root.querySelector('[data-ai-check]').addEventListener('click', () => reviewAction('check'));
  root.querySelector('[data-ai-accept]').addEventListener('click', () => reviewAction('accept'));
  root.querySelector('[data-ai-reject]').addEventListener('click', () => reviewAction('reject'));
  root.querySelector('[data-ai-defer]').addEventListener('click', () => reviewAction('defer'));
}})();
</script>'''

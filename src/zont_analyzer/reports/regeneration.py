"""Small standalone regeneration control embedded in report HTML."""
# The embedded JavaScript is intentionally kept readable as a single snippet.
# ruff: noqa: E501
from __future__ import annotations

import json
from html import escape


def render_regeneration(report: object, feedback_api_base_url: str = "/api") -> str:
    if getattr(report, "kind", "daily") == "initial":
        return ""
    report_id = str(getattr(report, "id", report))
    rid = escape(report_id, quote=True)
    api = json.dumps(feedback_api_base_url.rstrip("/"), ensure_ascii=False).replace("<", "\\u003c")
    js_id = json.dumps(report_id, ensure_ascii=False).replace("<", "\\u003c")
    return f'''<section class="full-width report-regeneration" data-report-id="{rid}">
  <label for="counterfactual-question">Вопрос о небольшом изменении ПЗА (необязательно)</label>
  <textarea id="counterfactual-question" class="counterfactual-question" maxlength="500" rows="2"
    placeholder="Например: что будет при небольшом изменении ПЗА?" aria-label="Вопрос о небольшом изменении ПЗА"></textarea>
  <button type="button" class="regenerate-report">Перегенерировать отчёт</button>
  <span class="regeneration-status" role="status" aria-live="polite"></span>
</section>
<script>(function() {{
  const root = document.currentScript.previousElementSibling;
  const button = root.querySelector('.regenerate-report');
  const question = root.querySelector('.counterfactual-question');
  const status = root.querySelector('.regeneration-status');
  const id = {js_id};
  const configured = {api};
  const prefix = location.pathname.startsWith('/za/') ? '/za/' : '/';
  const api = ['/api', '/za/api'].includes(configured) ? prefix + 'api' : configured;
  const url = api + '/reports/' + encodeURIComponent(id) + '/regenerate';
  let requested = false;
  const show = (s, error) => {{ status.textContent = s === 'queued' ? 'В очереди…' : s === 'running' ? 'Выполняется…' : s === 'success' ? 'Готово' : s === 'error' ? ('Ошибка пересчёта' + (error ? ': ' + error : '')) : ''; }};
  const poll = async () => {{ try {{ const r = await fetch(url, {{credentials:'same-origin'}}); const v = await r.json(); if (!r.ok) throw new Error(v.error || 'HTTP ' + r.status); show(v.status, v.error); if (v.status === 'queued' || v.status === 'running') setTimeout(poll, 1000); else {{ button.disabled = false; if (requested && v.status === 'success') window.location.reload(); }} }} catch (error) {{ show('error', error.message); button.disabled = false; }} }};
  button.addEventListener('click', async () => {{ requested = true; button.disabled = true; show('queued'); try {{ const text = question.value.trim(); const body = text ? JSON.stringify({{question:text}}) : '{{}}'; const r = await fetch(url, {{method:'POST', credentials:'same-origin', body:body, headers:{{'Content-Type':'application/json'}}}}); const v = await r.json(); if (!r.ok) throw new Error(v.error || 'HTTP ' + r.status); show(v.status, v.error); }} catch (error) {{ show('error', error.message); button.disabled = false; return; }} poll(); }});
  poll();
}})();</script>'''


def regeneration_control(report_id: str, *, api_base_url: str = "/api") -> str:
    """Compatibility wrapper for callers that only have an id."""
    return render_regeneration(report_id, api_base_url)

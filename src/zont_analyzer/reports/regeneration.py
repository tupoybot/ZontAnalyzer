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
  <details data-regeneration-status>
    <summary>Перегенерировать отчёт</summary>
    <div class="regeneration-fields">
      <label for="counterfactual-question">Вопрос для AI</label>
      <textarea id="counterfactual-question" class="counterfactual-question" maxlength="500" rows="2"
        placeholder="Необязательно. Например: что изменится, если снизить уставку?" aria-label="Вопрос для AI"></textarea>
      <button type="button" class="regenerate-report">Перегенерировать отчёт</button>
      <span class="regeneration-status" role="status" aria-live="polite"></span>
    </div>
  </details>
</section>
<style>
.report-regeneration>details{{grid-column:1/-1}}
.regeneration-fields{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 14px;align-items:end}}
.regeneration-fields label{{grid-column:1/-1;font-weight:650}}
.regeneration-fields textarea{{box-sizing:border-box;min-width:0;width:100%;resize:vertical}}
@media(max-width:560px){{.regeneration-fields{{grid-template-columns:1fr}}.regeneration-fields .regenerate-report{{width:100%}}}}
</style>
<script>(function() {{
  const root = document.currentScript.parentElement.querySelector('.report-regeneration');
  const panel = root.querySelector('[data-regeneration-status]');
  const button = root.querySelector('.regenerate-report');
  const question = root.querySelector('.counterfactual-question');
  const status = root.querySelector('.regeneration-status');
  const id = {js_id};
  const configured = {api};
  const prefix = location.pathname.startsWith('/za/') ? '/za/' : '/';
  const api = ['/api', '/za/api'].includes(configured) ? prefix + 'api' : configured;
  const url = api + '/reports/' + encodeURIComponent(id) + '/regenerate';
  let requested = false;
  let timer = null;
  let pollInFlight = null;
  let pollVersion = 0;
  let submitting = false;
  const show = (s, error) => {{ status.textContent = s === 'queued' ? 'В очереди…' : s === 'running' ? 'Выполняется…' : s === 'success' ? 'Готово' : s === 'error' ? ('Ошибка пересчёта' + (error ? ': ' + error : '')) : ''; }};
  const poll = () => {{
    if (!panel.open || submitting) return Promise.resolve();
    if (pollInFlight) return pollInFlight;
    const version = pollVersion;
    const pending = (async () => {{
      try {{
        const r = await fetch(url, {{credentials:'same-origin', cache:'no-store'}});
        const v = await r.json();
        if (version !== pollVersion) return;
        if (!r.ok) throw new Error(v.error || 'HTTP ' + r.status);
        show(v.status, v.error);
        if (v.status === 'queued' || v.status === 'running') timer = setTimeout(poll, 1000);
        else {{ button.disabled = false; if (requested && v.status === 'success') window.location.reload(); }}
      }} catch (error) {{
        if (version === pollVersion) {{ show('error', error.message); button.disabled = false; }}
      }}
    }})();
    const wrapped = pending.finally(() => {{ if (pollInFlight === wrapped) pollInFlight = null; }});
    pollInFlight = wrapped;
    return wrapped;
  }};
  panel.addEventListener('toggle', () => {{
    if (panel.open) {{
      const pending = pollInFlight;
      if (pending) pending.then(() => {{ if (panel.open && !submitting) poll(); }});
      else poll();
    }} else {{ ++pollVersion; clearTimeout(timer); timer = null; }}
  }});
  button.addEventListener('click', async () => {{
    ++pollVersion;
    submitting = true;
    clearTimeout(timer); timer = null;
    requested = true; button.disabled = true; show('queued');
    try {{
      const text = question.value.trim();
      const body = text ? JSON.stringify({{question:text}}) : '{{}}';
      const r = await fetch(url, {{method:'POST', credentials:'same-origin', body:body,
        headers:{{'Content-Type':'application/json'}}}});
      const v = await r.json();
      if (!r.ok) throw new Error(v.error || 'HTTP ' + r.status);
      show(v.status, v.error);
    }} catch (error) {{ submitting = false; show('error', error.message); button.disabled = false; return; }}
    submitting = false;
    if (pollInFlight) await pollInFlight;
    poll();
  }});
}})();</script>'''


def regeneration_control(report_id: str, *, api_base_url: str = "/api") -> str:
    """Compatibility wrapper for callers that only have an id."""
    return render_regeneration(report_id, api_base_url)

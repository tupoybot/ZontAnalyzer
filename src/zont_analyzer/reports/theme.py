"""Shared standalone report design tokens and progressive disclosure controls."""
STYLE = r"""
:root{--page:#f3f5f4;
--surface:#fff;
--elevated:#f8faf9;
--text:#192d2b;
--secondary:#536862;
--muted:#63756e;
--border:#d9e2dd;
--accent:#176757;
--success:#26704c;
--warning:#95600d;
--danger:#a13832;
--info:#326783}

*{box-sizing:border-box}
body{font:16px/1.55 system-ui,sans-serif;
margin:0;
color:var(--text);
background:var(--page);
font-variant-numeric:tabular-nums}
body>header,body>main,body>footer{max-width:1360px;
margin:auto;
padding:24px 32px}
body>header{padding-bottom:12px}
a{color:var(--accent)}
h1,h2,h3,p{margin-top:0}
h1{font-size:32px;
line-height:1.18;
letter-spacing:-.8px;
margin:14px 0 18px}
h2{font-size:23px;
line-height:1.3;
margin-bottom:18px}
h3{font-size:18px;
line-height:1.4}
p{margin-bottom:12px}
small,.secondary{color:var(--secondary);
font-size:13px}
.brand{font-weight:750;
font-size:20px;
letter-spacing:-.6px}
.header-line{display:flex;
align-items:center;
justify-content:space-between;
gap:18px}
.header-tools{display:flex;
gap:20px;
align-items:center}
.period-label{font-size:18px;
font-weight:600}
.eyebrow{font-size:12px;
letter-spacing:1.8px;
color:var(--accent);
font-weight:700}
.report-layout{display:grid;
grid-template-columns:minmax(0,1fr) minmax(300px,360px);
gap:24px}
.overview,.actions{min-width:0}
.full-width{grid-column:1/-1}
.hero{padding:30px;
background:var(--surface);
border:1px solid var(--border);
border-top:4px solid var(--accent);
border-radius:12px}
.hero.warning{border-top-color:var(--warning)}
.lead{max-width:70ch;
font-size:17px;
line-height:1.65}
.actions{background:var(--surface);
border:1px solid var(--border);
border-radius:12px;
padding:24px;
align-self:start}
.actions>h2{margin-bottom:12px}
.recommendation{padding:20px 0;
border-top:1px solid var(--border);
overflow-wrap:anywhere}
.recommendation p{font-size:14px}
.recommendation h3{margin-bottom:12px}
.kpi-grid{display:grid;
grid-template-columns:repeat(3,minmax(0,1fr));
gap:0;
margin-top:18px;
background:var(--surface);
border:1px solid var(--border);
border-radius:12px;
overflow:hidden}
.kpi{padding:18px;
border-bottom:1px solid var(--border)}
.kpi span{display:block;
font-size:12px;
color:var(--secondary)}
.kpi strong{display:block;
font-size:25px;
letter-spacing:-.5px;
margin-top:5px}
.kpi small{display:block;font-size:12px;color:var(--secondary);margin-top:7px}
.kpi-gas-strip{grid-column:1/-1;display:grid;
grid-template-columns:minmax(130px,180px) minmax(0,1fr);gap:18px;align-items:center}
.gas-kpi-total span,.gas-distribution-label{display:block;font-size:12px;color:var(--secondary)}
.gas-kpi-total strong{display:block;font-size:25px;letter-spacing:-.5px;margin-top:5px}
.gas-kpi-total small{display:block;font-size:12px;color:var(--secondary);margin-top:7px}
.gas-distribution-bar{display:flex;height:12px;overflow:hidden;border-radius:6px;
background:var(--elevated);margin:8px 0}
.gas-bar-heat{background:#b36332}.gas-bar-dhw{background:#227d8c}.gas-bar-unknown{background:#9aa8aa}
.gas-distribution-legend{display:flex;gap:8px 20px;flex-wrap:wrap;font-size:12px;color:var(--secondary)}
.gas-distribution-legend span{display:flex;align-items:center;gap:5px;min-width:0}
.gas-distribution-legend b{color:var(--text);font-weight:600;white-space:nowrap}
.gas-swatch{display:inline-block;width:8px;height:8px;border-radius:3px;flex:none}.gas-swatch-heat{background:#b36332}.gas-swatch-dhw{background:#227d8c}.gas-swatch-unknown{background:#9aa8aa}
.gas-distribution-note:empty{display:none}.gas-distribution-unavailable{margin:0;color:var(--secondary);font-size:12px}
.kpi-uptime-row{grid-column:1/-1;display:flex;gap:10px 24px;flex-wrap:wrap;
padding:13px 18px;background:var(--elevated);border-bottom:0;font-size:12px;color:var(--secondary)}
.kpi-uptime-row>span{display:flex;align-items:center;gap:5px}.kpi-uptime-row .debug-only{display:none}
.uptime-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block}
.uptime-offline .uptime-dot{background:var(--danger)}.uptime-unknown .uptime-dot{background:var(--muted)}
.gas-period-card{display:block;
gap:12px 24px;margin-top:18px;padding:22px;background:var(--surface);border:1px solid var(--border);border-radius:12px}
.gas-period-card>summary{cursor:pointer;font-weight:650}.gas-period-card[open]>summary{margin-bottom:12px}
.gas-period-card h2{margin:5px 0 0;font-size:19px}.gas-period-value{display:block;font-size:28px;
letter-spacing:-.5px;margin-top:5px}.gas-period-details{flex:1 1 280px;margin:0;color:var(--secondary);
font-size:14px;overflow-wrap:anywhere}
.gas-period-stale{flex-basis:100%;margin:0;color:var(--warning);font-size:14px}
.gas-savings{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:26px}
.gas-savings article{padding:12px 0;border-top:1px solid var(--border)}.gas-savings h3{margin-bottom:8px}
.gas-savings p{font-size:14px}
.chart-section,.thermal-system,.details-area{background:var(--surface);
border:1px solid var(--border);
border-radius:12px;
padding:26px}
.thermal-columns,.lower-grid{display:grid;
grid-template-columns:1fr 1fr;
gap:28px}
.thermal-columns>section{padding:20px;
background:var(--elevated);
border-radius:8px}
.thermal-system .dhw{padding:0;
background:none}
.thermal-system .dhw h2{font-size:18px}
.boiler-context{color:var(--secondary);
border-bottom:1px solid var(--border);
padding-bottom:14px}
.interaction{padding:16px 0;
font-size:15px}
.chart-section svg{display:block;
width:100%;
height:auto}
.chart-section figure{margin:0}
.chart-section figcaption{font-size:13px;
color:var(--secondary)}
.timeline{list-style:none;
padding:0;
margin:0}
.timeline li{display:flex;
gap:18px;
padding:14px 0;
border-bottom:1px solid var(--border);
font-size:14px}
.timeline time{flex:0 0 68px;
color:var(--secondary)}
.timeline strong{font-weight:550}
.timeline span{display:block;
color:var(--secondary)}
.event-severity:empty{display:none}
.quality{padding:22px;
background:var(--surface);
border:1px solid var(--border);
border-radius:10px}
.quality h2{font-size:18px}
.quality p{font-size:14px}
.metric-group{border-bottom:1px solid var(--border)}
details{margin:8px 0}
summary{cursor:pointer;
font-weight:600;
padding:13px 0;
min-height:44px}
table{width:100%;
border-collapse:collapse;
table-layout:fixed}
td,th{text-align:left;
padding:12px 8px;
border-bottom:1px solid var(--border);
font-size:14px;
vertical-align:top;
overflow-wrap:anywhere}
th{font-weight:500;
width:65%}
pre{white-space:pre-wrap;
overflow-wrap:anywhere;
font:12px/1.5 ui-monospace,monospace;
max-height:600px;
overflow:auto;
background:var(--elevated);
padding:14px}
.debug-only{display:none!important}
body.debug-mode .debug-only{display:block!important;
font-size:12px;
color:var(--secondary)}
button,input,select,textarea{font:inherit}
button{min-height:44px;
padding:8px 13px;
border:1px solid var(--border);
border-radius:7px;
color:var(--text);
background:var(--surface);
cursor:pointer}
button:hover{background:var(--elevated)}
button:disabled{opacity:.5;
cursor:default}
a:focus-visible,button:focus-visible,input:focus-visible,summary:focus-visible,
textarea:focus-visible,select:focus-visible{outline:3px solid var(--accent);
outline-offset:3px}
input[type=checkbox]{width:18px;
height:18px;
accent-color:var(--accent)}
.feedback-actions{display:flex;
flex-wrap:wrap;
gap:6px}
.feedback-actions button{font-size:13px}
.feedback-actions [data-feedback-status=applied]{color:var(--accent)}
.feedback-status{font-size:12px;
font-weight:650}
.status-applied{color:var(--success)}
.status-rejected,.error{color:var(--danger)}
.saved-note{font-size:13px}
.feedback-note{width:100%;
padding:10px;
border:1px solid var(--border);
border-radius:6px}
.feedback-message{font-size:13px;
margin:8px 0}
.feedback-message:empty{display:none}
.report-regeneration{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px 14px;align-items:end}
.report-regeneration label{grid-column:1/-1;font-weight:650}.counterfactual-question{display:block;width:100%;
min-height:76px;resize:vertical;padding:10px;border:1px solid var(--border);
border-radius:7px;background:var(--surface);color:var(--text);overflow-wrap:anywhere}
.regenerate-report{align-self:end;white-space:normal}.regeneration-status{grid-column:1/-1;color:var(--secondary);font-size:13px;overflow-wrap:anywhere}
.feedback-comment summary,.feedback-experiment summary{font-size:13px}
.feedback-experiment label{display:block;margin:10px 0;font-size:13px}
.feedback-experiment input,.feedback-experiment select{display:block;width:100%;min-width:0;
box-sizing:border-box;padding:8px;border:1px solid var(--border);border-radius:6px;
background:var(--surface);color:var(--text)}
.archive-navigation{margin-top:18px}
.archive-controls{display:flex;
flex-wrap:wrap;
gap:12px;
align-items:center}
.archive-controls[hidden]{display:none}
.archive-period-tabs,.archive-day-actions,.archive-month-controls{display:flex;
gap:5px;
align-items:center}
.archive-period-tabs button[aria-selected=true]{background:var(--accent);
color:white;
border-color:var(--accent)}
.archive-picker{flex-basis:100%;
background:var(--surface);
border:1px solid var(--border);
padding:0 16px;
border-radius:8px}
.archive-picker:not([open]){flex-basis:auto;
background:transparent;
border:0;
padding:0}
.archive-calendar{display:grid;
grid-template-columns:repeat(7,minmax(32px,1fr));
gap:4px;
max-width:400px;
margin:12px 0}
.archive-day,.archive-weekday{display:grid;
place-items:center;
min-height:40px;
font-size:14px}
.archive-day.available{background:var(--elevated);
border-radius:5px}
.archive-day.selected{background:var(--accent);
color:white}
.archive-day.unavailable{color:var(--muted)}
.archive-status:empty{display:none}
.archive-month-label{min-width:160px;
text-align:center}
.archive-nojs{font-size:13px}
.reasoning-item{padding:18px 0;
border-bottom:1px solid var(--border)}
.reasoning-item p{font-size:14px}
.sensor-list li{padding:8px 0}
.sensor-list span{color:var(--secondary)}
footer{color:var(--secondary);
font-size:13px}
.skip-link{position:absolute;
left:-10000px}
.skip-link:focus{left:20px;
top:12px;
background:white;
padding:12px;
z-index:10}

@media(max-width:1000px){.report-layout{grid-template-columns:minmax(0,1fr) minmax(280px,320px);
gap:18px}
body>header,body>main,body>footer{padding:20px}
.hero{padding:22px}
.kpi strong{font-size:22px}
}

@media(max-width:720px){body>header,body>main,body>footer{padding:16px}
.header-line{flex-wrap:wrap}
.header-tools{gap:14px;
font-size:14px}
.report-layout,.thermal-columns,.lower-grid{display:flex;
flex-direction:column;
gap:18px}
.actions{width:100%}
.hero{padding:22px}
h1{font-size:28px}
.kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.kpi-gas-strip{grid-template-columns:1fr;gap:10px}.kpi-uptime-row{gap:8px 18px}
.chart-section,.thermal-system,.details-area{padding:18px}
.reliability{flex-wrap:wrap;
gap:12px;
padding:8px}
.period-label{font-size:16px}
.archive-controls{gap:8px}
.archive-day-actions button{font-size:13px}
.timeline time{flex-basis:50px}
.chart-section svg{min-width:0}
.engineering-chart{display:none}
body.debug-mode .engineering-chart{display:block}
th{width:60%}
.owner-form-grid{grid-template-columns:1fr!important}
.report-regeneration{grid-template-columns:1fr}.report-regeneration .regenerate-report{width:100%}
}

@media print{button,.header-tools,.feedback-controls,.archive-navigation{display:none}
.report-layout{display:block}
.actions{margin:20px 0}
body{background:white}
.debug-only{display:none!important}
}

.report-charts{display:grid;gap:24px;margin-top:18px}
.more-actions .recommendation{background:var(--surface);padding:24px;border:1px solid var(--border);border-radius:12px}
.thermal-system .report-chart{padding:0;border:0}
.report-chart{margin:0;padding:24px;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
.chart-unit{font-size:14px;color:var(--secondary);margin:12px 0 18px}
.chart-plot-grid{display:grid;grid-template-columns:48px minmax(0,1fr);gap:12px 0}
.chart-y-axis{position:relative;font-size:14px;color:var(--secondary)}
.chart-y-axis span{position:absolute;left:0;transform:translateY(-50%);line-height:1}
.chart-svg{display:block;width:100%;height:clamp(180px,22vw,260px);min-width:0}
.chart-x-axis{grid-column:2;position:relative;height:1.6em;font-size:13px;color:var(--secondary)}
.chart-x-axis span{position:absolute;white-space:nowrap;transform:translateX(-50%)}
.chart-x-axis .chart-first-tick{transform:none}.chart-x-axis .chart-last-tick{transform:translateX(-100%)}
@media(max-width:720px){.chart-x-axis .chart-minor-tick{display:none}}
.chart-legend{display:flex;gap:16px;flex-wrap:wrap;padding:0;margin:12px 0 0;list-style:none;font-size:13px}
.chart-legend span{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
.chart-state-legend{display:flex;gap:12px;flex-wrap:wrap;padding:0;list-style:none;font-size:13px}
.chart-state-legend span{display:inline-block;width:10px;height:10px;margin-right:5px}
.chart-note,.chart-unavailable{color:var(--secondary);margin:10px 0 0;font-size:13px}
"""

SCRIPT = r"""
(() => {
 const toggle = document.querySelector('#debug-toggle');
 let enabled = false;
 try { enabled = localStorage.getItem('zont-debug') === '1'; } catch (_) {}
 const query = new URLSearchParams(location.search).get('debug');
 if (query !== null) enabled = query === '1';
 const apply = () => { document.body.classList.toggle('debug-mode', enabled); toggle.checked = enabled; };
 apply();
 toggle.addEventListener('change', () => {
   enabled = toggle.checked; apply();
   try { localStorage.setItem('zont-debug', enabled ? '1' : '0'); } catch (_) {}
 });
 document.querySelector('[data-open-profile]').addEventListener('click', () => {
   const profile = document.querySelector('#system-profile');
   if (profile) { profile.open = true;
     profile.scrollIntoView({behavior:'smooth'});
     profile.querySelector('summary').focus(); }
 });
})();
"""

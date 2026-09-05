from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.domain import MetricValue, QualityResult, Report
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.renderers import _ARCHIVE_NAVIGATION_SCRIPT


def _report(*, kind: str = "daily") -> Report:
    start = datetime(2026, 9, 1, 20, tzinfo=UTC)
    return Report(
        id=f"report:{kind}:1:report-v2",
        kind=kind,  # type: ignore[arg-type]
        period_start=start,
        period_end=start + timedelta(days=1),
        generated_at=start,
        timezone="Europe/Samara",
        quality=QualityResult(
            score=1,
            coverage_pct=100,
            max_gap_seconds=0,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=1,
        ),
        summary="Архивный отчёт",
    )


@pytest.mark.parametrize("unknown_restore", [0, 2])
def test_missing_mttr_is_explained_without_inventing_a_value(unknown_restore: int) -> None:
    report = _report().model_copy(update={"context": {"reliability": {"boiler": {
        "confirmed_service_failures": unknown_restore,
        "service_failures_with_unknown_restore": unknown_restore,
    }}}})
    reason = "разрывы наблюдаемости" if unknown_restore else "нет подтверждённых отказов"
    for content in (render_html(report), render_text(report)):
        assert "MTTR котельного сервиса" in content
        assert "достоверных данных" in content
        assert reason in content
    assert report.metrics == []


def test_observed_mttr_keeps_its_numeric_value() -> None:
    report = _report().model_copy(update={
        "context": {"reliability": {"boiler": {"service_failures_with_unknown_restore": 2}}},
        "metrics": [MetricValue(id="mttr", name="boiler_mttr_hours", value=1.5, unit="h")],
    })
    for content in (render_html(report), render_text(report)):
        assert "MTTR котельного сервиса" in content
        assert "00:01:30" in content
        assert "Нет достоверных данных" not in content
        assert "Момент восстановления" not in content


def test_archive_ui_uses_local_report_boundaries_and_standalone_navigation() -> None:
    rendered = render_html(
        _report(),
        feedback_api_base_url="/za/api",
        latest_report_href="../latest.html",
    )

    assert 'data-report-kind="daily"' in rendered
    assert 'data-report-start="2026-09-02"' in rendered
    assert 'data-report-end="2026-09-03"' in rendered
    assert 'data-archive-kind="daily">День' in rendered
    assert 'data-archive-kind="weekly">Неделя' in rendered
    assert 'data-archive-kind="monthly">Месяц' in rendered
    assert 'data-archive-action="previous"' in rendered
    assert 'data-archive-action="latest"' in rendered
    assert 'data-archive-action="next"' in rendered
    assert 'data-archive-month="previous"' in rendered
    assert 'data-archive-month="next"' in rendered
    assert 'href="../latest.html">последний сформированный дневной отчёт' in rendered
    assert "reports.json" in rendered
    assert "credentials: \"same-origin\"" in rendered
    assert "<iframe" not in rendered
    assert "cdn" not in rendered.lower()


def test_archive_ui_uses_a_root_stable_latest_link_by_default() -> None:
    rendered = render_html(_report())

    assert 'href="latest.html">последний сформированный дневной отчёт' in rendered


def test_archive_ui_limits_manifest_to_direct_archive_links_and_uses_root_aware_feedback() -> None:
    rendered = render_html(_report(kind="weekly"), feedback_api_base_url="/za/api")

    assert 'data-report-kind="weekly"' in rendered
    assert "start.toISOString().slice(0, 10) === value.start" in rendered
    assert "value.start < value.end" in rendered
    assert "value.href === `${value.kind}/${value.start}.html`" in rendered
    assert "new URL(report.href, window.location.origin + root).pathname" in rendered
    assert "window.location.assign(directUrl({href: button.dataset.href}))" in rendered
    assert 'configuredApiBase === "/api" || configuredApiBase === "/za/api"' in rendered
    assert "? `${archiveRoot}api`" in rendered


def test_archive_navigation_javascript_has_valid_node_syntax(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    source = tmp_path / "archive-navigation.js"
    source.write_text(_ARCHIVE_NAVIGATION_SCRIPT, encoding="utf-8")

    result = subprocess.run([node, "--check", str(source)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_archive_navigation_renders_sparse_days_and_root_aware_links(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    source = tmp_path / "archive-navigation-behavior.js"
    source.write_text(
        "const archiveScript = "
        + json.dumps(_ARCHIVE_NAVIGATION_SCRIPT)
        + ";\n"
        + r'''
const listeners = new WeakMap();
function element(dataset = {}) {
  return {
    dataset, disabled: false, hidden: true, textContent: "", innerHTML: "", tabIndex: 0,
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) {
      const registered = listeners.get(this) || {};
      registered[name] = callback;
      listeners.set(this, registered);
    },
    click() { listeners.get(this)?.click?.({}); },
    focus() { document.activeElement = this; },
  };
}
const controls = element();
const status = element();
const panel = element();
const noJs = element();
const periodTabs = element();
const previous = element();
const latest = element();
const next = element();
const monthPrevious = element({archiveMonth: "previous"});
const monthNext = element({archiveMonth: "next"});
const monthLabel = element();
const daily = element({archiveKind: "daily"});
const weekly = element({archiveKind: "weekly"});
const monthly = element({archiveKind: "monthly"});
const navigation = element({reportKind: "daily", reportStart: "2026-09-03", reportEnd: "2026-09-04"});
const one = {
  ".archive-controls": controls, ".archive-status": status, ".archive-panel": panel,
  ".archive-nojs": noJs, ".archive-period-tabs": periodTabs,
  '[data-archive-action="previous"]': previous, '[data-archive-action="latest"]': latest,
  '[data-archive-action="next"]': next, ".archive-month-label": monthLabel,
};
navigation.querySelector = (selector) => one[selector] || null;
navigation.querySelectorAll = (selector) => {
  if (selector === "[data-archive-kind]") return [daily, weekly, monthly];
  if (selector === "[data-archive-month]") return [monthPrevious, monthNext];
  return [];
};
document = {
  activeElement: daily,
  querySelector: (selector) => selector === "[data-archive-navigation]" ? navigation : null,
};
let assigned = "";
window = {
  location: {
    pathname: "/za/daily/2026-09-03.html", origin: "https://archive.example",
    assign: (url) => { assigned = url; },
  },
};
let fetched = "";
fetch = async (url) => { fetched = String(url); return {ok: true, json: async () => ({version: 1, reports: [
  {kind: "daily", start: "2026-09-01", end: "2026-09-02", href: "daily/2026-09-01.html"},
  {kind: "daily", start: "2026-09-03", end: "2026-09-04", href: "daily/2026-09-03.html"},
  {kind: "daily", start: "2026-09-05", end: "2026-09-06", href: "daily/2026-09-05.html"},
  {kind: "daily", start: "2026-09-31", end: "2026-10-01", href: "daily/2026-09-31.html"},
  {kind: "daily", start: "2026-09-04", end: "2026-09-04", href: "daily/2026-09-04.html"},
  {kind: "daily", start: "2026-09-04", end: "2026-09-05", href: "weekly/2026-09-04.html"},
]})}};
eval(archiveScript);
setTimeout(() => {
  next.click();
  console.log(JSON.stringify({
    controlsVisible: !controls.hidden,
    fetchUrl: fetched,
    calendar: panel.innerHTML,
    month: monthLabel.textContent,
    nextUrl: assigned,
  }));
}, 0);
''',
        encoding="utf-8",
    )

    result = subprocess.run([node, str(source)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["controlsVisible"] is True
    assert payload["fetchUrl"] == "https://archive.example/za/reports.json"
    assert 'href="/za/daily/2026-09-01.html"' in payload["calendar"]
    assert 'href="/za/daily/2026-09-03.html"' in payload["calendar"]
    assert 'href="/za/daily/2026-09-05.html"' in payload["calendar"]
    assert "2026-09-31.html" not in payload["calendar"]
    assert payload["nextUrl"] == "/za/daily/2026-09-05.html"
    assert "Invalid Date" not in payload["month"]

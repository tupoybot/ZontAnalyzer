from datetime import UTC, datetime

from zont_analyzer.domain import EvidenceReference, QualityResult, Report, TimeInterval, Unknown
from zont_analyzer.reports import render_html, render_text


def _report(settings: dict | None = None) -> Report:
    return Report(
        id="report:settings-presentation",
        kind="daily",
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 2, tzinfo=UTC),
        generated_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="Europe/Samara",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0, implausible_jumps=0,
                              sample_count=1),
        summary="summary",
        context={"control_settings": settings} if settings is not None else {},
    )


def test_settings_render_snapshot_in_report_timezone_and_curve() -> None:
    settings = {
        "captured_at": "2026-09-01T20:05:00+00:00",
        "regulation": {"status": "decoded", "label": "Воздушное <PID>", "pza_role_label": "уставка подачи"},
        "parameters": [
            {"field": "pid_prop_koef", "value": 1.5},
            {"field": "pid_integral_koef", "value": 20},
        ],
        "pza_curve": {"points_c": [{"outdoor_c": -20, "flow_c": 70}, {"outdoor_c": 10, "flow_c": 35}]},
    }
    text = render_text(_report(settings))
    page = render_html(_report(settings))
    assert "Воздушное <PID>" in text and "P: 1,5" in text and "I: 20" in text
    assert "Роль ПЗА: уставка подачи" in text and "-20 → 70" in text
    assert "Время снимка: 2026-09-02 00:05:00" in text
    assert "Снимок настроек не подтверждает их неизменность за весь период." in page
    assert "Воздушное &lt;PID&gt;" in page and "Воздушное <PID>" not in page
    assert "<table class=\"control-settings-curve\">" in page
    assert "На улице, °C" in page and "Теплоноситель, °C" in page
    assert "-20" in page and "70" in page
    assert '<details class="control-settings">' in page


def test_legacy_and_malformed_settings_do_not_decode_raw_curve_or_mutate_report() -> None:
    settings = {
        "captured_at": "bad",
        "parameters": [{"field": "pid_prop_koef", "value": 2}],
        "pza_curve": {"x_raw": [-20, 10], "y_raw": [70, 35], "encoding": "unverified"},
    }
    report = _report(settings)
    original = report.model_dump_json()
    text = render_text(report)
    assert "P: 2" in text and "Настроенное регулирование: неизвестно" in text
    assert "-20 → 70" not in text
    assert report.model_dump_json() == original


def test_disabled_and_invalid_snapshot_values_are_explicit() -> None:
    settings = {
        "captured_at": "2026-09-01T20:05:00",
        "regulation": {"status": "decoded", "label": "PID", "enabled": False},
        "parameters": [{"field": "pid_prop_koef", "value": float("nan")}],
        "pza_curve": {"points_c": [{"outdoor_c": float("inf"), "flow_c": 40}]},
    }
    text = render_text(_report(settings))
    assert "Контур отключён в настройках" in text
    assert "P: не указано" not in text
    assert "Время снимка: время снимка не указано" in text


def test_settings_unknown_uses_snapshot_label_and_report_timezone() -> None:
    instant = datetime(2026, 9, 1, 20, 5, tzinfo=UTC)
    report = _report({"captured_at": instant.isoformat()})
    report.unknowns = [Unknown(
        id="unknown:settings", statement="Настройка не расшифрована",
        interval=TimeInterval(started_at=instant, ended_at=instant, timezone="UTC+04"),
        evidence=[EvidenceReference(id="setting:1:pid_prop_koef")],
    )]
    text = render_text(report)
    assert "Время снимка: 2026-09-02 00:05:00" in text
    assert "Временной интервал: 2026-09-02 00:05 +04 — 2026-09-02 00:05 +04" not in text

    report.unknowns[0].evidence = [EvidenceReference(id="metric:temperature")]
    regular = render_text(report)
    assert "Временной интервал: 2026-09-02 00:05 +04 — 2026-09-02 00:05 +04" in regular

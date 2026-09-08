from datetime import UTC, datetime, timedelta

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.language import normalize_user_text
from zont_analyzer.reports.wording import normalize_report_for_display


def test_translates_internal_names_and_keeps_stuck_meaning() -> None:
    text = (
        "Паттерн подтверждён несколькими derived-метриками при покрытии "
        "temporal_evidence 99,93%; оценка газа имеет статус estimated и низкий "
        "reliability_index_pct 35%; качество датчика ограничено stuck_pct 86,17%; "
        "но отсутствуют period_comparisons."
    )

    normalized = normalize_user_text(text)

    assert "расчётными показателями" in normalized
    assert "Закономерность подтверждена" in normalized
    assert "полноте временных данных 99,93%" in normalized
    assert "Расход газа рассчитан по модели" in normalized
    assert "индекс надёжности низкий — 35%" in normalized
    assert "доля неизменных показаний 86,17%" in normalized
    assert "сравнения с другими периодами" in normalized
    assert "stuck_pct" not in normalized
    assert normalize_user_text("один derived-кандидат; отсутствуют период_comparisons") == (
        "один косвенный признак; отсутствуют сравнения с другими периодами"
    )
    assert normalize_user_text("Отмечен повторяющийся паттерн коротких циклов, но его связь не установлена.") == (
        "Отмечена повторяющаяся закономерность коротких циклов, но её связь не установлена."
    )
    assert normalize_user_text("часовой пояс Etc/GMT-4") == "часовой пояс UTC+4"


def test_evidence_ids_and_canonical_report_are_unchanged() -> None:
    value = "Окно temporal_evidence:window-1 имеет stuck_pct:metric-1."
    assert normalize_user_text(value) == value

    start = datetime(2026, 1, 1, tzinfo=UTC)
    report = Report(
        id="report-1", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start,
        summary="Паттерн подтверждён derived-метриками при stuck_pct 86,17%.",
        quality=QualityResult(score=.9, coverage_pct=100, max_gap_seconds=0, stuck_pct=86.17,
                              implausible_jumps=0, sample_count=1),
    )

    display = normalize_report_for_display(report)

    assert "derived-метриками" in report.summary
    assert "stuck_pct 86,17%" in report.summary
    assert "расчётными показателями" in display.summary
    assert "доля неизменных показаний 86,17%" in display.summary

from datetime import UTC, datetime, timedelta

from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.wording import normalize_report_for_display, normalize_text


def test_normalizes_normal_state_without_erasing_uncertainty_or_warning() -> None:
    text = (
        "Явная неисправность регулирования не подтверждена. "
        "Это объясняет часть отсутствия отопительного запроса без признака неисправности."
    )
    normalized = normalize_text(text)
    assert "Работу регулирования можно оценить" in normalized
    assert "при текущем режиме" in normalized
    assert normalize_text("Причина пока неизвестна; нужны дополнительные данные.") == (
        "Причина пока неизвестна; нужны дополнительные данные."
    )
    assert normalize_text("Критическая проблема: отключение котла.") == (
        "Критическая проблема: отключение котла."
    )
    assert normalize_text("Нельзя утверждать, что аномалий не выявлено.") == (
        "Нельзя утверждать, что аномалий не выявлено."
    )


def test_report_display_copy_normalizes_whitelisted_fields_and_preserves_canonical() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    report = Report(
        id="report-1", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start, summary="Аномалий не выявлено.",
        quality=QualityResult(score=.9, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
    )
    display = normalize_report_for_display(report)
    assert report.summary == "Аномалий не выявлено."
    assert display.summary == "Оценка текущего режима работы представлена в наблюдениях отчёта."


def test_latest_report_combined_sentence_keeps_observation_and_quality_limits() -> None:
    text = (
        "За период система оставалась наблюдаемой и явная неисправность регулирования не подтверждена. "
        "Качество датчика ГВС остаётся ограниченным из-за длительного застывания."
    )
    assert normalize_text(text) == (
        "За период система оставалась наблюдаемой. "
        "Качество датчика ГВС остаётся ограниченным из-за длительного застывания."
    )

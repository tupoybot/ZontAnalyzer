from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zont_analyzer.application.period_schedule import schedule_signature, scheduled_periods
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.domain.periods import calendar_period
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.runtime import build_runtime


def test_equivalent_zont_offset_does_not_schedule_existing_week_again(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    analysis = runtime.analysis(no_ai=True)
    period = calendar_period('weekly', date(2026, 8, 31), 'Europe/Samara')
    signature = schedule_signature(analysis, period)
    report = Report(id=analysis.report_id_for('weekly', period.start), kind='weekly',
                    period_start=period.start, period_end=period.end, generated_at=period.end,
                    timezone='Europe/Samara', summary='Неделя',
                    context={'period': period.model_dump(mode='json'), 'schedule_signature': signature},
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=1))
    runtime.db.save_report(report, 'Неделя')
    runtime.db.save_devices([{'id': '1', 'timezone': 4}])
    analysis = runtime.analysis(no_ai=True)
    periods = scheduled_periods(analysis, date(2026, 8, 31), date(2026, 9, 7))
    week = next(p for p in periods if p.kind == 'weekly')
    assert week.timezone == 'Etc/GMT-4'
    assert schedule_signature(analysis, week) == signature
    changed = calendar_period('weekly', date(2026, 8, 31), 'Etc/GMT-3')
    assert schedule_signature(analysis, changed) != signature


def test_timezone_source_visible_in_html_and_text_without_relabelling_old_boundaries() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    report = Report(id='tz', kind='daily', period_start=start, period_end=start+timedelta(days=1),
                    generated_at=start, timezone='Europe/Samara', summary='День',
                    context={'gas': {'timezone_provenance': {'timezone': 'Etc/GMT-4', 'source': 'zont'}}},
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=1))
    for render in (render_html, render_text):
        assert 'Часовой пояс: UTC+04:00 · настройки ZONT' in render(report)
        changed = report.model_copy(deep=True)
        changed.context['gas']['timezone_provenance']['timezone'] = 'Etc/GMT-3'
        assert 'сохранён при расчёте отчёта (Europe/Samara)' in render(changed)
        assert 'UTC+04:00 · настройки ZONT' not in render(changed)

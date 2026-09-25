from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.gas import GasInterval, GasModel
from zont_analyzer.application.gas import GasService
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html
from zont_analyzer.reports.charts.gas import render_daily_gas


def report(kind='weekly', days=7, daily=None):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return Report(id='gas-days', kind=kind, timezone='UTC', period_start=start,
                  period_end=start + timedelta(days=days), generated_at=start + timedelta(days=days),
                  summary='Период обработан', context={'gas': {'daily': daily or []}},
                  quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                        implausible_jumps=0, sample_count=1))


@pytest.mark.parametrize('kind,days', [('weekly', 7), ('monthly', 31)])
def test_chart_preserves_dates_zero_gaps_and_sources(kind, days):
    r = report(kind, days, [
        {'day': '2026-01-01', 'status': 'measured', 'volume_m3': 0},
        {'day': '2026-01-03', 'status': 'estimated', 'volume_m3': 12.5},
        {'day': '2026-01-04', 'status': 'unknown', 'volume_m3': 9},
        {'day': '2025-12-31', 'status': 'estimated', 'volume_m3': 999},
    ])
    chart = render_daily_gas(r)
    assert chart.count('class="gas-day-bar') == 2
    assert chart.count('<th scope="row">') == days
    assert '01.01: 0,00 м³ · По счётчику' in chart
    assert '02.01: — · Нет данных' in chart
    assert '03.01: 12,50 м³ · Оценка' in chart
    assert '04.01: — · Нет данных' in chart
    assert '999' not in chart
    assert '<svg' in render_html(r)
    assert 'id="gas-daily"' in render_html(r)


def test_chart_invalid_values_do_not_become_bars():
    for value in (-1, True, float('nan'), float('inf'), '<script>'):
        chart = render_daily_gas(report(daily=[{'day': '2026-01-01', 'status': 'estimated', 'volume_m3': value}]))
        assert '<svg' not in chart
        assert 'Нет дневных данных' in chart


def test_chart_does_not_fabricate_from_period_total_or_render_on_daily():
    r = report()
    r.context['gas']['volume_m3'] = 70
    assert '<svg' not in render_daily_gas(r)
    assert render_daily_gas(report('daily', 1)) == ''


def test_daily_calculation_uses_local_days_and_keeps_meter_intervals_intact(monkeypatch):
    from zont_analyzer.analytics.gas import Exposure

    service = object.__new__(GasService)
    service.timezone = 'Europe/Berlin'
    windows = []

    def window(start, end):
        minutes = (end - start).total_seconds() / 60
        windows.append((start, end))
        return Exposure(minutes=minutes, bin_minutes=(minutes,), observed_minutes=minutes,
                        unknown_modulation_minutes=0, flame_minutes=minutes)

    monkeypatch.setattr(service, 'window', window)
    monkeypatch.setattr(service, '_exposure', lambda value: value)
    model = GasModel(rates_m3_per_minute=(.01,), bin_edges=(0, 100), mean_rate_m3_per_minute=.01)
    # DST day has 23 hours. A multi-day reading must not be divided into daily readings.
    start = datetime(2026, 3, 28, 23, tzinfo=UTC)
    end = datetime(2026, 3, 30, 22, tzinfo=UTC)
    interval = GasInterval(start=start + timedelta(hours=12), end=end + timedelta(hours=12),
                           volume_m3=100, features=(100,), flame_minutes=100, observed_minutes=100, coverage=1)
    values = service.daily_values(start, end, model, [interval])
    assert [v['day'] for v in values] == ['2026-03-29', '2026-03-30']
    assert [v['volume_m3'] for v in values] == pytest.approx([13.8, 14.4])
    assert all(v['status'] != 'measured' for v in values)
    assert windows[0][1] == windows[1][0]
    measured = GasInterval(start=start + timedelta(hours=12), end=windows[0][1] + timedelta(hours=12),
                           volume_m3=8, features=(100,), flame_minutes=100, observed_minutes=100, coverage=1)
    values = service.daily_values(start, end, model, [measured])
    assert values[0]['status'] == 'measured' and values[0]['volume_m3'] == 8
    assert values[1]['status'] != 'measured'


def test_partial_day_is_not_claimed_as_meter_measurement(monkeypatch):
    from zont_analyzer.analytics.gas import Exposure

    service = object.__new__(GasService)
    service.timezone = 'UTC'
    monkeypatch.setattr(service, 'window', lambda a, b: Exposure(
        minutes=720, bin_minutes=(60,), observed_minutes=720,
        unknown_modulation_minutes=0, flame_minutes=60))
    monkeypatch.setattr(service, '_exposure', lambda value: value)
    model = GasModel(rates_m3_per_minute=(.01,), bin_edges=(0, 100), mean_rate_m3_per_minute=.01)
    start = datetime(2026, 1, 1, 12, tzinfo=UTC)
    end = datetime(2026, 1, 2, tzinfo=UTC)
    measured = GasInterval(start=start, end=start + timedelta(days=1), volume_m3=12,
                           features=(60,), flame_minutes=60, observed_minutes=60, coverage=1)
    value, = service.daily_values(start, end, model, [measured])
    assert value['complete'] is False
    assert value['status'] != 'measured'
    assert value['volume_m3'] == pytest.approx(.6)

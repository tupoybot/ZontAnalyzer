from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.burner_usage import burner_usage
from zont_analyzer.domain import MetricValue, QualityResult, Report
from zont_analyzer.reports import render_html, render_text


def test_weekly_purpose_shares_partition_observed_burning() -> None:
    gas = dict(flame_hours=41+37/60, observed_hours=168,
               heating_flame_hours=37+59/60, dhw_flame_hours=3.5,
               purpose_unknown_flame_hours=8/60)
    usage = burner_usage(gas, 168)
    assert usage['flame_pct'] == pytest.approx(24.7718254)
    assert usage['heating_flame_pct'] == pytest.approx(91.2695, abs=.0001)
    assert sum(usage[k] for k in ('heating_flame_pct', 'dhw_flame_pct', 'purpose_unknown_flame_pct')) == 100
    start = datetime(2026, 8, 31, tzinfo=UTC)
    report = Report(id='week', kind='weekly', period_start=start, period_end=start+timedelta(days=7),
                    generated_at=start, summary='Неделя', context={'gas': gas},
                    metrics=[MetricValue(id='heating-duty', name='burner_duty_cycle_pct', value=38.816, unit='percent'),
                             MetricValue(id='dhw-duty', name='dhw_burner_duty_cycle_pct', value=2.084, unit='percent')],
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=1))
    for output in (render_html(report), render_text(report)):
        assert '91,3 % от времени горения' in output
        assert '8,4 % от времени горения' in output
        assert '0,3 % от времени горения' in output
        assert '24,8 % от всего периода' in output
        assert 'за 168,0 ч периода' in output
        assert 'Доля работы горелки на ГВС' not in output
        assert '38,8 %' not in output


def test_missing_observation_is_not_idle_and_zero_flame_has_no_purpose_share() -> None:
    usage = burner_usage(dict(flame_hours=4, observed_hours=20, heating_flame_hours=3,
                             dhw_flame_hours=1, purpose_unknown_flame_hours=0), 24)
    assert usage['flame_pct'] == pytest.approx(100/6)
    assert usage['heating_flame_pct'] == 75
    assert usage['unobserved_hours'] == 4
    idle = burner_usage(dict(flame_hours=0, observed_hours=24, heating_flame_hours=0), 24)
    assert idle['flame_pct'] == 0
    assert idle['heating_flame_pct'] is None
    absent = burner_usage(dict(flame_hours=None, observed_hours=0), 24)
    assert absent['flame_pct'] is None
    assert absent['heating_flame_pct'] is None
    assert absent['unobserved_hours'] == 24

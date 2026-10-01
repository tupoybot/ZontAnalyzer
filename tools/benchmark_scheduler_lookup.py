"""Compare existing-report selection on disposable Docker YDB, never production."""
from __future__ import annotations

import argparse
import itertools
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.benchmark_ydb_steady import baseline, measure, seed_events, seed_month_samples
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import _already_current, schedule_signature, seasonal_daily_signature
from zont_analyzer.application.pilot import _report_source_event_revision
from zont_analyzer.cloud import scheduler
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint
from zont_analyzer.domain.periods import calendar_period


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-scheduler', type=Path, required=True)
    args = parser.parse_args()
    endpoint = os.environ['YDB_TEST_ENDPOINT']
    if not endpoint.startswith('grpc://zont-steady-cost-ydb:'):
        raise ValueError('benchmark requires isolated local Docker YDB')
    old = baseline(args.baseline_scheduler, 'period_needs_report', {
        **vars(scheduler), 'schedule_signature': schedule_signature, '_already_current': _already_current,
        'seasonal_daily_signature': seasonal_daily_signature,
        '_report_source_event_revision': _report_source_event_revision, 'itertools': itertools,
    })
    db = Database(YdbConfig(endpoint, '/local', 'scheduler_lookup', True))
    try:
        db.initialize()
        db.save_devices([{'id': 'fixture'}])
        config = AppConfig()
        config.home.timezone = 'UTC'
        analysis = AnalysisService(db, config)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        period = calendar_period('monthly', start.date(), 'UTC')
        report = analysis.analyze_period(period, use_ai=False)
        db.telemetry.write_window(
            device_id='fixture', data_type='fixture', start=start, end=start + timedelta(hours=1),
            points=[TelemetryPoint(device_id='fixture', source_type='fixture', entity_id='room',
                                   metric_key='temperature', timestamp_utc=start, value_num=20)],
        )
        seed_month_samples(db, start)
        for years in (1, 5, 10):
            seed_events(db, 365 * years)
            report.context['schedule_signature'] = schedule_signature(analysis, period)
            db.save_report(report, report.summary)
            db.storage.execute("DELETE FROM app_meta WHERE key >= 'telemetry-period-revision:' "
                               "AND key < 'telemetry-period-revision;';")
            for attempt in range(2):
                before = measure(db, 'existing-monthly-before', lambda: old(analysis, period),
                                 years=years, attempt=attempt)
                after = measure(db, 'existing-monthly-after', lambda: scheduler.period_needs_report(analysis, period),
                                years=years, attempt=attempt)
                assert before == after is False
    finally:
        db.close()


if __name__ == '__main__':
    main()

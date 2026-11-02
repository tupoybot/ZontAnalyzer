"""Measure real owner-reading updates and complete publication on disposable YDB.

Run in the local Docker test image. JSONL contains measured SDK/RPC costs, not
managed-service billing estimates. Each history runs in a fresh child process so
its peak RSS is independent. The fixture is intentionally sparse outside a short
dense calibration period; monthly archives are NOT a daily-report capacity test.
No source client, AI client, cloud publication, or production endpoint is used.
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tools.benchmark_ydb_cost import ObserveQueries, _seed
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.application.gas import GasService
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.application.publication import archive_paths, publish_reports
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.domain import MetricValue, QualityResult, Report, TelemetryPoint
from zont_analyzer.domain.periods import Period
from zont_analyzer.runtime import Runtime

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def emit(record, output=None):
    line = json.dumps(record, sort_keys=True, default=str)
    print(line, flush=True)
    if output:
        with Path(output).open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')


def measured(storage, years, label, operation, output):
    with ObserveQueries(storage) as costs:
        begun = time.perf_counter()
        result = operation()
        elapsed = time.perf_counter() - begun
    native = costs.native
    if not native['all_rpc_complete'] or native['ru_missing_rpc_count']:
        raise AssertionError('incomplete native RPC accounting')
    emit({'years': years, 'operation': label, 'elapsed_seconds': round(elapsed, 4),
          'cost': asdict(costs), 'result': result}, output)
    return result


def seed_dense(db, start):
    points = []
    for minute in range(0, 32 * 1440 + 1, 10):
        moment = start + timedelta(minutes=minute)
        for key, value in [('s', "['fl', 'ch']"), ('rml', 0.0), ('temperature', -3.0)]:
            points.append(TelemetryPoint(
                device_id='1', entity_id='outdoor' if key == 'temperature' else 'boiler',
                source_type='z3k_boiler_adapter', metric_key=key, timestamp_utc=moment,
                **({'value_text': value} if isinstance(value, str) else {'value_num': value})))
    for offset in range(0, len(points), 1500):
        page = points[offset:offset + 1500]
        db.telemetry.write_window(device_id='1', data_type='dense-calibration',
                                  start=page[0].timestamp_utc, end=page[-1].timestamp_utc + timedelta(seconds=1),
                                  points=page, roles={'outdoor': 'outdoor_temperature'})
    return len(points)


def fixture_reports(start, years):
    quality = QualityResult(score=0, coverage_pct=0, max_gap_seconds=86400,
                            stuck_pct=0, implausible_jumps=0, sample_count=0,
                            flags=['synthetic_sparse_fixture'])
    periods = []
    for offset in range(years * 12):
        year, month0 = divmod(start.year * 12 + offset, 12)
        left = datetime(year, month0 + 1, 1, tzinfo=UTC)
        year2, month2 = divmod(start.year * 12 + offset + 1, 12)
        periods.append(('monthly', left, datetime(year2, month2 + 1, 1, tzinfo=UTC)))
    periods.extend([
        ('daily', start + timedelta(days=28), start + timedelta(days=29)),
        ('weekly', start + timedelta(days=24), start + timedelta(days=31)),
        ('seasonal', start, start.replace(month=4)),
    ])
    return [Report(id=f'fixture-{kind}-{left.date()}', kind=kind,
                   period_start=left, period_end=right, generated_at=NOW, quality=quality,
                   context={'period': Period(kind=kind, start=left, end=right,
                                             observed_end=right, timezone='UTC', complete=True,
                                             season='winter' if kind == 'seasonal' else None,
                                             year=left.year if kind == 'seasonal' else None,
                                             boundary_source='synthetic_fixture').model_dump(mode='json'),
                            'ai_provenance': {'requested_model': 'synthetic-fixture-no-request',
                                              'response_model': 'synthetic-fixture-no-request',
                                              'generated_at': NOW.isoformat(),
                                              'parameters': {'fixture': True},
                                              'prompt_version': 'synthetic-fixture-v1',
                                              'schema_version': 'synthetic-fixture-v1',
                                              'settings_version': 'synthetic-fixture-v1',
                                              'ai_log_id': 'synthetic-fixture-no-request'},
                            'fixture': 'stored report; heat/AI calculation is outside this measurement'},
                   metrics=[MetricValue(id='fixture-heat', name='Synthetic preserved metric',
                                        value=42, unit='fixture')],
                   summary='Synthetic stored AI interpretation; no AI request was made.')
            for kind, left, right in periods]


def stable_payload(report):
    value = report.model_dump(mode='json')
    for key in ('gas', 'gas_savings', 'gas_interpretation_stale'):
        value['context'].pop(key, None)
    return value


def run_case(args):
    # Prevent a caller's cloud settings from redirecting local publication.
    if os.environ.get('CLOUD_PUBLICATION_BUCKET'):
        raise ValueError('unset CLOUD_PUBLICATION_BUCKET for local acceptance')
    storage = YdbDatabase(YdbConfig(args.endpoint, args.database,
                                   f'{args.namespace}_{args.case_year}y', True))
    begun = time.perf_counter()
    try:
        storage.initialize()
        db = Database(storage)
        config = AppConfig()
        config.home.timezone = 'UTC'
        config.analysis.modulation_capability_profile = 'flame_zero_is_minimum'
        with tempfile.TemporaryDirectory(prefix='gas-update-benchmark-') as temporary:
            root = Path(temporary)
            config.pilot.reports_dir = str(root / 'published')
            runtime = Runtime(LoadedConfig(config=config, secrets=Secrets(), config_path=None,
                                          data_dir=root, sources={}), db)
            years = args.case_year
            start = datetime(2026 - years, 1, 1, tzinfo=UTC)
            end = datetime(2026, 1, 1, tzinfo=UTC)
            sparse_points = _seed(db, start + timedelta(days=33), end)
            dense_points = seed_dense(db, start)
            store = OwnerContextStore(db)
            store.update_profile('1', {'fields': {
                'has_gas_stove': {'value': False}, 'gas_min_m3h': {'value': 0.5},
                'gas_max_m3h': {'value': 3.0}}})
            reports = fixture_reports(start, years)
            for report in reports:
                db.save_report(report, report.summary)
            anchor = next(r for r in reports if r.kind == 'daily')
            # Three original readings yield two unchanged reusable intervals.
            for day, value in [(0, 100), (8, 292), (16, 484)]:
                store.update_gas(anchor.id, {'day': (start + timedelta(days=day)).date().isoformat(),
                                             'value_m3': value, 'reading_id': None})
            service = GasService(db, config)
            for report in reports:
                refreshed = service.refresh(report)
                refreshed.ai_used = True
                db.save_report(refreshed, refreshed.summary)

            def drain():
                batches = []
                for _ in range(len(reports) + 4):
                    result = publish_reports(runtime, now=NOW, batch_size=args.batch_size)
                    batches.append({'rendered_reports': result['rendered_reports'],
                                    'pending_reports': result['pending_reports']})
                    if not result['pending_reports']:
                        return {'rendered_reports': sum(b['rendered_reports'] for b in batches),
                                'completed_batches': len(batches), 'batches': batches}
                raise AssertionError('publication queue failed to drain')

            measured(storage, years, 'initial_full_publication', drain, args.output)
            original = {r.id: db.report(r.id) for r in reports}
            original_intervals = [asdict(i) for i in GasService(db, config).intervals()]
            before_model = GasService(db, config).model()[0]
            # These fail-fast guards complement native row/byte measurements.
            def forbidden(*_args, **_kwargs):
                raise AssertionError('unchanged warm gas pass reread telemetry/day dependencies')

            def warm():
                with patch.object(db, 'fetch_samples', forbidden), \
                     patch.object(db, 'fetch_text_samples', forbidden), \
                     patch.object(db, 'fetch_numeric_observations', forbidden), \
                     patch.object(db, 'telemetry_day_revisions', forbidden):
                    cached = GasService(db, config)
                    assert [asdict(i) for i in cached.intervals()] == original_intervals
                    assert cached.model()[0] == before_model
                    cached.context(anchor.period_start, anchor.period_end)
                    idle = drain()
                    assert idle['rendered_reports'] == 0
                    return {'raw_and_day_dependency_reads': 0, **idle}

            measured(storage, years, 'warm_unchanged', warm, args.output)
            payload = {'day': (start + timedelta(days=24)).date().isoformat(),
                       'value_m3': 772}
            measured(storage, years, 'owner_reading_addition',
                     lambda: store.update_gas(anchor.id, payload), args.output)

            def intervals_after():
                windows = []
                original_window = GasService.window

                def window(instance, left, right):
                    windows.append((left, right))
                    return original_window(instance, left, right)

                with patch.object(GasService, 'window', window):
                    intervals = [asdict(i) for i in GasService(db, config).intervals()]
                assert intervals[:2] == original_intervals
                old_bounds = {(i['start'], i['end']) for i in original_intervals}
                assert not old_bounds.intersection(windows), 'unchanged intervals were rebuilt'
                assert len(intervals) == 3
                return {'unchanged_intervals_reused': 2, 'new_intervals': 1,
                        'window_calculations': len(windows)}

            measured(storage, years, 'calibration_interval_reuse', intervals_after, args.output)
            after_model = measured(storage, years, 'model_recalibration',
                                   lambda: GasService(db, config).model()[0], args.output)
            assert after_model != before_model
            updated = measured(storage, years, 'changed_full_publication', drain, args.output)
            changed = 0
            volume_changes = 0
            for report in reports:
                old = original[report.id]
                current = db.report(report.id)
                assert stable_payload(current) == stable_payload(old)
                assert current.context['gas']['ai_stale'] is True
                if current.context['gas'] != old.context['gas']:
                    changed += 1
                if current.context['gas']['volume_m3'] != old.context['gas']['volume_m3']:
                    volume_changes += 1
                _, published_path = archive_paths(root / 'published', current)
                assert json.loads(published_path.read_text()) == current.model_dump(mode='json')
                assert db.get_app_meta('gas-model:' + old.context['gas']['model_version'])
            current_anchor = db.report(anchor.id)
            assert current_anchor.context['gas']['volume_m3'] > original[anchor.id].context['gas']['volume_m3']
            assert changed == len(reports) and updated['rendered_reports'] == len(reports)
            revision = db.source_revision()
            replay = measured(storage, years, 'exact_owner_replay',
                              lambda: store.update_gas(anchor.id, payload), args.output)
            assert replay['reading'] is not None
            assert db.source_revision() == revision, 'exact replay created a source revision'
            idle = measured(storage, years, 'exact_replay_publication', drain, args.output)
            assert idle['rendered_reports'] == 0
            emit({'years': years, 'operation': 'acceptance', 'passed': True,
                  'history_days': (end - start).days,
                  'report_cadence': {'monthly': years * 12, 'daily': 1, 'weekly': 1, 'seasonal': 1},
                  'completed_changed_reports': changed, 'volume_changed_reports': volume_changes,
                  'completed_changed_publications': updated['rendered_reports'],
                  'seed_points': {'dense_10_minute_32_days': dense_points,
                                  'sparse_three_points_per_day_rest': sparse_points},
                  'model_identifiable': GasService(db, config).model()[1].identifiable,
                  'model_before': before_model, 'model_after': after_model,
                  'daily_volume_before_m3': original[anchor.id].context['gas']['volume_m3'],
                  'daily_volume_after_m3': current_anchor.context['gas']['volume_m3'],
                  'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                  'case_elapsed_seconds_including_setup': round(time.perf_counter() - begun, 3),
                  'limits': ['Sparse long archive; no daily-report archive capacity claim.',
                             'Stored synthetic AI reports; analysis/AI/source retrieval excluded.',
                             'Local YDB native RU; not managed-service billing.',
                             'Two unchanged intervals and one new interval; non-identifiable bounded model.']},
                 args.output)
    finally:
        storage.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='grpc://ydb:2136')
    parser.add_argument('--database', default='/local')
    parser.add_argument('--namespace', default='gas_update_benchmark')
    parser.add_argument('--years', type=int, nargs='+', choices=[1, 5, 10], default=[1, 5, 10])
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--output', help='append JSONL evidence to an isolated writable artifact path')
    parser.add_argument('--case-year', type=int, choices=[1, 5, 10], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.endpoint != 'grpc://ydb:2136' or args.database != '/local':
        parser.error('requires disposable grpc://ydb:2136 and /local')
    if not args.namespace.replace('_', '').isalnum() or args.batch_size < 1:
        parser.error('namespace must be alphanumeric/underscore; batch size must be positive')
    if args.case_year:
        run_case(args)
    else:
        for years in args.years:
            command = [sys.executable, '-m', 'tools.benchmark_gas_updates', '--case-year', str(years),
                       '--namespace', args.namespace, '--batch-size', str(args.batch_size)]
            if args.output:
                command.extend(['--output', args.output])
            subprocess.run(command, check=True)


if __name__ == '__main__':
    main()

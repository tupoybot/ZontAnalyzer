"""Paired steady-state reads on disposable local YDB only; never production.

Run in the Docker test image with YDB_TEST_ENDPOINT and baseline source files
from the compared commit. Query statistics are local measurements, not managed RU.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ydb  # type: ignore[import-untyped]

from tools.benchmark_ydb_cost import ObserveQueries
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.period_schedule import _already_current, schedule_signature, seasonal_daily_signature
from zont_analyzer.application.pilot import _report_source_event_revision
from zont_analyzer.cloud import monitoring, scheduler
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import SourceEvent, TelemetryPoint
from zont_analyzer.domain.periods import calendar_period
from zont_analyzer.observability import capture


def baseline(path: Path, name: str, scope: dict[str, Any], cls: str | None = None) -> Any:
    tree = ast.parse(path.read_text())
    nodes = tree.body if cls is None else next(
        node.body for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls
    )
    function = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == name)
    scope = dict(scope)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), scope)
    return scope[name]


def measure(db: Database, name: str, operation: Any, **labels: Any) -> Any:
    transactions = 0
    write_queries = 0
    write_rows = write_bytes = 0
    write_stats_available = False
    original = db.storage.transaction
    original_execute = db.storage.execute
    observer = ObserveQueries(db.storage)
    original_add = observer.costs.add

    def add(results: Any, stats: Any) -> None:
        nonlocal write_rows, write_bytes, write_stats_available
        original_add(results, stats)
        if stats is not None:
            for phase in stats.query_phases:
                for access in phase.table_access:
                    if hasattr(access, 'updates'):
                        write_stats_available = True
                        write_rows += int(access.updates.rows)
                        write_bytes += int(access.updates.bytes)

    observer.costs.add = add  # type: ignore[method-assign]

    def transaction(callback: Any) -> Any:
        nonlocal transactions
        transactions += 1
        return original(callback)

    db.storage.transaction = transaction  # type: ignore[method-assign]

    def count_write(query: str) -> None:
        nonlocal write_queries
        if any(word in query for word in ('UPSERT ', 'UPDATE ', 'DELETE ', 'INSERT ')):
            write_queries += 1

    def execute(query: str, *args: Any, **kwargs: Any) -> Any:
        count_write(query)
        return original_execute(query, *args, **kwargs)

    db.storage.execute = execute  # type: ignore[method-assign]
    try:
        with observer as costs:
            original_tx_execute = Transaction.execute

            def tx_execute(tx: Transaction, query: str, *args: Any, **kwargs: Any) -> Any:
                count_write(query)
                return original_tx_execute(tx, query, *args, **kwargs)

            Transaction.execute = tx_execute  # type: ignore[method-assign]
            started = time.perf_counter()
            result = operation()
            elapsed = time.perf_counter() - started
    finally:
        db.storage.transaction = original  # type: ignore[method-assign]
        db.storage.execute = original_execute  # type: ignore[method-assign]
    checksum = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    print(json.dumps({"scenario": name, **labels, **asdict(costs),
                      "explicit_transactions": transactions, "seconds": round(elapsed, 6),
                      "write_queries": write_queries, "managed_ru": None,
                      "sdk_update_bytes": write_bytes if write_stats_available and write_rows else None,
                      "sdk_update_rows": write_rows if write_stats_available and write_rows else None,
                      "result_sha256": checksum},
                     sort_keys=True), flush=True)
    return result


def seed_events(db: Database, days: int) -> datetime:
    start = datetime(2015, 1, 1, tzinfo=UTC)
    event_type = (ydb.StructType().add_member('device_id', ydb.PrimitiveType.Utf8)
                  .add_member('timestamp_utc', ydb.PrimitiveType.Int64).add_member('id', ydb.PrimitiveType.Utf8)
                  .add_member('payload', ydb.PrimitiveType.Utf8))
    marker_type = ydb.StructType().add_member('key', ydb.PrimitiveType.Utf8).add_member('value', ydb.PrimitiveType.Utf8)
    for first in range(0, days, 25):
        events, markers = [], []
        for day in range(first, min(first + 25, days)):
            at = start + timedelta(days=day)
            markers.append({'key': f'telemetry-day:{at.date().isoformat()}', 'value': f'fixture:{day}'})
            for hour in range(24):
                event = SourceEvent(id=f'{day}:{hour}', device_id='fixture', event_type='connected',
                                    timestamp_utc=at + timedelta(hours=hour), details={'fixture': 'x' * 100})
                events.append({'device_id': 'fixture', 'timestamp_utc': int(event.timestamp_utc.timestamp()),
                               'id': event.id, 'payload': event.model_dump_json()})
        db.storage.execute(
            'DECLARE $events AS List<Struct<device_id:Utf8,timestamp_utc:Int64,id:Utf8,payload:Utf8>>; '
            'DECLARE $markers AS List<Struct<key:Utf8,value:Utf8>>; '
            'UPSERT INTO source_events SELECT * FROM AS_TABLE($events); '
            'UPSERT INTO app_meta SELECT * FROM AS_TABLE($markers);',
            {'$events': ydb.TypedValue(events, ydb.ListType(event_type)),
             '$markers': ydb.TypedValue(markers, ydb.ListType(marker_type))},
        )
    return start + timedelta(days=days)


def seed_month_samples(db: Database, start: datetime) -> None:
    """A month of minute samples; input writes are outside measured reads."""
    series_id = db.list_series()[0]['id']
    row_type = (ydb.StructType().add_member('series_id', ydb.PrimitiveType.Int64)
                .add_member('timestamp_utc', ydb.PrimitiveType.Int64)
                .add_member('value_num', ydb.PrimitiveType.Double).add_member('quality', ydb.PrimitiveType.Utf8))
    for offset in range(0, 31 * 1440, 1000):
        rows = [{'series_id': series_id, 'timestamp_utc': int(start.timestamp()) + minute * 60,
                 'value_num': float(minute % 30), 'quality': 'valid'}
                for minute in range(offset, min(offset + 1000, 31 * 1440))]
        db.storage.execute(
            'DECLARE $rows AS List<Struct<series_id:Int64,timestamp_utc:Int64,value_num:Double,quality:Utf8>>; '
            'UPSERT INTO telemetry_samples SELECT * FROM AS_TABLE($rows);',
            {'$rows': ydb.TypedValue(rows, ydb.ListType(row_type))},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-db', type=Path, required=True)
    parser.add_argument('--baseline-monitoring', type=Path, required=True)
    parser.add_argument('--baseline-scheduler', type=Path)
    parser.add_argument('--label', default='baseline')
    parser.add_argument('--years', type=int, nargs='+', default=[1, 5, 10])
    args = parser.parse_args()
    endpoint = os.environ['YDB_TEST_ENDPOINT']
    if not endpoint.startswith('grpc://zont-steady-cost-ydb:'):
        raise ValueError('benchmark requires its isolated local Docker YDB')
    import zont_analyzer.adapters.ydb.application as application
    old_revision = baseline(args.baseline_db, 'source_event_revision', vars(application), 'Database')
    old_snapshot = baseline(args.baseline_monitoring, '_snapshot', vars(monitoring))
    db = Database(YdbConfig(endpoint, '/local', 'steady_cost', True))
    try:
        db.initialize()
        db.save_devices([{'id': 'fixture'}])
        # This namespace is owned by this disposable benchmark, never an app DB.
        db.storage.execute('DELETE FROM source_events; DELETE FROM app_meta '
                           "WHERE key >= 'telemetry-period-revision:' AND key < 'telemetry-period-revision;';")
        empty_end = datetime(2026, 1, 1, tzinfo=UTC)
        measure(db, 'empty-events-before', lambda: old_revision(db, empty_end))
        for years in args.years:
            end = seed_events(db, years * 365)
            measure(db, 'events-before', lambda end=end: old_revision(db, end), years=years)
        at = datetime(2026, 1, 1, tzinfo=UTC)
        points = [TelemetryPoint(device_id='fixture', entity_id=str(i), source_type='fixture',
                                 metric_key='temperature', timestamp_utc=at + timedelta(seconds=i),
                                 value_num=10.0) for i in range(64)]
        db.telemetry.write_window(device_id='fixture', data_type='fixture', start=at,
                                  end=at + timedelta(hours=1), points=points)
        runtime = SimpleNamespace(db=db, config=AppConfig())

        def snapshot(function: Any) -> list[Any]:
            events: list[Any] = []
            with capture(events.append):
                function(runtime)
            return [event for event in events if not event[0].startswith('zont_ydb_')]

        expected = measure(db, 'monitoring-before-64-series', lambda: snapshot(old_snapshot))
        if args.label != 'baseline':
            for attempt in range(3):
                actual = measure(db, 'monitoring-after-64-series', lambda: snapshot(monitoring._snapshot),
                                 attempt=attempt)
                assert actual == expected
        if args.baseline_scheduler:
            db.storage.execute('DELETE FROM source_events; DELETE FROM app_meta '
                               "WHERE key >= 'telemetry-period-revision:' AND key < 'telemetry-period-revision;';")
            seed_month_samples(db, at)
            analysis = AnalysisService(db, AppConfig())
            period = calendar_period('monthly', at.date(), 'UTC')
            import itertools

            old_needs = baseline(args.baseline_scheduler, 'period_needs_report', {
                **vars(scheduler), 'schedule_signature': schedule_signature, '_already_current': _already_current,
                'seasonal_daily_signature': seasonal_daily_signature,
                '_report_source_event_revision': _report_source_event_revision, 'itertools': itertools,
            })
            current_revision = db.source_event_revision
            db.source_event_revision = lambda end: old_revision(db, end)  # type: ignore[method-assign]
            try:
                expected = measure(db, 'missing-monthly-before', lambda: old_needs(analysis, period))
            finally:
                db.source_event_revision = current_revision  # type: ignore[method-assign]
            actual = measure(db, 'missing-monthly-after', lambda: scheduler.period_needs_report(analysis, period))
            assert actual == expected is True
    finally:
        db.close()


if __name__ == '__main__':
    main()

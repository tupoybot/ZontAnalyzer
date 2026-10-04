"""Compare exact PR #62 gas code with current code on disposable local YDB.

Run inside the repository's Docker test image against a disposable local YDB.
Supply the two baseline source files extracted from commit 3149665:

    git show 3149665:src/zont_analyzer/application/gas.py > /tmp/gas-3149665.py
    git show 3149665:src/zont_analyzer/adapters/ydb/application.py > /tmp/db-3149665.py

Mount those files read-only at the paths passed by --baseline-gas/--baseline-db.
The tool writes only to the configured disposable YDB namespace and stdout.
Results are JSON lines with local Query SDK FULL read statistics. They are not
managed YDB request units or a production cost estimate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import time
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import ydb  # type: ignore[import-untyped]

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import Transaction, YdbConfig, YdbDatabase
from zont_analyzer.application.gas import GasService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint


@dataclass
class Costs:
    queries: int = 0
    result_rows: int = 0
    result_json_bytes: int = 0
    sdk_read_rows: int = 0
    sdk_read_bytes: int = 0
    sdk_cpu_us: int = 0
    queries_with_stats: int = 0

    def add(self, results: list[Any], stats: Any) -> None:
        self.queries += 1
        for part in results:
            if part is None:
                continue
            for row in part.rows:
                self.result_rows += 1
                self.result_json_bytes += len(json.dumps(dict(row), default=str).encode())
        if stats is not None:
            self.queries_with_stats += 1
            self.sdk_cpu_us += int(stats.process_cpu_time_us)
            for phase in stats.query_phases:
                for access in phase.table_access:
                    self.sdk_read_rows += int(access.reads.rows)
                    self.sdk_read_bytes += int(access.reads.bytes)


class ObserveQueries:
    """Temporary SDK instrumentation; normal application code is untouched."""

    def __init__(self, storage: YdbDatabase):
        self.storage = storage
        self.costs = Costs()
        self._pool_execute = storage.pool.execute_with_retries
        self._tx_execute = Transaction.execute

    def __enter__(self) -> Costs:
        costs, pool = self.costs, self.storage.pool

        def pool_execute(query: str, parameters: dict[str, Any] | None = None,
                         retry_settings: Any = None, *args: Any, **kwargs: Any) -> list[Any]:
            def run(session: Any) -> list[Any]:
                with session.execute(query, parameters, *args, stats_mode=ydb.QueryStatsMode.FULL,
                                     **kwargs) as stream:
                    results = list(stream)
                costs.add(results, session.last_query_stats)
                return results

            return cast(list[Any], pool.retry_operation_sync(run, retry_settings))

        def tx_execute(tx: Transaction, query: str,
                       parameters: dict[str, Any] | None = None) -> list[Any]:
            with tx.raw.execute(tx.prefix + query, parameters=parameters,
                                stats_mode=ydb.QueryStatsMode.FULL) as stream:
                parts = []
                for part in stream:
                    if part is None:
                        continue
                    if part.truncated:
                        raise ValueError('YDB returned a truncated result')
                    parts.append(part)
            costs.add(parts, tx.raw.last_query_stats)
            merged: dict[int, Any] = {}
            for part in parts:
                index = int(part.index or 0)
                if index in merged:
                    merged[index].rows.extend(part.rows)
                else:
                    merged[index] = part
            return [merged[index] for index in sorted(merged)]

        pool.execute_with_retries = pool_execute
        Transaction.execute = tx_execute  # type: ignore[assignment]
        return costs

    def __exit__(self, *_args: object) -> None:
        self.storage.pool.execute_with_retries = self._pool_execute
        Transaction.execute = self._tx_execute  # type: ignore[method-assign]


def _baseline(gas_path: Path, db_path: Path) -> tuple[type, Any]:
    module = types.ModuleType('zont_analyzer.application._baseline_gas_benchmark')
    module.__package__ = 'zont_analyzer.application'
    exec(compile(gas_path.read_text(), str(gas_path), 'exec'), module.__dict__)
    tree = ast.parse(db_path.read_text())
    database = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Database')
    old_revision = next(node for node in database.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'period_data_revision')
    import zont_analyzer.adapters.ydb.application as current_db

    scope = vars(current_db).copy()
    exec(compile(ast.Module(body=[old_revision], type_ignores=[]), str(db_path), 'exec'), scope)
    return module.GasService, scope['period_data_revision']


def _seed(db: Database, start: datetime, end: datetime) -> int:
    db.save_devices([{'id': '1', 'name': 'synthetic boiler'}])
    points: list[TelemetryPoint] = []
    cursor = start
    while cursor <= end + timedelta(days=1):
        hour = cursor + timedelta(hours=12)
        points.extend((
            TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                           metric_key='s', timestamp_utc=hour, value_text="['fl', 'ch']"),
            TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                           metric_key='rml', timestamp_utc=hour, value_num=35.0),
            TelemetryPoint(device_id='1', entity_id='outdoor', source_type='z3k_boiler_adapter',
                           metric_key='temperature', timestamp_utc=hour, value_num=-3.0),
        ))
        cursor += timedelta(days=1)
    for offset in range(0, len(points), 1500):
        page = points[offset:offset + 1500]
        db.telemetry.write_window(
            device_id='1', data_type='fixture-samples',
            start=page[0].timestamp_utc - timedelta(seconds=1),
            end=page[-1].timestamp_utc + timedelta(seconds=1),
            points=page, roles={'outdoor': 'outdoor_temperature'},
        )
    return len(points)


def _clear_derived(db: Database) -> None:
    rows = db.storage.execute('SELECT key FROM app_meta;')[0].rows
    keys = [str(row.key) for row in rows if str(row.key).startswith((
        'gas-exposure:', 'gas-model:', 'telemetry-period-revision:v2:',
    ))]
    for offset in range(0, len(keys), 128):
        selected = keys[offset:offset + 128]
        db.storage.execute(
            'DECLARE $keys AS List<Utf8>; DELETE FROM app_meta WHERE key IN $keys;',
            {'$keys': ydb.TypedValue(selected, ydb.ListType(ydb.PrimitiveType.Utf8))},
        )


def _context(service_type: type, db: Database, config: AppConfig,
             start: datetime, end: datetime) -> dict[str, Any]:
    service = service_type(db, config)
    service.readings = [
        {'id': 'first', 'day': start.date().isoformat(), 'value_m3': '100',
         'segment': 0, 'updated_at': '2026-01-01 00:00:00'},
        {'id': 'last', 'day': end.date().isoformat(),
         'value_m3': str(100 + (end - start).days * 0.5),
         'segment': 0, 'updated_at': '2026-01-01 00:00:00'},
    ]
    result = service.context(start, end, include_daily=False)
    result['savings_status'] = service.savings(end)['status']
    return cast(dict[str, Any], result)


def _run(service_type: type, db: Database, config: AppConfig,
         start: datetime, end: datetime, *, label: str, years: int) -> tuple[dict[str, Any], dict[str, Any]]:
    with ObserveQueries(db.storage) as cost:
        begun = time.perf_counter()
        result = _context(service_type, db, config, start, end)
        elapsed = time.perf_counter() - begun
    semantic = {key: result.get(key) for key in (
        'status', 'volume_m3', 'lower_m3', 'upper_m3', 'coverage_pct', 'model_version',
        'measured_intervals', 'purpose_split', 'savings_status',
    )}
    record = {
        'years': years, 'run': label, 'elapsed_seconds': round(elapsed, 3),
        'queries': cost.queries, 'result_rows': cost.result_rows,
        'result_json_bytes': cost.result_json_bytes,
        'sdk_read_rows': cost.sdk_read_rows, 'sdk_read_bytes': cost.sdk_read_bytes,
        'sdk_cpu_us': cost.sdk_cpu_us, 'queries_with_stats': cost.queries_with_stats,
        'semantic_sha256': hashlib.sha256(json.dumps(semantic, sort_keys=True,
                                                   default=str).encode()).hexdigest(),
        'model_identifiable': result['model']['identifiable'], 'gas_status': result['status'],
        'volume_m3': result['volume_m3'],
    }
    print(json.dumps(record, sort_keys=True), flush=True)
    return record, semantic


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-gas', type=Path, required=True)
    parser.add_argument('--baseline-db', type=Path, required=True)
    parser.add_argument('--endpoint', default='grpc://ydb:2136')
    parser.add_argument('--database', default='/local')
    parser.add_argument('--namespace', default='gas_cost_benchmark')
    parser.add_argument('--years', type=int, nargs='+', default=[1, 5, 10])
    parser.add_argument('--candidate-only', action='store_true',
                        help='rerun only the current candidate after baseline evidence was captured')
    args = parser.parse_args()
    if any(year not in {1, 5, 10} for year in args.years):
        parser.error('years must be selected from 1, 5, 10')
    if args.endpoint != 'grpc://ydb:2136' or args.database != '/local':
        parser.error('benchmark requires the disposable local YDB endpoint and /local database')
    old_service, old_revision = _baseline(args.baseline_gas, args.baseline_db)
    storage = YdbDatabase(YdbConfig(args.endpoint, args.database, args.namespace, True))
    try:
        storage.initialize()
        db = Database(storage)
        config = AppConfig()
        config.home.timezone = 'UTC'
        config.analysis.modulation_capability_profile = 'flame_zero_is_minimum'
        start = datetime(2015, 1, 1, tzinfo=UTC)
        furthest = start.replace(year=start.year + max(args.years))
        source_hash = hashlib.sha256()
        for path in (Path('src/zont_analyzer/application/gas.py'),
                     Path('src/zont_analyzer/adapters/ydb/application.py'),
                     Path('src/zont_analyzer/adapters/ydb/period_revisions.py')):
            source_hash.update(path.read_bytes())
        print(json.dumps({'fixture_points': _seed(db, start, furthest),
                          'baseline_commit': '3149665', 'storage': 'local YDB emulator',
                          'candidate_source_sha256': source_hash.hexdigest()}), flush=True)
        current_revision = db.period_data_revision
        for years in args.years:
            end = start.replace(year=start.year + years)
            _clear_derived(db)
            if not args.candidate_only:
                db.period_data_revision = types.MethodType(old_revision, db)  # type: ignore[method-assign]
                old_cold, old_semantic = _run(old_service, db, config, start, end,
                                              label='baseline_cold', years=years)
                _run(old_service, db, config, start, end, label='baseline_warm', years=years)
                _clear_derived(db)
                db.period_data_revision = current_revision  # type: ignore[method-assign]
            new_cold, new_semantic = _run(GasService, db, config, start, end,
                                          label='candidate_cold', years=years)
            _run(GasService, db, config, start, end, label='candidate_warm', years=years)
            if not args.candidate_only:
                if old_semantic != new_semantic:
                    raise AssertionError(f'gas calculation changed for {years} years')
                print(json.dumps({'years': years, 'equivalent': True,
                                  'cold_query_delta': new_cold['queries'] - old_cold['queries']}), flush=True)
    finally:
        storage.close()


if __name__ == '__main__':
    main()

"""Bounded gas-period acceptance against raw full-period integration on local YDB.

Run in the Docker test image. Writes only the disposable namespace and JSON lines
to stdout. The oracle never calls GasService or reads its derived snapshots.
Shared fit/estimate formulas are intentional: this verifies cache/aggregation
equivalence, not independent validation of the physical calibration algorithm.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.analytics.gas import (
    Exposure,
    GasInterval,
    StateSample,
    estimate_exposure,
    estimate_gas_purpose_split,
    fit_intervals,
    integrate_exposure,
)
from zont_analyzer.application.gas import GasService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2025, 9, 30, tzinfo=UTC)
END = datetime(2026, 4, 2, tzinfo=UTC)
TTL = timedelta(minutes=15)


def local(day: str, hour: int = 0) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(hour), ZONE).astimezone(UTC)


def same(actual: Any, expected: Any, path: str = '') -> None:
    """Compare structure exactly and numerical roundoff explicitly."""
    if isinstance(expected, dict):
        assert set(actual) == set(expected), path
        for key in expected:
            same(actual[key], expected[key], f'{path}.{key}')
    elif isinstance(expected, (tuple, list)):
        assert len(actual) == len(expected), path
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            same(left, right, f'{path}[{index}]')
    elif isinstance(expected, (float, int)) and not isinstance(expected, bool):
        assert actual is not None and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-7), (
            path, actual, expected,
        )
    else:
        assert actual == expected, (path, actual, expected)


def fixture() -> list[TelemetryPoint]:
    points = []
    cursor = START
    while cursor <= END:
        gap = (local('2025-10-25') <= cursor < local('2025-10-25', 6)
               or local('2026-02-15') <= cursor < local('2026-02-15', 6))
        if not gap:
            # Four modulation bins, heating/DHW, explicit off states and long gaps.
            block = cursor.hour // 6
            flags = "[]" if cursor.hour == 18 else ("['fl', 'dhw']" if block == 1 else "['fl', 'ch']")
            points.extend([
                TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                               metric_key='s', timestamp_utc=cursor, value_text=flags),
                TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                               metric_key='rml', timestamp_utc=cursor, value_num=[10., 35., 65., 85.][block]),
            ])
        cursor += TTL
    return points


def write(db: Database, points: list[TelemetryPoint]) -> None:
    for offset in range(0, len(points), 1000):
        page = points[offset:offset + 1000]
        db.telemetry.write_window(device_id='1', data_type='period-acceptance',
                                  start=page[0].timestamp_utc - timedelta(seconds=1),
                                  end=page[-1].timestamp_utc + timedelta(seconds=1), points=page)


def raw_samples(db: Database) -> list[StateSample]:
    """Read the entire fixture directly, without exposure/model/cache helpers."""
    series = {row['metric_key']: int(row['id']) for row in db.list_series()}
    states = db.fetch_text_samples(series['s'], START, END + TTL)
    modulation = dict(db.fetch_samples(series['rml'], START, END + TTL))
    result = []
    for moment, encoded in states:
        flags = ast.literal_eval(encoded)
        result.append(StateSample(moment, 'fl' in flags, modulation[moment],
                                  'dhw' in flags, 'ch' in flags))
    return result


def exposure(samples: list[StateSample], start: datetime, end: datetime) -> Exposure:
    # The low-level integrator treats long source intervals as wholly unknown.
    # Encode the known 15-minute lifetime explicitly before integrating the full
    # raw period; missing history after that marker stays unknown, never off.
    cuts = []
    for index, point in enumerate(samples):
        cuts.append(point)
        next_at = samples[index+1].timestamp if index+1 < len(samples) else END + TTL*2
        if next_at > point.timestamp + TTL:
            cuts.append(StateSample(point.timestamp + TTL, None))
    integrated = integrate_exposure(start, end, cuts)
    # Independent closed-form overlap sums for this synchronized raw fixture.
    # No daily splitting, cache keys, revision marks or application methods.
    fields = asdict(Exposure((end-start).total_seconds()/60, (0.,)*4, 0., 0.))
    for key in ('heating_bin_minutes', 'dhw_bin_minutes', 'ambiguous_purpose_bin_minutes'):
        fields[key] = [0.] * 4
    fields['bin_minutes'] = [0.] * 4
    for index, point in enumerate(samples):
        stop = min(end, point.timestamp + TTL,
                   samples[index+1].timestamp if index+1 < len(samples) else end)
        minutes = max(0., (stop-max(start, point.timestamp)).total_seconds()/60)
        fields['observed_minutes'] += minutes
        if not point.fl:
            continue
        assert point.modulation is not None
        bin_index = min(3, int(point.modulation // 25))
        fields['bin_minutes'][bin_index] += minutes
        fields['flame_minutes'] += minutes
        purpose = 'dhw' if point.dhw else 'heating'
        fields[f'{purpose}_minutes'] += minutes
        fields[f'{purpose}_bin_minutes'][bin_index] += minutes
    same(asdict(integrated), fields, 'closed_form_exposure')
    return integrated


def readings() -> list[dict[str, Any]]:
    return [dict(id=str(index), day=day, value_m3=str(value), segment=segment,
                 updated_at='2026-01-01 00:00:00')
            for index, (day, value, segment) in enumerate([
                ('2025-11-01', 100, 'old'), ('2025-11-05', 196, 'old'),
                # Replacement starts above the old value: a mistaken cross-meter
                # subtraction would be positive and cannot hide behind reset checks.
                ('2025-11-09', 292, 'old'), ('2025-12-01', 500, 'replacement'),
                ('2025-12-05', 596, 'replacement'), ('2025-12-09', 692, 'replacement'),
            ])]


def oracle(samples: list[StateSample], rows: list[dict[str, Any]]) -> tuple[Any, list[GasInterval]]:
    intervals = []
    for first, last in zip(rows, rows[1:], strict=False):
        if first['segment'] != last['segment']:
            continue
        start, end = local(first['day'], 12), local(last['day'], 12)
        volume = float(last['value_m3']) - float(first['value_m3'])
        raw = exposure(samples, start, end)
        boundary = exposure(samples, start-timedelta(hours=12), start+timedelta(hours=12))
        after = exposure(samples, end-timedelta(hours=12), end+timedelta(hours=12))
        uncertainty = volume/raw.flame_minutes*(boundary.flame_minutes+after.flame_minutes)
        intervals.append(GasInterval(start, end, volume, raw.bin_minutes, raw.flame_minutes,
                                     raw.observed_minutes, raw.observed_minutes/raw.minutes,
                                     raw.unknown_modulation_minutes, uncertainty))
    assert len(intervals) == 4  # Never subtract readings across meter replacement.
    return fit_intervals(intervals, has_gas_stove=False), intervals


def service(db: Database, config: AppConfig, rows: list[dict[str, Any]]) -> GasService:
    result = GasService(db, config)
    result.readings = rows
    result.fields = {'has_gas_stove': {'value': False}}
    return result


def verify(db: Database, config: AppConfig, rows: list[dict[str, Any]], phase: str) -> str:
    samples = raw_samples(db)
    model, intervals = oracle(samples, rows)
    assert model.mean_rate_m3_per_minute is not None
    candidate = service(db, config, rows)
    version, cached_model, cached_intervals = candidate.model()
    same(asdict(cached_model), asdict(model), 'model')
    same([asdict(item) for item in cached_intervals], [asdict(item) for item in intervals], 'intervals')
    periods = [
        ('weekly_dst_fall', '2025-10-23', '2025-10-30', 169),
        ('weekly_dst_spring', '2026-03-26', '2026-04-02', 167),
        ('monthly', '2025-11-01', '2025-12-01', 720),
        ('season_cross_year', '2025-10-01', '2026-04-01', 4368),
    ]
    for name, first, last, hours in periods:
        start, end = local(first), local(last)
        assert (end-start).total_seconds()/3600 == hours, name
        raw = exposure(samples, start, end)
        window = candidate.window(start, end)
        same({key: window[key] for key in asdict(raw)}, asdict(raw), name+'.window')
        estimate = estimate_exposure(raw, model, start=start, end=end)
        assert estimate.volume_m3 is not None and estimate.uncertainty_m3 is not None, name
        context = candidate.context(start, end)
        expected = dict(status=estimate.status, volume_m3=estimate.volume_m3,
                        lower_m3=estimate.uncertainty_m3[0] if estimate.uncertainty_m3 else None,
                        upper_m3=estimate.uncertainty_m3[1] if estimate.uncertainty_m3 else None,
                        observed_volume_m3=estimate.observed_volume_m3,
                        coverage_pct=estimate.coverage*100, unknown_minutes=raw.minutes-raw.observed_minutes)
        same({key: context[key] for key in expected}, expected, name+'.estimate')
        split = estimate_gas_purpose_split(raw, model, estimate)
        actual_split = context['purpose_split']
        # Application adds tariff costs; this acceptance has no tariff fixture.
        actual_split = {key: value for key, value in actual_split.items()
                        if key not in {'cost', 'unallocated_cost'}}
        if isinstance(actual_split.get('components'), dict):
            actual_split['components'] = {key: {k: v for k, v in value.items() if k != 'cost'}
                                          for key, value in actual_split['components'].items()}
        same(actual_split, split, name+'.purpose')
        with patch.object(db, 'fetch_text_samples', side_effect=AssertionError('warm raw state read')), \
             patch.object(db, 'fetch_samples', side_effect=AssertionError('warm raw modulation read')), \
             patch('zont_analyzer.analytics.gas.fit_intervals', side_effect=AssertionError('warm model fit')):
            warm = service(db, config, rows)
            assert warm.model()[0] == version
            same(warm.window(start, end), window, name+'.warm')
        print(json.dumps(dict(case=name, phase=phase, hours=hours, raw_points=len(samples),
                             observed_minutes=raw.observed_minutes, status=context['status'],
                             volume_m3=context['volume_m3'], full_period_equal=True,
                             closed_form_exposure_equal=True, warm_reuse=True)), flush=True)
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='grpc://ydb:2136')
    parser.add_argument('--database', default='/local')
    parser.add_argument('--namespace', default='gas_period_acceptance')
    args = parser.parse_args()
    if args.endpoint != 'grpc://ydb:2136' or args.database != '/local':
        parser.error('requires the disposable local YDB endpoint grpc://ydb:2136 and /local')
    storage = YdbDatabase(YdbConfig(args.endpoint, args.database, args.namespace, True))
    try:
        storage.initialize()
        db = Database(storage)
        db.save_devices([{'id': '1', 'name': 'synthetic boiler'}])
        config = AppConfig()
        config.home.timezone = 'Europe/Berlin'
        config.analysis.modulation_capability_profile = 'flame_zero_is_minimum'
        points = fixture()
        write(db, points)
        rows = readings()
        before = verify(db, config, rows, 'initial')
        stale = service(db, config, rows)
        # Correct an actual calibrated raw sample and demand revision fencing.
        corrected = TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                                   metric_key='s', timestamp_utc=local('2025-11-03', 6), value_text='[]')
        write(db, [corrected])
        try:
            stale.context(local('2025-11-01'), local('2025-12-01'))
        except ValueError as error:
            assert 'inputs changed' in str(error)
        else:
            raise AssertionError('mixed input versions accepted')
        after = verify(db, config, rows, 'corrected_telemetry')
        assert before != after, 'full model version did not change'
        rows = [dict(row) for row in rows]
        rows[1]['value_m3'] = '208'
        final = verify(db, config, rows, 'corrected_reading')
        assert final != after, 'reading correction did not change full model version'
        print(json.dumps(dict(mutation_invalidated=True, stale_snapshot_rejected=True,
                              meter_replacement_intervals=4, external_calls=0)), flush=True)
    finally:
        storage.close()


if __name__ == '__main__':
    main()

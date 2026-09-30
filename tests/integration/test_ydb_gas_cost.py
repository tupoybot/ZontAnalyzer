"""Gas calculation reuse with real isolated YDB revisions and owner corrections."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.integration.test_stage8_gas import history
from tests.ydb_support import make_runtime, seed_samples
from zont_analyzer.analytics import gas as gas_analytics
from zont_analyzer.application.gas import GasService
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import TelemetryPoint


def _scalar_fingerprint(db, start: datetime, end: datetime) -> str:
    """Independent copy of the previous per-period content fingerprint."""
    digest = hashlib.sha256()
    count = 0
    for series in db.list_series():
        for row in db._samples(series['id'], start, end):
            digest.update(json.dumps([
                series['id'], row['timestamp_utc'], row['value_num'],
                row['value_text'], row['quality'],
            ], ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())
            digest.update(b'\n')
            count += 1
    return f'telemetry-v2:{digest.hexdigest()}' if count else hashlib.sha256(b'[]').hexdigest()


@pytest.mark.ydb
def test_batched_period_revisions_match_scalar_and_refresh_corrected_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def point(day: int, key: str, value: float) -> TelemetryPoint:
        return TelemetryPoint(
            device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
            metric_key=key, timestamp_utc=start + timedelta(days=day, hours=1), value_num=value,
        )

    initial = [point(day, key, value) for day in (0, 2, 9)
               for key, value in (('rml', 10.0), ('outdoor', -2.0))]
    seed_samples(runtime.db, initial)
    windows = [
        (start, start + timedelta(days=3)),
        (start + timedelta(hours=1), start + timedelta(days=10)),
        (start + timedelta(days=8), start + timedelta(days=11)),
        (start + timedelta(days=5), start + timedelta(days=6)),
    ]
    expected = {window: _scalar_fingerprint(runtime.db, *window) for window in windows}
    assert runtime.db.period_data_revisions(windows) == expected
    assert runtime.db.period_data_revisions(windows) == expected  # Durable warm path.

    seed_samples(runtime.db, [point(2, 'rml', 10.0)])
    assert runtime.db.period_data_revisions(windows) == expected  # Idempotent replay.
    seed_samples(runtime.db, [point(2, 'rml', 40.0)])
    changed = runtime.db.period_data_revisions(windows)
    assert changed == {window: _scalar_fingerprint(runtime.db, *window) for window in windows}
    assert changed[windows[0]] != expected[windows[0]]
    assert changed[windows[1]] != expected[windows[1]]
    assert changed[windows[2]] == expected[windows[2]]
    assert changed[windows[3]] == expected[windows[3]]

    # A write during a cold fingerprint must abort rather than persist stale bytes.
    cold = (start + timedelta(minutes=30), start + timedelta(days=10, minutes=30))
    original_samples = runtime.db._samples
    mutated = False

    def concurrent_samples(series_id: int, left: datetime, right: datetime):
        nonlocal mutated
        for row in original_samples(series_id, left, right):
            if not mutated:
                mutated = True
                seed_samples(runtime.db, [point(2, 'rml', 50.0)])
            yield row

    monkeypatch.setattr(runtime.db, '_samples', concurrent_samples)
    with pytest.raises(ValueError, match='inputs changed'):
        runtime.db.period_data_revisions([cold])
    monkeypatch.setattr(runtime.db, '_samples', original_samples)
    assert runtime.db.period_data_revisions([cold])[cold] == _scalar_fingerprint(runtime.db, *cold)


@pytest.mark.ydb
def test_mutation_during_exposure_does_not_poison_reverted_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(tmp_path)
    runtime.db.save_devices([{'id': '1'}])
    start = datetime(2026, 2, 1, 13, tzinfo=UTC)
    sample_time = start + timedelta(minutes=10)

    def state(flags: str) -> TelemetryPoint:
        return TelemetryPoint(
            device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
            metric_key='s', timestamp_utc=sample_time, value_text=flags,
        )

    seed_samples(runtime.db, [state("['fl', 'ch']")])
    original_fetch = runtime.db.fetch_text_samples
    original_meta = runtime.db.set_app_meta
    exposure_writes: list[str] = []
    mutated = False

    def mutate_before_fetch(series_id: int, left: datetime, right: datetime):
        nonlocal mutated
        if not mutated:
            mutated = True
            seed_samples(runtime.db, [state('[]')])
        return original_fetch(series_id, left, right)

    def track_meta(key: str, value: str) -> None:
        if key.startswith('gas-exposure:'):
            exposure_writes.append(key)
        original_meta(key, value)

    monkeypatch.setattr(runtime.db, 'fetch_text_samples', mutate_before_fetch)
    monkeypatch.setattr(runtime.db, 'set_app_meta', track_meta)
    with pytest.raises(ValueError, match='inputs changed'):
        GasService(runtime.db, runtime.config).window(start, start + timedelta(hours=1))
    assert exposure_writes == []

    monkeypatch.setattr(runtime.db, 'fetch_text_samples', original_fetch)
    seed_samples(runtime.db, [state("['fl', 'ch']")])
    assert GasService(runtime.db, runtime.config).window(
        start, start + timedelta(hours=1),
    )['flame_minutes'] == 15


@pytest.mark.ydb
def test_gas_reuses_calibration_and_batched_revisions_without_freezing_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, owner, reports = history(tmp_path)
    owner.update_gas(reports[0].id, {'value_m3': 100})
    owner.update_gas(reports[2].id, {'value_m3': 292})

    original_batch = runtime.db.period_data_revisions
    revision_batches: list[list[tuple]] = []

    def batch(windows: list[tuple]) -> dict:
        revision_batches.append(windows)
        return original_batch(windows)

    def scalar_forbidden(*args: object) -> str:
        raise AssertionError('A prefetched gas slice must not request its revision separately')

    monkeypatch.setattr(runtime.db, 'period_data_revisions', batch)
    monkeypatch.setattr(runtime.db, 'period_data_revision', scalar_forbidden)
    original_fit = gas_analytics.fit_intervals
    fit_calls = 0

    def fit_once(*args: object, **kwargs: object):
        nonlocal fit_calls
        fit_calls += 1
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(gas_analytics, 'fit_intervals', fit_once)
    service = GasService(runtime.db, runtime.config)
    first = service.context(*runtime.analysis(no_ai=True).local_day_window(date(2026, 1, 10)))
    published = reports[-1].model_copy(update={'summary': 'A later report write'})
    runtime.db.save_report(published, published.summary)
    second = service.context(*runtime.analysis(no_ai=True).local_day_window(date(2026, 1, 11)))
    assert first['model_version'] == second['model_version']
    assert first['volume_m3'] == second['volume_m3']
    assert fit_calls == 1
    assert len(revision_batches) == 3  # Calibration and the two distinct report days.
    assert len({window for batch_windows in revision_batches for window in batch_windows}) == sum(
        len(batch_windows) for batch_windows in revision_batches
    )

    seed_samples(runtime.db, [TelemetryPoint(
        device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
        metric_key='rml', timestamp_utc=datetime(2026, 1, 12, 12, 1, tzinfo=UTC), value_num=0.0,
    )])
    with pytest.raises(ValueError, match='inputs changed'):
        service.context(*runtime.analysis(no_ai=True).local_day_window(date(2026, 1, 12)))

    # The next job takes a fresh owner snapshot and recalibrates an old correction.
    owner.update_gas(reports[2].id, {'value_m3': 388})
    updated = GasService(runtime.db, runtime.config).context(
        *runtime.analysis(no_ai=True).local_day_window(date(2026, 1, 10))
    )
    assert updated['model_version'] != first['model_version']
    assert updated['volume_m3'] > first['volume_m3']

    original_readings = OwnerContextStore.gas_readings_for_analysis
    injected = False

    def concurrent_owner_edit(context: OwnerContextStore):
        nonlocal injected
        rows = original_readings(context)
        if not injected:
            injected = True
            owner.update_profile('1', {'fields': {'gas_min_m3h': {'value': 1.0}}})
        return rows

    monkeypatch.setattr(OwnerContextStore, 'gas_readings_for_analysis', concurrent_owner_edit)
    with pytest.raises(ValueError, match='snapshot was loaded'):
        GasService(runtime.db, runtime.config)

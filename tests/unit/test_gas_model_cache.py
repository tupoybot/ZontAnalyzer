"""Durable calibration snapshots preserve the complete typed model across jobs."""

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from zont_analyzer.analytics.gas import GasInterval
from zont_analyzer.application.gas import GasService


def service(metadata: dict[str, str]) -> GasService:
    # Exercise the real model/cache/fence path with already integrated intervals.
    # Telemetry integration and revision markers have separate YDB coverage.
    result = object.__new__(GasService)
    result.db = SimpleNamespace(  # type: ignore[assignment]
        get_app_meta=metadata.get, set_app_meta=metadata.__setitem__, gas_input_revision=lambda: 'stable',
    )
    result._input_revision = 'stable'
    result._calculation_depth = 0
    result._pending_exposures = {}
    result._model_results = {}
    result._models = {}
    result._intervals = {}
    result.timezone = 'UTC'
    result.state = None
    result.series = []
    result.config = SimpleNamespace(analysis=SimpleNamespace(modulation_capability_profile='unknown'))
    result.readings = []
    result.fields = {}
    result.device_id = None
    start = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = [GasInterval(start + timedelta(days=i), start + timedelta(days=i+1),
                            10.0+i, (60.0, 30.0, 20.0, 10.0), 120.0, 1440.0, 1.0)
                 for i in range(5)]
    result.intervals = lambda **kwargs: intervals  # type: ignore[method-assign]
    result._prefetch_revisions = lambda windows: None  # type: ignore[method-assign]
    return result


def test_model_snapshot_reused_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata: dict[str, str] = {}
    first = service(metadata).model()
    monkeypatch.setattr('zont_analyzer.analytics.gas.fit_intervals',
                        lambda *args, **kwargs: pytest.fail('refitted a valid durable snapshot'))
    second = service(metadata).model()
    assert second == first
    assert asdict(second[1]) == asdict(first[1])
    assert isinstance(second[1].rates_m3_per_minute, tuple)
    assert isinstance(second[1].reasons, tuple)
    assert second[1].calibration_end.tzinfo is not None


@pytest.mark.parametrize('damage', ['json', 'missing', 'type', 'version', 'inputs', 'datetime', 'nan'])
def test_invalid_model_snapshot_recomputed(damage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    metadata: dict[str, str] = {}
    first = service(metadata).model()
    key = 'gas-model:' + first[0]
    snapshot = json.loads(metadata[key])
    if damage == 'missing':
        del snapshot['model']['support_intervals']
    elif damage == 'type':
        snapshot['model']['rates_m3_per_minute'][0] = '0.1'
    elif damage == 'version':
        snapshot['model']['version'] = 'obsolete'
    elif damage == 'inputs':
        snapshot['intervals'][0]['volume_m3'] = 999
    elif damage == 'datetime':
        snapshot['model']['calibration_end'] = '2026-01-06T00:00:00'
    elif damage == 'nan':
        snapshot['model']['mean_rate_m3_per_minute'] = float('nan')
    metadata[key] = '{' if damage == 'json' else json.dumps(snapshot)
    from zont_analyzer.analytics import gas

    original = gas.fit_intervals
    calls = []

    def fit(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(gas, 'fit_intervals', fit)
    assert service(metadata).model() == first
    assert calls == [True]
    assert json.loads(metadata[key])['model'] == json.loads(json.dumps(asdict(first[1]), default=str))


def test_cached_model_still_checks_input_revision() -> None:
    metadata: dict[str, str] = {}
    service(metadata).model()
    fresh = service(metadata)
    fresh.db.gas_input_revision = lambda: 'changed'  # type: ignore[method-assign]
    with pytest.raises(ValueError, match='inputs changed'):
        fresh.model()

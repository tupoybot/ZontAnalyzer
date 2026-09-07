from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.analytics.evidence import (
    NumericSample,
    SignalMetadata,
    SignalSeries,
    _derived_statistic,
    _statistic,
)
from zont_analyzer.domain import TelemetryPoint

START = datetime(2026, 9, 6, tzinfo=UTC)


def signal(kind, offsets, *, unknown=()):
    return SignalSeries(
        "target", SignalMetadata("source", "Target", "°C", value_kind=kind),
        tuple(NumericSample(START + timedelta(minutes=m), v) for m, v in offsets),
        tuple(START + timedelta(minutes=m) for m in unknown),
    )


def test_constant_setpoint_is_not_an_expired_or_stuck_measurement():
    end = START + timedelta(hours=24)
    sparse = signal("setpoint", [(0, 22)])
    dense = signal("setpoint", [(m, 22) for m in range(0, 1440, 3) if not 220 < m < 278])
    for s in [sparse, dense]:
        stat = _statistic(s, START, end)
        assert stat.coverage_pct == 100
        assert stat.mean == 22
        assert stat.stale_seconds is None
    measured = signal("measurement", [(m, 22) for m in range(0, 1440, 3) if not 220 < m < 278])
    assert _statistic(measured, START, end).coverage_pct < 100


def test_explicit_unknown_stops_setpoint_and_derived_temperature_error():
    end = START + timedelta(hours=4)
    target = signal("setpoint", [(0, 22), (180, 24)], unknown=[60])
    measured = signal("measurement", [(m, 25) for m in range(0, 241)])
    stat = _statistic(target, START, end)
    assert stat.coverage_pct == 50
    assert stat.mean == 23
    error = _derived_statistic(measured, target, START, end, lambda a, b: a - b)
    assert error is not None
    assert error.coverage_pct == 50
    assert error.mean == 2
    assert _statistic(target, START + timedelta(hours=1), START + timedelta(hours=3)).mean is None


def test_no_setpoint_is_invented_before_first_known_state():
    target = signal("setpoint", [(60, 22)])
    assert _statistic(target, START, START + timedelta(hours=2)).coverage_pct == 50


def test_database_keeps_unknown_state_and_previous_command(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    db.upsert_samples([
        TelemetryPoint(device_id="d", source_type="z3k_heating_circuit", entity_id="h", metric_key="target_temp",
                       timestamp_utc=START + timedelta(hours=h), value_num=value,
                       quality="invalid" if value is None else "valid")
        for h, value in [(0, 22), (1, None), (3, 24)]
    ])
    series_id = db.list_series()[0]["id"]
    assert db.fetch_numeric_observations(series_id, START + timedelta(hours=2), START + timedelta(hours=4),
                                         include_previous=True) == [
        (START + timedelta(hours=1), None), (START + timedelta(hours=3), 24),
    ]
    assert db.fetch_samples(series_id, START, START + timedelta(hours=4)) == [
        (START, 22), (START + timedelta(hours=3), 24),
    ]


@pytest.mark.parametrize("source,key,expected", [
    ("z3k_heating_circuit", "target_temp", True),
    ("z3k_heating_circuit", "setpoint_temp", True),
    ("z3k_boiler_adapter", "cs", True),
    ("z3k_boiler_adapter", "dt", False),
    ("z3k_radio_sensor", "temperature", False),
])
def test_source_contract_distinguishes_setpoints_from_readings(source, key, expected):
    from zont_analyzer.analytics.series_semantics import is_setpoint_series
    assert is_setpoint_series(source, key) is expected

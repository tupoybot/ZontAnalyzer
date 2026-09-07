from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application import publication
from zont_analyzer.domain import QualityResult, Report, TelemetryPoint
from zont_analyzer.reports.chart_data import MAX_POINTS_PER_SERIES, _point_dicts, build_chart_data, cached_chart_data
from zont_analyzer.runtime import build_runtime


def _report(context: dict) -> Report:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return Report(
        id="report:daily:chart-data:report-v2",
        kind="daily",
        period_start=start,
        period_end=start + timedelta(days=2),
        generated_at=start + timedelta(days=2),
        timezone="UTC",
        context=context,
        quality=QualityResult(
            score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0, implausible_jumps=0, sample_count=1,
        ),
        summary="Отчёт",
    )


def test_chart_data_reuses_report_selected_series_and_keeps_observed_gap(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    selected = [
        TelemetryPoint(
            device_id="device-a", source_type="sensor", entity_id="room-a", metric_key="temperature",
            timestamp_utc=start + timedelta(minutes=index if index < 500 else index + 240),
            value_num=20 + (index % 11), unit="°C",
        )
        for index in range(960)
    ]
    other = [
        TelemetryPoint(
            device_id="device-b", source_type="sensor", entity_id="room-b", metric_key="temperature",
            timestamp_utc=start + timedelta(minutes=index), value_num=99, unit="°C",
        )
        for index in range(3)
    ]
    db.upsert_samples(
        [*selected, *other], {"room-a": "control_indoor_temperature", "room-b": "control_indoor_temperature"},
    )
    report = _report({"temporal_evidence": {"signals": {
        "control": {
            "role": "control_temperature",
            "identity": "device-a/sensor/room-a/temperature",
        },
    }}})

    packet = build_chart_data(db, report)

    assert packet is not None
    points = packet["series"]["control_temperature"]["points"]
    assert 2 <= len(points) <= MAX_POINTS_PER_SERIES
    assert {item["value"] for item in points} != {99.0}
    timestamps = [datetime.fromisoformat(item["timestamp"]) for item in points]
    assert any((right - left) >= timedelta(hours=4) for left, right in zip(timestamps, timestamps[1:], strict=False))
    assert any(item.get("gap_before") is True for item in points)


def test_chart_data_exposes_explicit_ch_dhw_and_concurrent_state_bands(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    db.upsert_samples([
        TelemetryPoint(
            device_id="device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start, value_text="['ch', 'fl']",
        ),
        TelemetryPoint(
            device_id="device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start + timedelta(minutes=5), value_text="['dhw', 'fl']",
        ),
        TelemetryPoint(
            device_id="device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start + timedelta(minutes=10), value_text="['ch', 'dhw', 'fl']",
        ),
        TelemetryPoint(
            device_id="device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start + timedelta(minutes=15), value_text="[]",
        ),
    ])

    packet = build_chart_data(db, _report({}))

    assert packet is not None
    assert packet["series"] == {}
    assert [band["state"] for band in packet["state_bands"]] == ["ch", "dhw", "concurrent"]


def test_setpoint_holds_across_scheduler_gap_but_explicit_invalid_breaks_line() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    held = _point_dicts([
        (start, 22.0),
        (start + timedelta(hours=4), 22.0),
    ], stateful=True)
    assert not any(point.get("gap_before") for point in held)

    invalid = _point_dicts([
        (start, 22.0),
        (start + timedelta(hours=1), None),
        (start + timedelta(hours=2), 22.0),
    ], stateful=True)
    assert invalid[-1]["gap_before"] is True


def test_decimation_keeps_both_edges_of_setpoint_change() -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    points = [(start + timedelta(minutes=index), float(20 if index < 500 else 22)) for index in range(1000)]
    decimated = _point_dicts(points, stateful=True)
    timestamps = {datetime.fromisoformat(point["timestamp"]) for point in decimated}
    assert start + timedelta(minutes=499) in timestamps
    assert start + timedelta(minutes=500) in timestamps


def test_publication_passes_chart_packet_to_archive_renderer(tmp_path) -> None:
    runtime = build_runtime(None, tmp_path)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    runtime.db.upsert_samples([
        TelemetryPoint(
            device_id="device", source_type="sensor", entity_id="room", metric_key="temperature",
            timestamp_utc=start + timedelta(hours=index), value_num=20 + index / 10, unit="°C",
        )
        for index in range(3)
    ], {"room": "control_indoor_temperature"})
    report = _report({"temporal_evidence": {"signals": {
        "control": {"role": "control_temperature", "identity": "device/sensor/room/temperature"},
    }}})
    runtime.db.save_report(report, "canonical report is unchanged")

    publication.publish_reports(runtime, now=start + timedelta(days=3))

    rendered = (tmp_path / "reports" / "daily" / "2026-09-01.html").read_text(encoding="utf-8")
    assert 'data-chart="climate"' in rendered
    assert "Контрольная температура" in rendered


def test_chart_data_cache_reuses_canonical_report_and_invalidates_on_change(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    db.upsert_samples([
        TelemetryPoint(
            device_id="device", source_type="sensor", entity_id="room", metric_key="temperature",
            timestamp_utc=start + timedelta(hours=index), value_num=20 + index / 10, unit="°C",
        )
        for index in range(3)
    ], {"room": "control_indoor_temperature"})
    report = _report({"temporal_evidence": {"signals": {
        "control": {"role": "control_temperature", "identity": "device/sensor/room/temperature"},
    }}})
    original_fetch = db.fetch_numeric_observations
    calls: list[int] = []

    def counting_fetch(*args, **kwargs):
        calls.append(1)
        return original_fetch(*args, **kwargs)

    monkeypatch.setattr(db, "fetch_numeric_observations", counting_fetch)

    assert cached_chart_data(db, report) is not None
    assert len(calls) == 1
    assert cached_chart_data(db, report) is not None
    assert len(calls) == 1
    assert cached_chart_data(db, report.model_copy(update={"summary": "Пересчитанный отчёт"})) is not None
    assert len(calls) == 2
    assert len(list((tmp_path / "chart-data-cache").glob("*.json"))) == 1


def test_held_setpoint_spans_report_but_stops_at_explicit_unknown(tmp_path):
    db = Database(tmp_path / 'state.sqlite3')
    db.initialize()
    report = _report({})
    start = report.period_start
    db.upsert_samples([
        TelemetryPoint(device_id='d', source_type='z3k_heating_circuit', entity_id='h', metric_key='target_temp',
                       timestamp_utc=start - timedelta(days=1), value_num=22),
    ], {'h': 'target_temperature'})
    packet = build_chart_data(db, report)
    points = packet['series']['target_temperature']['points']
    assert points[0]['timestamp'] == start.isoformat()
    assert points[-1]['timestamp'] == report.period_end.isoformat()
    assert all(p['value'] == 22 for p in points)
    unknown = start + timedelta(hours=3)
    db.upsert_samples([
        TelemetryPoint(device_id='d', source_type='z3k_heating_circuit', entity_id='h', metric_key='target_temp',
                       timestamp_utc=unknown, value_num=None, quality='invalid'),
    ])
    packet = build_chart_data(db, report)
    points = packet['series']['target_temperature']['points']
    assert points[-1]['timestamp'] == unknown.isoformat()
    assert points[-1]['value'] == 22

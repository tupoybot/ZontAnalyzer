from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.ydb_support import make_database, seed_samples
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.application import analysis as analysis_module
from zont_analyzer.application import comparison_context
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, TelemetryPoint
from zont_analyzer.reports import render_html, render_text


class CaptureAnalyst:
    packet: dict[str, Any]

    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        self.packet = packet
        return AnalysisResult(summary="Проверены временные свидетельства.")


@pytest.mark.ydb
def test_daily_evidence_reuses_exact_and_superset_sample_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db: Database = make_database(tmp_path)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for entity, key, role, base, source in (
        ("living", "temperature", "control_indoor_temperature", 21.0, "synthetic"),
        ("circuit", "target_temp", "target_temperature", 20.0, "z3k_heating_circuit"),
        ("circuit", "mode_id", "operating_mode", 1.0, "z3k_heating_circuit"),
        ("circuit", "status", "unknown", 1.0, "z3k_heating_circuit"),
        ("boiler", "status_flags", "unknown", 0.0, "ztc_state"),
        ("outdoor", "temperature", "outdoor_temperature", 5.0, "synthetic"),
        ("boiler", "bt", "flow_temperature", 40.0, "z3k_boiler_adapter"),
        ("boiler", "cs", "target_flow_temperature", 38.0, "z3k_boiler_adapter"),
        ("return", "temperature", "return_temperature", 30.0, "synthetic"),
        ("other-room", "temperature", "room_temperature", 19.0, "synthetic"),
    ):
        points = [TelemetryPoint(
            device_id="test-device", source_type=source, entity_id=entity, metric_key=key,
            timestamp_utc=start + timedelta(minutes=5 * index), value_num=base + (index % 2) * 0.1, unit="°C",
        ) for index in range(288)]
        seed_samples(db, points, {entity: role})
        row = next(item for item in db.list_series() if item["entity_id"] == entity and item["metric_key"] == key)
        db.update_series_role(row["id"], role, f"Датчик {entity}", provenance="test fixture")
    seed_samples(db, [TelemetryPoint(
        device_id="test-device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
        timestamp_utc=start + timedelta(minutes=5 * index), value_text="['ch']",
    ) for index in range(288)], {"boiler": "state"})
    # Context rows just outside the period must stay out of period-only detection.
    seed_samples(db, [
        TelemetryPoint(
            device_id="test-device", source_type="z3k_heating_circuit", entity_id="circuit",
            metric_key="mode_id", timestamp_utc=start - timedelta(minutes=5), value_num=99.0,
        ),
        TelemetryPoint(
            device_id="test-device", source_type="z3k_heating_circuit", entity_id="circuit",
            metric_key="mode_id", timestamp_utc=start + timedelta(days=1), value_num=98.0,
        ),
    ], {"circuit": "operating_mode"})
    seed_samples(db, [
        TelemetryPoint(
            device_id="test-device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start - timedelta(minutes=5), value_text="['dhw']",
        ),
        TelemetryPoint(
            device_id="test-device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
            timestamp_utc=start + timedelta(days=1), value_text="['dhw']",
        ),
    ], {"boiler": "state"})
    series_by_identity = {
        (item["entity_id"], item["metric_key"]): item for item in db.list_series()
    }
    control_id = int(series_by_identity[("living", "temperature")]["id"])
    mode_id = int(series_by_identity[("circuit", "mode_id")]["id"])
    state_id = int(series_by_identity[("boiler", "s")]["id"])
    status_id = int(series_by_identity[("boiler", "status_flags")]["id"])
    sample_reads: list[tuple[int, datetime, datetime]] = []
    text_reads: list[tuple[int, datetime, datetime]] = []
    observed_modes: list[list[tuple[datetime, float]]] = []
    observed_states: list[list[tuple[datetime, str]]] = []
    device_timestamp_reads: list[tuple[str, datetime, datetime]] = []
    boiler_timestamp_reads: list[tuple[int, datetime, datetime]] = []
    reliability_inputs: list[tuple[datetime, list[datetime]]] = []
    fetch_samples = db.fetch_samples
    fetch_text_samples = db.fetch_text_samples
    fetch_device_timestamps = db.fetch_device_sample_timestamps
    fetch_series_timestamps = db.fetch_sample_timestamps
    analyze_reliability = analysis_module.analyze_reliability

    def track_samples(series_id: int, left: datetime, right: datetime):
        if series_id in {control_id, mode_id, status_id}:
            sample_reads.append((series_id, left, right))
        return fetch_samples(series_id, left, right)

    def track_text_samples(series_id: int, left: datetime, right: datetime):
        if series_id == state_id:
            text_reads.append((series_id, left, right))
        return fetch_text_samples(series_id, left, right)

    def track_device_timestamps(device_id: str, left: datetime, right: datetime):
        device_timestamp_reads.append((device_id, left, right))
        return fetch_device_timestamps(device_id, left, right)

    def track_series_timestamps(series_id: int, left: datetime, right: datetime):
        if series_id == state_id:
            boiler_timestamp_reads.append((series_id, left, right))
        return fetch_series_timestamps(series_id, left, right)

    detect_control = analysis_module.detect_control_context
    detect_flame_noise = analysis_module.detect_unconfirmed_burner_pulses

    def track_control_context(**kwargs):
        observed_modes.append(kwargs["mode_samples"])
        return detect_control(**kwargs)

    def track_flame_noise(**kwargs):
        observed_states.append(kwargs["boiler_state_samples"])
        return detect_flame_noise(**kwargs)

    def track_reliability(**kwargs):
        reliability_inputs.append((kwargs["as_of"], kwargs["zont_metric_timestamps"]))
        return analyze_reliability(**kwargs)

    def run_comparison_windows(db_arg, report, period, *, boundaries, analyze_window):
        del db_arg, report, boundaries
        windows = (
            (period.start - timedelta(minutes=5), period.start + timedelta(hours=1)),
            (period.start + timedelta(hours=1), period.observed_end),
            (period.start - timedelta(minutes=10), period.start + timedelta(hours=1)),
            (period.start + timedelta(hours=1), period.observed_end + timedelta(minutes=1)),
        )
        for left, right in windows:
            analyze_window(left, right)
        return {"comparison_probe": len(windows)}

    monkeypatch.setattr(db, "fetch_samples", track_samples)
    monkeypatch.setattr(db, "fetch_text_samples", track_text_samples)
    monkeypatch.setattr(db, "fetch_device_sample_timestamps", track_device_timestamps)
    monkeypatch.setattr(db, "fetch_sample_timestamps", track_series_timestamps)
    monkeypatch.setattr(analysis_module, "detect_control_context", track_control_context)
    monkeypatch.setattr(analysis_module, "detect_unconfirmed_burner_pulses", track_flame_noise)
    monkeypatch.setattr(analysis_module, "analyze_reliability", track_reliability)
    monkeypatch.setattr(comparison_context, "build_comparison_context", run_comparison_windows)
    analyst = CaptureAnalyst()
    config = AppConfig.model_validate({
        "home": {"timezone": "UTC"}, "analysis": {"daily_ai_when_normal": True},
        "preferences": {"target_temperature_c": 25},
    })
    service = AnalysisService(db, config, analyst)
    report = service.analyze_daily(date(2026, 8, 1))
    period_start = datetime(2026, 8, 1, tzinfo=UTC)
    period_end = datetime(2026, 8, 2, tzinfo=UTC)
    assert [read for read in sample_reads if read == (control_id, period_start, period_end)] == [
        (control_id, period_start, period_end),
    ]  # Quality uses the same selected control-temperature values.
    assert [read for read in sample_reads if read == (mode_id, period_start - timedelta(days=7), period_end)] == [
        (mode_id, period_start - timedelta(days=7), period_end),
    ]  # Period-only mode analysis slices the already-read context window.
    assert [read for read in text_reads if read == (state_id, period_start - timedelta(days=7), period_end)] == [
        (state_id, period_start - timedelta(days=7), period_end),
    ]  # Analysis and evidence share the same state superset.
    assert observed_modes and all(period_start <= at < period_end for at, _ in observed_modes[0])
    assert observed_states and all(period_start <= at < period_end for at, _ in observed_states[0])
    assert len(observed_modes[0]) == len(observed_states[0]) == 288
    assert device_timestamp_reads == [
        ("test-device", datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end),
        ("test-device", datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_start + timedelta(hours=1)),
        ("test-device", datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end + timedelta(minutes=1)),
    ]  # Only non-contained windows refetch device-wide history.
    assert boiler_timestamp_reads == [
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end),
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end),
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_start + timedelta(hours=1)),
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_start + timedelta(hours=1)),
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end + timedelta(minutes=1)),
        (state_id, datetime(2026, 7, 31, 23, 55, tzinfo=UTC), period_end + timedelta(minutes=1)),
    ]  # Explicit boiler + device-wide scan per non-cached window.
    assert len(reliability_inputs) == 5
    expected_history = [period_start - timedelta(minutes=5)] + [
        period_start + timedelta(minutes=5 * index) for index in range(289)
    ]
    for end, timestamps in reliability_inputs:
        assert timestamps == [at for at in expected_history if at < end]
    evidence = report.context["temporal_evidence"]
    assert report.ai_used
    assert evidence["windows"][0]["facts"]["room_error_c"]["mean"] == 1
    assert evidence["windows"][0]["facts"]["delta_t_c"]["mean"] == 10
    assert evidence["signals"]["return_temperature"]["identity"].endswith("return/temperature")
    assert any(key.startswith("room:") for key in evidence["signals"])
    assert analyst.packet["control_context"]["temporal_evidence"]["metrics"]
    assert analyst.packet["control_context"]["temporal_evidence"]["quality"]
    saved = db.report(report.id)
    assert saved is not None and saved.context["temporal_evidence"] == evidence
    assert "Временные свидетельства" in render_text(saved)
    assert "Датчик return" in render_html(saved)
    assert db.report(report.id).recommendations == []

    # A subsequent public call has a fresh outer snapshot and reloads history once.
    service.analyze_daily(date(2026, 8, 1), use_ai=False)
    assert len(device_timestamp_reads) == 6

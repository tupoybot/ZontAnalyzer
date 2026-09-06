from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, TelemetryPoint
from zont_analyzer.reports import render_html, render_text


class CaptureAnalyst:
    packet: dict[str, Any]

    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        self.packet = packet
        return AnalysisResult(summary="Проверены временные свидетельства.")


def test_daily_evidence_reaches_ai_storage_and_reports_with_historical_target(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 8, 1, tzinfo=UTC)
    for entity, key, role, base, source in (
        ("living", "temperature", "control_indoor_temperature", 21.0, "synthetic"),
        ("circuit", "target_temp", "target_temperature", 20.0, "z3k_heating_circuit"),
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
        db.upsert_samples(points, {entity: role})
        row = next(item for item in db.list_series() if item["entity_id"] == entity and item["metric_key"] == key)
        db.update_series_role(row["id"], role, f"Датчик {entity}", provenance="test fixture")
    db.upsert_samples([TelemetryPoint(
        device_id="test-device", source_type="z3k_boiler_adapter", entity_id="boiler", metric_key="s",
        timestamp_utc=start + timedelta(minutes=5 * index), value_text="['ch']",
    ) for index in range(288)], {"boiler": "state"})
    analyst = CaptureAnalyst()
    config = AppConfig.model_validate({
        "home": {"timezone": "UTC"}, "analysis": {"daily_ai_when_normal": True},
        "preferences": {"target_temperature_c": 25},
    })
    report = AnalysisService(db, config, analyst).analyze_daily(date(2026, 8, 1))
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

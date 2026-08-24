from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, TelemetryPoint


def _room_points(start: datetime) -> list[TelemetryPoint]:
    return [
        TelemetryPoint(
            device_id="1",
            source_type="synthetic",
            entity_id="room",
            metric_key="temperature",
            timestamp_utc=start + timedelta(minutes=5 * index),
            value_num=22.0,
            unit="°C",
        )
        for index in range(288)
    ]


def test_recommendation_feedback_contains_rejection_and_latest_applied_note(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    service = AnalysisService(db, AppConfig())

    applied_report = service.analyze_daily(date(2026, 8, 1), use_ai=False)
    applied = applied_report.recommendations[0]
    assert applied.id is not None
    db.mark_applied(applied.id, "Первая проверка")
    db.mark_applied(applied.id, "Датчик проверен: показания верные")

    rejected_report = service.analyze_daily(date(2026, 8, 2), use_ai=False)
    rejected = rejected_report.recommendations[0]
    assert rejected.id is not None
    db.reject(rejected.id, "Датчик исправен; гипотезу закрыть на период наблюдения")

    feedback = {item["recommendation_id"]: item for item in db.recommendation_feedback(limit=10)}

    assert feedback[applied.id]["status"] == "applied"
    assert feedback[applied.id]["owner_note"] == "Датчик проверен: показания верные"
    assert feedback[rejected.id]["status"] == "rejected"
    assert feedback[rejected.id]["owner_note"] == (
        "Датчик исправен; гипотезу закрыть на период наблюдения"
    )
    assert feedback[rejected.id]["title"] == rejected.title
    assert feedback[rejected.id]["hypothesis"] == rejected.hypothesis


def test_identical_applied_feedback_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    recommendation_id = report.recommendations[0].id
    assert recommendation_id is not None

    first = db.mark_applied(recommendation_id, "Проверено владельцем")
    second = db.mark_applied(recommendation_id, "Проверено владельцем")

    assert second == first
    with db.engine.connect() as connection:
        count = connection.exec_driver_sql(
            "SELECT count(*) FROM interventions WHERE recommendation_id = ?",
            (recommendation_id,),
        ).scalar_one()
    assert count == 1


def test_next_openai_packet_includes_owner_recommendation_feedback(tmp_path: Path) -> None:
    class CapturingAnalyst:
        def __init__(self) -> None:
            self.packets: list[dict[str, Any]] = []

        def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
            self.packets.append(packet)
            return AnalysisResult(summary="AI summary")

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    config = AppConfig.model_validate({"analysis": {"daily_ai_when_normal": True}})
    previous = AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)
    recommendation = previous.recommendations[0]
    assert recommendation.id is not None
    owner_note = "Датчик исправен; тему пока закрыть и наблюдать"
    db.reject(recommendation.id, owner_note)

    start = datetime(2026, 8, 1, 20, tzinfo=UTC)
    db.upsert_samples(_room_points(start), {"room": "indoor_temperature"})
    analyst = CapturingAnalyst()

    report = AnalysisService(db, config, analyst).analyze_daily(date(2026, 8, 2))

    assert report.ai_used is True
    assert len(analyst.packets) == 1
    feedback = analyst.packets[0]["recommendation_feedback"]
    assert feedback == [
        {
            "recommendation_id": recommendation.id,
            "report_id": previous.id,
            "status": "rejected",
            "title": recommendation.title,
            "category": recommendation.category,
            "hypothesis": recommendation.hypothesis,
            "owner_note": owner_note,
            "updated_at": feedback[0]["updated_at"],
        }
    ]

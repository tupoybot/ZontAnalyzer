from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.sqlite.database import RecommendationRow
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, TelemetryPoint
from zont_analyzer.reports import render_html


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
    assert feedback[rejected.id]["owner_note"] == ("Датчик исправен; гипотезу закрыть на период наблюдения")
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


def test_stale_new_recommendation_becomes_ignored_idempotently_and_can_receive_late_feedback(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    service = AnalysisService(db, AppConfig())
    old_report = service.analyze_daily(date(2026, 8, 1), use_ai=False)
    fresh_report = service.analyze_daily(date(2026, 8, 2), use_ai=False)
    old_id = old_report.recommendations[0].id
    fresh_id = fresh_report.recommendations[0].id
    assert old_id is not None and fresh_id is not None

    reference = datetime(2026, 8, 5, tzinfo=UTC)
    with db.session() as session:
        old_row = session.get(RecommendationRow, old_id)
        fresh_row = session.get(RecommendationRow, fresh_id)
        assert old_row is not None and fresh_row is not None
        old_row.created_at = reference - timedelta(hours=48, seconds=1)
        fresh_row.created_at = reference - timedelta(hours=47)

    first = db.expire_stale_recommendations(now=reference)
    old_view = db.recommendation(old_id)
    fresh_view = db.recommendation(fresh_id)
    assert first["eligible"] == 1
    assert first["ignored"] == 1
    assert first["status_counts"] == {"new": 1, "applied": 0, "rejected": 0, "ignored": 1}
    assert old_view is not None and old_view["status"] == "ignored"
    assert fresh_view is not None and fresh_view["status"] == "new"
    assert db.recommendation_feedback() == []
    ignored_updated_at = old_view["updated_at"]

    repeated = db.expire_stale_recommendations(now=reference)
    assert repeated["ignored"] == 0
    repeated_old_view = db.recommendation(old_id)
    assert repeated_old_view is not None
    assert repeated_old_view["updated_at"] == ignored_updated_at

    rendered = render_html(old_report, db.recommendation_views_for_report(old_report.id))
    assert "Без реакции" in rendered
    assert "status-ignored" in rendered

    late = db.set_recommendation_feedback(old_id, "applied", "Проверено после наблюдения")
    assert late["status"] == "applied"
    assert late["owner_note"] == "Проверено после наблюдения"
    assert db.recommendation_feedback()[0]["recommendation_id"] == old_id


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

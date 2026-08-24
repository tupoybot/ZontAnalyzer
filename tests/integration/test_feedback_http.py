from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.application.pilot import PilotService, atomic_write_text, reports_directory
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, TelemetryPoint
from zont_analyzer.reports import render_html
from zont_analyzer.runtime import build_runtime

TOKEN = "test-feedback-token-123456"


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


def test_html_feedback_round_trip_and_next_ai_packet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZONT_FEEDBACK_TOKEN", TOKEN)
    runtime = build_runtime(None, tmp_path / "data")
    runtime.loaded.config.feedback.listen_port = 0
    runtime.loaded.config.feedback.public_api_base_url = "/api"
    report = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 1), use_ai=False)
    recommendation = report.recommendations[0]
    assert recommendation.id is not None

    pilot = PilotService(runtime)
    archive_path, _json_path = pilot._publish_archive(date(2026, 8, 1), report)
    latest_path = reports_directory(runtime) / "latest.html"
    atomic_write_text(
        latest_path,
        render_html(
            report,
            runtime.db.recommendation_views_for_report(report.id),
            feedback_api_base_url="/api",
        ),
        mode=0o644,
    )
    initial_html = archive_path.read_text(encoding="utf-8")
    assert recommendation.id in initial_html
    assert "Выполнено" in initial_html
    assert "Отклонить" in initial_html
    assert "Комментарий владельца" in initial_html

    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/api/recommendations"
    feedback_url = f"{base_url}/{recommendation.id}/feedback"
    try:
        with httpx.Client(timeout=5) as client:
            unauthorized = client.put(feedback_url, json={"status": "applied", "owner_note": "готово"})
            assert unauthorized.status_code == 401

            headers = {"Authorization": f"Bearer {TOKEN}"}
            unknown = client.put(
                f"{base_url}/unknown/feedback",
                headers=headers,
                json={"status": "applied", "owner_note": "готово"},
            )
            assert unknown.status_code == 404

            payload = {"status": "rejected", "owner_note": "Датчик исправен; наблюдаем дальше"}
            saved = client.put(feedback_url, headers=headers, json=payload)
            repeated = client.put(feedback_url, headers=headers, json=payload)
            reopened = client.get(feedback_url, headers=headers)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert saved.status_code == 200
    assert repeated.json() == saved.json()
    assert reopened.json()["status"] == "rejected"
    assert reopened.json()["owner_note"] == payload["owner_note"]
    refreshed_html = latest_path.read_text(encoding="utf-8")
    assert "status-rejected" in refreshed_html
    assert payload["owner_note"] in refreshed_html

    class CapturingAnalyst:
        def __init__(self) -> None:
            self.packets: list[dict[str, Any]] = []

        def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
            self.packets.append(packet)
            return AnalysisResult(summary="AI summary")

    config = AppConfig.model_validate({"analysis": {"daily_ai_when_normal": True}})
    runtime.db.upsert_samples(_room_points(datetime(2026, 8, 1, 20, tzinfo=UTC)), {"room": "indoor_temperature"})
    analyst = CapturingAnalyst()
    next_report = AnalysisService(runtime.db, config, analyst).analyze_daily(date(2026, 8, 2))

    assert next_report.ai_used is True
    feedback = analyst.packets[0]["recommendation_feedback"]
    assert feedback[0]["recommendation_id"] == recommendation.id
    assert feedback[0]["status"] == "rejected"
    assert feedback[0]["owner_note"] == payload["owner_note"]

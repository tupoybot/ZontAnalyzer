"""Owner feedback lifecycle against an isolated local YDB database."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.feedback import FeedbackRepository
from zont_analyzer.adapters.ydb.reports import ReportRepository
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.feedback import build_feedback_server
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.domain import AnalysisResult, QualityResult, Recommendation, Report, TelemetryPoint
from zont_analyzer.runtime import Runtime

START = datetime(2026, 1, 1, tzinfo=UTC)


def _report(day: int) -> Report:
    start = START + timedelta(days=day)
    return Report(
        id=f"feedback:report:{day}", kind="daily", period_start=start,
        period_end=start + timedelta(days=1), generated_at=start + timedelta(days=1, minutes=1),
        context={"device_id": "device:one", "gas": {"total": 3}},
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0,
                              stuck_pct=0, implausible_jumps=0, sample_count=1),
        summary="Summary", recommendations=[Recommendation(
            title="Check schedule", category="safe_user_setting", priority="low",
            confidence=0.8, hypothesis="Schedule may be changed",
            suggested_manual_action="Observe", expected_effect="More comfort",
            observation_period_days=7,
        )],
    )


def _setup(tmp_path: Path, *, clock: int = 1_000_000):
    db = make_database(tmp_path)
    db.reports = ReportRepository(db.storage, clock=lambda: clock)
    repo = FeedbackRepository(db.storage, clock=lambda: clock)
    return db, repo


def test_feedback_lifecycle_snapshots_prediction_audit_and_render_queue(tmp_path: Path) -> None:
    db, feedback = _setup(tmp_path)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    rec_id = f"rec:{report.id}:1"
    db.storage.execute(
        "DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
        "UPSERT INTO devices (id,payload) VALUES ($id,$payload);",
        {"$id": "device:one", "$payload": json.dumps({
            "id": "device:one", "discovered_at": "2025-12-31T00:00:00+00:00",
            "raw": {"z3k_config": {"pid": {"target": 19}}},
        })},
    )
    before = feedback.recommendation(rec_id)
    assert before is not None and before["status"] == "new"
    assert feedback.recommendation_views_for_report(report.id)[rec_id] == before
    experiment = {"category": "settings", "parameter": "target", "before": 19, "after": 20,
                  "performed_at": "2026-01-01T12:00:00+00:00"}
    applied = feedback.set_recommendation_feedback(rec_id, "applied", "  Owner observed  ", experiment)
    assert applied["owner_note"] == "Owner observed"
    assert applied["experiment"]["control_snapshot"]["value"]["fields"] == {
        "z3k_config.pid.target": 19,
    }
    assert feedback.set_recommendation_feedback(rec_id, "applied", "Owner observed", experiment) == applied
    interventions = db.storage.execute("SELECT id FROM interventions;")[0].rows
    assert len(interventions) == 1
    prediction = db.get_app_meta(f"intervention-prediction:{applied['intervention_id']}")
    assert prediction is not None and json.loads(prediction)["hypothesis"] == "Schedule may be changed"
    assert feedback.recommendation_feedback(before=START + timedelta(hours=6)) == []
    assert feedback.recommendation_feedback(before=START + timedelta(days=2))[0]["owner_note"] == "Owner observed"
    history = feedback.intervention_history(before=START + timedelta(days=2))
    assert len(history) == 1 and history[0]["temporal_boundary"] == experiment["performed_at"]
    assert feedback.recommendation_status_counts() == {"new": 0, "applied": 1, "rejected": 0, "ignored": 0}
    audit = db.storage.execute("SELECT id,payload FROM recommendation_audit;")[0].rows
    assert len(audit) == 1 and json.loads(audit[0].payload)["status"] == "applied"
    queued = db.storage.execute("SELECT scope,identifier FROM publication_changes;")[0].rows
    assert any(row.scope == "render" and row.identifier == report.id for row in queued)
    revised = feedback.set_recommendation_feedback(rec_id, "applied", "Updated note")
    assert revised["experiment"] == applied["experiment"]
    assert revised["intervention_id"] != applied["intervention_id"]
    assert len(feedback.intervention_history()) == 1  # Same experiment is not repeated in AI context.
    assert db.get_app_meta(f"intervention-prediction:{revised['intervention_id']}") == prediction
    db.storage.execute("DECLARE $id AS Utf8; DECLARE $report AS Utf8; DECLARE $payload AS Utf8; "
                       "UPSERT INTO notification_outbox (id,report_id,channel,payload,state,attempts) "
                       "VALUES ($id,$report,'email',$payload,'pending',0);",
                       {"$id": "other-channel", "$report": report.id, "$payload": "do not log"})
    assert feedback.flush_log_outbox() == ["rendered"]
    assert feedback.flush_log_outbox() == []
    pending = db.storage.execute("SELECT state,attempts FROM notification_outbox WHERE id='other-channel';")[0].rows
    assert pending[0].state == "pending" and pending[0].attempts == 0
    delivered = db.storage.execute("SELECT state,attempts FROM notification_outbox WHERE channel='log';")[0].rows
    assert delivered[0].state == "delivered" and delivered[0].attempts == 1


def test_stale_recommendation_expiry_and_late_rejection_are_idempotent(tmp_path: Path) -> None:
    created = int(START.timestamp()) * 1_000_000
    db, feedback = _setup(tmp_path, clock=created)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    rec_id = f"rec:{report.id}:1"
    assert feedback.stale_recommendation_count(now=START + timedelta(hours=47)) == 0
    assert feedback.stale_recommendation_count(now=START + timedelta(hours=48)) == 1
    first = feedback.expire_stale_recommendations(now=START + timedelta(hours=48))
    assert first["ignored"] == 1
    ignored = feedback.recommendation(rec_id)
    assert ignored is not None and ignored["status"] == "ignored"
    assert feedback.expire_stale_recommendations(now=START + timedelta(hours=48))["ignored"] == 0
    assert feedback.recommendation(rec_id) == ignored
    rejected = feedback.set_recommendation_feedback(rec_id, "rejected", "  Checked  ")
    assert rejected["status"] == "rejected" and rejected["owner_note"] == "Checked"
    assert feedback.set_recommendation_feedback(rec_id, "rejected", "Checked") == rejected
    assert feedback.recommendation_feedback()[0]["owner_note"] == "Checked"
    assert feedback.intervention_history() == []
    assert len(db.storage.execute("SELECT id FROM recommendation_audit;")[0].rows) == 1


def test_invalid_feedback_does_not_mutate_state(tmp_path: Path) -> None:
    db, feedback = _setup(tmp_path)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    rec_id = f"rec:{report.id}:1"
    with pytest.raises(ValueError, match="status"):
        feedback.set_recommendation_feedback(rec_id, "ignored")
    with pytest.raises(ValueError, match="experiment"):
        feedback.set_recommendation_feedback(rec_id, "rejected", experiment={"after": 20})
    with pytest.raises(ValueError):
        feedback.set_recommendation_feedback(rec_id, "applied", experiment={"performed_at": "2026-01-01"})
    with pytest.raises(KeyError):
        feedback.set_recommendation_feedback("missing", "applied")
    assert feedback.recommendation(rec_id)["status"] == "new"
    assert db.storage.execute("SELECT id FROM recommendation_audit;")[0].rows == []


@pytest.mark.parametrize("experiment", [
    {"category": "invalid"},
    {"parameter": "PZA", "performed_at": "2026-08-01T12:00:00"},
    {"parameter": "x" * 201},
    {"before": "x" * 501},
])
def test_experiment_contract_rejects_invalid_values(tmp_path: Path, experiment: dict[str, object]) -> None:
    db, feedback = _setup(tmp_path)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    with pytest.raises(ValueError):
        feedback.set_recommendation_feedback(f"rec:{report.id}:1", "applied", experiment=experiment)
    assert feedback.recommendation_status_counts()["new"] == 1


def test_feedback_keeps_latest_note_and_explicit_temporal_boundaries(tmp_path: Path) -> None:
    db, feedback = _setup(tmp_path)
    first, second = _report(0), _report(1)
    db.reports.save_report(first, "first")
    db.reports.save_report(second, "second")
    first_id, second_id = f"rec:{first.id}:1", f"rec:{second.id}:1"
    firmware = {"category": "firmware_rollback", "before": "2.0", "after": "1.9",
                "performed_at": "2026-08-02T00:00:00+00:00"}
    first_intervention = feedback.mark_applied(first_id, "First note")
    second_intervention = feedback.set_recommendation_feedback(first_id, "applied", "Latest note", firmware)
    assert second_intervention["intervention_id"] != first_intervention
    feedback.reject(second_id, "Sensor checked; close hypothesis")
    values = {item["recommendation_id"]: item for item in feedback.recommendation_feedback()}
    assert values[first_id]["owner_note"] == "Latest note"
    assert values[second_id]["owner_note"] == "Sensor checked; close hypothesis"
    assert values[second_id]["title"] == second.recommendations[0].title
    assert values[first_id]["experiment"]["category"] == "firmware_rollback"
    assert feedback.recommendation_feedback(before=datetime(2026, 8, 2, tzinfo=UTC)) == [values[second_id]]
    assert feedback.intervention_history(before=datetime(2026, 8, 2, tzinfo=UTC)) == [
        item for item in feedback.intervention_history() if item["intervention_id"] == first_intervention
    ]
    assert feedback.intervention_history(limit=1)[0]["temporal_boundary"] == firmware["performed_at"]
    assert feedback.intervention_history(limit=0) == []


def test_report_regeneration_keeps_owner_state_and_original_expiry_time(tmp_path: Path) -> None:
    created = int(START.timestamp()) * 1_000_000
    db, feedback = _setup(tmp_path, clock=created)
    report = _report(0)
    db.reports.save_report(report, "first")
    rec_id = f"rec:{report.id}:1"
    original_created = db.storage.execute("SELECT created_at FROM recommendations;")[0].rows[0].created_at
    feedback.reject(rec_id, "Keep owner decision")
    revised = report.model_copy(deep=True)
    revised.summary = "Recalculated"
    db.reports.save_report(revised, "second", expected_revision=1)
    assert feedback.recommendation(rec_id)["owner_note"] == "Keep owner decision"
    assert db.storage.execute("SELECT created_at FROM recommendations;")[0].rows[0].created_at == original_created
    assert feedback.expire_stale_recommendations(now=START + timedelta(days=10))["ignored"] == 0


def test_imported_intervention_and_experiment_keep_historical_snapshot(tmp_path: Path) -> None:
    db, feedback = _setup(tmp_path)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    rec_id = f"rec:{report.id}:1"
    applied_at = int((START + timedelta(hours=1)).timestamp()) * 1_000_000
    db.storage.execute("DECLARE $id AS Utf8; UPDATE recommendations SET status='applied' WHERE id=$id;",
                       {"$id": rec_id})
    db.storage.execute("DECLARE $id AS Utf8; DECLARE $rec AS Utf8; DECLARE $at AS Int64; "
                       "DECLARE $payload AS Utf8; UPSERT INTO interventions "
                       "(id,recommendation_id,applied_at,payload) VALUES ($id,$rec,$at,$payload);",
                       {"$id": "intervention:legacy", "$rec": rec_id, "$at": applied_at,
                        "$payload": json.dumps({"id": "intervention:legacy", "recommendation_id": rec_id,
                                                "applied_at": applied_at, "note": "Imported note"})})
    snapshot = {"fields": {"z3k_config.pid.target": 19}}
    db.storage.execute("DECLARE $id AS Utf8; DECLARE $intervention AS Utf8; DECLARE $payload AS Utf8; "
                       "UPSERT INTO intervention_experiments (id,intervention_id,payload) "
                       "VALUES ($id,$intervention,$payload);",
                       {"$id": "experiment:legacy", "$intervention": "intervention:legacy",
                        "$payload": json.dumps({
                            "category": "settings", "parameter": "target", "before_json": "19",
                            "after_json": "20", "performed_at": applied_at,
                            "snapshot_json": json.dumps(snapshot), "snapshot_fingerprint": "fingerprint",
                            "snapshot_captured_at": applied_at, "snapshot_source": "source",
                            "historical_context": "configuration_at_intervention_not_verified",
                        })})
    view = feedback.recommendation(rec_id)
    assert view is not None and view["owner_note"] == "Imported note"
    assert view["experiment"]["control_snapshot"]["value"] == snapshot
    assert view["experiment"]["performed_at"] == "2026-01-01T01:00:00+00:00"
    assert feedback.intervention_history()[0]["experiment"] == view["experiment"]


def test_next_analysis_packet_includes_owner_feedback_from_ydb(tmp_path: Path) -> None:
    db, _feedback = _setup(tmp_path)
    prior = _report(0)
    db.reports.save_report(prior, "rendered")
    rec_id = f"rec:{prior.id}:1"
    _feedback.reject(rec_id, "Sensor checked; close this hypothesis")
    start = datetime(2026, 1, 1, 20, tzinfo=UTC)
    points = [TelemetryPoint(
        device_id="device:one", source_type="synthetic", entity_id="room", metric_key="temperature",
        timestamp_utc=start + timedelta(minutes=5 * index), value_num=22.0, unit="°C",
    ) for index in range(288)]
    db.telemetry.write_window(device_id="device:one", data_type="history", start=start,
                              end=start + timedelta(days=1), points=points)

    class CapturingAnalyst:
        def __init__(self) -> None:
            self.packets: list[dict] = []

        def analyze(self, packet: dict) -> AnalysisResult:
            self.packets.append(packet)
            return AnalysisResult(summary="AI summary")

    analyst = CapturingAnalyst()
    config = AppConfig.model_validate({"analysis": {"daily_ai_when_normal": True, "minimum_quality_score": 0}})
    report = AnalysisService(db, config, analyst).analyze_daily(datetime(2026, 1, 2).date())
    assert report.ai_used and len(analyst.packets) == 1
    context = analyst.packets[0]["recommendation_feedback"]
    assert context[0]["recommendation_id"] == rec_id
    assert context[0]["owner_note"] == "Sensor checked; close this hypothesis"


def test_feedback_http_round_trip_uses_ydb_and_refreshes_report(tmp_path: Path) -> None:
    db, _feedback = _setup(tmp_path)
    report = _report(0)
    db.reports.save_report(report, "rendered")
    loaded = LoadedConfig(config=AppConfig(), secrets=Secrets(), config_path=None,
                          data_dir=tmp_path, sources={})
    runtime = Runtime(loaded, db)
    runtime.config.feedback.listen_port = 0
    runtime.config.feedback.public_api_base_url = "/api"
    rec_id = f"rec:{report.id}:1"
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}/api", timeout=20) as client:
            assert client.get(f"/recommendations/{rec_id}/feedback").json()["status"] == "new"
            rejected = client.put(f"/recommendations/{rec_id}/feedback", json={
                "status": "rejected", "owner_note": "Owner checked",
            })
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["status"] == "rejected"
            repeated = client.put(f"/recommendations/{rec_id}/feedback", json={
                "status": "rejected", "owner_note": "Owner checked",
            })
            assert repeated.status_code == 200 and repeated.json() == rejected.json()
            assert client.get(f"/recommendations/{rec_id}/feedback").json()["owner_note"] == "Owner checked"
            assert client.put(f"/recommendations/{rec_id}/feedback", json={
                "status": "rejected", "experiment": {"after": 20},
            }).status_code == 422
            assert client.put("/recommendations/missing/feedback", json={"status": "applied"}).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

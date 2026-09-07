from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from zont_analyzer.adapters.openai.provider import analysis_packet
from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.reasoning_context import reasoning_context
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import Prediction, QualityResult, Report
from zont_analyzer.domain.experiments import control_snapshot


def _recommendation(db: Database) -> str:
    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    recommendation_id = report.recommendations[0].id
    assert recommendation_id is not None
    return recommendation_id


def test_structured_experiment_is_idempotent_and_note_edit_preserves_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    db.save_devices([{
        "device_id": "device-1",
        "z3k_config": {
            "heating_circuits": [{"id": 1, "name": "Heating", "pid": {"kp": 1.2}}],
            "pza": {"enabled": True},
        },
    }])
    recommendation_id = _recommendation(db)
    experiment = {
        "category": "settings", "parameter": "PZA curve", "before": 1.0, "after": 1.2,
        "performed_at": "2026-08-01T12:00:00+04:00",
    }

    first = db.set_recommendation_feedback(recommendation_id, "applied", "Curve updated", experiment)
    repeated = db.set_recommendation_feedback(recommendation_id, "applied", "Curve updated", experiment)
    edited = db.set_recommendation_feedback(recommendation_id, "applied", "Observe for a week")

    assert repeated == first
    assert first["experiment"] is not None
    assert first["experiment"]["after"] == 1.2
    snapshot = first["experiment"]["control_snapshot"]
    assert snapshot is not None
    assert snapshot["source"] == "zont:discover.read_only.z3k_config"
    assert snapshot["historical_context"] == "captured_after_reported_intervention; historical_configuration_unknown"
    assert edited["experiment"] == first["experiment"]
    history = db.intervention_history()
    assert len(history) == 1
    assert history[0]["owner_note"] == "Observe for a week"


@pytest.mark.parametrize(
    "experiment",
    [
        {"category": "invalid"},
        {"parameter": "PZA", "performed_at": "2026-08-01T12:00:00"},
        {"parameter": "x" * 201},
        {"before": "x" * 501},
    ],
)
def test_experiment_contract_rejects_invalid_values(tmp_path: Path, experiment: dict[str, object]) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    with pytest.raises(ValueError):
        db.set_recommendation_feedback(_recommendation(db), "applied", experiment=experiment)


def test_intervention_history_is_bounded_and_keeps_explicit_temporal_boundary(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    recommendation_id = _recommendation(db)
    db.set_recommendation_feedback(
        recommendation_id, "applied", "Firmware installed",
        {"category": "firmware_update", "before": "1.0", "after": "1.1", "performed_at": "2026-08-01T12:00:00+04:00"},
    )
    history = db.intervention_history(limit=1)
    assert history[0]["temporal_boundary"] == "2026-08-01T08:00:00+00:00"
    assert history[0]["experiment"]["control_snapshot"] is None
    assert db.intervention_history(before=datetime(2026, 8, 1, 8, tzinfo=UTC)) == []


def test_rejected_feedback_cannot_silently_record_an_experiment(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    with pytest.raises(ValueError, match="applied"):
        db.set_recommendation_feedback(
            _recommendation(db), "rejected", experiment={"category": "other"},
        )


def test_firmware_rollback_and_feedback_boundary_are_preserved(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    recommendation_id = _recommendation(db)
    db.set_recommendation_feedback(
        recommendation_id, "applied", "Rollback completed",
        {"category": "firmware_rollback", "before": "2.0", "after": "1.9",
         "performed_at": "2026-08-02T00:00:00+00:00"},
    )
    stored = db.recommendation(recommendation_id)
    assert stored is not None and stored["experiment"]["category"] == "firmware_rollback"
    assert db.recommendation_feedback(before=datetime(2026, 8, 2, tzinfo=UTC)) == []
    assert db.recommendation_feedback(before=datetime(2026, 8, 3, tzinfo=UTC))[0]["experiment"]["after"] == "1.9"


def test_snapshot_uses_real_discovery_control_paths_and_ai_bounds_raw_fields() -> None:
    import json

    device = json.loads(Path("tests/fixtures/zont_contract/devices.json").read_text(encoding="utf-8"))["devices"][0]
    snapshot = control_snapshot(device)
    assert snapshot is not None
    assert snapshot["fields"]["z3k_config.heating_circuits.0.id"] == 20001

    large = {f"z3k_config.pzas.0.point.{index:02d}": index for index in range(30)}
    context = reasoning_context([], [], "UTC", [{
        "intervention_id": "i", "recommendation_id": "r", "recorded_at": "2026-08-01T00:00:00+00:00",
        "owner_note": "", "temporal_boundary": "2026-08-01T00:00:00+00:00",
        "experiment": {"category": "settings", "parameter": "PZA", "before": 1, "after": 2,
                       "performed_at": "2026-08-01T00:00:00+00:00", "control_snapshot": {
                           "value": {"fields": large}, "fingerprint": "full-fingerprint",
                           "captured_at": "2026-08-01T00:00:00+00:00", "source": "discover",
                           "historical_context": "unknown",
                       }},
    }])
    selected = context["intervention_history"][0]["experiment"]["control_snapshot"]
    assert len(selected["value"]["fields"]) == 24
    assert selected["fingerprint"] == "full-fingerprint"
    packet = analysis_packet(
        quality={"score": 1, "coverage_pct": 100, "max_gap_seconds": 0, "stuck_pct": 0,
                 "implausible_jumps": 0, "sample_count": 1},
        metrics=[], events=[], period={"kind": "daily"}, context=context,
    )
    assert packet["control_context"]["intervention_history"][0]["experiment"]["control_snapshot"] == selected


def test_prior_predictions_reach_ai_packet() -> None:
    quality = QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                            implausible_jumps=0, sample_count=1)
    report = Report(
        id="prior", kind="daily", period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 8, 2, tzinfo=UTC), generated_at=datetime(2026, 8, 2, tzinfo=UTC),
        quality=quality, summary="Prior", ai_used=True,
        predictions=[Prediction(id="p:1", scenario="Same mode", expected_effect="Observe", confidence=.5,
                                confidence_basis="Prior period", verification="Review next day")],
    )
    context = reasoning_context([], [report], "UTC")
    packet = analysis_packet(
        quality=quality.model_dump(), metrics=[], events=[], period={"kind": "daily"}, context=context,
    )
    assert packet["control_context"]["prior_interpretations"][0]["predictions"][0]["id"] == "p:1"


def test_firmware_timeline_is_owner_context_not_verified_telemetry() -> None:
    context = reasoning_context([], [], "UTC", [{
        "experiment": {"category": "firmware_rollback", "parameter": "firmware", "before": "2.0",
                       "after": "1.9", "performed_at": "2026-08-02T00:00:00+00:00"},
    }])
    firmware = context["dhw_profiles"]["firmware"]
    assert firmware["status"] == "unknown"
    assert firmware["owner_recorded_timeline"] == [{
        "category": "firmware_rollback", "parameter": "firmware", "before": "2.0", "after": "1.9",
        "performed_at": "2026-08-02T00:00:00+00:00", "source": "owner_recorded_manual_intervention",
        "epistemic_level": "owner_confirmed",
    }]

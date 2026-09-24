"""Coalesced YDB publication revisions and semantic source changes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.adapters.ydb.telemetry import bump_revision
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import QualityResult, Recommendation, Report, TelemetryPoint


def _database(tmp_path: Path) -> Database:
    return make_database(tmp_path)


def _record_change(db: Database, scope: str, identifier: str) -> None:
    def write(tx) -> None:
        revision = bump_revision(tx, "publication")
        tx.execute(
            "DECLARE $scope AS Utf8; DECLARE $identifier AS Utf8; DECLARE $revision AS Int64; "
            "UPSERT INTO publication_changes (scope,identifier,revision,payload) "
            "VALUES ($scope,$identifier,$revision,'{}');",
            {"$scope": scope, "$identifier": identifier, "$revision": revision},
        )

    db.storage.transaction(write)


def _changes(db: Database, after: int, through: int | None = None) -> tuple[int, list[dict[str, object]]]:
    high = db.source_revision() if through is None else through
    return high, PublicationRepository(db.storage).changes_since(after, high)


def _report() -> Report:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return Report(
        id="journal-report", kind="daily", period_start=start,
        period_end=start + timedelta(days=1), generated_at=start + timedelta(days=1, minutes=1),
        timezone="Europe/Samara", context={"device_id": "device"},
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
        summary="journal fixture",
        recommendations=[Recommendation(title="Check schedule", category="safe_user_setting", priority="low",
                                        confidence=0.8, hypothesis="Schedule may matter",
                                        suggested_manual_action="Observe", expected_effect="More comfort",
                                        observation_period_days=7)],
    )


@pytest.mark.ydb
def test_journal_coalesces_keys_and_keeps_monotonic_high_water_mark(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _record_change(db, "render", "report-1")
    first = db.source_revision()
    _record_change(db, "render", "report-1")
    second = db.source_revision()
    assert second > first
    high, changes = _changes(db, first)
    assert high == second
    assert [(item["scope"], item["identifier"], item["revision"]) for item in changes] == [
        ("render", "report-1", second)
    ]
    assert _changes(db, second) == (second, [])


@pytest.mark.ydb
def test_journal_range_and_rollback(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _record_change(db, "render", "a")
    start = db.source_revision()
    _record_change(db, "global", "gas")
    middle = db.source_revision()
    _record_change(db, "telemetry", "2026-09-13")
    end = db.source_revision()
    assert [item["identifier"] for item in _changes(db, start, middle)[1]] == ["gas"]
    assert _changes(db, start, end)[0] == end

    def rolled_back(tx) -> None:
        revision = bump_revision(tx, "publication")
        tx.execute("DECLARE $revision AS Int64; UPSERT INTO publication_changes "
                   "(scope,identifier,revision,payload) VALUES ('render','rolled-back',$revision,'{}');",
                   {"$revision": revision})
        raise RuntimeError("rollback")

    with pytest.raises(RuntimeError, match="rollback"):
        db.storage.transaction(rolled_back)
    assert _changes(db, end) == (end, [])


@pytest.mark.ydb
def test_representative_writes_mark_report_feedback_gas_device_and_telemetry(tmp_path: Path) -> None:
    db = _database(tmp_path)
    db.save_devices([{"id": "device", "name": "Device"}])
    report = _report()
    db.save_report(report, "rendered")
    baseline = db.source_revision()
    db.feedback.set_recommendation_feedback(f"rec:{report.id}:1", "applied", "done")
    OwnerContextStore(db).update_gas(report.id, {"value_m3": "10"})
    db.save_devices([{"id": "device", "name": "Renamed"}])
    point = TelemetryPoint(device_id="device", source_type="temperature", entity_id="room",
                           metric_key="temperature", timestamp_utc=report.period_start,
                           value_num=20.0)
    db.telemetry.write_window(device_id="device", data_type="history", start=report.period_start,
                              end=report.period_start + timedelta(hours=1), points=[point])
    high, changes = _changes(db, baseline)
    assert high > baseline
    scopes = {(str(item["scope"]), str(item["identifier"])) for item in changes}
    assert ("render", report.id) in scopes
    assert ("owner-gas:installation", "") in scopes
    assert ("device:device", "") in scopes
    assert ("telemetry:device", "") in scopes
    assert ("global", "gas") not in scopes


@pytest.mark.ydb
def test_noop_feedback_is_ignored_and_status_reversal_is_audited(tmp_path: Path) -> None:
    db = _database(tmp_path)
    report = _report()
    db.save_report(report, "rendered")
    recommendation_id = f"rec:{report.id}:1"
    db.feedback.set_recommendation_feedback(recommendation_id, "applied", "done")
    baseline = db.source_revision()
    db.feedback.set_recommendation_feedback(recommendation_id, "applied", "done")
    assert _changes(db, baseline) == (baseline, [])
    reversed_state = db.feedback.set_recommendation_feedback(recommendation_id, "rejected", "corrected")
    assert reversed_state["status"] == "rejected"
    high, changes = _changes(db, baseline)
    assert high > baseline
    assert {(item["scope"], item["identifier"]) for item in changes} == {("render", report.id)}
    assert db.feedback.intervention_history()

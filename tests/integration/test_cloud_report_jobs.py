"""Resumable report phases against real local YDB with fixture-only source calls."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.openai.provider import AIRequestPending
from zont_analyzer.cloud.report_jobs import ReportJobRunner, ReportRequest
from zont_analyzer.config import AppConfig, LoadedConfig, Secrets
from zont_analyzer.runtime import Runtime

NOW = datetime(2026, 9, 24, 2, tzinfo=UTC)


class FixtureClient:
    def __init__(self) -> None:
        self.history_calls = 0
        self.event_calls = 0
        self.fail_once = False

    def discover_devices(self):
        return [{"id": "fixture"}]

    def load_history(self, **_kwargs):
        self.history_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("fixture outage")
        return [{"device_id": "fixture", "ok": True}]

    def normalize_history(self, _response):
        return [], {}

    def load_events(self, **_kwargs):
        self.event_calls += 1
        return []

    def normalize_events(self, _device_id, _rows):
        return []

    def close(self):
        pass


def _runner(tmp_path: Path):
    db = make_database(tmp_path)
    config = AppConfig()
    config.home.timezone = "UTC"
    config.zont.history_data_types = ["temperature"]
    runtime = Runtime(LoadedConfig(config=config, secrets=Secrets(), config_path=None,
                                   data_dir=tmp_path, sources={}), db)
    client = FixtureClient()
    runner = ReportJobRunner(runtime, client_factory=lambda: client, now=lambda: NOW)
    return db, runner, client


def _seed_archive(db, runner, payload) -> None:
    period = runner.period(ReportRequest.model_validate(payload))
    db.save_devices([{"id": "fixture"}])
    for data_type in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="fixture", data_type=data_type, start=period.start,
                                  end=period.end - timedelta(hours=1), state="empty")


def _seed_imported_report(db, runner, payload: dict, *, state: str = "complete") -> str:
    request = ReportRequest.model_validate(payload)
    report = runner.runtime.analysis(no_ai=True).analyze_daily(request.selected_date(), use_ai=False)
    row = db.storage.execute(
        "DECLARE $id AS Utf8; SELECT kind,period_start,period_end,algorithm_version,id,payload "
        "FROM reports VIEW by_id WHERE id=$id;", {"$id": report.id},
    )[0].rows[0]
    canonical = json.loads(row.payload)["report"]
    source = {"id": report.id, "canonical_json": json.dumps(canonical, ensure_ascii=False)}
    target_key = {"kind": row.kind, "period_start": row.period_start,
                  "period_end": row.period_end, "algorithm_version": row.algorithm_version}
    db.storage.execute(
        "DECLARE $source AS Utf8; DECLARE $target AS Utf8; "
        "DECLARE $checksum AS Utf8; DECLARE $payload AS Utf8; "
        "UPSERT INTO migration_records (source_table,source_key,target_table,target_key,checksum,payload) "
        "VALUES ('reports',$source,'reports',$target,$checksum,$payload);",
        {"$source": json.dumps([report.id], separators=(",", ":")),
         "$target": json.dumps(target_key),
         "$checksum": hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest(),
         "$payload": json.dumps(source)},
    )
    digest = "a" * 64
    db.storage.execute(
        "DECLARE $state AS Utf8; DECLARE $digest AS Utf8; "
        "UPSERT INTO metadata (name,value) VALUES ('sqlite_import_state',$state); "
        "UPSERT INTO metadata (name,value) VALUES ('sqlite_import_sha256',$digest);",
        {"$state": json.dumps({"state": state, "source_sha256": digest}), "$digest": digest},
    )
    return report.id


@pytest.mark.ydb
def test_daily_job_collects_bounded_gaps_resumes_and_reuses_completed_report(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "max_requests": 2, "use_ai": False}
    _seed_archive(db, runner, payload)
    period = runner.period(ReportRequest.model_validate(payload))
    competitor = db.jobs.acquire(period.job_key, "other", 30)
    assert competitor is not None
    assert runner.run(payload)["status"] == "busy"
    assert client.history_calls == client.event_calls == 0
    assert db.jobs.release(period.job_key, "other", competitor.attempt)

    first = runner.run(payload)
    assert first["status"] == "pending" and first["phase"] == "collect"
    assert first["collection"]["requests"] == 2
    second = runner.run(payload)
    assert second["status"] == "pending" and second["phase"] == "analyze"
    assert second["collection"]["requests"] == 2
    assert client.history_calls == 2 and client.event_calls == 2
    third = runner.run(payload)
    assert third["status"] == "done" and third["ai_used"] is False
    assert db.report(third["report_id"]) is not None
    assert db.storage.execute("SELECT id FROM notification_outbox;")[0].rows
    repeated = runner.run(payload)
    assert repeated["status"] == "done" and repeated["reused"] is True
    assert repeated["report_id"] == third["report_id"]
    assert client.history_calls == 2 and client.event_calls == 2
    assert len(db.storage.execute("SELECT id FROM reports;")[0].rows) == 1


@pytest.mark.ydb
def test_failed_window_is_retried_from_coverage_without_restarting_completed_archive(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "max_requests": 2, "use_ai": False}
    _seed_archive(db, runner, payload)
    client.fail_once = True
    first = runner.run(payload)
    assert first["status"] == "pending" and first["collection"]["failed_windows"] == 1
    second = runner.run(payload)
    assert second["status"] == "pending"
    third = runner.run(payload)
    assert third["phase"] == "analyze"
    assert runner.run(payload)["status"] == "done"
    assert client.history_calls == 3 and client.event_calls == 2


@pytest.mark.ydb
def test_report_request_calendar_validation_and_not_due(tmp_path: Path) -> None:
    _db, runner, client = _runner(tmp_path)
    deterministic = runner.period(ReportRequest.model_validate(
        {"kind": "daily", "date": "2026-09-23", "use_ai": False},
    ))
    ai = runner.period(ReportRequest.model_validate({"kind": "daily", "date": "2026-09-23"}))
    assert deterministic.job_key != ai.job_key
    not_due = runner.run({"kind": "daily", "date": "2026-09-24", "use_ai": False})
    assert not_due["status"] == "not_due"
    assert client.history_calls == client.event_calls == 0
    for bad in (
        {"kind": "daily", "date": "2026-09-23", "month": 9},
        {"kind": "weekly", "year": 2026, "week": 54},
        {"kind": "monthly", "year": 2026, "month": 13},
        {"kind": "daily", "date": "2026-09-23", "max_requests": 100},
    ):
        try:
            runner.run(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid report request: {bad}")


@pytest.mark.parametrize("payload", [
    {"kind": "weekly", "year": 2026, "week": 38, "max_requests": 2, "use_ai": False},
    {"kind": "monthly", "year": 2026, "month": 8, "max_requests": 2, "use_ai": False},
])
@pytest.mark.ydb
def test_long_period_reuses_archive_and_fetches_only_gaps(tmp_path: Path, payload: dict) -> None:
    db, runner, client = _runner(tmp_path)
    _seed_archive(db, runner, payload)
    assert runner.run(payload)["phase"] == "collect"
    assert runner.run(payload)["phase"] == "analyze"
    result = runner.run(payload)
    assert result["status"] == "done"
    assert db.report(result["report_id"]) is not None
    assert client.history_calls == client.event_calls == 2
    assert runner.run(payload)["reused"] is True


@pytest.mark.ydb
def test_saved_checkpoint_resumes_after_interrupted_completion_without_reanalysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, runner, _client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    _seed_archive(db, runner, payload)
    assert runner.run(payload)["phase"] == "analyze"
    complete = runner.jobs.complete
    monkeypatch.setattr(runner.jobs, "complete", lambda *_args: False)
    with pytest.raises(RuntimeError, match="ownership expired"):
        runner.run(payload)
    checkpoint = runner.jobs.get(runner.period(ReportRequest.model_validate(payload)).job_key)
    assert checkpoint is not None
    assert json.loads(checkpoint.checkpoint or "{}").get("phase") == "saved"
    assert json.loads(checkpoint.checkpoint or "{}").get("input_fingerprint")
    report_rows = db.storage.execute("SELECT id FROM reports;")[0].rows
    assert len(report_rows) == 1
    monkeypatch.setattr(runner.jobs, "complete", complete)
    original_analysis = runner.runtime.analysis

    def no_reanalysis(*args: object, **kwargs: object):
        if kwargs.get("job_fence") is not None:
            raise AssertionError("committed report was analyzed again")
        return original_analysis(*args, **kwargs)

    monkeypatch.setattr(runner.runtime, "analysis", no_reanalysis)
    result = runner.run(payload)
    assert result["status"] == "done"
    assert len(db.storage.execute("SELECT id FROM reports;")[0].rows) == 1
    assert result["report_id"] == report_rows[0]["id"]


@pytest.mark.ydb
def test_completed_job_reopens_after_source_change(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    _seed_archive(db, runner, payload)
    assert runner.run(payload)["phase"] == "analyze"
    first = runner.run(payload)
    assert first["status"] == "done"
    original = db.report(first["report_id"])
    assert original is not None
    db.save_devices([{"id": "fixture", "name": "updated source"}])
    assert runner.run(payload)["phase"] == "analyze"
    refreshed = runner.run(payload)
    assert refreshed["status"] == "done"
    assert refreshed["reused"] is False
    assert refreshed["report_id"] == first["report_id"]
    assert db.storage.execute("SELECT revision FROM reports;")[0].rows[0].revision == 2
    assert runner.run(payload)["reused"] is True


@pytest.mark.ydb
def test_unknown_ai_request_leaves_job_pending_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, runner, _client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    _seed_archive(db, runner, payload)
    assert runner.run(payload)["phase"] == "analyze"

    def pending(*_args: object) -> None:
        raise AIRequestPending("request-key", "unknown")

    monkeypatch.setattr(runner, "_advance", pending)
    result = runner.run(payload)
    assert result["status"] == "reconciliation_required"
    assert result["request_key"] == "request-key"
    job = runner.jobs.get(result["job_key"])
    assert job is not None and job.state != "done"


@pytest.mark.ydb
def test_complete_import_reuses_exact_report_without_source_or_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, runner, client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": True}
    report_id = _seed_imported_report(db, runner, payload)
    original_analysis = runner.runtime.analysis

    def only_calendar(*args: object, **kwargs: object):
        if kwargs.get("job_fence") is not None:
            raise AssertionError("imported report was analyzed")
        return original_analysis(*args, **kwargs)

    monkeypatch.setattr(runner.runtime, "analysis", only_calendar)
    result = runner.run(payload)
    job_key = runner.period(ReportRequest.model_validate(payload)).job_key
    assert result == {"status": "stored_imported", "job_key": job_key,
                      "report_id": report_id, "ai_used": False,
                      "reused": True, "freshness": "unverified"}
    assert runner.jobs.get(result["job_key"]) is None
    assert client.history_calls == client.event_calls == 0
    assert db.storage.execute("SELECT device_id FROM coverage;")[0].rows == []


@pytest.mark.ydb
def test_incomplete_import_cannot_be_adopted_and_refresh_starts_collection(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False, "max_requests": 2}
    report_id = _seed_imported_report(db, runner, payload, state="running")
    assert runner.run(payload)["status"] == "import_in_progress"
    assert client.history_calls == client.event_calls == 0
    assert runner.jobs.get(runner.period(ReportRequest.model_validate(payload)).job_key) is None
    db.storage.execute(
        "DECLARE $value AS Utf8; UPSERT INTO metadata (name,value) VALUES ('sqlite_import_state',$value);",
        {"$value": json.dumps({"state": "complete", "source_sha256": "a" * 64})},
    )
    assert runner.run(payload)["report_id"] == report_id
    result = runner.run({**payload, "refresh": True})
    assert result["status"] == "pending" and result["phase"] == "collect"
    assert result["collection"]["requests"] == 2
    assert client.history_calls + client.event_calls == 2


@pytest.mark.ydb
def test_import_reuse_requires_exact_identity_bounds_and_canonical_content(tmp_path: Path) -> None:
    db, runner, client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False, "max_requests": 2}
    report_id = _seed_imported_report(db, runner, payload)
    period = runner.period(ReportRequest.model_validate(payload))
    assert runner._imported_artifact(period, refresh=False) is not None
    other = runner.period(ReportRequest.model_validate({"kind": "daily", "date": "2026-09-22"}))
    assert runner._imported_artifact(other, refresh=False) is None
    row = db.storage.execute(
        "DECLARE $id AS Utf8; SELECT target_key FROM migration_records "
        "WHERE source_table='reports' AND source_key=$id;",
        {"$id": json.dumps([report_id], separators=(",", ":"))},
    )[0].rows[0]
    original_target = row.target_key
    wrong = json.loads(original_target)
    wrong["period_end"] += 1
    db.storage.execute(
        "DECLARE $key AS Utf8; DECLARE $target AS Utf8; "
        "UPDATE migration_records SET target_key=$target WHERE source_table='reports' AND source_key=$key;",
        {"$key": json.dumps([report_id], separators=(",", ":")), "$target": json.dumps(wrong)},
    )
    assert runner._imported_artifact(period, refresh=False) is None
    db.storage.execute(
        "DECLARE $key AS Utf8; DECLARE $target AS Utf8; "
        "UPDATE migration_records SET target_key=$target WHERE source_table='reports' AND source_key=$key;",
        {"$key": json.dumps([report_id], separators=(",", ":")), "$target": original_target},
    )
    report = db.report(report_id)
    assert report is not None
    report.summary = "changed after import"
    db.save_report(report, "changed rendered text")
    assert runner._imported_artifact(period, refresh=False) is None
    assert runner.run(payload)["phase"] == "collect"
    assert client.history_calls + client.event_calls == 2


@pytest.mark.ydb
def test_import_reuse_requires_matching_completion_attestation(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    _seed_imported_report(db, runner, payload)
    period = runner.period(ReportRequest.model_validate(payload))
    assert runner._imported_artifact(period, refresh=False) is not None
    db.storage.execute(
        "UPDATE metadata SET value='b' WHERE name='sqlite_import_sha256';"
    )
    assert runner._imported_artifact(period, refresh=False) is None
    db.storage.execute("DELETE FROM metadata WHERE name='sqlite_import_state';")
    assert runner._imported_artifact(period, refresh=False) is None


@pytest.mark.ydb
def test_import_reuse_rejects_corrupted_source_manifest(tmp_path: Path) -> None:
    db, runner, _client = _runner(tmp_path)
    payload = {"kind": "daily", "date": "2026-09-23", "use_ai": False}
    _seed_imported_report(db, runner, payload)
    period = runner.period(ReportRequest.model_validate(payload))
    assert runner._imported_artifact(period, refresh=False) is not None
    db.storage.execute("UPDATE migration_records SET checksum='" + "0" * 64 + "' WHERE source_table='reports';")
    assert runner._imported_artifact(period, refresh=False) is None

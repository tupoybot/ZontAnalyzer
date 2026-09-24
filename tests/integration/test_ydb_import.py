"""SQLite migration against a disposable local YDB namespace."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.import_sqlite import import_backup
from zont_analyzer.adapters.ydb.ai_usage import AiUsageRepository
from zont_analyzer.adapters.ydb.database import YdbDatabase
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports.chart_data import _cache_key


def _source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT,model TEXT,raw_json TEXT,discovered_at TEXT);
            CREATE TABLE telemetry_series(id INTEGER PRIMARY KEY,device_id TEXT,source_type TEXT,
                entity_id TEXT,metric_key TEXT,unit TEXT,display_name TEXT,role TEXT,confidence REAL,
                provenance TEXT,origin TEXT);
            CREATE TABLE telemetry_samples(series_id INTEGER,timestamp_utc INTEGER,value_num REAL,
                value_text TEXT,quality TEXT,ingested_at TEXT,PRIMARY KEY(series_id,timestamp_utc));
            CREATE TABLE ingestion_cursors(device_id TEXT,data_type TEXT,timestamp_utc INTEGER,
                updated_at TEXT,PRIMARY KEY(device_id,data_type));
            CREATE TABLE gas_tariffs(id TEXT PRIMARY KEY,scope TEXT,effective_month TEXT,
                effective_from TEXT,price TEXT,currency TEXT,recorded_at TEXT);
            CREATE TABLE gas_tariff_audit(id TEXT PRIMARY KEY,tariff_id TEXT,action TEXT,
                before_json TEXT,after_json TEXT,reason TEXT,created_at TEXT);
            CREATE TABLE llm_calls(id TEXT PRIMARY KEY,report_id TEXT,input_hash TEXT,prompt_version TEXT,
                model TEXT,reasoning_effort TEXT,input_tokens INTEGER,cached_tokens INTEGER,
                output_tokens INTEGER,status TEXT,request_id TEXT,created_at TEXT);
            CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
            INSERT INTO devices VALUES('device-1','Boiler',NULL,
                '{"model":"raw-model","_equipment":{"boiler_model":{"value":"A","source":"fixture"}}}',
                '2025-01-01 00:00:00.123456');
            INSERT INTO telemetry_series VALUES(47,'device-1','sensor','s1','temperature','C',
                'Room','room',0.9,'test','history');
            INSERT INTO telemetry_samples VALUES(47,1735689600,20.5,NULL,'valid',
                '2025-01-01 00:00:00.123456');
            INSERT INTO ingestion_cursors VALUES('device-1','history',1735689600,
                '2025-01-01 00:00:00.123456');
            INSERT INTO gas_tariffs VALUES('tariff-uuid','house','2025-01',
                '2025-01-01 00:00:00.123456','1.2300','RUB','2025-01-01 00:00:01.123456');
            INSERT INTO gas_tariff_audit VALUES('audit-uuid','tariff-uuid','create',NULL,
                '{"price":"1.2300"}',NULL,'2025-01-01 00:00:02.123456');
            INSERT INTO llm_calls VALUES('call-uuid','report-1','hash','v1','model','low',10,3,4,
                'failed','request-1','2025-01-01 00:00:03.123456');
            INSERT INTO alembic_version VALUES('f1a2b3c4d5e6');
        """)


def _rows(db: YdbDatabase, query: str) -> list[object]:
    return list(db.execute(query)[0].rows)


@pytest.mark.ydb
def test_import_repeat_delta_and_audit(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "online-backup.sqlite"
    _source(backup)
    first = import_backup(backup, ydb_database, batch_size=2)
    assert first["telemetry_samples"] == 1
    state = _rows(ydb_database, "SELECT value FROM metadata WHERE name='sqlite_import_state';")[0]
    assert json.loads(state.value) == {
        "state": "complete", "source_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    assert all(value == 0 for value in import_backup(backup, ydb_database, batch_size=2).values())


    device = json.loads(_rows(ydb_database, "SELECT payload FROM devices;")[0].payload)
    assert device == {
        "id": "device-1", "name": "Boiler", "model": None,
        "raw": {"model": "raw-model", "_equipment": {"boiler_model": {"value": "A", "source": "fixture"}}},
        "discovered_at": "2025-01-01T00:00:00.123456+00:00",
    }
    series = _rows(ydb_database, "SELECT id,payload FROM telemetry_series;")
    assert len(series) == 1 and series[0].id == 47
    sample = _rows(ydb_database, "SELECT timestamp_utc,ingested_at FROM telemetry_samples;")[0]
    assert sample.timestamp_utc == 1735689600
    assert sample.ingested_at == 1735689600123456
    assert _rows(ydb_database, "SELECT * FROM coverage;") == []
    tariff = json.loads(_rows(ydb_database, "SELECT payload FROM gas_tariffs;")[0].payload)
    assert tariff["id"] == "tariff-uuid"
    assert tariff["price"] == "1.2300"
    assert tariff["corrections"][0]["id"] == "audit-uuid"
    assert tariff["corrections"][0]["after"] == {"price": "1.2300"}
    audit = _rows(ydb_database, "SELECT id,payload FROM gas_tariff_audit;")[0]
    assert isinstance(audit.id, int)
    assert json.loads(audit.payload)["id"] == "audit-uuid"
    manifest = _rows(ydb_database, "SELECT payload FROM migration_records WHERE source_table='gas_tariff_audit';")
    assert json.loads(manifest[0].payload)["id"] == "audit-uuid"
    assert _rows(ydb_database, "SELECT * FROM ai_response_cache;") == []
    call_payload = json.loads(_rows(ydb_database, "SELECT payload FROM llm_calls;")[0].payload)
    assert (call_payload["id"], call_payload["cached_tokens"], call_payload["request_id"]) == (
        "call-uuid", 3, "request-1"
    )
    budget = _rows(ydb_database, "SELECT reserved_tokens,charged_tokens FROM ai_budget_months;")[0]
    assert (budget.reserved_tokens, budget.charged_tokens) == (0, 14)
    ydb_database.execute(
        "UPSERT INTO ai_budget_months (month,reserved_tokens,charged_tokens) "
        "VALUES ('2025-01',10,114);"
    )
    call = _rows(ydb_database, "SELECT created_at,sent_at FROM llm_calls;")[0]
    assert call.created_at == call.sent_at == 1735689603123456

    with sqlite3.connect(backup) as connection:
        connection.execute("UPDATE telemetry_samples SET value_num=21.75 WHERE series_id=47")
        connection.execute("UPDATE llm_calls SET input_tokens=15 WHERE id='call-uuid'")
        connection.execute("DELETE FROM gas_tariff_audit WHERE id='audit-uuid'")
        connection.execute(
            "INSERT INTO gas_tariff_audit VALUES(?,?,?,?,?,?,?)",
            ("another-uuid", "tariff-uuid", "correct", '{"price":"1.2300"}',
             '{"price":"1.2500"}', "meter", "2025-01-02 00:00:00.000001"),
        )
    changed = import_backup(backup, ydb_database, batch_size=2)
    assert changed["telemetry_samples"] == 1
    assert _rows(ydb_database, "SELECT value_num FROM telemetry_samples;")[0].value_num == 21.75
    assert json.loads(_rows(ydb_database, "SELECT payload FROM gas_tariff_audit;")[0].payload)["id"] == "another-uuid"
    tariff = json.loads(_rows(ydb_database, "SELECT payload FROM gas_tariffs;")[0].payload)
    assert tariff["corrections"] == [{"id": "another-uuid", "action": "correct",
                                     "before": {"price": "1.2300"}, "after": {"price": "1.2500"},
                                     "reason": "meter", "created_at": 1735776000000001}]
    assert len(_rows(ydb_database, "SELECT * FROM migration_records WHERE source_table='gas_tariff_audit';")) == 1
    budget = _rows(ydb_database, "SELECT reserved_tokens,charged_tokens FROM ai_budget_months;")[0]
    assert (budget.reserved_tokens, budget.charged_tokens) == (10, 119)
    assert all(value == 0 for value in import_backup(backup, ydb_database, batch_size=2).values())


@pytest.mark.ydb
def test_telemetry_pages_resume_and_repair_target(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "paged.sqlite"
    _source(backup)
    with sqlite3.connect(backup) as connection:
        connection.executemany(
            "INSERT INTO telemetry_samples VALUES(?,?,?,?,?,?)",
            [(47, 1735689600 + offset, float(offset), None, "valid", "2025-01-01 00:00:00")
             for offset in range(1, 503)],
        )
    assert import_backup(backup, ydb_database, batch_size=500)["telemetry_samples"] == 503
    assert import_backup(backup, ydb_database, batch_size=500)["telemetry_samples"] == 0
    ydb_database.execute(
        "UPSERT INTO telemetry_samples (series_id,timestamp_utc,value_num) "
        "VALUES (47,1735689601,-1.0);"
    )
    assert import_backup(backup, ydb_database, batch_size=500)["telemetry_samples"] == 1
    assert _rows(ydb_database, "SELECT value_num FROM telemetry_samples "
                 "WHERE series_id=47 AND timestamp_utc=1735689601;")[0].value_num == 1.0
    with sqlite3.connect(backup) as connection:
        connection.execute("DELETE FROM telemetry_samples WHERE timestamp_utc=1735689601")
    assert import_backup(backup, ydb_database, batch_size=500)["telemetry_samples"] == 1
    assert _rows(ydb_database, "SELECT COUNT(*) AS n FROM telemetry_samples;")[0].n == 502


@pytest.mark.ydb
def test_unmapped_source_table_fails_before_writes(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "bad.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE unknown_data(id TEXT PRIMARY KEY)")
    with pytest.raises(ValueError, match="unmapped SQLite tables"):
        import_backup(backup, ydb_database)
    assert _rows(ydb_database, "SELECT * FROM migration_records;") == []


@pytest.mark.ydb
def test_broken_source_foreign_key_fails_before_writes(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "broken.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.executescript("""
            CREATE TABLE devices(id TEXT PRIMARY KEY);
            CREATE TABLE entities(id TEXT PRIMARY KEY,device_id TEXT REFERENCES devices(id));
            INSERT INTO entities VALUES('orphan','missing');
        """)
    with pytest.raises(ValueError, match="foreign_key_check"):
        import_backup(backup, ydb_database)
    assert _rows(ydb_database, "SELECT * FROM migration_records;") == []


@pytest.mark.ydb
def test_corrupt_backup_fails_before_writes(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "corrupt.sqlite"
    backup.write_bytes(b"not a SQLite backup")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        import_backup(backup, ydb_database)
    assert _rows(ydb_database, "SELECT * FROM migration_records;") == []


@pytest.mark.ydb
def test_report_text_and_model_review_history(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    backup = tmp_path / "history.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.executescript("""
            CREATE TABLE reports(id TEXT PRIMARY KEY,kind TEXT,period_start INTEGER,period_end INTEGER,
                canonical_json TEXT,algorithm_version TEXT);
            CREATE TABLE notification_outbox(id TEXT PRIMARY KEY,report_id TEXT,channel TEXT,payload TEXT,
                status TEXT,attempts INTEGER);
            CREATE TABLE recommendations(id TEXT PRIMARY KEY,report_id TEXT,payload_json TEXT,status TEXT,
                rejection_reason TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE model_review_state(scope TEXT PRIMARY KEY,last_success_at TEXT,next_due_at TEXT,
                last_attempt_at TEXT,attempts INTEGER,lease_token TEXT,lease_until TEXT,last_error TEXT,
                updated_at TEXT);
            CREATE TABLE model_review_runs(id TEXT PRIMARY KEY,scope TEXT,started_at TEXT,finished_at TEXT,
                trigger TEXT,status TEXT,settings_version TEXT,settings_json TEXT,sources_json TEXT,
                catalog_json TEXT,result_json TEXT,error TEXT);
            CREATE TABLE model_review_proposals(id TEXT PRIMARY KEY,run_id TEXT,status TEXT,
                settings_version TEXT,profile TEXT,current_model TEXT,candidate_model TEXT,
                recommendation_json TEXT,created_at TEXT,decided_at TEXT,decision_note TEXT,version INTEGER);
            INSERT INTO reports VALUES('report-1','daily',1735689600,1735776000,
                '{"summary":"Summary","context":{"ai_provenance":{"model":"fixture"}}}','v1');
            INSERT INTO notification_outbox VALUES('outbox-1','report-1','log','Full rendered report',
                'pending',2);
            INSERT INTO recommendations VALUES('rec-1','report-1','{"id":"rec-1","title":"Fixture"}',
                'new',NULL,'2025-01-01 00:00:00.123456','2025-01-01 00:00:01.123456');
            INSERT INTO model_review_state VALUES('installation','2025-01-01 00:00:00.123456',NULL,
                NULL,2,NULL,NULL,NULL,'2025-01-01 00:00:01.123456');
            INSERT INTO model_review_runs VALUES('run-1','installation','2025-01-01 00:00:00.123456',
                '2025-01-01 00:00:01.123456','scheduled','completed','3','{"version":"3"}',
                '["official"]','{"model":"fixture"}','{"accepted":true}',NULL);
            INSERT INTO model_review_proposals VALUES('proposal-1','run-1','open','3','default',
                'old','new','{"reason":"fixture"}','2025-01-01 00:00:01.123456',NULL,NULL,1);
        """)
    import_backup(backup, ydb_database, batch_size=1)
    report = json.loads(_rows(ydb_database, "SELECT payload FROM reports;")[0].payload)
    assert report == {"report": {"summary": "Summary", "context": {"ai_provenance": {"model": "fixture"}}},
                      "rendered_text": "Full rendered report"}
    outbox = _rows(ydb_database, "SELECT channel,attempts FROM notification_outbox;")[0]
    assert (outbox.channel, outbox.attempts) == ("log", 2)
    recommendation = _rows(ydb_database, "SELECT created_at,updated_at FROM recommendations;")[0]
    assert (recommendation.created_at, recommendation.updated_at) == (1735689600123456, 1735689601123456)
    state = json.loads(_rows(ydb_database, "SELECT payload FROM model_review_state;")[0].payload)
    assert state["last_success_at"] == "2025-01-01T00:00:00.123456+00:00"
    run_row = _rows(ydb_database, "SELECT started_at,payload FROM model_review_runs;")[0]
    run = json.loads(run_row.payload)
    assert run_row.started_at == run["started_at_us"] == 1735689600123456
    assert run["settings"] == {"version": "3"}
    assert run["sources"] == ["official"]
    assert run["result"] == {"accepted": True}
    proposal = json.loads(_rows(ydb_database, "SELECT payload FROM model_review_proposals;")[0].payload)
    assert proposal["recommendation"] == {"reason": "fixture"}
    assert proposal["decided_at"] is None


@pytest.mark.ydb
def test_optional_chart_cache_bundle_preserves_matching_packet(tmp_path: Path, ydb_database: YdbDatabase) -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    report = Report(
        id="report-1", kind="daily", period_start=start, period_end=start + timedelta(days=1),
        generated_at=start + timedelta(days=1), timezone="UTC", summary="Fixture",
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
        context={"z": {"b": 2, "a": 1}, "a": {"y": 2, "x": 1}},
    )
    canonical = report.model_dump_json()
    backup = tmp_path / "online-backup.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "CREATE TABLE reports(id TEXT PRIMARY KEY,kind TEXT,period_start INTEGER,period_end INTEGER,"
            "canonical_json TEXT,algorithm_version TEXT)"
        )
        connection.execute("INSERT INTO reports VALUES(?,?,?,?,?,?)", (
            report.id, report.kind, int(report.period_start.timestamp()), int(report.period_end.timestamp()),
            canonical, report.algorithm_version,
        ))
    bundle = tmp_path / "chart-data-cache"
    bundle.mkdir()
    name = hashlib.sha256(report.id.encode()).hexdigest() + ".json"
    packet = {"schema_version": 2, "report_digest": hashlib.sha256(canonical.encode()).hexdigest(),
              "data": {"timezone": "UTC", "series": {"control_temperature": {"points": []}}}}
    (bundle / name).write_text(json.dumps(packet), encoding="utf-8")
    assert import_backup(backup, ydb_database, chart_cache=bundle)["chart_cache"] == 1
    key, digest = _cache_key(report)
    stored_report = Report.model_validate(json.loads(_rows(ydb_database, "SELECT payload FROM reports;")[0].payload)[
        "report"
    ])
    assert _cache_key(stored_report) == (key, digest)
    stored = _rows(ydb_database, f"SELECT value FROM app_meta WHERE key='{key}';")[0]
    assert json.loads(stored.value) == {**packet, "report_digest": digest}
    assert import_backup(backup, ydb_database, chart_cache=bundle)["chart_cache"] == 0
    packet["report_digest"] = "stale"
    (bundle / name).write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match accepted report"):
        import_backup(backup, ydb_database, chart_cache=bundle)
    assert json.loads(_rows(ydb_database, f"SELECT value FROM app_meta WHERE key='{key}';")[0].value)[
        "report_digest"
    ] == digest
    (bundle / name).unlink()
    assert import_backup(backup, ydb_database, chart_cache=bundle)["chart_cache"] == 1
    assert _rows(ydb_database, f"SELECT value FROM app_meta WHERE key='{key}';") == []


@pytest.mark.ydb
def test_optional_ai_ledger_preserves_results_reservations_and_delta(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    backup = tmp_path / "online-backup.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "CREATE TABLE llm_calls(id TEXT PRIMARY KEY,report_id TEXT,status TEXT,created_at TEXT,"
            "input_tokens INTEGER,output_tokens INTEGER)"
        )
        connection.execute("INSERT INTO llm_calls VALUES(?,?,?,?,?,?)", (
            "old-call", "report-1", "success", "2025-01-01 00:00:00", 10, 4,
        ))
    ydb_database.execute(
        "UPSERT INTO ai_budget_months (month,reserved_tokens,charged_tokens) "
        "VALUES ('2025-01',10,100);"
    )
    keys = ("a" * 64, "b" * 64, "c" * 64)
    success = {"status": "success", "created_at": 1735689600.25, "billing_month": "2025-01",
               "input_tokens": 10, "output_tokens": 4, "result": {
                   "summary": "Cached result", "provenance": {"requested_model": "fixture-model",
                   "settings_version": "3", "ai_log_id": "old-call"},
               }}
    pending = {"status": "pending", "created_at": 1735689601.0, "billing_month": "2025-01",
               "reserved_tokens": 17}
    failed = {"status": "failure", "created_at": 1735689602.0, "billing_month": "2025-01",
              "input_tokens": 0, "output_tokens": 0, "charged_tokens": 11, "error": "fixture failure"}
    bundle = tmp_path / "online-backup.sqlite.ai-ledger.json"
    bundle.write_text(json.dumps({"entries": dict(zip(keys, (success, pending, failed), strict=True))}))
    first = import_backup(backup, ydb_database, ai_ledger=bundle)
    assert first["ai_ledger"] == 3
    budget = _rows(ydb_database, "SELECT reserved_tokens,charged_tokens FROM ai_budget_months;")[0]
    assert (budget.reserved_tokens, budget.charged_tokens) == (27, 125)
    assert AiUsageRepository(ydb_database).cached(keys[0])["result"]["summary"] == "Cached result"
    assert AiUsageRepository(ydb_database).cached(keys[1])["status"] == "unknown"
    assert import_backup(backup, ydb_database, ai_ledger=bundle)["ai_ledger"] == 0
    changed = dict(zip(keys[:2], (success, {**pending, "status": "success", "result": {
        "summary": "Recovered result"
    }}), strict=True))
    bundle.write_text(json.dumps({"entries": changed}))
    assert import_backup(backup, ydb_database, ai_ledger=bundle)["ai_ledger"] == 2
    budget = _rows(ydb_database, "SELECT reserved_tokens,charged_tokens FROM ai_budget_months;")[0]
    assert (budget.reserved_tokens, budget.charged_tokens) == (10, 114)
    assert AiUsageRepository(ydb_database).cached(keys[1])["result"]["summary"] == "Recovered result"
    assert _rows(ydb_database, "SELECT * FROM llm_calls WHERE call_key='" + keys[2] + "';") == []


@pytest.mark.ydb
def test_ai_ledger_rejects_invalid_entry_before_bundle_writes(
    tmp_path: Path, ydb_database: YdbDatabase,
) -> None:
    backup = tmp_path / "empty.sqlite"
    with sqlite3.connect(backup) as connection:
        connection.execute("CREATE TABLE app_meta(key TEXT PRIMARY KEY,value TEXT)")
    bundle = tmp_path / "ledger.json"
    valid = {"status": "pending", "created_at": 1735689600.0, "reserved_tokens": 5}
    invalid = {"status": "success", "created_at": 1735689600.0, "billing_month": "2025-01"}
    bundle.write_text(json.dumps({"entries": {"a" * 64: valid, "b" * 64: invalid}}))
    with pytest.raises(ValueError, match="no cached result"):
        import_backup(backup, ydb_database, ai_ledger=bundle)
    state = _rows(ydb_database, "SELECT value FROM metadata WHERE name='sqlite_import_state';")[0]
    assert json.loads(state.value)["state"] == "running"
    assert _rows(ydb_database, "SELECT * FROM ai_response_cache;") == []
    assert _rows(ydb_database, "SELECT * FROM ai_budget_months;") == []
    assert _rows(ydb_database, "SELECT * FROM migration_records WHERE source_table='ai_ledger';") == []
    bundle.write_text(json.dumps({"entries": {"a" * 64: valid}}))
    assert import_backup(backup, ydb_database, ai_ledger=bundle)["ai_ledger"] == 1
    state = _rows(ydb_database, "SELECT value FROM metadata WHERE name='sqlite_import_state';")[0]
    assert json.loads(state.value)["state"] == "complete"
    budget = _rows(ydb_database, "SELECT month,reserved_tokens FROM ai_budget_months;")[0]
    assert (budget.month, budget.reserved_tokens) == ("2025-01", 5)

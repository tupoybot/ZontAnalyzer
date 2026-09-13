from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from zont_analyzer.adapters.sqlite.database import Database
from zont_analyzer.adapters.sqlite.publication_journal import changes_since, latest_revision, mark_change, record_change

# Representative trigger SQL is intentionally kept close to its test case.
# ruff: noqa: E501


def _database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "journal.sqlite3")
    db.initialize()
    return db


def test_journal_coalesces_keys_and_keeps_monotonic_high_water_mark(tmp_path: Path) -> None:
    db = _database(tmp_path)
    record_change(db, "render", "report-1")
    first = latest_revision(db)
    record_change(db, "render", "report-1")
    second = latest_revision(db)
    assert second > first
    assert changes_since(db, first) == (second, [{"scope": "render", "identifier": "report-1", "revision": second}])
    assert changes_since(db, second) == (second, [])


def test_journal_range_and_rollback(tmp_path: Path) -> None:
    db = _database(tmp_path)
    record_change(db, "render", "a")
    start = latest_revision(db)
    record_change(db, "global", "gas")
    middle = latest_revision(db)
    record_change(db, "telemetry", "2026-09-13")
    end = latest_revision(db)
    assert [item["identifier"] for item in changes_since(db, start, middle)[1]] == ["gas"]
    assert changes_since(db, start, end)[0] == end
    try:
        with db.session() as session:
            session.execute(text("INSERT INTO publication_changes(scope, identifier) VALUES ('render', 'rolled-back')"))
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    assert changes_since(db, end)[1] == []


def test_representative_triggers_coalesce_report_feedback_gas_and_telemetry(tmp_path: Path) -> None:
    db = _database(tmp_path)
    with db.session() as session:
        session.execute(text("INSERT INTO reports(id,kind,period_start,period_end,canonical_json,generated_at,algorithm_version) VALUES ('r','daily',1,2,'{}',CURRENT_TIMESTAMP,'v')"))
        session.execute(text("INSERT INTO recommendations(id,report_id,category,priority,status,payload_json,created_at,updated_at) VALUES ('rec','r','x','low','new','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        session.execute(text("INSERT INTO devices(id,name,model,raw_json,discovered_at) VALUES ('d','old','m','{}',CURRENT_TIMESTAMP)"))
    before = latest_revision(db)
    with db.session() as session:
        session.execute(text("UPDATE recommendations SET status='applied',updated_at=CURRENT_TIMESTAMP WHERE id='rec'"))
        session.execute(text("UPDATE devices SET raw_json='{""changed"":true}' WHERE id='d'"))
        mark_change(session, "telemetry", "2026-09-13T12")
    high, items = changes_since(db, before)
    assert high >= before
    assert {(item["scope"], item["identifier"]) for item in items} >= {
        ("render", "r"), ("telemetry", "2026-09-13T12"),
    }
    assert ("global", "gas") not in {(item["scope"], item["identifier"]) for item in items}


def test_noop_updates_are_ignored_and_intervention_delete_is_safe(tmp_path: Path) -> None:
    db = _database(tmp_path)
    with db.session() as session:
        session.execute(text("INSERT INTO reports(id,kind,period_start,period_end,canonical_json,generated_at,algorithm_version) VALUES ('r','daily',1,2,'{}',CURRENT_TIMESTAMP,'v')"))
        session.execute(text("INSERT INTO recommendations(id,report_id,category,priority,status,payload_json,created_at,updated_at) VALUES ('rec','r','x','low','new','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
    baseline = latest_revision(db)
    with db.session() as session:
        session.execute(text("UPDATE recommendations SET updated_at=CURRENT_TIMESTAMP WHERE id='rec'"))
    assert changes_since(db, baseline)[1] == []
    with db.session() as session:
        session.execute(text("INSERT INTO interventions(id,recommendation_id,applied_at,note) VALUES ('i','rec',CURRENT_TIMESTAMP,'done')"))
    baseline = latest_revision(db)
    with db.session() as session:
        session.execute(text("DELETE FROM interventions WHERE id='i'"))
    high, items = changes_since(db, baseline)
    assert high > baseline
    assert {item["scope"] for item in items} == {"render", "global"}

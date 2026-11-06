"""Configuration cleanup uses real YDB transactions and exact private manifests."""
from copy import deepcopy

import pytest

from tests.ydb_support import make_database
from tools.cleanup_configuration_snapshots import (
    _MAX_BYTES,
    _chunks,
    apply_configuration_cleanup,
    plan_configuration_cleanup,
)
from tools.compact_configuration_export import compact
from zont_analyzer.adapters.ydb.telemetry import encode


def _row(number, setting="A", device="fixture"):
    return {"device_id": device, "content_hash": f"legacy-{number}", "id": number, "captured_at": number + 1,
            "payload": encode({"id": device, "online": bool(number % 2), "last_contact": number,
                               "z3k_config": {"limit": setting}, "name": "Синтетический котёл"})}


def _seed(db, rows):
    for row in rows:
        db.execute(
            "DECLARE $device AS Utf8; DECLARE $hash AS Utf8; DECLARE $id AS Int64; "
            "DECLARE $payload AS Utf8; DECLARE $at AS Int64; "
            "UPSERT INTO config_snapshots (device_id,content_hash,id,payload,captured_at) "
            "VALUES ($device,$hash,$id,$payload,$at);",
            {"$device": row["device_id"], "$hash": row["content_hash"], "$id": row["id"],
             "$payload": row["payload"], "$at": row["captured_at"]},
        )


def _stored(db):
    return [dict(row) for row in db.execute("SELECT * FROM config_snapshots;")[0].rows]


def _sorted(rows):
    return sorted(rows, key=lambda row: (row["device_id"], row["content_hash"]))


def test_plan_matches_exact_compact_reference_without_mutating_input():
    rows = [_row(4, "A"), _row(1, "A"), _row(3, "B"), _row(2, "A"), _row(1, "B", "second")]
    before = deepcopy(rows)
    plan = plan_configuration_cleanup(rows)
    assert [event["target"] for event in plan["events"]] == compact(rows)[0]
    assert [len(event["sources"]) for event in plan["events"]] == [2, 1, 1, 1]
    assert rows == before


def test_tampered_plan_is_rejected_before_any_database_operation():
    plan = plan_configuration_cleanup([_row(1)])
    plan["events"][0]["sources"].append(_row(2))
    with pytest.raises(ValueError, match="source manifest"):
        apply_configuration_cleanup(None, plan)
    plan = plan_configuration_cleanup([_row(1)])
    plan["version"] = True  # Python equality alone treats True and 1 as equal.
    with pytest.raises(ValueError, match="source manifest"):
        apply_configuration_cleanup(None, plan)


def test_duplicate_keys_and_oversized_single_source_are_rejected():
    with pytest.raises(ValueError, match="duplicate source primary key"):
        plan_configuration_cleanup([_row(1), {**_row(2), "content_hash": "legacy-1"}])
    with pytest.raises(ValueError, match="byte limit"):
        plan_configuration_cleanup([{**_row(1), "payload": "x" * (_MAX_BYTES + 1)}])


def test_chunk_limits_count_utf8_source_bytes():
    rows = [{**_row(index), "payload": "я" * 350_000} for index in range(7)]
    chunks = list(_chunks(rows, 50))
    assert [len(batch) for batch in chunks] == [2, 2, 2, 1]
    assert all(sum(len(row["payload"].encode()) for row in batch) <= _MAX_BYTES for batch in chunks)
    assert all(len(batch) <= 50 for batch in _chunks([_row(i) for i in range(101)], 50))


@pytest.mark.ydb
def test_compaction_preserves_A_B_A_timestamps_and_unrelated_state(tmp_path):
    application = make_database(tmp_path)
    db = application.storage
    rows = [_row(1), _row(2), _row(3, "B"), _row(4, "A"), _row(1, "B", "second")]
    _seed(db, rows)
    application.set_app_meta("fixture-sentinel", "retain")
    untouched = {name: [dict(row) for row in db.execute(f"SELECT * FROM {name};")[0].rows]
                 for name in ("devices", "app_meta", "sequences", "revisions", "publication_changes", "metadata")}
    summary = apply_configuration_cleanup(db, plan_configuration_cleanup(rows))
    assert summary["status"] == "complete"
    assert summary["deleted_rows"] == 5 and summary["upserted_rows"] == 4
    assert _sorted(_stored(db)) == _sorted(compact(rows)[0])
    assert {name: [dict(row) for row in db.execute(f"SELECT * FROM {name};")[0].rows]
            for name in untouched} == untouched


@pytest.mark.ydb
def test_interrupted_large_event_resumes_and_is_idempotent(tmp_path, monkeypatch):
    db = make_database(tmp_path).storage
    rows = [_row(index) for index in range(1, 56)]
    _seed(db, rows)
    plan = plan_configuration_cleanup(rows)
    original = db.transaction
    calls = 0

    def interrupt(callback):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return original(callback)

    with monkeypatch.context() as patch:
        patch.setattr(db, "transaction", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            apply_configuration_cleanup(db, plan)
    assert len(_stored(db)) == 6  # first fifty deleted, one compact target created
    resumed = apply_configuration_cleanup(db, plan)
    assert resumed["status"] == "complete" and resumed["upserted_rows"] == 0
    assert resumed["deleted_rows"] == 5
    assert _stored(db) == compact(rows)[0]
    repeated = apply_configuration_cleanup(db, plan)
    assert repeated["deleted_rows"] == repeated["upserted_rows"] == 0


@pytest.mark.parametrize("field,value", [("payload", "{}"), ("id", 999), ("captured_at", 999)])
@pytest.mark.ydb
def test_changed_source_aborts_entire_batch_without_writing(tmp_path, field, value):
    db = make_database(tmp_path).storage
    rows = [_row(1), _row(2)]
    plan = plan_configuration_cleanup(rows)
    _seed(db, [rows[0], {**rows[1], field: value}])
    before = _stored(db)
    result = apply_configuration_cleanup(db, plan)
    assert result["status"] == "conflicted"
    assert result["deleted_rows"] == result["upserted_rows"] == 0
    assert _stored(db) == before


@pytest.mark.ydb
def test_different_existing_target_is_not_overwritten(tmp_path):
    db = make_database(tmp_path).storage
    rows = [_row(1), _row(2)]
    plan = plan_configuration_cleanup(rows)
    _seed(db, [*rows, {**plan["events"][0]["target"], "payload": "{}"}])
    before = _stored(db)
    assert apply_configuration_cleanup(db, plan)["status"] == "conflicted"
    assert _stored(db) == before


@pytest.mark.ydb
def test_missing_source_without_target_fails_and_new_append_survives(tmp_path):
    db = make_database(tmp_path).storage
    rows = [_row(1), _row(2)]
    plan = plan_configuration_cleanup(rows)
    _seed(db, [rows[1]])
    assert apply_configuration_cleanup(db, plan)["status"] == "conflicted"
    assert _stored(db) == [rows[1]]
    appended = _row(3, "new setting")
    _seed(db, [rows[0], appended])
    assert apply_configuration_cleanup(db, plan)["status"] == "complete"
    assert _sorted(_stored(db)) == _sorted([*compact(rows)[0], appended])

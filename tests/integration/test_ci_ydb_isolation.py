"""Namespace reuse must preserve test isolation without reusing drivers."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from tests import ydb_support
from zont_analyzer.adapters.ydb.database import YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES


def _sentinel_values(definition: str) -> tuple[list[str], list[str]]:
    match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", definition)
    assert match is not None
    columns = [name.strip() for name in match.group(1).split(",")]
    literals = []
    for column in columns:
        kind = re.search(rf"\b{column}\s+(Utf8|Int64)\b", definition)
        assert kind is not None, column
        literals.append("1" if kind.group(1) == "Int64" else "'sentinel'")
    return columns, literals


@pytest.mark.ydb
def test_all_tables_and_extra_metadata_are_empty_on_reuse(tmp_path: Path) -> None:
    first = ydb_support.make_database(tmp_path)
    namespace = first.storage.path
    for table, definition in TABLES.items():
        columns, literals = _sentinel_values(definition)
        first.storage.execute(
            f"UPSERT INTO `{table}` ({','.join(columns)}) VALUES ({','.join(literals)});"
        )
    ydb_support.cleanup_databases()

    second = ydb_support.make_database(tmp_path)
    assert second is not first and second.storage is not first.storage
    assert second.storage.path == namespace
    for table in TABLES:
        rows = second.storage.execute(f"SELECT * FROM `{table}`;")[0].rows
        if table == "metadata":
            assert {row.name: row.value for row in rows} == ydb_support._CANONICAL_METADATA
        else:
            assert rows == []


@pytest.mark.ydb
def test_simultaneous_databases_have_independent_leases(tmp_path: Path) -> None:
    left = ydb_support.make_database(tmp_path)
    right = ydb_support.make_database(tmp_path)
    assert left is not right and left.storage.path != right.storage.path
    left.set_app_meta("independent", "left")
    right.set_app_meta("independent", "right")
    assert left.get_app_meta("independent") == "left"
    assert right.get_app_meta("independent") == "right"


@pytest.mark.ydb
def test_direct_client_on_leased_namespace_is_closed_before_reuse(tmp_path: Path) -> None:
    first = ydb_support.make_database(tmp_path)
    namespace = first.storage.path
    extra = YdbDatabase(first.storage.config)
    closed = []
    original_close = extra.close

    def close_spy() -> None:
        original_close()
        closed.append(True)

    extra.close = close_spy  # type: ignore[method-assign]
    extra.execute("UPSERT INTO app_meta (key,value) VALUES ('extra-client','written');")
    ydb_support.cleanup_databases()
    assert closed == [True]
    second = ydb_support.make_database(tmp_path)
    assert second.storage.path == namespace
    assert second.get_app_meta("extra-client") is None


@pytest.mark.ydb
def test_runtime_shares_one_database_per_path_only_within_test(tmp_path: Path) -> None:
    first = ydb_support.make_runtime(tmp_path)
    second = ydb_support.make_runtime(tmp_path)
    assert first is not second and first.db is second.db
    first.db.set_app_meta("runtime-sentinel", "old")
    namespace = first.db.storage.path
    ydb_support.cleanup_databases()
    third = ydb_support.make_runtime(tmp_path)
    assert third.db is not first.db and third.db.storage is not first.db.storage
    assert third.db.storage.path == namespace
    assert third.db.get_app_meta("runtime-sentinel") is None


@pytest.mark.ydb
def test_failed_reset_quarantines_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = ydb_support.make_database(tmp_path)
    namespace = first.storage.path
    before = ydb_support.pool_statistics()["schema_quarantines"]
    with monkeypatch.context() as patch:
        patch.setattr(ydb_support, "_reset_slot", lambda _slot: (_ for _ in ()).throw(RuntimeError("reset failed")))
        with pytest.raises(RuntimeError, match="cleanup failed"):
            ydb_support.cleanup_databases()
    second = ydb_support.make_database(tmp_path)
    assert second.storage.path != namespace
    assert ydb_support.pool_statistics()["schema_quarantines"] == before + 1


@pytest.mark.parametrize("thread_name", ["regenerate-blocked-isolation", "unregistered-writer"])
@pytest.mark.ydb
def test_background_drain_timeout_quarantines_every_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, thread_name: str,
) -> None:
    first = ydb_support.make_database(tmp_path)
    second = ydb_support.make_database(tmp_path)
    old_namespaces = {first.storage.path, second.storage.path}
    assert len(old_namespaces) == 2
    quarantines_before = ydb_support.pool_statistics()["schema_quarantines"]
    entered = threading.Event()
    release = threading.Event()

    def blocked_job() -> None:
        entered.set()
        release.wait()

    thread = threading.Thread(target=blocked_job, name=thread_name, daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        with monkeypatch.context() as patch:
            patch.setattr(ydb_support, "_DRAIN_SECONDS", 0.02)
            with pytest.raises(RuntimeError, match="cleanup failed") as failure:
                ydb_support.cleanup_databases()
        assert isinstance(failure.value.__cause__, TimeoutError)
        expected_phase = "known app" if thread_name.startswith("regenerate-") else "new test"
        assert expected_phase in str(failure.value.__cause__)
        assert ydb_support.pool_statistics()["schema_quarantines"] == quarantines_before + 2
    finally:
        release.set()
        thread.join(timeout=1)
    assert not thread.is_alive()
    replacements = [ydb_support.make_database(tmp_path) for _ in range(2)]
    assert all(db.storage.path not in old_namespaces for db in replacements)


@pytest.mark.ydb
def test_late_named_writer_finishes_before_reset(tmp_path: Path) -> None:
    first = ydb_support.make_database(tmp_path)
    namespace = first.storage.path
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def delayed_write() -> None:
        entered.set()
        release.wait(2)
        first.set_app_meta("late-write", "completed")
        done.set()

    thread = threading.Thread(target=delayed_write, name="regenerate-isolation-test", daemon=True)
    thread.start()
    assert entered.wait(1)
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        ydb_support.cleanup_databases()
    finally:
        release.set()
        timer.join(timeout=1)
    assert done.is_set() and not thread.is_alive()
    second = ydb_support.make_database(tmp_path)
    assert second.storage.path == namespace
    assert second.get_app_meta("late-write") is None


@pytest.mark.ydb
def test_fresh_schema_never_enters_reuse_pool(tmp_path: Path) -> None:
    fresh = ydb_support.make_database(tmp_path, fresh_schema=True)
    namespace = fresh.storage.path
    ydb_support.cleanup_databases()
    pooled = ydb_support.make_database(tmp_path)
    assert pooled.storage.path != namespace

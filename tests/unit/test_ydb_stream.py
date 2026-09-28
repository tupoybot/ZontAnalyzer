from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import ydb

from zont_analyzer.adapters.ydb.database import Transaction, YdbDatabase


class Stream:
    def __init__(self, parts: list[Any]):
        self.parts = parts

    @contextmanager
    def execute(self, *_: Any, **__: Any) -> Any:
        yield iter(self.parts)


def test_result_parts_merge_by_logical_index() -> None:
    parts = [SimpleNamespace(index=0, rows=[1, 2], truncated=False),
             SimpleNamespace(index=0, rows=[3], truncated=False),
             SimpleNamespace(index=1, rows=[4], truncated=False)]
    results = Transaction(Stream(parts), "").execute("SELECT 1; SELECT 2;")
    assert [r.rows for r in results] == [[1, 2, 3], [4]]


def test_truncated_result_is_never_accepted() -> None:
    with pytest.raises(ValueError, match="truncated"):
        Transaction(Stream([SimpleNamespace(index=0, rows=[1], truncated=True)]), "").execute("SELECT 1;")


class TableStream:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.cancelled = False

    def __iter__(self):
        yield SimpleNamespace(rows=[1, 2])
        if self.fail:
            raise ydb.Unavailable("synthetic stream failure")
        yield SimpleNamespace(rows=[3])

    def cancel(self):
        self.cancelled = True


def test_read_table_retries_partial_stream_without_leaking_rows_or_sessions(monkeypatch) -> None:
    import ydb.retries

    monkeypatch.setattr(ydb.retries.time, "sleep", lambda _seconds: None)
    first, second = TableStream(fail=True), TableStream()
    session = Mock()
    session.create.return_value = session
    session.read_table.side_effect = [first, second]
    db = object.__new__(YdbDatabase)
    db.path = "/local/fixture"
    db.driver = Mock()
    db.driver.table_client.session.return_value = session

    assert db.read_table("telemetry_samples", columns=["timestamp_utc"],
                         key_range=None, consume=list) == [1, 2, 3]
    assert first.cancelled and second.cancelled
    assert session.delete.call_count == 2


def test_read_table_consumer_failure_cancels_stream_and_releases_session() -> None:
    stream = TableStream()
    session = Mock()
    session.create.return_value = session
    session.read_table.return_value = stream
    db = object.__new__(YdbDatabase)
    db.path = "/local/fixture"
    db.driver = Mock()
    db.driver.table_client.session.return_value = session

    def reject(rows):
        assert next(rows) == 1
        raise ValueError("invalid sample")

    with pytest.raises(ValueError, match="invalid sample"):
        db.read_table("telemetry_samples", columns=["timestamp_utc"], key_range=None, consume=reject)
    assert stream.cancelled
    session.delete.assert_called_once()

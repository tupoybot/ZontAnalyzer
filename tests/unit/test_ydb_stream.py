from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from zont_analyzer.adapters.ydb.database import Transaction


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

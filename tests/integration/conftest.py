from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.ydb_support import make_ydb_database
from zont_analyzer.adapters.ydb.database import YdbDatabase


@pytest.fixture
def ydb_database() -> Iterator[YdbDatabase]:
    yield make_ydb_database()

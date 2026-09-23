from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.schema import TABLES


@pytest.fixture
def ydb_database() -> Iterator[YdbDatabase]:
    endpoint = os.environ.get("YDB_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("requires isolated YDB; use deploy/check-ydb.sh")
    namespace = "test_" + uuid.uuid4().hex
    db = YdbDatabase(YdbConfig(endpoint, os.environ.get("YDB_TEST_DATABASE", "/local"), namespace, True))
    try:
        db.initialize()
        yield db
    finally:
        for name in reversed(TABLES):
            db.execute(f"DROP TABLE IF EXISTS `{name}`;")
        db.driver.scheme_client.remove_directory(db.path)
        db.close()

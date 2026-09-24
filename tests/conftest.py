from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def cleanup_application_databases() -> Iterator[None]:
    yield
    from tests.ydb_support import cleanup_databases
    cleanup_databases()

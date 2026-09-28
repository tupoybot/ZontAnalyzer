"""Operational reads cap requests and retries without changing normal queries."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer.adapters.ydb.database import YdbDatabase


def _database():
    db = object.__new__(YdbDatabase)
    db.prefix = "fixture-prefix:"
    db.pool = Mock()
    db.pool.execute_with_retries.return_value = [SimpleNamespace(index=0, truncated=False, rows=[])]
    return db


def test_bounded_query_disables_retries_and_sets_request_and_session_timeouts():
    db = _database()
    db.execute("SELECT 1;", timeout_seconds=2)
    call = db.pool.execute_with_retries.call_args
    assert call.args == ("fixture-prefix:SELECT 1;",)
    assert call.kwargs["settings"].timeout == 2
    retry = call.kwargs["retry_settings"]
    assert retry.max_retries == 0
    assert retry.max_session_acquire_timeout == retry.get_session_client_timeout == 1


def test_default_queries_preserve_sdk_request_and_retry_defaults():
    db = _database()
    db.execute("SELECT 1;")
    db.pool.execute_with_retries.assert_called_once_with("fixture-prefix:SELECT 1;", parameters=None)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_fails_before_query(timeout):
    db = _database()
    with pytest.raises(ValueError, match="positive and finite"):
        db.execute("SELECT 1;", timeout_seconds=timeout)
    db.pool.execute_with_retries.assert_not_called()

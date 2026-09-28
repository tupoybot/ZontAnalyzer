"""Fresh timer children must validate existing storage without startup writes."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer import runtime
from zont_analyzer.adapters.ydb.schema import TABLES
from zont_analyzer.config import AppConfig


def _database(monkeypatch, metadata):
    db = Mock()
    db.storage.execute.return_value = [SimpleNamespace(rows=[
        SimpleNamespace(name=name, value=value) for name, value in metadata.items()
    ])]
    db.list_devices.return_value = [{"id": "fixture", "raw": {"timezone": 4}}]
    loaded = SimpleNamespace(config=AppConfig())
    monkeypatch.setattr(runtime, "load_config", Mock(return_value=loaded))
    monkeypatch.setattr(runtime.YdbConfig, "from_environment", Mock())
    monkeypatch.setattr(runtime, "Database", Mock(return_value=db))
    lifecycle = Mock(side_effect=AssertionError("startup maintenance must not run"))
    monkeypatch.setattr(runtime.Runtime, "maintain_recommendation_lifecycle", lifecycle)
    return db, lifecycle


def test_fresh_background_runtimes_validate_schema_and_resolve_device_timezone(monkeypatch):
    db, lifecycle = _database(monkeypatch, {
        "schema_version": "2",
        "schema_hash": hashlib.sha256(json.dumps(TABLES, sort_keys=True).encode()).hexdigest(),
    })
    for _ in range(2):
        opened = runtime.open_runtime()
        assert opened.config.home.effective_timezone == "Etc/GMT-4"
        opened.db.close()
    assert db.storage.execute.call_count == 2
    assert db.list_devices.call_count == 2
    assert db.close.call_count == 2
    db.initialize.assert_not_called()
    lifecycle.assert_not_called()
    for call in db.storage.execute.call_args_list:
        assert call.args[0].startswith("SELECT ")


@pytest.mark.parametrize("metadata", [
    {}, {"schema_version": "1"}, {"schema_version": "2"},
    {"schema_version": "2", "schema_hash": "incompatible"},
])
def test_background_runtime_refuses_missing_or_incompatible_schema(monkeypatch, metadata):
    db, lifecycle = _database(monkeypatch, metadata)
    with pytest.raises(ValueError):
        runtime.open_runtime()
    db.close.assert_called_once()
    db.initialize.assert_not_called()
    db.list_devices.assert_not_called()
    lifecycle.assert_not_called()


@pytest.mark.ydb
def test_existing_runtime_schema_guard_uses_native_ydb(monkeypatch, tmp_path):
    from tests.ydb_support import make_database

    db = make_database(tmp_path)
    db.save_devices([{"id": "fixture", "timezone": 4}])
    monkeypatch.setattr(runtime, "load_config", Mock(return_value=SimpleNamespace(config=AppConfig())))
    monkeypatch.setattr(runtime.YdbConfig, "from_environment", Mock(return_value=db.storage.config))
    opened = runtime.open_runtime()
    try:
        assert opened.config.home.effective_timezone == "Etc/GMT-4"
        assert opened.db.get_schema_revision() == "2"
    finally:
        opened.db.close()

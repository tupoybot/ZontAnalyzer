from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.config import AppConfig


@pytest.fixture
def store(tmp_path: Path) -> AISettingsStore:
    db = Database(tmp_path / "settings.sqlite3")
    db.initialize()
    return AISettingsStore(db, AppConfig())


def test_settings_override_reset_restart_and_config_changes(store: AISettingsStore) -> None:
    initial = store.snapshot()
    assert initial["effective"]["review_interval_days"] == 60
    assert not initial["overridden"]
    saved = store.save({"expected_version": initial["version"], "values": {
        "enabled": True, "daily_reasoning_effort": "low",
    }})
    restarted = AISettingsStore(store.db, store.config)
    assert restarted.snapshot() == saved
    assert len(restarted.view()["history"]) == 1
    config = store.config.model_copy(deep=True)
    config.openai.daily_model = "gpt-5.6-sol"
    changed = AISettingsStore(store.db, config)
    assert changed.snapshot()["version"] != saved["version"]
    assert changed.effective_config().openai.daily_model == "gpt-5.6-sol"
    reset = changed.save({"expected_version": changed.snapshot()["version"], "reset": True})
    assert not reset["overridden"]
    assert reset["effective"]["daily_reasoning_effort"] == "medium"
    assert len(changed.view()["history"]) == 2
    assert not config.openai.enabled


def test_settings_snapshot_is_fixed_for_entire_analysis(store: AISettingsStore) -> None:
    fixed = store.effective_config()
    store.save({"expected_version": store.snapshot()["version"], "values": {"daily_model": "gpt-5.6-sol"}})
    assert fixed.openai.daily_model == "gpt-5.6-luna"
    assert store.effective_config().openai.daily_model == "gpt-5.6-sol"
    assert fixed.openai.settings_version != store.effective_config().openai.settings_version


def test_settings_concurrent_saves_cannot_overwrite_each_other(store: AISettingsStore) -> None:
    version = store.snapshot()["version"]

    def save(effort: str) -> bool:
        try:
            store.save({"expected_version": version, "values": {"daily_reasoning_effort": effort}})
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["low", "high"]))
    assert sum(results) == 1
    assert len(store.view()["history"]) == 1


@pytest.mark.parametrize("values", [
    {"enabled": "false"}, {"review_enabled": 0}, {"review_interval_days": True},
    {"review_interval_days": 0}, {"review_interval_days": 366}, {"api_key": "secret"},
    {"daily_model": "gpt-nonexistent"}, {"daily_model": "gpt-6-astra", "daily_reasoning_effort": "none"},
    {"review_reasoning_effort": "invented"}, {"daily_model": "gpt-5-mini", "daily_reasoning_effort": "max"},
])
def test_invalid_settings_leave_audit_and_state_unchanged(store: AISettingsStore, values: dict) -> None:
    before = store.view()
    with pytest.raises(ValueError):
        store.save({"expected_version": before["version"], "values": values})
    assert store.view() == before


def test_yaml_legacy_model_stays_usable_and_unrelated_settings_can_change(store: AISettingsStore) -> None:
    store.config.openai.daily_model = "custom-compatible-deployment"
    initial = store.snapshot()
    store.save({"expected_version": initial["version"], "values": {"review_interval_days": 30}})
    assert store.effective_config().openai.daily_model == "custom-compatible-deployment"


def test_repeated_identical_save_does_not_add_revision(store: AISettingsStore) -> None:
    first = store.save({"expected_version": store.snapshot()["version"], "values": {"enabled": True}})
    second = store.save({"expected_version": first["version"], "values": {"enabled": True}})
    assert first == second
    assert len(store.view()["history"]) == 1

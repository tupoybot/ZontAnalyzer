from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from zont_analyzer.config import AppConfig, load_config


def test_default_history_types_include_confirmed_radio_sensor_source() -> None:
    assert "z3k_radio_sensor" in AppConfig().zont.history_data_types


def test_config_is_optional_and_secrets_are_not_in_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZONT_TOKEN", "very-secret")
    monkeypatch.setenv("ZONT_CLIENT_EMAIL", "owner@example.test")
    loaded = load_config(data_dir=tmp_path / "data")
    assert loaded.config.home.timezone == "Europe/Samara"
    assert loaded.config.dhw.recirculation_present is True
    assert loaded.config.openai.reasoning_effort == "medium"
    assert loaded.secrets.zont_token is not None
    assert "very-secret" not in loaded.config.model_dump_json()


def test_unknown_yaml_fields_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("unknown_section:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(config, tmp_path / "data")


def test_token_file_must_be_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    token = tmp_path / "zontaccesstoken.json"
    token.write_text(json.dumps({"token": "secret", "email": "a@example.test"}), encoding="utf-8")
    token.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_config(data_dir=tmp_path / "data")
    token.chmod(0o600)
    assert load_config(data_dir=tmp_path / "data").secrets.zont_token is not None


def test_openai_key_can_come_from_private_access_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    access = tmp_path / ".access"
    access.mkdir()
    key = access / ".openai_access_token.txt"
    key.write_text("test-openai-key\n", encoding="utf-8")
    key.chmod(0o600)

    loaded = load_config(data_dir=tmp_path / "data")

    assert loaded.secrets.openai_api_key is not None
    assert loaded.secrets.openai_api_key.get_secret_value() == "test-openai-key"
    assert loaded.openai_key_path == key


def test_feedback_has_no_application_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZONT_FEEDBACK_TOKEN", "must-not-be-used")

    loaded = load_config(data_dir=tmp_path / "data")

    assert not hasattr(loaded.secrets, "feedback_token")
    assert "feedback_token" not in loaded.secrets.model_dump()


def test_legacy_feedback_token_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("feedback:\n  token_file: .access/feedback_token.txt\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(config, tmp_path / "data")


def test_dhw_recirculation_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("dhw:\n  recirculation_present: false\n", encoding="utf-8")

    assert load_config(config, tmp_path / "data").config.dhw.recirculation_present is False

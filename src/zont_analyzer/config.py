from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuietHours(StrictModel):
    from_: str = Field(default="23:00", alias="from")
    to: str = "08:00"


class HomeConfig(StrictModel):
    timezone: str = "Europe/Samara"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value


class PreferencesConfig(StrictModel):
    objective: Literal["neutral", "comfort", "balanced", "economy"] = "neutral"
    comfort_band_c: float = Field(default=0.5, gt=0, le=5)
    target_temperature_c: float | None = Field(default=None, ge=5, le=35)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)


class AnalysisConfig(StrictModel):
    daily_ai_when_normal: bool = False
    max_recommendations_per_report: int = Field(default=3, ge=1, le=5)
    default_experiment_days: int = Field(default=7, ge=1, le=60)
    minimum_quality_score: float = Field(default=0.70, ge=0, le=1)
    short_cycle_minutes: float = Field(default=5.0, gt=0, le=60)
    modulation_capability_profile: Literal["unknown", "flame_zero_is_minimum"] = "unknown"


class DomesticHotWaterConfig(StrictModel):
    recirculation_present: bool = True


class SafetyConfig(StrictModel):
    never_suggest_categories: list[str] = Field(default_factory=list)


class LogNotificationConfig(StrictModel):
    enabled: bool = True


class TelegramConfig(StrictModel):
    enabled: bool = False
    chat_id: int | None = None
    severity_at_least: Literal["info", "warning", "critical"] = "info"
    send_weekly_html: bool = True


class NotificationsConfig(StrictModel):
    log: LogNotificationConfig = Field(default_factory=LogNotificationConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)


class StorageConfig(StrictModel):
    backend: Literal["sqlite"] = "sqlite"
    path: str = "zont-analyzer.sqlite3"
    warn_disk_usage_pct: int = Field(default=25, ge=1, le=95)
    backup_dir: str = "backups"


class ZontConfig(StrictModel):
    base_url: str = "https://my.zont.online/api"
    client_email: str | None = None
    token_file: str = "zontaccesstoken.json"
    history_data_types: list[str] = Field(
        default_factory=lambda: [
            "temperature",
            "z3k_temperature",
            "z3k_radio_sensor",
            "z3k_heating_circuit",
            "z3k_boiler_adapter",
            "ztc_state",
        ]
    )
    request_timeout_seconds: float = Field(default=30, gt=0, le=300)
    sync_chunk_hours: int = Field(default=24, ge=1, le=168)


class OpenAIConfig(StrictModel):
    enabled: bool = False
    daily_model: str = "gpt-5.6-luna"
    review_model: str = "gpt-5.6-terra"
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    prompt_version: str = "analyst-v4"
    monthly_token_budget: int = Field(default=100_000, ge=0)


class SchedulerConfig(StrictModel):
    sync_every_minutes: int = Field(default=5, ge=1, le=1440)
    overlap_minutes: int = Field(default=15, ge=1, le=1440)


class PilotConfig(StrictModel):
    reports_dir: str = Field(default="reports", min_length=1)
    worker_status_file: str = Field(default="worker-status.json", min_length=1)
    max_catchup_days: int = Field(default=90, ge=1, le=3660)

    @field_validator("reports_dir", "worker_status_file")
    @classmethod
    def valid_path_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("path must be a non-empty filesystem path")
        return value


class FeedbackConfig(StrictModel):
    enabled: bool = True
    listen_host: str = Field(default="127.0.0.1", min_length=1)
    listen_port: int = Field(default=8787, ge=1, le=65535)
    public_api_base_url: str = Field(default="/api", min_length=1)

    @field_validator("listen_host", "public_api_base_url")
    @classmethod
    def valid_feedback_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("value must be non-empty")
        return value


class AppConfig(StrictModel):
    home: HomeConfig = Field(default_factory=HomeConfig)
    preferences: PreferencesConfig = Field(default_factory=PreferencesConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    dhw: DomesticHotWaterConfig = Field(default_factory=DomesticHotWaterConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    zont: ZontConfig = Field(default_factory=ZontConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    pilot: PilotConfig = Field(default_factory=PilotConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    entity_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Secrets(StrictModel):
    zont_token: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None


class LoadedConfig(StrictModel):
    config: AppConfig
    secrets: Secrets
    config_path: Path | None
    data_dir: Path
    sources: dict[str, str]
    zont_token_path: Path | None = None
    openai_key_path: Path | None = None


def _read_token_file(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Secret file {path} must have mode 0600")
    payload = json.loads(path.read_text(encoding="utf-8"))
    token = payload.get("token")
    email = payload.get("email") or payload.get("username")
    return (str(token) if token else None, str(email) if email else None)


def _read_private_text_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Secret file {path} must have mode 0600")
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def load_config(config_path: Path | None = None, data_dir: Path | None = None) -> LoadedConfig:
    root = (data_dir or Path(os.getenv("ZONT_ANALYZER_DATA_DIR", ".zont-analyzer"))).resolve()
    raw: dict[str, Any] = {}
    sources: dict[str, str] = {"*": "built-in default"}
    if config_path is None:
        env_path = os.getenv("ZONT_ANALYZER_CONFIG")
        candidates = [Path(env_path)] if env_path else [Path("config.yaml"), root / "config.yaml"]
        config_path = next((p for p in candidates if p.exists()), None)
    if config_path is not None:
        config_path = config_path.resolve()
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml root must be a mapping")
        raw = loaded
        sources["overrides"] = str(config_path)
    config = AppConfig.model_validate(raw)
    token = os.getenv("ZONT_TOKEN")
    client_email = os.getenv("ZONT_CLIENT_EMAIL")
    configured_token_path = Path(config.zont.token_file)
    if not configured_token_path.is_absolute():
        configured_token_path = Path.cwd() / configured_token_path
    token_candidates = [configured_token_path]
    secure_fallback = Path.cwd() / ".access" / "zontaccesstoken.json"
    if secure_fallback not in token_candidates:
        token_candidates.append(secure_fallback)
    token_path = next((candidate for candidate in token_candidates if candidate.exists()), configured_token_path)
    file_token, file_email = _read_token_file(token_path)
    token = token or file_token
    client_email = client_email or config.zont.client_email or file_email
    if client_email != config.zont.client_email:
        config = config.model_copy(update={"zont": config.zont.model_copy(update={"client_email": client_email})})
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_key_candidate = Path.cwd() / ".access" / ".openai_access_token.txt"
    openai_key_path = openai_key_candidate if openai_key_candidate.exists() else None
    if openai_key is None and openai_key_path is not None:
        openai_key = _read_private_text_secret(openai_key_path)
    secrets = Secrets(
        zont_token=SecretStr(token) if token else None,
        openai_api_key=SecretStr(openai_key) if openai_key else None,
        telegram_bot_token=SecretStr(value) if (value := os.getenv("TELEGRAM_BOT_TOKEN")) else None,
    )
    return LoadedConfig(
        config=config,
        secrets=secrets,
        config_path=config_path,
        data_dir=root,
        sources=sources,
        zont_token_path=token_path if token_path.exists() else None,
        openai_key_path=openai_key_path,
    )


def explain_config(loaded: LoadedConfig) -> dict[str, Any]:
    payload = loaded.config.model_dump(mode="json", by_alias=True)
    return {
        "effective": payload,
        "sources": loaded.sources,
        "secrets": {
            "zont_token": "present" if loaded.secrets.zont_token else "missing",
            "openai_api_key": "present" if loaded.secrets.openai_api_key else "missing",
            "telegram_bot_token": "present" if loaded.secrets.telegram_bot_token else "missing",
        },
    }

"""Versioned installation-wide AI settings; reads never contact OpenAI."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zont_analyzer.adapters.ydb.model_settings import ModelSettingsStorage, ModelSettingsTransaction
from zont_analyzer.config import AppConfig

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.application import Database

# Explicit Responses + strict output compatibility, verified 2026-09-08.
# Unknown models remain usable from YAML; web changes require a supported contract.
_MODERN = ("none", "low", "medium", "high", "xhigh", "max")
MODEL_EFFORTS = {
    "gpt-5.6-luna": _MODERN,
    "gpt-5.6-terra": _MODERN,
    "gpt-5.6-sol": _MODERN,
    "gpt-6-astra": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5-mini": ("minimal", "low", "medium", "high"),
}
_FIELDS = frozenset({
    "enabled", "daily_model", "review_model", "daily_reasoning_effort", "review_reasoning_effort",
    "review_enabled", "review_interval_days",
})


def validate_profile(model: str, effort: str) -> None:
    supported = MODEL_EFFORTS.get(model)
    if supported is None:
        raise ValueError(f"Для модели {model} совместимость с форматом анализа ещё не проверена.")
    if effort not in supported:
        raise ValueError(f"Модель {model} не поддерживает глубину {effort}.")


def _iso_us(value: int) -> str:
    return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat()


class AISettingsStore:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config
        self.storage = ModelSettingsStorage(db.storage)

    def _defaults(self) -> dict[str, Any]:
        ai = self.config.openai
        return {
            "enabled": ai.enabled, "daily_model": ai.daily_model, "review_model": ai.review_model,
            "daily_reasoning_effort": ai.daily_reasoning_effort or ai.reasoning_effort,
            "review_reasoning_effort": ai.review_reasoning_effort or ai.reasoning_effort,
            "review_enabled": ai.review_enabled, "review_interval_days": ai.review_interval_days,
        }

    def _snapshot(self, tx: ModelSettingsTransaction) -> dict[str, Any]:
        row = tx.settings_head()
        overrides = row["payload"].get("overrides", {}) if row else {}
        effective = self._defaults() | overrides
        fingerprint = json.dumps({"revision": row["version"] if row else 0, "effective": effective}, sort_keys=True)
        return {
            "version": hashlib.sha256(fingerprint.encode()).hexdigest(),
            "effective": effective, "overridden": bool(overrides),
            "overrides": overrides,
        }

    def snapshot(self) -> dict[str, Any]:
        return self.storage.transaction(self._snapshot)

    def snapshot_in_transaction(self, session: ModelSettingsTransaction) -> dict[str, Any]:
        return self._snapshot(session)

    def view(self) -> dict[str, Any]:
        def read(tx: ModelSettingsTransaction) -> dict[str, Any]:
            result = self._snapshot(tx)
            result["history"] = [{
                "id": row["version"], "created_at": _iso_us(row["effective_at"]),
                "values": row["payload"].get("overrides", {}),
                "before": row["payload"].get("before", {}),
            } for row in tx.settings_history(limit=50)]
            supported = self._supported_models(tx)
            result["models"] = [{"id": model, "efforts": list(efforts)}
                                for model, efforts in supported.items()]
            return result

        return self.storage.transaction(read)

    @staticmethod
    def _supported_models(tx: ModelSettingsTransaction) -> dict[str, tuple[str, ...]]:
        supported = dict(MODEL_EFFORTS)
        cutoff = datetime.now(UTC) - timedelta(days=60)
        for run in tx.runs("installation", limit=50):
            if (run.get("status") not in ("proposal", "no_change")
                    or datetime.fromisoformat(run["started_at"]) < cutoff):
                continue
            for fact in run.get("catalog", {}).get("models", []):
                if (fact.get("responses_supported") is True
                        and fact.get("structured_outputs_supported") is True
                        and not fact.get("deprecated") and fact.get("reasoning_efforts")):
                    supported[fact["id"]] = tuple(fact["reasoning_efforts"])
            break
        return supported

    def effective_config(self) -> AppConfig:
        snapshot = self.snapshot()
        return self.config.model_copy(deep=True, update={
            "openai": self.config.openai.model_copy(update={
                **snapshot["effective"], "settings_version": snapshot["version"],
            }),
        })

    def save(
        self, payload: dict[str, Any], *, session: ModelSettingsTransaction | None = None,
    ) -> dict[str, Any]:
        if session is not None:
            return self._save(session, payload)
        return self.storage.transaction(lambda tx: self._save(tx, payload))

    def _save(self, tx: ModelSettingsTransaction, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"expected_version", "values", "reset"}:
            raise ValueError("Неизвестные поля настроек AI.")
        current = self._snapshot(tx)
        if payload.get("expected_version") != current["version"]:
            raise ValueError("Настройки уже изменились. Откройте их заново перед сохранением.")
        if payload.get("reset") is True:
            if "values" in payload:
                raise ValueError("Сброс и изменение нельзя выполнить одновременно.")
            overrides: dict[str, Any] = {}
        else:
            values = payload.get("values")
            if "reset" in payload or not isinstance(values, dict) or not values or set(values) - _FIELDS:
                raise ValueError("Укажите допустимые значения настроек AI.")
            for name, value in values.items():
                if name in {"enabled", "review_enabled"}:
                    if type(value) is not bool:
                        raise ValueError("Включение должно быть логическим значением.")
                elif name == "review_interval_days":
                    if type(value) is not int or not 1 <= value <= 365:
                        raise ValueError("Интервал проверки: от 1 до 365 дней.")
                elif not isinstance(value, str) or not value or len(value) > 100:
                    raise ValueError("Некорректная модель или глубина рассуждения.")
            overrides = current["overrides"] | values
            effective = self._defaults() | overrides
            supported = self._supported_models(tx)
            for profile in ("daily", "review"):
                if {f"{profile}_model", f"{profile}_reasoning_effort"} & values.keys():
                    model, effort = effective[f"{profile}_model"], effective[f"{profile}_reasoning_effort"]
                    if model not in supported or effort not in supported[model]:
                        raise ValueError(f"Для модели {model} глубина {effort} или совместимость не подтверждены.")
        if overrides != current["overrides"]:
            row = tx.settings_head()
            tx.put_settings(
                (row["version"] if row else 0) + 1,
                {"overrides": overrides, "before": current["effective"]},
                time.time_ns() // 1_000,
            )
        return self._snapshot(tx)

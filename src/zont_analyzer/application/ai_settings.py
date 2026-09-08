"""Versioned installation-wide AI settings; reads never contact OpenAI."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, Text, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from zont_analyzer.adapters.sqlite.database import Base, Database, utcnow
from zont_analyzer.config import AppConfig

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


class AISettingsRevisionRow(Base):
    __tablename__ = "ai_settings_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    values_json: Mapped[str] = mapped_column(Text)
    before_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def validate_profile(model: str, effort: str) -> None:
    supported = MODEL_EFFORTS.get(model)
    if supported is None:
        raise ValueError(f"Для модели {model} совместимость с форматом анализа ещё не проверена.")
    if effort not in supported:
        raise ValueError(f"Модель {model} не поддерживает глубину {effort}.")


class AISettingsStore:
    def __init__(self, db: Database, config: AppConfig):
        self.db = db
        self.config = config

    def _defaults(self) -> dict[str, Any]:
        ai = self.config.openai
        return {
            "enabled": ai.enabled, "daily_model": ai.daily_model, "review_model": ai.review_model,
            "daily_reasoning_effort": ai.daily_reasoning_effort or ai.reasoning_effort,
            "review_reasoning_effort": ai.review_reasoning_effort or ai.reasoning_effort,
            "review_enabled": ai.review_enabled, "review_interval_days": ai.review_interval_days,
        }

    def _snapshot(self, session: Session) -> dict[str, Any]:
        row = session.scalar(select(AISettingsRevisionRow).order_by(AISettingsRevisionRow.id.desc()).limit(1))
        overrides = json.loads(row.values_json) if row else {}
        effective = self._defaults() | overrides
        fingerprint = json.dumps({"revision": row.id if row else 0, "effective": effective}, sort_keys=True)
        return {
            "version": hashlib.sha256(fingerprint.encode()).hexdigest(),
            "effective": effective, "overridden": bool(overrides),
            "overrides": overrides,
        }

    def snapshot(self) -> dict[str, Any]:
        with self.db.session() as session:
            return self._snapshot(session)

    def view(self) -> dict[str, Any]:
        with self.db.session() as session:
            result = self._snapshot(session)
            rows = session.scalars(select(AISettingsRevisionRow).order_by(AISettingsRevisionRow.id.desc()).limit(50))
            result["history"] = [{
                "id": row.id, "created_at": row.created_at.isoformat(),
                "values": json.loads(row.values_json), "before": json.loads(row.before_json),
            } for row in rows]
            supported = self._supported_models(session)
        result["models"] = [{"id": model, "efforts": list(efforts)} for model, efforts in supported.items()]
        return result

    @staticmethod
    def _supported_models(session: Session) -> dict[str, tuple[str, ...]]:
        from zont_analyzer.application.model_review import ModelReviewRunRow

        supported = dict(MODEL_EFFORTS)
        row = session.scalar(select(ModelReviewRunRow).where(
            ModelReviewRunRow.status.in_(("proposal", "no_change")),
            ModelReviewRunRow.started_at >= datetime.now(UTC) - timedelta(days=60),
        ).order_by(ModelReviewRunRow.started_at.desc()).limit(1))
        if row:
            for fact in json.loads(row.catalog_json).get("models", []):
                if (fact.get("responses_supported") is True and fact.get("structured_outputs_supported") is True
                        and not fact.get("deprecated") and fact.get("reasoning_efforts")):
                    supported[fact["id"]] = tuple(fact["reasoning_efforts"])
        return supported

    def effective_config(self) -> AppConfig:
        snapshot = self.snapshot()
        return self.config.model_copy(deep=True, update={
            "openai": self.config.openai.model_copy(update={
                **snapshot["effective"], "settings_version": snapshot["version"],
            }),
        })

    def save(self, payload: dict[str, Any], *, session: Session | None = None) -> dict[str, Any]:
        if session is not None:
            return self._save(session, payload)
        with self.db.session() as transaction:
            transaction.execute(text("BEGIN IMMEDIATE"))
            return self._save(transaction, payload)

    def _save(self, session: Session, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"expected_version", "values", "reset"}:
            raise ValueError("Неизвестные поля настроек AI.")
        current = self._snapshot(session)
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
            supported = self._supported_models(session)
            for profile in ("daily", "review"):
                if {f"{profile}_model", f"{profile}_reasoning_effort"} & values.keys():
                    model, effort = effective[f"{profile}_model"], effective[f"{profile}_reasoning_effort"]
                    if model not in supported or effort not in supported[model]:
                        raise ValueError(f"Для модели {model} глубина {effort} или совместимость не подтверждены.")
        if overrides != current["overrides"]:
            session.add(AISettingsRevisionRow(values_json=json.dumps(overrides, sort_keys=True),
                                             before_json=json.dumps(current["effective"], sort_keys=True)))
            session.flush()
        return self._snapshot(session)

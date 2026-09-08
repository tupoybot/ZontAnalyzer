"""Persistent, monthly gas tariffs with explicit audited corrections."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zont_analyzer.adapters.sqlite.database import Base, Database, utcnow

CURRENCIES = ("RUB", "USD", "EUR", "GBP", "KZT", "BYN")

_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_MAX_PRICE_DIGITS = 18
_MAX_PRICE_DECIMALS = 6
_MAX_SCOPE_LENGTH = 128
_MAX_REASON_LENGTH = 1000


class GasTariffRow(Base):
    __tablename__ = "gas_tariffs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, index=True)
    effective_month: Mapped[str] = mapped_column(String)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("scope", "effective_month"),)


class GasTariffAuditRow(Base):
    __tablename__ = "gas_tariff_audit"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    tariff_id: Mapped[str] = mapped_column(ForeignKey("gas_tariffs.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _price(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("price must be a finite non-negative decimal")
    text = str(value).strip()
    if isinstance(value, str):
        if "," in text and "." in text:
            raise ValueError("price must use one decimal separator")
        text = text.replace(",", ".")
    if not text or len(text) > 64:
        raise ValueError("price has an unsupported precision or magnitude")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price must be a finite non-negative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("price must be a finite non-negative decimal")
    _sign, digits, exponent = result.as_tuple()
    numeric_exponent = int(exponent)
    if (
        len(digits) > _MAX_PRICE_DIGITS
        or numeric_exponent < -_MAX_PRICE_DECIMALS
        or result.adjusted() >= _MAX_PRICE_DIGITS
    ):
        raise ValueError("price has an unsupported precision or magnitude")
    return "0" if result.is_zero() else format(result.normalize(), "f")


def _currency(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
    result = value.strip().upper()
    if result not in CURRENCIES:
        raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
    return result


def _scope(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_SCOPE_LENGTH:
        raise ValueError("scope must be a non-empty string")
    return value.strip()


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _next_month(now: datetime, zone: ZoneInfo) -> str:
    local = _as_utc(now).astimezone(zone)
    year, month = local.year, local.month + 1
    if month == 13:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def _month_start(month: Any, zone: ZoneInfo) -> tuple[str, datetime]:
    if not isinstance(month, str) or _MONTH_PATTERN.fullmatch(month) is None:
        raise ValueError("effective_month must use YYYY-MM")
    year, month_number = (int(part) for part in month.split("-"))
    if year < 1 or year > 9999:
        raise ValueError("effective_month is outside the supported range")
    try:
        start = datetime(year, month_number, 1, tzinfo=zone).astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("effective_month is outside the supported timezone range") from exc
    return month, start


def _snapshot(row: GasTariffRow) -> dict[str, str]:
    return {"price": row.price, "currency": row.currency}


class GasTariffStore:
    """Store tariff versions for one installation or a future explicit scope."""

    def __init__(self, db: Database, timezone: str = "UTC"):
        self.db = db
        self.timezone = timezone
        _zone(timezone)

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        with self.db.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            session = Session(bind=connection)
            try:
                yield session
                session.flush()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                session.close()

    @staticmethod
    def _audit(session: Session, tariff_id: str) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(GasTariffAuditRow)
            .where(GasTariffAuditRow.tariff_id == tariff_id)
            .order_by(GasTariffAuditRow.created_at, GasTariffAuditRow.id)
        ).all()
        return [
            {
                "id": row.id,
                "action": row.action,
                "before": json.loads(row.before_json) if row.before_json is not None else None,
                "after": json.loads(row.after_json),
                "reason": row.reason,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]

    @classmethod
    def _item(cls, session: Session, row: GasTariffRow) -> dict[str, Any]:
        audit = cls._audit(session, row.id)
        return {
            "id": row.id,
            "price": row.price,
            "currency": row.currency,
            "effective_month": row.effective_month,
            "effective_from": _iso(row.effective_from),
            "recorded_at": _iso(row.recorded_at),
            "corrections": [entry for entry in audit if entry["action"] == "correct"],
        }

    def history(self, scope: str = "installation") -> list[dict[str, Any]]:
        selected_scope = _scope(scope)
        with self.db.session() as session:
            rows = session.scalars(
                select(GasTariffRow)
                .where(GasTariffRow.scope == selected_scope)
                .order_by(GasTariffRow.effective_from, GasTariffRow.recorded_at, GasTariffRow.id)
            ).all()
            return [self._item(session, row) for row in rows]

    @staticmethod
    def _affected_end(session: Session, row: GasTariffRow) -> str | None:
        following = session.scalar(
            select(GasTariffRow)
            .where(GasTariffRow.scope == row.scope, GasTariffRow.effective_from > row.effective_from)
            .order_by(GasTariffRow.effective_from)
            .limit(1)
        )
        return _iso(following.effective_from) if following is not None else None

    def save(
        self,
        payload: dict[str, Any],
        scope: str = "installation",
        *,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("tariff payload must be an object")
        action = payload.get("action", "create")
        if action not in {"create", "correct"}:
            raise ValueError("action must be create or correct")
        selected_scope = _scope(scope)
        zone = _zone(timezone or self.timezone)
        if action == "create":
            allowed = {"action", "price", "currency", "effective_month"}
        else:
            allowed = {"action", "id", "price", "currency", "correction_reason"}
        if set(payload) - allowed:
            raise ValueError(f"{action} payload contains unsupported fields")
        price = _price(payload.get("price"))
        currency = _currency(payload.get("currency"))

        with self._write_session() as session:
            if action == "create":
                raw_month = payload.get("effective_month")
                month = _next_month(utcnow(), zone) if raw_month is None else raw_month
                effective_month, effective_from = _month_start(month, zone)
                existing = session.scalar(
                    select(GasTariffRow).where(
                        GasTariffRow.scope == selected_scope,
                        GasTariffRow.effective_month == effective_month,
                    )
                )
                if existing is not None:
                    row = existing
                    before = _snapshot(row)
                    if before == {"price": price, "currency": currency}:
                        idempotent = True
                        result_action = "create"
                    else:
                        current_month = utcnow().astimezone(zone).strftime("%Y-%m")
                        if effective_month <= current_month:
                            raise ValueError("historical tariff month already exists; use an explicit correction")
                        row.price = price
                        row.currency = currency
                        session.add(
                            GasTariffAuditRow(
                                id=str(uuid4()),
                                tariff_id=row.id,
                                action="correct",
                                before_json=json.dumps(before, sort_keys=True),
                                after_json=json.dumps(_snapshot(row), sort_keys=True),
                                reason=None,
                                created_at=utcnow(),
                            )
                        )
                        session.flush()
                        idempotent = False
                        result_action = "correct"
                else:
                    row = GasTariffRow(
                        id=str(uuid4()),
                        scope=selected_scope,
                        effective_month=effective_month,
                        effective_from=effective_from,
                        price=price,
                        currency=currency,
                        recorded_at=utcnow(),
                    )
                    session.add(row)
                    session.flush()
                    session.add(
                        GasTariffAuditRow(
                            id=str(uuid4()),
                            tariff_id=row.id,
                            action="create",
                            before_json=None,
                            after_json=json.dumps(_snapshot(row), sort_keys=True),
                            reason=None,
                            created_at=utcnow(),
                        )
                    )
                    idempotent = False
                    result_action = "create"
            else:
                tariff_id = payload.get("id")
                reason = payload.get("correction_reason")
                if not isinstance(tariff_id, str) or not tariff_id.strip():
                    raise ValueError("id is required for a correction")
                if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > _MAX_REASON_LENGTH:
                    raise ValueError("correction_reason must be a non-empty string")
                corrected_row = session.get(GasTariffRow, tariff_id.strip())
                if corrected_row is None or corrected_row.scope != selected_scope:
                    raise KeyError(tariff_id)
                row = corrected_row
                before = _snapshot(row)
                if before == {"price": price, "currency": currency}:
                    idempotent = True
                else:
                    row.price = price
                    row.currency = currency
                    session.add(
                        GasTariffAuditRow(
                            id=str(uuid4()),
                            tariff_id=row.id,
                            action="correct",
                            before_json=json.dumps(before, sort_keys=True),
                            after_json=json.dumps(_snapshot(row), sort_keys=True),
                            reason=reason.strip(),
                            created_at=utcnow(),
                        )
                    )
                    session.flush()
                    idempotent = False
                result_action = "correct"

            return {
                "action": result_action,
                "id": row.id,
                "tariff": self._item(session, row),
                "affected_start": _iso(row.effective_from),
                "affected_end": self._affected_end(session, row),
                "idempotent": idempotent,
            }

"""Validated monthly gas tariffs and audited corrections."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from zont_analyzer.adapters.ydb.application import Database


def utcnow() -> datetime:
    return datetime.now(UTC)


CURRENCIES = ("RUB", "USD", "EUR", "GBP", "KZT", "BYN")
_MONTH_PATTERN = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _price(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("price must be a finite non-negative decimal")
    raw = str(value).strip()
    if isinstance(value, str):
        if "," in raw and "." in raw:
            raise ValueError("price must use one decimal separator")
        raw = raw.replace(",", ".")
    if not raw or len(raw) > 64:
        raise ValueError("price has an unsupported precision or magnitude")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price must be a finite non-negative decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("price must be a finite non-negative decimal")
    _sign, digits, exponent = number.as_tuple()
    if len(digits) > 18 or int(exponent) < -6 or number.adjusted() >= 18:
        raise ValueError("price has an unsupported precision or magnitude")
    return "0" if number.is_zero() else format(number.normalize(), "f")


def _currency(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() not in CURRENCIES:
        raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
    return value.strip().upper()


def _scope(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise ValueError("scope must be a non-empty string")
    return value.strip()


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _month_start(month: Any, zone: ZoneInfo) -> tuple[str, datetime]:
    if not isinstance(month, str) or _MONTH_PATTERN.fullmatch(month) is None:
        raise ValueError("effective_month must use YYYY-MM")
    year, number = (int(part) for part in month.split("-"))
    if year < 1 or year > 9999:
        raise ValueError("effective_month is outside the supported range")
    try:
        start = datetime(year, number, 1, tzinfo=zone).astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("effective_month is outside the supported timezone range") from exc
    return month, start


class GasTariffStore:
    """Public tariff API; the repository owns atomic persistence and audit."""

    def __init__(self, db: Database, timezone: str = "UTC") -> None:
        self.db = db
        self.timezone = timezone
        _zone(timezone)

    def history(self, scope: str = "installation") -> list[dict[str, Any]]:
        return self.db.owner.application_tariff_history(_scope(scope))

    def save(
        self, payload: dict[str, Any], scope: str = "installation", *, timezone: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("tariff payload must be an object")
        action = payload.get("action", "create")
        if action not in {"create", "correct"}:
            raise ValueError("action must be create or correct")
        selected_scope = _scope(scope)
        zone = _zone(timezone or self.timezone)
        allowed = (
            {"action", "price", "currency", "effective_month"}
            if action == "create"
            else {"action", "id", "price", "currency", "correction_reason"}
        )
        if set(payload) - allowed:
            raise ValueError(f"{action} payload contains unsupported fields")
        price, currency = _price(payload.get("price")), _currency(payload.get("currency"))
        moment = utcnow()
        if action == "create":
            month = payload.get("effective_month")
            if month is None:
                local = moment.astimezone(zone)
                year, number = local.year, local.month + 1
                if number == 13:
                    year, number = year + 1, 1
                month = f"{year:04d}-{number:02d}"
            month, start = _month_start(month, zone)
            return self.db.owner.application_tariff_save(
                selected_scope,
                "create",
                month,
                start.isoformat(),
                price,
                currency,
                moment.isoformat(),
                current_month=moment.astimezone(zone).strftime("%Y-%m"),
            )
        tariff_id, reason = payload.get("id"), payload.get("correction_reason")
        if not isinstance(tariff_id, str) or not tariff_id.strip():
            raise ValueError("id is required for a correction")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("correction_reason must be a non-empty string")
        return self.db.owner.application_tariff_save(
            selected_scope,
            "correct",
            None,
            None,
            price,
            currency,
            moment.isoformat(),
            tariff_id=tariff_id.strip(),
            reason=reason.strip(),
        )

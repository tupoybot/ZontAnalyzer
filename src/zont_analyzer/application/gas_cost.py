"""Deterministic decimal pricing of gas volumes by calendar-month tariffs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any
from zoneinfo import ZoneInfo

_CURRENCY_LABELS = {"RUB": "руб."}


def _utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _canonical(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    with localcontext() as context:
        context.prec = 80
        return format(value.normalize(), "f")


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return left * right


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return left + right


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return left - right


def _divide(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return left / right


def _tariffs(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        price = _decimal(row.get("price"))
        currency = row.get("currency")
        try:
            effective_from = _utc(str(row["effective_from"]))
        except (KeyError, TypeError, ValueError):
            continue
        if price is None or price < 0 or not isinstance(currency, str) or not currency:
            continue
        result.append({**row, "price_decimal": price, "effective_datetime": effective_from})
    return sorted(result, key=lambda item: item["effective_datetime"])


def _active_tariff(rows: list[dict[str, Any]], at: datetime) -> dict[str, Any] | None:
    active = [row for row in rows if row["effective_datetime"] <= at]
    return active[-1] if active else None


def _unknown_cost(
    *,
    timezone: str,
    limitation: str,
    coverage_pct: float = 0.0,
    amounts: list[dict[str, str]] | None = None,
    priced_volume_m3: float | None = None,
    unpriced_volume_m3: float | None = None,
    slices: list[dict[str, Any]] | None = None,
    status: str = "unknown",
) -> dict[str, Any]:
    values = amounts or []
    return {
        "status": status,
        "amounts": values,
        "coverage_pct": coverage_pct,
        "priced_volume_m3": priced_volume_m3,
        "unpriced_volume_m3": unpriced_volume_m3,
        "currency_status": "mixed" if len(values) > 1 else "single" if values else "none",
        "basis": "calendar_month_tariffs",
        "timezone": timezone,
        "slices": slices or [],
        "limitations": [limitation],
    }


def calculate_gas_cost(
    start: datetime,
    end: datetime,
    volume_m3: Any,
    tariffs: Iterable[Mapping[str, Any]],
    *,
    timezone: str,
    volume_slices: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Price a period without fabricating a volume split at tariff boundaries.

    ``volume_slices`` is required only when tariff coverage or price/currency
    changes inside the period. Its volumes must come from an existing gas
    estimate or measurement rather than a duration-based allocation.
    """
    left, right = _utc(start), _utc(end)
    if right <= left:
        raise ValueError("cost period end must be after start")
    zone = ZoneInfo(timezone)  # validate the configured object timezone
    total_volume = _decimal(volume_m3)
    if total_volume is None or total_volume < 0:
        return _unknown_cost(timezone=timezone, limitation="gas_volume_unavailable")
    history = _tariffs(tariffs)
    if not history:
        return _unknown_cost(
            timezone=timezone,
            limitation="tariff_unavailable",
            unpriced_volume_m3=float(total_volume),
        )

    boundaries = [row["effective_datetime"] for row in history if left < row["effective_datetime"] < right]
    active_at_start = _active_tariff(history, left)
    needs_split = active_at_start is None or bool(boundaries)
    if needs_split and volume_slices is None:
        return _unknown_cost(
            timezone=timezone,
            limitation="volume_distribution_unavailable",
            unpriced_volume_m3=float(total_volume),
        )

    raw_slices = list(volume_slices or ({"start": left, "end": right, "volume_m3": total_volume},))
    prepared: list[tuple[datetime, datetime, Mapping[str, Any]]] = []
    for raw in raw_slices:
        try:
            slice_start = _utc(raw["start"])
            slice_end = _utc(raw["end"])
        except (KeyError, TypeError, ValueError):
            return _unknown_cost(
                timezone=timezone, limitation="invalid_volume_slices", unpriced_volume_m3=float(total_volume),
            )
        if slice_start < left or slice_end > right or slice_end <= slice_start:
            return _unknown_cost(
                timezone=timezone, limitation="invalid_volume_slices", unpriced_volume_m3=float(total_volume),
            )
        prepared.append((slice_start, slice_end, raw))
    prepared.sort(key=lambda item: item[0])
    cursor = left
    for slice_start, slice_end, _raw in prepared:
        if slice_start != cursor:
            return _unknown_cost(
                timezone=timezone, limitation="invalid_volume_slices", unpriced_volume_m3=float(total_volume),
            )
        if slice_start != left:
            local = slice_start.astimezone(zone)
            if local.day != 1 or any((local.hour, local.minute, local.second, local.microsecond)):
                return _unknown_cost(
                    timezone=timezone,
                    limitation="volume_slices_must_follow_calendar_months",
                    unpriced_volume_m3=float(total_volume),
                )
        cursor = slice_end
    if cursor != right:
        return _unknown_cost(
            timezone=timezone, limitation="invalid_volume_slices", unpriced_volume_m3=float(total_volume),
        )
    known_slice_volumes = [_decimal(raw.get("volume_m3")) for _, _, raw in prepared]
    if all(value is not None and value >= 0 for value in known_slice_volumes):
        sliced_total = sum((value for value in known_slice_volumes if value is not None), Decimal(0))
        tolerance = max(Decimal("0.000000001"), abs(total_volume) * Decimal("0.000000001"))
        if abs(sliced_total - total_volume) > tolerance:
            return _unknown_cost(
                timezone=timezone,
                limitation="volume_slices_do_not_match_total",
                unpriced_volume_m3=float(total_volume),
            )
    priced_volume = Decimal(0)
    unpriced_volume = Decimal(0)
    unknown_volume = False
    covered_seconds = 0.0
    amounts: dict[str, Decimal] = {}
    calculated: list[dict[str, Any]] = []
    limitations: list[str] = []
    for slice_start, slice_end, raw in prepared:
        volume = _decimal(raw.get("volume_m3"))
        tariff = _active_tariff(history, slice_start)
        if any(slice_start < boundary < slice_end for boundary in boundaries):
            return _unknown_cost(
                timezone=timezone,
                limitation="volume_slice_crosses_tariff_boundary",
                unpriced_volume_m3=float(total_volume),
            )
        item: dict[str, Any] = {
            "start": slice_start.isoformat(),
            "end": slice_end.isoformat(),
            "volume_m3": float(volume) if volume is not None else None,
            "allocation": str(raw.get("allocation", "existing_gas_volume")),
        }
        if tariff is None:
            item.update(tariff_id=None, price_per_m3=None, currency=None, amount=None)
            if volume is None:
                unknown_volume = True
            else:
                unpriced_volume = _add(unpriced_volume, volume)
            limitations.append("tariff_unavailable_for_part_of_period")
        elif volume is None or volume < 0:
            item.update(
                tariff_id=str(tariff.get("id", "")),
                price_per_m3=_canonical(tariff["price_decimal"]),
                currency=str(tariff["currency"]),
                amount=None,
            )
            unknown_volume = True
            limitations.append("gas_volume_unavailable_for_tariff_month")
        else:
            amount = _multiply(volume, tariff["price_decimal"])
            currency = str(tariff["currency"])
            amounts[currency] = _add(amounts.get(currency, Decimal(0)), amount)
            priced_volume = _add(priced_volume, volume)
            covered_seconds += (slice_end - slice_start).total_seconds()
            item.update(
                tariff_id=str(tariff.get("id", "")),
                price_per_m3=_canonical(tariff["price_decimal"]),
                currency=currency,
                amount=_canonical(amount),
            )
        calculated.append(item)

    duration = (right - left).total_seconds()
    coverage = min(100.0, max(0.0, covered_seconds / duration * 100))
    values = [{"currency": currency, "amount": _canonical(amount)} for currency, amount in sorted(amounts.items())]
    fully_covered = not limitations and not unknown_volume and coverage >= 99.999999
    status = "available" if fully_covered else "partial" if values else "unknown"
    if not fully_covered and not limitations:
        limitations.append("tariff_coverage_incomplete")
    return {
        "status": status,
        "amounts": values,
        "coverage_pct": coverage,
        "priced_volume_m3": float(priced_volume),
        "unpriced_volume_m3": None if unknown_volume else float(unpriced_volume),
        "currency_status": "mixed" if len(values) > 1 else "single" if values else "none",
        "basis": "calendar_month_tariffs",
        "timezone": timezone,
        "slices": calculated,
        "limitations": list(dict.fromkeys(limitations)),
    }


def value_volume_by_period_tariffs(volume_m3: Any, evaluated_cost: Mapping[str, Any]) -> dict[str, Any]:
    """Value a signed gas effect with the evaluated period's observed tariff weights."""
    volume = _decimal(volume_m3)
    timezone = str(evaluated_cost.get("timezone", "UTC"))
    if volume is None or evaluated_cost.get("status") != "available":
        return _unknown_cost(timezone=timezone, limitation="evaluated_period_cost_unavailable")
    slices = [item for item in evaluated_cost.get("slices", ()) if isinstance(item, Mapping)]
    weighted_volume = Decimal(0)
    for item in slices:
        weighted_volume = _add(weighted_volume, _decimal(item.get("volume_m3")) or Decimal(0))
    if weighted_volume <= 0:
        return _unknown_cost(timezone=timezone, limitation="evaluated_period_tariff_weights_unavailable")
    amounts: dict[str, Decimal] = {}
    valued_slices: list[dict[str, Any]] = []
    for item in slices:
        source_volume = _decimal(item.get("volume_m3"))
        price = _decimal(item.get("price_per_m3"))
        currency = item.get("currency")
        if source_volume is None or price is None or not isinstance(currency, str):
            return _unknown_cost(timezone=timezone, limitation="evaluated_period_tariff_weights_unavailable")
        allocated = _divide(_multiply(volume, source_volume), weighted_volume)
        amount = _multiply(allocated, price)
        amounts[currency] = _add(amounts.get(currency, Decimal(0)), amount)
        valued_slices.append({
            "start": item.get("start"), "end": item.get("end"),
            "volume_m3": float(allocated), "allocation": "evaluated_period_volume_weight",
            "tariff_id": item.get("tariff_id"), "price_per_m3": _canonical(price),
            "currency": currency, "amount": _canonical(amount),
        })
    values = [{"currency": currency, "amount": _canonical(amount)} for currency, amount in sorted(amounts.items())]
    return {
        "status": "available",
        "amounts": values,
        "coverage_pct": 100.0,
        "priced_volume_m3": float(volume),
        "unpriced_volume_m3": 0.0,
        "currency_status": "mixed" if len(values) > 1 else "single",
        "basis": "evaluated_period_tariff_weights",
        "timezone": timezone,
        "slices": valued_slices,
        "limitations": [],
    }


def subtract_costs(after: Mapping[str, Any], before: Mapping[str, Any]) -> dict[str, Any]:
    """Return an actual cost change only for one identical comparable currency."""
    timezone = str(after.get("timezone", before.get("timezone", "UTC")))
    left = before.get("amounts")
    right = after.get("amounts")
    if before.get("status") != "available" or after.get("status") != "available":
        return _unknown_cost(timezone=timezone, limitation="actual_cost_incomplete")
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != 1 or len(right) != 1:
        return _unknown_cost(timezone=timezone, limitation="currencies_not_comparable")
    if left[0].get("currency") != right[0].get("currency"):
        return _unknown_cost(timezone=timezone, limitation="currencies_not_comparable")
    before_amount, after_amount = _decimal(left[0].get("amount")), _decimal(right[0].get("amount"))
    if before_amount is None or after_amount is None:
        return _unknown_cost(timezone=timezone, limitation="actual_cost_incomplete")
    currency = str(right[0]["currency"])
    return {
        "status": "available",
        "amounts": [{"currency": currency, "amount": _canonical(_subtract(after_amount, before_amount))}],
        "coverage_pct": 100.0,
        "priced_volume_m3": None,
        "unpriced_volume_m3": None,
        "currency_status": "single",
        "basis": "actual_period_cost_difference",
        "timezone": timezone,
        "slices": [],
        "limitations": [],
    }


def scale_cost(cost: Mapping[str, Any], factor: Any, *, basis: str) -> dict[str, Any]:
    """Scale monetary amounts without reinterpreting tariff or volume evidence."""
    multiplier = _decimal(factor)
    timezone = str(cost.get("timezone", "UTC"))
    if multiplier is None or cost.get("status") != "available":
        return _unknown_cost(timezone=timezone, limitation="source_cost_unavailable")
    raw = cost.get("amounts")
    if not isinstance(raw, list):
        return _unknown_cost(timezone=timezone, limitation="source_cost_unavailable")
    values: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("currency"), str):
            return _unknown_cost(timezone=timezone, limitation="source_cost_unavailable")
        amount = _decimal(item.get("amount"))
        if amount is None:
            return _unknown_cost(timezone=timezone, limitation="source_cost_unavailable")
        values.append({"currency": str(item["currency"]), "amount": _canonical(_multiply(amount, multiplier))})
    priced = _decimal(cost.get("priced_volume_m3"))
    return {
        "status": "available",
        "amounts": values,
        "coverage_pct": 100.0,
        "priced_volume_m3": float(_multiply(priced, multiplier)) if priced is not None else None,
        "unpriced_volume_m3": 0.0,
        "currency_status": "mixed" if len(values) > 1 else "single" if values else "none",
        "basis": basis,
        "timezone": timezone,
        "slices": [],
        "limitations": [],
    }


def format_cost(cost: Mapping[str, Any] | None) -> str:
    """Format a canonical cost safely for all text and HTML renderers."""
    if not isinstance(cost, Mapping) or cost.get("status") not in {"available", "partial"}:
        return "Стоимость неизвестна"
    raw = cost.get("amounts")
    if not isinstance(raw, list):
        return "Стоимость неизвестна"
    formatted: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("currency"), str):
            continue
        amount = _decimal(item.get("amount"))
        if amount is None:
            continue
        try:
            with localcontext() as context:
                context.prec = 80
                rounded = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            continue
        number = f"{rounded:,.2f}"
        number = number.replace(",", " ").replace(".", ",")
        currency = str(item["currency"])
        formatted.append(f"{number} {_CURRENCY_LABELS.get(currency, currency)}")
    if not formatted:
        return "Стоимость неизвестна"
    result = " + ".join(formatted)
    return f"{result} (частично)" if cost.get("status") == "partial" else result


# Concise public alias for callers that already operate in gas-cost context.
calculate_cost = calculate_gas_cost


__all__ = [
    "calculate_cost",
    "calculate_gas_cost",
    "format_cost",
    "scale_cost",
    "subtract_costs",
    "value_volume_by_period_tariffs",
]

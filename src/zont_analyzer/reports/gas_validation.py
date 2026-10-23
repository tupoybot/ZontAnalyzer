"""Shared wording for empirical checks of whole meter intervals."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

VIEW_VERSION = "gas-validation-v1"


def _nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _count(value: Any) -> int | None:
    number = _nonnegative(value)
    return int(number) if number is not None and number.is_integer() else None


def _number(value: float, places: int = 1) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".").replace(".", ",")


def validation_summary(gas: Mapping[str, Any]) -> str:
    model = gas.get("model")
    if isinstance(model, Mapping):
        error = _nonnegative(model.get("validation_relative_error"))
        count = _count(model.get("heldout_interval_count"))
        if error is not None and math.isfinite(error * 100) and count is not None and count > 0:
            return f"ошибка проверки {_number(error * 100)}% · интервалов: {count}"
        absolute = _nonnegative(model.get("validation_error_m3"))
        if error is None and absolute is not None and count is not None and count > 0:
            return f"ошибка проверки {_number(absolute, 2)} м³ за интервал · интервалов: {count}"
    return "Проверка модели: недостаточно данных"


VALIDATION_NOTE = (
    "Проверка выполнена на целых интервалах между показаниями счётчика, "
    "которые заранее исключили из обучения. Ошибка — сумма абсолютных отклонений "
    "расчёта от показаний, делённая на суммарный измеренный расход. "
    "Это не уверенность и не вероятность; проверка не гарантирует точность "
    "распределения расхода по отдельным дням."
)


def validation_lines(gas: Mapping[str, Any]) -> list[str]:
    summary = validation_summary(gas)
    lines = [summary if summary.startswith("Проверка") else "Проверка модели: " + summary]
    model = gas.get("model")
    if isinstance(model, Mapping):
        if not summary.startswith("Проверка"):
            if _nonnegative(model.get("validation_relative_error")) is None:
                lines.append("Относительная ошибка проверки не определена; показана ошибка в м³.")
            absolute = _nonnegative(model.get("validation_error_m3"))
            if absolute is not None:
                lines.append(f"Средняя абсолютная ошибка за проверочный интервал: {_number(absolute, 2)} м³")
            lines.append(VALIDATION_NOTE)
        usable = _count(model.get("usable_interval_count"))
        if usable is not None:
            lines.append(f"Пригодных интервалов для калибровки: {usable}")
    boundary = _nonnegative(model.get("boundary_relative_uncertainty")) if isinstance(model, Mapping) else None
    intervals = gas.get("measured_intervals")
    day_precision = gas.get("measurement_time_precision") == "day" or (
        isinstance(intervals, list) and any(
            isinstance(item, Mapping) and item.get("time_precision") == "day" for item in intervals
        )
    )
    if day_precision or boundary is not None and boundary > 0:
        lines.append(
            "Время снятия показаний внутри дня неизвестно: это отдельное ограничение "
            "сопоставления счётчика с телеметрией и распределения расхода по дням; "
            "оно не является ошибкой проверки модели."
        )
    if gas.get("scope") == "shared_meter" or (
        isinstance(model, Mapping) and model.get("has_gas_stove") is not False
        and "has_gas_stove" in model
    ):
        lines.append("Проверка по общему счётчику не отделяет расход котла от других газовых потребителей.")
    return lines

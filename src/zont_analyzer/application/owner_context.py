"""Owner profile and gas meter rules backed by the YDB owner repository."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def _when(value: datetime | str | int | None) -> datetime:
    if value is None:
        return utcnow()
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1_000_000, UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("effective_from must be an ISO date or datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError("effective_from must be an ISO date or datetime")
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _as_utc(value: datetime | str | None) -> datetime:
    return _when(value)


def _time(value: datetime | str | int) -> str:
    return _when(value).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_PROFILE_FIELDS = frozenset(
    {
        "auto_adapt",
        "auto_adapt_node",
        "auto_adapt_pump_model",
        "dhw_type",
        "hydraulic_separator",
        "nominal_power_kw",
        "gas_min_m3h",
        "gas_max_m3h",
        "has_gas_stove",
        "gas_unit",
        "gas_source",
        "gas_applicability",
        "gas_type",
        "boiler_model",
        "coordinates",
        "installation_notes",
        "season_boundaries",
    }
)
_TRISTATE_FIELDS = frozenset({"auto_adapt", "hydraulic_separator", "has_gas_stove"})
_NUMBER_FIELDS = frozenset({"nominal_power_kw", "gas_min_m3h", "gas_max_m3h"})
_TEXT_FIELDS = frozenset(
    {
        "auto_adapt_node",
        "auto_adapt_pump_model",
        "gas_unit",
        "gas_source",
        "gas_applicability",
        "gas_type",
        "boiler_model",
        "installation_notes",
    }
)
_MAX_TEXT = 500
_MAX_GAS_DIGITS = 18
_MAX_GAS_DECIMALS = 6


def _decimal(value: Any) -> Decimal:
    # Decimal('1e999999999') is cheap to parse but ``format(..., 'f')`` could
    # allocate an enormous string. Bound input and exponent before formatting.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("gas reading must be a finite non-negative decimal")
    text = str(value).strip()
    if isinstance(value, str):
        if "," in text and "." in text:
            raise ValueError("gas reading must use one decimal separator")
        text = text.replace(",", ".")
    if not text or len(text) > 64:
        raise ValueError("gas reading has an unsupported precision or magnitude")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("gas reading must be a finite non-negative decimal") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("gas reading must be a finite non-negative decimal")
    _sign, digits, exponent = result.as_tuple()
    numeric_exponent = int(exponent)
    if len(digits) > _MAX_GAS_DIGITS or numeric_exponent < -_MAX_GAS_DECIMALS or result.adjusted() >= _MAX_GAS_DIGITS:
        raise ValueError("gas reading has an unsupported precision or magnitude")
    return result


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("value must be a finite number")
    text = str(value).strip()
    if isinstance(value, str):
        if "," in text and "." in text:
            raise ValueError("value must use one decimal separator")
        text = text.replace(",", ".")
    if not text or len(text) > 64:
        raise ValueError("value must be a finite number")
    try:
        decimal = Decimal(text)
        number = float(decimal)
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("value must be a finite number") from exc
    if not decimal.is_finite() or not math.isfinite(number):
        raise ValueError("value must be a finite number")
    return number


def _decimal_text(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value.normalize(), "f")


class OwnerContextStore:
    """Validated public Store API; persistence is in the owner repository."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _validate_text(field: str, value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
            raise ValueError(f"{field} must be a non-empty string no longer than {_MAX_TEXT} characters")
        return value.strip()

    @staticmethod
    def _validate_value(field: str, value: Any) -> Any:
        # ``null`` is the owner's explicit "unknown". It remains a manual
        # revision; reset is the separate operation that resumes auto values.
        if value is None:
            return None
        if field == "season_boundaries":
            from zont_analyzer.domain.periods import SeasonBoundaries

            return SeasonBoundaries.model_validate(value).model_dump()
        if field in _TRISTATE_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{field} must be true, false, or null")
            return value
        if field in _NUMBER_FIELDS:
            try:
                number = _finite_number(value)
            except ValueError as exc:
                raise ValueError(f"{field} must be a positive finite number") from exc
            if number <= 0:
                raise ValueError(f"{field} must be a positive finite number")
            return number
        if field == "coordinates":
            if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
                raise ValueError("coordinates must be an object with latitude and longitude")
            try:
                latitude = _finite_number(value["latitude"])
                longitude = _finite_number(value["longitude"])
            except ValueError as exc:
                raise ValueError("coordinates latitude and longitude must be finite numbers") from exc

            if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                raise ValueError("coordinates are outside geographic bounds")
            return {"latitude": latitude, "longitude": longitude}
        if field == "dhw_type":
            if value not in (None, "tank", "combi", "none"):
                raise ValueError("dhw_type must be tank, combi, none, or null")
            return value
        if field == "gas_unit":
            if value not in {"м³/ч", "m3/h"}:
                raise ValueError("gas_unit must be m³/ч or m3/h")
            return "м³/ч"
        if field in _TEXT_FIELDS:
            return OwnerContextStore._validate_text(field, value)
        raise ValueError(f"unknown profile field: {field}")

    @staticmethod
    def _manual_fields(payload: dict[str, Any]) -> tuple[dict[str, tuple[bool, Any]], datetime]:
        if not isinstance(payload, dict) or set(payload) - {"fields", "effective_from"}:
            raise ValueError("profile payload accepts only fields and optional effective_from")
        fields = payload.get("fields")
        if not isinstance(fields, dict):
            raise ValueError("profile fields must be an object")
        effective = _as_utc(payload.get("effective_from"))
        result: dict[str, tuple[bool, Any]] = {}
        for field, item in fields.items():
            if field not in _PROFILE_FIELDS:
                raise ValueError(f"unknown profile field: {field}")
            if not isinstance(item, dict):
                raise ValueError(f"{field} must be an object with value or reset")
            if set(item) == {"reset"}:
                if item["reset"] is not True:
                    raise ValueError(f"{field}.reset must be true")
                result[field] = (True, None)
            elif set(item) == {"value"}:
                result[field] = (False, OwnerContextStore._validate_value(field, item["value"]))
            else:
                raise ValueError(f"{field} must contain exactly value or reset")
        return result, effective

    @staticmethod
    def _validate_gas_bounds(current: dict[str, dict[str, Any]], values: dict[str, tuple[bool, Any]]) -> None:
        lower = values.get("gas_min_m3h", (False, current.get("gas_min_m3h", {}).get("value")))[1]
        upper = values.get("gas_max_m3h", (False, current.get("gas_max_m3h", {}).get("value")))[1]
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("gas_min_m3h must not exceed gas_max_m3h")

    @staticmethod
    def _selected_gas_day(value: Any, report_day: str, timezone: ZoneInfo) -> str:
        """Validate an explicitly selected local calendar day.

        A browser timezone is intentionally not submitted as authority.  Permit
        one day past the UTC date so an owner on either side of the site's
        timezone can enter their already-current browser day, while still
        rejecting arbitrary future meter readings.
        """
        if value is None:
            return report_day
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Укажите дату показания в формате YYYY-MM-DD.")
        try:
            selected = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Укажите дату показания в формате YYYY-MM-DD.") from exc
        if selected.isoformat() != value:
            raise ValueError("Укажите дату показания в формате YYYY-MM-DD.")
        if selected > datetime.now(UTC).date() + timedelta(days=1):
            raise ValueError("Дата показания не может быть в будущем.")
        return value

    @staticmethod
    def _gas_payload(payload: dict[str, Any]) -> tuple[bool, bool, Decimal | None, Any, bool]:
        if not isinstance(payload, dict) or set(payload) - {"value_m3", "delete", "reset", "day", "reading_id"}:
            raise ValueError("gas payload accepts only value_m3, delete, reset, day, and reading_id")
        delete, reset = payload.get("delete", False), payload.get("reset", False)
        if not isinstance(delete, bool) or not isinstance(reset, bool):
            raise ValueError("delete and reset must be booleans")
        reading_id = payload.get("reading_id")
        if reading_id is not None and (not isinstance(reading_id, str) or not reading_id):
            raise ValueError("reading_id must be a non-empty string or null")
        has_value = "value_m3" in payload
        if delete:
            if has_value:
                raise ValueError("delete cannot include value_m3")
            return True, reset, None, reading_id, "reading_id" in payload
        if not has_value:
            raise ValueError("value_m3 is required unless delete is true")
        return False, reset, _decimal(payload["value_m3"]), reading_id, "reading_id" in payload

    @staticmethod
    def _profile_state(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        latest_auto: dict[str, dict[str, Any]] = {}
        manual_active: set[str] = set()
        for row in sorted(rows, key=lambda r: (_when(r["effective_at"]), _when(r["recorded_at"]), r["id"])):
            field = row["field"]
            if row.get("is_reset", False):
                manual_active.discard(field)
                if field in latest_auto:
                    current[field] = latest_auto[field]
                else:
                    current.pop(field, None)
                continue
            value = row.get("value", json.loads(row["value_json"]) if "value_json" in row else None)
            item = {
                "value": value,
                "source": row["source"],
                "effective_from": _time(row["effective_at"]),
                "provenance": row["provenance"],
            }
            if row["source"] == "auto":
                latest_auto[field] = item
                if field not in manual_active:
                    current[field] = item
            else:
                manual_active.add(field)
                current[field] = item
        return current

    def profile(self, device_id: str, as_of: datetime | str | None = None) -> dict[str, Any]:
        moment = _as_utc(as_of)
        rows = self.db.owner.application_profile_rows(device_id, moment)
        return {
            "device_id": device_id,
            "as_of": _time(moment),
            "fields": self._profile_state(rows),
            "history": [
                {
                    "id": row["id"],
                    "field": row["field"],
                    "value": row.get("value", json.loads(row["value_json"]) if "value_json" in row else None),
                    "source": row["source"],
                    "provenance": row["provenance"],
                    "effective_from": _time(row["effective_at"]),
                    "recorded_at": _time(row["recorded_at"]),
                    "reset": bool(row.get("is_reset", False)),
                }
                for row in rows
            ],
        }

    def update_profile(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        values, effective = self._manual_fields(payload)
        self.db.owner.application_profile_update(
            device_id,
            values,
            None if payload.get("effective_from") is None else effective,
        )
        return self.profile(device_id)

    def observe_auto(
        self, device_id: str, fields: dict[str, Any], observed_at: datetime | str | None = None
    ) -> dict[str, Any]:
        if not isinstance(fields, dict) or not fields:
            raise ValueError("automatic profile fields must be a non-empty object")
        candidates: dict[str, tuple[Any, str]] = {}
        for field, item in fields.items():
            if (
                field not in _PROFILE_FIELDS
                or not isinstance(item, dict)
                or set(item) - {"value", "source", "provenance"}
            ):
                raise ValueError(f"invalid automatic profile field: {field}")
            if "value" not in item:
                raise ValueError(f"automatic profile field {field} requires value")
            provenance = item.get("provenance", item.get("source"))
            if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > _MAX_TEXT:
                raise ValueError(f"automatic profile field {field} requires a source path")
            candidates[field] = (self._validate_value(field, item["value"]), provenance.strip())
        self.db.owner.application_profile_observe(device_id, candidates, _as_utc(observed_at))
        return self.profile(device_id)

    @staticmethod
    def _report_day(report: Any) -> tuple[str, str, ZoneInfo]:
        if report.kind != "daily":
            raise ValueError("gas readings belong to daily reports only")
        try:
            zone = ZoneInfo(str(report.timezone or "UTC"))
            day = _when(report.period_start).astimezone(zone).date().isoformat()
        except Exception as exc:
            raise ValueError("invalid report time or timezone") from exc
        return day, "installation", zone

    @staticmethod
    def _segment_for_day(boundaries: list[dict[str, Any]], day: str) -> str:
        segment = "default"
        for boundary in sorted(boundaries, key=lambda b: (b["boundary_day"], b["id"])):
            if boundary["boundary_day"] > day:
                break
            segment = boundary["id"]
        return segment

    @staticmethod
    def _reading(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "device_id": row["device_id"],
            "report_id": row["report_id"],
            "day": row["reading_day"],
            "meter_segment": row["meter_segment"],
            "value_m3": row["value_m3"],
            "entered_at": _time(row["entered_at"]),
            "updated_at": _time(row["updated_at"]),
        }

    @staticmethod
    def _audit(row: dict[str, Any]) -> dict[str, Any]:
        def snapshot(key: str) -> Any:
            value = row.get(key)
            return json.loads(value) if isinstance(value, str) else value

        return {
            "id": row["id"],
            "action": row["action"],
            "before": snapshot("before_json"),
            "after": snapshot("after_json"),
            "created_at": _time(row["created_at"]),
        }

    def gas(self, report_id: str, day: str | None = None) -> dict[str, Any]:
        report = self.db.reports.report(report_id)
        if report is None:
            raise KeyError(report_id)
        report_day, device_id, zone = self._report_day(report)
        selected_day = self._selected_gas_day(day, report_day, zone)
        readings, boundaries, audits = self.db.owner.application_gas_state(device_id)
        row = next((r for r in readings if r["reading_day"] == selected_day), None)
        visible = [
            item
            for item in audits
            if item["reading_day"] == selected_day
            or row is not None
            and item.get("reading_id") == row["id"]
            or any(
                snapshot and snapshot.get("day") == selected_day
                for snapshot in (self._audit(item)["before"], self._audit(item)["after"])
            )
        ]
        visible.sort(key=lambda item: (_when(item["created_at"]), item["id"]))
        completed = self.db.reports.completed_reports(utcnow())
        latest_start = max((r.period_start for r in completed if r.kind == "daily"), default=None)
        return {
            "report_id": report_id,
            "time_precision": "day",
            "report_day": report_day,
            "selected_day": selected_day,
            "is_latest_report": report.period_start == latest_start,
            "reading": self._reading(row) if row else None,
            "readings": [self._reading(r) for r in sorted(readings, key=lambda r: (r["reading_day"], r["id"]))],
            "audit": [self._audit(item) for item in visible],
            "plausibility": self._gas_plausibility(report, selected_day, row, readings, boundaries),
        }

    def update_gas(self, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        delete, reset, number, reading_id, reading_id_supplied = self._gas_payload(payload)
        report = self.db.reports.report(report_id)
        if report is None:
            raise KeyError(report_id)
        report_day, device_id, zone = self._report_day(report)
        day = self._selected_gas_day(payload.get("day"), report_day, zone)
        self.db.owner.application_gas_update(
            device_id,
            report_id,
            day,
            None if number is None else _decimal_text(number),
            delete=delete,
            reset=reset,
            reading_id=reading_id,
            reading_id_supplied=reading_id_supplied,
        )
        return self.gas(report_id, day)

    def gas_readings_for_analysis(self) -> list[dict[str, Any]]:
        readings, _boundaries, _audits = self.db.owner.application_gas_state("installation")
        return [
            {
                "id": row["id"],
                "day": row["reading_day"],
                "value_m3": row["value_m3"],
                "meter_segment": row["meter_segment"],
                "segment": row["meter_segment"],
                "updated_at": _when(row["updated_at"]),
            }
            for row in sorted(readings, key=lambda row: (row["reading_day"], row["id"]))
        ]

    def _gas_plausibility(
        self,
        report: Any,
        day: str,
        reading: dict[str, Any] | None,
        readings: list[dict[str, Any]],
        boundaries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        devices = self.db.owner.application_devices()
        if len(devices) != 1:
            return {
                "status": "unknown",
                "reason": "Не удалось однозначно выбрать профиль оборудования для проверки расхода.",
                "warnings": [],
            }
        profile = self.profile(devices[0])["fields"]
        values = {key: profile.get(key, {}).get("value") for key in ("gas_max_m3h", "has_gas_stove")}
        if values["gas_max_m3h"] is None:
            return {
                "status": "unknown",
                "reason": "Максимальный паспортный расход котла, м³/ч, ещё не указан.",
                "warnings": [],
            }
        if values["has_gas_stove"] is not False:
            return {
                "status": "preliminary",
                "reason": "Другие потребители газа не исключены; показание не сравнивается с расходом котла.",
                "warnings": [],
            }
        if reading is None:
            return {"status": "preliminary", "reason": "Нет показания за этот день.", "warnings": []}
        segment = self._segment_for_day(boundaries, day)
        neighbors = [
            r
            for r in readings
            if r["id"] != reading["id"] and self._segment_for_day(boundaries, r["reading_day"]) == segment
        ]
        current_day = date.fromisoformat(day)
        current_value = _decimal(reading["value_m3"])
        zone = ZoneInfo(str(report.timezone or "UTC"))
        compared = False
        for neighbor in neighbors:
            neighbor_day = date.fromisoformat(neighbor["reading_day"])
            if neighbor_day == current_day:
                continue
            first = datetime.combine(min(current_day, neighbor_day), time.min, zone).astimezone(UTC)
            after = datetime.combine(max(current_day, neighbor_day) + timedelta(days=1), time.min, zone).astimezone(UTC)
            if any(_when(profile[key]["effective_from"]) > first for key in values):
                continue
            compared = True
            hours = Decimal(str((after - first).total_seconds())) / Decimal(3600)
            maximum = Decimal(str(values["gas_max_m3h"])) * hours
            volume = abs(current_value - _decimal(neighbor["value_m3"]))
            if volume > maximum:
                return {
                    "status": "warning",
                    "reason": "Нужна проверка показаний или параметров профиля.",
                    "warnings": [
                        "Разность показаний превышает паспортный max даже при максимальной "
                        "неопределённости границы дней."
                    ],
                }
        return {
            "status": "preliminary",
            "reason": "Предварительная проверка по паспортному максимуму; другие потребители могут влиять на итог."
            if compared
            else "Нет соседнего показания с известными параметрами на всём интервале.",
            "warnings": [],
        }

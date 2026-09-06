"""Auditable local storage for owner equipment context and gas meter readings.

This module never writes to ZONT. Automatic discoveries are recorded as
``source=auto`` with their read-only API path in ``provenance``; owner edits
are separate manual revisions. SQLite writes start with ``BEGIN IMMEDIATE`` so
concurrent requests cannot validate stale meter state and both commit it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from zont_analyzer.adapters.sqlite.database import Base, Database, DeviceRow, ReportRow, utcnow


class OwnerProfileRevisionRow(Base):
    __tablename__ = "owner_profile_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    field: Mapped[str] = mapped_column(String, index=True)
    value_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String)
    provenance: Mapped[str] = mapped_column(String)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_reset: Mapped[bool] = mapped_column(Boolean, default=False)


class GasReadingRow(Base):
    __tablename__ = "gas_readings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    report_id: Mapped[str] = mapped_column(String, index=True)
    reading_day: Mapped[str] = mapped_column(String)
    meter_segment: Mapped[str] = mapped_column(String, default="default")
    value_m3: Mapped[str] = mapped_column(String)
    entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("device_id", "reading_day"),)


class GasMeterBoundaryRow(Base):
    """An explicit reset/replacement boundary independent of a reading row."""

    __tablename__ = "gas_meter_boundaries"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    report_id: Mapped[str] = mapped_column(String)
    boundary_day: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("device_id", "boundary_day"),)


class GasReadingAuditRow(Base):
    __tablename__ = "gas_reading_audit"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    reading_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    device_id: Mapped[str] = mapped_column(String, index=True)
    report_id: Mapped[str] = mapped_column(String)
    reading_day: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return utcnow()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("effective_from must be an ISO date or datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError("effective_from must be an ISO date or datetime")
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _time(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _decimal(value: Any) -> Decimal:
    # Decimal('1e999999999') is cheap to parse but ``format(..., 'f')`` could
    # allocate an enormous string. Bound input and exponent before formatting.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("gas reading must be a finite non-negative decimal")
    text = str(value).strip()
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


def _decimal_text(value: Decimal) -> str:
    return "0" if value.is_zero() else format(value.normalize(), "f")


class OwnerContextStore:
    """Storage API for the authenticated owner-context HTTP adapter."""

    def __init__(self, db: Database):
        self.db = db

    @contextmanager
    def _write_session(self) -> Iterator[Session]:
        """Acquire SQLite's writer lock before inspecting and modifying state."""
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
    def _ordered_profile_rows(session: Session, device_id: str, moment: datetime) -> list[OwnerProfileRevisionRow]:
        return list(
            session.scalars(
                select(OwnerProfileRevisionRow)
                .where(OwnerProfileRevisionRow.device_id == device_id, OwnerProfileRevisionRow.effective_from <= moment)
                .order_by(
                    OwnerProfileRevisionRow.effective_from,
                    OwnerProfileRevisionRow.recorded_at,
                    OwnerProfileRevisionRow.id,
                )
            ).all()
        )

    @staticmethod
    def _profile_state(rows: list[OwnerProfileRevisionRow]) -> dict[str, dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        latest_auto: dict[str, dict[str, Any]] = {}
        manual_active: set[str] = set()
        for row in rows:
            if row.is_reset:
                manual_active.discard(row.field)
                if row.field in latest_auto:
                    current[row.field] = latest_auto[row.field]
                else:
                    current.pop(row.field, None)
                continue
            item = {
                "value": json.loads(row.value_json),
                "source": row.source,
                "effective_from": _time(row.effective_from),
                "provenance": row.provenance,
            }
            if row.source == "auto":
                latest_auto[row.field] = item
                if row.field not in manual_active:
                    current[row.field] = item
            else:
                manual_active.add(row.field)
                current[row.field] = item
        return current

    @staticmethod
    def _history(rows: list[OwnerProfileRevisionRow]) -> list[dict[str, Any]]:
        return [
            {
                "id": row.id,
                "field": row.field,
                "value": json.loads(row.value_json),
                "source": row.source,
                "provenance": row.provenance,
                "effective_from": _time(row.effective_from),
                "recorded_at": _time(row.recorded_at),
                "reset": row.is_reset,
            }
            for row in rows
        ]

    def profile(self, device_id: str, as_of: datetime | str | None = None) -> dict[str, Any]:
        moment = _as_utc(as_of)
        with self.db.session() as session:
            if session.get(DeviceRow, device_id) is None:
                raise KeyError(device_id)
            rows = self._ordered_profile_rows(session, device_id, moment)
            return {
                "device_id": device_id,
                "as_of": _time(moment),
                "fields": self._profile_state(rows),
                "history": self._history(rows),
            }

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
        if field in _TRISTATE_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{field} must be true, false, or null")
            return value
        if field in _NUMBER_FIELDS:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{field} must be a positive finite number")
            return value
        if field == "coordinates":
            if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
                raise ValueError("coordinates must be an object with latitude and longitude")
            latitude, longitude = value["latitude"], value["longitude"]
            if any(
                isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item))
                for item in (latitude, longitude)
            ):
                raise ValueError("coordinates latitude and longitude must be finite numbers")
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

    def update_profile(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        values, effective = self._manual_fields(payload)
        with self._write_session() as session:
            if session.get(DeviceRow, device_id) is None:
                raise KeyError(device_id)
            current = self._profile_state(self._ordered_profile_rows(session, device_id, effective))
            self._validate_gas_bounds(current, values)
            recorded_at = utcnow()
            for field, (reset, value) in values.items():
                current_field = current.get(field)
                if reset and (current_field is None or current_field["source"] != "manual"):
                    continue
                if (
                    not reset
                    and current_field
                    and current_field["source"] == "manual"
                    and current_field["value"] == value
                ):
                    continue
                session.add(
                    OwnerProfileRevisionRow(
                        id=str(uuid4()),
                        device_id=device_id,
                        field=field,
                        value_json=_json(value),
                        source="manual",
                        provenance="owner",
                        effective_from=effective,
                        recorded_at=recorded_at,
                        is_reset=reset,
                    )
                )
        return self.profile(device_id)

    def observe_auto(
        self, device_id: str, fields: dict[str, Any], observed_at: datetime | str | None = None
    ) -> dict[str, Any]:
        """Persist verified discovery once per value/path; never bypass manual priority."""
        if not isinstance(fields, dict) or not fields:
            raise ValueError("automatic profile fields must be a non-empty object")
        effective = _as_utc(observed_at)
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
        with self._write_session() as session:
            if session.get(DeviceRow, device_id) is None:
                raise KeyError(device_id)
            for field, (value, provenance) in candidates.items():
                previous = session.scalar(
                    select(OwnerProfileRevisionRow)
                    .where(
                        OwnerProfileRevisionRow.device_id == device_id,
                        OwnerProfileRevisionRow.field == field,
                        OwnerProfileRevisionRow.source == "auto",
                        OwnerProfileRevisionRow.is_reset.is_(False),
                    )
                    .order_by(OwnerProfileRevisionRow.recorded_at.desc(), OwnerProfileRevisionRow.id.desc())
                )
                if previous and json.loads(previous.value_json) == value and previous.provenance == provenance:
                    continue
                session.add(
                    OwnerProfileRevisionRow(
                        id=str(uuid4()),
                        device_id=device_id,
                        field=field,
                        value_json=_json(value),
                        source="auto",
                        provenance=provenance,
                        effective_from=effective,
                        recorded_at=utcnow(),
                        is_reset=False,
                    )
                )
        return self.profile(device_id)

    @staticmethod
    def _report_day(session: Session, report_id: str) -> tuple[ReportRow, str, str]:
        row = session.get(ReportRow, report_id)
        if row is None:
            raise KeyError(report_id)
        if row.kind != "daily":
            raise ValueError("gas readings belong to daily reports only")
        try:
            report = json.loads(row.canonical_json)
            timezone = str(report.get("timezone") or "UTC")
            context = report.get("context") if isinstance(report.get("context"), dict) else {}
            # Gas readings describe the installation meter. A report may name
            # a selected analysis device, which must not split this meter.
            del context
            device_id = "installation"
            day = datetime.fromtimestamp(row.period_start, UTC).astimezone(ZoneInfo(timezone)).date().isoformat()
        except Exception as exc:
            raise ValueError("invalid report time or timezone") from exc
        return row, day, device_id

    @staticmethod
    def _boundaries(session: Session, device_id: str) -> list[GasMeterBoundaryRow]:
        return list(
            session.scalars(
                select(GasMeterBoundaryRow)
                .where(GasMeterBoundaryRow.device_id == device_id)
                .order_by(GasMeterBoundaryRow.boundary_day, GasMeterBoundaryRow.id)
            ).all()
        )

    @classmethod
    def _segment_for_day(cls, session: Session, device_id: str, day: str) -> str:
        segment = "default"
        for boundary in cls._boundaries(session, device_id):
            if boundary.boundary_day > day:
                break
            segment = boundary.id
        return segment

    @classmethod
    def _refresh_segments(cls, session: Session, device_id: str) -> None:
        for reading in session.scalars(select(GasReadingRow).where(GasReadingRow.device_id == device_id)).all():
            reading.meter_segment = cls._segment_for_day(session, device_id, reading.reading_day)

    def gas(self, report_id: str) -> dict[str, Any]:
        with self.db.session() as session:
            report, day, device_id = self._report_day(session, report_id)
            row = session.scalar(
                select(GasReadingRow).where(
                    GasReadingRow.device_id == device_id,
                    GasReadingRow.reading_day == day,
                )
            )
            audits = session.scalars(
                select(GasReadingAuditRow)
                .where(
                    GasReadingAuditRow.device_id == device_id,
                    GasReadingAuditRow.reading_day == day,
                )
                .order_by(GasReadingAuditRow.created_at, GasReadingAuditRow.id)
            ).all()
            return {
                "report_id": report_id,
                "time_precision": "day",
                "reading": self._reading(row) if row else None,
                "audit": [self._audit(item) for item in audits],
                "plausibility": self._gas_plausibility(session, report, day, row),
            }

    def _gas_plausibility(
        self, session: Session, report: ReportRow, day: str, reading: GasReadingRow | None
    ) -> dict[str, Any]:
        devices = list(session.scalars(select(DeviceRow).order_by(DeviceRow.id)).all())
        if len(devices) != 1:
            return {
                "status": "unknown",
                "reason": "Не удалось однозначно выбрать профиль оборудования для проверки расхода.",
                "warnings": [],
            }
        as_of = datetime.fromtimestamp(report.period_start, UTC)
        profile = self._profile_state(self._ordered_profile_rows(session, devices[0].id, as_of))
        values = {
            key: profile.get(key, {}).get("value")
            for key in (
                "gas_max_m3h",
                "gas_unit",
                "gas_source",
                "gas_applicability",
                "has_gas_stove",
            )
        }
        if any(values[key] is None for key in ("gas_max_m3h", "gas_unit", "gas_source", "gas_applicability")):
            return {
                "status": "unknown",
                "reason": "Паспортные max, единица, источник или применимость ещё не указаны.",
                "warnings": [],
            }
        if values["gas_unit"] != "м³/ч":
            return {
                "status": "unknown",
                "reason": "Паспортная единица не позволяет сопоставить расход с м³/ч.",
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
        segment = self._segment_for_day(session, "installation", day)
        neighbors = [
            candidate
            for candidate in session.scalars(
                select(GasReadingRow).where(GasReadingRow.device_id == "installation")
            ).all()
            if candidate.id != reading.id
            and self._segment_for_day(session, "installation", candidate.reading_day) == segment
        ]
        warnings: list[str] = []
        current_day = date.fromisoformat(day)
        current_value = _decimal(reading.value_m3)
        timezone = ZoneInfo(json.loads(report.canonical_json).get("timezone") or "UTC")
        compared = False
        for neighbor in neighbors:
            neighbor_day = date.fromisoformat(neighbor.reading_day)
            if neighbor_day == current_day:
                continue
            first = datetime.combine(min(current_day, neighbor_day), time.min, timezone).astimezone(UTC)
            after = datetime.combine(max(current_day, neighbor_day) + timedelta(days=1), time.min, timezone)
            if any(_as_utc(profile[key]["effective_from"]) > first for key in values):
                continue
            compared = True
            hours = Decimal(str((after.astimezone(UTC) - first).total_seconds())) / Decimal(3600)
            maximum = Decimal(str(values["gas_max_m3h"])) * hours
            volume = abs(current_value - _decimal(neighbor.value_m3))
            if volume > maximum:
                warnings.append(
                    "Разность показаний превышает паспортный max даже при максимальной неопределённости границы дней."
                )
                break
        if warnings:
            return {
                "status": "warning",
                "reason": "Нужна проверка показаний или параметров профиля.",
                "warnings": warnings,
            }
        return {
            "status": "preliminary",
            "reason": (
                "Предварительная проверка по паспортному максимуму; другие потребители могут влиять на итог."
                if compared else "Нет соседнего показания с известными параметрами на всём интервале."
            ),
            "warnings": [],
        }

    @staticmethod
    def _reading(row: GasReadingRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "device_id": row.device_id,
            "report_id": row.report_id,
            "day": row.reading_day,
            "meter_segment": row.meter_segment,
            "value_m3": row.value_m3,
            "entered_at": _time(row.entered_at),
            "updated_at": _time(row.updated_at),
        }

    @staticmethod
    def _audit(row: GasReadingAuditRow) -> dict[str, Any]:
        return {
            "id": row.id,
            "action": row.action,
            "before": json.loads(row.before_json) if row.before_json else None,
            "after": json.loads(row.after_json) if row.after_json else None,
            "created_at": _time(row.created_at),
        }

    @staticmethod
    def _gas_payload(payload: dict[str, Any]) -> tuple[bool, bool, Decimal | None]:
        if not isinstance(payload, dict) or set(payload) - {"value_m3", "delete", "reset"}:
            raise ValueError("gas payload accepts only value_m3, delete, and reset")
        delete, reset = payload.get("delete", False), payload.get("reset", False)
        if not isinstance(delete, bool) or not isinstance(reset, bool):
            raise ValueError("delete and reset must be booleans")
        has_value = "value_m3" in payload
        if delete:
            if has_value:
                raise ValueError("delete cannot include value_m3")
            return True, reset, None
        if not has_value:
            raise ValueError("value_m3 is required unless delete is true")
        return False, reset, _decimal(payload["value_m3"])

    def _validate_monotonic(
        self, session: Session, device_id: str, day: str, number: Decimal, reading_id: str | None
    ) -> None:
        segment = self._segment_for_day(session, device_id, day)
        for candidate in session.scalars(select(GasReadingRow).where(GasReadingRow.device_id == device_id)).all():
            if (
                candidate.id == reading_id
                or self._segment_for_day(session, device_id, candidate.reading_day) != segment
            ):
                continue
            other = _decimal(candidate.value_m3)
            if candidate.reading_day < day and number < other or candidate.reading_day > day and number > other:
                raise ValueError(
                    f"Конфликт с показанием за {candidate.reading_day}: {candidate.value_m3} м³. "
                    "Накопленное показание не может уменьшаться. Проверьте значение; "
                    "при замене или сбросе счётчика укажите отдельную границу учёта."
                )

    def update_gas(self, report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        delete, reset, number = self._gas_payload(payload)
        with self._write_session() as session:
            _report, day, device_id = self._report_day(session, report_id)
            existing = session.scalar(
                select(GasReadingRow).where(
                    GasReadingRow.device_id == device_id,
                    GasReadingRow.reading_day == day,
                )
            )
            boundary = session.scalar(
                select(GasMeterBoundaryRow).where(
                    GasMeterBoundaryRow.device_id == device_id,
                    GasMeterBoundaryRow.boundary_day == day,
                )
            )
            if delete and existing is None:
                return self.gas(report_id)
            if (
                not delete
                and existing is not None
                and number is not None
                and existing.value_m3 == _decimal_text(number)
                and (not reset or boundary is not None)
            ):
                return self.gas(report_id)
            before = self._reading(existing) if existing else None
            if reset and boundary is None:
                boundary = GasMeterBoundaryRow(
                    id=f"reset:{report_id}",
                    device_id=device_id,
                    report_id=report_id,
                    boundary_day=day,
                    created_at=utcnow(),
                )
                session.add(boundary)
                session.flush()
                self._refresh_segments(session, device_id)
            if delete:
                assert existing is not None
                session.delete(existing)
                action, after, reading_id = "delete", None, existing.id
            else:
                assert number is not None
                self._validate_monotonic(session, device_id, day, number, existing.id if existing else None)
                now = utcnow()
                segment = self._segment_for_day(session, device_id, day)
                if existing is None:
                    existing = GasReadingRow(
                        id=str(uuid4()),
                        device_id=device_id,
                        report_id=report_id,
                        reading_day=day,
                        meter_segment=segment,
                        value_m3=_decimal_text(number),
                        entered_at=now,
                        updated_at=now,
                    )
                    session.add(existing)
                    action = "reset" if reset else "create"
                else:
                    existing.meter_segment, existing.value_m3, existing.updated_at = segment, _decimal_text(number), now
                    action = "reset" if reset else "update"
                session.flush()
                after, reading_id = self._reading(existing), existing.id
            session.add(
                GasReadingAuditRow(
                    id=str(uuid4()),
                    reading_id=reading_id,
                    device_id=device_id,
                    report_id=report_id,
                    reading_day=day,
                    action=action,
                    before_json=_json(before) if before else None,
                    after_json=_json(after) if after else None,
                    created_at=utcnow(),
                )
            )
        return self.gas(report_id)

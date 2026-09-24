"""Transactional persistence for owner supplied state and model decisions."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase
from .owner_data import OwnerDataMixin
from .telemetry import bump_revision, encode, next_id


def _micros(value: datetime | None = None) -> int:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _day(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _decimal(value: str | Decimal) -> str:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("value must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError("value must be a finite decimal")
    if number < 0:
        raise ValueError("value cannot be negative")
    return format(number, "f")


def _with_stable_identity(old: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    previous, changes = old or {}, incoming or {}
    for key in ("id", "source", "report_id", "meter_segment"):
        if key in previous and key in changes and previous[key] != changes[key]:
            raise ValueError(f"{key} identity cannot change")
    return {**previous, **changes}


def _page_size(limit: int) -> int:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    return limit


class OwnerRepository(OwnerDataMixin):
    """Stores effective profile history, gas readings, tariffs and AI choices."""

    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def profile_history(
        self, device_id: str, field: str, *, limit: int = 1000, after_revision: int = 0
    ) -> list[dict[str, Any]]:
        """Return append order; continue with the last row's ``revision``."""
        bound = _page_size(limit)
        if after_revision < 0:
            raise ValueError("after_revision must be non-negative")
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $field AS Utf8; DECLARE $after AS Int64; "
            "DECLARE $limit AS Uint64; SELECT revision,payload FROM owner_profile_revisions "
            "WHERE device_id=$device AND field=$field AND revision > $after "
            "ORDER BY revision LIMIT $limit;",
            {
                "$device": device_id,
                "$field": field,
                "$after": after_revision,
                "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64),
            },
        )[0].rows
        result = []
        for row in rows:
            item = json.loads(row.payload)
            item["revision"] = int(row.revision)
            result.append(item)
        return result

    def profile(self, device_id: str, *, as_of: datetime | None = None) -> dict[str, Any]:
        moment = _micros(as_of) if as_of else None
        at = moment if moment is not None else 9_223_372_036_854_775_807

        def read(tx: Transaction) -> dict[str, dict[str, Any]]:
            latest: dict[str, dict[str, Any]] = {}
            after_field, after_at, after_revision = "", -9_223_372_036_854_775_808, 0
            while True:
                rows = tx.execute(
                    "DECLARE $device AS Utf8; DECLARE $at AS Int64; DECLARE $field AS Utf8; "
                    "DECLARE $after_at AS Int64; DECLARE $revision AS Int64; DECLARE $has_after AS Bool; "
                    "DECLARE $limit AS Uint64; SELECT field,effective_at,revision,payload "
                    "FROM owner_profile_revisions WHERE device_id=$device AND effective_at <= $at "
                    "AND (NOT $has_after OR field > $field OR "
                    "(field = $field AND (effective_at > $after_at OR "
                    "(effective_at = $after_at AND revision > $revision)))) "
                    "ORDER BY field,effective_at,revision LIMIT $limit;",
                    {
                        "$device": device_id,
                        "$at": at,
                        "$field": after_field,
                        "$after_at": after_at,
                        "$revision": after_revision,
                        "$has_after": bool(after_field),
                        "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64),
                    },
                )[0].rows
                for row in rows:
                    latest[str(row.field)] = json.loads(row.payload)
                if len(rows) < 1000:
                    return latest
                last = rows[-1]
                after_field, after_at, after_revision = (str(last.field), int(last.effective_at), int(last.revision))

        return self.db.transaction(read)

    def save_profile_revision(
        self,
        device_id: str,
        field: str,
        value: Any,
        *,
        effective_at: datetime,
        provenance: str,
        expected_version: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        at = _micros(effective_at)
        if not device_id or not field or not provenance:
            raise ValueError("device, field and provenance are required")

        def write(tx: Transaction) -> int:
            rows = tx.execute(
                "DECLARE $device AS Utf8; DECLARE $field AS Utf8; "
                "SELECT revision FROM owner_profile_revisions WHERE device_id=$device AND field=$field "
                "ORDER BY revision DESC LIMIT 1;",
                {"$device": device_id, "$field": field},
            )[0].rows
            current = int(rows[0].revision) if rows else 0
            if expected_version is not None and current != expected_version:
                raise ValueError("profile revision changed")
            revision = current + 1
            payload = encode(
                {
                    **(metadata or {}),
                    "device_id": device_id,
                    "field": field,
                    "value": value,
                    "effective_at": at,
                    "recorded_at": _micros(),
                    "provenance": provenance,
                    "source": (metadata or {}).get("source", provenance),
                    "is_reset": value is None,
                    "revision": revision,
                }
            )
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $field AS Utf8; DECLARE $at AS Int64; "
                "DECLARE $revision AS Int64; DECLARE $payload AS Utf8; "
                "UPSERT INTO owner_profile_revisions (device_id,effective_at,field,revision,payload) "
                "VALUES ($device,$at,$field,$revision,$payload);",
                {"$device": device_id, "$at": at, "$field": field, "$revision": revision, "$payload": payload},
            )
            bump_revision(tx, "owner-profile:" + device_id)
            return revision

        return self.db.transaction(write)

    def gas_reading(self, device_id: str, reading_day: str) -> dict[str, Any] | None:
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
            "SELECT payload FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
            {"$device": device_id, "$day": _day(reading_day)},
        )[0].rows
        return json.loads(rows[0].payload) if rows else None

    def gas_audit_history(self, device_id: str, *, limit: int = 1000, after_id: int = 0) -> list[dict[str, Any]]:
        bound = _page_size(limit)
        if after_id < 0:
            raise ValueError("after_id must be non-negative")
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $after AS Int64; DECLARE $limit AS Uint64; "
            "SELECT id,payload FROM gas_reading_audit WHERE device_id=$device AND id > $after "
            "ORDER BY id LIMIT $limit;",
            {"$device": device_id, "$after": after_id, "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [{**json.loads(row.payload), "audit_id": int(row.id)} for row in rows]

    def save_gas_reading(
        self,
        device_id: str,
        reading_day: str,
        value_m3: str | Decimal,
        *,
        payload: dict[str, Any] | None = None,
        expected_version: int | None = None,
        reason: str = "save",
    ) -> dict[str, Any]:
        day, exact = _day(reading_day), _decimal(value_m3)
        if not device_id:
            raise ValueError("device identity is required")

        def write(tx: Transaction) -> dict[str, Any]:
            params = {"$device": device_id, "$day": day}
            rows = tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                "SELECT payload FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                params,
            )[0].rows
            old = json.loads(rows[0].payload) if rows else None
            version = int(old.get("version", 0)) if old else 0
            if expected_version is not None and version != expected_version:
                raise ValueError("gas reading changed")
            item = {
                **_with_stable_identity(old, payload),
                "device_id": device_id,
                "reading_day": day,
                "value_m3": exact,
                "version": version + 1,
                "updated_at": _micros(),
            }
            encoded = encode(item)
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_readings (device_id,reading_day,payload) VALUES ($device,$day,$payload);",
                {**params, "$payload": encoded},
            )
            audit_id = next_id(tx, "gas_reading_audit")
            audit_payload = encode({"before": old, "after": item, "reason": reason})
            tx.execute(
                "DECLARE $id AS Int64; DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_reading_audit (id,device_id,reading_day,at,payload) "
                "VALUES ($id,$device,$day,$at,$payload);",
                {
                    "$id": audit_id,
                    "$device": device_id,
                    "$day": day,
                    "$at": item["updated_at"],
                    "$payload": audit_payload,
                },
            )
            bump_revision(tx, "owner-gas:" + device_id)
            return item

        return self.db.transaction(write)

    def move_gas_reading(self, device_id: str, old_day: str, new_day: str, *, expected_version: int) -> dict[str, Any]:
        source, target = _day(old_day), _day(new_day)
        if source == target:
            result = self.gas_reading(device_id, source)
            if not result or int(result.get("version", 0)) != expected_version:
                raise ValueError("gas reading changed or missing")
            return result

        def write(tx: Transaction) -> dict[str, Any]:
            decl = "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
            source_rows = tx.execute(
                decl + "SELECT payload FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                {"$device": device_id, "$day": source},
            )[0].rows
            target_rows = tx.execute(
                decl + "SELECT payload FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                {"$device": device_id, "$day": target},
            )[0].rows
            if not source_rows or target_rows:
                raise ValueError("source missing or destination occupied")
            item = json.loads(source_rows[0].payload)
            if int(item.get("version", 0)) != expected_version:
                raise ValueError("gas reading changed")
            moved = {**item, "reading_day": target, "version": expected_version + 1, "updated_at": _micros()}
            tx.execute(
                decl
                + "DECLARE $payload AS Utf8; "
                + "UPSERT INTO gas_readings (device_id,reading_day,payload) VALUES ($device,$day,$payload);",
                {"$device": device_id, "$day": target, "$payload": encode(moved)},
            )
            tx.execute(
                decl + "DELETE FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                {"$device": device_id, "$day": source},
            )
            for audit_day, audit_item in (
                (source, {"action": "move_from", "to": target}),
                (target, {"action": "move_to", "from": source, "reading": moved}),
            ):
                audit_id = next_id(tx, "gas_reading_audit")
                tx.execute(
                    "DECLARE $id AS Int64; DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                    "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
                    "UPSERT INTO gas_reading_audit (id,device_id,reading_day,at,payload) "
                    "VALUES ($id,$device,$day,$at,$payload);",
                    {
                        "$id": audit_id,
                        "$device": device_id,
                        "$day": audit_day,
                        "$at": moved["updated_at"],
                        "$payload": encode(audit_item),
                    },
                )
            bump_revision(tx, "owner-gas:" + device_id)
            return moved

        return self.db.transaction(write)

    def save_tariff(
        self,
        scope: str,
        effective_month: str,
        price: str | Decimal,
        currency: str,
        *,
        expected_version: int | None = None,
        metadata: dict[str, Any] | None = None,
        reason: str = "save",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"\d{4}-\d{2}", effective_month):
            raise ValueError("effective month must use YYYY-MM")
        month = _day(effective_month + "-01")[:7]
        exact = _decimal(price)
        if not scope or not currency:
            raise ValueError("scope and currency are required")

        def write(tx: Transaction) -> dict[str, Any]:
            params = {"$scope": scope, "$month": month}
            rows = tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; "
                "SELECT payload FROM gas_tariffs WHERE scope=$scope AND effective_month=$month;",
                params,
            )[0].rows
            old = json.loads(rows[0].payload) if rows else None
            version = int(old.get("version", 0)) if old else 0
            if expected_version is not None and version != expected_version:
                raise ValueError("tariff changed")
            item = {
                **_with_stable_identity(old, metadata),
                "scope": scope,
                "effective_month": month,
                "price": exact,
                "currency": currency.upper(),
                "version": version + 1,
                "updated_at": _micros(),
            }
            encoded = encode(item)
            tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_tariffs (scope,effective_month,payload) VALUES ($scope,$month,$payload);",
                {**params, "$payload": encoded},
            )
            audit_id = next_id(tx, "gas_tariff_audit")
            audit_payload = encode({"before": old, "after": item, "reason": reason})
            tx.execute(
                "DECLARE $id AS Int64; DECLARE $scope AS Utf8; DECLARE $month AS Utf8; "
                "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_tariff_audit (id,scope,effective_month,at,payload) "
                "VALUES ($id,$scope,$month,$at,$payload);",
                {
                    "$id": audit_id,
                    "$scope": scope,
                    "$month": month,
                    "$at": item["updated_at"],
                    "$payload": audit_payload,
                },
            )
            bump_revision(tx, "tariff:" + scope)
            return item

        return self.db.transaction(write)

    def tariff_history(self, scope: str, *, limit: int = 1000, after_month: str = "") -> list[dict[str, Any]]:
        bound = _page_size(limit)
        rows = self.db.execute(
            "DECLARE $scope AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT effective_month,payload FROM gas_tariffs WHERE scope=$scope "
            "AND effective_month > $after ORDER BY effective_month LIMIT $limit;",
            {"$scope": scope, "$after": after_month, "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [{**json.loads(row.payload), "effective_month": str(row.effective_month)} for row in rows]

    def tariff_audit_history(self, scope: str, *, limit: int = 1000, after_id: int = 0) -> list[dict[str, Any]]:
        bound = _page_size(limit)
        if after_id < 0:
            raise ValueError("after_id must be non-negative")
        rows = self.db.execute(
            "DECLARE $scope AS Utf8; DECLARE $after AS Int64; DECLARE $limit AS Uint64; "
            "SELECT id,payload FROM gas_tariff_audit WHERE scope=$scope AND id > $after "
            "ORDER BY id LIMIT $limit;",
            {"$scope": scope, "$after": after_id, "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [{**json.loads(row.payload), "audit_id": int(row.id)} for row in rows]

    def save_meter_boundary(
        self,
        device_id: str,
        boundary_day: str,
        *,
        meter_id: str,
        expected_version: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        day = _day(boundary_day)
        if not device_id or not meter_id:
            raise ValueError("device and meter identities are required")

        def write(tx: Transaction) -> dict[str, Any]:
            params = {"$device": device_id, "$day": day}
            rows = tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                "SELECT payload FROM gas_meter_boundaries WHERE device_id=$device AND boundary_day=$day;",
                params,
            )[0].rows
            old = json.loads(rows[0].payload) if rows else None
            version = int(old.get("version", 0)) if old else 0
            if expected_version is not None and version != expected_version:
                raise ValueError("meter boundary changed")
            item = {
                **_with_stable_identity(old, metadata),
                "device_id": device_id,
                "boundary_day": day,
                "meter_id": meter_id,
                "version": version + 1,
                "updated_at": _micros(),
            }
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_meter_boundaries (device_id,boundary_day,payload) "
                "VALUES ($device,$day,$payload);",
                {**params, "$payload": encode(item)},
            )
            audit_id = next_id(tx, "gas_meter_boundary_audit")
            tx.execute(
                "DECLARE $id AS Int64; DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_meter_boundary_audit (id,device_id,boundary_day,at,payload) "
                "VALUES ($id,$device,$day,$at,$payload);",
                {
                    "$id": audit_id,
                    "$device": device_id,
                    "$day": day,
                    "$at": item["updated_at"],
                    "$payload": encode(item),
                },
            )
            bump_revision(tx, "owner-gas:" + device_id)
            return item

        return self.db.transaction(write)

    def create_model_proposal(
        self,
        proposal_id: str,
        settings: dict[str, Any],
        *,
        base_settings_version: int,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        if not proposal_id:
            raise ValueError("proposal identity is required")
        proposed = {
            "settings": settings,
            "model": model,
            "effort": effort,
            "base_settings_version": base_settings_version,
        }

        def write(tx: Transaction) -> None:
            rows = tx.execute(
                "DECLARE $id AS Utf8; SELECT payload FROM model_review_proposals WHERE id=$id;", {"$id": proposal_id}
            )[0].rows
            if rows:
                old = json.loads(rows[0].payload)
                if any(old.get(key) != value for key, value in proposed.items()):
                    raise ValueError("proposal identity already has different content")
                return
            tx.execute(
                "DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO model_review_proposals (id,payload) VALUES ($id,$payload);",
                {"$id": proposal_id, "$payload": encode({**proposed, "decision": None, "created_at": _micros()})},
            )

        self.db.transaction(write)

    def ai_settings(self, scope: str = "default") -> dict[str, Any] | None:
        rows = self.db.execute(
            "DECLARE $scope AS Utf8; SELECT version,payload,effective_at FROM ai_settings_revisions "
            "WHERE scope=$scope ORDER BY version DESC LIMIT 1;",
            {"$scope": scope},
        )[0].rows
        return (
            {
                "version": int(rows[0].version),
                "effective_at": int(rows[0].effective_at),
                "settings": json.loads(rows[0].payload),
            }
            if rows
            else None
        )

    def save_ai_settings(
        self, settings: dict[str, Any], *, scope: str = "default", expected_version: int | None = None
    ) -> int:
        def write(tx: Transaction) -> int:
            rows = tx.execute(
                "DECLARE $scope AS Utf8; SELECT version FROM ai_settings_revisions WHERE scope=$scope "
                "ORDER BY version DESC LIMIT 1;",
                {"$scope": scope},
            )[0].rows
            current = int(rows[0].version) if rows else 0
            if expected_version is not None and current != expected_version:
                raise ValueError("AI settings changed")
            version = current + 1
            tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $version AS Int64; DECLARE $at AS Int64; "
                "DECLARE $payload AS Utf8; UPSERT INTO ai_settings_revisions "
                "(scope,version,effective_at,payload) VALUES ($scope,$version,$at,$payload);",
                {"$scope": scope, "$version": version, "$at": _micros(), "$payload": encode(settings)},
            )
            bump_revision(tx, "ai-settings:" + scope)
            return version

        return self.db.transaction(write)

    def decide_model_proposal(
        self, proposal_id: str, *, decision: str, expected_settings_version: int, settings_scope: str = "default"
    ) -> int:
        if decision not in {"accepted", "rejected"}:
            raise ValueError("decision must be accepted or rejected")

        def write(tx: Transaction) -> int:
            rows = tx.execute(
                "DECLARE $id AS Utf8; SELECT payload FROM model_review_proposals WHERE id=$id;", {"$id": proposal_id}
            )[0].rows
            if not rows:
                raise KeyError(proposal_id)
            proposal = json.loads(rows[0].payload)
            if proposal.get("decision") is not None:
                raise ValueError("proposal already decided")
            version_rows = tx.execute(
                "DECLARE $scope AS Utf8; SELECT version FROM ai_settings_revisions WHERE scope=$scope "
                "ORDER BY version DESC LIMIT 1;",
                {"$scope": settings_scope},
            )[0].rows
            current = int(version_rows[0].version) if version_rows else 0
            if current != expected_settings_version or int(proposal.get("base_settings_version", -1)) != current:
                raise ValueError("AI settings changed")
            new_version = current
            if decision == "accepted":
                new_version += 1
                tx.execute(
                    "DECLARE $scope AS Utf8; DECLARE $version AS Int64; DECLARE $at AS Int64; "
                    "DECLARE $payload AS Utf8; UPSERT INTO ai_settings_revisions "
                    "(scope,version,effective_at,payload) VALUES ($scope,$version,$at,$payload);",
                    {
                        "$scope": settings_scope,
                        "$version": new_version,
                        "$at": _micros(),
                        "$payload": encode(proposal["settings"]),
                    },
                )
                bump_revision(tx, "ai-settings:" + settings_scope)
            proposal["decision"] = decision
            proposal["decided_at"] = _micros()
            proposal["settings_version"] = new_version if decision == "accepted" else current
            tx.execute(
                "DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO model_review_proposals (id,payload) VALUES ($id,$payload);",
                {"$id": proposal_id, "$payload": encode(proposal)},
            )
            return new_version

        return self.db.transaction(write)

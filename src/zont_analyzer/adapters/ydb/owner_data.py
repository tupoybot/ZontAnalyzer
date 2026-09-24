"""Application owner and tariff persistence in serializable YDB transactions."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase
from .telemetry import bump_revision, encode, next_id


class OwnerDataMixin:
    """High-level operations used by owner-facing application stores."""

    db: YdbDatabase

    def application_tariff_history(self, scope: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        after = ""
        while True:
            rows = self.db.execute(
                "DECLARE $scope AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
                "SELECT effective_month,payload FROM gas_tariffs WHERE scope=$scope "
                "AND effective_month > $after ORDER BY effective_month LIMIT $limit;",
                {"$scope": scope, "$after": after, "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64)},
            )[0].rows
            result.extend(_tariff_public(json.loads(row.payload)) for row in rows)
            if len(rows) < 1000:
                return result
            after = str(rows[-1].effective_month)

    def application_tariff_save(
        self,
        scope: str,
        action: str,
        month: str | None,
        effective_from: str | None,
        price: str,
        currency: str,
        now: str,
        *,
        current_month: str | None = None,
        tariff_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        def save(tx: Transaction) -> dict[str, Any]:
            selected_month = month
            if action == "correct":
                rows = tx.execute(
                    "DECLARE $scope AS Utf8; SELECT effective_month,payload FROM gas_tariffs "
                    "WHERE scope=$scope ORDER BY effective_month;",
                    {"$scope": scope},
                )[0].rows
                found = next((row for row in rows if json.loads(row.payload).get("id") == tariff_id), None)
                if found is None:
                    raise KeyError(tariff_id)
                selected_month = str(found.effective_month)
                old = json.loads(found.payload)
            else:
                assert selected_month is not None
                rows = tx.execute(
                    "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; SELECT payload FROM gas_tariffs "
                    "WHERE scope=$scope AND effective_month=$month;",
                    {"$scope": scope, "$month": selected_month},
                )[0].rows
                old = json.loads(rows[0].payload) if rows else None
            before = {"price": old["price"], "currency": old["currency"]} if old else None
            after = {"price": price, "currency": currency}
            if before == after:
                assert old is not None
                item = old
                result_action, idempotent = action, True
            else:
                if old and action == "create" and selected_month <= (current_month or ""):
                    raise ValueError("historical tariff month already exists; use an explicit correction")
                result_action, idempotent = ("correct" if old else "create"), False
                if old:
                    item = {**old, **after}
                else:
                    assert effective_from is not None
                    item = {
                        "id": str(uuid4()),
                        "scope": scope,
                        "effective_month": selected_month,
                        "effective_from": effective_from,
                        "recorded_at": now,
                        **after,
                        "corrections": [],
                    }
                event = {
                    "id": str(uuid4()),
                    "action": result_action,
                    "before": before,
                    "after": after,
                    "reason": reason if action == "correct" else None,
                    "created_at": now,
                }
                if result_action == "correct":
                    item["corrections"] = [*item.get("corrections", []), event]
                tx.execute(
                    "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; DECLARE $payload AS Utf8; "
                    "UPSERT INTO gas_tariffs (scope,effective_month,payload) VALUES ($scope,$month,$payload);",
                    {"$scope": scope, "$month": selected_month, "$payload": encode(item)},
                )
                audit_id = next_id(tx, "gas_tariff_audit")
                tx.execute(
                    "DECLARE $id AS Int64; DECLARE $scope AS Utf8; DECLARE $month AS Utf8; "
                    "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
                    "UPSERT INTO gas_tariff_audit (id,scope,effective_month,at,payload) "
                    "VALUES ($id,$scope,$month,$at,$payload);",
                    {
                        "$id": audit_id,
                        "$scope": scope,
                        "$month": selected_month,
                        "$at": _iso_micros(now),
                        "$payload": encode(event),
                    },
                )
                bump_revision(tx, "tariff:" + scope, publication_scope="tariff",
                              identifier=_iso_timestamp(item["effective_from"]))
            following = tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; SELECT payload FROM gas_tariffs "
                "WHERE scope=$scope AND effective_month > $month ORDER BY effective_month LIMIT 1;",
                {"$scope": scope, "$month": selected_month},
            )[0].rows
            end = json.loads(following[0].payload)["effective_from"] if following else None
            public_item = _tariff_public(item)
            return {
                "action": result_action,
                "id": item["id"],
                "tariff": public_item,
                "affected_start": _iso_timestamp(item["effective_from"]),
                "affected_end": _iso_timestamp(end) if end is not None else None,
                "idempotent": idempotent,
            }

        return self.db.transaction(save)


def _iso_micros(value: str) -> int:
    from datetime import UTC, datetime

    return int(datetime.fromisoformat(value).astimezone(UTC).timestamp() * 1_000_000)


def _iso_timestamp(value: Any) -> str:
    from datetime import UTC, datetime

    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1_000_000, UTC).isoformat()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).isoformat()


def _tariff_public(item: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in item.items() if key != "scope"}
    for key in ("effective_from", "recorded_at"):
        result[key] = _iso_timestamp(item[key])
    corrections = []
    for correction in item.get("corrections", []):
        event = dict(correction)
        if "created_at" in event:
            event["created_at"] = _iso_timestamp(event["created_at"])
        corrections.append(event)
    result["corrections"] = corrections
    return result


def _timestamp(value: Any) -> int:
    from datetime import UTC, datetime

    if isinstance(value, int):
        return value
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if isinstance(value, str) else value
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _now() -> int:
    from datetime import UTC, datetime

    return _timestamp(datetime.now(UTC))


def _profile_state(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    latest_auto: dict[str, dict[str, Any]] = {}
    manual_active: set[str] = set()
    for row in sorted(rows, key=lambda r: (_timestamp(r["effective_at"]), _timestamp(r["recorded_at"]), r["id"])):
        field = row["field"]
        if row.get("is_reset", False):
            manual_active.discard(field)
            if field in latest_auto:
                current[field] = latest_auto[field]
            else:
                current.pop(field, None)
            continue
        value = row.get("value", json.loads(row["value_json"]) if "value_json" in row else None)
        item = {"value": value, "source": row["source"]}
        if row["source"] == "auto":
            latest_auto[field] = item
            if field not in manual_active:
                current[field] = item
        else:
            manual_active.add(field)
            current[field] = item
    return current


def _profile_rows(tx: Transaction, device_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    after_field, after_revision = "", 0
    while True:
        rows = tx.execute(
            "DECLARE $device AS Utf8; DECLARE $field AS Utf8; DECLARE $revision AS Int64; "
            "DECLARE $limit AS Uint64; SELECT field,revision,payload FROM owner_profile_revisions "
            "WHERE device_id=$device AND (field > $field OR (field=$field AND revision>$revision)) "
            "ORDER BY field,revision LIMIT $limit;",
            {
                "$device": device_id,
                "$field": after_field,
                "$revision": after_revision,
                "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64),
            },
        )[0].rows
        result.extend(json.loads(r.payload) for r in rows)
        if len(rows) < 1000:
            return result
        after_field, after_revision = str(rows[-1].field), int(rows[-1].revision)


def _device_exists(tx: Transaction, device_id: str) -> bool:
    rows = tx.execute("DECLARE $id AS Utf8; SELECT id FROM devices WHERE id=$id;", {"$id": device_id})[0].rows
    return bool(rows)


def _put_profile(tx: Transaction, item: dict[str, Any]) -> None:
    tx.execute(
        "DECLARE $device AS Utf8; DECLARE $field AS Utf8; DECLARE $revision AS Int64; "
        "DECLARE $effective AS Int64; DECLARE $payload AS Utf8; "
        "UPSERT INTO owner_profile_revisions (device_id,field,revision,effective_at,payload) "
        "VALUES ($device,$field,$revision,$effective,$payload);",
        {
            "$device": item["device_id"],
            "$field": item["field"],
            "$revision": item["revision"],
            "$effective": item["effective_at"],
            "$payload": encode(item),
        },
    )


def _application_profile_rows(self: OwnerDataMixin, device_id: str, moment: Any) -> list[dict[str, Any]]:
    at = _timestamp(moment)

    def read(tx: Transaction) -> list[dict[str, Any]]:
        if not _device_exists(tx, device_id):
            raise KeyError(device_id)
        return [r for r in _profile_rows(tx, device_id) if _timestamp(r["effective_at"]) <= at]

    rows = self.db.transaction(read)
    return sorted(rows, key=lambda r: (_timestamp(r["effective_at"]), _timestamp(r["recorded_at"]), r["id"]))


def _application_profile_update(
    self: OwnerDataMixin,
    device_id: str,
    values: dict[str, tuple[bool, Any]],
    effective: Any | None,
) -> None:
    def write(tx: Transaction) -> None:
        if not _device_exists(tx, device_id):
            raise KeyError(device_id)
        at = _timestamp(effective) if effective is not None else _now()
        rows = _profile_rows(tx, device_id)
        current = _profile_state([r for r in rows if _timestamp(r["effective_at"]) <= at])
        lower = values.get("gas_min_m3h", (False, current.get("gas_min_m3h", {}).get("value")))[1]
        upper = values.get("gas_max_m3h", (False, current.get("gas_max_m3h", {}).get("value")))[1]
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("gas_min_m3h must not exceed gas_max_m3h")
        recorded = _now()
        changed = False
        for field, (reset, value) in values.items():
            old = current.get(field)
            if reset and (old is None or old["source"] != "manual"):
                continue
            if not reset and old and old["source"] == "manual" and old["value"] == value:
                continue
            revision = max((int(r["revision"]) for r in rows if r["field"] == field), default=0) + 1
            item = {
                "id": str(uuid4()),
                "device_id": device_id,
                "field": field,
                "value": value,
                "source": "manual",
                "provenance": "owner",
                "effective_at": at,
                "recorded_at": recorded,
                "is_reset": reset,
                "revision": revision,
            }
            _put_profile(tx, item)
            rows.append(item)
            changed = True
        if changed:
            bump_revision(tx, "owner-profile:" + device_id)

    self.db.transaction(write)


def _application_profile_observe(
    self: OwnerDataMixin,
    device_id: str,
    values: dict[str, tuple[Any, str]],
    effective: Any,
) -> None:
    def write(tx: Transaction) -> None:
        if not _device_exists(tx, device_id):
            raise KeyError(device_id)
        rows = _profile_rows(tx, device_id)
        changed = False
        for field, (value, provenance) in values.items():
            previous = max(
                (r for r in rows if r["field"] == field and r["source"] == "auto" and not r.get("is_reset", False)),
                key=lambda r: (_timestamp(r["recorded_at"]), r["id"]),
                default=None,
            )
            if (
                previous is not None
                and previous.get("value", json.loads(previous["value_json"]) if "value_json" in previous else None)
                == value
                and previous["provenance"] == provenance
            ):
                continue
            revision = max((int(r["revision"]) for r in rows if r["field"] == field), default=0) + 1
            item = {
                "id": str(uuid4()),
                "device_id": device_id,
                "field": field,
                "value": value,
                "source": "auto",
                "provenance": provenance,
                "effective_at": _timestamp(effective),
                "recorded_at": _now(),
                "is_reset": False,
                "revision": revision,
            }
            _put_profile(tx, item)
            rows.append(item)
            changed = True
        if changed:
            bump_revision(tx, "owner-profile:" + device_id)

    self.db.transaction(write)


def _application_devices(self: OwnerDataMixin) -> list[str]:
    rows = self.db.execute("SELECT id FROM devices ORDER BY id;")[0].rows
    return [str(row.id) for row in rows]


OwnerDataMixin.application_profile_rows = _application_profile_rows  # type: ignore[attr-defined]
OwnerDataMixin.application_profile_update = _application_profile_update  # type: ignore[attr-defined]
OwnerDataMixin.application_profile_observe = _application_profile_observe  # type: ignore[attr-defined]
OwnerDataMixin.application_devices = _application_devices  # type: ignore[attr-defined]


def _gas_rows(
    tx: Transaction, device_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    readings: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    after = ""
    while True:
        rows = tx.execute(
            "DECLARE $device AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT reading_day,payload FROM gas_readings WHERE device_id=$device AND reading_day>$after "
            "ORDER BY reading_day LIMIT $limit;",
            {"$device": device_id, "$after": after, "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64)},
        )[0].rows
        readings.extend(json.loads(row.payload) for row in rows)
        if len(rows) < 1000:
            break
        after = str(rows[-1].reading_day)
    after = ""
    while True:
        rows = tx.execute(
            "DECLARE $device AS Utf8; DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT boundary_day,payload FROM gas_meter_boundaries WHERE device_id=$device AND boundary_day>$after "
            "ORDER BY boundary_day LIMIT $limit;",
            {"$device": device_id, "$after": after, "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64)},
        )[0].rows
        boundaries.extend(json.loads(row.payload) for row in rows)
        if len(rows) < 1000:
            break
        after = str(rows[-1].boundary_day)
    after_id = 0
    while True:
        rows = tx.execute(
            "DECLARE $device AS Utf8; DECLARE $after AS Int64; DECLARE $limit AS Uint64; "
            "SELECT id,payload FROM gas_reading_audit WHERE device_id=$device AND id>$after "
            "ORDER BY id LIMIT $limit;",
            {"$device": device_id, "$after": after_id, "$limit": ydb.TypedValue(1000, ydb.PrimitiveType.Uint64)},
        )[0].rows
        audits.extend(json.loads(row.payload) for row in rows)
        if len(rows) < 1000:
            break
        after_id = int(rows[-1].id)
    return readings, boundaries, audits


def _segment(boundaries: list[dict[str, Any]], day: str) -> str:
    result = "default"
    for boundary in sorted(boundaries, key=lambda b: (b["boundary_day"], b["id"])):
        if boundary["boundary_day"] > day:
            break
        result = boundary["id"]
    return result


def _put_reading(tx: Transaction, row: dict[str, Any]) -> None:
    tx.execute(
        "DECLARE $device AS Utf8; DECLARE $day AS Utf8; DECLARE $payload AS Utf8; "
        "UPSERT INTO gas_readings (device_id,reading_day,payload) VALUES ($device,$day,$payload);",
        {"$device": row["device_id"], "$day": row["reading_day"], "$payload": encode(row)},
    )


def _public_reading(row: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime

    def iso(value: Any) -> str:
        return datetime.fromtimestamp(_timestamp(value) / 1_000_000, UTC).isoformat()

    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "report_id": row["report_id"],
        "day": row["reading_day"],
        "meter_segment": row["meter_segment"],
        "value_m3": row["value_m3"],
        "entered_at": iso(row["entered_at"]),
        "updated_at": iso(row["updated_at"]),
    }


def _application_gas_state(
    self: OwnerDataMixin, device_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return self.db.transaction(lambda tx: _gas_rows(tx, device_id))


def _application_gas_update(
    self: OwnerDataMixin,
    device_id: str,
    report_id: str,
    day: str,
    value_m3: str | None,
    *,
    delete: bool,
    reset: bool,
    reading_id: str | None,
    reading_id_supplied: bool,
) -> None:
    from decimal import Decimal

    def write(tx: Transaction) -> None:
        readings, boundaries, _audits = _gas_rows(tx, device_id)
        target = next((r for r in readings if r["reading_day"] == day), None)
        existing = target
        if isinstance(reading_id, str):
            existing = next((r for r in readings if r["id"] == reading_id), None)
            if existing is None:
                raise ValueError("Показание не найдено. Обновите страницу и выберите его в истории.")
            if delete and existing["reading_day"] != day:
                raise ValueError("Дата показания изменилась. Перед удалением выберите его в истории заново.")
            if existing["reading_day"] != day and any(b["boundary_day"] == existing["reading_day"] for b in boundaries):
                raise ValueError("Нельзя переместить показание на границе сброса счётчика.")
            if target is not None and target["id"] != existing["id"]:
                raise ValueError(f"Конфликт: показание за {day} уже существует.")
        elif reading_id_supplied and target is not None:
            raise ValueError(f"Конфликт: показание за {day} уже существует.")
        boundary = next((b for b in boundaries if b["boundary_day"] == day), None)
        if delete and existing is None:
            return
        if (
            not delete
            and existing is not None
            and existing["value_m3"] == value_m3
            and (not reset or boundary is not None)
            and existing["reading_day"] == day
        ):
            return
        before = _public_reading(existing) if existing else None
        if reset and boundary is None:
            boundary = {
                "id": f"reset:{uuid4()}",
                "device_id": device_id,
                "report_id": report_id,
                "boundary_day": day,
                "created_at": _now(),
            }
            boundaries.append(boundary)
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO gas_meter_boundaries (device_id,boundary_day,payload) VALUES ($device,$day,$payload);",
                {"$device": device_id, "$day": day, "$payload": encode(boundary)},
            )
            for prior in readings:
                segment = _segment(boundaries, prior["reading_day"])
                if prior["meter_segment"] != segment:
                    prior["meter_segment"] = segment
                    _put_reading(tx, prior)
        if delete:
            assert existing is not None
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                "DELETE FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                {"$device": device_id, "$day": existing["reading_day"]},
            )
            action, after, actual_id = "delete", None, existing["id"]
        else:
            assert value_m3 is not None
            segment = _segment(boundaries, day)
            number = Decimal(value_m3)
            for candidate in readings:
                if candidate is existing or _segment(boundaries, candidate["reading_day"]) != segment:
                    continue
                other = Decimal(candidate["value_m3"])
                if (
                    candidate["reading_day"] < day
                    and number < other
                    or candidate["reading_day"] > day
                    and number > other
                ):
                    raise ValueError(
                        f"Конфликт с показанием за {candidate['reading_day']}: {candidate['value_m3']} м³. "
                        "Накопленное показание не может уменьшаться. Проверьте значение; "
                        "при замене или сбросе счётчика укажите отдельную границу учёта."
                    )
            now = _now()
            if existing is None:
                row = {
                    "id": str(uuid4()),
                    "device_id": device_id,
                    "report_id": report_id,
                    "reading_day": day,
                    "meter_segment": segment,
                    "value_m3": value_m3,
                    "entered_at": now,
                    "updated_at": now,
                }
                action = "reset" if reset else "create"
            else:
                moved = existing["reading_day"] != day
                if moved:
                    tx.execute(
                        "DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
                        "DELETE FROM gas_readings WHERE device_id=$device AND reading_day=$day;",
                        {"$device": device_id, "$day": existing["reading_day"]},
                    )
                row = {
                    **existing,
                    "report_id": report_id,
                    "reading_day": day,
                    "meter_segment": segment,
                    "value_m3": value_m3,
                    "updated_at": now,
                }
                action = "move" if moved else ("reset" if reset else "update")
            _put_reading(tx, row)
            after, actual_id = _public_reading(row), row["id"]
        audit = {
            "id": str(uuid4()),
            "reading_id": actual_id,
            "device_id": device_id,
            "report_id": report_id,
            "reading_day": day,
            "action": action,
            "before_json": json.dumps(before, ensure_ascii=False, sort_keys=True) if before else None,
            "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True) if after else None,
            "created_at": _now(),
        }
        audit_id = next_id(tx, "gas_reading_audit")
        tx.execute(
            "DECLARE $id AS Int64; DECLARE $device AS Utf8; DECLARE $day AS Utf8; "
            "DECLARE $at AS Int64; DECLARE $payload AS Utf8; "
            "UPSERT INTO gas_reading_audit (id,device_id,reading_day,at,payload) "
            "VALUES ($id,$device,$day,$at,$payload);",
            {"$id": audit_id, "$device": device_id, "$day": day, "$at": audit["created_at"], "$payload": encode(audit)},
        )
        bump_revision(tx, "owner-gas:" + device_id)

    self.db.transaction(write)


OwnerDataMixin.application_gas_state = _application_gas_state  # type: ignore[attr-defined]
OwnerDataMixin.application_gas_update = _application_gas_update  # type: ignore[attr-defined]

"""Atomic AI dispatch reservations, usage accounting and successful-result cache."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import ydb  # type: ignore[import-untyped]

T = TypeVar("T")


class Transaction(Protocol):
    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]: ...


class Database(Protocol):
    def transaction(self, callback: Callable[[Transaction], T]) -> T: ...


def _row(result_sets: list[Any]) -> Any | None:
    return result_sets[0].rows[0] if result_sets and result_sets[0].rows else None


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clock_us() -> int:
    return time.time_ns() // 1_000


_GET_CALL = """
DECLARE $key AS Utf8;
SELECT call_key,job_key,state,payload,updated_at,created_at,sent_at
FROM llm_calls WHERE call_key=$key;
"""
_PUT_CALL = """
DECLARE $key AS Utf8; DECLARE $job AS Utf8; DECLARE $state AS Utf8;
DECLARE $payload AS Utf8; DECLARE $updated AS Int64; DECLARE $created AS Int64;
DECLARE $sent AS Int64?;
UPSERT INTO llm_calls (call_key,job_key,state,payload,updated_at,created_at,sent_at)
VALUES ($key,$job,$state,$payload,$updated,$created,$sent);
"""
_GET_BUDGET = """
DECLARE $month AS Utf8;
SELECT reserved_tokens,charged_tokens FROM ai_budget_months WHERE month=$month;
"""
_PUT_BUDGET = """
DECLARE $month AS Utf8; DECLARE $reserved AS Int64; DECLARE $charged AS Int64;
UPSERT INTO ai_budget_months (month,reserved_tokens,charged_tokens)
VALUES ($month,$reserved,$charged);
"""
_GET_CACHE = """
DECLARE $key AS Utf8;
SELECT fingerprint,payload,provenance,settings_version,model,created_at
FROM ai_response_cache WHERE fingerprint=$key;
"""
_PUT_CACHE = """
DECLARE $key AS Utf8; DECLARE $payload AS Utf8; DECLARE $provenance AS Utf8;
DECLARE $version AS Utf8; DECLARE $model AS Utf8; DECLARE $at AS Int64;
UPSERT INTO ai_response_cache (fingerprint,payload,provenance,settings_version,model,created_at)
VALUES ($key,$payload,$provenance,$version,$model,$at);
"""


class AiUsageRepository:
    """No network call is ever made inside a retried YDB transaction."""

    def __init__(self, db: Database, *, clock: Callable[[], int] = _clock_us) -> None:
        self.db = db
        self.clock = clock

    @staticmethod
    def _entry(row: Any) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(_text(row.payload))
        return {
            "key": _text(row.call_key), "job_key": _text(row.job_key),
            "status": _text(row.state), "payload": payload,
            "created_at": int(row.created_at), "updated_at": int(row.updated_at),
            "sent_at": int(row.sent_at) if row.sent_at is not None else None,
        }

    def cached(self, key: str) -> dict[str, Any] | None:
        def read(tx: Transaction) -> dict[str, Any] | None:
            cache = _row(tx.execute(_GET_CACHE, {"$key": key}))
            if cache is not None:
                return {"status": "success", "result": json.loads(_text(cache.payload)),
                        "provenance": json.loads(_text(cache.provenance))}
            call = _row(tx.execute(_GET_CALL, {"$key": key}))
            if call is None:
                return None
            entry = self._entry(call)
            return {"status": entry["status"], "error": entry["payload"].get("error")}

        return self.db.transaction(read)

    def reserve(
        self, key: str, job_key: str, request_payload: dict[str, Any], *,
        budget: int, estimate: int, billing_month: str,
    ) -> dict[str, Any] | None:
        if not key or not job_key or budget < 0 or estimate <= 0:
            raise ValueError("valid key, job, budget and estimate are required")
        if len(billing_month) != 7 or billing_month[4] != "-":
            raise ValueError("billing_month must be YYYY-MM")
        payload = dict(request_payload)
        payload.update(reserved_tokens=estimate, billing_month=billing_month)

        def save(tx: Transaction) -> dict[str, Any] | None:
            cache = _row(tx.execute(_GET_CACHE, {"$key": key}))
            if cache is not None:
                return {"status": "success", "result": json.loads(_text(cache.payload))}
            old = _row(tx.execute(_GET_CALL, {"$key": key}))
            if old is not None:
                existing = self._entry(old)
                if existing["job_key"] != job_key:
                    raise ValueError("request key belongs to a different job")
                return {"status": existing["status"], "error": existing["payload"].get("error")}
            budget_row = _row(tx.execute(_GET_BUDGET, {"$month": billing_month}))
            reserved = int(budget_row.reserved_tokens) if budget_row else 0
            charged = int(budget_row.charged_tokens) if budget_row else 0
            if reserved + charged + estimate > budget:
                raise RuntimeError("Monthly OpenAI token budget is exhausted (including reservations)")
            now = self.clock()
            self._put_call(tx, key, job_key, "prepared", payload, now, now, None)
            self._put_budget(tx, billing_month, reserved + estimate, charged)
            return None

        return self.db.transaction(save)

    def mark_sent(self, key: str) -> bool:
        def save(tx: Transaction) -> bool:
            row = _row(tx.execute(_GET_CALL, {"$key": key}))
            if row is None:
                raise KeyError(key)
            old = self._entry(row)
            if old["status"] != "prepared":
                return False
            now = self.clock()
            self._put_call(tx, key, old["job_key"], "sent", old["payload"],
                           now, old["created_at"], now)
            return True

        return self.db.transaction(save)

    def mark_unknown(self, key: str, error: str) -> None:
        def save(tx: Transaction) -> None:
            row = _row(tx.execute(_GET_CALL, {"$key": key}))
            if row is None:
                raise KeyError(key)
            old = self._entry(row)
            if old["status"] == "unknown":
                return
            if old["status"] != "sent":
                raise ValueError("only a sent call can become unknown")
            payload = {**old["payload"], "error": error}
            self._put_call(tx, key, old["job_key"], "unknown", payload,
                           self.clock(), old["created_at"], old["sent_at"])

        self.db.transaction(save)

    def finish_success(
        self, key: str, result: dict[str, Any], provenance: dict[str, Any], *,
        settings_version: str, model: str, input_tokens: int,
        cached_tokens: int, output_tokens: int, charge_reserved: bool = False,
    ) -> None:
        self._finish(
            key, "succeeded", result=result, provenance=provenance,
            settings_version=settings_version, model=model,
            input_tokens=input_tokens, cached_tokens=cached_tokens,
            output_tokens=output_tokens, charge_reserved=charge_reserved,
        )

    def finish_error(
        self, key: str, error: str, *, input_tokens: int,
        cached_tokens: int, output_tokens: int, charge_reserved: bool = False,
    ) -> None:
        self._finish(
            key, "error", error=error, input_tokens=input_tokens,
            cached_tokens=cached_tokens, output_tokens=output_tokens,
            charge_reserved=charge_reserved,
        )

    def _finish(
        self, key: str, state: str, *, result: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None, settings_version: str = "",
        model: str = "", error: str | None = None, input_tokens: int,
        cached_tokens: int, output_tokens: int, charge_reserved: bool = False,
    ) -> None:
        if min(input_tokens, cached_tokens, output_tokens) < 0:
            raise ValueError("token counts must be non-negative")

        def save(tx: Transaction) -> None:
            row = _row(tx.execute(_GET_CALL, {"$key": key}))
            if row is None:
                raise KeyError(key)
            old = self._entry(row)
            if old["status"] == state:
                if state == "succeeded":
                    cache = _row(tx.execute(_GET_CACHE, {"$key": key}))
                    if cache is None or json.loads(_text(cache.payload)) != result:
                        raise ValueError("conflicting successful response")
                return
            if old["status"] not in ("sent", "unknown"):
                raise ValueError("call has not been sent or is already final")
            month = str(old["payload"]["billing_month"])
            estimate = int(old["payload"]["reserved_tokens"])
            budget_row = _row(tx.execute(_GET_BUDGET, {"$month": month}))
            if budget_row is None:
                raise RuntimeError("missing AI budget reservation")
            charge = estimate if charge_reserved else input_tokens + output_tokens
            reserved = int(budget_row.reserved_tokens) - estimate
            charged = int(budget_row.charged_tokens) + charge
            if reserved < 0:
                raise RuntimeError("AI budget reservation underflow")
            payload = {
                **old["payload"], "input_tokens": input_tokens,
                "cached_tokens": cached_tokens, "output_tokens": output_tokens,
                "charged_tokens": charge,
            }
            if error is not None:
                payload["error"] = error
            if result is not None:
                payload["result"] = result
            if state == "succeeded":
                existing_cache = _row(tx.execute(_GET_CACHE, {"$key": key}))
                if existing_cache is not None:
                    if json.loads(_text(existing_cache.payload)) != result:
                        raise ValueError("fingerprint already has a different result")
                else:
                    tx.execute(_PUT_CACHE, {
                        "$key": key, "$payload": _json(result),
                        "$provenance": _json(provenance or {}),
                        "$version": settings_version, "$model": model,
                        "$at": self.clock(),
                    })
            self._put_call(tx, key, old["job_key"], state, payload,
                           self.clock(), old["created_at"], old["sent_at"])
            self._put_budget(tx, month, reserved, charged)

        self.db.transaction(save)

    def token_usage_this_month(self, now: datetime | None = None) -> int:
        month = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")

        def read(tx: Transaction) -> int:
            row = _row(tx.execute(_GET_BUDGET, {"$month": month}))
            return int(row.charged_tokens) if row is not None else 0

        return self.db.transaction(read)

    @staticmethod
    def _put_call(
        tx: Transaction, key: str, job: str, state: str, payload: dict[str, Any],
        updated: int, created: int, sent: int | None,
    ) -> None:
        tx.execute(_PUT_CALL, {
            "$key": key, "$job": job, "$state": state, "$payload": _json(payload),
            "$updated": updated, "$created": created,
            "$sent": ydb.TypedValue(sent, ydb.OptionalType(ydb.PrimitiveType.Int64)),
        })

    @staticmethod
    def _put_budget(tx: Transaction, month: str, reserved: int, charged: int) -> None:
        tx.execute(_PUT_BUDGET, {
            "$month": month, "$reserved": reserved, "$charged": charged,
        })

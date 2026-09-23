"""Transactional YDB leases and durable external-call dispatch state."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, TypeVar

import ydb  # type: ignore[import-untyped]


class Transaction(Protocol):
    def execute(
        self, query: str, parameters: dict[str, object] | None = None
    ) -> list[Any]: ...


T = TypeVar("T")


class Database(Protocol):
    def transaction(self, callback: Callable[[Transaction], T]) -> T: ...


Clock = Callable[[], int]


def _clock_us() -> int:
    return time.time_ns() // 1_000


def _first(result_sets: list[Any]) -> Any | None:
    if not result_sets:
        return None
    rows = result_sets[0].rows
    return rows[0] if rows else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class JobLease:
    job_key: str
    owner: str
    attempt: int
    lease_until: int
    state: str
    checkpoint: str | None


def _job(row: Any) -> JobLease:
    return JobLease(
        job_key=_text(row["job_key"]) or "",
        owner=_text(row["owner"]) or "",
        attempt=int(row["attempt"]),
        lease_until=int(row["lease_until"]),
        state=_text(row["state"]) or "",
        checkpoint=_text(row["checkpoint"]),
    )


_GET_JOB = """
DECLARE $job_key AS Utf8;
SELECT job_key, owner, attempt, lease_until, state, checkpoint
FROM jobs WHERE job_key = $job_key;
"""

_PUT_JOB = """
DECLARE $job_key AS Utf8;
DECLARE $owner AS Utf8;
DECLARE $attempt AS Int64;
DECLARE $lease_until AS Int64;
DECLARE $state AS Utf8;
DECLARE $checkpoint AS Utf8?;
UPSERT INTO jobs (job_key, owner, attempt, lease_until, state, checkpoint)
VALUES ($job_key, $owner, $attempt, $lease_until, $state, $checkpoint);
"""


class JobLeaseRepository:
    """All decisions are made inside a retried SerializableRW transaction.

    ``clock`` returns integer Unix microseconds, which keeps tests deterministic.
    Callbacks contain only YDB operations; no external side effect is retried.
    """

    def __init__(self, db: Database, *, clock: Clock = _clock_us) -> None:
        self.db = db
        self.clock = clock

    def get(self, job_key: str) -> JobLease | None:
        def read(tx: Transaction) -> JobLease | None:
            row = _first(tx.execute(_GET_JOB, {"$job_key": job_key}))
            return _job(row) if row is not None else None

        return self.db.transaction(read)

    def acquire(
        self, job_key: str, owner: str, lease_seconds: int
    ) -> JobLease | None:
        if not job_key or not owner or lease_seconds <= 0:
            raise ValueError("job_key, owner and positive lease_seconds are required")
        def take(tx: Transaction) -> JobLease | None:
            now = self.clock()
            until = now + lease_seconds * 1_000_000
            row = _first(tx.execute(_GET_JOB, {"$job_key": job_key}))
            old = _job(row) if row is not None else None
            if old is not None:
                if old.state == "done":
                    return None
                if old.lease_until > now:
                    # A retry after a lost response retains its fencing token.
                    return old if old.owner == owner else None
            lease = JobLease(
                job_key=job_key,
                owner=owner,
                attempt=old.attempt + 1 if old else 1,
                lease_until=until,
                state="active",
                checkpoint=old.checkpoint if old else None,
            )
            self._put(tx, lease)
            return lease

        return self.db.transaction(take)

    def renew(
        self, job_key: str, owner: str, attempt: int, lease_seconds: int
    ) -> JobLease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        def extend(tx: Transaction) -> JobLease | None:
            now = self.clock()
            old = self._owned_active(tx, job_key, owner, attempt, now)
            if old is None:
                return None
            # Do not shorten a lease when an earlier renewal response is lost.
            lease = JobLease(
                job_key, owner, attempt,
                max(old.lease_until, now + lease_seconds * 1_000_000),
                "active", old.checkpoint,
            )
            self._put(tx, lease)
            return lease

        return self.db.transaction(extend)

    def checkpoint(
        self, job_key: str, owner: str, attempt: int, checkpoint: str
    ) -> bool:
        def save(tx: Transaction) -> bool:
            now = self.clock()
            old = self._owned_active(tx, job_key, owner, attempt, now)
            if old is None:
                return False
            self._put(tx, JobLease(
                job_key, owner, attempt, old.lease_until, "active", checkpoint
            ))
            return True

        return self.db.transaction(save)

    def complete(self, job_key: str, owner: str, attempt: int) -> bool:
        def finish(tx: Transaction) -> bool:
            now = self.clock()
            old = self._owned_active(tx, job_key, owner, attempt, now)
            if old is None:
                # Completion is idempotent after a lost transaction response.
                row = _first(tx.execute(_GET_JOB, {"$job_key": job_key}))
                current = _job(row) if row is not None else None
                return bool(current and current.state == "done"
                            and current.owner == owner and current.attempt == attempt)
            self._put(tx, JobLease(
                job_key, owner, attempt, old.lease_until, "done", old.checkpoint
            ))
            return True

        return self.db.transaction(finish)

    @staticmethod
    def _owned_active(
        tx: Transaction, job_key: str, owner: str, attempt: int, now: int
    ) -> JobLease | None:
        row = _first(tx.execute(_GET_JOB, {"$job_key": job_key}))
        old = _job(row) if row is not None else None
        if (old is None or old.state != "active" or old.owner != owner
                or old.attempt != attempt or old.lease_until <= now):
            return None
        return old

    @staticmethod
    def _put(tx: Transaction, lease: JobLease) -> None:
        tx.execute(_PUT_JOB, {
            "$job_key": lease.job_key,
            "$owner": lease.owner,
            "$attempt": lease.attempt,
            "$lease_until": lease.lease_until,
            "$state": lease.state,
            "$checkpoint": ydb.TypedValue(
                lease.checkpoint, ydb.OptionalType(ydb.PrimitiveType.Utf8)
            ),
        })


@dataclass(frozen=True)
class LlmCall:
    call_key: str
    job_key: str
    state: str
    payload: str
    updated_at: int
    created_at: int
    sent_at: int | None


@dataclass(frozen=True)
class UsageTotals:
    calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost: Decimal
    truncated: bool


def _call(row: Any) -> LlmCall:
    return LlmCall(
        call_key=_text(row["call_key"]) or "",
        job_key=_text(row["job_key"]) or "",
        state=_text(row["state"]) or "",
        payload=_text(row["payload"]) or "",
        updated_at=int(row["updated_at"]),
        created_at=int(row["created_at"]),
        sent_at=int(row["sent_at"]) if row["sent_at"] is not None else None,
    )


_GET_CALL = """
DECLARE $call_key AS Utf8;
SELECT call_key, job_key, state, payload, updated_at, created_at, sent_at
FROM llm_calls WHERE call_key = $call_key;
"""

_PUT_CALL = """
DECLARE $call_key AS Utf8;
DECLARE $job_key AS Utf8;
DECLARE $state AS Utf8;
DECLARE $payload AS Utf8;
DECLARE $updated_at AS Int64;
DECLARE $created_at AS Int64;
DECLARE $sent_at AS Int64?;
UPSERT INTO llm_calls (call_key, job_key, state, payload, updated_at, created_at, sent_at)
VALUES ($call_key, $job_key, $state, $payload, $updated_at, $created_at, $sent_at);
"""


class UsageLedger:
    """Durable dispatch gate for a single external LLM call key.

    Dispatch is allowed only when ``mark_sent`` returns True. Once sent,
    an uncertain response must be reconciled, never dispatched again.
    """

    def __init__(self, db: Database, *, clock: Clock = _clock_us) -> None:
        self.db = db
        self.clock = clock

    def get(self, call_key: str) -> LlmCall | None:
        def read(tx: Transaction) -> LlmCall | None:
            row = _first(tx.execute(_GET_CALL, {"$call_key": call_key}))
            return _call(row) if row is not None else None

        return self.db.transaction(read)

    def list_calls(
        self, start_us: int, end_us: int, *, limit: int = 1000
    ) -> list[LlmCall]:
        if end_us <= start_us:
            raise ValueError("end_us must follow start_us")
        bound = max(0, min(limit, 1001))
        if bound == 0:
            return []
        query = """
        DECLARE $start AS Int64;
        DECLARE $end AS Int64;
        DECLARE $limit AS Uint64;
        SELECT call_key, job_key, state, payload, updated_at, created_at, sent_at
        FROM llm_calls
        WHERE sent_at >= $start AND sent_at < $end
        ORDER BY sent_at DESC, call_key LIMIT $limit;
        """

        def read(tx: Transaction) -> list[LlmCall]:
            rows = tx.execute(query, {
                "$start": start_us, "$end": end_us,
                "$limit": ydb.TypedValue(bound, ydb.PrimitiveType.Uint64),
            })[0].rows
            return [_call(row) for row in rows]

        return self.db.transaction(read)

    def usage_totals(self, start_us: int, end_us: int, *, limit: int = 1000) -> UsageTotals:
        """Aggregate structured successful payloads within a bounded interval.

        The stable first-dispatch timestamp keeps a later reconciliation in
        the original usage interval.
        """
        bound = max(0, min(limit, 1000))
        calls = self.list_calls(start_us, end_us, limit=bound + 1)
        truncated = len(calls) > bound
        calls = calls[:bound]
        input_tokens = cached_tokens = output_tokens = 0
        cost = Decimal(0)
        successful = 0
        for call in calls:
            if call.state != "succeeded":
                continue
            try:
                payload = json.loads(call.payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage", payload)
            if not isinstance(usage, dict):
                continue
            successful += 1
            input_tokens += int(usage.get("input_tokens", 0))
            cached_tokens += int(usage.get("cached_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
            cost += Decimal(str(usage.get("cost", "0")))
        return UsageTotals(
            successful, input_tokens, cached_tokens, output_tokens, cost, truncated,
        )

    def prepare(self, call_key: str, job_key: str, payload: str) -> LlmCall:
        if not call_key or not job_key:
            raise ValueError("call_key and job_key are required")
        now = self.clock()

        def save(tx: Transaction) -> LlmCall:
            row = _first(tx.execute(_GET_CALL, {"$call_key": call_key}))
            if row is not None:
                old = _call(row)
                if old.job_key != job_key:
                    raise ValueError("call_key already belongs to another job")
                return old
            new = LlmCall(call_key, job_key, "prepared", payload, now, now, None)
            self._put(tx, new)
            return new

        return self.db.transaction(save)

    def mark_sent(self, call_key: str) -> bool:
        def send(tx: Transaction) -> bool:
            now = self.clock()
            row = _first(tx.execute(_GET_CALL, {"$call_key": call_key}))
            if row is None:
                raise KeyError(call_key)
            old = _call(row)
            if old.state != "prepared":
                return False
            self._put(tx, LlmCall(
                call_key, old.job_key, "sent", old.payload, now, old.created_at, now,
            ))
            return True

        return self.db.transaction(send)

    def mark_succeeded(self, call_key: str, payload: str) -> LlmCall:
        return self._resolve(call_key, "succeeded", payload)

    def mark_error(self, call_key: str, payload: str) -> LlmCall:
        return self._resolve(call_key, "error", payload)

    def mark_unknown(self, call_key: str, payload: str) -> LlmCall:
        return self._resolve(call_key, "unknown", payload)

    def _resolve(self, call_key: str, state: str, payload: str) -> LlmCall:
        now = self.clock()

        def save(tx: Transaction) -> LlmCall:
            row = _first(tx.execute(_GET_CALL, {"$call_key": call_key}))
            if row is None:
                raise KeyError(call_key)
            old = _call(row)
            if old.state == state and old.payload == payload:
                return old
            allowed = old.state == "sent" or (old.state == "unknown" and state in ("succeeded", "error"))
            if not allowed:
                raise ValueError(f"cannot change LLM call from {old.state} to {state}")
            new = LlmCall(
                call_key, old.job_key, state, payload, now, old.created_at, old.sent_at,
            )
            self._put(tx, new)
            return new

        return self.db.transaction(save)

    @staticmethod
    def _put(tx: Transaction, call: LlmCall) -> None:
        tx.execute(_PUT_CALL, {
            "$call_key": call.call_key,
            "$job_key": call.job_key,
            "$state": call.state,
            "$payload": call.payload,
            "$updated_at": call.updated_at,
            "$created_at": call.created_at,
            "$sent_at": ydb.TypedValue(
                call.sent_at, ydb.OptionalType(ydb.PrimitiveType.Int64)
            ),
        })

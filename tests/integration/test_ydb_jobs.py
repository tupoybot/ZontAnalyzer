"""Concurrent lease and external-call guarantees against a real YDB fixture."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest

from zont_analyzer.adapters.ydb.jobs import JobLeaseRepository, UsageLedger


def _key() -> str:
    return uuid4().hex


def test_competing_workers_and_expired_fencing(ydb_database: object) -> None:
    now = [1_000_000]
    repo = JobLeaseRepository(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    job_key = _key()
    barrier = Barrier(8)

    def compete(i: int) -> object:
        barrier.wait()
        return repo.acquire(job_key, f"worker-{i}", 10)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(compete, range(8)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    first = winners[0]
    assert first.attempt == 1  # type: ignore[attr-defined]
    assert repo.acquire(job_key, first.owner, 10) == first  # type: ignore[attr-defined]
    assert repo.acquire(job_key, "other", 10) is None
    assert repo.checkpoint(job_key, first.owner, first.attempt, "page-1")  # type: ignore[attr-defined]

    now[0] += 10_000_000
    second = repo.acquire(job_key, "other", 10)
    assert second is not None
    assert second.attempt == 2
    assert second.checkpoint == "page-1"
    assert not repo.checkpoint(job_key, first.owner, first.attempt, "stale")  # type: ignore[attr-defined]
    assert repo.renew(job_key, first.owner, first.attempt, 10) is None  # type: ignore[attr-defined]
    assert not repo.complete(job_key, first.owner, first.attempt)  # type: ignore[attr-defined]
    assert repo.checkpoint(job_key, "other", second.attempt, "page-2")
    assert repo.get(job_key).checkpoint == "page-2"  # type: ignore[union-attr]
    assert repo.complete(job_key, "other", second.attempt)
    assert repo.complete(job_key, "other", second.attempt)
    assert repo.acquire(job_key, "third", 10) is None


def test_lease_renewal_cannot_revive_expired_attempt(ydb_database: object) -> None:
    now = [2_000_000]
    repo = JobLeaseRepository(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    job_key = _key()
    first = repo.acquire(job_key, "a", 5)
    assert first is not None
    now[0] += 2_000_000
    renewed = repo.renew(job_key, "a", first.attempt, 5)
    assert renewed is not None
    assert renewed.lease_until == 9_000_000
    now[0] = renewed.lease_until
    assert repo.renew(job_key, "a", first.attempt, 5) is None
    assert not repo.checkpoint(job_key, "a", first.attempt, "late")


def test_lease_rechecks_clock_when_transaction_starts(ydb_database: object) -> None:
    now = [3_000_000]
    job_key = _key()
    first_repo = JobLeaseRepository(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    lease = first_repo.acquire(job_key, "a", 5)
    assert lease is not None
    now[0] = lease.lease_until - 1

    class DelayedDatabase:
        def transaction(self, callback: object) -> object:
            now[0] = lease.lease_until
            return ydb_database.transaction(callback)  # type: ignore[attr-defined,arg-type]

    delayed_repo = JobLeaseRepository(DelayedDatabase(), clock=lambda: now[0])  # type: ignore[arg-type]
    assert delayed_repo.renew(job_key, "a", lease.attempt, 5) is None


def test_llm_dispatch_is_single_winner_and_unknown_is_not_redispatched(
    ydb_database: object,
) -> None:
    ledger = UsageLedger(ydb_database)  # type: ignore[arg-type]
    call_key = _key()
    job_key = _key()
    prepared = ledger.prepare(call_key, job_key, "request")
    assert prepared.state == "prepared"
    assert ledger.prepare(call_key, job_key, "request") == prepared
    with pytest.raises(ValueError):
        ledger.prepare(call_key, _key(), "request")

    barrier = Barrier(8)

    def dispatch(_: int) -> bool:
        barrier.wait()
        return ledger.mark_sent(call_key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(dispatch, range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert ledger.get(call_key).state == "sent"  # type: ignore[union-attr]
    assert ledger.mark_unknown(call_key, "timeout").state == "unknown"
    assert ledger.prepare(call_key, job_key, "request").state == "unknown"
    assert not ledger.mark_sent(call_key)
    assert ledger.mark_succeeded(call_key, "reconciled").state == "succeeded"
    assert ledger.mark_succeeded(call_key, "reconciled").state == "succeeded"
    with pytest.raises(ValueError):
        ledger.mark_error(call_key, "different terminal result")


def test_llm_call_cannot_be_resolved_before_send(ydb_database: object) -> None:
    ledger = UsageLedger(ydb_database)  # type: ignore[arg-type]
    call_key = _key()
    ledger.prepare(call_key, _key(), "request")
    with pytest.raises(ValueError):
        ledger.mark_succeeded(call_key, "response")
    assert ledger.mark_sent(call_key)
    assert ledger.mark_error(call_key, "known failure").state == "error"
    assert not ledger.mark_sent(call_key)


def test_usage_window_aggregates_structured_successes_without_silent_truncation(
    ydb_database: object,
) -> None:
    now = [100]
    ledger = UsageLedger(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    for _index in range(2):
        call_key = _key()
        ledger.prepare(call_key, _key(), "request")
        assert ledger.mark_sent(call_key)
        ledger.mark_succeeded(call_key, (
            '{"usage":{"input_tokens":10,"cached_tokens":2,'
            '"output_tokens":3,"cost":"0.125"}}'
        ))
        now[0] += 1
    calls = ledger.list_calls(100, 102)
    assert len(calls) == 2
    total = ledger.usage_totals(100, 102)
    assert (total.calls, total.input_tokens, total.cached_tokens,
            total.output_tokens, total.cost, total.truncated) == (
                2, 20, 4, 6, Decimal("0.250"), False,
            )
    assert ledger.usage_totals(100, 102, limit=1).truncated


def test_usage_month_remains_first_dispatch_month_after_later_reconciliation(
    ydb_database: object,
) -> None:
    october_start = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()) * 1_000_000
    september_start = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()) * 1_000_000
    now = [october_start - 1]
    ledger = UsageLedger(ydb_database, clock=lambda: now[0])  # type: ignore[arg-type]
    call_key = _key()
    prepared = ledger.prepare(call_key, _key(), "request")
    assert prepared.created_at == october_start - 1
    assert prepared.sent_at is None
    assert ledger.mark_sent(call_key)
    sent = ledger.get(call_key)
    assert sent is not None and sent.sent_at == october_start - 1

    now[0] = october_start + 10
    ledger.mark_unknown(call_key, "timeout")
    now[0] += 10
    resolved = ledger.mark_succeeded(call_key, '{"usage":{"input_tokens":7,"cost":"0.02"}}')
    assert resolved.created_at == prepared.created_at
    assert resolved.sent_at == sent.sent_at
    assert resolved.updated_at >= october_start
    assert ledger.usage_totals(september_start, october_start).input_tokens == 7
    assert ledger.usage_totals(october_start, october_start + 1_000_000).calls == 0

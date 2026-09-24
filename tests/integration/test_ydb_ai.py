"""AI provenance cache and model-review result transactions in real YDB."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from zont_analyzer.adapters.ydb.ai import AiResponseCache, ModelReviewRunRepository


@pytest.mark.ydb
def test_success_cache_is_immutable_and_keeps_unknown_model(ydb_database: object) -> None:
    cache = AiResponseCache(ydb_database, clock=lambda: 42)  # type: ignore[arg-type]
    fingerprint = uuid4().hex
    stored = cache.put_success(fingerprint, '{"answer":1}', '{"request_id":"r"}',
                               "settings-7", "historical-unknown-model")
    assert cache.get(fingerprint) == stored
    assert cache.put_success(fingerprint, '{"answer":1}', '{"request_id":"r"}',
                             "settings-7", "historical-unknown-model") == stored
    with pytest.raises(ValueError, match="different response"):
        cache.put_success(fingerprint, '{"answer":2}', '{"request_id":"r"}',
                          "settings-7", "historical-unknown-model")
    assert cache.get(fingerprint).model == "historical-unknown-model"  # type: ignore[union-attr]


@pytest.mark.ydb
def test_concurrent_cache_collision_has_one_immutable_winner(ydb_database: object) -> None:
    cache = AiResponseCache(ydb_database)  # type: ignore[arg-type]
    fingerprint = uuid4().hex
    barrier = Barrier(2)

    def write(index: int) -> bool:
        barrier.wait()
        try:
            cache.put_success(fingerprint, f'{{"answer":{index}}}', "{}", "v1", "model")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(write, (1, 2))) == 1
    assert cache.get(fingerprint) is not None


@pytest.mark.ydb
def test_model_review_run_and_state_commit_together(ydb_database: object) -> None:
    repo = ModelReviewRunRepository(ydb_database)  # type: ignore[arg-type]
    scope = uuid4().hex
    first_run = uuid4().hex
    recorded = repo.record_result(first_run, scope, 123, "succeeded", '{"model":"old-name"}',
                                  state_payload='{"next_due":456}', expected_state_version=0)
    assert repo.get_run(first_run) == recorded
    assert repo.get_state(scope).version == 1  # type: ignore[union-attr]
    assert repo.record_result(first_run, scope, 123, "succeeded", '{"model":"old-name"}',
                              state_payload='{"next_due":456}', expected_state_version=0) == recorded
    assert repo.get_state(scope).version == 1  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="different result"):
        repo.record_result(first_run, scope, 123, "error", "failure")

    second_run = uuid4().hex
    with pytest.raises(ValueError, match="stale"):
        repo.record_result(second_run, scope, 456, "error", "failure",
                           state_payload='{"retry":true}', expected_state_version=0)
    assert repo.get_run(second_run) is None
    error = repo.record_result(second_run, scope, 456, "error", "failure",
                               state_payload='{"retry":true}', expected_state_version=1)
    assert error.status == "error"
    assert repo.get_state(scope).version == 2  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="stale"):
        repo.save_state(scope, "different", expected_version=1)

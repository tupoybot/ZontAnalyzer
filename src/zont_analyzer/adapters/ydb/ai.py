"""Immutable successful AI responses and model-review results in YDB."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class Transaction(Protocol):
    def execute(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[Any]: ...


class Database(Protocol):
    def transaction(self, callback: Callable[[Transaction], T]) -> T: ...


def _first(result_sets: list[Any]) -> Any | None:
    return result_sets[0].rows[0] if result_sets and result_sets[0].rows else None


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _clock_us() -> int:
    return time.time_ns() // 1_000


@dataclass(frozen=True)
class CachedResponse:
    fingerprint: str
    payload: str
    provenance: str
    settings_version: str
    model: str
    created_at: int


def _cached(row: Any) -> CachedResponse:
    return CachedResponse(
        _text(row["fingerprint"]), _text(row["payload"]),
        _text(row["provenance"]), _text(row["settings_version"]),
        _text(row["model"]), int(row["created_at"]),
    )


_GET_CACHE = """
DECLARE $fingerprint AS Utf8;
SELECT fingerprint, payload, provenance, settings_version, model, created_at
FROM ai_response_cache WHERE fingerprint = $fingerprint;
"""

_PUT_CACHE = """
DECLARE $fingerprint AS Utf8;
DECLARE $payload AS Utf8;
DECLARE $provenance AS Utf8;
DECLARE $settings_version AS Utf8;
DECLARE $model AS Utf8;
DECLARE $created_at AS Int64;
UPSERT INTO ai_response_cache
(fingerprint, payload, provenance, settings_version, model, created_at)
VALUES ($fingerprint, $payload, $provenance, $settings_version, $model, $created_at);
"""


class AiResponseCache:
    """Only completed successful responses may be stored by the caller."""

    def __init__(self, db: Database, *, clock: Callable[[], int] = _clock_us) -> None:
        self.db = db
        self.clock = clock

    def get(self, fingerprint: str) -> CachedResponse | None:
        if not fingerprint:
            raise ValueError("fingerprint is required")

        def read(tx: Transaction) -> CachedResponse | None:
            row = _first(tx.execute(_GET_CACHE, {"$fingerprint": fingerprint}))
            return _cached(row) if row is not None else None

        return self.db.transaction(read)

    def put_success(
        self, fingerprint: str, payload: str, provenance: str,
        settings_version: str, model: str,
    ) -> CachedResponse:
        if not all((fingerprint, payload, provenance, settings_version, model)):
            raise ValueError("complete successful response metadata is required")
        created_at = self.clock()

        def save(tx: Transaction) -> CachedResponse:
            row = _first(tx.execute(_GET_CACHE, {"$fingerprint": fingerprint}))
            if row is not None:
                old = _cached(row)
                if (old.payload, old.provenance, old.settings_version, old.model) != (
                    payload, provenance, settings_version, model,
                ):
                    raise ValueError("fingerprint already belongs to a different response")
                return old
            new = CachedResponse(
                fingerprint, payload, provenance, settings_version, model, created_at,
            )
            tx.execute(_PUT_CACHE, {
                "$fingerprint": fingerprint, "$payload": payload,
                "$provenance": provenance, "$settings_version": settings_version,
                "$model": model, "$created_at": created_at,
            })
            return new

        return self.db.transaction(save)


@dataclass(frozen=True)
class ModelReviewRun:
    id: str
    scope: str
    started_at: int
    status: str
    payload: str


@dataclass(frozen=True)
class ModelReviewState:
    scope: str
    payload: str
    version: int


def _run(row: Any) -> ModelReviewRun:
    return ModelReviewRun(
        _text(row["id"]), _text(row["scope"]), int(row["started_at"]),
        _text(row["status"]), _text(row["payload"]),
    )


def _state(row: Any) -> ModelReviewState:
    return ModelReviewState(
        _text(row["scope"]), _text(row["payload"]), int(row["version"]),
    )


_GET_RUN = """
DECLARE $id AS Utf8;
SELECT id, scope, started_at, status, payload FROM model_review_runs WHERE id = $id;
"""

_PUT_RUN = """
DECLARE $id AS Utf8;
DECLARE $scope AS Utf8;
DECLARE $started_at AS Int64;
DECLARE $status AS Utf8;
DECLARE $payload AS Utf8;
UPSERT INTO model_review_runs (id, scope, started_at, status, payload)
VALUES ($id, $scope, $started_at, $status, $payload);
"""

_GET_STATE = """
DECLARE $scope AS Utf8;
SELECT scope, payload, version FROM model_review_state WHERE scope = $scope;
"""

_PUT_STATE = """
DECLARE $scope AS Utf8;
DECLARE $payload AS Utf8;
DECLARE $version AS Int64;
UPSERT INTO model_review_state (scope, payload, version)
VALUES ($scope, $payload, $version);
"""


class ModelReviewRunRepository:
    """Record immutable review outcomes and optionally advance state atomically."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get_run(self, run_id: str) -> ModelReviewRun | None:
        def read(tx: Transaction) -> ModelReviewRun | None:
            row = _first(tx.execute(_GET_RUN, {"$id": run_id}))
            return _run(row) if row is not None else None

        return self.db.transaction(read)

    def get_state(self, scope: str) -> ModelReviewState | None:
        def read(tx: Transaction) -> ModelReviewState | None:
            row = _first(tx.execute(_GET_STATE, {"$scope": scope}))
            return _state(row) if row is not None else None

        return self.db.transaction(read)

    def save_state(
        self, scope: str, payload: str, *, expected_version: int = 0,
    ) -> ModelReviewState:
        if not scope or not payload or expected_version < 0:
            raise ValueError("scope, payload and non-negative expected_version are required")

        def save(tx: Transaction) -> ModelReviewState:
            return self._advance_state(tx, scope, payload, expected_version)

        return self.db.transaction(save)

    def record_result(
        self, run_id: str, scope: str, started_at: int, status: str,
        payload: str, *, state_payload: str | None = None,
        expected_state_version: int | None = None,
    ) -> ModelReviewRun:
        if not all((run_id, scope, status, payload)):
            raise ValueError("complete model review result is required")
        if status not in ("succeeded", "error"):
            raise ValueError("only terminal model review results are immutable")
        if (state_payload is None) != (expected_state_version is None):
            raise ValueError("state payload and expected version are required together")
        if expected_state_version is not None and expected_state_version < 0:
            raise ValueError("expected_state_version must be non-negative")

        def save(tx: Transaction) -> ModelReviewRun:
            row = _first(tx.execute(_GET_RUN, {"$id": run_id}))
            if row is not None:
                old = _run(row)
                if old != ModelReviewRun(run_id, scope, started_at, status, payload):
                    raise ValueError("run ID already belongs to a different result")
                return old
            if state_payload is not None and expected_state_version is not None:
                self._advance_state(tx, scope, state_payload, expected_state_version)
            result = ModelReviewRun(run_id, scope, started_at, status, payload)
            tx.execute(_PUT_RUN, {
                "$id": run_id, "$scope": scope, "$started_at": started_at,
                "$status": status, "$payload": payload,
            })
            return result

        return self.db.transaction(save)

    @staticmethod
    def _advance_state(
        tx: Transaction, scope: str, payload: str, expected_version: int,
    ) -> ModelReviewState:
        row = _first(tx.execute(_GET_STATE, {"$scope": scope}))
        actual = int(row["version"]) if row is not None else 0
        if actual != expected_version:
            raise ValueError("stale model review state")
        next_state = ModelReviewState(scope, payload, actual + 1)
        tx.execute(_PUT_STATE, {
            "$scope": scope, "$payload": payload, "$version": next_state.version,
        })
        return next_state

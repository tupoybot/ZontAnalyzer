"""YDB persistence primitives for AI settings and model review."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import ydb  # type: ignore[import-untyped]

T = TypeVar("T")


class RawTransaction(Protocol):
    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]: ...


class Database(Protocol):
    def transaction(self, callback: Callable[[RawTransaction], T]) -> T: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _data(value: Any) -> Any:
    return json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)


def _row(result_sets: list[Any]) -> Any | None:
    return result_sets[0].rows[0] if result_sets and result_sets[0].rows else None


class ModelSettingsTransaction:
    def __init__(self, raw: RawTransaction) -> None:
        self.raw = raw

    def settings_head(self, scope: str = "default") -> dict[str, Any] | None:
        row = _row(self.raw.execute(
            "DECLARE $scope AS Utf8; SELECT version,effective_at,payload "
            "FROM ai_settings_revisions WHERE scope=$scope "
            "ORDER BY version DESC LIMIT 1;", {"$scope": scope},
        ))
        return ({"version": int(row.version), "effective_at": int(row.effective_at),
                 "payload": _data(row.payload)} if row is not None else None)

    def settings_history(self, scope: str = "default", *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.raw.execute(
            "DECLARE $scope AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT version,effective_at,payload FROM ai_settings_revisions "
            "WHERE scope=$scope ORDER BY version DESC LIMIT $limit;",
            {"$scope": scope,
             "$limit": ydb.TypedValue(min(max(limit, 0), 1000), ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [{"version": int(row.version), "effective_at": int(row.effective_at),
                 "payload": _data(row.payload)} for row in rows]

    def put_settings(self, version: int, payload: dict[str, Any], at: int, scope: str = "default") -> None:
        self.raw.execute(
            "DECLARE $scope AS Utf8; DECLARE $version AS Int64; DECLARE $at AS Int64; "
            "DECLARE $payload AS Utf8; UPSERT INTO ai_settings_revisions "
            "(scope,version,effective_at,payload) VALUES ($scope,$version,$at,$payload);",
            {"$scope": scope, "$version": version, "$at": at, "$payload": _json(payload)},
        )

    def state(self, scope: str) -> tuple[dict[str, Any], int] | None:
        row = _row(self.raw.execute(
            "DECLARE $scope AS Utf8; SELECT payload,version FROM model_review_state WHERE scope=$scope;",
            {"$scope": scope},
        ))
        return (_data(row.payload), int(row.version)) if row is not None else None

    def put_state(self, scope: str, payload: dict[str, Any], version: int) -> None:
        self.raw.execute(
            "DECLARE $scope AS Utf8; DECLARE $payload AS Utf8; DECLARE $version AS Int64; "
            "UPSERT INTO model_review_state (scope,payload,version) "
            "VALUES ($scope,$payload,$version);",
            {"$scope": scope, "$payload": _json(payload), "$version": version},
        )

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = _row(self.raw.execute(
            "DECLARE $id AS Utf8; SELECT payload FROM model_review_runs WHERE id=$id;",
            {"$id": run_id},
        ))
        return _data(row.payload) if row is not None else None

    def runs(self, scope: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.raw.execute(
            "DECLARE $scope AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT payload FROM model_review_runs WHERE scope=$scope "
            "ORDER BY started_at DESC LIMIT $limit;",
            {"$scope": scope,
             "$limit": ydb.TypedValue(min(max(limit, 0), 1000), ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [_data(row.payload) for row in rows]

    def put_run(self, run: dict[str, Any]) -> None:
        self.raw.execute(
            "DECLARE $id AS Utf8; DECLARE $scope AS Utf8; DECLARE $at AS Int64; "
            "DECLARE $status AS Utf8; DECLARE $payload AS Utf8; "
            "UPSERT INTO model_review_runs (id,scope,started_at,status,payload) "
            "VALUES ($id,$scope,$at,$status,$payload);",
            {"$id": run["id"], "$scope": run["scope"], "$at": run["started_at_us"],
             "$status": run["status"], "$payload": _json(run)},
        )

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = _row(self.raw.execute(
            "DECLARE $id AS Utf8; SELECT payload FROM model_review_proposals WHERE id=$id;",
            {"$id": proposal_id},
        ))
        return _data(row.payload) if row is not None else None

    def proposals(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.raw.execute(
            "DECLARE $limit AS Uint64; SELECT payload FROM model_review_proposals "
            "ORDER BY id LIMIT $limit;",
            {"$limit": ydb.TypedValue(min(max(limit, 0), 1000), ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [_data(row.payload) for row in rows]

    def put_proposal(self, proposal: dict[str, Any]) -> None:
        self.raw.execute(
            "DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
            "UPSERT INTO model_review_proposals (id,payload) VALUES ($id,$payload);",
            {"$id": proposal["id"], "$payload": _json(proposal)},
        )


class ModelSettingsStorage:
    def __init__(self, db: Database) -> None:
        self.db = db

    def transaction(self, callback: Callable[[ModelSettingsTransaction], T]) -> T:
        return self.db.transaction(lambda raw: callback(ModelSettingsTransaction(raw)))

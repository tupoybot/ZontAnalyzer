"""Bounded native Query SDK transactions and versioned schema initialization."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TypeVar

import ydb  # type: ignore[import-untyped]

T = TypeVar("T")


@dataclass(frozen=True)
class YdbConfig:
    endpoint: str
    database: str
    namespace: str = "application"
    anonymous: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", self.namespace):
            raise ValueError("invalid YDB namespace")
        if not self.database.startswith("/") or any(c in self.database for c in '`";\n\r'):
            raise ValueError("invalid YDB database path")
        if not self.endpoint.startswith(("grpc://", "grpcs://")):
            raise ValueError("invalid YDB endpoint")
        if not self.anonymous and not self.endpoint.startswith("grpcs://"):
            raise ValueError("authenticated YDB requires TLS")

    @classmethod
    def from_environment(cls) -> YdbConfig:
        return cls(
            endpoint=os.environ["YDB_ENDPOINT"], database=os.environ["YDB_DATABASE"],
            namespace=os.environ.get("YDB_NAMESPACE", "application"),
            anonymous=os.environ.get("YDB_ANONYMOUS_CREDENTIALS") == "1",
        )


class Transaction:
    def __init__(self, raw: Any, prefix: str) -> None:
        self.raw = raw
        self.prefix = prefix

    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        # Query API streams multiple parts with the same logical result index.
        # Merely list(results)[0] silently loses later parts on realistic periods.
        with self.raw.execute(self.prefix + query, parameters=parameters) as results:
            merged: dict[int, Any] = {}
            for part in results:
                if part.truncated:
                    raise ValueError("YDB returned a truncated result")
                index = int(part.index or 0)
                if index in merged:
                    merged[index].rows.extend(part.rows)
                else:
                    merged[index] = part
            return [merged[index] for index in sorted(merged)]


class YdbDatabase:
    """Callbacks may only access the DB: the SDK can replay a transaction."""

    def __init__(self, config: YdbConfig) -> None:
        self.config = config
        self.path = config.database.rstrip("/") + "/" + config.namespace
        self.prefix = f'PRAGMA TablePathPrefix("{self.path}");\n'
        credentials = ydb.AnonymousCredentials() if config.anonymous else ydb.credentials_from_env_variables()
        self.driver = ydb.Driver(endpoint=config.endpoint, database=config.database, credentials=credentials)
        try:
            self.driver.wait(timeout=10, fail_fast=True)
        except Exception:
            self.driver.stop()
            raise
        self.pool = ydb.QuerySessionPool(self.driver, size=8)

    def close(self) -> None:
        self.pool.stop()
        self.driver.stop()

    def execute(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        results = list(self.pool.execute_with_retries(self.prefix + query, parameters=parameters))
        if any(result.truncated for result in results):
            raise ValueError("YDB returned a truncated result")
        return results

    def transaction(self, callback: Callable[[Transaction], T]) -> T:
        def run(session: Any) -> T:
            with session.transaction(ydb.QuerySerializableReadWrite()) as raw:
                value = callback(Transaction(raw, self.prefix))
                raw.commit()
                return value

        result: T = self.pool.retry_operation_sync(run)
        return result

    def initialize(self) -> None:
        from .schema import TABLES

        schema_hash = hashlib.sha256(json.dumps(TABLES, sort_keys=True).encode()).hexdigest()
        with suppress(ydb.AlreadyExists):
            self.driver.scheme_client.make_directory(self.path)
        existing = {entry.name for entry in self.driver.scheme_client.list_directory(self.path).children}
        if "metadata" in existing:
            rows = self.execute("SELECT value FROM metadata WHERE name='schema_hash';")[0].rows
            if rows and rows[0].value != schema_hash:
                raise ValueError("YDB schema changed; an explicit migration is required")
        for name, definition in TABLES.items():
            if name not in existing:
                self.execute(f"CREATE TABLE IF NOT EXISTS `{name}` ({definition});")
        # A future schema upgrade must explicitly handle the recorded version.
        def version(tx: Transaction) -> None:
            rows = tx.execute("SELECT value FROM metadata WHERE name = 'schema_version';")[0].rows
            if rows and rows[0].value != "1":
                raise ValueError("unsupported YDB schema version")
            tx.execute("UPSERT INTO metadata (name, value) VALUES ('schema_version', '1');")
            tx.execute(
                "DECLARE $hash AS Utf8; UPSERT INTO metadata (name,value) VALUES ('schema_hash',$hash);",
                {"$hash": schema_hash},
            )

        self.transaction(version)

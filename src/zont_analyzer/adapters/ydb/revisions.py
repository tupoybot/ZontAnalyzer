"""Coalesced publication changes and compare-and-set progress."""

from __future__ import annotations

from typing import Any

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase


class RevisionRepository:
    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def changes_since(self, revision: int, *, limit: int = 100) -> list[dict[str, Any]]:
        if revision < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid revision page")
        rows = self.db.execute(
            "DECLARE $after AS Int64; DECLARE $limit AS Uint64; "
            "SELECT scope,identifier,revision,payload FROM publication_changes VIEW by_revision "
            "WHERE revision > $after ORDER BY revision LIMIT $limit;",
            {"$after": revision, "$limit": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [dict(row) for row in rows]

    def progress(self) -> int:
        rows = self.db.execute("SELECT value FROM metadata WHERE name='publication_progress';")[0].rows
        return int(rows[0].value) if rows else 0

    def acknowledge(self, *, expected: int, through: int) -> bool:
        if expected < 0 or through < expected:
            raise ValueError("publication progress must be monotonic")

        def save(tx: Transaction) -> bool:
            rows = tx.execute("SELECT value FROM metadata WHERE name='publication_progress';")[0].rows
            current = int(rows[0].value) if rows else 0
            if current != expected:
                return False
            latest = tx.execute("SELECT revision FROM revisions WHERE scope='publication';")[0].rows
            maximum = int(latest[0].revision) if latest else 0
            if through > maximum:
                raise ValueError("cannot acknowledge a future revision")
            tx.execute(
                "DECLARE $value AS Utf8; UPSERT INTO metadata (name,value) VALUES ('publication_progress',$value);",
                {"$value": str(through)},
            )
            return True

        return self.db.transaction(save)

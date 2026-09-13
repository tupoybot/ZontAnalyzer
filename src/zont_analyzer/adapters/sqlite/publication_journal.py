"""Durable, coalescing inputs for incremental report publication."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from zont_analyzer.adapters.sqlite.database import Database


def record_change(
    db: Database, scope: str, identifier: str, *, session: Session | None = None,
) -> None:
    """Record one invalidation, coalescing repeated changes to the same key."""
    if not scope or not identifier:
        raise ValueError("publication journal scope and identifier are required")
    if session is not None:
        mark_change(session, scope, identifier)
        return
    statement = text(
        "INSERT INTO publication_changes (scope, identifier) VALUES (:scope, :identifier) "
        "ON CONFLICT(scope, identifier) DO UPDATE SET revision=excluded.revision"
    )
    with db.session() as transaction:
        transaction.execute(statement, {"scope": scope, "identifier": identifier})


def mark_change(session: Session, scope: str, identifier: str) -> None:
    """Record an invalidation in an already open database transaction."""
    if not scope or not identifier:
        raise ValueError("publication journal scope and identifier are required")
    session.execute(
        text(
            "INSERT INTO publication_changes (scope, identifier) VALUES (:scope, :identifier) "
            "ON CONFLICT(scope,identifier) DO UPDATE SET revision=excluded.revision"
        ),
        {"scope": scope, "identifier": identifier},
    )


def latest_revision(db: Database, *, session: Session | None = None) -> int:
    statement = text("SELECT COALESCE(MAX(revision), 0) FROM publication_changes")
    if session is not None:
        return int(session.execute(statement).scalar_one())
    with db.session() as transaction:
        return int(transaction.execute(statement).scalar_one())


def changes_since(
    db: Database, revision: int, through: int | None = None, *, session: Session | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Return the current high-water mark and journal keys after ``revision``."""
    if revision < 0 or through is not None and through < revision:
        raise ValueError("invalid publication journal revision range")
    def read(transaction: Session) -> tuple[int, list[dict[str, Any]]]:
        high_water = latest_revision(db, session=transaction)
        upper = min(through, high_water) if through is not None else high_water
        rows = transaction.execute(statement, {"revision": revision, "through": upper}).mappings()
        return high_water, [
            {"scope": str(row["scope"]), "identifier": str(row["identifier"]),
             "revision": int(row["revision"])}
            for row in rows
        ]
    statement = text(
        "SELECT revision, scope, identifier FROM publication_changes "
        "WHERE revision > :revision AND revision <= :through ORDER BY revision"
    )
    if session is not None:
        return read(session)
    with db.session() as transaction:
        return read(transaction)

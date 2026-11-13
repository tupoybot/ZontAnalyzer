"""Versioned day-cache orchestration; calculation does not depend on report AI."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.ydb.regulation import RegulationRepository
from zont_analyzer.analytics.regulation import ALGORITHM_VERSION, aggregate_regulation_facts


def regulation_fingerprint(*, source_revision: str, settings_version: str,
                           previous_boundary: dict[str, Any] | None = None,
                           rules: dict[str, Any] | None = None) -> str:
    """Include previous recovery state and all caller selection/rule dependencies."""
    return hashlib.sha256(json.dumps(
        {"algorithm": ALGORITHM_VERSION, "source": source_revision, "settings": settings_version,
         "previous_boundary": previous_boundary, "rules": rules or {}},
        sort_keys=True, allow_nan=False, separators=(",", ":"),
    ).encode()).hexdigest()


class RegulationFactStore:
    def __init__(self, db: Database) -> None:
        self.repository = RegulationRepository(db.storage)

    def read(self, scope: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.repository.read(scope, start, end)

    def save(self, facts: dict[str, Any], source_fingerprint: str,
             *, source_fence: dict[str, int | str] | None = None, publish: bool = False) -> bool:
        return self.repository.save(facts, source_fingerprint=source_fingerprint, source_fence=source_fence,
                                     publish=publish)

    def cached(self, scope: str, day_start: datetime, source_fingerprint: str,
               settings_version: str) -> dict[str, Any] | None:
        facts = self.read(scope, day_start, day_start + timedelta(seconds=1))
        return next((item for item in facts
                     if item.get("source_fingerprint") == source_fingerprint
                     and item.get("algorithm_version") == ALGORITHM_VERSION
                     and item.get("settings_version") == settings_version), None)

    def get_or_build(self, *, scope: str, start: datetime, source_fingerprint: str, settings_version: str,
                     build: Callable[[], dict[str, Any]], source_fence: dict[str, int | str] | None = None,
                     ) -> tuple[dict[str, Any], bool]:
        """Cache hit does no telemetry reads; caller computes cheap day markers first."""
        cached = self.cached(scope, start, source_fingerprint, settings_version)
        if cached is not None:
            return cached, True
        facts = build()
        if facts["scope"] != scope or facts["period_start"] != start.isoformat():
            raise ValueError("regulation builder returned another scope/period")
        if facts.get("settings_version") != settings_version:
            raise ValueError("regulation builder returned another settings version")
        self.save(facts, source_fingerprint, source_fence=source_fence)
        return {**facts, "source_fingerprint": source_fingerprint}, False

    def aggregate(self, scope: str, start: datetime, end: datetime, *, expected_days: int) -> dict[str, Any]:
        return aggregate_regulation_facts(self.read(scope, start, end), start=start, end=end,
                                          expected_days=expected_days)

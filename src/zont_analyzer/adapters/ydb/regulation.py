"""Bounded compact daily regulation facts, separate from paid AI attempts."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase
from .regulation_events import EventProjectionRepository, projection_states
from .telemetry import _record_publication_change, encode, utc_seconds


class RegulationRepository:
    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def read(self, scope: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "DECLARE $scope AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
            "SELECT payload FROM regulation_day_facts WHERE scope=$scope "
            "AND day_start >= $start AND day_start < $end ORDER BY day_start LIMIT 402;",
            {"$scope": scope, "$start": utc_seconds(start), "$end": utc_seconds(end)},
        )[0].rows
        if len(rows) > 401:
            raise ValueError("regulation aggregation is limited to 400 days plus one boundary predecessor")
        return [json.loads(row.payload) for row in rows]

    def source_events(self, device_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Four family predecessor/window reads in one stable snapshot transaction."""
        return EventProjectionRepository(self.db).source_context(device_id, start, end)

    def event_projection_states(self, devices: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        return self.db.transaction(lambda tx: projection_states(tx, devices))

    def save(self, facts: dict[str, Any], *, source_fingerprint: str,
             source_fence: dict[str, int | str] | None = None, publish: bool = False) -> bool:
        """Idempotent upsert; an offline upload must supply its source fence.

        All calculation happens outside this retryable transaction. A source
        fence is the complete telemetry/series revision map captured at export;
        it is checked atomically with upload, never trusted from stale local data.
        """
        scope = facts["scope"]
        start = utc_seconds(datetime.fromisoformat(facts["period_start"]))
        payload = encode({**facts, "source_fingerprint": source_fingerprint})

        def save(tx: Transaction) -> bool:
            expected_projection = facts.get("cache_dependency", {}).get("event_projection", {})
            if expected_projection:
                current_projection = projection_states(tx, tuple(sorted(expected_projection)))
                if current_projection != expected_projection:
                    raise ValueError("reliability event projection changed during calculation")
            negative = facts.get("cache_dependency", {}).get("negative_inputs", dict(
                facts.get("negative_input_guards", [])))
            if negative:
                first = self._read_fence(tx, ["meta:" + key for key in negative])
                if any(first["meta:" + key] != "" and int(first["meta:" + key]) < cutoff
                       for key, cutoff in negative.items()):
                    raise ValueError("previously absent regulation input arrived; calculate again")
            if source_fence is not None:
                actual = self._read_fence(tx, list(source_fence)) if any(
                    key.startswith("meta:") for key in source_fence) else self._legacy_fence(tx)
                if actual != source_fence:
                    raise ValueError("regulation export source changed; export again before upload")
            rows = tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $start AS Int64; "
                "SELECT payload FROM regulation_day_facts WHERE scope=$scope AND day_start=$start;",
                {"$scope": scope, "$start": start},
            )[0].rows
            if rows and rows[0].payload == payload:
                return False
            tx.execute(
                "DECLARE $scope AS Utf8; DECLARE $start AS Int64; DECLARE $payload AS Utf8; "
                "DECLARE $fingerprint AS Utf8; UPSERT INTO regulation_day_facts "
                "(scope,day_start,payload,fingerprint) VALUES ($scope,$start,$payload,$fingerprint);",
                {"$scope": scope, "$start": start, "$payload": payload, "$fingerprint": source_fingerprint},
            )
            if publish:
                _record_publication_change(tx, "regulation-facts", scope, payload=encode({
                    "change_start": int(datetime.fromisoformat(facts["period_start"]).timestamp() * 1_000_000),
                    "change_end": int(datetime.fromisoformat(facts["period_end"]).timestamp() * 1_000_000),
                }))
            return True
        return self.db.transaction(save)

    def source_fence(self, *, start: datetime | None = None, end: datetime | None = None,
                     device_ids: tuple[str, ...] = ()) -> dict[str, int | str]:
        """Fence only exported UTC days plus relevant device/series metadata.

        New ingestion outside this history does not block compact upload. The
        caller includes lookback and end-point observations in these bounds.
        An absent marker is fenced too, so late first arrivals are detected.
        """
        if start is None and end is None:
            return self._legacy_fence(self.db)
        if start is None or end is None:
            raise ValueError("scoped regulation source fence requires at most 33 days")
        return self.source_fences([(start, end)], device_ids=device_ids)[(start, end)]

    def source_fences(self, windows: list[tuple[datetime, datetime]], *,
                      device_ids: tuple[str, ...] = ()) -> dict[tuple[datetime, datetime], dict[str, int | str]]:
        """One metadata query for <=400 days plus predecessor, never raw telemetry."""
        if len(windows) > 401:
            raise ValueError("regulation fences are limited to 400 windows plus one predecessor")
        selected = {window: self._fence_keys(*window, device_ids=device_ids) for window in windows}
        if not selected:
            return {}
        keys = sorted({key for values in selected.values() for key in values})
        if len(keys) > 8192:
            raise ValueError("regulation fence metadata selection is too large")
        actual = self._read_fence(self.db, keys)
        return {window: {key: actual[key] for key in values} for window, values in selected.items()}

    def read_fence(self, keys: list[str]) -> dict[str, int | str]:
        """Read a bounded persisted lineage fence in one metadata query."""
        if len(keys) > 8192 or not all(key.startswith(("meta:", "revision:")) for key in keys):
            raise ValueError("invalid regulation lineage fence")
        return self._read_fence(self.db, keys) if keys else {}

    @staticmethod
    def _fence_keys(start: datetime, end: datetime, *, device_ids: tuple[str, ...]) -> list[str]:
        if end <= start or (end - start).days > 33:
            raise ValueError("scoped regulation source fence requires at most 33 days")
        day = start.astimezone(UTC).date()
        last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
        keys = []
        while day <= last:
            keys.append("meta:telemetry-day:" + day.isoformat())
            day += timedelta(days=1)
        keys.extend(prefix + device for device in sorted(set(device_ids))
                    for prefix in ("revision:device:", "revision:series:"))
        return keys

    @staticmethod
    def _legacy_fence(database: Transaction | YdbDatabase) -> dict[str, int | str]:
        rows = database.execute(
            "SELECT scope,revision FROM revisions WHERE "
            "(scope >= 'telemetry:' AND scope < 'telemetry;') OR "
            "(scope >= 'series:' AND scope < 'series;') OR "
            "(scope >= 'device:' AND scope < 'device;');",
        )[0].rows
        return {row.scope: int(row.revision) for row in rows}

    @staticmethod
    def _read_fence(database: Transaction | YdbDatabase, keys: list[str]) -> dict[str, int | str]:
        metadata = [key.removeprefix("meta:") for key in keys if key.startswith("meta:")]
        revisions = [key.removeprefix("revision:") for key in keys if key.startswith("revision:")]
        result = database.execute(
            "DECLARE $metadata AS List<Utf8>; DECLARE $revisions AS List<Utf8>; "
            "SELECT key,value FROM app_meta WHERE key IN $metadata; "
            "SELECT scope,revision FROM revisions WHERE scope IN $revisions;",
            {"$metadata": ydb.TypedValue(metadata, ydb.ListType(ydb.PrimitiveType.Utf8)),
             "$revisions": ydb.TypedValue(revisions, ydb.ListType(ydb.PrimitiveType.Utf8))},
        )
        actual: dict[str, int | str] = {"meta:" + key: "" for key in metadata}
        actual.update({"revision:" + key: 0 for key in revisions})
        actual.update({"meta:" + row.key: row.value for row in result[0].rows})
        actual.update({"revision:" + row.scope: int(row.revision) for row in result[1].rows})
        return actual

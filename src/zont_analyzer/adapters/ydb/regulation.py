"""Bounded compact daily regulation facts, separate from paid AI attempts."""
from __future__ import annotations

import json
from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from typing import Any

import ydb  # type: ignore[import-untyped]

from .database import Transaction, YdbDatabase
from .regulation_events import EventProjectionRepository, projection_states
from .telemetry import _record_publication_change, encode, utc_seconds
from .telemetry_fences import (
    PART_EDGES,
    WINDOW_FORMAT,
    WINDOW_PREFIX,
    hour_bounds,
    part_revisions,
    supports_precision,
    window_bounds,
)

HOUR_FENCE_LIMIT = 10_000
HOUR_FENCE_SPAN = timedelta(hours=HOUR_FENCE_LIMIT)


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
        """Fence only exported UTC hours plus relevant device/series metadata.

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
        first, last = hour_bounds(start, end)
        if (start != first or end != last) and supports_precision(start) and supports_precision(end):
            keys = [f"{WINDOW_PREFIX}{start.astimezone(UTC):{WINDOW_FORMAT}}/{end.astimezone(UTC):{WINDOW_FORMAT}}"]
        else:
            keys = [f"meta:telemetry-hours:{first:%Y-%m-%dT%H}/{last:%Y-%m-%dT%H}"]
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
        hours: dict[str, tuple[datetime, datetime]] = {}
        precision: dict[str, tuple[datetime, datetime]] = {}
        for key in keys:
            if key.startswith(WINDOW_PREFIX):
                precision[key] = window_bounds(key)
                hours[key] = hour_bounds(*precision[key])
            elif key.startswith("meta:telemetry-hours:"):
                left, separator, right = key.removeprefix("meta:telemetry-hours:").partition("/")
                try:
                    start = datetime.strptime(left, "%Y-%m-%dT%H").replace(tzinfo=UTC)
                    end = datetime.strptime(right, "%Y-%m-%dT%H").replace(tzinfo=UTC)
                except ValueError as exc:
                    raise ValueError("invalid regulation telemetry hour range") from exc
                if (not separator or start >= end or left != start.strftime("%Y-%m-%dT%H")
                        or right != end.strftime("%Y-%m-%dT%H")):
                    raise ValueError("invalid regulation telemetry hour range")
                hours[key] = (start, end)
        if any(end - start > HOUR_FENCE_SPAN for start, end in hours.values()):
            raise ValueError(f"regulation telemetry hour range exceeds {HOUR_FENCE_LIMIT} hours")

        # Retained boundary lineage can extend before the report period or have
        # gaps. Bound each scan without limiting the age of otherwise valid keys.
        batches: list[tuple[datetime, datetime, list[str]]] = []
        for key, (start, end) in sorted(hours.items(), key=lambda item: item[1]):
            if batches:
                first, last, batch_keys = batches[-1]
                if start <= last and end - first <= HOUR_FENCE_SPAN:
                    batch_keys.append(key)
                    batches[-1] = first, max(last, end), batch_keys
                    continue
            batches.append((start, end, [key]))

        metadata = [key.removeprefix("meta:") for key in keys
                    if key.startswith("meta:") and key not in hours]
        revisions = [key.removeprefix("revision:") for key in keys if key.startswith("revision:")]
        metadata_declarations = "DECLARE $metadata AS List<Utf8>; DECLARE $revisions AS List<Utf8>; "
        hour_declarations = "DECLARE $first AS Utf8; DECLARE $last AS Utf8; "
        metadata_query = (
            "SELECT key,value FROM app_meta WHERE key IN $metadata; "
            "SELECT scope,revision FROM revisions WHERE scope IN $revisions; "
        )
        columns = "identifier,revision,payload" if precision else "identifier,revision"
        hour_query = (
            f"SELECT {columns} FROM publication_changes WHERE scope='telemetry' "
            f"AND identifier >= $first AND identifier < $last ORDER BY identifier LIMIT {HOUR_FENCE_LIMIT + 1};"
        )

        def read(session: Transaction | YdbDatabase) -> dict[str, int | str]:
            parameters: dict[str, Any] = {
                "$metadata": ydb.TypedValue(metadata, ydb.ListType(ydb.PrimitiveType.Utf8)),
                "$revisions": ydb.TypedValue(revisions, ydb.ListType(ydb.PrimitiveType.Utf8)),
            }
            query = metadata_declarations + metadata_query
            if batches:
                first, last, _ = batches[0]
                parameters.update({"$first": first.strftime("%Y-%m-%dT%H"), "$last": last.strftime("%Y-%m-%dT%H")})
                query = metadata_declarations + hour_declarations + metadata_query + hour_query
            result = session.execute(query, parameters)
            actual: dict[str, int | str] = {"meta:" + key: "" for key in metadata}
            actual.update({"revision:" + key: 0 for key in revisions})
            actual.update({"meta:" + row.key: row.value for row in result[0].rows})
            actual.update({"revision:" + row.scope: int(row.revision) for row in result[1].rows})
            for index, (first, last, batch_keys) in enumerate(batches):
                rows = result[2].rows if index == 0 else session.execute(hour_declarations + hour_query, {
                    "$first": first.strftime("%Y-%m-%dT%H"), "$last": last.strftime("%Y-%m-%dT%H"),
                })[0].rows
                if len(rows) > HOUR_FENCE_LIMIT:
                    raise ValueError("regulation telemetry hour selection is truncated")
                identifiers = [row.identifier for row in rows]
                part_cache: dict[str, list[int]] = {}
                for key in batch_keys:
                    start_hour, end_hour = hours[key]
                    left, right = start_hour.strftime("%Y-%m-%dT%H"), end_hour.strftime("%Y-%m-%dT%H")
                    start, end = bisect_left(identifiers, left), bisect_left(identifiers, right)
                    if key not in precision:
                        actual[key] = max((int(row.revision) for row in rows[start:end]), default=0)
                        continue
                    exact_start, exact_end = precision[key]
                    first_part = bisect_left(PART_EDGES, exact_start.minute * 60 + exact_start.second)
                    last_part = bisect_left(PART_EDGES, exact_end.minute * 60 + exact_end.second)
                    last_partial_hour = exact_end.strftime("%Y-%m-%dT%H")
                    revisions_in_window = []
                    for row in rows[start:end]:
                        low = first_part if row.identifier == left else 0
                        high = last_part if row.identifier == last_partial_hour else len(PART_EDGES) - 1
                        if low == 0 and high == len(PART_EDGES) - 1:
                            revisions_in_window.append(int(row.revision))
                            continue
                        if row.identifier not in part_cache:
                            try:
                                payload = json.loads(row.payload)
                            except (ValueError, TypeError):
                                payload = None
                            part_cache[row.identifier] = part_revisions(payload, int(row.revision))
                        revisions_in_window.extend(part_cache[row.identifier][low:high])
                    actual[key] = max(revisions_in_window, default=0)
            return actual

        if len(batches) > 1 and isinstance(database, YdbDatabase):
            return database.transaction(read)
        return read(database)

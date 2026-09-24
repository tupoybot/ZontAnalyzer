"""Telemetry, catalogue, coverage and cursors committed as bounded YDB units."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any, Literal

import ydb  # type: ignore[import-untyped]

from zont_analyzer.domain import SourceEvent, TelemetryPoint

from .database import Transaction, YdbDatabase

CoverageState = Literal["complete", "empty", "failed", "unavailable"]


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def utc_seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware UTC-convertible timestamp is required")
    return int(value.timestamp())


def next_id(tx: Transaction, name: str) -> int:
    rows = tx.execute("DECLARE $name AS Utf8; SELECT value FROM sequences WHERE name=$name;", {"$name": name})[0].rows
    value = int(rows[0].value) + 1 if rows else 1
    tx.execute(
        "DECLARE $name AS Utf8; DECLARE $value AS Int64; "
        "UPSERT INTO sequences (name, value) VALUES ($name, $value);", {"$name": name, "$value": value},
    )
    return value


def bump_revision(
    tx: Transaction, scope: str, *, publication_scope: str | None = None, identifier: str = "",
) -> int:
    rows = tx.execute(
        "DECLARE $scope AS Utf8; SELECT revision FROM revisions WHERE scope=$scope;", {"$scope": scope},
    )[0].rows
    revision = int(rows[0].revision) + 1 if rows else 1
    tx.execute(
        "DECLARE $scope AS Utf8; DECLARE $revision AS Int64; "
        "UPSERT INTO revisions (scope, revision) VALUES ($scope, $revision);",
        {"$scope": scope, "$revision": revision},
    )
    if scope != "publication":
        publication_revision = bump_revision(tx, "publication")
        tx.execute(
            "DECLARE $scope AS Utf8; DECLARE $identifier AS Utf8; DECLARE $revision AS Int64; "
            "UPSERT INTO publication_changes (scope,identifier,revision,payload) "
            "VALUES ($scope,$identifier,$revision,'{}');",
            {"$scope": publication_scope or scope, "$identifier": identifier, "$revision": publication_revision},
        )
    return revision


class TelemetryRepository:
    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def save_devices(self, devices: Iterable[dict[str, Any]]) -> int:
        saved = 0
        for device in devices:
            device_id = str(device.get("device_id") or device.get("id") or "")
            if not device_id:
                raise ValueError("device identity is required")
            payload = encode(device)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            captured_at = int(datetime.now(UTC).timestamp() * 1_000_000)

            def write(
                tx: Transaction, device_id: str = device_id, payload: str = payload,
                digest: str = digest, captured_at: int = captured_at,
            ) -> None:
                previous = tx.execute(
                    "DECLARE $id AS Utf8; SELECT payload FROM devices WHERE id=$id;", {"$id": device_id},
                )[0].rows
                tx.execute(
                    "DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
                    "UPSERT INTO devices (id,payload) VALUES ($id,$payload);",
                    {"$id": device_id, "$payload": payload},
                )
                rows = tx.execute(
                    "DECLARE $id AS Utf8; DECLARE $hash AS Utf8; "
                    "SELECT id FROM config_snapshots WHERE device_id=$id AND content_hash=$hash;",
                    {"$id": device_id, "$hash": digest},
                )[0].rows
                if not rows:
                    snapshot_id = next_id(tx, "config_snapshots")
                    tx.execute(
                        "DECLARE $id AS Utf8; DECLARE $hash AS Utf8; DECLARE $n AS Int64; "
                        "DECLARE $payload AS Utf8; DECLARE $at AS Int64; "
                        "UPSERT INTO config_snapshots (device_id,content_hash,id,payload,captured_at) "
                        "VALUES ($id,$hash,$n,$payload,$at);",
                        {"$id": device_id, "$hash": digest, "$n": snapshot_id,
                         "$payload": payload, "$at": captured_at},
                    )
                if not previous or previous[0].payload != payload:
                    bump_revision(tx, "device:" + device_id)

            self.db.transaction(write)
            saved += 1
        return saved

    def list_devices(self) -> list[dict[str, Any]]:
        return [json.loads(row.payload) for row in self.db.execute("SELECT payload FROM devices ORDER BY id;")[0].rows]

    def upsert_entity(
        self, *, entity_id: str, device_id: str, source_type: str, external_id: str,
        display_name: str, role: str, unit: str | None, confidence: float,
        provenance: str = "zont history metadata",
    ) -> None:
        if not entity_id or not device_id or not 0 <= confidence <= 1:
            raise ValueError("invalid entity")
        payload = encode({"id": entity_id, "device_id": device_id, "source_type": source_type,
                          "external_id": external_id, "display_name": display_name, "role": role,
                          "unit": unit, "confidence": confidence, "provenance": provenance})

        def save(tx: Transaction) -> None:
            if not tx.execute("DECLARE $id AS Utf8; SELECT id FROM devices WHERE id=$id;",
                              {"$id": device_id})[0].rows:
                raise ValueError("entity device does not exist")
            entities = tx.execute("DECLARE $device AS Utf8; SELECT id,payload FROM entities WHERE device_id=$device;",
                                  {"$device": device_id})[0].rows
            for row in entities:
                existing = json.loads(row.payload)
                if row.id == entity_id and (existing["source_type"], existing["external_id"]) != (
                    source_type, external_id,
                ):
                    raise ValueError("entity identity cannot change")
                if (row.id != entity_id and existing["source_type"] == source_type
                        and existing["external_id"] == external_id):
                    raise ValueError("entity natural identity already exists")
            tx.execute(
                "DECLARE $device AS Utf8; DECLARE $id AS Utf8; DECLARE $payload AS Utf8; "
                "UPSERT INTO entities (device_id,id,payload) VALUES ($device,$id,$payload);",
                {"$device": device_id, "$id": entity_id, "$payload": payload},
            )

        self.db.transaction(save)

    def list_entities(self, device_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "DECLARE $device AS Utf8; SELECT payload FROM entities WHERE device_id=$device ORDER BY id;",
            {"$device": device_id},
        )[0].rows
        return [json.loads(r.payload) for r in rows]

    def _series(self, tx: Transaction, point: TelemetryPoint, role: str) -> int:
        params = {"$device": point.device_id, "$source": point.source_type,
                  "$entity": point.entity_id, "$metric": point.metric_key}
        declarations = (
            "DECLARE $device AS Utf8; DECLARE $source AS Utf8; DECLARE $entity AS Utf8; DECLARE $metric AS Utf8; "
        )
        rows = tx.execute(
            declarations + "SELECT id,payload FROM telemetry_series WHERE device_id=$device AND source_type=$source "
            "AND entity_id=$entity AND metric_key=$metric;", params,
        )[0].rows
        if rows:
            payload = json.loads(rows[0].payload)
            if payload.get("unit") is None and point.unit is not None:
                payload["unit"] = point.unit
                tx.execute(
                    declarations + "DECLARE $payload AS Utf8; UPDATE telemetry_series SET payload=$payload "
                    "WHERE device_id=$device AND source_type=$source AND entity_id=$entity AND metric_key=$metric;",
                    {**params, "$payload": encode(payload)},
                )
                bump_revision(tx, "series:" + point.device_id)
            return int(rows[0].id)
        series_id = next_id(tx, "telemetry_series")
        payload = {"id": series_id, "device_id": point.device_id, "source_type": point.source_type,
                   "entity_id": point.entity_id, "metric_key": point.metric_key, "role": role,
                   "unit": point.unit, "display_name": point.entity_id, "confidence": 0.3,
                   "provenance": "history source only", "origin": point.source_type}
        tx.execute(
            declarations + "DECLARE $id AS Int64; DECLARE $payload AS Utf8; "
            "UPSERT INTO telemetry_series (device_id,source_type,entity_id,metric_key,id,payload) "
            "VALUES ($device,$source,$entity,$metric,$id,$payload);",
            {**params, "$id": series_id, "$payload": encode(payload)},
        )
        return series_id

    def list_series(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload FROM telemetry_series ORDER BY id;")[0].rows
        return [json.loads(row.payload) for row in rows]

    def write_window(
        self, *, device_id: str, data_type: str, start: datetime, end: datetime,
        points: Iterable[TelemetryPoint] = (), events: Iterable[SourceEvent] = (),
        state: CoverageState = "complete", roles: dict[str, str] | None = None,
    ) -> int:
        """Only a fully saved successful response advances the independent cursor.

        A response is bounded to 2000 records. Split larger source requests, never
        mark partially committed pages as covered. The source's end point is kept.
        """
        started, ended = utc_seconds(start), utc_seconds(end)
        samples, source_events = list(points), list(events)
        if started >= ended or len(samples) + len(source_events) > 2000:
            raise ValueError("invalid or oversized ingestion window")
        if state not in {"complete", "empty", "failed", "unavailable"}:
            raise ValueError("invalid coverage state")
        if state != "complete" and (samples or source_events):
            raise ValueError("only complete windows can contain records")
        records: list[TelemetryPoint | SourceEvent] = [*samples, *source_events]
        if any(p.device_id != device_id or not started <= utc_seconds(p.timestamp_utc) <= ended for p in records):
            raise ValueError("record outside requested device/window")
        checked = int(datetime.now(UTC).timestamp() * 1_000_000)

        def write(tx: Transaction) -> int:
            rows: list[dict[str, Any]] = []
            changed = False
            series_cache: dict[tuple[str, str, str], int] = {}
            for point in samples:
                key = (point.source_type, point.entity_id, point.metric_key)
                if key not in series_cache:
                    series_cache[key] = self._series(tx, point, (roles or {}).get(point.entity_id, "unknown"))
                rows.append({"series_id": series_cache[key], "timestamp_utc": utc_seconds(point.timestamp_utc),
                             "value_num": point.value_num, "value_text": point.value_text,
                             "quality": point.quality, "ingested_at": checked})
            if rows:
                key_type = (ydb.TupleType().add_element(ydb.PrimitiveType.Int64)
                            .add_element(ydb.PrimitiveType.Int64))
                old_rows = tx.execute(
                    "DECLARE $keys AS List<Tuple<Int64,Int64>>; "
                    "SELECT * FROM telemetry_samples WHERE (series_id,timestamp_utc) IN $keys;",
                    {"$keys": ydb.TypedValue(
                        [(row["series_id"], row["timestamp_utc"]) for row in rows],
                        ydb.ListType(key_type),
                    )},
                )[0].rows
                existing = {(r.series_id, r.timestamp_utc): dict(r) for r in old_rows}
                rows = [r for r in rows if any(
                    existing.get((r["series_id"], r["timestamp_utc"]), {}).get(key) != r[key]
                    for key in ("series_id", "timestamp_utc", "value_num", "value_text", "quality")
                )]
                changed = bool(rows)
            if rows:
                row_type = (ydb.StructType().add_member("series_id", ydb.PrimitiveType.Int64)
                            .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
                            .add_member("value_num", ydb.OptionalType(ydb.PrimitiveType.Double))
                            .add_member("value_text", ydb.OptionalType(ydb.PrimitiveType.Utf8))
                            .add_member("quality", ydb.PrimitiveType.Utf8)
                            .add_member("ingested_at", ydb.PrimitiveType.Int64))
                tx.execute("UPSERT INTO telemetry_samples SELECT * FROM AS_TABLE($rows);",
                           {"$rows": ydb.TypedValue(rows, ydb.ListType(row_type))})
            changed_times = {int(row["timestamp_utc"]) for row in rows}
            if source_events:
                # Source IDs identify canonical events even when the source
                # corrects their timestamp. Resolve only this bounded batch of
                # IDs, then move old keys and write new rows in the same commit.
                by_id = {event.id: event for event in source_events}
                ids = ydb.TypedValue(list(by_id), ydb.ListType(ydb.PrimitiveType.Utf8))
                prior = tx.execute(
                    "DECLARE $ids AS List<Utf8>; "
                    "SELECT id,device_id,timestamp_utc,payload FROM source_events VIEW by_id "
                    "WHERE id IN $ids;", {"$ids": ids},
                )[0].rows
                old_by_id: dict[str, list[Any]] = {}
                for old in prior:
                    old_by_id.setdefault(str(old.id), []).append(old)
                old_keys: list[dict[str, Any]] = []
                new_rows: list[dict[str, Any]] = []
                for event_id, event in by_id.items():
                    payload = event.model_dump_json()
                    previous = old_by_id.get(event_id, [])
                    if previous and SourceEvent.model_validate_json(previous[0].payload) == event:
                        continue
                    for old in previous:
                        old_keys.append({"device_id": str(old.device_id),
                                         "timestamp_utc": int(old.timestamp_utc), "id": event_id})
                        changed_times.add(int(old.timestamp_utc))
                    new_rows.append({"device_id": device_id, "timestamp_utc": utc_seconds(event.timestamp_utc),
                                     "id": event_id, "payload": payload})
                    changed_times.add(utc_seconds(event.timestamp_utc))
                if new_rows:
                    changed = True
                    if old_keys:
                        key_type = (ydb.StructType().add_member("device_id", ydb.PrimitiveType.Utf8)
                                    .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
                                    .add_member("id", ydb.PrimitiveType.Utf8))
                        tx.execute("DECLARE $keys AS List<Struct<device_id:Utf8,timestamp_utc:Int64,id:Utf8>>; "
                                   "DELETE FROM source_events ON SELECT * FROM AS_TABLE($keys);",
                                   {"$keys": ydb.TypedValue(old_keys, ydb.ListType(key_type))})
                    row_type = (ydb.StructType().add_member("device_id", ydb.PrimitiveType.Utf8)
                                .add_member("timestamp_utc", ydb.PrimitiveType.Int64)
                                .add_member("id", ydb.PrimitiveType.Utf8)
                                .add_member("payload", ydb.PrimitiveType.Utf8))
                    tx.execute("DECLARE $rows AS List<Struct<device_id:Utf8,timestamp_utc:Int64,"
                               "id:Utf8,payload:Utf8>>; "
                               "UPSERT INTO source_events SELECT * FROM AS_TABLE($rows);",
                               {"$rows": ydb.TypedValue(new_rows, ydb.ListType(row_type))})
            # A failed recheck must not erase evidence of an earlier successful response.
            params = {"$device": device_id, "$type": data_type, "$start": started, "$end": ended}
            decl = "DECLARE $device AS Utf8; DECLARE $type AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
            old = tx.execute(
                decl + "SELECT state FROM coverage WHERE device_id=$device AND data_type=$type "
                "AND started_at=$start AND ended_at=$end;", params,
            )[0].rows
            if state in {"complete", "empty"} or not old or old[0].state not in {"complete", "empty"}:
                tx.execute(
                    decl + "DECLARE $state AS Utf8; DECLARE $at AS Int64; "
                    "UPSERT INTO coverage (device_id,data_type,started_at,ended_at,state,checked_at) "
                    "VALUES ($device,$type,$start,$end,$state,$at);",
                    {**params, "$state": state, "$at": checked},
                )
            if state in {"complete", "empty"}:
                tx.execute(
                    "DECLARE $device AS Utf8; DECLARE $type AS Utf8; DECLARE $end AS Int64; "
                    "$old = SELECT timestamp_utc FROM ingestion_cursors WHERE device_id=$device AND data_type=$type; "
                    "UPSERT INTO ingestion_cursors (device_id,data_type,timestamp_utc) "
                    "SELECT $device, $type, MAX_OF(COALESCE(MAX(timestamp_utc), $end), $end) FROM $old;",
                    {"$device": device_id, "$type": data_type, "$end": ended},
                )
            if changed:
                revision = bump_revision(tx, "telemetry:" + device_id)
                for day in {datetime.fromtimestamp(at, UTC).date().isoformat() for at in changed_times}:
                    tx.execute(
                        "DECLARE $key AS Utf8; DECLARE $value AS Utf8; "
                        "UPSERT INTO app_meta (key,value) VALUES ($key,$value);",
                        {"$key": f"telemetry-day:{day}", "$value": f"ydb:{device_id}:{revision}"},
                    )
            return len(samples) + len(source_events)

        return self.db.transaction(write)

    def read_period(self, device_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        """One consistent bounded calculation snapshot, including its revision.

        Large periods must be aggregated in explicit bounded jobs; silently
        calculating from a truncated query result is forbidden.
        """
        def read(tx: Transaction) -> dict[str, Any]:
            series_rows = tx.execute(
                "DECLARE $device AS Utf8; SELECT id,payload FROM telemetry_series WHERE device_id=$device ORDER BY id;",
                {"$device": device_id},
            )[0].rows
            params = {"$ids": ydb.TypedValue([r.id for r in series_rows], ydb.ListType(ydb.PrimitiveType.Int64)),
                      "$start": utc_seconds(start), "$end": utc_seconds(end)}
            samples = tx.execute(
                "DECLARE $ids AS List<Int64>; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                "SELECT * FROM telemetry_samples WHERE series_id IN $ids AND timestamp_utc >= $start "
                "AND timestamp_utc < $end ORDER BY series_id,timestamp_utc LIMIT 500001;", params,
            )[0].rows
            if len(samples) > 500000:
                raise ValueError("period exceeds bounded snapshot size")
            events = tx.execute(
                "DECLARE $device AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                "SELECT payload FROM source_events WHERE device_id=$device AND timestamp_utc >= $start "
                "AND timestamp_utc < $end ORDER BY timestamp_utc,id LIMIT 10001;",
                {"$device": device_id, "$start": utc_seconds(start), "$end": utc_seconds(end)},
            )[0].rows
            if len(events) > 10000:
                raise ValueError("period exceeds bounded event size")
            revisions = tx.execute(
                "DECLARE $scope AS Utf8; SELECT revision FROM revisions WHERE scope=$scope;",
                {"$scope": "telemetry:" + device_id},
            )[0].rows
            return {"series": [json.loads(r.payload) for r in series_rows], "samples": [dict(r) for r in samples],
                    "events": [SourceEvent.model_validate_json(r.payload) for r in events],
                    "revision": int(revisions[0].revision) if revisions else 0}

        return self.db.transaction(read)

    def period_pages(
        self, device_id: str, start: datetime, end: datetime, *, page_size: int = 2000,
    ) -> Iterator[dict[str, Any]]:
        """Optimistically consistent pages for long periods, without retaining all rows.

        Every page carries the original revision. A concurrent change invalidates
        the scan; the consumer must discard its partial calculation and restart.
        Save the final result using the same revision guard in ReportRepository.
        """
        if not 1 <= page_size <= 10000 or utc_seconds(start) >= utc_seconds(end):
            raise ValueError("invalid period page")

        def begin(tx: Transaction) -> tuple[int, list[int]]:
            rows = tx.execute(
                "DECLARE $scope AS Utf8; SELECT revision FROM revisions WHERE scope=$scope;",
                {"$scope": "telemetry:" + device_id},
            )[0].rows
            series = tx.execute(
                "DECLARE $device AS Utf8; SELECT id FROM telemetry_series WHERE device_id=$device ORDER BY id;",
                {"$device": device_id},
            )[0].rows
            return (int(rows[0].revision) if rows else 0, [int(r.id) for r in series])

        revision, series_ids = self.db.transaction(begin)
        for series_id in series_ids:
            after = utc_seconds(start) - 1
            while True:
                def read(tx: Transaction, series_id: int = series_id, after: int = after) -> list[dict[str, Any]]:
                    latest = tx.execute(
                        "DECLARE $scope AS Utf8; SELECT revision FROM revisions WHERE scope=$scope;",
                        {"$scope": "telemetry:" + device_id},
                    )[0].rows
                    if (int(latest[0].revision) if latest else 0) != revision:
                        raise ValueError("telemetry changed during paged calculation")
                    rows = tx.execute(
                        "DECLARE $id AS Int64; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                        "DECLARE $after AS Int64; DECLARE $limit AS Uint64; SELECT * FROM telemetry_samples "
                        "WHERE series_id=$id AND timestamp_utc >= $start AND timestamp_utc < $end "
                        "AND timestamp_utc > $after ORDER BY timestamp_utc LIMIT $limit;",
                        {"$id": series_id, "$start": utc_seconds(start), "$end": utc_seconds(end),
                         "$after": after, "$limit": ydb.TypedValue(page_size, ydb.PrimitiveType.Uint64)},
                    )[0].rows
                    return [dict(r) for r in rows]

                page = self.db.transaction(read)
                if not page:
                    break
                yield {"revision": revision, "series_id": series_id, "samples": page}
                after = page[-1]["timestamp_utc"]
                if len(page) < page_size:
                    break

    def get_cursor(self, device_id: str, data_type: str) -> datetime | None:
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $type AS Utf8; "
            "SELECT timestamp_utc FROM ingestion_cursors WHERE device_id=$device AND data_type=$type;",
            {"$device": device_id, "$type": data_type},
        )[0].rows
        return datetime.fromtimestamp(rows[0].timestamp_utc, UTC) if rows else None

    def read_samples(
        self, series_id: int, start: datetime, end: datetime, *, after: int | None = None, limit: int = 2000,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10000:
            raise ValueError("invalid page size")
        rows = self.db.execute(
            "DECLARE $id AS Int64; DECLARE $start AS Int64; DECLARE $end AS Int64; DECLARE $after AS Int64; "
            "DECLARE $limit AS Uint64; SELECT * FROM telemetry_samples "
            "WHERE series_id=$id AND timestamp_utc >= $start AND timestamp_utc < $end AND timestamp_utc > $after "
            "ORDER BY timestamp_utc LIMIT $limit;",
            {"$id": series_id, "$start": utc_seconds(start), "$end": utc_seconds(end),
             "$after": after if after is not None else utc_seconds(start) - 1,
             "$limit": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [dict(row) for row in rows]

    def coverage(
        self, device_id: str, data_type: str, start: datetime, end: datetime, *,
        after: tuple[int, int] | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 10000:
            raise ValueError("invalid coverage page")
        after_start, after_end = after or (-9_223_372_036_854_775_808, -9_223_372_036_854_775_808)
        rows = self.db.execute(
            "DECLARE $device AS Utf8; DECLARE $type AS Utf8; DECLARE $start AS Int64; DECLARE $end AS Int64; "
            "DECLARE $after_start AS Int64; DECLARE $after_end AS Int64; DECLARE $limit AS Uint64; "
            "SELECT started_at,ended_at,state FROM coverage WHERE device_id=$device AND data_type=$type "
            "AND started_at < $end AND ended_at > $start "
            "AND (started_at > $after_start OR (started_at=$after_start AND ended_at > $after_end)) "
            "ORDER BY started_at,ended_at LIMIT $limit;",
            {"$device": device_id, "$type": data_type, "$start": utc_seconds(start), "$end": utc_seconds(end),
             "$after_start": after_start, "$after_end": after_end,
             "$limit": ydb.TypedValue(limit, ydb.PrimitiveType.Uint64)},
        )[0].rows
        return [dict(row) for row in rows]

    def missing_intervals(
        self, device_id: str, data_type: str, start: datetime, end: datetime, *, now: datetime,
    ) -> list[tuple[int, int, str]]:
        """Return missing intervals with recoverability; never age out stored coverage."""
        first, last = utc_seconds(start), utc_seconds(end)
        if first >= last:
            raise ValueError("empty or reversed period")
        cursor = first
        missing = []
        horizon = utc_seconds(now) - 90 * 86400
        def intervals() -> Iterator[tuple[int, int]]:
            after = None
            while True:
                page = self.coverage(device_id, data_type, start, end, after=after)
                for row in page:
                    if row["state"] in {"complete", "empty"}:
                        yield max(first, row["started_at"]), min(last, row["ended_at"])
                if len(page) < 1000:
                    break
                after = (page[-1]["started_at"], page[-1]["ended_at"])
            yield last, last

        for left, right in intervals():
            if left > cursor:
                if cursor < horizon:
                    missing.append((cursor, min(left, horizon), "unavailable"))
                if left > horizon:
                    missing.append((max(cursor, horizon), left, "fetch"))
            cursor = max(cursor, right)
        return missing

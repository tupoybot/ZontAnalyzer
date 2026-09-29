"""Bounded archive completion with durable per-source coverage in YDB."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import SourceEvent, TelemetryPoint
from zont_analyzer.observability import observe, span


class CollectionService:
    def __init__(self, db: Database, client: ZontReadOnlyClient, config: AppConfig) -> None:
        self.db, self.client, self.config = db, client, config

    def ensure_period(
        self, start: datetime, end: datetime, *, now: datetime | None = None,
        max_requests: int = 24, replay_recent: bool = False,
        coverage_prefix: str = "", device_ids: set[str] | None = None,
        deadline: float | None = None, monotonic: Callable[[], float] = time.monotonic,
        replay_checked_after: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            with span("zont_collection"):
                result = self._ensure_period(
                    start, end, now=now, max_requests=max_requests, replay_recent=replay_recent,
                    coverage_prefix=coverage_prefix, device_ids=device_ids,
                    deadline=deadline, monotonic=monotonic, replay_checked_after=replay_checked_after,
                )
        except Exception:
            observe("zont_sync_observed_timestamp_seconds", datetime.now(UTC).timestamp())
            observe("zont_sync_success", 0)
            observe("zont_collection_runs_total", outcome="failure")
            raise
        observe("zont_sync_observed_timestamp_seconds", datetime.now(UTC).timestamp())
        observe("zont_sync_success", float(not result["failed_windows"]))
        outcome = "failure" if result["failed_windows"] else "pending" if result["pending"] else "success"
        observe("zont_collection_runs_total", outcome=outcome)
        observe("zont_collection_failed_windows_total", result["failed_windows"])
        return result

    def _ensure_period(
        self, start: datetime, end: datetime, *, now: datetime | None = None,
        max_requests: int = 24, replay_recent: bool = False,
        coverage_prefix: str = "", device_ids: set[str] | None = None,
        deadline: float | None = None, monotonic: Callable[[], float] = time.monotonic,
        replay_checked_after: datetime | None = None,
    ) -> dict[str, Any]:
        reference = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        start, end = start.astimezone(UTC).replace(microsecond=0), min(end, reference).replace(microsecond=0)
        if start >= end or not 1 <= max_requests <= 100:
            raise ValueError("invalid bounded collection interval")
        requests = samples = events = unavailable = 0
        latest_timestamp: float | None = None
        errors: list[str] = []
        pending = False
        entities: dict[str, dict[str, Any]] = {}
        devices = self.db.list_devices()
        if device_ids is not None:
            devices = [device for device in devices if str(device["id"]) in device_ids]
        if not devices:
            raise ValueError("discover devices before collecting a period")
        history_types = list(self.config.zont.history_data_types)
        types = history_types + ["raw_events"]
        # Keep durable coverage independent per source, but combine aligned
        # history windows into one load_data request. This preserves recovery
        # semantics while using the batching already supported by the ZONT API.
        queues: list[tuple[str, str, int, list[tuple[datetime, datetime]]]] = []
        for device in devices:
            for data_type in types:
                hint_key = f"collection-window-seconds:{device['id']}:{data_type}"
                hint = self.db.get_app_meta(hint_key)
                window_seconds = max(1, min(1800, int(hint))) if hint else 1800
                windows = []
                coverage_type = coverage_prefix + data_type
                gaps = self.db.telemetry.missing_intervals(device["id"], coverage_type, start, end, now=reference)
                for left, right, state in gaps:
                    lo, hi = datetime.fromtimestamp(left, UTC), datetime.fromtimestamp(right, UTC)
                    if state == "unavailable":
                        self.db.telemetry.write_window(device_id=device["id"], data_type=coverage_type,
                                                       start=lo, end=hi, state="unavailable")
                        unavailable += 1
                    else:
                        windows.append((lo, hi))
                if replay_recent:
                    replay_start = max(start, reference - timedelta(minutes=self.config.scheduler.overlap_minutes))
                    if replay_start < end:
                        if replay_checked_after is None:
                            windows.append((replay_start, end))
                        else:
                            # A fixed poll slot must replay old successful coverage once,
                            # and retain that progress across bounded invocations.
                            rows = self.db.storage.execute(
                                "DECLARE $device AS Utf8; DECLARE $type AS Utf8; "
                                "DECLARE $since AS Int64; DECLARE $start AS Int64; DECLARE $end AS Int64; "
                                "SELECT started_at,ended_at FROM coverage WHERE device_id=$device AND data_type=$type "
                                "AND started_at < $end AND ended_at > $start AND checked_at >= $since "
                                "AND (state='complete' OR state='empty') ORDER BY started_at;",
                                {"$device": str(device["id"]), "$type": coverage_type,
                                 "$since": int(replay_checked_after.timestamp() * 1_000_000),
                                 "$start": int(replay_start.timestamp()), "$end": int(end.timestamp())},
                            )[0].rows
                            cursor = replay_start
                            for row in rows:
                                replay_left = datetime.fromtimestamp(row.started_at, UTC)
                                replay_right = datetime.fromtimestamp(row.ended_at, UTC)
                                if replay_left > cursor:
                                    windows.append((cursor, min(replay_left, end)))
                                cursor = max(cursor, replay_right)
                            if cursor < end:
                                windows.append((cursor, end))
                merged: list[tuple[datetime, datetime]] = []
                for lo, hi in sorted(windows):
                    if merged and lo <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                    else:
                        merged.append((lo, hi))
                queues.append((device["id"], data_type, window_seconds, merged))
        # A small per-invocation budget must still reach every source, even
        # when the first source fails on every invocation. Aligned history
        # sources consume one HTTP-request budget entry together.
        next_source = int(self.db.get_app_meta("collection-next-source") or "0") % len(queues)
        next_source_value = next_source
        ordered = [(index, queues[index]) for index in
                   [(next_source + offset) % len(queues) for offset in range(len(queues))]]
        while any(queue for _, _, _, queue in queues) and requests < max_requests:
            if deadline is not None and deadline - monotonic() < 20:
                break
            processed: set[int] = set()
            for index, (device_id, data_type, window_seconds, queue) in ordered:
                if index in processed or not queue or requests >= max_requests:
                    continue
                if deadline is not None and deadline - monotonic() < 20:
                    break
                lo, original_hi = queue.pop(0)
                hi = min(lo + timedelta(seconds=window_seconds), original_hi)
                if hi < original_hi:
                    queue.insert(0, (hi, original_hi))
                bundled = [(index, data_type, queue)]
                if data_type != "raw_events":
                    for other_index, (other_device, other_type, other_seconds, other_queue) in ordered:
                        if (other_index == index or other_index in processed or other_device != device_id
                                or other_type == "raw_events" or not other_queue):
                            continue
                        other_lo, other_original_hi = other_queue[0]
                        other_hi = min(other_lo + timedelta(seconds=other_seconds), other_original_hi)
                        if other_lo != lo or other_hi != hi:
                            continue
                        other_queue.pop(0)
                        if other_hi < other_original_hi:
                            other_queue.insert(0, (other_hi, other_original_hi))
                        bundled.append((other_index, other_type, other_queue))
                processed.update(item[0] for item in bundled)
                requests += 1
                next_source_value = (index + 1) % len(queues)
                values: list[SourceEvent] = []
                try:
                    if data_type == "raw_events":
                        raw = self.client.load_events(device_id=device_id, start=lo, end=hi)
                        values = self.client.normalize_events(device_id, raw)
                        if len(values) > 2000:
                            if (hi - lo).total_seconds() <= 1:
                                raise ValueError("source response exceeds atomic storage limit")
                            middle = lo + timedelta(seconds=int((hi - lo).total_seconds()) // 2)
                            self.db.set_app_meta(f"collection-window-seconds:{device_id}:raw_events",
                                                 str(int((middle - lo).total_seconds())))
                            queue[0:0] = [(lo, middle), (middle, hi)]
                            continue
                        self.db.telemetry.write_window(
                            device_id=device_id, data_type=coverage_prefix + data_type,
                            start=lo, end=hi, events=values,
                            state="complete" if values else "empty",
                        )
                        events += len(values)
                        continue

                    requested_types = [item[1] for item in bundled]
                    responses = self.client.load_history(
                        device_ids=[device_id], start=lo, end=hi, data_types=requested_types,
                    )
                    matching = [row for row in responses if str(row.get("device_id")) == device_id]
                    if len(matching) != 1 or matching[0].get("ok") is False:
                        raise ValueError("source did not return a successful response")
                    if matching[0].get("time_truncated") is True:
                        raise ValueError("source returned a truncated interval")
                    points, inferred = self.client.normalize_history(matching[0])
                    entities.update(inferred)
                    if len(points) > 2000:
                        if (hi - lo).total_seconds() <= 1:
                            raise ValueError("source response exceeds atomic storage limit")
                        middle = lo + timedelta(seconds=int((hi - lo).total_seconds()) // 2)
                        learned = str(int((middle - lo).total_seconds()))
                        for _source_index, source, source_queue in bundled:
                            self.db.set_app_meta(
                                f"collection-window-seconds:{device_id}:{source}", learned,
                            )
                            source_queue[0:0] = [(lo, middle), (middle, hi)]
                        continue
                    roles = {
                        key: str(self.config.entity_overrides.get(key, {}).get("role", value["role"]))
                        for key, value in entities.items()
                    }
                    if len(requested_types) == 1:
                        source = requested_types[0]
                        self.db.telemetry.write_window(
                            device_id=device_id, data_type=coverage_prefix + source,
                            start=lo, end=hi, points=points, roles=roles,
                            state="complete" if points else "empty",
                        )
                    else:
                        self.db.telemetry.write_history_window(
                            device_id=device_id, data_types=requested_types,
                            start=lo, end=hi, points=points, roles=roles,
                            coverage_prefix=coverage_prefix,
                        )
                    if points:
                        latest = max(point.timestamp_utc.timestamp() for point in points)
                        latest_timestamp = max(latest_timestamp or latest, latest)
                    samples += len(points)
                except Exception as exc:
                    for _source_index, source, _source_queue in bundled:
                        self.db.telemetry.write_window(
                            device_id=device_id, data_type=coverage_prefix + source,
                            start=lo, end=hi, state="failed",
                        )
                        errors.append(f"{source} {lo.isoformat()}: {type(exc).__name__}")
        if requests:
            self.db.set_app_meta("collection-next-source", str(next_source_value))
        pending = any(queue for _, _, _, queue in queues)
        for entity_id, entity in entities.items():
            override = self.config.entity_overrides.get(entity_id, {})
            self.db.upsert_entity(
                entity_id=entity_id, device_id=entity["device_id"], source_type=entity["source_type"],
                external_id=entity["external_id"],
                display_name=str(override.get("display_name", entity["display_name"])),
                role=str(override.get("role", entity["role"])), unit=entity["unit"],
                confidence=float(entity["confidence"]), provenance="ZONT history metadata",
            )
        if latest_timestamp is not None:
            observe("zont_telemetry_timestamp_seconds", latest_timestamp)
            observe("zont_telemetry_lag_seconds", max(0.0, reference.timestamp() - latest_timestamp))
        return {"samples": samples, "source_events": events, "requests": requests,
                "complete": not pending and not errors, "pending": pending,
                "failed_windows": len(errors), "unavailable_intervals": unavailable, "errors": errors[:10]}

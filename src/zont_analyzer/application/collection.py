"""Bounded archive completion with durable per-source coverage in YDB."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import SourceEvent, TelemetryPoint


class CollectionService:
    def __init__(self, db: Database, client: ZontReadOnlyClient, config: AppConfig) -> None:
        self.db, self.client, self.config = db, client, config

    def ensure_period(
        self, start: datetime, end: datetime, *, now: datetime | None = None,
        max_requests: int = 24, replay_recent: bool = False,
        coverage_prefix: str = "", device_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        reference = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        start, end = start.astimezone(UTC).replace(microsecond=0), min(end, reference).replace(microsecond=0)
        if start >= end or not 1 <= max_requests <= 100:
            raise ValueError("invalid bounded collection interval")
        requests = samples = events = unavailable = 0
        errors: list[str] = []
        pending = False
        entities: dict[str, dict[str, Any]] = {}
        devices = self.db.list_devices()
        if device_ids is not None:
            devices = [device for device in devices if str(device["id"]) in device_ids]
        if not devices:
            raise ValueError("discover devices before collecting a period")
        types = list(self.config.zont.history_data_types) + ["raw_events"]
        # Round-robin source queues keep a busy history stream from starving events.
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
                        windows.append((replay_start, end))
                merged: list[tuple[datetime, datetime]] = []
                for lo, hi in sorted(windows):
                    if merged and lo <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                    else:
                        merged.append((lo, hi))
                queues.append((device["id"], data_type, window_seconds, merged))
        # A small per-invocation budget must still reach every source, even
        # when the first source fails on every invocation.
        next_source = int(self.db.get_app_meta("collection-next-source") or "0") % len(queues)
        ordered = [(index, queues[index]) for index in
                   [(next_source + offset) % len(queues) for offset in range(len(queues))]]
        while any(queue for _, _, _, queue in queues) and requests < max_requests:
            for index, (device_id, data_type, window_seconds, queue) in ordered:
                if not queue or requests >= max_requests:
                    continue
                lo, hi = queue.pop(0)
                stop = min(lo + timedelta(seconds=window_seconds), hi)
                if stop < hi:
                    queue.insert(0, (stop, hi))
                    hi = stop
                requests += 1
                self.db.set_app_meta("collection-next-source", str((index + 1) % len(queues)))
                points: list[TelemetryPoint] = []
                values: list[SourceEvent] = []
                try:
                    if data_type == "raw_events":
                        raw = self.client.load_events(device_id=device_id, start=lo, end=hi)
                        values = self.client.normalize_events(device_id, raw)
                        points = []
                    else:
                        responses = self.client.load_history(device_ids=[device_id], start=lo, end=hi,
                                                             data_types=[data_type])
                        matching = [row for row in responses if str(row.get("device_id")) == device_id]
                        if len(matching) != 1 or matching[0].get("ok") is False:
                            raise ValueError("source did not return a successful response")
                        if matching[0].get("time_truncated") is True:
                            raise ValueError("source returned a truncated interval")
                        points, inferred = self.client.normalize_history(matching[0])
                        entities.update(inferred)
                        values = []
                    if len(points) + len(values) > 2000:
                        if (hi - lo).total_seconds() <= 1:
                            raise ValueError("source response exceeds atomic storage limit")
                        middle = lo + timedelta(seconds=int((hi - lo).total_seconds()) // 2)
                        # Persist the learned bound even if this invocation used its
                        # final API request; the next invocation must make progress.
                        self.db.set_app_meta(f"collection-window-seconds:{device_id}:{data_type}",
                                             str(int((middle - lo).total_seconds())))
                        queue[0:0] = [(lo, middle), (middle, hi)]
                        continue
                    roles = {key: str(self.config.entity_overrides.get(key, {}).get("role", value["role"]))
                             for key, value in entities.items()}
                    self.db.telemetry.write_window(device_id=device_id, data_type=coverage_prefix + data_type,
                                                   start=lo, end=hi,
                                                   points=points, events=values,
                                                   roles=roles, state="complete" if points or values else "empty")
                    samples += len(points)
                    events += len(values)
                except Exception as exc:
                    self.db.telemetry.write_window(device_id=device_id, data_type=coverage_prefix + data_type,
                                                   start=lo, end=hi,
                                                   state="failed")
                    errors.append(f"{data_type} {lo.isoformat()}: {type(exc).__name__}")
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
        return {"samples": samples, "source_events": events, "requests": requests,
                "complete": not pending and not errors, "pending": pending,
                "failed_windows": len(errors), "unavailable_intervals": unavailable, "errors": errors[:10]}

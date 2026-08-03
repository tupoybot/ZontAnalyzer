from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.config import AppConfig

logger = logging.getLogger(__name__)


def _object_names(devices: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}

    def walk(device_id: str, value: Any) -> None:
        if isinstance(value, dict):
            if value.get("id") is not None and value.get("name"):
                names[(device_id, str(value["id"]))] = str(value["name"])
            for child in value.values():
                walk(device_id, child)
        elif isinstance(value, list):
            for child in value:
                walk(device_id, child)

    for device in devices:
        device_id = str(device["id"])
        walk(device_id, device["raw"])
    return names


def _linked_indoor_sensor_ids(
    devices: list[dict[str, Any]],
    config_names: dict[tuple[str, str], str],
    series_rows: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    heating_circuit_ids: set[tuple[str, str]] = set()
    for series in series_rows:
        if series["source_type"] != "z3k_heating_circuit" or series["metric_key"] != "target_temp":
            continue
        device_id = str(series["device_id"])
        external_id = str(series["entity_id"]).rsplit(":", 1)[-1]
        name = config_names.get((device_id, external_id), str(series.get("display_name", "")))
        role, _confidence = infer_role("z3k_heating_circuit", external_id, "target_temp", name)
        if role == "target_temperature":
            heating_circuit_ids.add((device_id, external_id))

    linked: set[tuple[str, str]] = set()

    def walk(device_id: str, value: Any, parent_key: str | None = None) -> None:
        if isinstance(value, dict):
            if parent_key is not None and (device_id, parent_key) in heating_circuit_ids:
                target_sensor_id = value.get("target_sensor_id")
                if target_sensor_id is not None:
                    linked.add((device_id, str(target_sensor_id)))
            for key, child in value.items():
                walk(device_id, child, str(key))
        elif isinstance(value, list):
            for child in value:
                walk(device_id, child)

    for device in devices:
        walk(str(device["id"]), device["raw"])
    return linked


class IngestionService:
    def __init__(self, db: Database, client: ZontReadOnlyClient, config: AppConfig):
        self.db = db
        self.client = client
        self.config = config

    def discover(self) -> dict[str, Any]:
        devices = self.client.discover_devices()
        saved = self.db.save_devices(devices)
        return {
            "devices": saved,
            "inventory": [
                {
                    "id": str(device.get("device_id") or device.get("id")),
                    "type": (device.get("device_type") or {}).get("name")
                    if isinstance(device.get("device_type"), dict)
                    else device.get("devtype"),
                    "online": device.get("online"),
                }
                for device in devices
            ],
        }

    def _refresh_series_roles(
        self,
        devices: list[dict[str, Any]],
        inferred_entities: dict[str, dict[str, Any]],
        config_names: dict[tuple[str, str], str],
    ) -> None:
        series_rows = self.db.list_series()
        linked_indoor_sensor_ids = _linked_indoor_sensor_ids(devices, config_names, series_rows)
        for series in series_rows:
            entity_id = str(series["entity_id"])
            entity = inferred_entities.get(entity_id, {})
            external_id = str(entity.get("external_id", entity_id.rsplit(":", 1)[-1]))
            device_id = str(series["device_id"])
            name = config_names.get((device_id, external_id), str(entity.get("display_name", entity_id)))
            role, _confidence = infer_role(
                str(series["source_type"]),
                external_id,
                str(series["metric_key"]),
                name,
            )
            if series["source_type"] == "z3k_temperature" and (device_id, external_id) in linked_indoor_sensor_ids:
                role = "indoor_temperature"
            override = self.config.entity_overrides.get(entity_id, {})
            self.db.update_series_role(
                int(series["id"]),
                str(override.get("role", role)),
                str(override.get("display_name", name)),
            )

    def sync(self, *, backfill: timedelta | None = None, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        devices = self.db.list_devices()
        try:
            self.discover()
            devices = self.db.list_devices()
        except Exception:
            if not devices:
                raise
            logger.warning("ZONT discovery refresh failed; using the latest cached configuration")
        device_ids = [str(device["id"]) for device in devices]
        if not device_ids:
            return {"samples": 0, "series": 0, "windows": 0}
        data_types = self.config.zont.history_data_types
        config_names = _object_names(devices)
        inferred_entities: dict[str, dict[str, Any]] = {}
        earliest_cursor = min(
            (
                cursor
                for device_id in device_ids
                for data_type in data_types
                if (cursor := self.db.get_cursor(device_id, data_type)) is not None
            ),
            default=None,
        )
        chunk = timedelta(hours=self.config.zont.sync_chunk_hours)
        start: datetime
        if backfill is not None:
            requested_start = now - backfill
            earliest_sample = self.db.earliest_sample_time()
            can_resume_backfill = (
                earliest_cursor is not None
                and earliest_sample is not None
                and earliest_sample <= requested_start + chunk
            )
            if can_resume_backfill:
                assert earliest_cursor is not None
                start = max(
                    requested_start,
                    earliest_cursor - timedelta(minutes=self.config.scheduler.overlap_minutes),
                )
            else:
                start = requested_start
        elif earliest_cursor is not None:
            start = earliest_cursor - timedelta(minutes=self.config.scheduler.overlap_minutes)
        else:
            start = now - timedelta(days=1)
        samples = 0
        windows = 0
        failed_windows = 0
        errors: list[str] = []
        cursor = start
        total_windows = max(1, math.ceil((now - start) / chunk))
        try:
            while cursor < now:
                window_end = min(cursor + chunk, now)
                responses = self.client.load_history(
                    device_ids=device_ids,
                    start=cursor,
                    end=window_end,
                    data_types=data_types,
                )
                window_points = []
                successful_device_ids: set[str] = set()
                for response in responses:
                    response_device_id = str(response.get("device_id", ""))
                    if response.get("ok") is False:
                        reason = str(response.get("error_ui") or response.get("error") or "unknown error")
                        errors.append(f"{cursor.isoformat()} device {response_device_id}: {reason}")
                        logger.warning(
                            "ZONT history failed for device %s at %s: %s",
                            response_device_id,
                            cursor,
                            reason,
                        )
                        continue
                    if response_device_id in device_ids:
                        successful_device_ids.add(response_device_id)
                    points, entities = self.client.normalize_history(response)
                    inferred_entities.update(entities)
                    for entity_id, entity in entities.items():
                        override = self.config.entity_overrides.get(entity_id, {})
                        role = str(override.get("role", entity["role"]))
                        name = str(override.get("display_name", entity["display_name"]))
                        self.db.upsert_entity(
                            entity_id=entity_id,
                            device_id=entity["device_id"],
                            source_type=entity["source_type"],
                            external_id=entity["external_id"],
                            display_name=name,
                            role=role,
                            unit=entity["unit"],
                            confidence=float(entity["confidence"]),
                            provenance="config override" if override else "ZONT history metadata",
                        )
                    window_points.extend(points)
                role_map = {entity_id: str(entity["role"]) for entity_id, entity in inferred_entities.items()}
                for point in window_points:
                    if point.entity_id in self.config.entity_overrides:
                        role_map[point.entity_id] = str(
                            self.config.entity_overrides[point.entity_id].get("role", "unknown")
                        )
                samples += self.db.upsert_samples(window_points, role_map)
                self._refresh_series_roles(devices, inferred_entities, config_names)
                missing_device_ids = set(device_ids) - successful_device_ids
                if missing_device_ids:
                    failed_windows += 1
                    errors.append(
                        f"{cursor.isoformat()}: no successful response for devices {sorted(missing_device_ids)}"
                    )
                    break
                for device_id in successful_device_ids:
                    for data_type in data_types:
                        self.db.set_cursor(device_id, data_type, window_end)
                cursor = window_end
                windows += 1
                if windows % 10 == 0 or cursor >= now:
                    logger.info("ZONT sync progress: %d/%d windows", windows, total_windows)
        finally:
            self._refresh_series_roles(devices, inferred_entities, config_names)
        source_events = 0
        if failed_windows == 0 and cursor >= now:
            retained_start = now - backfill if backfill is not None else self.db.earliest_sample_time()
            retained_start = retained_start or now - timedelta(days=1)
            for device_id in device_ids:
                event_cursor = self.db.get_cursor(device_id, "raw_events")
                event_start = retained_start
                if event_cursor is not None:
                    event_start = max(
                        retained_start,
                        event_cursor - timedelta(minutes=self.config.scheduler.overlap_minutes),
                    )
                if event_start >= now:
                    continue
                try:
                    raw_events = self.client.load_events(device_id=device_id, start=event_start, end=now)
                    normalized_events = self.client.normalize_events(device_id, raw_events)
                    source_events += self.db.upsert_source_events(normalized_events)
                    self.db.set_cursor(device_id, "raw_events", now)
                except Exception as exc:
                    failed_windows += 1
                    errors.append(f"raw events device {device_id}: {type(exc).__name__}: {exc}")
                    logger.warning("ZONT raw event sync failed for device %s: %s", device_id, type(exc).__name__)
        return {
            "samples": samples,
            "source_events": source_events,
            "series": len(self.db.list_series()),
            "windows": windows,
            "total_windows": total_windows,
            "failed_windows": failed_windows,
            "complete": failed_windows == 0 and cursor >= now,
            "errors": errors[:10],
        }

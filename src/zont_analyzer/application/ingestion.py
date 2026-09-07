from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.config import AppConfig

logger = logging.getLogger(__name__)


# These cursors record all-time, read-only requests per ZONT data type.  They
# keep a large boiler history response from being combined with every other
# source in memory, and ensure an interrupted type is retried on the next run.
_BOOTSTRAP_COMPLETE_CURSOR = "history_bootstrap_complete"
_BOOTSTRAP_FIRST_OBSERVED_CURSOR = "history_bootstrap_first_observed"
_BOOTSTRAP_LAST_OBSERVED_CURSOR = "history_bootstrap_last_observed"
_BOOTSTRAP_OBSERVATION_META = "history_bootstrap_observation"
# This is a protocol boundary rather than an assumed archive-retention date.
# mintime=0 requests all available ZONT history without a home-specific cutoff.
_UNIVERSAL_HISTORY_START = datetime.fromtimestamp(0, UTC)


def _bootstrap_cursor(kind: str, data_type: str) -> str:
    return f"{kind}:{data_type}"


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


@dataclass(frozen=True)
class SensorLink:
    device_id: str
    circuit_external_id: str
    sensor_external_id: str
    confidence: float
    provenance: str


def _is_dhw_name(name: str) -> bool:
    normalized = name.casefold()
    return any(term in normalized for term in ("гвс", "dhw", "hot water", "бойлер", "boiler tank"))


def heating_circuit_sensor_links(
    devices: list[dict[str, Any]],
    config_names: dict[tuple[str, str], str],
    series_rows: list[dict[str, Any]],
) -> list[SensorLink]:
    """Resolve space-heating circuit -> indoor sensor links, strongest source first."""

    links: dict[tuple[str, str], SensorLink] = {}
    heating_circuit_ids: set[tuple[str, str]] = set()
    raw_by_device: dict[str, dict[str, Any]] = {}
    for device in devices:
        device_id = str(device["id"])
        raw = device["raw"]
        raw_by_device[device_id] = raw
        z3k_config = raw.get("z3k_config")
        circuits = z3k_config.get("heating_circuits", []) if isinstance(z3k_config, dict) else []
        if not isinstance(circuits, list):
            continue
        for circuit in circuits:
            if not isinstance(circuit, dict) or circuit.get("id") is None:
                continue
            circuit_id = str(circuit["id"])
            name = str(circuit.get("name") or config_names.get((device_id, circuit_id), ""))
            if _is_dhw_name(name):
                continue
            heating_circuit_ids.add((device_id, circuit_id))
            sensor_id = circuit.get("air_temp_sensor")
            if sensor_id is not None:
                links[(device_id, circuit_id)] = SensorLink(
                    device_id=device_id,
                    circuit_external_id=circuit_id,
                    sensor_external_id=str(sensor_id),
                    confidence=1.0,
                    provenance="zont_config.heating_circuits[].air_temp_sensor",
                )

    # History metadata identifies a circuit only as a fallback. It is never used
    # to override an explicit circuit configuration link.
    for series in series_rows:
        if series["source_type"] != "z3k_heating_circuit" or series["metric_key"] != "target_temp":
            continue
        device_id = str(series["device_id"])
        external_id = str(series["entity_id"]).rsplit(":", 1)[-1]
        name = config_names.get((device_id, external_id), str(series.get("display_name", "")))
        role, _confidence = infer_role("z3k_heating_circuit", external_id, "target_temp", name)
        if role == "target_temperature":
            heating_circuit_ids.add((device_id, external_id))

    for device_id, circuit_id in heating_circuit_ids:
        if (device_id, circuit_id) in links:
            continue
        raw = raw_by_device.get(device_id, {})
        io = raw.get("io")
        z3k_state = io.get("z3k-state") if isinstance(io, dict) else None
        state = z3k_state.get(circuit_id) if isinstance(z3k_state, dict) else None
        sensor_id = state.get("target_sensor_id") if isinstance(state, dict) else None
        if sensor_id is not None:
            links[(device_id, circuit_id)] = SensorLink(
                device_id=device_id,
                circuit_external_id=circuit_id,
                sensor_external_id=str(sensor_id),
                confidence=0.9,
                provenance="zont_io.z3k-state.target_sensor_id",
            )
    return sorted(links.values(), key=lambda item: (item.device_id, item.circuit_external_id))


def _linked_indoor_sensor_ids(
    devices: list[dict[str, Any]],
    config_names: dict[tuple[str, str], str],
    series_rows: list[dict[str, Any]],
) -> set[tuple[str, str]]:
    return {
        (link.device_id, link.sensor_external_id)
        for link in heating_circuit_sensor_links(devices, config_names, series_rows)
    }


def _is_sensor_temperature_series(series: dict[str, Any]) -> bool:
    source_type = str(series["source_type"])
    metric_key = str(series["metric_key"])
    return (
        source_type == "z3k_radio_sensor" and metric_key == "temperature"
    ) or source_type == "z3k_temperature" or (
        source_type == "temperature" and metric_key in {"temperature", source_type}
    )


def _series_origin(source_type: str, metric_key: str, role: str) -> str:
    if source_type == "z3k_boiler_adapter" and metric_key == "rwt":
        return "boiler_reported_rwt"
    if role == "return_temperature":
        return "external_sensor"
    if source_type == "z3k_radio_sensor":
        return "radio_sensor"
    if source_type == "z3k_temperature":
        return "wired_temperature_sensor"
    return source_type


class IngestionService:
    def __init__(self, db: Database, client: ZontReadOnlyClient, config: AppConfig):
        self.db = db
        self.client = client
        self.config = config

    def discover(self) -> dict[str, Any]:
        devices = self.client.discover_devices()
        saved = self.db.save_devices(devices)
        from zont_analyzer.application.timezone import apply_device_timezone
        apply_device_timezone(self.db, self.config)
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
        links = heating_circuit_sensor_links(devices, config_names, series_rows)
        links_by_sensor = {(item.device_id, item.sensor_external_id): item for item in links}
        for series in series_rows:
            entity_id = str(series["entity_id"])
            entity = inferred_entities.get(entity_id, {})
            external_id = str(entity.get("external_id", entity_id.rsplit(":", 1)[-1]))
            device_id = str(series["device_id"])
            name = config_names.get((device_id, external_id), str(entity.get("display_name", entity_id)))
            role, confidence = infer_role(
                str(series["source_type"]),
                external_id,
                str(series["metric_key"]),
                name,
            )
            provenance = "display_name heuristic" if confidence < 0.95 else "history source semantics"
            link = links_by_sensor.get((device_id, external_id))
            if link is not None and _is_sensor_temperature_series(series):
                role = "control_indoor_temperature"
                confidence = link.confidence
                provenance = link.provenance
            override = self.config.entity_overrides.get(entity_id, {})
            override_role = override.get("role")
            if override_role is not None:
                configured_role = str(override_role)
                temperature_roles = {
                    "control_indoor_temperature",
                    "room_temperature",
                    "technical_temperature",
                    "outdoor_temperature",
                    "flow_temperature",
                    "return_temperature",
                    "dhw_temperature",
                }
                is_temperature_measurement = _is_sensor_temperature_series(series) or series.get("unit") == "°C"
                if configured_role not in temperature_roles or is_temperature_measurement:
                    role = configured_role
                    confidence = 1.0
                    provenance = "config.entity_overrides"
            self.db.update_series_role(
                int(series["id"]),
                role,
                str(override.get("display_name", name)),
                confidence=confidence,
                provenance=provenance,
                origin=_series_origin(str(series["source_type"]), str(series["metric_key"]), role),
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
        samples = 0

        def ingest_window(
            *, requested_device_ids: list[str], start: datetime, end: datetime, requested_data_types: list[str]
        ) -> tuple[set[str], dict[str, tuple[datetime | None, datetime | None]], dict[str, bool | None]]:
            """Load one read-only history request and return successful devices and points."""

            nonlocal samples
            try:
                responses = self.client.load_history(
                    device_ids=requested_device_ids,
                    start=start,
                    end=end,
                    data_types=requested_data_types,
                )
            except Exception as exc:
                errors.append(f"{start.isoformat()} history request: {type(exc).__name__}: {exc}")
                logger.warning("ZONT history request failed at %s: %s", start, type(exc).__name__)
                return set(), {}, {}
            successful_device_ids: set[str] = set()
            bounds_by_device: dict[str, tuple[datetime | None, datetime | None]] = {}
            truncation_by_device: dict[str, bool | None] = {}
            for response in responses:
                response_device_id = str(response.get("device_id", ""))
                if response.get("ok") is False:
                    reason = str(response.get("error_ui") or response.get("error") or "unknown error")
                    errors.append(f"{start.isoformat()} device {response_device_id}: {reason}")
                    logger.warning(
                        "ZONT history failed for device %s at %s: %s",
                        response_device_id,
                        start,
                        reason,
                    )
                    continue
                if response_device_id not in requested_device_ids:
                    continue
                successful_device_ids.add(response_device_id)
                value = response.get("time_truncated")
                truncation_by_device[response_device_id] = value if isinstance(value, bool) else None
                iterator = getattr(self.client, "iter_normalized_history", None)
                if iterator is None:
                    points, entities = self.client.normalize_history(response)
                else:
                    points, entities = iterator(response)
                first_observed: datetime | None = None
                last_observed: datetime | None = None
                batch: list[Any] = []
                roles: dict[str, str] = {}
                for point in points:
                    if not start <= point.timestamp_utc <= end:
                        continue
                    first_observed = (
                        point.timestamp_utc
                        if first_observed is None or point.timestamp_utc < first_observed
                        else first_observed
                    )
                    last_observed = (
                        point.timestamp_utc
                        if last_observed is None or point.timestamp_utc > last_observed
                        else last_observed
                    )
                    batch.append(point)
                    entity = entities.get(point.entity_id, {})
                    override = self.config.entity_overrides.get(point.entity_id, {})
                    roles[point.entity_id] = str(override.get("role", entity.get("role", "unknown")))
                    if len(batch) == 2000:
                        samples += self.db.upsert_samples(batch, roles)
                        batch.clear()
                        roles.clear()
                if batch:
                    samples += self.db.upsert_samples(batch, roles)
                bounds_by_device[response_device_id] = (first_observed, last_observed)
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
            self._refresh_series_roles(devices, inferred_entities, config_names)
            return successful_device_ids, bounds_by_device, truncation_by_device

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
        bootstrap_requests = [
            (device_id, data_type)
            for device_id in device_ids
            for data_type in data_types
            if self.db.get_cursor(device_id, _bootstrap_cursor(_BOOTSTRAP_COMPLETE_CURSOR, data_type)) is None
        ]
        bootstrap = backfill is None and bool(bootstrap_requests)
        start: datetime
        if backfill is not None:
            # An explicit backfill is a request to replay the whole selected
            # interval.  The regular cursor may already point at the present
            # even when a newly enabled history type has no older samples.
            # Reusing that cursor here would silently reduce a historical
            # backfill to the normal overlap window.  Sample upserts are
            # idempotent, so replaying completed windows is safe on retry.
            start = now - backfill
        elif bootstrap:
            # A blank local store has no trustworthy date from which to infer
            # archive retention.  Ask for the whole possible interval.  ZONT
            # may return ``time_truncated`` when the account's archive starts
            # later.  That is an observed archive boundary, not a reason to
            # invent an earlier retention date or to page into a gap.
            start = _UNIVERSAL_HISTORY_START
        elif earliest_cursor is not None:
            start = earliest_cursor - timedelta(minutes=self.config.scheduler.overlap_minutes)
        else:
            start = now - timedelta(days=1)
        windows = 0
        failed_windows = 0
        errors: list[str] = []
        cursor = start
        total_windows = len(bootstrap_requests) if bootstrap else max(1, math.ceil((now - start) / chunk))
        try:
            if bootstrap:
                while bootstrap_requests:
                    device_id, data_type = bootstrap_requests[0]
                    successful_device_ids, bounds_by_device, truncation_by_device = ingest_window(
                        requested_device_ids=[device_id],
                        start=_UNIVERSAL_HISTORY_START,
                        end=now,
                        requested_data_types=[data_type],
                    )
                    windows += 1
                    if device_id not in successful_device_ids:
                        failed_windows += 1
                        errors.append(f"bootstrap device {device_id} data type {data_type}: no successful response")
                        break
                    first_observed, last_observed = bounds_by_device[device_id]
                    if first_observed is not None and last_observed is not None:
                        self.db.set_cursor(
                            device_id,
                            _bootstrap_cursor(_BOOTSTRAP_FIRST_OBSERVED_CURSOR, data_type),
                            first_observed,
                        )
                        self.db.set_cursor(
                            device_id,
                            _bootstrap_cursor(_BOOTSTRAP_LAST_OBSERVED_CURSOR, data_type),
                            last_observed,
                        )
                    self.db.set_cursor(device_id, _bootstrap_cursor(_BOOTSTRAP_COMPLETE_CURSOR, data_type), now)
                    self.db.set_cursor(device_id, data_type, now)
                    self.db.set_app_meta(
                        _bootstrap_cursor(_BOOTSTRAP_OBSERVATION_META, f"{device_id}:{data_type}"),
                        json.dumps(
                            {
                                "requested_start": _UNIVERSAL_HISTORY_START.isoformat(),
                                "requested_end": now.isoformat(),
                                "time_truncated": truncation_by_device[device_id],
                            },
                            sort_keys=True,
                        ),
                    )
                    bootstrap_requests.pop(0)
                    logger.info("ZONT bootstrap completed for device %s data type %s", device_id, data_type)
                cursor = now if not bootstrap_requests and failed_windows == 0 else cursor
            else:
                while cursor < now:
                    window_end = min(cursor + chunk, now)
                    successful_device_ids, _bounds_by_device, _truncation_by_device = ingest_window(
                        requested_device_ids=device_ids,
                        start=cursor,
                        end=window_end,
                        requested_data_types=data_types,
                    )
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
        earliest_observed = self.db.earliest_sample_time()
        latest_observed = self.db.latest_sample_time()
        bootstrap_ranges: dict[str, dict[str, Any]] = {}
        for device_id in device_ids:
            for data_type in data_types:
                first = self.db.get_cursor(
                    device_id, _bootstrap_cursor(_BOOTSTRAP_FIRST_OBSERVED_CURSOR, data_type)
                )
                last = self.db.get_cursor(device_id, _bootstrap_cursor(_BOOTSTRAP_LAST_OBSERVED_CURSOR, data_type))
                complete = self.db.get_cursor(device_id, _bootstrap_cursor(_BOOTSTRAP_COMPLETE_CURSOR, data_type))
                observation_json = self.db.get_app_meta(
                    _bootstrap_cursor(_BOOTSTRAP_OBSERVATION_META, f"{device_id}:{data_type}")
                )
                try:
                    observation = json.loads(observation_json) if observation_json else {}
                except json.JSONDecodeError:
                    observation = {}
                if complete or first or last or observation:
                    bootstrap_ranges[f"{device_id}:{data_type}"] = {
                        "requested_start": observation.get("requested_start"),
                        "requested_end": observation.get("requested_end"),
                        "time_truncated": observation.get("time_truncated"),
                        "first_observed": first.isoformat() if first else None,
                        "last_observed": last.isoformat() if last else None,
                    }
        return {
            "samples": samples,
            "source_events": source_events,
            "series": len(self.db.list_series()),
            "windows": windows,
            "total_windows": total_windows,
            "failed_windows": failed_windows,
            "complete": failed_windows == 0 and cursor >= now,
            "errors": errors[:10],
            "history_range": {
                "first_observed": earliest_observed.isoformat() if earliest_observed else None,
                "last_observed": latest_observed.isoformat() if latest_observed else None,
            },
            "bootstrap_ranges": bootstrap_ranges,
        }

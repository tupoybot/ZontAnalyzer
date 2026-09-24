from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import SourceEvent

logger = logging.getLogger(__name__)


_CONNECTION_RECOVERY_META = "connection_recovery"


def _connection_recovery_key(device_id: str) -> str:
    return f"{_CONNECTION_RECOVERY_META}:{device_id}"


def _parse_recovery_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _load_connection_recovery(db: Database, device_id: str) -> dict[str, datetime | None]:
    try:
        raw = json.loads(db.get_app_meta(_connection_recovery_key(device_id)) or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        name: _parse_recovery_time(raw.get(name))
        for name in ("open_disconnect_at", "pending_replay_start", "pending_restore_at", "handled_restore_at")
    }


def _save_connection_recovery(
    db: Database, device_id: str, state: dict[str, datetime | None]
) -> None:
    db.set_app_meta(
        _connection_recovery_key(device_id),
        json.dumps(
            {name: value.isoformat() for name, value in state.items() if value is not None},
            sort_keys=True,
        ),
    )


def _record_connection_events(
    state: dict[str, datetime | None], events: list[SourceEvent]
) -> dict[str, datetime | None]:
    handled = state.get("handled_restore_at")
    pending_start = state.get("pending_replay_start")
    pending_restore = state.get("pending_restore_at")
    open_disconnect = state.get("open_disconnect_at")
    for event in sorted(
        events,
        key=lambda item: (
            item.timestamp_utc,
            item.event_type != "disconnected",
            item.id,
        ),
    ):
        timestamp = event.timestamp_utc.astimezone(UTC)
        if event.event_type == "disconnected":
            if handled is not None and timestamp <= handled:
                continue
            if pending_restore is not None and timestamp <= pending_restore:
                continue
            open_disconnect = (
                timestamp if open_disconnect is None else min(open_disconnect, timestamp)
            )
        elif event.event_type in {"connected", "reconnected"}:
            if handled is not None and timestamp <= handled:
                continue
            if pending_restore is not None and timestamp <= pending_restore:
                continue
            if open_disconnect is None or timestamp < open_disconnect:
                continue
            pending_start = (
                open_disconnect if pending_start is None else min(pending_start, open_disconnect)
            )
            pending_restore = timestamp
            open_disconnect = None
    return {
        "open_disconnect_at": open_disconnect,
        "pending_replay_start": pending_start,
        "pending_restore_at": pending_restore,
        "handled_restore_at": handled,
    }


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

    def sync(
        self, *, backfill: timedelta | None = None, now: datetime | None = None,
        max_requests: int = 24,
    ) -> dict[str, Any]:
        from zont_analyzer.application.collection import CollectionService

        reference = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        devices = self.db.list_devices()
        try:
            self.discover()
            devices = self.db.list_devices()
        except Exception:
            if not devices:
                raise
            logger.warning("ZONT discovery refresh failed; using the latest cached configuration")
        overlap = timedelta(minutes=self.config.scheduler.overlap_minutes)
        if backfill is not None:
            start = reference - backfill
        else:
            cursors = [self.db.get_cursor(str(device["id"]), data_type) or reference - timedelta(days=1)
                       for device in devices for data_type in [*self.config.zont.history_data_types, "raw_events"]]
            start = min(cursors) - overlap if cursors else reference - timedelta(days=1)
            for device in devices:
                state = _load_connection_recovery(self.db, str(device["id"]))
                if state["pending_replay_start"] is not None:
                    start = min(start, state["pending_replay_start"] - overlap)
        collector = CollectionService(self.db, self.client, self.config)
        result: dict[str, Any] = {"samples": 0, "source_events": 0, "requests": 0,
                                  "complete": True, "pending": False, "failed_windows": 0,
                                  "unavailable_intervals": 0, "errors": []}

        def combine(part: dict[str, Any]) -> None:
            for name in ("samples", "source_events", "requests", "failed_windows", "unavailable_intervals"):
                result[name] += part[name]
            result["complete"] = result["complete"] and part["complete"]
            result["pending"] = result["pending"] or part["pending"]
            result["errors"] = (result["errors"] + part["errors"])[:10]

        # A reconnect requires a fresh read even if ordinary archive coverage
        # previously recorded a successful response with incomplete buffered data.
        # Separate durable coverage makes replay resumable without destroying it.
        for device in devices:
            device_id = str(device["id"])
            state = _load_connection_recovery(self.db, device_id)
            restored, replay_start = state["pending_restore_at"], state["pending_replay_start"]
            if restored is None or replay_start is None:
                continue
            if result["requests"] >= max_requests:
                result["complete"], result["pending"] = False, True
                break
            part = collector.ensure_period(
                replay_start - overlap, min(reference, restored + overlap), now=reference,
                max_requests=max_requests - result["requests"], device_ids={device_id},
                coverage_prefix=f"recovery:{int(restored.timestamp())}:",
            )
            combine(part)
            if part["complete"] and self.db.fetch_device_sample_timestamps(
                device_id, restored, reference + timedelta(seconds=1),
            ):
                state["handled_restore_at"] = restored
                state["pending_restore_at"] = None
                state["pending_replay_start"] = None
                _save_connection_recovery(self.db, device_id, state)
            else:
                result["complete"], result["pending"] = False, True
        if result["requests"] < max_requests:
            combine(collector.ensure_period(
                start, reference, now=reference, max_requests=max_requests - result["requests"],
                replay_recent=backfill is None,
            ))
        else:
            result["complete"], result["pending"] = False, True
        self._refresh_series_roles(devices, {}, _object_names(devices))
        for device in devices:
            device_id = str(device["id"])
            state = _load_connection_recovery(self.db, device_id)
            events = [event for event in self.db.list_source_events(start, reference)
                      if event.device_id == device_id]
            state = _record_connection_events(state, events)
            if state["pending_restore_at"] is not None:
                result["complete"], result["pending"] = False, True
            _save_connection_recovery(self.db, device_id, state)
        first, last = self.db.earliest_sample_time(), self.db.latest_sample_time()
        return {**result, "series": len(self.db.list_series()), "windows": result["requests"],
                "history_range": {"first_observed": first.isoformat() if first else None,
                                  "last_observed": last.isoformat() if last else None}}

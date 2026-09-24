"""YDB ingestion contract: bounded coverage, independent sources, and role links."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from tests.ydb_support import make_database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.application.analysis import _select_control_temperature_series
from zont_analyzer.application.ingestion import (
    IngestionService,
    _linked_indoor_sensor_ids,
    _object_names,
    _record_connection_events,
)
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import SourceEvent, TelemetryPoint

CONTRACT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "zont_contract"
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


class RecordingClient:
    def __init__(self) -> None:
        self.history_calls: list[dict[str, Any]] = []
        self.event_calls: list[dict[str, Any]] = []
        self.fail_history_once = False
        self.late_point = False

    def discover_devices(self) -> list[dict[str, Any]]:
        return [{"device_id": 1, "name": "fixture"}]

    def load_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.history_calls.append(kwargs)
        if self.fail_history_once:
            self.fail_history_once = False
            raise RuntimeError("temporary source error")
        return [{"device_id": 1, "ok": True, "start": kwargs["start"], "end": kwargs["end"]}]

    def normalize_history(self, response: dict[str, Any]) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
        at = NOW - timedelta(minutes=90) if self.late_point else response["start"] + timedelta(minutes=1)
        if not response["start"] <= at < response["end"]:
            return [], {}
        return [TelemetryPoint(device_id="1", source_type="temperature", entity_id="zont:1:temperature:1",
                               metric_key="temperature", timestamp_utc=at, value_num=21, unit="°C")], {}

    def load_events(self, **kwargs: Any) -> list[list[Any]]:
        self.event_calls.append(kwargs)
        at = NOW - timedelta(minutes=20)
        return [["power", int(at.timestamp()), "PowerOn"]] if kwargs["start"] <= at < kwargs["end"] else []

    def normalize_events(self, device_id: str, rows: list[list[Any]]) -> list[SourceEvent]:
        return ZontReadOnlyClient.normalize_events(device_id, rows)


def _service(tmp_path: Path) -> tuple[Any, IngestionService, RecordingClient]:
    db = make_database(tmp_path)
    client = RecordingClient()
    config = AppConfig()
    config.zont.history_data_types = ["temperature"]
    return db, IngestionService(db, cast(ZontReadOnlyClient, client), config), client


def _seed_points(db: Any, points: list[TelemetryPoint]) -> None:
    start = min(point.timestamp_utc for point in points) - timedelta(seconds=1)
    end = max(point.timestamp_utc for point in points) + timedelta(seconds=1)
    db.telemetry.write_window(device_id="1", data_type="fixture", start=start, end=end,
                              points=points, state="complete")


def test_same_second_connection_events_apply_disconnect_before_restore() -> None:
    timestamp = datetime(2026, 8, 25, tzinfo=UTC)
    events = [SourceEvent(id="a-restore", device_id="1", event_type="connected", timestamp_utc=timestamp),
              SourceEvent(id="z-loss", device_id="1", event_type="disconnected", timestamp_utc=timestamp)]
    state = _record_connection_events({}, events)
    assert state["open_disconnect_at"] is None
    assert state["pending_replay_start"] == timestamp
    assert state["pending_restore_at"] == timestamp


def test_explicit_backfill_uses_requested_interval_and_bounded_calls(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    db.save_devices(client.discover_devices())
    db.telemetry.write_window(device_id="1", data_type="temperature", start=NOW - timedelta(hours=1),
                              end=NOW, state="empty")
    result = service.sync(backfill=timedelta(days=90), now=NOW, max_requests=2)
    assert result["requests"] == 2 and not result["complete"]
    assert len(client.history_calls) + len(client.event_calls) == 2
    assert client.history_calls[0]["start"] == NOW - timedelta(days=90)
    assert all(call["end"] - call["start"] <= timedelta(minutes=30)
               for call in client.history_calls + client.event_calls)
    before = len(client.history_calls) + len(client.event_calls)
    service.sync(backfill=timedelta(days=90), now=NOW, max_requests=2)
    assert len(client.history_calls) + len(client.event_calls) == before + 2
    assert all(call["start"] >= NOW - timedelta(days=90)
               for call in client.history_calls + client.event_calls)


def test_history_failure_keeps_event_coverage_and_retries_without_duplicates(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    client.fail_history_once = True
    first = service.sync(backfill=timedelta(minutes=30), now=NOW, max_requests=2)
    assert not first["complete"] and first["failed_windows"] == 1
    assert first["source_events"] == 1
    assert db.get_cursor("1", "temperature") is None
    assert db.get_cursor("1", "raw_events") == NOW
    second = service.sync(backfill=timedelta(minutes=30), now=NOW, max_requests=2)
    assert second["complete"]
    assert len(client.history_calls) == 2 and len(client.event_calls) == 1
    assert len(db.list_source_events(NOW - timedelta(hours=1), NOW)) == 1
    assert len(db.fetch_samples(db.list_series()[0]["id"], NOW - timedelta(hours=1), NOW)) == 1


def test_normal_sync_replays_late_history_and_events_with_two_hour_overlap(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    db.save_devices(client.discover_devices())
    start = NOW - timedelta(hours=2)
    for data_type in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="1", data_type=data_type,
                                  start=NOW - timedelta(days=1), end=start, state="empty")
    first = service.sync(backfill=timedelta(hours=2), now=NOW, max_requests=8)
    assert first["complete"]
    client.late_point = True
    second = service.sync(now=NOW + timedelta(minutes=10), max_requests=16)
    assert second["complete"]
    assert any(call["start"] <= NOW - timedelta(minutes=90) for call in client.history_calls[4:])
    samples = db.fetch_samples(db.list_series()[0]["id"], NOW - timedelta(hours=2), NOW)
    assert NOW - timedelta(minutes=90) in {at for at, _ in samples}
    assert len(samples) >= 5
    assert len(db.list_source_events(start, NOW)) == 1


def test_reconnect_state_requires_recovered_sample_and_survives_retry(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    disconnected = NOW - timedelta(hours=5)
    restored = NOW - timedelta(hours=1)
    db.save_devices(client.discover_devices())
    db.set_app_meta("connection_recovery:1", json.dumps({
        "pending_replay_start": disconnected.isoformat(),
        "pending_restore_at": restored.isoformat(),
    }))
    client.fail_history_once = True
    failed = service.sync(now=NOW, max_requests=2)
    assert not failed["complete"]
    pending = json.loads(db.get_app_meta("connection_recovery:1") or "{}")
    assert pending["pending_replay_start"] == disconnected.isoformat()
    assert min(call["start"] for call in client.history_calls) <= disconnected - timedelta(hours=2)
    # A later successful window with post-restore evidence closes the replay state.
    db.telemetry.write_window(device_id="1", data_type="temperature", start=restored,
                              end=restored + timedelta(minutes=30),
                              points=[TelemetryPoint(device_id="1", source_type="temperature",
                                                     entity_id="zont:1:temperature:1", metric_key="temperature",
                                                     timestamp_utc=restored + timedelta(minutes=1),
                                                     value_num=21, unit="°C")], state="complete")
    assert db.fetch_device_sample_timestamps("1", restored, NOW)
    replay_start = disconnected - timedelta(hours=2)
    for data_type in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="1", data_type=data_type,
                                  start=replay_start + timedelta(minutes=30), end=NOW, state="empty")
    completed = service.sync(now=NOW, max_requests=16)
    for _ in range(5):
        if completed["complete"]:
            break
        completed = service.sync(now=NOW, max_requests=16)
    assert completed["complete"]
    assert len(client.history_calls) >= 2
    assert client.history_calls[1]["start"] == replay_start
    recovery = json.loads(db.get_app_meta("connection_recovery:1") or "{}")
    assert recovery == {"handled_restore_at": restored.isoformat()}


def test_reconnect_rereads_completed_history_to_capture_late_buffered_sample(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    disconnected = NOW - timedelta(hours=5)
    restored = NOW - timedelta(hours=1)
    buffered = disconnected + timedelta(hours=1)
    replay_start = disconnected - timedelta(hours=2)
    db.save_devices(client.discover_devices())
    # These windows were already checked while the controller was offline.
    for data_type in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="1", data_type=data_type,
                                  start=replay_start, end=NOW, state="empty")
    db.set_app_meta("connection_recovery:1", json.dumps({
        "pending_replay_start": disconnected.isoformat(),
        "pending_restore_at": restored.isoformat(),
    }))

    def buffered_history(response: dict[str, Any]) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
        if not response["start"] <= buffered < response["end"]:
            return [], {}
        return [TelemetryPoint(device_id="1", source_type="temperature", entity_id="zont:1:temperature:1",
                               metric_key="temperature", timestamp_utc=buffered,
                               value_num=20, unit="°C")], {}

    client.normalize_history = buffered_history  # type: ignore[method-assign]
    first = service.sync(now=NOW, max_requests=24)
    assert first["requests"] <= 24
    for _ in range(3):
        if any(call["start"] <= buffered < call["end"] for call in client.history_calls):
            break
        service.sync(now=NOW, max_requests=24)
    assert any(call["start"] <= buffered < call["end"] for call in client.history_calls)
    assert buffered in {at for at, _ in db.fetch_samples(db.list_series()[0]["id"], replay_start, NOW)}
    assert json.loads(db.get_app_meta("connection_recovery:1") or "{}")["pending_replay_start"]


def test_new_restore_waits_for_separate_replay_before_marking_handled(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    disconnected = NOW - timedelta(minutes=100)
    restored = NOW - timedelta(minutes=50)
    db.save_devices(client.discover_devices())
    for data_type in ("temperature", "raw_events"):
        db.telemetry.write_window(device_id="1", data_type=data_type,
                                  start=NOW - timedelta(hours=2), end=NOW, state="empty")

    def events(**kwargs: Any) -> list[list[Any]]:
        client.event_calls.append(kwargs)
        return [[label, int(at.timestamp()), event_type]
                for label, at, event_type in (("loss", disconnected, "disconnected"),
                                              ("restore", restored, "reconnected"))
                if kwargs["start"] <= at < kwargs["end"]]

    client.load_events = events  # type: ignore[method-assign]
    first = service.sync(now=NOW, max_requests=8)
    state = json.loads(db.get_app_meta("connection_recovery:1") or "{}")
    assert not first["complete"]
    assert state["pending_replay_start"] == disconnected.isoformat()
    assert "handled_restore_at" not in state


def test_empty_bootstrap_has_no_invented_observation_bounds(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    client.normalize_history = lambda _response: ([], {})  # type: ignore[method-assign]
    result = service.sync(backfill=timedelta(minutes=30), now=NOW, max_requests=2)
    assert result["complete"]
    assert result["history_range"] == {"first_observed": None, "last_observed": None}
    assert db.get_cursor("1", "temperature") == NOW


def test_oversized_source_response_is_split_before_storage(tmp_path: Path) -> None:
    db, service, client = _service(tmp_path)
    start = NOW - timedelta(minutes=30)

    def too_many(_response: dict[str, Any]) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
        return [TelemetryPoint(device_id="1", source_type="temperature",
                               entity_id=f"zont:1:temperature:{entity}", metric_key="temperature",
                               timestamp_utc=start + timedelta(seconds=second),
                               value_num=20, unit="°C")
                for entity in (1, 2) for second in range(1800)], {}

    client.normalize_history = too_many  # type: ignore[method-assign]
    first = service.sync(backfill=timedelta(minutes=30), now=NOW, max_requests=1)
    assert not first["complete"] and first["requests"] == 1
    assert first["samples"] == 0
    assert db.get_app_meta("collection-window-seconds:1:temperature") == "900"
    assert db.list_series() == []


def test_heating_circuit_target_sensor_is_indoor_regardless_of_room_name() -> None:
    devices = [
        {
            "id": "1",
            "raw": {
                "io": {
                    "z3k-state": {
                        "200": {"target_sensor_id": 100},
                        "300": {"target_sensor_id": 101},
                    }
                }
            },
        }
    ]
    names = {("1", "200"): "Отопление", ("1", "300"): "ГВС", ("1", "100"): "Любое имя комнаты"}
    series = [
        {
            "device_id": "1",
            "source_type": "z3k_heating_circuit",
            "entity_id": "zont:1:z3k_heating_circuit:200",
            "metric_key": "target_temp",
            "display_name": "Отопление",
        },
        {
            "device_id": "1",
            "source_type": "z3k_heating_circuit",
            "entity_id": "zont:1:z3k_heating_circuit:300",
            "metric_key": "target_temp",
            "display_name": "ГВС",
        },
    ]

    assert _linked_indoor_sensor_ids(devices, names, series) == {("1", "100")}


def test_anonymized_live_contract_reproduces_sensor_links_and_return_identity() -> None:
    devices_payload = json.loads((CONTRACT_FIXTURES / "devices.json").read_text(encoding="utf-8"))
    history_payload = json.loads((CONTRACT_FIXTURES / "load_data.json").read_text(encoding="utf-8"))
    raw_device = devices_payload["devices"][0]
    devices = [{"id": str(raw_device["device_id"]), "raw": raw_device}]
    points, entities = ZontReadOnlyClient.normalize_history(history_payload["responses"][0])
    names = _object_names(devices)
    series = [
        {
            "device_id": point.device_id,
            "source_type": point.source_type,
            "entity_id": point.entity_id,
            "metric_key": point.metric_key,
            "display_name": entities[point.entity_id]["display_name"],
        }
        for point in points
    ]

    assert _linked_indoor_sensor_ids(devices, names, series) == {("100001", "30001")}
    assert names[("100001", "30002")] == "Внешний датчик обратки"
    assert any(
        point.source_type == "z3k_temperature" and point.entity_id.endswith(":30002")
        for point in points
    )
    assert infer_role(
        "z3k_temperature",
        "30002",
        "z3k_temperature",
        names[("100001", "30002")],
    ) == ("return_temperature", 0.85)


@pytest.mark.parametrize("sensor_order", [["101", "102", "103", "104"], ["104", "103", "102", "101"]])
def test_sensor_roles_follow_config_link_not_import_order(tmp_path: Path, sensor_order: list[str]) -> None:
    raw_device = {
        "device_id": 1,
        "z3k_config": {
            "heating_circuits": [{"id": 200, "name": "Отопление", "air_temp_sensor": 101}],
            # The misleading name proves that the explicit link outranks display-name heuristics.
            "radiosensors": [{"id": 101, "name": "Котельная"}],
            "wired_temperature_sensors": [
                {"id": 102, "name": "Котельная"},
                {"id": 103, "name": "Спальня"},
                {"id": 104, "name": "Датчик 104"},
                {"id": 105, "name": "Обратка"},
            ],
        },
        # This stale/conflicting current-state link must not replace air_temp_sensor.
        "io": {"z3k-state": {"200": {"target_sensor_id": 102}}},
    }
    devices = [{"id": "1", "raw": raw_device}]
    points_by_sensor = {
        "101": [
            TelemetryPoint(
                device_id="1",
                source_type="z3k_radio_sensor",
                entity_id="zont:1:z3k_radio_sensor:101",
                metric_key="temperature",
                timestamp_utc=datetime(2026, 8, 24, tzinfo=UTC),
                value_num=22,
                unit="°C",
            ),
            TelemetryPoint(
                device_id="1",
                source_type="z3k_radio_sensor",
                entity_id="zont:1:z3k_radio_sensor:101",
                metric_key="humidity",
                timestamp_utc=datetime(2026, 8, 24, tzinfo=UTC),
                value_num=55,
                unit="%",
            ),
        ],
        **{
            sensor_id: [
                TelemetryPoint(
                    device_id="1",
                    source_type="z3k_temperature",
                    entity_id=f"zont:1:z3k_temperature:{sensor_id}",
                    metric_key="z3k_temperature",
                    timestamp_utc=datetime(2026, 8, 24, tzinfo=UTC),
                    value_num=temperature,
                    unit="°C",
                )
            ]
            for sensor_id, temperature in (("102", 30), ("103", 21), ("104", 20))
        },
    }
    ordered_points = [point for sensor_id in sensor_order for point in points_by_sensor[sensor_id]]
    ordered_points.extend(
        [
            TelemetryPoint(
                device_id="1",
                source_type="z3k_temperature",
                entity_id="zont:1:z3k_temperature:105",
                metric_key="z3k_temperature",
                timestamp_utc=datetime(2026, 8, 24, tzinfo=UTC),
                value_num=31,
                unit="°C",
            ),
            TelemetryPoint(
                device_id="1",
                source_type="z3k_boiler_adapter",
                entity_id="zont:1:z3k_boiler_adapter:900",
                metric_key="rwt",
                timestamp_utc=datetime(2026, 8, 24, tzinfo=UTC),
                value_num=30.5,
                unit="°C",
            ),
        ]
    )

    db = make_database(tmp_path)
    db.save_devices([raw_device])
    _seed_points(db, ordered_points)
    service = IngestionService(db, cast(ZontReadOnlyClient, object()), AppConfig())
    service._refresh_series_roles(devices, {}, _object_names(devices))

    roles = {(item["entity_id"], item["metric_key"]): item for item in db.list_series()}
    control = roles[("zont:1:z3k_radio_sensor:101", "temperature")]
    assert control["role"] == "control_indoor_temperature"
    assert control["provenance"] == "zont_config.heating_circuits[].air_temp_sensor"
    assert control["confidence"] == 1.0
    assert roles[("zont:1:z3k_radio_sensor:101", "humidity")]["role"] == "humidity"
    assert roles[("zont:1:z3k_temperature:102", "z3k_temperature")]["role"] == "technical_temperature"
    assert roles[("zont:1:z3k_temperature:103", "z3k_temperature")]["role"] == "room_temperature"
    unresolved = roles[("zont:1:z3k_temperature:104", "z3k_temperature")]
    assert unresolved["role"] == "temperature"
    assert unresolved["confidence"] <= 0.4
    external_return = roles[("zont:1:z3k_temperature:105", "z3k_temperature")]
    boiler_return = roles[("zont:1:z3k_boiler_adapter:900", "rwt")]
    assert (external_return["role"], external_return["origin"]) == ("return_temperature", "external_sensor")
    assert (boiler_return["role"], boiler_return["origin"]) == (
        "return_temperature",
        "boiler_reported_rwt",
    )


def test_user_override_can_select_new_primary_without_reclassifying_radio_humidity(tmp_path: Path) -> None:
    raw_device = {
        "device_id": 1,
        "z3k_config": {
            "heating_circuits": [{"id": 200, "name": "Отопление", "air_temp_sensor": 101}],
            "radiosensors": [{"id": 101, "name": "Гостиная"}, {"id": 102, "name": "Спальня"}],
        },
    }
    devices = [{"id": "1", "raw": raw_device}]
    timestamp = datetime(2026, 8, 24, tzinfo=UTC)
    points = [
        TelemetryPoint(
            device_id="1",
            source_type="z3k_radio_sensor",
            entity_id=f"zont:1:z3k_radio_sensor:{sensor_id}",
            metric_key=metric,
            timestamp_utc=timestamp,
            value_num=value,
            unit=unit,
        )
        for sensor_id in (101, 102)
        for metric, value, unit in (("temperature", 22.0, "°C"), ("humidity", 50.0, "%"))
    ]
    config = AppConfig.model_validate(
        {"entity_overrides": {"zont:1:z3k_radio_sensor:102": {"role": "control_indoor_temperature"}}}
    )
    db = make_database(tmp_path)
    db.save_devices([raw_device])
    _seed_points(db, points)
    IngestionService(db, cast(ZontReadOnlyClient, object()), config)._refresh_series_roles(
        devices, {}, _object_names(devices)
    )

    series = db.list_series()
    selected = _select_control_temperature_series(series, devices, None)
    assert selected is not None
    assert selected["entity_id"] == "zont:1:z3k_radio_sensor:102"
    assert selected["provenance"] == "config.entity_overrides"
    humidity_roles = {item["role"] for item in series if item["metric_key"] == "humidity"}
    assert humidity_roles == {"humidity"}

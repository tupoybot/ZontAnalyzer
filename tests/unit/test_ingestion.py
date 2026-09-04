from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.zont_readonly import ZontReadOnlyClient
from zont_analyzer.adapters.zont_readonly.client import infer_role
from zont_analyzer.application.analysis import _select_control_temperature_series
from zont_analyzer.application.ingestion import IngestionService, _linked_indoor_sensor_ids, _object_names
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import TelemetryPoint

CONTRACT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "zont_contract"


def test_explicit_backfill_replays_requested_interval_despite_current_cursor(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []

        def discover_devices(self) -> list[dict[str, object]]:
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **kwargs: object) -> list[dict[str, object]]:
            self.history_calls.append(kwargs)
            return [{"device_id": 1, "ok": True, "dta": {}}]

        def normalize_history(
            self, _response: dict[str, object]
        ) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
            return [], {}

        def load_events(self, **_kwargs: object) -> list[list[Any]]:
            return []

        def normalize_events(self, _device_id: str, _events: list[list[Any]]) -> list[Any]:
            return []

    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    requested_start = now - timedelta(days=1)
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    db.upsert_samples(
        [
            TelemetryPoint(
                device_id="1",
                source_type="z3k_temperature",
                entity_id="zont:1:z3k_temperature:1",
                metric_key="z3k_temperature",
                timestamp_utc=now - timedelta(days=30),
                value_num=20.0,
                unit="°C",
            )
        ]
    )
    db.set_cursor("1", "temperature", now)
    client = RecordingClient()

    result = IngestionService(db, cast(ZontReadOnlyClient, client), AppConfig()).sync(
        backfill=timedelta(days=1),
        now=now,
    )

    assert result["complete"] is True
    assert result["windows"] == 1
    assert client.history_calls[0]["start"] == requested_start
    assert client.history_calls[0]["end"] == now


def test_empty_database_bootstrap_retries_the_all_time_request_and_reports_observed_range(tmp_path: Path) -> None:
    class PagingClient:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []
            self.calls = 0

        def discover_devices(self) -> list[dict[str, object]]:
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **kwargs: object) -> list[dict[str, object]]:
            self.history_calls.append(kwargs)
            self.calls += 1
            if self.calls == 1:
                return [{"device_id": 1, "ok": False, "error": "temporary"}]
            timestamp = now - timedelta(days=30)
            return [
                {
                    "device_id": 1,
                    "ok": True,
                    "time_truncated": True,
                    "timestamp": timestamp,
                }
            ]

        def normalize_history(
            self, response: dict[str, object]
        ) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
            timestamp = cast(datetime, response["timestamp"])
            return (
                [
                    TelemetryPoint(
                        device_id="1",
                        source_type="z3k_temperature",
                        entity_id="zont:1:z3k_temperature:1",
                        metric_key="z3k_temperature",
                        timestamp_utc=timestamp,
                        value_num=20.0,
                        unit="°C",
                    )
                ],
                {},
            )

        def load_events(self, **_kwargs: object) -> list[list[Any]]:
            return []

        def normalize_events(self, _device_id: str, _events: list[list[Any]]) -> list[Any]:
            return []

    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    client = PagingClient()
    config = AppConfig.model_validate({"zont": {"history_data_types": ["z3k_temperature"]}})
    service = IngestionService(db, cast(ZontReadOnlyClient, client), config)

    interrupted = service.sync(now=now)

    assert interrupted["complete"] is False
    assert client.history_calls[0]["start"] == datetime.fromtimestamp(0, UTC)
    assert client.history_calls[0]["end"] == now

    completed = service.sync(now=now)

    assert completed["complete"] is True
    assert client.history_calls[1]["start"] == datetime.fromtimestamp(0, UTC)
    assert client.history_calls[1]["end"] == now
    assert completed["history_range"] == {
        "first_observed": (now - timedelta(days=30)).isoformat(),
        "last_observed": (now - timedelta(days=30)).isoformat(),
    }
    assert completed["bootstrap_ranges"] == {
        "1:z3k_temperature": {
            "requested_start": datetime.fromtimestamp(0, UTC).isoformat(),
            "requested_end": now.isoformat(),
            "time_truncated": True,
            "first_observed": (now - timedelta(days=30)).isoformat(),
            "last_observed": (now - timedelta(days=30)).isoformat(),
        }
    }
    assert db.get_cursor("1", "z3k_temperature") == now


def test_empty_bootstrap_response_records_no_invented_bounds_and_completes(tmp_path: Path) -> None:
    class EmptyArchiveClient:
        def __init__(self) -> None:
            self.history_calls: list[dict[str, object]] = []

        def discover_devices(self) -> list[dict[str, object]]:
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **kwargs: object) -> list[dict[str, object]]:
            self.history_calls.append(kwargs)
            return [{"device_id": 1, "ok": True, "time_truncated": True}]

        def normalize_history(
            self, _response: dict[str, object]
        ) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
            return [], {}

        def load_events(self, **_kwargs: object) -> list[list[Any]]:
            return []

        def normalize_events(self, _device_id: str, _events: list[list[Any]]) -> list[Any]:
            return []

    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    client = EmptyArchiveClient()
    config = AppConfig.model_validate({"zont": {"history_data_types": ["z3k_temperature"]}})

    result = IngestionService(db, cast(ZontReadOnlyClient, client), config).sync(now=now)

    assert result["complete"] is True
    assert result["history_range"] == {"first_observed": None, "last_observed": None}
    assert result["bootstrap_ranges"] == {
        "1:z3k_temperature": {
            "requested_start": datetime.fromtimestamp(0, UTC).isoformat(),
            "requested_end": now.isoformat(),
            "time_truncated": True,
            "first_observed": None,
            "last_observed": None,
        }
    }
    assert db.get_cursor("1", "z3k_temperature") == now


def test_bootstrap_transport_error_is_reported_and_retried(tmp_path: Path) -> None:
    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def discover_devices(self) -> list[dict[str, object]]:
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **_kwargs: object) -> list[dict[str, object]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("connection reset")
            return [{"device_id": 1, "ok": True}]

        def normalize_history(
            self, _response: dict[str, object]
        ) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
            return [], {}

        def load_events(self, **_kwargs: object) -> list[list[Any]]:
            return []

        def normalize_events(self, _device_id: str, _events: list[list[Any]]) -> list[Any]:
            return []

    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    client = FlakyClient()
    config = AppConfig.model_validate({"zont": {"history_data_types": ["z3k_temperature"]}})
    service = IngestionService(db, cast(ZontReadOnlyClient, client), config)

    interrupted = service.sync(now=now)
    assert db.get_cursor("1", "history_bootstrap_complete:z3k_temperature") is None
    completed = service.sync(now=now)

    assert interrupted["complete"] is False
    assert interrupted["failed_windows"] == 1
    assert interrupted["errors"] == [
        f"{datetime.fromtimestamp(0, UTC).isoformat()} history request: RuntimeError: connection reset",
        "bootstrap device 1 data type z3k_temperature: no successful response",
    ]
    assert client.calls == 2
    assert db.get_cursor("1", "history_bootstrap_complete:z3k_temperature") == now
    assert completed["complete"] is True


def test_bootstrap_streams_real_client_points_in_bounded_batches_and_filters_requested_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StreamingClient:
        def discover_devices(self) -> list[dict[str, object]]:
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **_kwargs: object) -> list[dict[str, object]]:
            return [{"device_id": 1, "ok": True}]

        def normalize_history(
            self, _response: dict[str, object]
        ) -> tuple[list[TelemetryPoint], dict[str, dict[str, Any]]]:
            raise AssertionError("ingestion must use the streaming normalizer when it is available")

        def iter_normalized_history(
            self, _response: dict[str, object]
        ) -> tuple[object, dict[str, dict[str, Any]]]:
            def points() -> object:
                yield TelemetryPoint(
                    device_id="1",
                    source_type="z3k_boiler_adapter",
                    entity_id="zont:1:z3k_boiler_adapter:1",
                    metric_key="bt",
                    timestamp_utc=now + timedelta(seconds=1),
                    value_num=0.0,
                    unit="°C",
                )
                for index in range(4_001):
                    yield TelemetryPoint(
                        device_id="1",
                        source_type="z3k_boiler_adapter",
                        entity_id="zont:1:z3k_boiler_adapter:1",
                        metric_key="bt",
                        timestamp_utc=now - timedelta(seconds=index),
                        value_num=float(index),
                        unit="°C",
                    )

            return points(), {
                "zont:1:z3k_boiler_adapter:1": {
                    "device_id": "1",
                    "source_type": "z3k_boiler_adapter",
                    "external_id": "1",
                    "display_name": "boiler",
                    "role": "flow_temperature",
                    "confidence": 1.0,
                    "unit": "°C",
                }
            }

        def load_events(self, **_kwargs: object) -> list[list[Any]]:
            return []

        def normalize_events(self, _device_id: str, _events: list[list[Any]]) -> list[Any]:
            return []

    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    batch_sizes: list[int] = []
    original_upsert = db.upsert_samples

    def record_batches(points: object, roles: dict[str, str] | None = None) -> int:
        batch = list(cast(list[TelemetryPoint], points))
        batch_sizes.append(len(batch))
        return original_upsert(batch, roles)

    monkeypatch.setattr(db, "upsert_samples", record_batches)
    config = AppConfig.model_validate({"zont": {"history_data_types": ["z3k_boiler_adapter"]}})

    result = IngestionService(db, cast(ZontReadOnlyClient, StreamingClient()), config).sync(now=now)

    assert result["complete"] is True
    assert batch_sizes == [2_000, 2_000, 1]
    assert result["history_range"] == {
        "first_observed": (now - timedelta(seconds=4_000)).isoformat(),
        "last_observed": now.isoformat(),
    }


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

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    db.save_devices([raw_device])
    db.upsert_samples(ordered_points)
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
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    db.save_devices([raw_device])
    db.upsert_samples(points)
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

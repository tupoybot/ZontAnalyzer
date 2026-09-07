from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.adapters.sqlite.database import Base
from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.application.ingestion import IngestionService
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import MetricValue, SourceEvent, TelemetryPoint
from zont_analyzer.reports import render_html, render_text


def _points(start: datetime, values: list[float], *, entity: str, metric: str = "temperature"):
    for index, value in enumerate(values):
        yield TelemetryPoint(
            device_id="1",
            source_type="synthetic",
            entity_id=entity,
            metric_key=metric,
            timestamp_utc=start + timedelta(minutes=5 * index),
            value_num=value,
            unit="°C",
        )


def test_upsert_is_idempotent_and_analysis_persists_report(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)  # local 2026-08-01 in Samara
    values = [22 + (0.1 if index % 2 else -0.1) for index in range(288)]
    points = list(_points(start, values, entity="room"))
    assert db.upsert_samples(points, {"room": "indoor_temperature"}) == 288
    assert db.upsert_samples(points, {"room": "indoor_temperature"}) == 288
    outside = list(_points(start, [5.0] * 288, entity="outside"))
    assert db.upsert_samples(outside, {"outside": "outdoor_temperature"}) == 288
    assert db.status()["samples"] == 576

    config = AppConfig.model_validate({"preferences": {"target_temperature_c": 22}})
    report = AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)
    assert report.quality.score > 0.7
    assert any(item.name == "time_in_target_band_pct" for item in report.metrics)
    assert any(item.name == "outdoor_mean_temperature_c" for item in report.metrics)
    assert db.latest_report() is not None
    assert db.latest_report().id == report.id

    # Re-running uses stable IDs and does not duplicate the report.
    AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)
    assert db.status()["reports"] == 1


def test_upsert_fills_unit_for_series_discovered_before_unit_was_known(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    point = TelemetryPoint(
        device_id="1",
        source_type="z3k_radio_sensor",
        entity_id="radio",
        metric_key="humidity",
        timestamp_utc=timestamp,
        value_num=55,
    )
    db.upsert_samples([point], {"radio": "humidity"})
    point.unit = "%"
    db.upsert_samples([point], {"radio": "humidity"})

    assert db.list_series()[0]["unit"] == "%"


@pytest.mark.parametrize("control_first", [True, False])
def test_comfort_analysis_uses_only_control_sensor_regardless_of_series_order(
    tmp_path: Path, control_first: bool
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    control = (list(_points(start, [22.0] * 288, entity="control")), "control_indoor_temperature")
    technical = (list(_points(start, [35.0] * 288, entity="boiler-room")), "technical_temperature")
    room = (list(_points(start, [19.0] * 288, entity="bedroom")), "room_temperature")
    groups = [control, technical, room] if control_first else [room, technical, control]
    for points, role in groups:
        db.upsert_samples(points, {points[0].entity_id: role})

    report = AnalysisService(
        db,
        AppConfig.model_validate({"preferences": {"target_temperature_c": 22}}),
    ).analyze_daily(date(2026, 8, 1), use_ai=False)

    mean_temperature = next(item for item in report.metrics if item.name == "mean_temperature_c")
    assert mean_temperature.value == 22.0
    sensors = report.context["sensors"]
    assert sensors["control_resolution"] == "resolved"
    assert sensors["control_temperature"]["entity_id"] == "control"
    assert [item["entity_id"] for item in sensors["technical_temperatures"]] == ["boiler-room"]
    assert [item["entity_id"] for item in sensors["room_temperatures"]] == ["bedroom"]


def test_report_renders_compact_sensor_identity_and_return_origins(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    points = list(_points(start, [22.0] * 288, entity="living-room"))
    db.upsert_samples(points, {"living-room": "control_indoor_temperature"})
    control_series = next(item for item in db.list_series() if item["entity_id"] == "living-room")
    db.update_series_role(
        int(control_series["id"]),
        "control_indoor_temperature",
        "Гостиная",
        confidence=1.0,
        provenance="zont_config.heating_circuits[].air_temp_sensor",
        origin="radio_sensor",
    )
    for entity, source_type, metric, origin in (
        ("external-return", "z3k_temperature", "z3k_temperature", "external_sensor"),
        ("boiler-return", "z3k_boiler_adapter", "rwt", "boiler_reported_rwt"),
    ):
        point = TelemetryPoint(
            device_id="1",
            source_type=source_type,
            entity_id=entity,
            metric_key=metric,
            timestamp_utc=start,
            value_num=31,
            unit="°C",
        )
        db.upsert_samples([point], {entity: "return_temperature"})
        row = next(item for item in db.list_series() if item["entity_id"] == entity)
        db.update_series_role(
            int(row["id"]),
            "return_temperature",
            "Обратка",
            confidence=0.95,
            provenance="history source semantics",
            origin=origin,
        )

    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    text = render_text(report)
    html = render_html(report)

    assert "Контрольная температура контура: Гостиная" in text
    assert "внешний датчик" in text
    assert "значение rwt котла" in text
    assert "Контрольная температура контура: Гостиная" in html
    assert len(report.context["sensors"]["return_temperatures"]) == 2


def test_temperature_above_setpoint_is_not_attributed_to_inactive_heating(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    db.upsert_samples(
        list(_points(start, [24.0] * 288, entity="room")),
        {"room": "indoor_temperature"},
    )
    burner = list(_points(start, [0.0] * 288, entity="boiler", metric="flame"))
    db.upsert_samples(burner, {"boiler": "burner_activity"})

    config = AppConfig.model_validate({"preferences": {"target_temperature_c": 18}})
    report = AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)

    above = next(item for item in report.metrics if item.name == "degree_hours_above_target")
    assert above.value == pytest.approx(143.5, abs=0.001)
    assert above.unit == "°C·h"
    assert above.context["heating_causality"] == "not_supported_by_burner_activity"
    event = next(item for item in report.events if item.kind == "temperature_above_heating_setpoint")
    assert event.severity == "info"
    assert "не подтверждает перегрев от отопления" in report.summary


def test_low_quality_creates_observation_recommendation_and_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    config = AppConfig()
    report = AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)
    assert report.recommendations[0].category == "observe_only"
    recommendation_id = report.recommendations[0].id
    assert recommendation_id
    intervention = db.mark_applied(recommendation_id, "Проверил питание контроллера")
    assert intervention.startswith("intervention:")
    assert db.recommendation(recommendation_id)["status"] == "applied"

    # Idempotent report recalculation must preserve user lifecycle state.
    AnalysisService(db, config).analyze_daily(date(2026, 8, 1), use_ai=False)
    assert db.recommendation(recommendation_id)["status"] == "applied"


def test_html_escapes_report_content(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    report.summary = '<script>alert("x")</script>'
    recommendation_id = report.recommendations[0].id
    assert recommendation_id is not None
    owner_note = '</textarea><script>alert("owner")</script>'
    rendered = render_html(
        report,
        {recommendation_id: {"status": "rejected", "owner_note": owner_note}},
    )
    assert '<script>alert("x")</script>' not in rendered
    assert "&lt;script&gt;" in rendered
    assert owner_note not in rendered
    assert "&lt;/textarea&gt;&lt;script&gt;alert(&quot;owner&quot;)&lt;/script&gt;" in rendered


def test_reliability_events_persist_and_uptime_is_prominent(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 30, 20, tzinfo=UTC)
    points: list[TelemetryPoint] = []
    for index in range(3 * 24 * 12):
        timestamp = start + timedelta(minutes=index * 5)
        points.extend(
            [
                TelemetryPoint(
                    device_id="1",
                    source_type="z3k_boiler_adapter",
                    entity_id="boiler",
                    metric_key="s",
                    timestamp_utc=timestamp,
                    value_text="[]",
                ),
                TelemetryPoint(
                    device_id="1",
                    source_type="ztc_state",
                    entity_id="zont",
                    metric_key="status_flags",
                    timestamp_utc=timestamp,
                    value_num=73,
                ),
            ]
        )
    db.upsert_samples(points)
    restored = start + timedelta(hours=2)
    source = SourceEvent(
        id="restore",
        device_id="1",
        event_type="ReconnectingBoiler",
        timestamp_utc=restored,
    )
    assert db.upsert_source_events([source, source]) == 2
    assert len(db.list_source_events(start, start + timedelta(days=3))) == 1

    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    metrics = {item.name: item for item in report.metrics}
    assert metrics["boiler_uptime_seconds"].value == pytest.approx(46 * 3600)
    assert metrics["zont_uptime_seconds"].value == pytest.approx(48 * 3600)
    rendered_text = render_text(report)
    rendered_html = render_html(report)
    assert "Аптайм котла: 01:22:00 дд:чч:мм" in rendered_text
    assert "Аптайм ZONT: 02:00:00 дд:чч:мм" in rendered_text
    assert 'class="kpi-uptime-row"' in rendered_html
    assert "1 дн." in rendered_html
    assert "2 дн." in rendered_html
    assert "Аптайм котла" in rendered_html  # Exact values remain in metric details.
    assert "01:22:00" in rendered_html


def test_stale_reliability_data_is_rendered_as_offline(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    telemetry_start = datetime(2026, 7, 20, 20, tzinfo=UTC)
    points: list[TelemetryPoint] = []
    for offset in range(3):
        timestamp = telemetry_start + timedelta(minutes=offset * 5)
        points.extend(
            [
                TelemetryPoint(
                    device_id="1",
                    source_type="z3k_boiler_adapter",
                    entity_id="boiler",
                    metric_key="s",
                    timestamp_utc=timestamp,
                    value_text="[]",
                ),
                TelemetryPoint(
                    device_id="1",
                    source_type="ztc_state",
                    entity_id="zont",
                    metric_key="status_flags",
                    timestamp_utc=timestamp,
                    value_num=73,
                ),
            ]
        )
    db.upsert_samples(points)

    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    metrics = {item.name: item for item in report.metrics}
    assert metrics["boiler_uptime_seconds"].value == 0
    assert metrics["zont_uptime_seconds"].value == 0
    assert "boiler_mtbf_hours" not in metrics
    assert "boiler_mttr_hours" not in metrics

    rendered_text = render_text(report)
    rendered_html = render_html(report)
    assert "Аптайм котла (офлайн): 00:00:00 дд:чч:мм" in rendered_text
    assert "Аптайм ZONT (офлайн): 00:00:00 дд:чч:мм" in rendered_text
    assert "Аптайм котла (офлайн)" in rendered_html
    assert "Аптайм ZONT (офлайн)" in rendered_html


def test_uptime_renderer_does_not_wrap_days_after_99(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    report.metrics.append(
        MetricValue(
            id="uptime",
            name="zont_uptime_seconds",
            value=(123 * 24 + 4) * 3600 + 5 * 60 + 59,
            unit="s",
        )
    )
    report.metrics.extend(
        [
            MetricValue(
                id="mtbf",
                name="boiler_mtbf_hours",
                value=158 + 22 / 60,
                unit="h",
                context={"lower_bound": True, "completed_failures": 0},
            ),
            MetricValue(
                id="mttr",
                name="boiler_mttr_hours",
                value=17 / 60,
                unit="h",
            ),
        ]
    )

    text = render_text(report)
    html = render_html(report)
    assert "Аптайм ZONT: 123:04:05 дд:чч:мм" in text
    assert "MTBF котельного сервиса: > 06:14:22 дд:чч:мм" in text
    assert "MTTR котельного сервиса: 00:00:17 дд:чч:мм" in text
    assert "123:04:05" in html
    assert "&gt; 06:14:22" in html
    assert "00:00:17" in html


def test_renderers_show_disabled_dhw_target_as_inactive(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    report.context["dhw_interaction"] = {
        "dhw_circuit": {
            "current_mode": {"name": "Эконом"},
            "current_enabled": False,
            "current_target_c": None,
            "configured_or_last_target_c": 35.0,
        },
        "data_quality": {"score": 0.8},
        "recirculation": {"configured_present": True},
    }

    text = render_text(report)
    html = render_html(report)
    assert "ГВС: отключена выбранным режимом Эконом" in text
    assert "сохранённая неактивная уставка 35 °C" in text
    assert "активная цель 35" not in text
    assert "OFF; сохранённая неактивная уставка 35 °C" in html
    assert "прямого датчика насоса нет" in html


def test_initial_report_uses_latest_sample_and_stable_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    latest = datetime(2026, 5, 27, 12, tzinfo=UTC)
    db.upsert_samples(list(_points(latest, [21.0], entity="room")), {"room": "indoor_temperature"})

    first = AnalysisService(db, AppConfig()).analyze_initial(use_ai=False)
    second = AnalysisService(db, AppConfig()).analyze_initial(use_ai=False)

    assert first.period_end == latest + timedelta(seconds=1)
    assert first.period_start == latest
    assert second.id == first.id
    assert db.status()["reports"] == 1
    assert first.id in render_text(first)


def test_openai_failure_keeps_deterministic_report(tmp_path: Path) -> None:
    class FailingAnalyst:
        def analyze(self, _packet):
            raise RuntimeError("offline")

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    values = [22 + (0.1 if index % 2 else -0.1) for index in range(288)]
    db.upsert_samples(list(_points(start, values, entity="room")), {"room": "indoor_temperature"})
    config = AppConfig.model_validate(
        {
            "preferences": {"target_temperature_c": 22},
            "analysis": {"daily_ai_when_normal": True},
        }
    )

    report = AnalysisService(db, config, FailingAnalyst()).analyze_daily(date(2026, 8, 1))

    assert report.ai_used is False
    assert "AI-интерпретация недоступна" in report.summary
    assert db.latest_report() is not None


@pytest.mark.parametrize("legacy_policy", [False, True])
def test_openai_refresh_failure_reuses_last_valid_interpretation(tmp_path: Path, legacy_policy: bool) -> None:
    class SuccessfulAnalyst:
        def analyze(self, _packet):
            from zont_analyzer.domain import AnalysisResult
            from zont_analyzer.domain.reasoning import Unknown
            return AnalysisResult(summary="Последняя валидная AI-интерпретация", unknowns=[
                Unknown(id="unknown:presence", statement="Присутствие неизвестно")
            ])

    class FailingAnalyst:
        def analyze(self, _packet):
            raise RuntimeError("invalid structured output")

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    db.upsert_samples(
        list(_points(start, [22.0] * 288, entity="room")),
        {"room": "indoor_temperature"},
    )
    config = AppConfig.model_validate(
        {
            "preferences": {"target_temperature_c": 22},
            "analysis": {"daily_ai_when_normal": True},
        }
    )
    first = AnalysisService(db, config, SuccessfulAnalyst()).analyze_daily(date(2026, 8, 1))
    if legacy_policy:
        first.context.pop("recommendation_policy")
        db.save_report(first, render_text(first))
    refreshed = AnalysisService(db, config, FailingAnalyst()).analyze_daily(date(2026, 8, 1))

    assert first.ai_used is True
    if legacy_policy:
        assert refreshed.ai_used is False
        assert "ai_interpretation_reuse" not in refreshed.context
        assert refreshed.summary != first.summary
        return
    assert refreshed.ai_used is True
    assert refreshed.summary == "Последняя валидная AI-интерпретация"
    assert refreshed.unknowns == first.unknowns
    assert refreshed.context["ai_interpretation_reuse"]["source_generated_at"] == first.generated_at.isoformat()


def test_online_backup_passes_integrity_check(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    backup = db.backup(tmp_path / "backups")
    assert backup.exists()
    restored = Database(backup)
    assert restored.integrity_check() == "ok"


def test_analysis_integrates_dhw_episode_and_heating_return(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)

    def numeric_points(
        entity: str,
        metric: str,
        values: list[float],
        *,
        source_type: str,
        unit: str | None = None,
    ) -> list[TelemetryPoint]:
        return [
            TelemetryPoint(
                device_id="1",
                source_type=source_type,
                entity_id=entity,
                metric_key=metric,
                timestamp_utc=start + timedelta(minutes=5 * index),
                value_num=value,
                unit=unit,
            )
            for index, value in enumerate(values)
        ]

    room = [21.0] * 288
    dhw_temperature = [55.0 + (0.1 if index % 2 else 0.0) for index in range(288)]
    for index, value in enumerate((48.0, 50.0, 53.0, 55.0), start=100):
        dhw_temperature[index] = value
    states = ["['ch', 'fl']"] * 288
    for index in range(100, 104):
        states[index] = "['dhw', 'fl']"
    text_points = [
        TelemetryPoint(
            device_id="1",
            source_type="z3k_boiler_adapter",
            entity_id="boiler",
            metric_key="s",
            timestamp_utc=start + timedelta(minutes=5 * index),
            value_text=value,
        )
        for index, value in enumerate(states)
    ]
    batches = [
        (numeric_points("room", "temperature", room, source_type="synthetic", unit="°C"), "indoor_temperature"),
        (
            numeric_points("heating", "target_temp", [22.0] * 288, source_type="z3k_heating_circuit", unit="°C"),
            "target_temperature",
        ),
        (
            numeric_points("heating", "worktime", [1.0] * 288, source_type="z3k_heating_circuit"),
            "heating_activity",
        ),
        (
            numeric_points("dhw", "target_temp", [55.0] * 288, source_type="z3k_heating_circuit", unit="°C"),
            "dhw_target_temperature",
        ),
        (
            numeric_points("dhw", "worktime", [1.0] * 288, source_type="z3k_heating_circuit"),
            "dhw_activity",
        ),
        (
            numeric_points("boiler", "dt", dhw_temperature, source_type="z3k_boiler_adapter", unit="°C"),
            "dhw_temperature",
        ),
        (
            numeric_points("boiler", "bt", [50.0] * 288, source_type="z3k_boiler_adapter", unit="°C"),
            "flow_temperature",
        ),
        (text_points, "unknown"),
    ]
    for points, role in batches:
        db.upsert_samples(points)
        row = next(
            item
            for item in db.list_series()
            if item["entity_id"] == points[0].entity_id and item["metric_key"] == points[0].metric_key
        )
        db.update_series_role(int(row["id"]), role)

    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)

    assert report.context["dhw_interaction"]["data_quality"]["score"] >= 0.7
    assert next(item.value for item in report.metrics if item.name == "dhw_episode_count") == 1
    episode = next(item for item in report.events if item.kind == "dhw_reheat_episode")
    assert episode.details["inference"]["heating_demand"] == "confirmed"
    assert "По ГВС найдено 1 эпизод" in report.summary
    assert "ГВС ↔ отопление" in render_html(report)


def test_analysis_filters_only_short_flame_pulse_without_flow_response(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    start = datetime(2026, 7, 31, 20, tzinfo=UTC)
    state_values = ["[]"] * 17
    state_values[1] = "['dhw', 'fl']"
    state_values[10] = "['dhw', 'fl']"
    states = [
        TelemetryPoint(
            device_id="1",
            source_type="z3k_boiler_adapter",
            entity_id="boiler",
            metric_key="s",
            timestamp_utc=start + timedelta(minutes=index),
            value_text=value,
        )
        for index, value in enumerate(state_values)
    ]
    flow = [25.0] * 17
    flow[15] = 34.8
    flow[16] = 35.0
    dhw = [45.0] * 17
    dhw[15] = 48.0
    for points, role in (
        (states, "unknown"),
        (list(_points(start, flow, entity="boiler", metric="bt")), "flow_temperature"),
        (list(_points(start, dhw, entity="boiler", metric="dt")), "dhw_temperature"),
    ):
        # _points uses five-minute spacing; rebuild numeric boiler timestamps at one minute.
        if points is not states:
            for index, point in enumerate(points):
                point.timestamp_utc = start + timedelta(minutes=index)
                point.source_type = "z3k_boiler_adapter"
        db.upsert_samples(points)
        row = next(
            item
            for item in db.list_series()
            if item["entity_id"] == points[0].entity_id and item["metric_key"] == points[0].metric_key
        )
        db.update_series_role(int(row["id"]), role)

    report = AnalysisService(db, AppConfig()).analyze_daily(date(2026, 8, 1), use_ai=False)
    by_name = {item.name: item.value for item in report.metrics}

    assert by_name["unconfirmed_burner_pulse_count"] == 1
    assert by_name["dhw_burner_starts"] == 1
    assert by_name["dhw_episode_count"] == 1
    noise = next(item for item in report.events if item.kind == "unconfirmed_burner_pulse")
    assert noise.severity == "info"


def test_partial_sync_does_not_advance_cursor(tmp_path: Path) -> None:
    class PartialClient:
        def discover_devices(self):
            return [{"device_id": 1, "name": "test"}]

        def load_history(self, **_kwargs):
            return [{"device_id": 1, "ok": False, "error": "temporary"}]

        def normalize_history(self, _response):
            raise AssertionError("failed response must not be normalized")

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    service = IngestionService(db, PartialClient(), AppConfig())  # type: ignore[arg-type]

    result = service.sync(backfill=timedelta(days=1), now=datetime(2026, 8, 1, tzinfo=UTC))

    assert result["complete"] is False
    assert result["failed_windows"] == 1
    assert db.get_cursor("1", "temperature") is None


def test_fresh_database_is_created_at_alembic_head(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    result = db.initialize()

    assert result.previous_revision is None
    assert result.revision == "c3d7a8e9f102"
    assert result.backup_path is None
    assert db.current_revision() == result.revision
    assert db.status()["schema_revision"] == result.revision


def test_legacy_create_all_database_is_backed_up_and_adopted(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    legacy = Database(db_path)
    Base.metadata.create_all(legacy.engine)

    db = Database(db_path)
    result = db.initialize(tmp_path / "migration-backups")

    assert result.previous_revision is None
    assert result.adopted_legacy_schema is True
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert sqlite3.connect(result.backup_path).execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert db.current_revision() == result.revision

    repeated = Database(db_path).initialize(tmp_path / "migration-backups")
    assert repeated.previous_revision == result.revision
    assert repeated.backup_path is None
    assert repeated.adopted_legacy_schema is False


def test_previous_version_is_backed_up_and_migrated_with_series_semantics(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    previous = Database(db_path)
    previous._run_alembic(previous._migration_config(), "upgrade", "5a9ce2bd8b34")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO telemetry_series
                (device_id, source_type, entity_id, metric_key, unit, display_name, role)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("1", "z3k_temperature", "return", "z3k_temperature", "°C", "Обратка", "return_temperature"),
        )

    upgraded = Database(db_path)
    result = upgraded.initialize(tmp_path / "migration-backups")

    assert result.previous_revision == "5a9ce2bd8b34"
    assert result.revision == "c3d7a8e9f102"
    assert result.backup_path is not None and result.backup_path.exists()
    series = upgraded.list_series()[0]
    assert series["confidence"] == 0.3
    assert series["provenance"] == "unknown"
    assert series["origin"] == "unknown"


def test_unversioned_partial_schema_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE devices (id TEXT PRIMARY KEY)")

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="unversioned, incomplete schema"):
        db.initialize()


def test_unversioned_schema_with_wrong_shape_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    legacy = Database(db_path)
    Base.metadata.create_all(legacy.engine)
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE devices RENAME COLUMN name TO wrong_name")

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="does not match the known legacy baseline"):
        db.initialize()

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
from zont_analyzer.domain import TelemetryPoint
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
    rendered = render_html(report)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_initial_report_uses_latest_sample_and_stable_id(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    latest = datetime(2026, 5, 27, 12, tzinfo=UTC)
    db.upsert_samples(list(_points(latest, [21.0], entity="room")), {"room": "indoor_temperature"})

    first = AnalysisService(db, AppConfig()).analyze_initial(use_ai=False)
    second = AnalysisService(db, AppConfig()).analyze_initial(use_ai=False)

    assert first.period_end == latest + timedelta(seconds=1)
    assert first.period_end - first.period_start == timedelta(days=30)
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
            numeric_points(
                "boiler", "dt", dhw_temperature, source_type="z3k_boiler_adapter", unit="°C"
            ),
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
    assert result.revision == "dd4272b6d030"
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

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zont_analyzer.application.gas import GasService
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import Prediction, Recommendation, TelemetryPoint
from zont_analyzer.runtime import build_runtime


def _outdoor_temperature(moment: datetime, boundaries: list[datetime], temperatures: list[float]) -> float:
    for start, end, temperature in zip(boundaries[:-1], boundaries[1:], temperatures, strict=True):
        if start <= moment < end:
            return temperature
    return temperatures[0] if moment < boundaries[0] else temperatures[-1]


def _modeled_volume(temperature: float, *, saving_m3: float = 0.0) -> float:
    hours = 48.0
    return hours + 0.05 * max(0.0, 18.0 - temperature) * hours - saving_m3


def test_manual_intervention_prediction_is_frozen_then_checked_by_later_meter_readings(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.home.timezone = "UTC"
    runtime.config.analysis.modulation_capability_profile = "flame_zero_is_minimum"
    runtime.db.save_devices([{"id": "1", "name": "boiler"}])

    reading_days = (1, 3, 5, 7, 9, 11, 13, 15, 17)
    boundaries = [datetime(2026, 1, day, 12, tzinfo=UTC) for day in reading_days]
    temperatures = [3.0, 7.0, 10.0, 5.0, 12.0, 8.0, 9.0, 8.0]
    points: list[TelemetryPoint] = []
    telemetry_start = datetime(2026, 1, 1, tzinfo=UTC)
    telemetry_end = datetime(2026, 1, 18, tzinfo=UTC)
    cursor = telemetry_start
    while cursor <= telemetry_end:
        outdoor = _outdoor_temperature(cursor, boundaries, temperatures)
        points.extend((
            TelemetryPoint(
                device_id="1",
                entity_id="boiler",
                source_type="z3k_boiler_adapter",
                metric_key="s",
                timestamp_utc=cursor,
                value_text="['fl', 'ch']",
            ),
            TelemetryPoint(
                device_id="1",
                entity_id="boiler",
                source_type="z3k_boiler_adapter",
                metric_key="rml",
                timestamp_utc=cursor,
                value_num=0.0,
            ),
            TelemetryPoint(
                device_id="1",
                entity_id="outdoor",
                source_type="z3k_boiler_adapter",
                metric_key="outdoor",
                timestamp_utc=cursor,
                value_num=outdoor,
            ),
        ))
        cursor += timedelta(minutes=10)
    runtime.db.upsert_samples(points, roles={"outdoor": "outdoor_temperature"})

    reports = {
        day: runtime.analysis(no_ai=True).analyze_daily(date(2026, 1, day), use_ai=False)
        for day in reading_days + (14,)
    }
    source_report = reports[14]
    source_report.predictions = [Prediction(
        id="prediction:lower-curve",
        scenario="Снизить кривую отопления",
        expected_effect="Ожидается снижение расхода примерно на 8 м³ за интервал",
        confidence=0.45,
        confidence_basis="Погодная модель до ручного изменения",
        assumptions=["Расписание и ГВС не меняются"],
        verification="Снять два последующих показания счётчика",
    )]
    source_report.recommendations = [Recommendation(
        id="recommendation:lower-curve",
        title="Проверить меньшую кривую отопления",
        category="safe_user_setting",
        priority="low",
        confidence=0.45,
        hypothesis="При той же погоде расход уменьшится",
        suggested_manual_action="Изменить один параметр и дождаться следующего показания",
        expected_effect="Около 8 м³ экономии за два дня",
        observation_period_days=3,
    )]
    runtime.db.save_report(source_report, source_report.summary)

    store = OwnerContextStore(runtime.db)
    store.update_profile("1", {"fields": {"has_gas_stove": {"value": False}}})
    meter_value = 100.0
    store.update_gas(reports[1].id, {"value_m3": meter_value})
    for day, temperature in zip(reading_days[1:7], temperatures[:6], strict=True):
        meter_value = round(meter_value + _modeled_volume(temperature), 3)
        store.update_gas(reports[day].id, {"value_m3": meter_value})

    intervention_at = datetime(2026, 1, 14, tzinfo=UTC)
    owner_note = "Кривую снизил вручную; расписание не менял."
    feedback = runtime.db.set_recommendation_feedback(
        "recommendation:lower-curve",
        "applied",
        owner_note,
        {
            "category": "settings",
            "parameter": "PZA curve",
            "before": 1.2,
            "after": 1.0,
            "performed_at": intervention_at.isoformat(),
        },
    )
    intervention_id = feedback["intervention_id"]

    evaluation_end = datetime(2026, 1, 18, tzinfo=UTC)
    model_only = GasService(runtime.db, runtime.config).savings(evaluation_end)
    assert model_only["status"] == "available", model_only
    model_only_comparison = model_only["comparisons"][0]
    assert model_only_comparison["provenance"]["model_only"] is True
    assert model_only_comparison["provenance"]["validation"] == "только модель"
    assert model_only_comparison["owner_note"] == owner_note
    assert model_only_comparison["original_prediction"]["predictions"][0]["id"] == "prediction:lower-curve"
    assert model_only_comparison["original_prediction"]["hypothesis"] == "При той же погоде расход уменьшится"

    # The first reading after the intervention only closes a crossing interval;
    # the second creates the independent, whole after interval used for validation.
    meter_value = round(meter_value + _modeled_volume(temperatures[6]), 3)
    store.update_gas(reports[15].id, {"value_m3": meter_value})
    meter_value = round(meter_value + _modeled_volume(temperatures[7], saving_m3=8.0), 3)
    store.update_gas(reports[17].id, {"value_m3": meter_value})

    measured = GasService(runtime.db, runtime.config).savings(evaluation_end)
    assert measured["status"] == "available"
    measured_comparison = measured["comparisons"][0]
    assert measured_comparison["provenance"]["model_only"] is False
    assert measured_comparison["provenance"]["validation"] == "независимое показание после изменения"
    assert measured_comparison["normalized_savings"]["m3"] > 0
    assert measured_comparison["owner_note"] == owner_note
    assert measured_comparison["original_prediction"] == model_only_comparison["original_prediction"]
    assert measured_comparison["intervention_id"] == intervention_id
    assert measured_comparison["frozen_gas_model_version"] == model_only_comparison["frozen_gas_model_version"]

    frozen_weather = measured_comparison["frozen_weather_model"]
    assert frozen_weather["frozen"] is True
    assert frozen_weather["training_intervals"] == 6
    assert datetime.fromisoformat(frozen_weather["training_end"]) < intervention_at
    assert datetime.fromisoformat(frozen_weather["intervention_boundary"]) == intervention_at

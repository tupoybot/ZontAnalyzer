from __future__ import annotations

import ast
import calendar
import logging
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.openai.provider import Analyst, analysis_packet
from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.analytics import (
    analyze_dhw_interactions,
    assess_quality,
    build_heating_circuit_config,
    build_mode_catalog,
    burner_metrics,
    detect_burner_events,
    detect_control_context,
    detect_heating_availability,
    detect_temperature_events,
    temperature_metrics,
)
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import DetectedEvent, MetricValue, QualityResult, Recommendation, Report
from zont_analyzer.reports import render_text

logger = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self, db: Database, config: AppConfig, analyst: Analyst | None = None):
        self.db = db
        self.config = config
        self.analyst = analyst

    def local_day_window(self, selected: date) -> tuple[datetime, datetime]:
        timezone = ZoneInfo(self.config.home.timezone)
        start = datetime.combine(selected, time.min, timezone).astimezone(UTC)
        end = (datetime.combine(selected, time.min, timezone) + timedelta(days=1)).astimezone(UTC)
        return start, end

    @staticmethod
    def report_id_for(kind: str, start: datetime) -> str:
        return f"report:{kind}:{int(start.timestamp())}:report-v2"

    def analyze_daily(self, selected: date, *, use_ai: bool = True, kind: str = "daily") -> Report:
        start, end = self.local_day_window(selected)
        return self._analyze(start, end, kind=kind, use_ai=use_ai)

    def analyze_initial(self, *, use_ai: bool = True, days: int = 30) -> Report:
        if not 1 <= days <= 365:
            raise ValueError("Initial analysis period must be between 1 and 365 days")
        latest = self.db.latest_sample_time()
        end = (latest + timedelta(seconds=1)) if latest else datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(days=days)
        return self._analyze(start, end, kind="initial", use_ai=use_ai)

    def local_today(self) -> date:
        return datetime.now(ZoneInfo(self.config.home.timezone)).date()

    def analyze_week(self, year: int, week: int, *, use_ai: bool = True) -> Report:
        selected = date.fromisocalendar(year, week, 1)
        start, _ = self.local_day_window(selected)
        _, end = self.local_day_window(selected + timedelta(days=6))
        return self._analyze(start, end, kind="weekly", use_ai=use_ai)

    def analyze_month(self, year: int, month: int, *, use_ai: bool = True) -> Report:
        start, _ = self.local_day_window(date(year, month, 1))
        last_day = calendar.monthrange(year, month)[1]
        _, end = self.local_day_window(date(year, month, last_day))
        return self._analyze(start, end, kind="monthly", use_ai=use_ai)

    def analyze_season(self, year: int, season: str, *, use_ai: bool = True) -> Report:
        ranges = {
            "winter": (date(year - 1, 12, 1), date(year, 3, 1)),
            "spring": (date(year, 3, 1), date(year, 6, 1)),
            "summer": (date(year, 6, 1), date(year, 9, 1)),
            "autumn": (date(year, 9, 1), date(year, 12, 1)),
        }
        if season not in ranges:
            raise ValueError("Season must be winter, spring, summer, or autumn")
        first, after = ranges[season]
        start, _ = self.local_day_window(first)
        end, _ = self.local_day_window(after)
        return self._analyze(start, end, kind="seasonal", use_ai=use_ai)

    def _analyze(self, start: datetime, end: datetime, *, kind: str, use_ai: bool) -> Report:
        period_id = f"{kind}:{int(start.timestamp())}"
        context_start = start - timedelta(days=7)
        series = self.db.list_series()
        temperature_series = next(
            (item for item in series if item["role"] == "indoor_temperature"),
            None,
        )
        quality_series = temperature_series or next(
            (item for item in series if item["role"] == "temperature"),
            None,
        )
        burner_series = next(
            (item for item in series if item["role"] == "burner_activity" and item["metric_key"] == "flame"),
            next((item for item in series if item["role"] == "burner_activity"), None),
        )
        boiler_state_series = next(
            (item for item in series if item["source_type"] == "z3k_boiler_adapter" and item["metric_key"] == "s"),
            None,
        )
        target_series = next((item for item in series if item["role"] == "target_temperature"), None)
        mode_series = next(
            (
                item
                for item in series
                if item["role"] == "operating_mode"
                and target_series is not None
                and item["entity_id"] == target_series["entity_id"]
            ),
            None,
        )
        outdoor_series = next((item for item in series if item["role"] == "outdoor_temperature"), None)
        dhw_temperature_series = next((item for item in series if item["role"] == "dhw_temperature"), None)
        dhw_device_id = str(dhw_temperature_series["device_id"]) if dhw_temperature_series else ""

        def is_dhw_circuit(item: dict[str, Any]) -> bool:
            if item["source_type"] != "z3k_heating_circuit" or str(item["device_id"]) != dhw_device_id:
                return False
            label = f"{item.get('display_name', '')} {item.get('role', '')}".casefold()
            return any(term in label for term in ("гвс", "dhw", "hot water", "бойлер", "boiler tank"))

        dhw_target_series = next(
            (
                item
                for item in series
                if item["metric_key"] == "target_temp"
                and (item["role"] == "dhw_target_temperature" or is_dhw_circuit(item))
            ),
            None,
        )
        dhw_circuit_entity = str(dhw_target_series["entity_id"]) if dhw_target_series else ""

        def circuit_series(metric_key: str) -> dict[str, Any] | None:
            return next(
                (
                    item
                    for item in series
                    if dhw_circuit_entity
                    and item["entity_id"] == dhw_circuit_entity
                    and item["metric_key"] == metric_key
                ),
                None,
            )

        dhw_mode_series = circuit_series("mode_id")
        dhw_status_series = circuit_series("status")
        dhw_worktime_series = circuit_series("worktime")
        heating_worktime_series = next(
            (
                item
                for item in series
                if target_series is not None
                and item["entity_id"] == target_series["entity_id"]
                and item["metric_key"] == "worktime"
            ),
            None,
        )
        flow_temperature_series = next(
            (
                item
                for item in series
                if item["role"] == "flow_temperature"
                and (not dhw_device_id or str(item["device_id"]) == dhw_device_id)
            ),
            None,
        )
        temperature_samples = (
            self.db.fetch_samples(int(temperature_series["id"]), start, end) if temperature_series else []
        )
        burner_samples = self.db.fetch_samples(int(burner_series["id"]), start, end) if burner_series else []
        dhw_burner_samples: list[tuple[datetime, float]] = []
        burner_activity_scope = "generic_flame"
        if boiler_state_series:
            state_samples = self.db.fetch_text_samples(int(boiler_state_series["id"]), start, end)
            space_heating_samples = self._purpose_flame_samples(state_samples, "ch")
            dhw_burner_samples = self._purpose_flame_samples(state_samples, "dhw")
            if space_heating_samples:
                burner_samples = space_heating_samples
                burner_activity_scope = "space_heating_only"
        quality_samples = (
            self.db.fetch_samples(int(quality_series["id"]), start, end) if quality_series else burner_samples
        )
        quality = assess_quality(quality_samples, start, end)
        target_samples = self.db.fetch_samples(int(target_series["id"]), start, end) if target_series else []
        mode_samples = self.db.fetch_samples(int(mode_series["id"]), start, end) if mode_series else []
        circuit_id = str(target_series["entity_id"]).rsplit(":", 1)[-1] if target_series else ""
        device_id = str(target_series["device_id"]) if target_series else ""
        devices = self.db.list_devices()
        mode_catalog = build_mode_catalog(
            devices,
            device_id=device_id,
            circuit_id=circuit_id,
        )
        circuit_config = build_heating_circuit_config(
            devices,
            device_id=device_id,
            circuit_id=circuit_id,
        )
        dhw_circuit_id = dhw_circuit_entity.rsplit(":", 1)[-1] if dhw_circuit_entity else ""
        dhw_mode_catalog = build_mode_catalog(
            devices,
            device_id=dhw_device_id,
            circuit_id=dhw_circuit_id,
        )
        dhw_circuit_config = build_heating_circuit_config(
            devices,
            device_id=dhw_device_id,
            circuit_id=dhw_circuit_id,
        )
        control_events, control_context, transition_windows = detect_control_context(
            mode_samples=mode_samples,
            target_samples=target_samples,
            mode_catalog=mode_catalog,
            period_id=period_id,
            timezone=self.config.home.timezone,
        )
        status_series = next(
            (
                item
                for item in series
                if target_series is not None
                and item["entity_id"] == target_series["entity_id"]
                and item["metric_key"] == "status"
            ),
            None,
        )
        availability_modes = (
            self.db.fetch_samples(int(mode_series["id"]), context_start, end) if mode_series else mode_samples
        )
        status_samples = (
            self.db.fetch_samples(int(status_series["id"]), context_start, end) if status_series else []
        )
        availability_events, availability_context, inactive_windows = detect_heating_availability(
            start=start,
            end=end,
            mode_samples=availability_modes,
            status_samples=status_samples,
            mode_catalog=mode_catalog,
            circuit_config=circuit_config,
            period_id=period_id,
        )
        control_events.extend(availability_events)
        transition_windows.extend(
            (event.started_at, event.started_at + timedelta(hours=2)) for event in availability_events
        )
        control_context["heating_circuit"] = availability_context
        control_context["burner_activity_scope"] = burner_activity_scope
        dhw_temperature_samples = (
            self.db.fetch_samples(int(dhw_temperature_series["id"]), start, end)
            if dhw_temperature_series
            else []
        )
        dhw_target_samples = (
            self.db.fetch_samples(int(dhw_target_series["id"]), context_start, end) if dhw_target_series else []
        )
        dhw_mode_samples = (
            self.db.fetch_samples(int(dhw_mode_series["id"]), context_start, end) if dhw_mode_series else []
        )
        dhw_status_samples = (
            self.db.fetch_samples(int(dhw_status_series["id"]), context_start, end) if dhw_status_series else []
        )
        dhw_worktime_samples = (
            self.db.fetch_samples(int(dhw_worktime_series["id"]), context_start, end) if dhw_worktime_series else []
        )
        heating_worktime_samples = (
            self.db.fetch_samples(int(heating_worktime_series["id"]), context_start, end)
            if heating_worktime_series
            else []
        )
        heating_target_context_samples = (
            self.db.fetch_samples(int(target_series["id"]), context_start, end) if target_series else []
        )
        flow_temperature_samples = (
            self.db.fetch_samples(int(flow_temperature_series["id"]), start, end)
            if flow_temperature_series
            else []
        )
        interaction_state_samples: list[tuple[datetime, str | Collection[str]]] = list(
            self.db.fetch_text_samples(int(boiler_state_series["id"]), context_start, end)
            if boiler_state_series
            else []
        )
        availability_by_time: dict[datetime, float | bool] = {start: True}
        for inactive_start, inactive_end in inactive_windows:
            availability_by_time[inactive_start] = False
            availability_by_time[inactive_end] = True
        heating_available_samples = sorted(availability_by_time.items())
        target_c = self.config.preferences.target_temperature_c
        if target_c is None and target_series and target_samples:
            target_c = median(value for _timestamp, value in target_samples)
        effective_target_samples = [] if self.config.preferences.target_temperature_c is not None else target_samples
        metrics = temperature_metrics(
            temperature_samples,
            period_id=period_id,
            target_c=target_c,
            comfort_band_c=self.config.preferences.comfort_band_c,
            target_samples=effective_target_samples,
            ignore_windows=inactive_windows,
        )
        if outdoor_series:
            outdoor_samples = self.db.fetch_samples(int(outdoor_series["id"]), start, end)
            outdoor_metrics = temperature_metrics(
                outdoor_samples,
                period_id=f"{period_id}:outdoor",
                target_c=None,
                comfort_band_c=self.config.preferences.comfort_band_c,
            )
            for metric in outdoor_metrics:
                metric.name = f"outdoor_{metric.name}"
                metric.context = {"series_role": "outdoor_temperature"}
            metrics.extend(outdoor_metrics)
        events = detect_temperature_events(
            temperature_samples,
            period_id=period_id,
            target_c=target_c,
            comfort_band_c=self.config.preferences.comfort_band_c,
            target_samples=effective_target_samples,
            ignore_windows=[*transition_windows, *inactive_windows],
        )
        events.extend(control_events)
        if burner_series:
            space_heating_metrics = burner_metrics(
                burner_samples,
                period_id=period_id,
                period_hours=(end - start).total_seconds() / 3600,
                short_cycle_minutes=self.config.analysis.short_cycle_minutes,
                ignore_windows=[*transition_windows, *inactive_windows],
            )
            for metric in space_heating_metrics:
                metric.context["activity_scope"] = burner_activity_scope
            metrics.extend(space_heating_metrics)
            if dhw_burner_samples:
                dhw_metrics = burner_metrics(
                    dhw_burner_samples,
                    period_id=f"{period_id}:dhw",
                    period_hours=(end - start).total_seconds() / 3600,
                    short_cycle_minutes=self.config.analysis.short_cycle_minutes,
                )
                for metric in dhw_metrics:
                    metric.name = f"dhw_{metric.name}"
                    metric.context["activity_scope"] = "domestic_hot_water"
                metrics.extend(dhw_metrics)
            events.extend(
                detect_burner_events(
                    burner_samples,
                    period_id=period_id,
                    short_cycle_minutes=self.config.analysis.short_cycle_minutes,
                    context_windows=transition_windows,
                )
            )
        if dhw_temperature_series and boiler_state_series:
            dhw_quality = assess_quality(dhw_temperature_samples, start, end)
            dhw_analysis = analyze_dhw_interactions(
                period_id=period_id,
                period_start=start,
                period_end=end,
                boiler_state_samples=interaction_state_samples,
                dhw_temperature_samples=dhw_temperature_samples,
                dhw_target_samples=dhw_target_samples,
                dhw_mode_samples=dhw_mode_samples,
                dhw_status_samples=dhw_status_samples,
                dhw_worktime_samples=dhw_worktime_samples,
                dhw_mode_catalog=dhw_mode_catalog,
                dhw_circuit_config=dhw_circuit_config,
                heating_mode_samples=availability_modes,
                heating_status_samples=status_samples,
                heating_worktime_samples=heating_worktime_samples,
                heating_mode_catalog=mode_catalog,
                heating_available_samples=heating_available_samples,
                indoor_temperature_samples=temperature_samples,
                heating_target_samples=heating_target_context_samples,
                flow_temperature_samples=flow_temperature_samples,
                quality_score=dhw_quality.score,
                minimum_quality_score=self.config.analysis.minimum_quality_score,
                comfort_band_c=self.config.preferences.comfort_band_c,
            )
            metrics.extend(dhw_analysis.metrics)
            events.extend(dhw_analysis.events)
            control_context["dhw_interaction"] = {
                **dhw_analysis.context,
                "data_quality": dhw_quality.model_dump(mode="json"),
            }
        temperature_interpretation = self._annotate_temperature_attribution(metrics, events)
        if temperature_interpretation:
            control_context["temperature_above_setpoint_interpretation"] = temperature_interpretation
        events.sort(key=lambda item: item.started_at)
        recommendations = self._local_recommendations(quality, metrics, events)
        summary = self._local_summary(quality, metrics, events)
        dhw_summary = self._local_dhw_summary(metrics, control_context)
        if dhw_summary:
            summary = f"{summary} {dhw_summary}"
        if temperature_interpretation:
            duty = temperature_interpretation["burner_duty_cycle_pct"]
            summary = (
                f"{summary} Температура комнаты была выше уставки отопления, но горелка на отопление работала "
                f"только {duty:g}% наблюдаемого времени. Это не подтверждает перегрев от отопления: "
                "уставка включает нагрев снизу, но не охлаждает помещение при внешних теплопритоках."
            )
        if temperature_series is None:
            summary = f"{summary} Комнатный температурный ряд не определён; метрики комфорта не рассчитаны."
        ai_used = False
        should_use_ai = (
            use_ai
            and self.analyst is not None
            and quality.score >= self.config.analysis.minimum_quality_score
            and (
                kind == "initial"
                or self.config.analysis.daily_ai_when_normal
                or bool(recommendations)
                or any(event.severity != "info" for event in events)
            )
        )
        if should_use_ai:
            assert self.analyst is not None
            try:
                result = self.analyst.analyze(
                    analysis_packet(
                        quality=quality.model_dump(mode="json"),
                        metrics=metrics,
                        events=events,
                        period={
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "kind": kind,
                            "timezone": self.config.home.timezone,
                        },
                        context=control_context,
                        recommendation_feedback=self.db.recommendation_feedback(),
                    )
                )
                summary = result.summary
                recommendations = result.recommendations[: self.config.analysis.max_recommendations_per_report]
                ai_used = True
            except Exception as exc:
                logger.warning("OpenAI analysis failed; keeping deterministic report: %s", type(exc).__name__)
                summary = f"{summary} AI-интерпретация недоступна; сохранён локальный детерминированный отчёт."
        report_id = self.report_id_for(kind, start)
        report = Report(
            id=report_id,
            kind=kind,  # type: ignore[arg-type]
            period_start=start,
            period_end=end,
            generated_at=datetime.now(UTC),
            timezone=self.config.home.timezone,
            context=control_context,
            quality=quality,
            metrics=metrics,
            events=events,
            recommendations=recommendations,
            summary=summary,
            ai_used=ai_used,
        )
        self.db.save_report(report, render_text(report))
        return report

    @staticmethod
    def _purpose_flame_samples(
        samples: list[tuple[datetime, str]], purpose: str
    ) -> list[tuple[datetime, float]]:
        result: list[tuple[datetime, float]] = []
        for timestamp, encoded in samples:
            try:
                flags = ast.literal_eval(encoded)
            except (SyntaxError, ValueError):
                continue
            if isinstance(flags, list):
                selected = purpose in flags and "fl" in flags
                if purpose == "ch":
                    selected = selected and "dhw" not in flags
                elif purpose == "dhw":
                    selected = selected and "ch" not in flags
                result.append((timestamp, float(selected)))
        return result

    @staticmethod
    def _annotate_temperature_attribution(
        metrics: list[MetricValue], events: list[DetectedEvent]
    ) -> dict[str, float | str] | None:
        duty = next((item.value for item in metrics if item.name == "burner_duty_cycle_pct"), None)
        above_time = next((item.value for item in metrics if item.name == "time_above_target_band_pct"), 0.0)
        if duty is None or above_time <= 0 or duty >= 5:
            return None
        interpretation: dict[str, float | str] = {
            "classification": "room_temperature_above_heating_setpoint",
            "heating_causality": "not_supported_by_burner_activity",
            "burner_duty_cycle_pct": duty,
            "time_above_target_band_pct": above_time,
        }
        for metric in metrics:
            if metric.name in {
                "time_above_target_band_pct",
                "degree_hours_above_target",
                "mean_error_while_above_target_c",
            }:
                metric.context.update(interpretation)
        for event in events:
            if event.kind == "temperature_above_heating_setpoint":
                event.severity = "info"
                event.details.update(interpretation)
        return interpretation

    def _local_summary(self, quality: QualityResult, metrics: list[MetricValue], events: list[DetectedEvent]) -> str:
        if quality.score < self.config.analysis.minimum_quality_score:
            return "Данных недостаточно для надёжных выводов; сначала нужно восстановить наблюдаемость."
        warnings = [event for event in events if event.severity != "info"]
        if warnings:
            return f"Обнаружено {len(warnings)} заметных эпизодов; численные факты приведены ниже."
        if metrics:
            return "Данные достаточного качества; значимых локальных аномалий не обнаружено."
        return "Нет подходящих температурных рядов для расчёта метрик."

    @staticmethod
    def _local_dhw_summary(metrics: list[MetricValue], context: dict[str, Any]) -> str:
        dhw_context = context.get("dhw_interaction")
        if not isinstance(dhw_context, dict):
            return ""
        metric_values = {item.name: item.value for item in metrics}
        episodes = int(metric_values.get("dhw_episode_count", 0))
        quality = dhw_context.get("data_quality", {})
        quality_score = float(quality.get("score", 0)) if isinstance(quality, dict) else 0.0
        episode_word = (
            "эпизод"
            if episodes % 10 == 1 and episodes % 100 != 11
            else "эпизода"
            if episodes % 10 in {2, 3, 4} and episodes % 100 not in {12, 13, 14}
            else "эпизодов"
        )
        if not dhw_context.get("quality_sufficient_for_alerts", False):
            return (
                f"По ГВС найдено {episodes} {episode_word}, но отдельное качество данных ГВС "
                f"({quality_score:.0%}) недостаточно для предупреждений."
            )
        parts = [f"По ГВС найдено {episodes} {episode_word} догрева"]
        if "dhw_mean_recovery_minutes" in metric_values:
            parts.append(f"среднее восстановление {metric_values['dhw_mean_recovery_minutes']:g} мин")
        long_returns = int(metric_values.get("dhw_long_heating_return_count", 0))
        if long_returns:
            parts.append(f"подтверждённых долгих возвратов отопления: {long_returns}")
        else:
            parts.append("подтверждённых долгих возвратов отопления не найдено")
        return "; ".join(parts) + "."

    def _local_recommendations(
        self, quality: QualityResult, metrics: list[MetricValue], events: list[DetectedEvent]
    ) -> list[Recommendation]:
        if quality.score < self.config.analysis.minimum_quality_score:
            return [
                Recommendation(
                    id=None,
                    title="Сначала восстановить качество наблюдений",
                    category="observe_only",
                    priority="high",
                    confidence=0.95,
                    evidence_metric_ids=[],
                    evidence_event_ids=[],
                    hypothesis="Пробелы или некорректные показания делают сравнение отопления ненадёжным.",
                    suggested_manual_action=(
                        "Проверить связь контроллера и доступность показаний датчика; "
                        "настройки отопления пока не менять."
                    ),
                    expected_effect="Появится достаточный период данных для безопасного анализа.",
                    observation_period_days=1,
                    success_criteria=["Покрытие данных не ниже 70%", "Нет длительных пробелов"],
                    risks=["Изменение настроек сейчас может замаскировать причину"],
                    stop_conditions=["Показания физически неправдоподобны — обратиться к специалисту"],
                    alternatives=["Продолжить локальный сбор без рекомендаций"],
                    requires_specialist=False,
                )
            ]
        short_metric = next((m for m in metrics if m.name == "short_cycle_share_pct"), None)
        if short_metric and short_metric.value >= 30:
            evidence_events = [e.id for e in events if e.kind == "short_burner_cycle"][:10]
            return [
                Recommendation(
                    id=None,
                    title="Понаблюдать за повторяющимися короткими циклами",
                    category="observe_only",
                    priority="medium",
                    confidence=0.75,
                    evidence_metric_ids=[short_metric.id],
                    evidence_event_ids=evidence_events,
                    hypothesis="Доля коротких включений выше консервативного сигнального порога.",
                    suggested_manual_action=(
                        "Не менять параметры котла; сравнить ещё несколько сопоставимых суток "
                        "и проверить руководство оборудования."
                    ),
                    expected_effect="Станет понятно, устойчив ли сигнал и нужен ли сервисный осмотр.",
                    observation_period_days=self.config.analysis.default_experiment_days,
                    success_criteria=["Доля коротких циклов снижается либо подтверждается как устойчивая"],
                    risks=["Один день может быть несопоставим по погоде или режиму"],
                    stop_conditions=["Появилась ошибка котла или предупреждение безопасности"],
                    alternatives=["Передать evidence специалисту"],
                    requires_specialist=False,
                )
            ]
        return []

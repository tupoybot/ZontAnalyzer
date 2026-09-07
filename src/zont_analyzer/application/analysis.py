from __future__ import annotations

import ast
import calendar
import logging
from collections.abc import Collection
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo

from zont_analyzer.adapters.openai.provider import Analyst, analysis_packet
from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.analytics import (
    ReliabilityEvidencePoint,
    ReliabilityEvidenceSeries,
    analyze_dhw_interactions,
    analyze_reliability,
    assess_quality,
    build_heating_circuit_config,
    build_mode_catalog,
    burner_metrics,
    detect_burner_events,
    detect_control_context,
    detect_heating_availability,
    detect_temperature_events,
    detect_unconfirmed_burner_pulses,
    temperature_metrics,
)
from zont_analyzer.analytics.dhw import parse_opentherm_flags
from zont_analyzer.analytics.evidence import (
    ExclusionWindow,
    NumericSample,
    SignalMetadata,
    SignalSeries,
    StateSample,
    build_evidence,
)
from zont_analyzer.application.ingestion import _object_names, heating_circuit_sensor_links
from zont_analyzer.application.reasoning_context import reasoning_context, reasoning_payload
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue, QualityResult, Recommendation, Report
from zont_analyzer.reports import render_text

logger = logging.getLogger(__name__)


def _select_control_temperature_series(
    series: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    target_series: dict[str, Any] | None,
) -> dict[str, Any] | None:
    candidates = [item for item in series if item["role"] == "control_indoor_temperature"]
    configured = [item for item in candidates if item.get("provenance") == "config.entity_overrides"]
    if len(configured) == 1:
        return configured[0]
    if len(configured) > 1:
        return None

    if target_series is not None:
        circuit_id = str(target_series["entity_id"]).rsplit(":", 1)[-1]
        device_id = str(target_series["device_id"])
        links = heating_circuit_sensor_links(devices, _object_names(devices), series)
        linked_sensor_ids = {
            item.sensor_external_id
            for item in links
            if item.device_id == device_id and item.circuit_external_id == circuit_id
        }
        linked = [
            item
            for item in candidates
            if str(item["device_id"]) == device_id
            and str(item["entity_id"]).rsplit(":", 1)[-1] in linked_sensor_ids
        ]
        if len(linked) == 1:
            return linked[0]
        if len(linked) > 1:
            return None
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return None

    # Compatibility for pre-migration databases and synthetic tests. Multiple
    # legacy indoor rows remain deliberately unresolved instead of using order.
    legacy = [item for item in series if item["role"] == "indoor_temperature"]
    return legacy[0] if len(legacy) == 1 else None


def _sensor_report_context(
    series: list[dict[str, Any]], selected_control: dict[str, Any] | None
) -> dict[str, Any]:
    relevant_roles = {
        "control_indoor_temperature",
        "room_temperature",
        "technical_temperature",
        "humidity",
        "outdoor_temperature",
        "flow_temperature",
        "return_temperature",
        "dhw_temperature",
    }

    def compact(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "entity_id": str(item["entity_id"]),
            "external_id": str(item["entity_id"]).rsplit(":", 1)[-1],
            "display_name": str(item.get("display_name") or item["entity_id"]),
            "role": str(item["role"]),
            "source_type": str(item["source_type"]),
            "metric_key": str(item["metric_key"]),
            "unit": item.get("unit"),
            "origin": str(item.get("origin") or item["source_type"]),
            "confidence": float(item.get("confidence", 0.3)),
            "provenance": str(item.get("provenance") or "unknown"),
        }

    records = [compact(item) for item in series if item["role"] in relevant_roles]
    records.sort(key=lambda item: (item["role"], item["display_name"], item["entity_id"], item["metric_key"]))
    return {
        "control_resolution": "resolved" if selected_control is not None else "unresolved",
        "control_temperature": compact(selected_control) if selected_control is not None else None,
        "room_temperatures": [item for item in records if item["role"] == "room_temperature"],
        "technical_temperatures": [item for item in records if item["role"] == "technical_temperature"],
        "humidity": [item for item in records if item["role"] == "humidity"],
        "return_temperatures": [item for item in records if item["role"] == "return_temperature"],
        "other_temperature_sources": [
            item
            for item in records
            if item["role"] in {"outdoor_temperature", "flow_temperature", "dhw_temperature"}
        ],
    }


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

    def analyze_initial(self, *, use_ai: bool = True, days: int | None = None) -> Report:
        if days is not None and not 1 <= days <= 365:
            raise ValueError("Initial analysis period must be between 1 and 365 days")
        latest = self.db.latest_sample_time()
        end = (latest + timedelta(seconds=1)) if latest else datetime.now(UTC).replace(microsecond=0)
        start = (
            end - timedelta(days=days) if days is not None
            else self.db.earliest_sample_time() or end - timedelta(days=1)
        )
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
        devices = self.db.list_devices()
        burner_series = next(
            (item for item in series if item["role"] == "burner_activity" and item["metric_key"] == "flame"),
            next((item for item in series if item["role"] == "burner_activity"), None),
        )
        boiler_state_series = next(
            (item for item in series if item["source_type"] == "z3k_boiler_adapter" and item["metric_key"] == "s"),
            None,
        )
        zont_status_series = next(
            (item for item in series if item["source_type"] == "ztc_state" and item["metric_key"] == "status_flags"),
            None,
        )
        zont_heartbeat_series = next(
            (item for item in series if item["source_type"] == "ztc_state" and item["metric_key"] == "voltage"),
            zont_status_series,
        )
        target_series = next((item for item in series if item["role"] == "target_temperature"), None)
        temperature_series = _select_control_temperature_series(series, devices, target_series)
        quality_series = temperature_series
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
                if item["role"] == "flow_temperature" and (not dhw_device_id or str(item["device_id"]) == dhw_device_id)
            ),
            None,
        )
        temperature_samples = (
            self.db.fetch_samples(int(temperature_series["id"]), start, end) if temperature_series else []
        )
        flow_temperature_samples = (
            self.db.fetch_samples(int(flow_temperature_series["id"]), start, end) if flow_temperature_series else []
        )
        burner_samples = self.db.fetch_samples(int(burner_series["id"]), start, end) if burner_series else []
        dhw_burner_samples: list[tuple[datetime, float]] = []
        state_samples: list[tuple[datetime, str]] = []
        flame_noise_windows: list[tuple[datetime, datetime]] = []
        flame_noise_events: list[DetectedEvent] = []
        flame_noise_metrics: list[MetricValue] = []
        burner_activity_scope = "generic_flame"
        if boiler_state_series:
            state_samples = self.db.fetch_text_samples(int(boiler_state_series["id"]), start, end)
            if flow_temperature_samples:
                flame_noise = detect_unconfirmed_burner_pulses(
                    period_id=period_id,
                    boiler_state_samples=state_samples,
                    flow_temperature_samples=flow_temperature_samples,
                    maximum_pulse_minutes=2.0,
                )
                flame_noise_windows = flame_noise.ignored_windows
                flame_noise_events = flame_noise.events
                flame_noise_metrics = flame_noise.metrics
            space_heating_samples = self._purpose_flame_samples(
                state_samples,
                "ch",
                ignore_windows=flame_noise_windows,
            )
            dhw_burner_samples = self._purpose_flame_samples(
                state_samples,
                "dhw",
                ignore_windows=flame_noise_windows,
            )
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
        status_samples = self.db.fetch_samples(int(status_series["id"]), context_start, end) if status_series else []
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
        control_context["sensors"] = _sensor_report_context(series, temperature_series)
        control_context["recommendation_policy"] = "p2-1.7"
        from zont_analyzer.application.owner_context import OwnerContextStore

        owner_store = OwnerContextStore(self.db)
        control_context["equipment_profiles"] = [
            {
                "device_id": str(device["id"]),
                "fields": owner_store.profile(str(device["id"]), as_of=start)["fields"],
                "applicable_at": start.isoformat(),
                "changes_during_period": [
                    item for item in owner_store.profile(str(device["id"]), as_of=end)["history"]
                    if start < datetime.fromisoformat(item["effective_from"]).replace(tzinfo=UTC) < end
                ],
            }
            for device in devices
        ]
        dhw_temperature_samples = (
            self.db.fetch_samples(int(dhw_temperature_series["id"]), start, end) if dhw_temperature_series else []
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
        metrics.extend(flame_noise_metrics)
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
        events.extend(flame_noise_events)
        events.extend(control_events)
        history_start = self.db.earliest_sample_time() or context_start
        reliability_device_id = str(
            (boiler_state_series or burner_series or zont_status_series or {}).get("device_id", "")
        )
        source_events = [
            item
            for item in self.db.list_source_events(history_start, end)
            if not reliability_device_id or item.device_id == reliability_device_id
        ]
        boiler_reliability_series = boiler_state_series or burner_series or flow_temperature_series
        boiler_metric_timestamps = (
            self.db.fetch_sample_timestamps(int(boiler_reliability_series["id"]), history_start, end)
            if boiler_reliability_series
            else []
        )
        reliability_evidence: list[ReliabilityEvidenceSeries] = []
        if boiler_reliability_series:
            metric_key = str(boiler_reliability_series["metric_key"])
            role = str(boiler_reliability_series["role"])
            if boiler_reliability_series["source_type"] == "z3k_boiler_adapter" and metric_key == "s":
                role = "boiler_adapter_state"
            evidence_kind: Literal["activity", "thermal", "context"] = (
                "activity"
                if metric_key in {"s", "flame", "rml", "worktime"} or role == "burner_activity"
                else "thermal"
            )
            reliability_evidence.append(
                ReliabilityEvidenceSeries(
                    series_id=int(boiler_reliability_series["id"]),
                    role=role,
                    provenance=str(boiler_reliability_series["provenance"]),
                    origin=str(boiler_reliability_series["origin"]),
                    evidence_kind=evidence_kind,
                    points=tuple(
                        ReliabilityEvidencePoint(timestamp_utc=timestamp)
                        for timestamp in boiler_metric_timestamps
                    ),
                )
            )
        zont_status_samples = (
            self.db.fetch_samples(int(zont_status_series["id"]), history_start, end) if zont_status_series else []
        )
        zont_metric_timestamps = (
            self.db.fetch_sample_timestamps(int(zont_heartbeat_series["id"]), history_start, end)
            if zont_heartbeat_series
            else []
        )
        reliability = analyze_reliability(
            period_id=period_id,
            period_start=start,
            as_of=end,
            source_events=source_events,
            boiler_metric_timestamps=boiler_metric_timestamps,
            zont_status_samples=zont_status_samples,
            zont_metric_timestamps=zont_metric_timestamps,
            evidence_series=reliability_evidence,
        )
        metrics.extend(reliability.metrics)
        events.extend(reliability.events)
        control_context["reliability"] = reliability.context
        if burner_samples:
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
                recirculation_present=self.config.dhw.recirculation_present,
                ignored_state_windows=flame_noise_windows,
            )
            metrics.extend(dhw_analysis.metrics)
            events.extend(dhw_analysis.events)
            control_context["dhw_interaction"] = {
                **dhw_analysis.context,
                "data_quality": dhw_quality.model_dump(mode="json"),
            }
        temperature_interpretation = self._annotate_temperature_attribution(metrics, events)
        control_context["temporal_evidence"] = self._temporal_evidence(
            start=start, end=end, period_id=period_id, series=series,
            selected_control=temperature_series, selected_target=target_series,
            selected_boiler=boiler_state_series,
            transition_windows=transition_windows, inactive_windows=inactive_windows,
            noise_windows=flame_noise_windows, events=events,
        )
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
        report_id = self.report_id_for(kind, start)
        previous_report = self.db.report(report_id)
        ai_used = False
        reasoning = reasoning_payload(AnalysisResult(summary=""))
        control_context.update(reasoning_context(
            events,
            self.db.prior_reports(start),
            self.config.home.timezone,
            self.db.intervention_history(before=end),
        ))
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
                        recommendation_feedback=self.db.recommendation_feedback(before=end),
                    )
                )
                reasoning = reasoning_payload(result)
                summary = result.summary
                recommendations = result.recommendations[: self.config.analysis.max_recommendations_per_report]
                ai_used = True
            except Exception as exc:
                logger.warning("OpenAI analysis failed; keeping deterministic report: %s", type(exc).__name__)
                if (
                    previous_report is not None
                    and previous_report.ai_used
                    and previous_report.context.get("recommendation_policy") == "p2-1.7"
                ):
                    reasoning = reasoning_payload(previous_report)
                    summary = previous_report.summary
                    recommendations = previous_report.recommendations
                    ai_used = True
                    control_context["ai_interpretation_reuse"] = {
                        "source_generated_at": previous_report.generated_at.isoformat(),
                        "reason": "AI refresh failed validation; retained last valid interpretation",
                    }
                else:
                    summary = f"{summary} AI-интерпретация недоступна; сохранён локальный детерминированный отчёт."
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
            **reasoning,
        )
        self.db.save_report(report, render_text(report))
        return report

    def _temporal_evidence(
        self, *, start: datetime, end: datetime, period_id: str,
        series: list[dict[str, Any]], selected_control: dict[str, Any] | None,
        selected_target: dict[str, Any] | None, selected_boiler: dict[str, Any] | None,
        transition_windows: list[tuple[datetime, datetime]],
        inactive_windows: list[tuple[datetime, datetime]],
        noise_windows: list[tuple[datetime, datetime]], events: list[DetectedEvent],
    ) -> dict[str, Any]:
        # A bounded lookback supplies a preceding value, never a future sample.
        # Freshness/coverage is assessed independently by the evidence engine.
        lookback = start - timedelta(hours=6)

        def identity(item: dict[str, Any]) -> str:
            return "/".join(str(item[key]) for key in ("device_id", "source_type", "entity_id", "metric_key"))

        def unique(role: str) -> dict[str, Any] | None:
            candidates = [item for item in series if item["role"] == role]
            anchor = selected_target or selected_control or selected_boiler
            if anchor:
                candidates = [item for item in candidates if item["device_id"] == anchor["device_id"]]
            if selected_boiler and role in {"flow_temperature", "target_flow_temperature"}:
                candidates = [item for item in candidates if item["entity_id"] == selected_boiler["entity_id"]]
            overrides = [item for item in candidates if item.get("provenance") == "config.entity_overrides"]
            if role == "return_temperature" and not overrides:
                # External return is a distinct measurement, not the adapter's
                # potentially unsupported rwt placeholder. Keep ambiguous sensors unknown.
                candidates = [item for item in candidates if item["source_type"] != "z3k_boiler_adapter"]
            candidates = overrides or candidates
            return candidates[0] if len(candidates) == 1 else None

        chosen: dict[str, dict[str, Any] | None] = {
            "control_temperature": selected_control,
            "target_temperature": selected_target,
            **{role: unique(role) for role in (
                "outdoor_temperature", "flow_temperature", "return_temperature",
                "target_flow_temperature", "dhw_temperature", "recirculation",
            )},
        }
        modulation = [item for item in series if item["metric_key"] in {"rml", "modulation"}
                      and (selected_boiler is None or item["entity_id"] == selected_boiler["entity_id"])]
        chosen["modulation"] = modulation[0] if len(modulation) == 1 else None
        for item in series:
            if item["role"] == "room_temperature":
                chosen[f"room:{identity(item)}"] = item
            if (selected_target and item["entity_id"] == selected_target["entity_id"]
                    and item["metric_key"] in {"mode_id", "status"}):
                chosen[f"setting:{item['metric_key']}"] = item
        signals: list[SignalSeries] = []
        for key, selected in sorted(chosen.items()):
            if selected is None:
                continue
            item = selected
            role: Any = "room" if key.startswith("room:") else "other" if key.startswith("setting:") else key
            signals.append(SignalSeries(
                key=key,
                metadata=SignalMetadata(
                    identity=identity(item), display_name=str(item.get("display_name") or item["entity_id"]),
                    unit=str(item.get("unit") or "state"), role=role,
                    provenance=f"{item.get('origin', item['source_type'])}; {item.get('provenance', 'unknown')}",
                ),
                samples=tuple(NumericSample(timestamp, value)
                              for timestamp, value in self.db.fetch_samples(int(item["id"]), lookback, end)),
            ))
        exclusions = [
            *[ExclusionWindow(left, right, "transition") for left, right in transition_windows],
            *[ExclusionWindow(left, right, "inactive") for left, right in inactive_windows],
            *[ExclusionWindow(left, right, "noise") for left, right in noise_windows],
        ]
        for event in events:
            if (event.kind == "boiler_connection_loss"
                    and event.details.get("service_impact") != "confirmed_service_running"):
                exclusions.append(ExclusionWindow(event.started_at, event.ended_at or end, "reliability"))
            if event.kind in {"dhw_reheat_episode", "dhw_long_heating_return"} and event.ended_at:
                exclusions.append(ExclusionWindow(event.started_at, event.ended_at, "dhw"))
            if event.kind == "dhw_reheat_episode" and event.ended_at:
                hot_tail = event.details.get("facts", {}).get("hot_flow_tail_minutes")
                if isinstance(hot_tail, (int, float)) and hot_tail > 0:
                    exclusions.append(ExclusionWindow(
                        event.ended_at, event.ended_at + timedelta(minutes=hot_tail), "dhw",
                    ))
        states = [StateSample(timestamp, parse_opentherm_flags(encoded), identity(selected_boiler))
                  for timestamp, encoded in self.db.fetch_text_samples(int(selected_boiler["id"]), lookback, end)
                  ] if selected_boiler else []
        packet = build_evidence(
            start=start, end=end, timezone=self.config.home.timezone, period_id=period_id,
            signals=signals, state_samples=states, exclusions=exclusions,
            capability_profile=self.config.analysis.modulation_capability_profile,
            min_coverage_pct=self.config.analysis.minimum_quality_score * 100,
        )
        return packet.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _purpose_flame_samples(
        samples: list[tuple[datetime, str]],
        purpose: str,
        *,
        ignore_windows: list[tuple[datetime, datetime]] | None = None,
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
                if any(start <= timestamp < end for start, end in ignore_windows or []):
                    selected = False
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
        disabled_activity = int(metric_values.get("dhw_activity_while_disabled_count", 0))
        antilegionella = int(metric_values.get("dhw_antilegionella_cycle_count", 0))
        quality = dhw_context.get("data_quality", {})
        quality_score = float(quality.get("score", 0)) if isinstance(quality, dict) else 0.0
        circuit = dhw_context.get("dhw_circuit", {})
        currently_disabled = isinstance(circuit, dict) and circuit.get("current_enabled") is False
        episode_word = (
            "эпизод"
            if episodes % 10 == 1 and episodes % 100 != 11
            else "эпизода"
            if episodes % 10 in {2, 3, 4} and episodes % 100 not in {12, 13, 14}
            else "эпизодов"
        )
        if not dhw_context.get("quality_sufficient_for_alerts", False):
            state = "ГВС отключено выбранным режимом. " if currently_disabled else ""
            return (
                f"{state}По ГВС найдено {episodes} {episode_word}, но отдельное качество данных ГВС "
                f"({quality_score:.0%}) недостаточно для предупреждений."
            )
        parts = (
            ["ГВС отключено выбранным режимом", f"обычных эпизодов догрева: {episodes}"]
            if currently_disabled
            else [f"По ГВС найдено {episodes} {episode_word} догрева"]
        )
        if disabled_activity:
            parts.append(f"информационных сигналов активности при OFF: {disabled_activity}")
        if antilegionella:
            parts.append(f"вероятных штатных циклов антилегионеллы: {antilegionella}")
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
                    success_criteria=[
                        "Покрытие и интервалы данных достаточны для сопоставления", "Длительные пробелы устранены"
                    ],
                    risks=["Изменение настроек сейчас может замаскировать причину"],
                    stop_conditions=[
                        "Если показания остаются физически неправдоподобными, "
                        "не менять настройки до восстановления данных"
                    ],
                    alternatives=["Продолжить локальный сбор без изменения настроек"],
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
                    hypothesis=(
                        f"В выбранном периоде зафиксирована доля коротких включений {short_metric.value:g}% "
                        "; нужно проверить, сохраняется ли этот паттерн в сопоставимых периодах."
                    ),
                    suggested_manual_action=(
                        "Не менять параметры котла; сравнить ещё несколько сопоставимых суток "
                        "и проверить руководство оборудования."
                    ),
                    expected_effect="Станет понятно, устойчив ли наблюдаемый паттерн и требуется ли отдельный разбор.",
                    observation_period_days=self.config.analysis.default_experiment_days,
                    success_criteria=["Доля коротких циклов снижается либо подтверждается как устойчивая"],
                    risks=["Один день может быть несопоставим по погоде или режиму"],
                    stop_conditions=["Появилась ошибка котла или предупреждение безопасности"],
                    alternatives=["Продолжить наблюдение без изменения настроек"],
                    requires_specialist=False,
                )
            ]
        return []

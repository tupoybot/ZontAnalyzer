"""House history and manual outcomes built from the same operational evidence as reports."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from zont_analyzer.adapters.sqlite.database import Database, ReportRow
from zont_analyzer.analytics.evidence import EvidenceMetric
from zont_analyzer.application.period_comparison import ComparisonWindow, compare_periods, select_baseline
from zont_analyzer.domain import Report
from zont_analyzer.domain.periods import Period, calendar_period, season_period

COMFORT = {
    "time_above_target_band_pct",
    "time_below_target_band_pct",
    "mean_error_while_above_target_c",
    "dhw_mean_recovery_minutes",
    "dhw_long_heating_return_count",
}


def _when(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def window_from_report(report: Report, *, label: str = "") -> ComparisonWindow:
    evidence = report.context.get("temporal_evidence", {})
    metrics = [EvidenceMetric.model_validate(item) for item in evidence.get("metrics", [])]
    metrics.extend(
        EvidenceMetric(
            id=item.id,
            name=item.name,
            unit=item.unit,
            value=item.value,
            source="derived",
            coverage_pct=report.quality.coverage_pct,
        )
        for item in report.metrics
        if item.name in COMFORT
    )
    selected = [item for item in evidence.get("windows", []) if item.get("kind") != "representative"]
    if not selected:
        selected = evidence.get("windows", [])
    values: dict[str, float | str | None] = {}
    for name, key in (
        ("room_error_c", "room_error_c"),
        ("delta_t_c", "delta_t_c"),
        ("dhw_pct", "dhw_share_pct"),
        ("heating_request_pct", "heating_share_pct"),
    ):
        eligible = selected if name in {"dhw_pct", "heating_request_pct"} else [
            item for item in selected if not item.get("excluded_reasons")
        ]
        observed = [item.get("facts", {}).get(name, {}).get("mean") for item in eligible]
        numbers = [float(value) for value in observed if isinstance(value, (int, float))]
        values[key] = median(numbers) if numbers else None
        if name in {"room_error_c", "delta_t_c"}:
            metrics.append(
                EvidenceMetric(
                    id=f"comparison:{report.id}:{name}",
                    name=name,
                    value=median(numbers) if numbers else None,
                    unit="celsius",
                    source="derived",
                    unavailable_reason=None if numbers else "missing_aligned_samples",
                )
            )
    signal_quality = evidence.get("quality", {})
    weather = signal_quality.get("outdoor_temperature", {})
    values["outdoor_mean_c"] = weather.get("mean") if weather.get("coverage_pct", 0) >= 70 else None
    # Match observed hourly mode and target profiles, never today's discovery name.
    profiles: list[tuple[int, float, float]] = []
    for item in selected:
        signals = item.get("signals", {})
        mode_signal, target_signal = signals.get("setting:mode_id", {}), signals.get("target_temperature", {})
        if (mode_signal.get("coverage_pct", 0) >= 70 and target_signal.get("coverage_pct", 0) >= 70
                and mode_signal.get("mean") is not None and target_signal.get("mean") is not None):
            hour = _when(item["started_at"]).astimezone(ZoneInfo(report.timezone)).hour
            profiles.append((hour, round(mode_signal["mean"], 2), round(target_signal["mean"], 1)))
    values["mode"] = json.dumps(sorted(set(profiles))) if len(profiles) >= max(1, len(selected) * 0.7) else None
    return ComparisonWindow(
        label=label or f"{report.period_start.isoformat()} — {report.period_end.isoformat()}",
        start=report.period_start,
        end=report.period_end,
        context_values=values,
        mode=str(values["mode"]) if values.get("mode") is not None else None,
        quality_score=report.quality.score,
        coverage_pct=report.quality.coverage_pct,
        precomputed_metrics=tuple(metrics),
    )


def daily_history(
    db: Database,
    start: datetime,
    end: datetime,
    *,
    limit: int = 32,
    exclude_report_id: str | None = None,
) -> list[Report]:
    """Evenly spread days; load at most 32 canonical reports, never arbitrary raw telemetry."""
    with db.session() as session:
        rows = list(
            session.execute(
                select(ReportRow.id, ReportRow.period_start)
                .where(
                    ReportRow.kind == "daily",
                    ReportRow.period_start >= int(start.timestamp()),
                    ReportRow.period_end <= int(end.timestamp()),
                )
                .order_by(ReportRow.period_start)
            )
        )
    if exclude_report_id is not None:
        rows = [row for row in rows if str(row[0]) != exclude_report_id]
    if len(rows) > limit:
        rows = [rows[round(index * (len(rows) - 1) / (limit - 1))] for index in range(limit)]
    return [report for row in rows if (report := db.report(str(row[0]))) is not None]


def house_context(reports: list[Report]) -> dict[str, Any]:
    metrics: dict[str, list[float]] = {}
    room_pairs: dict[str, list[float]] = {}
    cooling: list[float] = []
    weather_response: list[dict[str, Any]] = []
    used: list[str] = []
    for report in reports:
        if report.quality.coverage_pct < 70:
            continue
        used.append(report.id)
        window = window_from_report(report)
        for item in window.precomputed_metrics:
            if item.value is not None:
                metrics.setdefault(item.name, []).append(item.value)
        weather_response.append(
            {
                "start": report.period_start.isoformat(),
                "outdoor_mean_c": window.context_values.get("outdoor_mean_c"),
                "room_error_c": window.context_values.get("room_error_c"),
                "mode": window.context_values.get("mode"),
            }
        )
        for item in report.context.get("temporal_evidence", {}).get("windows", []):
            if set(item.get("excluded_reasons", [])) - {"inactive"}:
                continue
            signals, facts = item.get("signals", {}), item.get("facts", {})
            reference = signals.get("control_temperature", {}).get("mean")
            for key, value in signals.items():
                if key.startswith("room:") and reference is not None and value.get("mean") is not None:
                    room_pairs.setdefault(key, []).append(value["mean"] - reference)
            slope = signals.get("control_temperature", {}).get("slope_per_hour")
            if facts.get("flame_pct", {}).get("mean") == 0 and isinstance(slope, (int, float)):
                cooling.append(slope)
    return {
        "epistemic_level": "derived",
        "selection": ("Up to 32 evenly spaced daily reports with coverage >=70%; "
                      "medians describe observed days, not an identified physical model"),
        "source_report_ids": used,
        "days": len(used),
        "typical_daily_metrics": {
            name: {"median": median(values), "minimum": min(values), "maximum": max(values), "days": len(values)}
            for name, values in metrics.items()
        },
        "weather_response": weather_response,
        "room_relative_to_control_c": {
            key: {"median": median(values), "windows": len(values)} for key, values in room_pairs.items()
        },
        "thermal_inertia": {
            "status": "observational_proxy" if cooling else "unknown",
            "room_drift_without_flame_c_per_hour": median(cooling) if cooling else None,
            "windows": len(cooling),
            "time_constant_hours": None,
            "limitations": "Heat gains, weather and unobserved demand confound cooling; no fitted time constant",
        },
        "occupancy": {
            "status": "unknown",
            "epistemic_level": "hypothesis",
            "policy": ("Owner statements take precedence; compare with and without inferred presence "
                       "if it changes the conclusion"),
        },
    }


def build_comparison_context(
    db: Database,
    report: Report,
    period: Period,
    *,
    boundaries: Any,
    analyze_window: Callable[[datetime, datetime], Report],
) -> dict[str, Any]:
    history_start = min(period.start, period.observed_end - timedelta(days=28))
    history = daily_history(db, history_start, period.observed_end, exclude_report_id=report.id)
    context: dict[str, Any] = {
        "house_context": house_context(history),
        "period_comparisons": [],
        "intervention_outcomes": [],
        "algorithm_version": "comparisons-v1",
    }
    interventions = db.intervention_history(limit=100, before=period.observed_end)
    times = [_when(item["temporal_boundary"]) for item in interventions]
    # A manual action needs an actual known time. Free text never supplies one by guessing.
    for item in interventions[:2]:
        experiment = item.get("experiment") or {}
        when = _when(item["temporal_boundary"])
        if when < period.observed_end - timedelta(days=60):
            continue
        outcome: dict[str, Any] = {
            "label": "Результат ручного изменения",
            "intervention_id": item["intervention_id"],
            "experiment": experiment,
            "epistemic_level": "derived",
            "hypothesis_assessment": "requires_ai_interpretation",
        }
        context["intervention_outcomes"].append(outcome)
        if not experiment.get("performed_at"):
            outcome["unavailable_reason"] = "Точное время изменения не указано; сравнение до/после недоступно."
            continue
        after_start = when + timedelta(days=1)
        duration = min(timedelta(days=3), period.observed_end - after_start)
        if duration < timedelta(hours=6):
            outcome["unavailable_reason"] = (
                "После изменения ещё нет окна наблюдения: пропущены первые 24 часа тепловой реакции."
            )
            continue
        before_report = analyze_window(when - duration, when)
        after_report = analyze_window(after_start, after_start + duration)
        outcome["selection"] = (
            "Равные окна до/после, до 3 суток; первые 24 часа после действия исключены. "
            "Погода, режим, ГВС и покрытие проверены отдельно."
        )
        outcome["comparison"] = compare_periods(
            window_from_report(before_report),
            window_from_report(after_report),
            intervention_at=when,
            interventions=times,
            timezone=report.timezone,
        ).model_dump(mode="json")
        recommendation = db.recommendation(item["recommendation_id"])
        original = db.report(recommendation["report_id"]) if recommendation else None
        captured = db.get_app_meta(f"intervention-prediction:{item['intervention_id']}")
        immutable = json.loads(captured) if captured else None
        if immutable and _when(immutable["generated_at"]) <= when:
            outcome["previous_prediction"] = immutable["predictions"]
            outcome["original_hypothesis"] = immutable.get("hypothesis")
        elif original and original.generated_at <= when:
            outcome["previous_prediction"] = [value.model_dump(mode="json") for value in original.predictions]
            outcome["original_hypothesis"] = recommendation.get("hypothesis") if recommendation else None
        else:
            outcome["previous_prediction"] = []
            outcome["prediction_status"] = "unknown: original pre-intervention report revision unavailable"
        outcome["owner_note"] = item.get("owner_note")
    baselines: list[tuple[str, Period]] = []
    if period.kind in {"weekly", "monthly"}:
        previous = calendar_period(
            "weekly" if period.kind == "weekly" else "monthly",
            (period.start - timedelta(days=1)).astimezone(ZoneInfo(report.timezone)).date(),
            report.timezone,
        )
        baselines.append(("Предыдущая неделя" if period.kind == "weekly" else "Предыдущий месяц", previous))
    elif period.kind == "seasonal" and period.year and period.season:
        pairs = (
            [("Эта весна", period.year, "spring"), ("Прошлая осень", period.year - 1, "autumn")]
            if period.season == "autumn"
            else [("Предыдущая осень", period.year - 1, "autumn"), ("Предыдущая весна", period.year - 1, "spring")]
            if period.season == "winter"
            else [("Тот же сезон год назад", period.year - 1, period.season)]
        )
        for label, year, season in pairs:
            baselines.append(
                (
                    label,
                    season_period(
                        year,
                        season,  # type: ignore[arg-type]
                        report.timezone,
                        boundaries,
                        as_of=period.start,
                    ),
                )
            )
    current_days = daily_history(db, period.start, period.observed_end, limit=16)
    for label, baseline in baselines:
        before_days = daily_history(db, baseline.start, baseline.observed_end, limit=32)
        comparison_item: dict[str, Any] = {
            "label": label,
            "baseline_period": baseline.model_dump(mode="json"),
            "current_period": period.model_dump(mode="json"),
            "epistemic_level": "derived",
        }
        context["period_comparisons"].append(comparison_item)
        pairs_found: list[dict[str, Any]] = []
        for current in reversed(current_days):
            target = window_from_report(current)
            selected = select_baseline(target, [window_from_report(value) for value in before_days])
            if selected:
                pairs_found.append(
                    compare_periods(selected, target, interventions=times, timezone=report.timezone).model_dump(
                        mode="json"
                    )
                )
            if len(pairs_found) == 3:
                break
        comparison_item["matched_windows"] = pairs_found
        comparison_item["selection"] = (
            "До трёх пар суток, одинаковые KPI; baseline выбран по погоде, режиму и покрытию. "
            "Это выборка, не эффект за весь сезон."
        )
        if pairs_found:
            comparison_item["comparison"] = pairs_found[0]
        else:
            comparison_item["unavailable_reason"] = (
                "Нет истории дневных отчётов за сравниваемый период."
                if not before_days
                else "Сопоставимые сутки по погоде, режиму и покрытию пока недоступны."
            )
    return context

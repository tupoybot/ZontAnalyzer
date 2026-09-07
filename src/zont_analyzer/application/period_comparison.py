"""Deterministic comparison of two bounded telemetry periods.

The comparison layer deliberately consumes prepared evidence windows.  Period
selection, database access and interpretation belong to callers; this module
only performs reproducible arithmetic and records reasons why a comparison is
not trustworthy.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from zont_analyzer.analytics.evidence import (
    EvidenceMetric,
    ExclusionWindow,
    SignalSeries,
    StateSample,
    build_evidence,
)


class ComparisonWindow(BaseModel):
    """A period prepared by the ingestion/analysis layer."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    start: datetime
    end: datetime
    signals: tuple[SignalSeries, ...] = ()
    states: tuple[StateSample, ...] = ()
    exclusions: tuple[ExclusionWindow, ...] = ()
    quality_score: float | None = Field(default=None, ge=0, le=1)
    coverage_pct: float | None = Field(default=None, ge=0, le=100)
    mode: str | None = None
    context_values: dict[str, float | str | None] = Field(default_factory=dict)
    # Callers that already have a deterministic report may pass its evidence
    # metrics.  Raw telemetry remains the preferred source.
    precomputed_metrics: tuple[EvidenceMetric, ...] = ()

    def model_post_init(self, __context: Any) -> None:
        if self.end <= self.start:
            raise ValueError("period end must be after start")


class ComparedMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    unit: str
    before: float | None
    after: float | None
    absolute_change: float | None
    relative_change_pct: float | None = None
    before_denominator: float | None = None
    after_denominator: float | None = None
    before_coverage_pct: float | None = None
    after_coverage_pct: float | None = None
    unavailable_reason: str | None = None


class PeriodQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_coverage_pct: float = Field(ge=0, le=100)
    after_coverage_pct: float = Field(ge=0, le=100)
    before_score: float | None = Field(default=None, ge=0, le=1)
    after_score: float | None = Field(default=None, ge=0, le=1)
    comparable: bool
    flags: list[str] = Field(default_factory=list)


class ContextComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: dict[str, float | str | None] = Field(default_factory=dict)
    after: dict[str, float | str | None] = Field(default_factory=dict)
    differences: dict[str, float | None] = Field(default_factory=dict)


class PeriodComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: str
    after: str
    status: str
    metrics: list[ComparedMetric] = Field(default_factory=list)
    context: ContextComparison
    quality: PeriodQuality
    confounders: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    before_start: datetime
    before_end: datetime
    after_start: datetime
    after_end: datetime
    timezone: str = "UTC"
    intervention_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    house_context: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class BaselineCandidate:
    window: ComparisonWindow
    weather_delta_c: float | None = None
    mode_match: bool | None = None
    dhw_delta_pct: float | None = None


def _metric_map(window: ComparisonWindow) -> dict[str, EvidenceMetric]:
    if window.precomputed_metrics:
        return {metric.name: metric for metric in window.precomputed_metrics}
    packet = build_evidence(
        start=window.start,
        end=window.end,
        timezone="UTC",
        signals=window.signals,
        state_samples=window.states,
        exclusions=window.exclusions,
        period_id=window.label,
        min_coverage_pct=0,
    )
    return {metric.name: metric for metric in packet.metrics}


def _coverage(window: ComparisonWindow) -> float:
    if window.coverage_pct is not None:
        return window.coverage_pct
    if not window.signals:
        return 0.0
    values: list[float] = []
    for series in window.signals:
        timestamps = sorted({sample.timestamp for sample in series.samples})
        if len(timestamps) < 2:
            values.append(0.0)
            continue
        observed = sum(
            (right - left).total_seconds()
            for left, right in zip(timestamps, timestamps[1:], strict=False)
            if left >= window.start and right <= window.end
        )
        values.append(min(100.0, observed / (window.end - window.start).total_seconds() * 100))
    return median(values) if values else 0.0


def _context(window: ComparisonWindow) -> dict[str, float | str | None]:
    result: dict[str, float | str | None] = dict(window.context_values)
    result.setdefault("mode", window.mode)
    for role, name in (
        ("outdoor_temperature", "outdoor_mean_c"),
        ("room", "room_mean_c"),
        ("return_temperature", "return_mean_c"),
    ):
        values = [
            sample.value
            for series in window.signals
            if series.metadata.role == role
            for sample in series.samples
        ]
        if name not in result:
            result[name] = round(sum(values) / len(values), 3) if values else None
    total = (window.end - window.start).total_seconds()
    dhw = 0.0
    heating = 0.0
    intervals = _state_intervals(window)
    for left, right, flags in intervals:
        seconds = (right - left).total_seconds()
        if "dhw" in flags:
            dhw += seconds
        if "ch" in flags and "dhw" not in flags:
            heating += seconds
    if "dhw_share_pct" not in result:
        result["dhw_share_pct"] = round(dhw / total * 100, 3) if intervals and total else None
    if "heating_share_pct" not in result:
        result["heating_share_pct"] = round(heating / total * 100, 3) if intervals and total else None
    return result


def _state_intervals(window: ComparisonWindow) -> list[tuple[datetime, datetime, frozenset[str]]]:
    states = sorted(window.states, key=lambda item: item.timestamp)
    result: list[tuple[datetime, datetime, frozenset[str]]] = []
    for left, right in zip(states, states[1:], strict=False):
        start, end = max(left.timestamp, window.start), min(right.timestamp, window.end)
        if start < end:
            result.append((start, end, left.flags))
    return result


def _compare_metric(before: EvidenceMetric | None, after: EvidenceMetric | None) -> ComparedMetric:
    metric = before or after
    assert metric is not None
    left, right = before.value if before else None, after.value if after else None
    absolute = right - left if left is not None and right is not None else None
    relative = None
    reason = (before.unavailable_reason if before and before.value is None else None) or (
        after.unavailable_reason if after and after.value is None else None
    )
    if left is not None and right is not None:
        ratio_units = {"ratio", "%", "count/hour", "1/h", "count"}
        if left != 0 and isfinite(left) and metric.unit in ratio_units:
            relative = absolute / abs(left) * 100 if absolute is not None else None
        elif metric.unit not in ratio_units:
            reason = reason or "relative_change_not_defined_for_absolute_unit"
        else:
            reason = reason or "relative_change_invalid_zero_denominator"
    return ComparedMetric(
        name=metric.name,
        unit=metric.unit,
        before=left,
        after=right,
        absolute_change=absolute,
        relative_change_pct=relative,
        before_denominator=before.denominator if before else None,
        after_denominator=after.denominator if after else None,
        before_coverage_pct=before.coverage_pct if before else None,
        after_coverage_pct=after.coverage_pct if after else None,
        unavailable_reason=reason,
    )


def _numeric_difference(left: Any, right: Any) -> float | None:
    return right - left if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None


def select_baseline(
    target: ComparisonWindow,
    candidates: Iterable[ComparisonWindow],
    *,
    max_weather_delta_c: float = 2.0,
    min_coverage_pct: float = 70.0,
) -> ComparisonWindow | None:
    """Choose the nearest sufficiently covered weather/mode comparable window."""
    target_context = _context(target)
    target_weather = target_context.get("outdoor_mean_c")
    if not isinstance(target_weather, (float, int)) or target.mode is None:
        return None
    if target.quality_score is not None and target.quality_score < 0.7:
        return None
    options: list[tuple[float, ComparisonWindow]] = []
    for candidate in candidates:
        if candidate.end > target.start or _coverage(candidate) < min_coverage_pct:
            continue
        if candidate.quality_score is not None and candidate.quality_score < 0.7:
            continue
        context = _context(candidate)
        weather = context.get("outdoor_mean_c")
        if not isinstance(weather, (float, int)):
            continue
        target_dhw, candidate_dhw = target_context.get("dhw_share_pct"), context.get("dhw_share_pct")
        if not isinstance(target_dhw, (float, int)) or not isinstance(candidate_dhw, (float, int)):
            continue
        if abs(target_dhw - candidate_dhw) > 15:
            continue
        if isinstance(target_weather, float) and isinstance(weather, float):
            if abs(weather - target_weather) > max_weather_delta_c:
                continue
        elif target_weather != weather:
            continue
        if target.mode is not None and candidate.mode != target.mode:
            continue
        distance = abs((target.start - candidate.end).total_seconds())
        options.append((distance, candidate))
    return min(options, key=lambda item: item[0])[1] if options else None


def compare_periods(
    before: ComparisonWindow,
    after: ComparisonWindow,
    *,
    intervention_at: datetime | None = None,
    interventions: Iterable[datetime] = (),
    min_coverage_pct: float = 70.0,
    weather_tolerance_c: float = 3.0,
    dhw_tolerance_pct: float = 15.0,
    timezone: str = "UTC",
) -> PeriodComparison:
    """Compare two windows and explicitly block confounded before/after pairs."""
    if after.start < before.end:
        raise ValueError("comparison windows must not overlap")
    confounders: list[str] = []
    unknowns: list[str] = []
    intervention_list = sorted(
        item for item in interventions
        if before.start <= item <= after.end and (intervention_at is None or item != intervention_at)
    )
    if intervention_list:
        confounders.append("second_intervention_between_windows")
    left_context, right_context = _context(before), _context(after)
    weather_left, weather_right = left_context["outdoor_mean_c"], right_context["outdoor_mean_c"]
    if isinstance(weather_left, (int, float)) and isinstance(weather_right, (int, float)):
        if abs(weather_right - weather_left) > weather_tolerance_c:
            confounders.append("weather_not_comparable")
    else:
        unknowns.append("weather_context_unavailable")
    dhw_left, dhw_right = left_context["dhw_share_pct"], right_context["dhw_share_pct"]
    if (
        isinstance(dhw_left, (int, float))
        and isinstance(dhw_right, (int, float))
        and abs(dhw_right - dhw_left) > dhw_tolerance_pct
    ):
        confounders.append("dhw_influence_not_comparable")
    elif dhw_left is None or dhw_right is None:
        unknowns.append("dhw_context_unavailable")
    mode_left, mode_right = left_context.get("mode"), right_context.get("mode")
    if mode_left is None or mode_right is None:
        unknowns.append("mode_context_unavailable")
    elif mode_left != mode_right:
        confounders.append("operating_mode_not_comparable")
    left_metrics, right_metrics = _metric_map(before), _metric_map(after)
    metrics = [_compare_metric(left_metrics[name], right_metrics.get(name)) for name in sorted(left_metrics)]
    metrics.extend(
        _compare_metric(None, right_metrics[name]) for name in sorted(set(right_metrics) - set(left_metrics))
    )
    left_coverage, right_coverage = _coverage(before), _coverage(after)
    flags: list[str] = []
    if left_coverage < min_coverage_pct:
        flags.append("before_low_coverage")
    if right_coverage < min_coverage_pct:
        flags.append("after_low_coverage")
    if before.quality_score is not None and before.quality_score < 0.7:
        flags.append("before_low_quality")
    if after.quality_score is not None and after.quality_score < 0.7:
        flags.append("after_low_quality")
    comparable = not confounders and not flags and not unknowns
    if not comparable:
        unknowns.append("comparison_not_isolated")
    return PeriodComparison(
        before=before.label,
        after=after.label,
        status="comparable" if comparable else "limited",
        metrics=metrics,
        context=ContextComparison(
            before=left_context,
            after=right_context,
            differences={
                key: _numeric_difference(left_context.get(key), right_context.get(key))
                for key in set(left_context) | set(right_context)
                if key != "mode"
            },
        ),
        quality=PeriodQuality(
            before_coverage_pct=left_coverage,
            after_coverage_pct=right_coverage,
            before_score=before.quality_score,
            after_score=after.quality_score,
            comparable=comparable,
            flags=flags,
        ),
        confounders=confounders,
        unknowns=unknowns,
        before_start=before.start,
        before_end=before.end,
        after_start=after.start,
        after_end=after.end,
        timezone=timezone,
    )


class _PeriodLike(Protocol):
    start: datetime
    end: datetime
    kind: str
    label: str


def _as_window(value: Any, *, label: str, analyze_window: Callable[..., Any]) -> ComparisonWindow:
    """Adapt a Period and a callback result without depending on domain DTOs."""
    observed_end = getattr(value, "observed_end", value.end)
    result = analyze_window(value.start, observed_end, value.kind)
    if isinstance(result, ComparisonWindow):
        return result.model_copy(update={"label": label})
    # A Report has the same deterministic MetricValue objects used by the
    # application.  It cannot provide raw signal context, so quality is kept
    # explicit and missing weather/DHW context remains unknown.
    quality = getattr(result, "quality", None)
    temporal = getattr(result, "context", {}).get("temporal_evidence", {})
    temporal_metrics = tuple(
        EvidenceMetric.model_validate(item)
        for item in temporal.get("metrics", ())
        if isinstance(item, dict) and item.get("name")
    )
    legacy_metrics = tuple(
        EvidenceMetric(
            id=item.id,
            name=item.name,
            value=item.value,
            unit=item.unit,
            source="derived",
            coverage_pct=float(getattr(quality, "coverage_pct", 0.0)) if quality else 0.0,
        )
        for item in getattr(result, "metrics", ())
    )
    known = {item.name for item in temporal_metrics}
    metrics = temporal_metrics + tuple(item for item in legacy_metrics if item.name not in known)
    context_values: dict[str, float | str | None] = {}
    packet_signals = temporal.get("signals", {}) if isinstance(temporal, dict) else {}
    packet_windows = temporal.get("windows", ()) if isinstance(temporal, dict) else ()
    for key, metadata in packet_signals.items():
        role = metadata.get("role") if isinstance(metadata, dict) else None
        if not isinstance(role, str):
            continue
        target = {
            "outdoor_temperature": "outdoor_mean_c",
            "return_temperature": "return_mean_c",
            "room": "room_mean_c",
        }.get(role)
        if target is None:
            continue
        means: list[float] = []
        for packet_window in packet_windows:
            item = packet_window.get("signals", {}).get(key, {})
            if isinstance(item, dict) and isinstance(item.get("mean"), (int, float)):
                means.append(float(item["mean"]))
        if means:
            context_values[target] = round(sum(means) / len(means), 3)
    exclusions = temporal.get("exclusions", {}) if isinstance(temporal, dict) else {}
    if isinstance(exclusions, dict) and temporal.get("period_start") and temporal.get("period_end"):
        seconds = (
            datetime.fromisoformat(temporal["period_end"]) - datetime.fromisoformat(temporal["period_start"])
        ).total_seconds()
        if seconds > 0 and isinstance(exclusions.get("dhw"), (int, float)):
            context_values["dhw_share_pct"] = round(exclusions["dhw"] / seconds * 100, 3)
    return ComparisonWindow(
        label=label,
        start=value.start,
        end=observed_end,
        quality_score=(float(quality.score) if quality else None),
        coverage_pct=(float(quality.coverage_pct) if quality else None),
        context_values=context_values,
        precomputed_metrics=metrics,
    )


def build_period_context(
    *,
    period: _PeriodLike,
    baseline_periods: Iterable[_PeriodLike] = (),
    analyze_window: Callable[..., Any],
    intervention_at: datetime | None = None,
    interventions: Iterable[datetime] = (),
    timezone: str = "UTC",
    max_analyses: int = 12,
) -> dict[str, Any]:
    """Build bounded comparison context for a long-period report.

    ``analyze_window`` is supplied by the application and must be deterministic
    (normally ``AnalysisService._analyze(..., persist=False, use_ai=False)`` or
    a direct evidence builder).  The callback is invoked at most
    ``max_analyses`` times, and no AI or persistence is performed here.
    """
    if max_analyses < 1:
        raise ValueError("max_analyses must be positive")
    selected = list(baseline_periods)[: max_analyses - 1]
    current = _as_window(period, label=getattr(period, "label", period.kind), analyze_window=analyze_window)
    comparisons: list[dict[str, Any]] = []
    for candidate in selected:
        baseline = _as_window(
            candidate,
            label=getattr(candidate, "label", candidate.kind),
            analyze_window=analyze_window,
        )
        comparisons.append(
            compare_periods(
                baseline,
                current,
                intervention_at=intervention_at,
                interventions=interventions,
                timezone=timezone,
            ).model_dump(mode="json")
        )
    return {
        "period_comparisons": comparisons,
        "baseline_count": len(comparisons),
        "analysis_budget": {"requested": len(selected) + 1, "maximum": max_analyses},
    }


__all__ = [
    "BaselineCandidate",
    "ComparedMetric",
    "ComparisonWindow",
    "ContextComparison",
    "PeriodComparison",
    "PeriodQuality",
    "compare_periods",
    "build_period_context",
    "select_baseline",
]

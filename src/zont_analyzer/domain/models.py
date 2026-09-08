from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelemetryPoint(DomainModel):
    device_id: str
    source_type: str
    entity_id: str
    metric_key: str
    timestamp_utc: datetime
    value_num: float | None = None
    value_text: str | None = None
    unit: str | None = None
    quality: Literal["valid", "invalid"] = "valid"


class SourceEvent(DomainModel):
    id: str
    device_id: str
    event_type: str
    timestamp_utc: datetime
    duration_seconds: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    important: bool = False


class QualityResult(DomainModel):
    score: float = Field(ge=0, le=1)
    coverage_pct: float = Field(ge=0, le=100)
    max_gap_seconds: float = Field(ge=0)
    stuck_pct: float = Field(ge=0, le=100)
    implausible_jumps: int = Field(ge=0)
    sample_count: int = Field(ge=0)
    flags: list[str] = Field(default_factory=list)


class MetricValue(DomainModel):
    id: str
    name: str
    value: float
    unit: str
    algorithm_version: str = "metrics-v2"
    context: dict[str, Any] = Field(default_factory=dict)


class DetectedEvent(DomainModel):
    id: str
    kind: str
    started_at: datetime
    ended_at: datetime | None = None
    severity: Literal["info", "warning", "critical"] = "info"
    details: dict[str, Any] = Field(default_factory=dict)
    algorithm_version: str = "events-v2"


RecommendationCategory = Literal[
    "observe_only",
    "safe_user_setting",
    "needs_manual_context",
    "service_required",
    "safety_warning",
]


class Recommendation(DomainModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=160)
    category: RecommendationCategory
    priority: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0, le=1)
    evidence_metric_ids: list[str] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    hypothesis: str
    suggested_manual_action: str
    expected_effect: str
    observation_period_days: int = Field(ge=1, le=60)
    success_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    requires_specialist: bool = False


class AnalysisResult(DomainModel):
    summary: str
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=3)
    observed_patterns: list[ObservedPattern] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    recommended_experiment: RecommendedExperiment | None = None
    # Provider metadata belongs to the successful response, rather than to the
    # active configuration.  The analysis service persists it with the report.
    provenance: dict[str, Any] | None = None


class Report(DomainModel):
    id: str
    kind: Literal["initial", "daily", "weekly", "monthly", "seasonal"]
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    timezone: str = "UTC"
    context: dict[str, Any] = Field(default_factory=dict)
    quality: QualityResult
    metrics: list[MetricValue] = Field(default_factory=list)
    events: list[DetectedEvent] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    summary: str
    observed_patterns: list[ObservedPattern] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    recommended_experiment: RecommendedExperiment | None = None
    ai_used: bool = False
    algorithm_version: str = "report-v2"


from .reasoning import (  # noqa: E402  # Resolve the Pydantic forward references above.
    Hypothesis,
    ObservedPattern,
    Prediction,
    RecommendedExperiment,
    Unknown,
)

AnalysisResult.model_rebuild()
Report.model_rebuild()

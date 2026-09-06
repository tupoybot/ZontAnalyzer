"""Typed, storage-safe contract for AI reasoning attached to a report.

The objects deliberately validate shape only.  In particular, an evidence ID is
allowed to be unknown here so a structurally valid model response remains
available for review and the renderer can label that reference honestly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .models import DomainModel


class TimeInterval(DomainModel):
    """A time interval supplied by the analyst, with its display timezone."""

    started_at: datetime
    ended_at: datetime
    timezone: str = Field(min_length=1)

    @field_validator("started_at", "ended_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def end_must_follow_start(self) -> TimeInterval:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        return self


class EvidenceReference(DomainModel):
    """Reference to a metric, event, or temporal-evidence item by its ID."""

    id: str = Field(min_length=1)


class ObservedPattern(DomainModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    interval: TimeInterval | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)
    epistemic_level: Literal["observed", "derived"]


class Hypothesis(DomainModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    interval: TimeInterval | None = None
    confidence: float = Field(ge=0, le=1)
    confidence_basis: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_for: list[EvidenceReference] = Field(default_factory=list)
    evidence_against: list[EvidenceReference] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    epistemic_level: Literal["inferred"] = "inferred"


class Prediction(DomainModel):
    id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    confidence_basis: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    verification: str = Field(min_length=1)
    epistemic_level: Literal["predicted"] = "predicted"


class Unknown(DomainModel):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    interval: TimeInterval | None = None
    evidence: list[EvidenceReference] = Field(default_factory=list)


class RecommendedExperiment(DomainModel):
    """One reversible, manual experiment proposed by the analyst."""

    id: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    current_value: str = Field(min_length=1)
    proposed_change: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    observation_period: str = Field(min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

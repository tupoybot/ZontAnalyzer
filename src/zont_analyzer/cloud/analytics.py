"""Bounded, storage-free analytics callable for the M2 runtime."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zont_analyzer.analytics.events import detect_temperature_events
from zont_analyzer.analytics.metrics import temperature_metrics
from zont_analyzer.analytics.quality import assess_quality

MAX_SAMPLES = 1_000
MAX_PERIOD = timedelta(hours=24)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    value: float

    @field_validator("timestamp")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class TargetSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    value: float | None

    @field_validator("timestamp")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class AnalyticsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_start: datetime
    period_end: datetime
    samples: list[Sample] = Field(min_length=2, max_length=MAX_SAMPLES)
    target_c: float | None = Field(default=None, ge=5, le=35)
    target_samples: list[TargetSample] = Field(default_factory=list, max_length=MAX_SAMPLES)
    comfort_band_c: float = Field(default=0.5, gt=0, le=5)
    period_id: str = Field(default="cloud-analytics", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")

    @field_validator("period_start", "period_end")
    @classmethod
    def aware_period(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("period boundaries must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_ordered_input(self) -> AnalyticsInput:
        if self.period_end <= self.period_start:
            raise ValueError("period_end must follow period_start")
        if self.period_end - self.period_start > MAX_PERIOD:
            raise ValueError("period must not exceed 24 hours")
        self._validate_series([item.timestamp for item in self.samples], "samples")
        self._validate_series([item.timestamp for item in self.target_samples], "target_samples")
        return self

    def _validate_series(self, timestamps: list[datetime], name: str) -> None:
        previous: datetime | None = None
        for timestamp in timestamps:
            if timestamp < self.period_start or timestamp > self.period_end:
                raise ValueError(f"{name} timestamp is outside the period")
            if previous is not None and timestamp <= previous:
                raise ValueError(f"{name} timestamps must be strictly increasing")
            previous = timestamp


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    """Run existing deterministic temperature analytics without storage or files."""
    request = AnalyticsInput.model_validate(payload)
    samples = [(item.timestamp, item.value) for item in request.samples]
    target_samples = [(item.timestamp, item.value) for item in request.target_samples]
    quality = assess_quality(samples, request.period_start, request.period_end)
    metrics = temperature_metrics(
        samples,
        period_id=request.period_id,
        target_c=request.target_c,
        comfort_band_c=request.comfort_band_c,
        target_samples=target_samples,
    )
    events = detect_temperature_events(
        samples,
        period_id=request.period_id,
        target_c=request.target_c,
        comfort_band_c=request.comfort_band_c,
        target_samples=target_samples,
    )
    return {
        "quality": quality.model_dump(mode="json"),
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "events": [item.model_dump(mode="json") for item in events],
    }

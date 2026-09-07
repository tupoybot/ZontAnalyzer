"""Calendar contract shared by analysis, scheduling, comparison and publication."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

Season = Literal["winter", "spring", "summer", "autumn"]
SEASONS: tuple[Season, ...] = ("spring", "summer", "autumn", "winter")
PERIOD_VERSION = "period-v1"


class SeasonBoundaries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spring: str = "03-01"
    summer: str = "06-01"
    autumn: str = "09-01"
    winter: str = "12-01"

    @field_validator("spring", "summer", "autumn", "winter")
    @classmethod
    def month_day(cls, value: str) -> str:
        # Feb 29 cannot be an annual boundary in non-leap years.
        parsed = date.fromisoformat(f"2001-{value}")
        if parsed.strftime("%m-%d") != value:
            raise ValueError("Season boundary must use MM-DD")
        return value

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if not self.spring < self.summer < self.autumn < self.winter:
            raise ValueError("Season starts must follow spring < summer < autumn < winter")
        return self


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["daily", "weekly", "monthly", "seasonal", "initial"]
    start: datetime
    end: datetime
    observed_end: datetime
    timezone: str
    complete: bool
    season: Season | None = None
    year: int | None = None
    boundary_source: str = "local_calendar"
    algorithm_version: str = PERIOD_VERSION

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        ZoneInfo(self.timezone)
        if any(value.tzinfo is None for value in (self.start, self.end, self.observed_end)):
            raise ValueError("Period boundaries must be timezone-aware")
        if not self.start < self.observed_end <= self.end:
            raise ValueError("Period must have a positive observed interval within its boundaries")
        if self.complete != (self.observed_end == self.end):
            raise ValueError("Period completeness does not match observed end")
        return self


def midnight(day: date, timezone: str) -> datetime:
    return datetime.combine(day, time.min, ZoneInfo(timezone)).astimezone(UTC)


def calendar_period(kind: Literal["daily", "weekly", "monthly"], selected: date, timezone: str) -> Period:
    if kind == "daily":
        first, after = selected, selected + timedelta(days=1)
    elif kind == "weekly":
        first = selected - timedelta(days=selected.weekday())
        after = first + timedelta(days=7)
    elif kind == "monthly":
        first = selected.replace(day=1)
        after = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        raise ValueError("Use season_period for seasons")
    end = midnight(after, timezone)
    return Period(
        kind=kind,
        start=midnight(first, timezone),
        end=end,
        observed_end=end,
        timezone=timezone,
        complete=True,
    )


def season_period(
    year: int,
    season: Season,
    timezone: str,
    boundaries: SeasonBoundaries,
    *,
    as_of: datetime,
    source: str = "home_config",
) -> Period:
    # Winter is named for its ending year, preserving the original CLI contract.
    start_year = year - 1 if season == "winter" else year
    first = date.fromisoformat(f"{start_year}-{getattr(boundaries, season)}")
    index = SEASONS.index(season)
    next_season = SEASONS[(index + 1) % 4]
    after = date.fromisoformat(f"{start_year + (season == 'winter')}-{getattr(boundaries, next_season)}")
    start, end = midnight(first, timezone), midnight(after, timezone)
    observed_end = min(end, as_of)
    return Period(
        kind="seasonal",
        start=start,
        end=end,
        observed_end=observed_end,
        timezone=timezone,
        complete=observed_end == end,
        season=season,
        year=year,
        boundary_source=source,
    )


def active_season(day: date, boundaries: SeasonBoundaries) -> tuple[int, Season]:
    month_day = day.strftime("%m-%d")
    for season in reversed(SEASONS):
        if month_day >= getattr(boundaries, season):
            return day.year + (season == "winter"), season
    return day.year, "winter"

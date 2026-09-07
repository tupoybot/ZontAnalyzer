from datetime import UTC, date, datetime

import pytest

from zont_analyzer.domain.periods import (
    Period,
    SeasonBoundaries,
    calendar_period,
    season_period,
)


def test_calendar_month_handles_leap_day_and_dst_in_local_timezone() -> None:
    period = calendar_period("monthly", date(2024, 2, 15), "America/New_York")
    assert period.start == datetime(2024, 2, 1, 5, tzinfo=UTC)
    assert period.end == datetime(2024, 3, 1, 5, tzinfo=UTC)
    assert (period.end - period.start).days == 29

    dst_week = calendar_period("weekly", date(2024, 3, 4), "America/New_York")
    assert (dst_week.end - dst_week.start).total_seconds() == 6 * 86400 + 23 * 3600


def test_custom_season_boundaries_are_used_and_source_is_preserved() -> None:
    boundaries = SeasonBoundaries(spring="02-15", summer="05-15", autumn="08-15", winter="11-15")
    period = season_period(
        2026,
        "autumn",
        "UTC",
        boundaries,
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
        source="owner_profile:device-1",
    )
    assert period.start == datetime(2026, 8, 15, tzinfo=UTC)
    assert period.end == datetime(2026, 11, 15, tzinfo=UTC)
    assert period.observed_end == datetime(2026, 9, 1, tzinfo=UTC)
    assert period.complete is False
    assert period.boundary_source == "owner_profile:device-1"


def test_period_rejects_naive_or_inconsistent_observation_boundaries() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Period(
            kind="daily",
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2),
            observed_end=datetime(2026, 1, 2),
            timezone="UTC",
            complete=True,
        )
    with pytest.raises(ValueError, match="completeness"):
        Period(
            kind="daily",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            observed_end=datetime(2026, 1, 1, 12, tzinfo=UTC),
            timezone="UTC",
            complete=True,
        )

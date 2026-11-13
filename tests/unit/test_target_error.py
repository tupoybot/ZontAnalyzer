from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.metrics import temperature_metrics
from zont_analyzer.domain import MetricValue


def _metrics(values: list[tuple[float, float]], *, band: float = 0.5) -> dict[str, MetricValue]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = temperature_metrics(
        [(start + timedelta(hours=hour), temperature) for hour, temperature in values],
        period_id="target-error",
        target_c=20.0,
        comfort_band_c=band,
    )
    return {metric.name: metric for metric in metrics}


def test_above_mean_uses_same_time_intervals_with_uneven_sampling() -> None:
    metrics = _metrics([(0, 21.0), (1, 22.0), (3, 20.0)])

    # One hour at +1 C and two hours at +2 C: 5 degree-hours / 3 hours.
    assert metrics["mean_error_while_above_target_c"].value == pytest.approx(5 / 3, abs=0.001)
    assert metrics["mean_error_while_above_target_c"].value <= 2.0
    assert metrics["mean_error_while_above_target_c"].context == {
        "comfort_band_c": 0.5,
        "average_deviation_basis": "exact_target",
        "eligible_intervals": "same_sign_relative_to_exact_target",
        "numerator": "degree_hours_on_eligible_intervals",
        "denominator": "same_sign_hours_relative_to_exact_target",
    }


def test_comfort_band_boundary_is_included_in_above_mean() -> None:
    metrics = _metrics([(0, 20.5), (1, 21.5), (2, 20.0)])

    # The first hour is exactly on the inclusive band boundary. The average
    # still covers all time above the exact target: (0.5 + 1.5) / 2.
    assert metrics["time_above_target_band_pct"].value == pytest.approx(50.0)
    assert metrics["mean_error_while_above_target_c"].value == pytest.approx(1.0)


def test_above_mean_includes_deviations_inside_band() -> None:
    metrics = _metrics([(0, 20.25), (1, 21.0), (2, 22.0), (3, 20.0)])

    # All three hours are above the exact target: (0.25 + 1 + 2) / 3.
    assert metrics["mean_error_while_above_target_c"].value == pytest.approx(3.25 / 3, abs=0.001)


def test_sensor_gap_is_excluded_from_both_mean_numerator_and_denominator() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = temperature_metrics(
        [(start, 21.0), (start + timedelta(minutes=10), 21.0),
         (start + timedelta(hours=8), 21.0), (start + timedelta(hours=8, minutes=10), 21.0)],
        period_id="target-gap",
        target_c=20.0,
        comfort_band_c=0.5,
    )
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["mean_error_while_above_target_c"].value == pytest.approx(1.0)
    assert by_name["degree_hours_above_target"].value == pytest.approx(1 / 3, abs=0.001)


def test_no_above_target_time_omits_above_mean() -> None:
    metrics = _metrics([(0, 20.0), (1, 20.0), (2, 20.0)])

    assert "mean_error_while_above_target_c" not in metrics


def test_below_mean_is_symmetric_and_bounded_by_maximum_deficit() -> None:
    metrics = _metrics([(0, 19.0), (1, 18.0), (3, 20.0)])

    # One hour at -1 C and two hours at -2 C: 5 degree-hours / 3 hours.
    assert metrics["mean_error_while_below_target_c"].value == pytest.approx(5 / 3, abs=0.001)
    assert metrics["mean_error_while_below_target_c"].value <= 2.0


def test_below_mean_includes_deviations_inside_band() -> None:
    metrics = _metrics([(0, 19.75), (1, 19.0), (2, 18.0), (3, 20.0)])

    assert metrics["mean_error_while_below_target_c"].value == pytest.approx(3.25 / 3, abs=0.001)

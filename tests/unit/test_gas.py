from datetime import datetime, timedelta

import pytest

from zont_analyzer.analytics.gas import (
    Exposure,
    GasInterval,
    MeterReading,
    StateSample,
    build_intervals,
    estimate_exposure,
    estimate_gas,
    estimate_gas_purpose_split,
    fit_gas_model,
    fit_intervals,
    integrate_exposure,
    integrate_flame,
)

START = datetime(2025, 1, 1)


def interval(
    index: int,
    features: tuple[float, float, float, float],
    rates: tuple[float, float, float, float] = (0.01, 0.02, 0.03, 0.04),
    *,
    coverage: float = 1.0,
    unknown_modulation: float = 0.0,
    boundary_uncertainty: float = 0.0,
) -> GasInterval:
    flame = sum(features) + unknown_modulation
    volume = sum(minutes * rate for minutes, rate in zip(features, rates, strict=True))
    volume += unknown_modulation * sum(rates) / len(rates)
    return GasInterval(
        START + timedelta(days=index),
        START + timedelta(days=index + 1),
        volume,
        features,
        flame,
        1440.0 * coverage,
        coverage,
        unknown_modulation,
        boundary_uncertainty,
    )


def rich_intervals(*, boundary_uncertainty: float = 0.0) -> list[GasInterval]:
    patterns = [
        (100.0, 0.0, 0.0, 0.0),
        (0.0, 100.0, 0.0, 0.0),
        (0.0, 0.0, 100.0, 0.0),
        (0.0, 0.0, 0.0, 100.0),
        (50.0, 50.0, 0.0, 0.0),
        (0.0, 50.0, 50.0, 0.0),
        (0.0, 0.0, 50.0, 50.0),
        (70.0, 0.0, 30.0, 0.0),
        (0.0, 30.0, 0.0, 70.0),
        (25.0, 25.0, 25.0, 25.0),
        (60.0, 10.0, 20.0, 10.0),
        (10.0, 50.0, 10.0, 30.0),
    ]
    return [
        interval(index, pattern, boundary_uncertainty=boundary_uncertainty)
        for index, pattern in enumerate(patterns)
    ]


def test_fl_is_authoritative_and_modulation_zero_is_lowest_bin() -> None:
    samples = [
        StateSample(START, True, 0),
        StateSample(START + timedelta(minutes=10), None, 80),
        StateSample(START + timedelta(minutes=20), False, 80),
    ]

    bins, flame, observed, coverage = integrate_flame(START, START + timedelta(minutes=20), samples)

    assert bins == (10.0, 0.0, 0.0, 0.0)
    assert flame == 10.0
    assert observed == 10.0
    assert coverage == 0.5


def test_unknown_modulation_keeps_explicit_flame_and_purpose_is_exclusive() -> None:
    samples = [
        StateSample(START, True, None, dhw=False, heating=True),
        StateSample(START + timedelta(minutes=5), True, 50, dhw=True, heating=True),
        StateSample(START + timedelta(minutes=10), True, 75, dhw=True, heating=False),
        StateSample(START + timedelta(minutes=15), False),
    ]

    exposure = integrate_exposure(START, START + timedelta(minutes=15), samples)

    assert exposure.flame_minutes == 15.0
    assert exposure.unknown_modulation_minutes == 5.0
    assert exposure.heating_minutes == 5.0
    assert exposure.dhw_minutes == 5.0
    assert exposure.ambiguous_purpose_minutes == 5.0
    assert exposure.heating_bin_minutes == (0.0, 0.0, 0.0, 0.0)
    assert exposure.heating_unknown_modulation_minutes == 5.0
    assert exposure.dhw_bin_minutes == (0.0, 0.0, 0.0, 5.0)
    assert exposure.ambiguous_purpose_bin_minutes == (0.0, 0.0, 5.0, 0.0)


def test_purpose_split_uses_mode_rates_and_leaves_telemetry_gap_unallocated() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(
        minutes=100,
        bin_minutes=(40.0, 0.0, 0.0, 40.0),
        unknown_modulation_minutes=0,
        observed_minutes=80,
        flame_minutes=80,
        heating_minutes=40,
        dhw_minutes=40,
        heating_bin_minutes=(40.0, 0.0, 0.0, 0.0),
        dhw_bin_minutes=(0.0, 0.0, 0.0, 40.0),
        ambiguous_purpose_bin_minutes=(0.0, 0.0, 0.0, 0.0),
    )
    estimate = estimate_exposure(exposure, model, end=model.calibration_end)
    split = estimate_gas_purpose_split(exposure, model, estimate)

    assert split["scope"] == "modelled_boiler"
    assert split["components"]["heating"]["volume_m3"] == pytest.approx(0.4, rel=1e-3)
    assert split["components"]["dhw"]["volume_m3"] == pytest.approx(1.6, rel=1e-3)
    assert split["allocated_observed_m3"] == pytest.approx(2.0, rel=1e-3)
    assert split["unallocated_m3"] == pytest.approx(0.5, rel=1e-3)
    assert split["allocated_observed_m3"] + split["unallocated_m3"] == pytest.approx(split["total_modelled_m3"])
    assert "telemetry_gap_unallocated" in split["reasons"]


def test_purpose_split_refuses_legacy_exposure_without_purpose_bins() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(60, (60.0, 0.0, 0.0, 0.0), 0, 60, flame_minutes=60)
    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    split = estimate_gas_purpose_split(exposure, model, estimate)

    assert split["status"] == "unknown"
    assert split["allocated_observed_m3"] is None
    assert "invalid_purpose_exposure" in split["reasons"]


def test_shared_meter_split_keeps_its_scope_and_limitation() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=True)
    exposure = Exposure(
        60, (60.0, 0.0, 0.0, 0.0), 0, 60, heating_minutes=60, flame_minutes=60,
        heating_bin_minutes=(60.0, 0.0, 0.0, 0.0), dhw_bin_minutes=(0.0, 0.0, 0.0, 0.0),
        ambiguous_purpose_bin_minutes=(0.0, 0.0, 0.0, 0.0),
    )
    split = estimate_gas_purpose_split(exposure, model, estimate_exposure(exposure, model, end=model.calibration_end))

    assert split["scope"] == "shared_meter_model"
    assert "shared_meter_gas_stove_unseparated" in split["reasons"]


def test_long_gap_and_unknown_fl_are_unknown_not_idle() -> None:
    samples = [StateSample(START, True, 0), StateSample(START + timedelta(minutes=30), False)]

    exposure = integrate_exposure(START, START + timedelta(minutes=30), samples)

    assert exposure.observed_minutes == 0.0
    assert exposure.flame_minutes == 0.0
    assert exposure.bin_minutes == (0.0, 0.0, 0.0, 0.0)


def test_fit_entry_points_share_the_same_math() -> None:
    samples = [StateSample(START, True, None), StateSample(START + timedelta(minutes=10), False)]
    readings = [
        MeterReading(0, timestamp=START, boundary_uncertainty_minutes=0),
        MeterReading(1, timestamp=START + timedelta(minutes=10), boundary_uncertainty_minutes=0),
    ]
    built = build_intervals(readings, samples)

    direct = fit_intervals(built, has_gas_stove=False)
    wrapped = fit_gas_model(readings, samples, has_gas_stove=False)

    assert direct == wrapped
    assert wrapped.mean_rate_m3_per_minute == pytest.approx(0.1)


def test_positive_meter_volume_without_flame_is_not_attributed_to_boiler() -> None:
    row = GasInterval(START, START + timedelta(days=1), 2.0, (0.0, 0.0, 0.0, 0.0), 0, 1440, 1)

    model = fit_intervals([row], has_gas_stove=False)

    assert model.mean_rate_m3_per_minute is None
    assert model.usable_interval_count == 0
    assert "positive_meter_volume_without_flame" in model.reasons


def test_small_bin_model_is_selected_by_unseen_whole_intervals() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)

    assert model.selected_bin_count == 4
    assert model.rates_m3_per_minute == pytest.approx((0.01, 0.02, 0.03, 0.04), rel=2e-4)
    assert model.heldout_interval_count == 3
    assert model.validation_relative_error == pytest.approx(0.0, abs=2e-5)
    assert model.identifiable


def test_identical_mixtures_do_not_claim_identifiable_bins() -> None:
    rows = [interval(index, (25.0, 25.0, 25.0, 25.0)) for index in range(10)]
    model = fit_intervals(rows, has_gas_stove=False)
    exposure = Exposure(100, (25.0, 25.0, 25.0, 25.0), 0, 100, flame_minutes=100)

    estimate = estimate_exposure(exposure, model, end=rows[-1].end)

    assert model.selected_bin_count == 1
    assert model.diversity_rank == 1
    assert not model.identifiable
    assert estimate.reliability_index <= 50


def test_unobserved_bins_use_mean_and_are_marked_as_extrapolation() -> None:
    model = fit_intervals([interval(0, (100.0, 0.0, 0.0, 0.0))], has_gas_stove=False)
    exposure = Exposure(60, (0.0, 0.0, 0.0, 60.0), 0, 60, flame_minutes=60)

    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    assert model.rates_m3_per_minute[3] == model.mean_rate_m3_per_minute
    assert model.rates_m3_per_minute[3] > 0
    assert 3 in model.extrapolated_bins
    assert estimate.volume_m3 == pytest.approx(0.6)
    assert estimate.status == "extrapolated"
    assert "extrapolated_modulation_range" in estimate.reasons


def test_few_intervals_have_no_heldout_error_and_capped_reliability() -> None:
    rows = [interval(0, (50.0, 50.0, 0.0, 0.0)), interval(1, (20.0, 80.0, 0.0, 0.0))]
    model = fit_intervals(rows, has_gas_stove=False)
    exposure = Exposure(100, (50.0, 50.0, 0.0, 0.0), 0, 100, flame_minutes=100)

    estimate = estimate_exposure(exposure, model, end=rows[-1].end)

    assert model.validation_error_m3 is None
    assert model.heldout_interval_count == 0
    assert model.selected_bin_count == 1
    assert estimate.reliability_index <= 35


def test_complete_passport_prior_preserves_actual_range() -> None:
    model = fit_intervals(
        [],
        passport_min_m3_per_hour=1,
        passport_max_m3_per_hour=3,
        has_gas_stove=False,
    )
    exposure = Exposure(60, (60.0, 0.0, 0.0, 0.0), 0, 60, flame_minutes=60)

    estimate = estimate_exposure(exposure, model, end=START)

    assert model.mean_rate_m3_per_minute == pytest.approx(2 / 60)
    assert model.rate_lower_m3_per_minute[0] == pytest.approx(1 / 60)
    assert model.rate_upper_m3_per_minute[0] == pytest.approx(3 / 60)
    assert estimate.volume_m3 == pytest.approx(2)
    assert estimate.uncertainty_m3 == pytest.approx((1, 3))
    assert estimate.reliability_index <= 20


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(1.0, None), (None, 3.0)],
)
def test_one_sided_passport_does_not_invent_the_missing_bound(minimum: float | None, maximum: float | None) -> None:
    model = fit_intervals(
        [],
        passport_min_m3_per_hour=minimum,
        passport_max_m3_per_hour=maximum,
        has_gas_stove=False,
    )

    assert model.mean_rate_m3_per_minute is None
    assert not model.passport_limited
    assert not model.rate_lower_m3_per_minute
    assert not model.rate_upper_m3_per_minute
    assert "incomplete_passport_range" in model.reasons


def test_empirical_rate_respects_real_passport_bound() -> None:
    model = fit_intervals(
        [interval(0, (100.0, 0.0, 0.0, 0.0), rates=(0.1, 0.1, 0.1, 0.1))],
        passport_min_m3_per_hour=1,
        passport_max_m3_per_hour=3,
        has_gas_stove=False,
    )

    assert model.mean_rate_m3_per_minute == pytest.approx(3 / 60)
    assert all(rate <= 3 / 60 for rate in model.rates_m3_per_minute)
    assert model.passport_limited


def test_gas_stove_allows_preliminary_shared_meter_model_with_lower_reliability() -> None:
    rows = rich_intervals()
    boiler_model = fit_intervals(rows, has_gas_stove=False)
    shared_model = fit_intervals(rows, has_gas_stove=True)
    exposure = Exposure(100, (25.0, 25.0, 25.0, 25.0), 0, 100, flame_minutes=100)

    boiler = estimate_exposure(exposure, boiler_model, end=rows[-1].end)
    shared = estimate_exposure(exposure, shared_model, end=rows[-1].end)

    assert shared.volume_m3 == pytest.approx(boiler.volume_m3)
    assert shared.status == "extrapolated"
    assert shared.reliability_index <= 35
    assert shared.reliability_index < boiler.reliability_index
    assert "shared_meter_gas_stove_unseparated" in shared.reasons
    assert shared.uncertainty_m3[0] < boiler.uncertainty_m3[0]  # type: ignore[index]


def test_telemetry_gap_is_estimated_explicitly_instead_of_zero_filled() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(100, (40.0, 0.0, 0.0, 0.0), 0, 80, flame_minutes=40)

    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    assert estimate.observed_volume_m3 == pytest.approx(0.4, rel=2e-4)
    assert estimate.volume_m3 == pytest.approx(0.5, rel=2e-4)
    assert estimate.estimated_gap_m3 == pytest.approx(0.1, rel=2e-4)
    assert estimate.uncertainty_m3[0] < estimate.volume_m3 < estimate.uncertainty_m3[1]  # type: ignore[index,operator]
    assert "telemetry_gap_estimated_from_observed_mix" in estimate.reasons


def test_too_little_telemetry_is_unknown() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(100, (20.0, 0.0, 0.0, 0.0), 0, 79, flame_minutes=20)

    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    assert estimate.volume_m3 is None
    assert estimate.observed_volume_m3 == pytest.approx(0.2, rel=2e-4)
    assert estimate.estimated_gap_m3 is None
    assert estimate.status == "unknown"
    assert estimate.reliability_index == 0
    assert "insufficient_telemetry_coverage" in estimate.reasons


def test_fully_observed_burner_off_is_a_real_zero() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(60, (0.0, 0.0, 0.0, 0.0), 0, 60, flame_minutes=0)

    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    assert estimate.volume_m3 == 0
    assert estimate.uncertainty_m3 == (0, 0)
    assert "observed_burner_off" in estimate.reasons


def test_unknown_modulation_uses_mean_but_remains_explicit() -> None:
    model = fit_intervals(rich_intervals(), has_gas_stove=False)
    exposure = Exposure(60, (0.0, 0.0, 0.0, 0.0), 60, 60, flame_minutes=60)

    estimate = estimate_exposure(exposure, model, end=model.calibration_end)

    assert estimate.flame_minutes == 60
    assert estimate.volume_m3 == pytest.approx(60 * model.mean_rate_m3_per_minute)  # type: ignore[operator]
    assert estimate.status == "extrapolated"
    assert "extrapolated_unknown_modulation" in estimate.reasons


def test_stale_calibration_and_day_boundaries_widen_range_and_reduce_reliability() -> None:
    clean_rows = rich_intervals()
    boundary_rows = rich_intervals(boundary_uncertainty=0.2)
    clean_model = fit_intervals(clean_rows, has_gas_stove=False)
    boundary_model = fit_intervals(boundary_rows, has_gas_stove=False)
    exposure = Exposure(100, (25.0, 25.0, 25.0, 25.0), 0, 100, flame_minutes=100)
    assert clean_model.calibration_end is not None

    fresh = estimate_exposure(exposure, clean_model, end=clean_model.calibration_end)
    stale = estimate_exposure(exposure, clean_model, end=clean_model.calibration_end + timedelta(days=400))
    uncertain_boundary = estimate_exposure(exposure, boundary_model, end=boundary_model.calibration_end)

    assert stale.reliability_index < fresh.reliability_index
    assert stale.uncertainty_m3[0] < fresh.uncertainty_m3[0]  # type: ignore[index]
    assert stale.uncertainty_m3[1] > fresh.uncertainty_m3[1]  # type: ignore[index]
    assert "stale_calibration" in stale.uncertainty_factors
    assert uncertain_boundary.uncertainty_m3[1] > fresh.uncertainty_m3[1]  # type: ignore[index]


def test_reset_or_meter_change_breaks_cumulative_difference() -> None:
    readings = [
        MeterReading(10, timestamp=START, meter_id="a"),
        MeterReading(11, timestamp=START + timedelta(minutes=10), meter_id="b"),
        MeterReading(12, timestamp=START + timedelta(minutes=20), meter_id="b", reset=True),
    ]

    assert build_intervals(readings, []) == []


def test_reset_reading_can_start_the_next_meter_segment() -> None:
    readings = [
        MeterReading(10, timestamp=START),
        MeterReading(1, timestamp=START + timedelta(minutes=10), reset=True),
        MeterReading(2, timestamp=START + timedelta(minutes=20)),
    ]
    samples = [
        StateSample(START + timedelta(minutes=10), True, 0),
        StateSample(START + timedelta(minutes=20), False),
    ]

    intervals = build_intervals(readings, samples)

    assert len(intervals) == 1
    assert intervals[0].volume_m3 == 1
    assert intervals[0].start == START + timedelta(minutes=10)


def test_estimate_gas_uses_the_same_cached_exposure_path() -> None:
    samples = [StateSample(START, True, None), StateSample(START + timedelta(minutes=10), False)]
    model = fit_gas_model(
        [MeterReading(0, timestamp=START), MeterReading(1, timestamp=START + timedelta(minutes=10))],
        samples,
        has_gas_stove=False,
    )
    exposure = integrate_exposure(START, START + timedelta(minutes=10), samples)

    direct = estimate_exposure(exposure, model, start=START, end=START + timedelta(minutes=10))
    wrapped = estimate_gas(START, START + timedelta(minutes=10), samples, model)

    assert direct == wrapped

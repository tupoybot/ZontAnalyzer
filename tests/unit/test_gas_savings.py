from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.gas_savings import (
    GasInterval,
    WeatherPoint,
    compare_gas_savings,
    degree_hours,
    fit_weather_baseline,
)
from zont_analyzer.application.gas_comparison import gas_comparison_context

ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def interval(
    offset: int,
    volume: float,
    outdoor: float,
    *,
    hours: float = 24,
    dhw: float | None = 0,
    schedule: float | None = 8,
    independent: bool = True,
    target: float | None = None,
    occupancy: float | None = None,
    uncertainty: float = 0,
) -> GasInterval:
    start = ORIGIN + timedelta(days=offset * 2)
    return GasInterval(
        start,
        start + timedelta(hours=hours),
        volume,
        (WeatherPoint(start, outdoor, hours),),
        dhw_hours=dhw,
        schedule_hours=schedule,
        independent_measurement=independent,
        target_c=target,
        occupancy_signal=occupancy,
        volume_uncertainty_m3=uncertainty,
    )


def modeled_interval(offset: int, outdoor: float, *, saving: float = 0, **kwargs: object) -> GasInterval:
    # 1 m³ per elapsed hour plus 0.1 m³ per degree-hour.
    volume = 24 + 0.1 * (18 - outdoor) * 24 - saving
    return interval(offset, volume, outdoor, **kwargs)


def training_history() -> list[GasInterval]:
    return [modeled_interval(index, outdoor) for index, outdoor in enumerate((5, 8, 11, 7, 12, 9, 6, 10))]


def test_degree_hours_uses_fixed_explicit_base_and_validates_weather() -> None:
    at = datetime.now(UTC)
    assert degree_hours([WeatherPoint(at, 10, 2), WeatherPoint(at + timedelta(hours=2), 20, 3)]) == 16
    with pytest.raises(ValueError, match="positive"):
        WeatherPoint(at, 10, 0)
    with pytest.raises(ValueError, match="fixed 18"):
        fit_weather_baseline(training_history(), base_temperature_c=16)


def test_interval_rejects_missing_mismatched_and_overlapping_weather() -> None:
    start = ORIGIN
    with pytest.raises(ValueError, match="match declared"):
        GasInterval(start, start + timedelta(hours=24), 10, ())
    with pytest.raises(ValueError, match="match declared"):
        GasInterval(
            start,
            start + timedelta(hours=24),
            10,
            (WeatherPoint(start, 5, 12),),
            weather_coverage_pct=100,
        )
    with pytest.raises(ValueError, match="must not overlap"):
        GasInterval(
            start,
            start + timedelta(hours=24),
            10,
            (WeatherPoint(start, 5, 12), WeatherPoint(start + timedelta(hours=6), 6, 12)),
        )


def test_baseline_uses_chronological_whole_interval_holdout() -> None:
    history = training_history()
    baseline = fit_weather_baseline(history, intervention_boundary=history[-1].end)
    assert baseline.frozen
    assert baseline.intercept_m3 == 0
    assert baseline.model_training_intervals == 6
    assert baseline.validation_intervals == 2
    assert baseline.validation_rmse_m3 == pytest.approx(0, abs=1e-9)
    assert baseline.duration_rate_m3 == pytest.approx(1)
    assert baseline.degree_hour_rate_m3 == pytest.approx(0.1)


def test_correct_savings_uses_expected_consumption_under_after_weather() -> None:
    baseline = fit_weather_baseline(training_history())
    before = modeled_interval(9, 5, target=20)
    after = modeled_interval(10, 8, saving=10, target=22)
    result = compare_gas_savings(baseline, before, after)
    assert result.expected_m3 == pytest.approx(48)
    assert result.normalized_savings_m3 == pytest.approx(10)
    assert result.raw_savings_m3 == pytest.approx(17.2)
    assert result.effect_status == "estimated"
    assert any("целевой" in item for item in result.diagnostics)


def test_warmer_weather_does_not_create_false_savings() -> None:
    baseline = fit_weather_baseline(training_history())
    before = modeled_interval(9, 5)
    after = modeled_interval(10, 12)
    result = compare_gas_savings(baseline, before, after)
    assert result.raw_savings_m3 > 0
    assert result.normalized_savings_m3 == pytest.approx(0, abs=1e-9)
    assert any("потеплением" in item for item in result.diagnostics)


def test_dhw_adjustment_requires_diverse_data_and_heldout_improvement() -> None:
    weather = (5, 9, 12, 7, 11, 6, 10, 8, 13, 4)
    dhw = (1, 4, 2, 6, 3, 7, 5, 2, 8, 4)
    history = [
        interval(index, 24 + 0.08 * (18 - outdoor) * 24 + 3 * hot_water, outdoor, dhw=hot_water)
        for index, (outdoor, hot_water) in enumerate(zip(weather, dhw, strict=True))
    ]
    baseline = fit_weather_baseline(history)
    assert baseline.uses_dhw_adjustment
    assert baseline.dhw_rate_m3 == pytest.approx(3)
    assert baseline.validation_rmse_m3 == pytest.approx(0, abs=1e-8)

    before = interval(11, 24 + 0.08 * 10 * 24 + 3 * 2, 8, dhw=2)
    after = interval(12, 24 + 0.08 * 8 * 24 + 3 * 6 - 5, 10, dhw=6)
    result = compare_gas_savings(baseline, before, after)
    assert result.normalized_savings_m3 == pytest.approx(5)
    assert not any("ГВС" in item for item in result.confounders)


def test_unknown_or_changed_dhw_without_validated_term_blocks_attribution() -> None:
    history = training_history()
    history[3] = modeled_interval(3, 7, dhw=None)
    baseline = fit_weather_baseline(history)
    assert not baseline.uses_dhw_adjustment
    result = compare_gas_savings(
        baseline,
        modeled_interval(9, 7, dhw=None),
        modeled_interval(10, 8, saving=8, dhw=4),
    )
    assert result.effect_status == "confounded"
    assert any("ГВС" in item for item in result.confounders)


def test_schedule_and_occupancy_are_hypothesis_sensitivity_not_corrections() -> None:
    baseline = fit_weather_baseline(training_history())
    before = modeled_interval(9, 8, schedule=8, occupancy=0.8)
    after = modeled_interval(10, 8, saving=8, schedule=4, occupancy=0.2)
    result = compare_gas_savings(baseline, before, after)
    assert result.effect_status == "confounded"
    assert any("Расписание" in item for item in result.confounders)
    assert any("Присутствие" in item for item in result.confounders)
    context = gas_comparison_context(baseline, before, after)
    assert context["sensitivity"]["occupancy_correction_applied"] is False
    assert context["sensitivity"]["shares_evidence_with_dhw"] is True


def test_two_percent_effect_and_input_volume_uncertainty_are_indistinguishable() -> None:
    baseline = fit_weather_baseline(training_history())
    expected = modeled_interval(10, 8).volume_m3
    before = modeled_interval(9, 8, uncertainty=1.2)
    after = interval(10, expected * 0.98, 8, uncertainty=1.8)
    result = compare_gas_savings(baseline, before, after)
    assert result.normalized_savings_pct == pytest.approx(2)
    assert result.uncertainty_m3 == pytest.approx(3)
    assert result.uncertainty_lower_m3 < 0 < result.uncertainty_upper_m3
    assert result.effect_status == "effect_indistinguishable"
    context = gas_comparison_context(baseline, before, after)
    assert context["uncertainty_range_m3"]["probabilistic"] is False


def test_model_only_after_evidence_is_distinguished_from_independent_meter_evidence() -> None:
    baseline = fit_weather_baseline(training_history())
    result = compare_gas_savings(
        baseline,
        modeled_interval(9, 8),
        modeled_interval(10, 8, saving=10, independent=False),
    )
    assert result.model_only and not result.measured_validation
    assert result.reliability_index <= 35
    context = gas_comparison_context(
        baseline,
        modeled_interval(9, 8),
        modeled_interval(10, 8, saving=10, independent=False),
    )
    assert context["provenance"]["validation"] == "только модель"


def test_rank_nonnegative_and_small_sample_guards() -> None:
    with pytest.raises(ValueError, match="at least three"):
        fit_weather_baseline([modeled_interval(0, 8), modeled_interval(1, 9)])
    with pytest.raises(ValueError, match="diversity"):
        fit_weather_baseline([interval(index, 50, 8) for index in range(3)])

    decreasing = [interval(index, 100 - index * 5, outdoor) for index, outdoor in enumerate((5, 8, 11, 7))]
    baseline = fit_weather_baseline(decreasing)
    assert baseline.duration_rate_m3 >= 0
    assert baseline.degree_hour_rate_m3 >= 0
    assert baseline.intercept_m3 == 0


def test_pre_intervention_cutoff_and_nonoverlap_prevent_leakage() -> None:
    history = training_history()
    with pytest.raises(ValueError, match="intervention"):
        fit_weather_baseline(history, intervention_boundary=history[-1].end - timedelta(hours=1))

    overlap = list(history[:3])
    first = overlap[0]
    overlap[1] = GasInterval(
        first.start + timedelta(hours=12),
        first.end + timedelta(hours=12),
        overlap[1].volume_m3,
        (WeatherPoint(first.start + timedelta(hours=12), 8, 24),),
        dhw_hours=0,
        schedule_hours=8,
    )
    with pytest.raises(ValueError, match="must not overlap"):
        fit_weather_baseline(overlap)

    baseline = fit_weather_baseline(history)
    overlapping_after = GasInterval(
        baseline.training_end - timedelta(hours=12),
        baseline.training_end + timedelta(hours=12),
        40,
        (WeatherPoint(baseline.training_end - timedelta(hours=12), 8, 24),),
        dhw_hours=0,
        schedule_hours=8,
    )
    with pytest.raises(ValueError, match="must end before"):
        compare_gas_savings(baseline, modeled_interval(9, 8), overlapping_after)


def test_extrapolation_and_absent_holdout_cap_reliability() -> None:
    baseline = fit_weather_baseline(training_history())
    result = compare_gas_savings(baseline, modeled_interval(9, 8), modeled_interval(10, -5))
    assert result.extrapolated
    assert result.reliability_index <= 35
    assert result.effect_status == "extrapolated"

    preliminary = fit_weather_baseline(training_history()[:4])
    assert preliminary.validation_rmse_m3 is None
    preliminary_result = compare_gas_savings(preliminary, modeled_interval(5, 8), modeled_interval(6, 8))
    assert preliminary_result.reliability_index <= 35
    assert any("отложенных" in item for item in preliminary_result.diagnostics)


def test_degree_hours_per_hour_detects_extrapolation_hidden_by_raw_ranges() -> None:
    durations = (12, 24, 36, 48, 30, 40, 24, 36)
    outdoors = (5, 12, 10, 11, 6, 10, 9, 8)
    history = [
        interval(
            index,
            hours + 0.1 * (18 - outdoor) * hours,
            outdoor,
            hours=hours,
        )
        for index, (hours, outdoor) in enumerate(zip(durations, outdoors, strict=True))
    ]
    baseline = fit_weather_baseline(history)
    after = interval(10, 24 + 0.1 * 14 * 24, 4)
    after_degree_hours = degree_hours(after.weather)
    assert baseline.training_degree_hours[0] <= after_degree_hours <= baseline.training_degree_hours[1]
    assert baseline.training_duration_hours[0] <= after.duration_hours <= baseline.training_duration_hours[1]
    result = compare_gas_savings(baseline, interval(9, 50, 8), after)
    assert result.extrapolated


def test_incomplete_comparison_is_rejected() -> None:
    baseline = fit_weather_baseline(training_history())
    complete = modeled_interval(9, 8)
    incomplete = GasInterval(
        complete.start,
        complete.end,
        complete.volume_m3,
        complete.weather,
        complete.dhw_hours,
        complete.schedule_hours,
        complete=False,
    )
    with pytest.raises(ValueError, match="complete"):
        compare_gas_savings(baseline, incomplete, modeled_interval(10, 8))

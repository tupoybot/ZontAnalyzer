"""Deterministic, weather-normalized comparisons of whole gas-meter intervals.

The baseline is deliberately small: elapsed hours and heating degree-hours at a
fixed 18 °C base. A DHW term is admitted only when complete DHW observations
improve prediction on later, whole held-out intervals. The reported range is a
non-probabilistic sensitivity range, not a confidence or credible interval.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from math import ceil, isfinite, sqrt
from statistics import mean

MIN_WEATHER_COVERAGE_PCT = 70.0
MIN_BASELINE_INTERVALS = 3
MIN_DHW_INTERVALS = 8


@dataclass(frozen=True)
class WeatherPoint:
    """A temperature sample or an exact aggregate sufficient statistic."""

    at: datetime
    outdoor_c: float
    hours: float = 1.0

    def __post_init__(self) -> None:
        if not isfinite(self.outdoor_c):
            raise ValueError("weather temperature must be finite")
        if not isfinite(self.hours) or self.hours <= 0:
            raise ValueError("weather sample hours must be finite and positive")


@dataclass(frozen=True)
class GasInterval:
    start: datetime
    end: datetime
    volume_m3: float
    weather: tuple[WeatherPoint, ...]
    dhw_hours: float | None = None
    schedule_hours: float | None = None
    complete: bool = True
    independent_measurement: bool = True
    target_c: float | None = None
    occupancy_signal: float | None = None
    weather_coverage_pct: float = 100.0
    volume_uncertainty_m3: float = 0.0

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("gas interval end must be after start")
        if not isfinite(self.volume_m3) or self.volume_m3 < 0:
            raise ValueError("gas volume must be finite and non-negative")
        if not isfinite(self.weather_coverage_pct) or not 0 <= self.weather_coverage_pct <= 100:
            raise ValueError("weather coverage must be between 0 and 100 percent")
        if not isfinite(self.volume_uncertainty_m3) or self.volume_uncertainty_m3 < 0:
            raise ValueError("volume uncertainty must be finite and non-negative")
        for name, value in (("DHW hours", self.dhw_hours), ("schedule hours", self.schedule_hours)):
            if value is not None and (not isfinite(value) or not 0 <= value <= self.duration_hours):
                raise ValueError(f"{name} must be within the gas interval")
        for name, value in (("target temperature", self.target_c), ("occupancy signal", self.occupancy_signal)):
            if value is not None and not isfinite(value):
                raise ValueError(f"{name} must be finite")

        ordered = sorted(self.weather, key=lambda point: point.at)
        weather_hours = 0.0
        previous_end: datetime | None = None
        for point in ordered:
            point_end = point.at + timedelta(hours=point.hours)
            if point.at < self.start or point_end > self.end + timedelta(seconds=1):
                raise ValueError("weather sample must be contained in its gas interval")
            if previous_end is not None and point.at < previous_end:
                raise ValueError("weather samples must not overlap")
            previous_end = point_end
            weather_hours += point.hours
        represented_coverage = weather_hours / self.duration_hours * 100
        if abs(represented_coverage - self.weather_coverage_pct) > 1.0:
            raise ValueError("weather sample hours do not match declared weather coverage")

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class WeatherBaseline:
    base_temperature_c: float
    intercept_m3: float
    degree_hour_rate_m3: float
    duration_rate_m3: float
    dhw_rate_m3: float
    schedule_rate_m3: float
    training_degree_hours: tuple[float, float]
    training_duration_hours: tuple[float, float]
    residual_rmse_m3: float
    training_intervals: int
    frozen: bool = True
    training_start: datetime | None = None
    training_end: datetime | None = None
    version: str = "weather-v2"
    validation_rmse_m3: float | None = None
    validation_max_abs_error_m3: float | None = None
    validation_intervals: int = 0
    model_training_intervals: int = 0
    uses_dhw_adjustment: bool = False
    training_dhw_hours: tuple[float, float] | None = None
    training_degree_hours_per_hour: tuple[float, float] | None = None
    intervention_boundary: datetime | None = None


@dataclass(frozen=True)
class GasSavings:
    observed_m3: float
    expected_m3: float
    raw_savings_m3: float
    normalized_savings_m3: float
    raw_savings_pct: float | None
    normalized_savings_pct: float | None
    uncertainty_m3: float
    reliability_index: int
    measured_validation: bool
    model_only: bool
    extrapolated: bool
    assumptions: tuple[str, ...] = ()
    confounders: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    effect_status: str = "estimated"
    uncertainty_lower_m3: float = 0.0
    uncertainty_upper_m3: float = 0.0
    uncertainty_method: str = "невероятностный эмпирический диапазон чувствительности"


def degree_hours(
    weather: tuple[WeatherPoint, ...] | list[WeatherPoint], *, base_temperature_c: float = 18.0,
) -> float:
    """Return integrated heating degree-hours at an explicit fixed base."""
    if not isfinite(base_temperature_c) or not -50 < base_temperature_c < 50:
        raise ValueError("base temperature must be between -50 and 50 °C")
    if not weather:
        raise ValueError("weather samples are required")
    return sum(max(0.0, base_temperature_c - point.outdoor_c) * point.hours for point in weather)


def _matrix_rank(rows: list[tuple[float, ...]]) -> int:
    """Numerical rank after column scaling (matrices here have at most 3 columns)."""
    if not rows:
        return 0
    columns = len(rows[0])
    scales = [max(abs(row[column]) for row in rows) for column in range(columns)]
    matrix = [[row[column] / scales[column] if scales[column] else 0.0 for column in range(columns)]
              for row in rows]
    rank = 0
    for column in range(columns):
        if rank >= len(matrix):
            break
        pivot = max(range(rank, len(matrix)), key=lambda index: abs(matrix[index][column]))
        if abs(matrix[pivot][column]) <= 1e-9:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        divisor = matrix[rank][column]
        matrix[rank] = [value / divisor for value in matrix[rank]]
        for row_index in range(len(matrix)):
            if row_index == rank:
                continue
            factor = matrix[row_index][column]
            matrix[row_index] = [value - factor * basis for value, basis in zip(
                matrix[row_index], matrix[rank], strict=True,
            )]
        rank += 1
        if rank == len(matrix):
            break
    return rank


def _linear_solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    size = len(rhs)
    augmented = [row[:] + [value] for row, value in zip(matrix, rhs, strict=True)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-10:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [value - factor * basis for value, basis in zip(
                augmented[row], augmented[column], strict=True,
            )]
    return [augmented[index][-1] for index in range(size)]


def _fit_nonnegative(rows: list[tuple[float, ...]], values: list[float]) -> tuple[float, ...]:
    """Exact active-set NNLS for the two or three coefficients used here."""
    columns = len(rows[0])
    if _matrix_rank(rows) < columns:
        raise ValueError("baseline inputs lack independent duration/weather diversity")
    scales = [max(abs(row[column]) for row in rows) for column in range(columns)]
    scaled_rows = [tuple(row[column] / scales[column] for column in range(columns)) for row in rows]
    best = tuple(0.0 for _ in range(columns))
    best_error = sum(value * value for value in values)
    for active_size in range(1, columns + 1):
        for active in combinations(range(columns), active_size):
            active_rows = [tuple(row[column] for column in active) for row in scaled_rows]
            gram = [[sum(row[left] * row[right] for row in active_rows)
                     for right in range(active_size)] for left in range(active_size)]
            rhs = [sum(row[column] * value for row, value in zip(active_rows, values, strict=True))
                   for column in range(active_size)]
            solved = _linear_solve(gram, rhs)
            if solved is None or any(value < -1e-9 for value in solved):
                continue
            candidate = [0.0] * columns
            for column, value in zip(active, solved, strict=True):
                candidate[column] = max(0.0, value / scales[column])
            error = sum((actual - sum(coefficient * feature for coefficient, feature in zip(
                candidate, row, strict=True,
            ))) ** 2 for row, actual in zip(rows, values, strict=True))
            if error < best_error:
                best = tuple(candidate)
                best_error = error
    return best


def _features(interval: GasInterval, base: float, *, include_dhw: bool) -> tuple[float, ...]:
    result = (interval.duration_hours, degree_hours(interval.weather, base_temperature_c=base))
    if include_dhw:
        if interval.dhw_hours is None:
            raise ValueError("DHW observations are required by the selected baseline")
        return (*result, interval.dhw_hours)
    return result


def _predict(coefficients: tuple[float, ...], row: tuple[float, ...]) -> float:
    return sum(coefficient * feature for coefficient, feature in zip(coefficients, row, strict=True))


def _rmse(errors: list[float]) -> float:
    return sqrt(mean(error * error for error in errors))


def fit_weather_baseline(
    intervals: list[GasInterval] | tuple[GasInterval, ...], *, base_temperature_c: float = 18.0,
    intervention_boundary: datetime | None = None,
) -> WeatherBaseline:
    """Fit a frozen non-negative baseline from complete pre-intervention intervals.

    With fewer than five intervals the result is explicitly preliminary and has
    no holdout metric. From five intervals onward, the newest whole intervals
    are reserved for chronological validation and never used to fit coefficients.
    """
    if abs(base_temperature_c - 18.0) > 1e-9:
        raise ValueError("the gas savings baseline uses the fixed 18 °C degree-hour base")
    if len(intervals) < MIN_BASELINE_INTERVALS:
        raise ValueError("at least three complete independent training intervals are required")
    usable = sorted(intervals, key=lambda item: (item.start, item.end))
    for item in usable:
        if not item.complete or not item.independent_measurement:
            raise ValueError("baseline requires complete independent meter intervals")
        if item.weather_coverage_pct < MIN_WEATHER_COVERAGE_PCT:
            raise ValueError("baseline requires at least 70% weather coverage")
        if intervention_boundary is not None and item.end > intervention_boundary:
            raise ValueError("baseline interval crosses the intervention boundary")
    for previous, current in zip(usable, usable[1:], strict=False):
        if current.start < previous.end:
            raise ValueError("baseline meter intervals must not overlap")

    validation_count = max(1, ceil(len(usable) * 0.2)) if len(usable) >= 5 else 0
    fit_items = usable[:-validation_count] if validation_count else usable
    validation_items = usable[-validation_count:] if validation_count else []
    if len(fit_items) < MIN_BASELINE_INTERVALS:
        raise ValueError("at least three intervals must remain for model fitting")

    base_rows = [_features(item, base_temperature_c, include_dhw=False) for item in fit_items]
    values = [item.volume_m3 for item in fit_items]
    base_coefficients = _fit_nonnegative(base_rows, values)
    selected = base_coefficients
    uses_dhw = False
    if validation_items:
        selected_errors = [item.volume_m3 - _predict(base_coefficients, _features(
            item, base_temperature_c, include_dhw=False,
        )) for item in validation_items]
    else:
        selected_errors = []

    all_dhw_known = all(item.dhw_hours is not None for item in usable)
    if len(usable) >= MIN_DHW_INTERVALS and all_dhw_known:
        dhw_rows = [_features(item, base_temperature_c, include_dhw=True) for item in fit_items]
        if _matrix_rank(dhw_rows) == 3:
            dhw_coefficients = _fit_nonnegative(dhw_rows, values)
            dhw_errors = [item.volume_m3 - _predict(dhw_coefficients, _features(
                item, base_temperature_c, include_dhw=True,
            )) for item in validation_items]
            base_rmse = _rmse(selected_errors)
            dhw_rmse = _rmse(dhw_errors)
            minimum_improvement = max(0.01, mean(item.volume_m3 for item in validation_items) * 0.01)
            if dhw_rmse <= base_rmse * 0.9 and base_rmse - dhw_rmse >= minimum_improvement:
                selected = dhw_coefficients
                selected_errors = dhw_errors
                uses_dhw = True

    selected_rows = [_features(item, base_temperature_c, include_dhw=uses_dhw) for item in fit_items]
    training_errors = [actual - _predict(selected, row) for actual, row in zip(values, selected_rows, strict=True)]
    degree_hour_values = [degree_hours(item.weather, base_temperature_c=base_temperature_c) for item in fit_items]
    durations = [item.duration_hours for item in fit_items]
    degree_hour_intensities = [degree_hours_value / duration for degree_hours_value, duration in zip(
        degree_hour_values, durations, strict=True,
    )]
    dhw_range = None
    if uses_dhw:
        known_dhw = [item.dhw_hours for item in fit_items if item.dhw_hours is not None]
        dhw_range = (min(known_dhw), max(known_dhw))
    return WeatherBaseline(
        base_temperature_c=base_temperature_c,
        intercept_m3=0.0,
        duration_rate_m3=selected[0],
        degree_hour_rate_m3=selected[1],
        dhw_rate_m3=selected[2] if uses_dhw else 0.0,
        schedule_rate_m3=0.0,
        training_degree_hours=(min(degree_hour_values), max(degree_hour_values)),
        training_duration_hours=(min(durations), max(durations)),
        residual_rmse_m3=_rmse(training_errors),
        validation_rmse_m3=_rmse(selected_errors) if selected_errors else None,
        validation_max_abs_error_m3=max((abs(error) for error in selected_errors), default=None),
        training_intervals=len(usable),
        validation_intervals=len(validation_items),
        model_training_intervals=len(fit_items),
        training_start=usable[0].start,
        training_end=max(item.end for item in usable),
        uses_dhw_adjustment=uses_dhw,
        training_dhw_hours=dhw_range,
        training_degree_hours_per_hour=(min(degree_hour_intensities), max(degree_hour_intensities)),
        intervention_boundary=intervention_boundary,
    )


def compare_gas_savings(baseline: WeatherBaseline, before: GasInterval, after: GasInterval) -> GasSavings:
    """Compare whole periods against expected use under the *after* weather."""
    if not baseline.frozen:
        raise ValueError("comparison requires a frozen baseline")
    if not before.complete or not after.complete:
        raise ValueError("comparison requires complete intervals")
    if before.weather_coverage_pct < MIN_WEATHER_COVERAGE_PCT or after.weather_coverage_pct < MIN_WEATHER_COVERAGE_PCT:
        raise ValueError("comparison requires at least 70% weather coverage")
    if before.end > after.start:
        raise ValueError("before interval must end before the after interval starts")
    if baseline.training_end is not None and after.start < baseline.training_end:
        raise ValueError("after interval overlaps the frozen baseline history")

    before_weather = degree_hours(before.weather, base_temperature_c=baseline.base_temperature_c)
    after_weather = degree_hours(after.weather, base_temperature_c=baseline.base_temperature_c)
    expected = baseline.duration_rate_m3 * after.duration_hours
    expected += baseline.degree_hour_rate_m3 * after_weather
    if baseline.uses_dhw_adjustment:
        if after.dhw_hours is None:
            raise ValueError("after DHW hours are required by the selected baseline")
        expected += baseline.dhw_rate_m3 * after.dhw_hours

    raw = before.volume_m3 - after.volume_m3
    normalized = expected - after.volume_m3
    extrapolated = not (baseline.training_degree_hours[0] <= after_weather <= baseline.training_degree_hours[1])
    extrapolated |= not (baseline.training_duration_hours[0] <= after.duration_hours
                         <= baseline.training_duration_hours[1])
    if baseline.training_degree_hours_per_hour is not None:
        after_degree_hours_per_hour = after_weather / after.duration_hours
        extrapolated |= not (baseline.training_degree_hours_per_hour[0] <= after_degree_hours_per_hour
                             <= baseline.training_degree_hours_per_hour[1])
    if baseline.uses_dhw_adjustment and baseline.training_dhw_hours is not None and after.dhw_hours is not None:
        extrapolated |= not (baseline.training_dhw_hours[0] <= after.dhw_hours <= baseline.training_dhw_hours[1])

    diagnostics: list[str] = []
    if extrapolated:
        diagnostics.append("Условия после изменения находятся вне диапазона погоды, длительности или ГВС модели.")
    if raw > 0 and normalized <= 0:
        diagnostics.append("Исходное снижение объясняется потеплением и не является подтверждённой экономией.")
    elif raw > normalized and after_weather < before_weather:
        diagnostics.append("Исходное снижение частично связано с потеплением.")
    if before.target_c != after.target_c:
        diagnostics.append("Изменение целевой температуры сохранено как исследуемое воздействие.")
    if baseline.validation_intervals == 0:
        diagnostics.append("Предварительная модель не проверена на отложенных интервалах.")
    if baseline.uses_dhw_adjustment:
        diagnostics.append(
            "Поправка ГВС выбрана на тех же отложенных интервалах: ошибка проверки может быть оптимистичной."
        )

    confounders: list[str] = []
    dhw_confound = False
    if not baseline.uses_dhw_adjustment:
        if before.dhw_hours is None or after.dhw_hours is None:
            confounders.append("Использование ГВС неизвестно и не применялось как поправка.")
            dhw_confound = True
        elif abs(before.dhw_hours - after.dhw_hours) > 1e-9:
            confounders.append("Использование ГВС изменилось, а проверенной поправки ГВС у модели нет.")
            dhw_confound = True
    schedule_confound = False
    if before.schedule_hours is None or after.schedule_hours is None:
        confounders.append("Расписание и условия использования неизвестны и не нормализованы.")
        schedule_confound = True
    elif abs(before.schedule_hours - after.schedule_hours) > 1e-9:
        confounders.append("Расписание или условия использования изменились и не нормализованы.")
        schedule_confound = True
    occupancy_confound = before.occupancy_signal is not None or after.occupancy_signal is not None
    if occupancy_confound:
        confounders.append("Присутствие жильцов — неподтверждённая гипотеза; поправка не применялась.")

    empirical_error = baseline.validation_max_abs_error_m3
    if empirical_error is None:
        empirical_error = max(baseline.residual_rmse_m3, expected * 0.10)
    uncertainty = empirical_error + before.volume_uncertainty_m3 + after.volume_uncertainty_m3
    lower = normalized - uncertainty
    upper = normalized + uncertainty

    measured = after.independent_measurement
    fit_scale = max(expected, 1.0)
    validation_quality = max(0.0, 1.0 - empirical_error / fit_scale)
    sample_quality = min(1.0, baseline.model_training_intervals / 12)
    reliability = round(100 * (0.25 + 0.75 * sample_quality) * validation_quality)
    if baseline.validation_intervals == 0:
        reliability = min(reliability, 35)
    elif baseline.validation_intervals == 1:
        reliability = min(reliability, 50)
    elif baseline.validation_intervals == 2:
        reliability = min(reliability, 65)
    if not measured:
        reliability = min(reliability, 35)
    if extrapolated:
        reliability = min(reliability, 35)
    if dhw_confound or schedule_confound or occupancy_confound:
        reliability = min(reliability, 45)
    reliability = max(0, min(100, reliability))

    raw_denom = before.volume_m3 or None
    expected_denom = expected or None
    normalized_pct = normalized / expected_denom * 100 if expected_denom else None
    if dhw_confound or schedule_confound or occupancy_confound:
        effect_status = "confounded"
    elif extrapolated:
        effect_status = "extrapolated"
    elif lower <= 0 <= upper or (normalized_pct is not None and abs(normalized_pct) <= 2.0):
        effect_status = "effect_indistinguishable"
    else:
        effect_status = "estimated"

    assumptions: tuple[str, ...] = (
        f"Фиксированная база градусо-часов: {baseline.base_temperature_c:.1f} °C.",
        "Модель заморожена по полным интервалам счётчика до вмешательства.",
        "Неопределённость дана как невероятностный эмпирический диапазон чувствительности.",
    )
    if baseline.uses_dhw_adjustment:
        assumptions += ("Поправка ГВС выбрана по улучшению на отложенных полных интервалах.",)
    if extrapolated:
        assumptions += ("Нормализованный результат является экстраполяцией и не подтверждает экономию.",)
    if not measured:
        assumptions += ("После изменения доступна только модельная оценка без независимого интервала счётчика.",)
    return GasSavings(
        observed_m3=after.volume_m3,
        expected_m3=expected,
        raw_savings_m3=raw,
        normalized_savings_m3=normalized,
        raw_savings_pct=raw / raw_denom * 100 if raw_denom else None,
        normalized_savings_pct=normalized_pct,
        uncertainty_m3=uncertainty,
        reliability_index=reliability,
        measured_validation=measured,
        model_only=not measured,
        extrapolated=extrapolated,
        assumptions=assumptions,
        confounders=tuple(confounders),
        diagnostics=tuple(diagnostics),
        effect_status=effect_status,
        uncertainty_lower_m3=lower,
        uncertainty_upper_m3=upper,
    )

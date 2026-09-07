"""Deterministic gas-use estimation from meter readings and burner telemetry.

Meter differences measure whole intervals. Burner telemetry only fits and integrates
a model: missing telemetry is never interpreted as an idle burner, and ``fl`` remains
authoritative when modulation is zero or unknown. Uncertainty bounds are deterministic
sensitivity ranges, not probability or confidence intervals.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from math import ceil, sqrt
from typing import Any

ALGORITHM_VERSION = "gas-model-v2"
DEFAULT_BIN_EDGES = (0.0, 25.0, 50.0, 75.0, 100.0)
MIN_CALIBRATION_COVERAGE = 0.8
MIN_ESTIMATE_COVERAGE = 0.8


@dataclass(frozen=True)
class StateSample:
    timestamp: datetime
    fl: bool | None
    modulation: float | None = None
    dhw: bool | None = None
    heating: bool | None = None


@dataclass(frozen=True)
class MeterReading:
    """A cumulative reading. ``day`` is used when an exact timestamp is absent."""

    value_m3: float
    day: date | None = None
    timestamp: datetime | None = None
    meter_id: str = "default"
    boundary_uncertainty_minutes: float = 720.0
    reset: bool = False


@dataclass(frozen=True)
class GasInterval:
    start: datetime
    end: datetime
    volume_m3: float
    features: tuple[float, ...]
    flame_minutes: float
    observed_minutes: float
    coverage: float
    unknown_modulation_minutes: float = 0.0
    boundary_uncertainty_m3: float = 0.0


@dataclass(frozen=True)
class Exposure:
    """Additive sufficient statistics suitable for application caching."""

    minutes: float
    bin_minutes: tuple[float, ...]
    unknown_modulation_minutes: float
    observed_minutes: float
    heating_minutes: float = 0.0
    dhw_minutes: float = 0.0
    # Explicit because flame time includes active samples with unknown modulation.
    flame_minutes: float = 0.0
    ambiguous_purpose_minutes: float = 0.0


@dataclass(frozen=True)
class GasModel:
    rates_m3_per_minute: tuple[float, ...]
    bin_edges: tuple[float, ...]
    mean_rate_m3_per_minute: float | None
    version: str = ALGORITHM_VERSION
    support_minutes: tuple[float, ...] = ()
    support_intervals: tuple[int, ...] = ()
    extrapolated_bins: tuple[int, ...] = ()
    identifiable: bool = False
    validation_error_m3: float | None = None
    passport_limited: bool = False
    reasons: tuple[str, ...] = ()
    # Additive metadata; the original public fields above remain stable.
    low_support_bins: tuple[int, ...] = ()
    selected_bin_count: int = 0
    interval_count: int = 0
    usable_interval_count: int = 0
    heldout_interval_count: int = 0
    validation_relative_error: float | None = None
    diversity_rank: int = 0
    average_coverage: float = 0.0
    boundary_relative_uncertainty: float = 0.0
    calibration_end: datetime | None = None
    passport_min_m3_per_minute: float | None = None
    passport_max_m3_per_minute: float | None = None
    rate_lower_m3_per_minute: tuple[float, ...] = ()
    rate_upper_m3_per_minute: tuple[float, ...] = ()
    has_gas_stove: bool | None = None
    uncertainty_method: str = "deterministic_sensitivity_range"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GasEstimate:
    volume_m3: float | None
    status: str  # measured, estimated, extrapolated, unknown
    reliability_index: float
    uncertainty_m3: tuple[float, float] | None
    coverage: float
    flame_minutes: float
    residual_m3: float | None
    model_version: str
    reasons: tuple[str, ...] = ()
    observed_volume_m3: float | None = None
    estimated_gap_m3: float | None = None
    calibration_age_days: float | None = None
    uncertainty_factors: tuple[str, ...] = ()
    uncertainty_method: str = "deterministic_sensitivity_range"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _sample(value: Any) -> StateSample:
    if isinstance(value, StateSample):
        return value
    if isinstance(value, (tuple, list)):
        return StateSample(value[0], value[1], value[2] if len(value) > 2 else None)
    return StateSample(
        _attr(value, "timestamp", _attr(value, "timestamp_utc")),
        _attr(value, "fl"),
        _attr(value, "modulation"),
        _attr(value, "dhw"),
        _attr(value, "heating"),
    )


def _reading(value: Any) -> MeterReading:
    if isinstance(value, MeterReading):
        return value
    if isinstance(value, (tuple, list)):
        return MeterReading(float(value[1]), timestamp=value[0])
    return MeterReading(
        float(_attr(value, "value_m3", _attr(value, "value"))),
        day=_attr(value, "day"),
        timestamp=_attr(value, "timestamp"),
        meter_id=_attr(value, "meter_id", "default"),
        boundary_uncertainty_minutes=float(_attr(value, "boundary_uncertainty_minutes", 720.0)),
        reset=bool(_attr(value, "reset", False)),
    )


def _edges(bin_edges: Sequence[float] | None) -> tuple[float, ...]:
    edges = tuple(float(x) for x in (bin_edges if bin_edges is not None else DEFAULT_BIN_EDGES))
    if len(edges) < 2 or any(right <= left for left, right in zip(edges, edges[1:], strict=False)):
        raise ValueError("bin_edges must contain at least two strictly increasing values")
    return edges


def _bin(modulation: float | None, edges: tuple[float, ...]) -> int:
    if modulation is None:
        return -1
    for index in range(len(edges) - 1):
        if modulation < edges[index + 1]:
            return index
    return len(edges) - 2


def _segments(
    start: datetime,
    end: datetime,
    samples: Iterable[Any],
    max_gap_minutes: float,
) -> Iterable[tuple[StateSample, float]]:
    points = sorted((_sample(item) for item in samples), key=lambda item: item.timestamp)
    for index, point in enumerate(points):
        left = max(start, point.timestamp)
        raw_right = points[index + 1].timestamp if index + 1 < len(points) else end
        right = min(end, raw_right)
        if right <= left:
            continue
        age_at_start = max(0.0, (start - point.timestamp).total_seconds() / 60.0)
        source_span = (raw_right - point.timestamp).total_seconds() / 60.0
        # A long state-to-state interval is a telemetry gap. The application can
        # insert explicit TTL cuts when source freshness is known.
        if age_at_start > max_gap_minutes or source_span > max_gap_minutes:
            continue
        yield point, (right - left).total_seconds() / 60.0


def integrate_flame(
    start: datetime,
    end: datetime,
    samples: Iterable[Any],
    *,
    bin_edges: Sequence[float] | None = None,
    max_gap_minutes: float = 15.0,
) -> tuple[tuple[float, ...], float, float, float]:
    """Return bin minutes, explicit flame minutes, observed minutes, and coverage."""
    edges = _edges(bin_edges)
    durations = [0.0] * (len(edges) - 1)
    if end <= start:
        return tuple(durations), 0.0, 0.0, 0.0
    observed = flame = 0.0
    for point, minutes in _segments(start, end, samples, max_gap_minutes):
        if point.fl is None:
            continue
        observed += minutes
        if point.fl is True:
            flame += minutes
            index = _bin(point.modulation, edges)
            if index >= 0:
                durations[index] += minutes
    total = (end - start).total_seconds() / 60.0
    return tuple(durations), flame, observed, max(0.0, min(1.0, observed / total))


def integrate_exposure(
    start: datetime,
    end: datetime,
    samples: Iterable[Any],
    *,
    bin_edges: Sequence[float] | None = None,
    max_gap_minutes: float = 15.0,
) -> Exposure:
    """Return additive exposure without treating gaps or unknown ``fl`` as off."""
    edges = _edges(bin_edges)
    bins = [0.0] * (len(edges) - 1)
    unknown = observed = heating = dhw = flame = ambiguous = 0.0
    if end <= start:
        return Exposure(0.0, tuple(bins), 0.0, 0.0)
    for point, minutes in _segments(start, end, samples, max_gap_minutes):
        if point.fl is None:
            continue
        observed += minutes
        if point.fl is not True:
            continue
        flame += minutes
        index = _bin(point.modulation, edges)
        if index < 0:
            unknown += minutes
        else:
            bins[index] += minutes
        # Purpose is attributed only when the flags identify one exclusive mode.
        if point.heating is True and point.dhw is False:
            heating += minutes
        elif point.dhw is True and point.heating is False:
            dhw += minutes
        else:
            ambiguous += minutes
    return Exposure(
        (end - start).total_seconds() / 60.0,
        tuple(bins),
        unknown,
        observed,
        heating,
        dhw,
        flame,
        ambiguous,
    )


def build_intervals(
    readings: Sequence[Any],
    samples: Iterable[Any],
    *,
    bin_edges: Sequence[float] | None = None,
    max_gap_minutes: float = 15.0,
) -> list[GasInterval]:
    edges = _edges(bin_edges)
    rs = sorted(
        (_reading(item) for item in readings),
        key=lambda item: item.timestamp or datetime.combine(item.day or date.min, datetime.min.time()),
    )
    telemetry = list(samples)
    result: list[GasInterval] = []
    for first, second in zip(rs, rs[1:], strict=False):
        # A reset reading is the first value of a new segment: skip the difference
        # into that boundary, but allow the next difference starting from it.
        if second.reset or first.meter_id != second.meter_id:
            continue
        start = first.timestamp or datetime.combine(first.day or date.min, datetime.min.time()) + timedelta(hours=12)
        end = second.timestamp or datetime.combine(second.day or date.min, datetime.min.time()) + timedelta(hours=12)
        volume = second.value_m3 - first.value_m3
        if end <= start or volume < 0:
            continue
        exposure = integrate_exposure(start, end, telemetry, bin_edges=edges, max_gap_minutes=max_gap_minutes)
        coverage = exposure.observed_minutes / exposure.minutes if exposure.minutes else 0.0
        uncertainty = volume * min(
            0.5,
            (max(0.0, first.boundary_uncertainty_minutes) + max(0.0, second.boundary_uncertainty_minutes))
            / max(1.0, exposure.minutes),
        )
        result.append(
            GasInterval(
                start,
                end,
                volume,
                exposure.bin_minutes,
                exposure.flame_minutes,
                exposure.observed_minutes,
                coverage,
                exposure.unknown_modulation_minutes,
                uncertainty,
            )
        )
    return result


def _matrix_rank(rows: Sequence[Sequence[float]], *, tolerance: float = 1e-10) -> int:
    if not rows:
        return 0
    matrix = [list(map(float, row)) for row in rows]
    row_count, column_count = len(matrix), len(matrix[0])
    rank = 0
    for column in range(column_count):
        if rank >= row_count:
            break
        pivot = max(range(rank, row_count), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) <= tolerance:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        divisor = matrix[rank][column]
        for j in range(column, column_count):
            matrix[rank][j] /= divisor
        for row in range(row_count):
            if row == rank:
                continue
            factor = matrix[row][column]
            if abs(factor) <= tolerance:
                continue
            for j in range(column, column_count):
                matrix[row][j] -= factor * matrix[rank][j]
        rank += 1
    return rank


def _groups(bin_count: int, parameter_count: int) -> tuple[tuple[int, ...], ...]:
    """Return deterministic contiguous small-bin candidates."""
    return tuple(
        tuple(index for index in range(bin_count) if index * parameter_count // bin_count == group)
        for group in range(parameter_count)
    )


def _unknown_minutes(interval: GasInterval) -> float:
    # Preserve explicit flame time if an older cache omitted unknown modulation.
    return max(interval.unknown_modulation_minutes, interval.flame_minutes - sum(interval.features), 0.0)


def _passport_bounds(
    minimum_m3_per_hour: float | None,
    maximum_m3_per_hour: float | None,
) -> tuple[float | None, float | None, tuple[str, ...]]:
    reasons: list[str] = []
    low = None if minimum_m3_per_hour is None else float(minimum_m3_per_hour) / 60.0
    high = None if maximum_m3_per_hour is None else float(maximum_m3_per_hour) / 60.0
    invalid = (
        low is not None
        and low < 0
        or high is not None
        and high <= 0
        or low is not None
        and high is not None
        and low > high
    )
    if invalid:
        return None, None, ("invalid_passport_range",)
    if (low is None) != (high is None):
        reasons.append("incomplete_passport_range")
    return low, high, tuple(reasons)


def _clamp(rate: float, low: float | None, high: float | None) -> float:
    if low is not None:
        rate = max(low, rate)
    if high is not None:
        rate = min(high, rate)
    return max(0.0, rate)


def _nnls(
    features: Sequence[Sequence[float]],
    targets: Sequence[float],
    prior: Sequence[float],
    low: float | None,
    high: float | None,
) -> tuple[float, ...]:
    """Small bounded ridge fit, kept local so the module has no numeric dependency."""
    rates = [_clamp(value, low, high) for value in prior]
    diagonal = [sum(row[index] ** 2 for row in features) for index in range(len(prior))]
    ridge = max(1e-9, sum(diagonal) / max(1, len(diagonal)) * 1e-5)
    for _ in range(200):
        for index in range(len(rates)):
            denominator = diagonal[index] + ridge
            numerator = ridge * prior[index]
            for row, target in zip(features, targets, strict=True):
                other = sum(row[j] * rates[j] for j in range(len(rates)) if j != index)
                numerator += row[index] * (target - other)
            rates[index] = _clamp(numerator / denominator if denominator else prior[index], low, high)
    return tuple(rates)


def _fit_candidate(
    rows: Sequence[GasInterval],
    groups: tuple[tuple[int, ...], ...],
    low: float | None,
    high: float | None,
) -> tuple[tuple[float, ...], float] | None:
    total_flame = sum(row.flame_minutes for row in rows)
    if total_flame <= 0:
        return None
    mean = _clamp(sum(row.volume_m3 for row in rows) / total_flame, low, high)
    if len(groups) == 1:
        return (mean,) * sum(len(group) for group in groups), mean
    matrix = [[sum(row.features[index] for index in group) for group in groups] for row in rows]
    if _matrix_rank(matrix) < len(groups):
        return None
    targets = [max(0.0, row.volume_m3 - _unknown_minutes(row) * mean) for row in rows]
    group_rates = _nnls(matrix, targets, [mean] * len(groups), low, high)
    expanded = [mean] * sum(len(group) for group in groups)
    for group, rate in zip(groups, group_rates, strict=True):
        for index in group:
            expanded[index] = rate
    return tuple(expanded), mean


def _predict_interval(interval: GasInterval, rates: Sequence[float], mean: float) -> float:
    known = sum(value * rate for value, rate in zip(interval.features, rates, strict=True))
    return known + _unknown_minutes(interval) * mean


def _loo_error(
    rows: Sequence[GasInterval],
    groups: tuple[tuple[int, ...], ...],
    low: float | None,
    high: float | None,
) -> float | None:
    if len(rows) < max(2, len(groups) + 2):
        return None
    errors: list[float] = []
    for heldout in range(len(rows)):
        training = [row for index, row in enumerate(rows) if index != heldout]
        fitted = _fit_candidate(training, groups, low, high)
        if fitted is None:
            return None
        rates, mean = fitted
        errors.append(abs(rows[heldout].volume_m3 - _predict_interval(rows[heldout], rates, mean)))
    return sum(errors) / len(errors)


def _unknown_model(
    edges: tuple[float, ...],
    *,
    interval_count: int,
    reasons: Sequence[str],
    low: float | None,
    high: float | None,
    has_gas_stove: bool | None,
) -> GasModel:
    count = len(edges) - 1
    complete_passport = low is not None and high is not None
    lowers: tuple[float, ...]
    uppers: tuple[float, ...]
    if low is not None and high is not None:
        mean = (low + high) / 2.0
        rates = (mean,) * count
        lowers, uppers = (low,) * count, (high,) * count
        final_reasons = tuple(dict.fromkeys((*reasons, "passport_prior_without_meter_readings")))
    else:
        mean = None
        rates = (0.0,) * count
        lowers, uppers = (), ()
        final_reasons = tuple(dict.fromkeys((*reasons, "no_valid_meter_intervals")))
    return GasModel(
        rates,
        edges,
        mean,
        extrapolated_bins=tuple(range(count)),
        passport_limited=complete_passport,
        reasons=final_reasons,
        low_support_bins=tuple(range(count)),
        interval_count=interval_count,
        passport_min_m3_per_minute=low,
        passport_max_m3_per_minute=high,
        rate_lower_m3_per_minute=lowers,
        rate_upper_m3_per_minute=uppers,
        has_gas_stove=has_gas_stove,
    )


def fit_intervals(
    intervals: Sequence[GasInterval],
    *,
    bin_edges: Sequence[float] | None = None,
    max_bins: int = 4,
    passport_min_m3_per_hour: float | None = None,
    passport_max_m3_per_hour: float | None = None,
    has_gas_stove: bool | None = None,
    **_kwargs: Any,
) -> GasModel:
    """Fit a mean or small-bin model from complete meter intervals.

    Complexity is selected on leave-one-interval-out predictions within a
    development prefix. The newest interval(s) remain untouched until the chosen
    form is evaluated, so reported validation error has no training leakage.
    """
    edges = _edges(bin_edges)
    bin_count = len(edges) - 1
    low, high, passport_reasons = _passport_bounds(passport_min_m3_per_hour, passport_max_m3_per_hour)
    reasons: list[str] = list(passport_reasons)
    usable: list[GasInterval] = []
    for interval in sorted(intervals, key=lambda item: item.end):
        if interval.volume_m3 < 0 or interval.end <= interval.start:
            reasons.append("invalid_meter_interval")
            continue
        if len(interval.features) != bin_count or any(value < 0 for value in interval.features):
            reasons.append("invalid_feature_shape")
            continue
        if interval.coverage < MIN_CALIBRATION_COVERAGE or interval.observed_minutes <= 0:
            reasons.append("telemetry_gaps_excluded")
            continue
        if interval.flame_minutes <= 0:
            if interval.volume_m3 > 0:
                reasons.append("positive_meter_volume_without_flame")
            continue
        usable.append(interval)
    if has_gas_stove is True:
        reasons.append("shared_meter_gas_stove_unseparated")
    elif has_gas_stove is None:
        reasons.append("meter_scope_unconfirmed")
    if not usable:
        return _unknown_model(
            edges,
            interval_count=len(intervals),
            reasons=tuple(dict.fromkeys(reasons)),
            low=low,
            high=high,
            has_gas_stove=has_gas_stove,
        )

    support = tuple(sum(row.features[index] for row in usable) for index in range(bin_count))
    counts = tuple(sum(row.features[index] > 0 for row in usable) for index in range(bin_count))
    extrapolated = tuple(index for index, minutes in enumerate(support) if minutes <= 0)
    low_support = tuple(index for index, count in enumerate(counts) if count < 2)
    diversity_rank = _matrix_rank([row.features for row in usable])

    heldout_count = max(1, ceil(len(usable) * 0.2)) if len(usable) >= 3 else 0
    development = usable[:-heldout_count] if heldout_count else usable
    validation = usable[-heldout_count:] if heldout_count else []
    selected_groups = _groups(bin_count, 1)
    selected_cv = _loo_error(development, selected_groups, low, high)
    for parameter_count in range(2, max(1, min(max_bins, bin_count)) + 1):
        candidate = _groups(bin_count, parameter_count)
        error = _loo_error(development, candidate, low, high)
        if error is None:
            continue
        mean_target = sum(row.volume_m3 for row in development) / len(development)
        absolute_margin = 0.01 * max(1e-9, mean_target)
        if selected_cv is None or error + absolute_margin < selected_cv * 0.9:
            selected_groups, selected_cv = candidate, error

    development_fit = _fit_candidate(development, selected_groups, low, high)
    validation_error: float | None = None
    validation_relative: float | None = None
    if validation and development_fit is not None:
        development_rates, development_mean = development_fit
        errors = [
            abs(row.volume_m3 - _predict_interval(row, development_rates, development_mean))
            for row in validation
        ]
        validation_error = sum(errors) / len(errors)
        validation_relative = sum(errors) / max(1e-9, sum(row.volume_m3 for row in validation))

    fitted = _fit_candidate(usable, selected_groups, low, high)
    assert fitted is not None
    rates, mean = fitted
    # A grouped fit must not make an unobserved constituent bin look learned.
    rates = tuple(mean if index in extrapolated else rate for index, rate in enumerate(rates))
    total_volume = sum(row.volume_m3 for row in usable)
    boundary_relative = sum(max(0.0, row.boundary_uncertainty_m3) for row in usable) / max(total_volume, 1e-9)
    average_coverage = sum(row.coverage for row in usable) / len(usable)
    base_relative = max(
        0.08,
        validation_relative or 0.0,
        boundary_relative,
        (1.0 - average_coverage) * 0.75,
        0.25 / sqrt(len(usable)),
    )
    if validation_relative is None:
        base_relative = max(base_relative, 0.30)
    lowers: list[float] = []
    uppers: list[float] = []
    for index, rate in enumerate(rates):
        spread = max(base_relative, 0.50 if index in extrapolated else 0.30 if index in low_support else 0.0)
        lower_rate, upper_rate = max(0.0, rate * (1.0 - spread)), rate * (1.0 + spread)
        if low is not None:
            lower_rate = max(low, lower_rate)
        if high is not None:
            upper_rate = min(high, upper_rate)
        lowers.append(min(lower_rate, rate))
        uppers.append(max(upper_rate, rate))

    selected_count = len(selected_groups)
    identifiable = (
        selected_count > 1
        and diversity_rank >= selected_count
        and len(usable) >= selected_count + 2
        and all(count >= 2 for count in counts if count > 0)
        and not extrapolated
    )
    if selected_count == 1:
        reasons.append("mean_model_selected")
    if diversity_rank < min(2, bin_count) or selected_count == 1 and len(usable) < 4:
        reasons.append("insufficient_interval_diversity")
    if extrapolated:
        reasons.append("unobserved_modulation_bins_use_mean_or_prior")
    if low_support:
        reasons.append("low_support_modulation_bins")
    if validation_error is None:
        reasons.append("no_heldout_validation")

    return GasModel(
        rates,
        edges,
        mean,
        support_minutes=support,
        support_intervals=counts,
        extrapolated_bins=extrapolated,
        identifiable=identifiable,
        validation_error_m3=validation_error,
        passport_limited=low is not None or high is not None,
        reasons=tuple(dict.fromkeys(reasons)),
        low_support_bins=low_support,
        selected_bin_count=selected_count,
        interval_count=len(intervals),
        usable_interval_count=len(usable),
        heldout_interval_count=heldout_count if validation_error is not None else 0,
        validation_relative_error=validation_relative,
        diversity_rank=diversity_rank,
        average_coverage=average_coverage,
        boundary_relative_uncertainty=boundary_relative,
        calibration_end=max(row.end for row in usable),
        passport_min_m3_per_minute=low,
        passport_max_m3_per_minute=high,
        rate_lower_m3_per_minute=tuple(lowers),
        rate_upper_m3_per_minute=tuple(uppers),
        has_gas_stove=has_gas_stove,
    )


def fit_gas_model(
    readings: Sequence[Any],
    samples: Iterable[Any],
    *,
    bin_edges: Sequence[float] | None = None,
    max_bins: int = 4,
    max_gap_minutes: float = 15.0,
    passport_min_m3_per_hour: float | None = None,
    passport_max_m3_per_hour: float | None = None,
    has_gas_stove: bool | None = None,
) -> GasModel:
    """Build meter intervals and delegate to the canonical interval fitter."""
    edges = _edges(bin_edges)
    return fit_intervals(
        build_intervals(readings, samples, bin_edges=edges, max_gap_minutes=max_gap_minutes),
        bin_edges=edges,
        max_bins=max_bins,
        passport_min_m3_per_hour=passport_min_m3_per_hour,
        passport_max_m3_per_hour=passport_max_m3_per_hour,
        has_gas_stove=has_gas_stove,
    )


def _age_days(end: datetime | None, calibration_end: datetime | None) -> float | None:
    if end is None or calibration_end is None:
        return None
    try:
        delta = end - calibration_end
    except TypeError:
        delta = end.replace(tzinfo=None) - calibration_end.replace(tzinfo=None)
    return max(0.0, delta.total_seconds() / 86400.0)


def _unknown_estimate(
    model: GasModel,
    coverage: float,
    flame: float,
    reasons: Sequence[str],
    *,
    age_days: float | None,
    observed_volume_m3: float | None = None,
) -> GasEstimate:
    return GasEstimate(
        None,
        "unknown",
        0.0,
        None,
        coverage,
        flame,
        None,
        model.version,
        tuple(dict.fromkeys((*model.reasons, *reasons))),
        observed_volume_m3=observed_volume_m3,
        calibration_age_days=age_days,
        uncertainty_factors=tuple(dict.fromkeys(reasons)),
        uncertainty_method=model.uncertainty_method,
    )


def estimate_exposure(
    exposure: Exposure,
    model: GasModel,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    uncertainty_m3: float = 0.0,
) -> GasEstimate:
    """Estimate a period from cached exposure and return a sensitivity range."""
    denominator = exposure.minutes
    coverage = max(0.0, min(1.0, exposure.observed_minutes / denominator)) if denominator > 0 else 0.0
    flame = max(0.0, exposure.flame_minutes)
    age_days = _age_days(end, model.calibration_end)
    if denominator <= 0 or len(exposure.bin_minutes) != len(model.bin_edges) - 1:
        return _unknown_estimate(model, coverage, flame, ("invalid_exposure",), age_days=age_days)
    if model.mean_rate_m3_per_minute is None or len(model.rates_m3_per_minute) != len(exposure.bin_minutes):
        return _unknown_estimate(model, coverage, flame, ("no_calibrated_rate",), age_days=age_days)
    assigned_flame = sum(exposure.bin_minutes) + max(0.0, exposure.unknown_modulation_minutes)
    unknown_modulation = max(0.0, exposure.unknown_modulation_minutes) + max(0.0, flame - assigned_flame)
    observed_volume = sum(
        minutes * rate for minutes, rate in zip(exposure.bin_minutes, model.rates_m3_per_minute, strict=True)
    ) + unknown_modulation * model.mean_rate_m3_per_minute
    if coverage < MIN_ESTIMATE_COVERAGE:
        return _unknown_estimate(
            model,
            coverage,
            flame,
            ("insufficient_telemetry_coverage",),
            age_days=age_days,
            observed_volume_m3=observed_volume,
        )
    if flame <= 0 and coverage < 0.95:
        return _unknown_estimate(
            model,
            coverage,
            flame,
            ("insufficient_observed_flame",),
            age_days=age_days,
            observed_volume_m3=observed_volume,
        )

    lower_rates = model.rate_lower_m3_per_minute or tuple(rate * 0.7 for rate in model.rates_m3_per_minute)
    upper_rates = model.rate_upper_m3_per_minute or tuple(rate * 1.3 for rate in model.rates_m3_per_minute)
    if (
        model.usable_interval_count == 0
        and model.passport_min_m3_per_minute is not None
        and model.passport_max_m3_per_minute is not None
    ):
        mean_lower = model.passport_min_m3_per_minute
        mean_upper = model.passport_max_m3_per_minute
    else:
        mean_spread = max(0.3, model.boundary_relative_uncertainty)
        mean_lower = max(0.0, model.mean_rate_m3_per_minute * (1.0 - mean_spread))
        mean_upper = model.mean_rate_m3_per_minute * (1.0 + mean_spread)
        if model.passport_min_m3_per_minute is not None:
            mean_lower = max(model.passport_min_m3_per_minute, mean_lower)
        if model.passport_max_m3_per_minute is not None:
            mean_upper = min(model.passport_max_m3_per_minute, mean_upper)
    observed_low = sum(
        minutes * rate for minutes, rate in zip(exposure.bin_minutes, lower_rates, strict=True)
    ) + unknown_modulation * min(mean_lower, model.mean_rate_m3_per_minute)
    observed_high = sum(
        minutes * rate for minutes, rate in zip(exposure.bin_minutes, upper_rates, strict=True)
    ) + unknown_modulation * max(mean_upper, model.mean_rate_m3_per_minute)

    used_uncertain_bins = [
        index
        for index in set((*model.extrapolated_bins, *model.low_support_bins))
        if exposure.bin_minutes[index] > 0
    ]
    factors: list[str] = []
    reasons = list(model.reasons)
    if unknown_modulation > 0:
        factors.append("unknown_modulation_uses_mean")
        reasons.append("extrapolated_unknown_modulation")
    if used_uncertain_bins:
        factors.append("unsupported_modulation_range")
        reasons.append("extrapolated_modulation_range")

    if coverage < 1.0:
        # The point estimate uses observed duty for missing time; the lower bound
        # allows zero use in the gap, so the gap is never silently called idle.
        volume = observed_volume / coverage
        estimated_gap = volume - observed_volume
        lower_bound = observed_low
        upper_bound = observed_high / coverage * (1.0 + 0.5 * (1.0 - coverage))
        factors.append("telemetry_gap_extrapolation")
        reasons.append("telemetry_gap_estimated_from_observed_mix")
    else:
        volume, estimated_gap = observed_volume, 0.0
        lower_bound, upper_bound = observed_low, observed_high

    if uncertainty_m3 > 0:
        lower_bound -= uncertainty_m3
        upper_bound += uncertainty_m3
        factors.append("caller_boundary_uncertainty")
    if model.boundary_relative_uncertainty > 0:
        boundary_spread = volume * model.boundary_relative_uncertainty
        lower_bound -= boundary_spread
        upper_bound += boundary_spread
        factors.append("meter_reading_day_boundary")
    if age_days is not None and age_days > 30:
        age_spread = volume * min(0.75, (age_days - 30.0) / 365.0 * 0.35)
        lower_bound -= age_spread
        upper_bound += age_spread
        factors.append("stale_calibration")
        reasons.append("calibration_is_stale")
    if model.has_gas_stove is True:
        lower_bound -= volume * 0.35
        upper_bound += volume * 0.20
        factors.append("unseparated_gas_stove")
    elif model.has_gas_stove is None:
        lower_bound -= volume * 0.15
        upper_bound += volume * 0.15
        factors.append("unconfirmed_meter_scope")

    lower_bound = max(0.0, min(lower_bound, volume))
    upper_bound = max(volume, upper_bound)
    extrapolated = (
        coverage < 1.0
        or unknown_modulation > 0
        or bool(used_uncertain_bins)
        or model.has_gas_stove is not False
        or age_days is not None
        and age_days > 30
    )
    status = "extrapolated" if extrapolated else "estimated"

    if model.usable_interval_count == 0:
        reliability, cap = 18.0 * coverage, 20.0
    else:
        sample_factor = min(1.0, model.usable_interval_count / 8.0)
        validation_factor = (
            0.65
            if model.validation_relative_error is None
            else max(0.25, 1.0 - min(0.75, model.validation_relative_error))
        )
        reliability, cap = (45.0 + 45.0 * sample_factor) * validation_factor * coverage, 100.0
        if model.heldout_interval_count == 0:
            cap = min(cap, 45.0)
        if model.usable_interval_count == 1:
            cap = min(cap, 25.0)
        elif model.usable_interval_count == 2:
            cap = min(cap, 35.0)
        elif model.usable_interval_count == 3:
            cap = min(cap, 45.0)
        if model.diversity_rank < min(2, len(model.rates_m3_per_minute)):
            cap = min(cap, 50.0)
    if used_uncertain_bins or unknown_modulation > 0:
        reliability *= 0.70
        cap = min(cap, 55.0)
    if coverage < 0.8:
        reliability *= coverage
        cap = min(cap, 40.0)
    if model.has_gas_stove is True:
        reliability *= 0.65
        cap = min(cap, 35.0)
    elif model.has_gas_stove is None:
        reliability *= 0.80
        cap = min(cap, 50.0)
    if model.boundary_relative_uncertainty > 0.2:
        cap = min(cap, 45.0)
    if age_days is not None and age_days > 30:
        reliability *= max(0.35, 1.0 - (age_days - 30.0) / 730.0)
        if age_days > 180:
            cap = min(cap, 40.0)

    if flame <= 0:
        reasons.append("observed_burner_off")
    return GasEstimate(
        volume,
        status,
        max(0.0, min(100.0, cap, reliability)),
        (lower_bound, upper_bound),
        coverage,
        flame,
        None,
        model.version,
        tuple(dict.fromkeys(reasons)),
        observed_volume_m3=observed_volume,
        estimated_gap_m3=estimated_gap,
        calibration_age_days=age_days,
        uncertainty_factors=tuple(dict.fromkeys(factors)),
        uncertainty_method=model.uncertainty_method,
    )


def estimate_gas(
    start: datetime,
    end: datetime,
    samples: Iterable[Any],
    model: GasModel,
    *,
    max_gap_minutes: float = 15.0,
) -> GasEstimate:
    """Convenience wrapper using the cached-exposure estimator."""
    exposure = integrate_exposure(start, end, samples, bin_edges=model.bin_edges, max_gap_minutes=max_gap_minutes)
    return estimate_exposure(exposure, model, start=start, end=end)


__all__ = [
    "ALGORITHM_VERSION",
    "StateSample",
    "MeterReading",
    "GasInterval",
    "Exposure",
    "GasModel",
    "GasEstimate",
    "integrate_flame",
    "integrate_exposure",
    "build_intervals",
    "fit_gas_model",
    "estimate_gas",
    "fit_intervals",
    "estimate_exposure",
]

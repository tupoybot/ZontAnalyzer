from __future__ import annotations

from datetime import datetime
from statistics import median

from zont_analyzer.domain import MetricValue


def _metric_id(period_id: str, name: str) -> str:
    return f"metric:{period_id}:{name}:metrics-v1"


def _segments(samples: list[tuple[datetime, float]]) -> list[tuple[datetime, datetime, float]]:
    ordered = sorted({timestamp: value for timestamp, value in samples}.items())
    if len(ordered) < 2:
        return []
    gaps = [(ordered[i + 1][0] - ordered[i][0]).total_seconds() for i in range(len(ordered) - 1)]
    expected = median(gap for gap in gaps if gap > 0)
    maximum = max(expected * 2.5, 60)
    result = []
    for index, (started, value) in enumerate(ordered[:-1]):
        ended = ordered[index + 1][0]
        if 0 < (ended - started).total_seconds() <= maximum:
            result.append((started, ended, value))
    return result


def temperature_metrics(
    samples: list[tuple[datetime, float]],
    *,
    period_id: str,
    target_c: float | None,
    comfort_band_c: float,
    target_samples: list[tuple[datetime, float]] | None = None,
    ignore_windows: list[tuple[datetime, datetime]] | None = None,
) -> list[MetricValue]:
    segments = _segments(samples)
    if not segments:
        return []
    total_seconds = sum((end - start).total_seconds() for start, end, _ in segments)
    values = [value for _, _, value in segments]
    metrics = [
        MetricValue(
            id=_metric_id(period_id, "mean_temperature_c"),
            name="mean_temperature_c",
            value=round(
                sum(value * (end - start).total_seconds() for start, end, value in segments) / total_seconds,
                3,
            ),
            unit="°C",
        ),
        MetricValue(
            id=_metric_id(period_id, "temperature_range_c"),
            name="temperature_range_c",
            value=round(max(values) - min(values), 3),
            unit="°C",
        ),
    ]
    target_segments = [
        segment
        for segment in segments
        if not any(window_start <= segment[0] < window_end for window_start, window_end in (ignore_windows or []))
    ]
    ordered_targets = sorted(target_samples or [])
    if target_c is None and not ordered_targets:
        return metrics
    in_band = 0.0
    absolute_error = 0.0
    above_seconds = 0.0
    below_seconds = 0.0
    above_degree_hours = 0.0
    below_degree_hours = 0.0
    targeted_seconds = 0.0
    target_index = 0
    current_target = target_c
    for start, end, value in target_segments:
        while target_index < len(ordered_targets) and ordered_targets[target_index][0] <= start:
            current_target = ordered_targets[target_index][1]
            target_index += 1
        if current_target is None:
            continue
        seconds = (end - start).total_seconds()
        targeted_seconds += seconds
        error = value - current_target
        if abs(error) <= comfort_band_c:
            in_band += seconds
        elif error > comfort_band_c:
            above_seconds += seconds
        else:
            below_seconds += seconds
        absolute_error += abs(error) * seconds
        above_degree_hours += max(0.0, error) * seconds / 3600
        below_degree_hours += max(0.0, -error) * seconds / 3600
    if targeted_seconds == 0:
        return metrics
    values_to_add = [
        ("heating_target_evaluation_time_pct", targeted_seconds / total_seconds * 100, "%"),
        ("time_in_target_band_pct", in_band / targeted_seconds * 100, "%"),
        ("time_above_target_band_pct", above_seconds / targeted_seconds * 100, "%"),
        ("time_below_target_band_pct", below_seconds / targeted_seconds * 100, "%"),
        ("mean_absolute_target_error_c", absolute_error / targeted_seconds, "°C"),
        ("degree_hours_above_target", above_degree_hours, "°C·h"),
        ("degree_hours_below_target", below_degree_hours, "°C·h"),
    ]
    if above_seconds:
        values_to_add.append(
            ("mean_error_while_above_target_c", above_degree_hours / (above_seconds / 3600), "°C")
        )
    if below_seconds:
        values_to_add.append(
            ("mean_error_while_below_target_c", below_degree_hours / (below_seconds / 3600), "°C")
        )
    metrics.extend(
        MetricValue(id=_metric_id(period_id, name), name=name, value=round(value, 3), unit=unit)
        for name, value, unit in values_to_add
    )
    return metrics


def burner_metrics(
    samples: list[tuple[datetime, float]],
    *,
    period_id: str,
    period_hours: float,
    short_cycle_minutes: float,
    ignore_windows: list[tuple[datetime, datetime]] | None = None,
) -> list[MetricValue]:
    del period_hours  # Starts/hour is normalized by observed, gap-filtered time.
    ordered_samples = sorted({timestamp: value for timestamp, value in samples}.items())
    segments = [
        segment
        for segment in _segments(samples)
        if not any(window_start <= segment[0] < window_end for window_start, window_end in (ignore_windows or []))
    ]
    if not segments:
        return []
    activity = [(start, end, value > 0) for start, end, value in segments]
    active_seconds = sum((end - start).total_seconds() for start, end, on in activity if on)
    observed_seconds = sum((end - start).total_seconds() for start, end, _ in activity)
    cycles: list[float] = []
    current_start: datetime | None = None
    previous_end: datetime | None = None
    for start, end, on in activity:
        if previous_end is None or start != previous_end:
            current_start = start if on else None
            previous_end = end
            continue
        if on and current_start is None:
            current_start = start
        if not on and current_start is not None:
            cycles.append((start - current_start).total_seconds())
            current_start = None
        previous_end = end
    if current_start is not None and ordered_samples[-1][1] <= 0:
        cycles.append((ordered_samples[-1][0] - current_start).total_seconds())
    short = [duration for duration in cycles if duration < short_cycle_minutes * 60]
    metrics = [
        ("burner_starts", float(len(cycles)), "count"),
        ("burner_starts_per_hour", len(cycles) / max(observed_seconds / 3600, 0.001), "1/h"),
        ("burner_duty_cycle_pct", active_seconds / max(observed_seconds, 1) * 100, "%"),
        ("short_cycle_share_pct", len(short) / max(len(cycles), 1) * 100, "%"),
        (
            "median_burner_cycle_minutes",
            median(cycles) / 60 if cycles else 0,
            "min",
        ),
    ]
    return [
        MetricValue(id=_metric_id(period_id, name), name=name, value=round(value, 3), unit=unit)
        for name, value, unit in metrics
    ]

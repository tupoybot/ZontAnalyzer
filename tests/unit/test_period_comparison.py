from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.evidence import NumericSample, SignalMetadata, SignalSeries, StateSample
from zont_analyzer.application.period_comparison import (
    ComparisonWindow,
    build_period_context,
    compare_periods,
    select_baseline,
)


def _window(
    label: str,
    start: datetime,
    *,
    outdoor: float = 0,
    dhw: bool = False,
    mode: str = "auto",
) -> ComparisonWindow:
    points = tuple(
        NumericSample(start + timedelta(minutes=i * 10), value)
        for i, value in enumerate([20, 20.5, 21, 20.5, 20])
    )
    outdoor_series = SignalSeries(
        "outdoor",
        SignalMetadata("outdoor", "Outdoor", "°C", role="outdoor_temperature"),
        tuple(NumericSample(point.timestamp, outdoor) for point in points),
    )
    room_series = SignalSeries("room", SignalMetadata("room", "Room", "°C", role="room"), points)
    return_series = SignalSeries(
        "return", SignalMetadata("return", "Return", "°C", role="return_temperature"),
        tuple(NumericSample(point.timestamp, 30 + i) for i, point in enumerate(points)),
    )
    flags = frozenset({"ch", "fl", "dhw"} if dhw else {"ch", "fl"})
    states = tuple(StateSample(start + timedelta(minutes=i * 10), flags) for i in range(5))
    return ComparisonWindow(
        label=label,
        start=start,
        end=start + timedelta(minutes=40),
        signals=(outdoor_series, room_series, return_series),
        states=states, mode=mode,
    )


def test_comparison_has_same_kpis_and_room_return_dhw_context() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = compare_periods(_window("before", start), _window("after", start + timedelta(days=2)))

    names = {metric.name for metric in result.metrics}
    assert "burner_runtime_request_ratio" in names
    assert result.context.before["room_mean_c"] == pytest.approx(20.4)
    assert result.context.before["return_mean_c"] == pytest.approx(32.0)
    assert result.context.before["dhw_share_pct"] == 0
    runtime = next(metric for metric in result.metrics if metric.name == "burner_runtime_request_ratio")
    assert runtime.before_denominator is not None
    assert runtime.relative_change_pct == pytest.approx(0)
    assert result.status == "comparable"


def test_weather_dhw_and_second_intervention_are_explicit_confounders() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    before = _window("before", start, outdoor=-5)
    after = _window("after", start + timedelta(days=2), outdoor=10, dhw=True)
    result = compare_periods(
        before,
        after,
        intervention_at=start + timedelta(hours=4),
        interventions=(start + timedelta(days=1),),
    )

    assert result.status == "limited"
    assert "weather_not_comparable" in result.confounders
    assert "dhw_influence_not_comparable" in result.confounders
    assert "second_intervention_between_windows" in result.confounders
    assert "comparison_not_isolated" in result.unknowns


def test_relative_change_never_uses_zero_denominator() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    before = _window("before", start)
    after = _window("after", start + timedelta(days=2))
    result = compare_periods(before, after)
    metric = next(item for item in result.metrics if item.name == "burner_starts_per_active_request_hour")
    # A zero before value has no valid relative denominator; absolute change is
    # still available and the reason is explicit.
    assert metric.relative_change_pct is None
    assert metric.unavailable_reason == "relative_change_invalid_zero_denominator"


def test_baseline_selection_requires_weather_mode_and_coverage() -> None:
    start = datetime(2026, 1, 10, tzinfo=UTC)
    target = _window("target", start, outdoor=-3)
    good = _window("good", start - timedelta(days=2), outdoor=-4)
    wrong_weather = _window("wrong-weather", start - timedelta(days=4), outdoor=8)
    wrong_mode = _window("wrong-mode", start - timedelta(days=6), outdoor=-4, mode="manual")

    selected = select_baseline(target, (wrong_weather, wrong_mode, good))
    assert selected is not None and selected.label == "good"


def test_period_context_is_bounded_and_does_not_call_ai() -> None:
    start = datetime(2026, 1, 10, tzinfo=UTC)
    target = _window("target", start)
    candidates = [_window(f"b{i}", start - timedelta(days=i + 2)) for i in range(20)]
    calls: list[str] = []

    def analyze(left: datetime, right: datetime, kind: str) -> ComparisonWindow:
        calls.append(kind)
        return target.model_copy(update={"start": left, "end": right})

    class Period:
        kind = "weekly"
        label = "period"
        start = target.start
        end = target.end

    periods = [
        type(
            "P",
            (),
            {"kind": "weekly", "label": item.label, "start": item.start, "end": item.end},
        )()
        for item in candidates
    ]
    result = build_period_context(
        period=Period(),
        baseline_periods=periods,
        analyze_window=analyze,
        max_analyses=4,
    )
    assert len(result["period_comparisons"]) == 3
    assert len(calls) == 4


def test_overlapping_windows_are_rejected() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="overlap"):
        compare_periods(_window("a", start), _window("b", start + timedelta(minutes=20)))

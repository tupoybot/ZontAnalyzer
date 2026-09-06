from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zont_analyzer.analytics.evidence import (
    ExclusionWindow,
    NumericSample,
    SignalMetadata,
    SignalSeries,
    StateSample,
    build_evidence,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _series(key: str, role: str, values: list[tuple[int, float]]) -> SignalSeries:
    return SignalSeries(
        key=key,
        metadata=SignalMetadata(
            identity=f"device:source:entity:{key}",
            display_name=key,
            unit="celsius",
            provenance="zont_history",
            role=role,  # type: ignore[arg-type]  # concise fixture accepts known role strings
        ),
        samples=tuple(NumericSample(START + timedelta(minutes=minute), value) for minute, value in values),
    )


def _metric(packet: object, name: str) -> float | None:
    return next(item.value for item in packet.metrics if item.name == name)  # type: ignore[union-attr]


def test_time_weighted_alignment_weather_and_stable_ids_are_input_order_independent() -> None:
    signals = [
        _series("flow", "flow_temperature", [(0, 40), (60, 45), (120, 50)]),
        _series("target_flow", "target_flow_temperature", [(0, 38), (60, 40), (120, 45)]),
        _series("control", "control_temperature", [(0, 20), (60, 21), (120, 22)]),
        _series("target", "target_temperature", [(0, 21), (60, 21), (120, 21)]),
        _series("weather", "outdoor_temperature", [(0, -4), (30, -4), (60, 3), (120, 3)]),
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=2),
        timezone="UTC",
        period_id="day",
        signals=list(reversed(signals)),
    )
    same = build_evidence(
        start=START,
        end=START + timedelta(hours=2),
        timezone="UTC",
        period_id="day",
        signals=signals,
    )

    assert [item.id for item in packet.windows] == [item.id for item in same.windows]
    first = packet.windows[0]
    assert first.facts["room_error_c"].mean == -1
    assert first.facts["flow_vs_cs_c"].mean == 2
    assert "delta_t_c" not in first.facts
    assert first.facts["weather_plateau_transitions"].mean == 1
    assert first.facts["weather_jumps_over_5c"].mean == 1
    assert packet.quality["control"].coverage_pct == 100
    assert "missing:return_temperature" in packet.unknowns


def test_cycle_kpis_exclude_noise_and_keep_flame_independent_of_zero_modulation() -> None:
    states = [
        StateSample(START + timedelta(minutes=minute), frozenset(flags), identity="state:z3k")
        for minute, flags in [
            (0, ["ch"]),
            (10, ["ch", "fl"]),
            (20, ["ch"]),
            (30, ["ch", "fl"]),
            (40, ["ch"]),
            (60, ["ch"]),
        ]
    ]
    modulation = _series("mod", "modulation", [(0, 50), (10, 0), (20, 20), (30, 0), (40, 30), (60, 30)])
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=1),
        timezone="UTC",
        period_id="day",
        signals=[modulation],
        state_samples=states,
        exclusions=[
            ExclusionWindow(START + timedelta(minutes=30), START + timedelta(minutes=40), "noise", "flame-noise")
        ],
        capability_profile="flame_zero_is_minimum",
    )

    # The denominator is qualified observed time: the 10-minute noise window
    # is excluded alongside its censored flame cycle.
    assert _metric(packet, "burner_starts_per_observed_hour") == 1.2
    assert _metric(packet, "burner_cycle_median_seconds") == 600.0
    assert _metric(packet, "flame_modulation_mean") == 0.0
    assert packet.state_source == "state:z3k"
    assert packet.exclusion_windows[0].reason == "noise"


def test_long_period_uses_same_bounded_hourly_window_limit_and_missing_state_is_explicit() -> None:
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=50),
        timezone="Europe/Samara",
        period_id="long",
        signals=[],
        max_windows=3,
    )

    assert len(packet.windows) == 3
    assert all(item.kind == "hour" for item in packet.windows)
    assert "insufficient:boiler_state_coverage" in packet.unknowns
    assert _metric(packet, "burner_starts_per_observed_hour") is None


def test_dhw_is_excluded_from_heating_kpis_without_a_dhw_temperature_series() -> None:
    states = [
        StateSample(START + timedelta(minutes=minute), frozenset(flags))
        for minute, flags in [(0, ["ch"]), (10, ["ch", "fl"]), (20, ["dhw", "fl"]), (30, ["ch"]), (60, ["ch"])]
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=1),
        timezone="UTC",
        period_id="dhw",
        signals=[],
        state_samples=states,
    )

    assert any(item.reason == "dhw" for item in packet.exclusion_windows)
    assert _metric(packet, "burner_starts_per_observed_hour") == 1.2


def test_packet_size_is_hard_bounded_with_many_series() -> None:
    series = [
        _series(f"room:{index}", "room", [(minute, float(index)) for minute in range(0, 240, 10)])
        for index in range(60)
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=4),
        timezone="UTC",
        period_id="size",
        signals=series,
    )

    assert len(packet.model_dump_json().encode()) <= 40_000
    assert any(item.startswith("omitted_signal_count:") for item in packet.unknowns)

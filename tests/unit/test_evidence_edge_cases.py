"""Regression specifications for stage-2 evidence edge cases."""

from datetime import UTC, datetime, timedelta

from zont_analyzer.analytics.evidence import (
    MAX_PACKET_BYTES,
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
        metadata=SignalMetadata(identity=key, display_name=key, unit="celsius", role=role),  # type: ignore[arg-type]
        samples=tuple(NumericSample(START + timedelta(minutes=minute), value) for minute, value in values),
    )


def test_packet_cap_holds_for_a_pathological_signal_identity() -> None:
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=2),
        timezone="UTC",
        signals=[_series("signal:" + "x" * 50_000, "room", [(0, 20), (60, 21), (120, 22)])],
    )

    assert len(packet.model_dump_json().encode()) <= MAX_PACKET_BYTES


def test_flow_vs_cs_is_available_with_complete_heating_demand_coverage() -> None:
    states = [
        StateSample(START + timedelta(minutes=minute), frozenset(flags))
        for minute, flags in [(0, ["ch"]), (10, ["ch"]), (20, ["ch"]), (30, []), (60, [])]
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=1),
        timezone="UTC",
        signals=[
            _series("flow", "flow_temperature", [(0, 40), (10, 40), (20, 40), (30, 40), (60, 40)]),
            _series("cs", "target_flow_temperature", [(0, 38), (10, 38), (20, 38), (30, 38), (60, 38)]),
        ],
        state_samples=states,
    )

    metric = next(item for item in packet.metrics if item.name == "flow_vs_cs_typical_c")
    assert metric.value == 2
    assert metric.denominator_unit == "qualified_heating_seconds"


def test_state_coverage_rejects_a_long_interior_gap() -> None:
    states = [
        StateSample(START, frozenset({"ch"})),
        StateSample(START + timedelta(minutes=5), frozenset({"ch"})),
        StateSample(START + timedelta(minutes=100), frozenset({"ch"})),
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(minutes=101),
        timezone="UTC",
        signals=[],
        state_samples=states,
    )

    metric = next(item for item in packet.metrics if item.name == "burner_starts_per_observed_hour")
    assert metric.coverage_pct < 20
    assert metric.unavailable_reason == "insufficient_boiler_state_coverage"


def test_size_shedding_keeps_a_spread_of_windows_and_the_hard_cap() -> None:
    signals = [
        SignalSeries(
            key=f"room:{index}",
            metadata=SignalMetadata(
                identity="source:" + "x" * 800,
                display_name=f"Room {index}",
                unit="celsius",
                role="room",
            ),
            samples=tuple(NumericSample(START + timedelta(hours=hour), float(hour)) for hour in range(25)),
        )
        for index in range(24)
    ]
    packet = build_evidence(
        start=START,
        end=START + timedelta(hours=24),
        timezone="UTC",
        signals=signals,
        exclusions=[
            ExclusionWindow(START + timedelta(minutes=index), START + timedelta(minutes=index + 1), "noise")
            for index in range(64)
        ],
        max_windows=24,
    )

    assert len(packet.model_dump_json().encode()) <= MAX_PACKET_BYTES
    assert len(packet.windows) >= 3
    starts = [window.started_at for window in packet.windows]
    assert min(starts) <= START + timedelta(hours=2)
    assert max(starts) >= START + timedelta(hours=22)


def test_duplicate_timestamp_values_do_not_depend_on_input_order() -> None:
    metadata = SignalMetadata(identity="control", display_name="Control", unit="celsius", role="control_temperature")
    forward = SignalSeries(
        "control",
        metadata,
        (NumericSample(START, 20), NumericSample(START, 25), NumericSample(START + timedelta(hours=1), 21)),
    )
    reverse = SignalSeries(
        "control",
        metadata,
        (NumericSample(START, 25), NumericSample(START, 20), NumericSample(START + timedelta(hours=1), 21)),
    )

    first = build_evidence(start=START, end=START + timedelta(hours=1), timezone="UTC", signals=[forward])
    second = build_evidence(start=START, end=START + timedelta(hours=1), timezone="UTC", signals=[reverse])

    assert first.quality["control"] == second.quality["control"]


def test_time_weighted_percentiles_do_not_overweight_dense_short_bursts() -> None:
    samples = tuple(
        [NumericSample(START + timedelta(minutes=minute), 2) for minute in range(58)]
        + [NumericSample(START + timedelta(seconds=second), 10) for second in range(3480, 3600)]
    )
    flow = SignalSeries("flow", SignalMetadata("flow", "Flow", "°C", role="flow_temperature"), samples)
    modulation = SignalSeries("mod", SignalMetadata("mod", "Modulation", "%", role="modulation"), samples)
    cs = _series("cs", "target_flow_temperature", [(minute, 0) for minute in range(61)])
    states = [StateSample(START + timedelta(minutes=minute), frozenset({"ch", "fl"})) for minute in range(61)]
    packet = build_evidence(start=START, end=START + timedelta(hours=1), timezone="UTC",
                            signals=[flow, cs, modulation], state_samples=states)
    metrics = {item.name: item for item in packet.metrics}
    assert metrics["flow_vs_cs_typical_c"].value == 2
    assert metrics["flow_vs_cs_p90_c"].value == 2
    assert metrics["flow_vs_cs_max_positive_overshoot_c"].value == 10
    assert metrics["flame_modulation_median"].value == 2


def test_request_flag_change_during_existing_flame_does_not_create_start() -> None:
    states = [StateSample(START + timedelta(minutes=minute), frozenset(flags)) for minute, flags in (
        (0, ["fl"]), (10, ["fl", "ch"]), (20, ["fl", "ch"]), (30, ["ch"]), (60, ["ch"]),
    )]
    packet = build_evidence(start=START, end=START + timedelta(hours=1), timezone="UTC",
                            signals=[], state_samples=states)
    metrics = {item.name: item for item in packet.metrics}
    assert metrics["burner_starts_per_observed_hour"].value == 0
    assert metrics["burner_cycle_median_seconds"].value is None

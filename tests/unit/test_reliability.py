from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.reliability import (
    ReliabilityEvidencePoint,
    ReliabilityEvidenceSeries,
    analyze_reliability,
)
from zont_analyzer.domain import SourceEvent

START = datetime(2026, 8, 1, tzinfo=UTC)


def _event(minutes: int, event_type: str) -> SourceEvent:
    return SourceEvent(
        id=f"{event_type}:{minutes}",
        device_id="1",
        event_type=event_type,
        timestamp_utc=START + timedelta(minutes=minutes),
    )


def _timestamps(start_minute: int, end_minute: int) -> list[datetime]:
    return [START + timedelta(minutes=minute) for minute in range(start_minute, end_minute + 1)]


def _status(start_minute: int, end_minute: int, value: int = 73) -> list[tuple[datetime, float]]:
    return [(item, float(value)) for item in _timestamps(start_minute, end_minute)]


def _activity(*minutes: int) -> list[ReliabilityEvidenceSeries]:
    return [
        ReliabilityEvidenceSeries(
            series_id=42,
            role="burner_activity",
            provenance="z3k_boiler_adapter.rml",
            origin="device",
            evidence_kind="activity",
            points=tuple(ReliabilityEvidencePoint(timestamp_utc=START + timedelta(minutes=item)) for item in minutes),
        )
    ]


def test_mtbf_includes_completed_intervals_and_current_uptime() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(hours=5),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(60, "LossConnectionBoiler"),
            _event(70, "ReconnectingBoiler"),
            _event(180, "LossConnectionBoiler"),
            _event(200, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 300),
        zont_status_samples=_status(0, 300),
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["boiler_uptime_seconds"].value == 100 * 60
    assert metrics["zont_uptime_seconds"].value == 300 * 60
    assert metrics["boiler_mtbf_hours"].value == 4.5
    assert metrics["boiler_mtbf_hours"].context == {
        "formula": "observed_operating_seconds / confirmed_service_failures",
        "observed_operating_seconds": 270 * 60,
        "observation_start": START.isoformat(),
        "confirmed_failures": 0,
        "completed_failures": 0,
        "lower_bound": True,
        "includes_current_uptime": True,
    }
    assert "boiler_mttr_hours" not in metrics


def test_mtbf_grows_while_current_uptime_continues() -> None:
    source_events = [
        _event(0, "ReconnectingBoiler"),
        _event(60, "LossConnectionBoiler"),
        _event(70, "ReconnectingBoiler"),
        _event(180, "LossConnectionBoiler"),
        _event(200, "ReconnectingBoiler"),
    ]
    earlier = analyze_reliability(
        period_id="earlier",
        period_start=START,
        as_of=START + timedelta(minutes=240),
        source_events=source_events,
        boiler_metric_timestamps=_timestamps(0, 240),
        zont_status_samples=_status(0, 240),
    )
    later = analyze_reliability(
        period_id="later",
        period_start=START,
        as_of=START + timedelta(minutes=300),
        source_events=source_events,
        boiler_metric_timestamps=_timestamps(0, 300),
        zont_status_samples=_status(0, 300),
    )

    earlier_mtbf = next(item.value for item in earlier.metrics if item.name == "boiler_mtbf_hours")
    later_mtbf = next(item.value for item in later.metrics if item.name == "boiler_mtbf_hours")
    assert earlier_mtbf == 3.5
    assert later_mtbf == 4.5


def test_mtbf_without_failures_is_a_lower_bound_instead_of_infinity() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(hours=5),
        source_events=[],
        boiler_metric_timestamps=_timestamps(0, 300),
        zont_status_samples=_status(0, 300),
    )

    metric = next(item for item in result.metrics if item.name == "boiler_mtbf_hours")
    assert metric.value == 5
    assert metric.context["lower_bound"] is True
    assert metric.context["confirmed_failures"] == 0
    assert metric.context["completed_failures"] == 0


def test_power_related_boiler_loss_is_a_service_failure() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=180),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(30, "MainPowerLost"),
            _event(31, "LossConnectionBoiler"),
            _event(90, "MainPowerFound"),
            _event(91, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 180),
        zont_status_samples=_status(0, 180),
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 180 * 60
    assert metrics["boiler_mtbf_hours"].value == pytest.approx(119 / 60, abs=0.001)
    assert metrics["boiler_mtbf_hours"].context["confirmed_failures"] == 1
    assert metrics["boiler_mttr_hours"].value == 1
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.severity == "warning"
    assert loss.details["cause"] == "power_outage"
    assert loss.details["excluded_from_boiler_reliability"] is False
    assert result.context["boiler"]["power_related_losses"] == 1  # type: ignore[index]


@pytest.mark.parametrize(("loss_minute", "expected_cause"), [(32, "power_outage"), (33, "boiler_or_adapter")])
def test_power_cause_uses_named_timestamp_tolerance(loss_minute: int, expected_cause: str) -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=120),
        source_events=[
            _event(30, "MainPowerLost"),
            _event(loss_minute, "LossConnectionBoiler"),
            _event(90, "MainPowerFound"),
            _event(91, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 120),
        zont_status_samples=_status(0, 120),
    )

    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["cause"] == expected_cause
    assert loss.details["service_impact"] == (
        "confirmed_service_failure" if expected_cause == "power_outage" else "unknown_service_impact"
    )


def test_main_power_loss_without_boiler_loss_does_not_create_failure() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=120),
        source_events=[_event(30, "MainPowerLost"), _event(90, "MainPowerFound")],
        boiler_metric_timestamps=_timestamps(0, 120),
        zont_status_samples=_status(0, 120),
    )

    metric = next(item for item in result.metrics if item.name == "boiler_mtbf_hours")
    assert metric.value == 2
    assert metric.context["lower_bound"] is True
    assert result.context["boiler"]["confirmed_service_failures"] == 0  # type: ignore[index]
    assert not any(item.kind == "boiler_connection_loss" for item in result.events)


def test_power_on_and_missing_restore_event_use_first_sustained_metrics() -> None:
    zont_times = [START, START + timedelta(minutes=20), *_timestamps(30, 60)]
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(0, "PowerOff"),
            _event(20, "PowerOn"),
            _event(35, "ReconnectingBoiler"),
            _event(50, "LossConnectionBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(30, 60),
        zont_status_samples=[(item, 73.0) for item in zont_times],
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 30 * 60
    assert metrics["boiler_uptime_seconds"].value == 10 * 60
    assert result.context["boiler"]["online"] is True  # type: ignore[index]
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["restore_inferred_from_stable_metrics"] is True


def test_open_boiler_loss_without_stable_metrics_reports_zero_uptime() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(10, "ReconnectingBoiler"), _event(50, "LossConnectionBoiler")],
        boiler_metric_timestamps=_timestamps(0, 49),
        zont_status_samples=_status(0, 60),
    )

    metric = next(item for item in result.metrics if item.name == "boiler_uptime_seconds")
    assert metric.value == 0
    assert metric.context["online"] is False
    assert result.context["boiler"]["online"] is False  # type: ignore[index]
    metrics = {item.name: item for item in result.metrics}
    assert "boiler_mtbf_hours" not in metrics
    assert "boiler_mttr_hours" not in metrics


def test_stale_telemetry_reports_zero_uptime_and_suppresses_reliability_means() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(days=9),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(60, "LossConnectionBoiler"),
            _event(70, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 70),
        zont_status_samples=_status(0, 70),
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 0
    assert metrics["zont_uptime_seconds"].context["online"] is False
    assert metrics["boiler_uptime_seconds"].value == 0
    assert metrics["boiler_uptime_seconds"].context["online"] is False
    assert "boiler_mtbf_hours" not in metrics
    assert "boiler_mttr_hours" not in metrics
    assert result.context["zont"]["data_fresh"] is False  # type: ignore[index]
    assert result.context["boiler"]["data_fresh"] is False  # type: ignore[index]


def test_zont_gap_resets_uptime_and_is_excluded_from_boiler_mtbf() -> None:
    zont_times = [*_timestamps(0, 10), *_timestamps(90, 120)]
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=120),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(100, "LossConnectionBoiler"),
            _event(110, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=[*_timestamps(0, 10), *_timestamps(90, 120)],
        zont_status_samples=[(item, 73.0) for item in zont_times],
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 30 * 60
    assert metrics["boiler_uptime_seconds"].value == 10 * 60
    assert metrics["boiler_mtbf_hours"].context["confirmed_failures"] == 0
    assert "boiler_mttr_hours" not in metrics
    assert result.context["zont"]["telemetry_gaps"] == 1  # type: ignore[index]


def test_zont_restart_is_observability_only() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(30, "PowerOff"),
            _event(31, "LossConnectionBoiler"),
            _event(40, "PowerOn"),
            _event(41, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["boiler_mtbf_hours"].value == pytest.approx(49 / 60, abs=0.001)
    assert metrics["boiler_mtbf_hours"].context["lower_bound"] is True
    assert "boiler_mttr_hours" not in metrics
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["cause"] == "zont_restart"
    assert loss.details["excluded_from_boiler_reliability"] is True
    assert result.context["boiler"]["confirmed_service_failures"] == 0  # type: ignore[index]


def test_zont_firmware_restart_without_boiler_loss_keeps_boiler_uptime() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(30, "PowerOff"),
            _event(31, "PowerOn"),
        ],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 29 * 60
    assert metrics["boiler_uptime_seconds"].value == 60 * 60
    assert metrics["boiler_uptime_seconds"].context["basis"] == "boiler_connection_restored"


def test_activity_inside_completed_loss_confirms_running_and_preserves_uptime() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(20, "PowerOff"),
            _event(21, "LossConnectionBoiler"),
            _event(40, "PowerOn"),
            _event(41, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=_activity(30),
    )
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "confirmed_service_running"
    assert loss.details["excluded_from_boiler_reliability"] is True
    assert loss.details["evidence"][0]["evidence_id"] == "LossConnectionBoiler:21"
    assert any(item["role"] == "burner_activity" for item in loss.details["evidence"])
    assert loss.details["reconciliation_version"] == "reliability-reconciliation-v1"
    assert result.context["boiler"]["confirmed_service_running"] == 1  # type: ignore[index]
    assert result.context["boiler"]["confirmed_service_failures"] == 0  # type: ignore[index]


def test_thermal_only_inside_loss_is_unknown_and_residual_heat_is_not_running() -> None:
    thermal = ReliabilityEvidenceSeries(
        series_id=43,
        role="flow_temperature",
        provenance="z3k_boiler_adapter.bt",
        origin="device",
        evidence_kind="thermal",
        points=(ReliabilityEvidencePoint(timestamp_utc=START + timedelta(minutes=30), value_num=55.0),),
    )
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(10, "LossConnectionBoiler"), _event(40, "ReconnectingBoiler")],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=[thermal],
    )
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "unknown_service_impact"
    assert result.context["boiler"]["unknown_service_impact"] == 1  # type: ignore[index]
    assert next(item for item in result.metrics if item.name == "boiler_mtbf_hours").context["confirmed_failures"] == 0


def test_activity_before_and_after_loss_does_not_confirm_service_running() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(20, "LossConnectionBoiler"), _event(40, "ReconnectingBoiler")],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=_activity(19, 41),
    )
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "unknown_service_impact"


def test_dhw_context_inside_loss_does_not_replace_direct_boiler_activity() -> None:
    dhw_context = ReliabilityEvidenceSeries(
        series_id=44,
        role="dhw_activity",
        provenance="heating_circuit.dhw",
        origin="device",
        evidence_kind="context",
        points=(ReliabilityEvidencePoint(timestamp_utc=START + timedelta(minutes=30), value_num=1.0),),
    )
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(20, "LossConnectionBoiler"), _event(40, "ReconnectingBoiler")],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=[dhw_context],
    )

    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "unknown_service_impact"


def test_invalid_activity_evidence_is_not_used() -> None:
    invalid_activity = ReliabilityEvidenceSeries(
        series_id=45,
        role="boiler_adapter_state",
        provenance="z3k_boiler_adapter.s",
        origin="device",
        evidence_kind="activity",
        points=(
            ReliabilityEvidencePoint(
                timestamp_utc=START + timedelta(minutes=30),
                value_text="invalid",
                quality="invalid",
            ),
        ),
    )
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(20, "LossConnectionBoiler"), _event(40, "ReconnectingBoiler")],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=[invalid_activity],
    )

    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "unknown_service_impact"


def test_power_outage_is_confirmed_failure_even_with_activity_evidence() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(20, "MainPowerLost"),
            _event(21, "LossConnectionBoiler"),
            _event(40, "MainPowerFound"),
            _event(41, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        evidence_series=_activity(30),
    )
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.details["service_impact"] == "confirmed_service_failure"
    assert result.context["boiler"]["confirmed_service_failures"] == 1  # type: ignore[index]


def test_unobserved_power_restore_is_not_used_for_mttr() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[
            _event(20, "MainPowerLost"),
            _event(21, "LossConnectionBoiler"),
            _event(49, "MainPowerFound"),
            _event(50, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 60),
        zont_status_samples=_status(0, 60),
        zont_metric_timestamps=[*_timestamps(0, 10), *_timestamps(50, 60)],
    )

    assert result.context["boiler"]["confirmed_service_failures"] == 1  # type: ignore[index]
    assert result.context["boiler"]["service_failures_with_unknown_restore"] == 1  # type: ignore[index]
    assert "boiler_mttr_hours" not in {item.name for item in result.metrics}

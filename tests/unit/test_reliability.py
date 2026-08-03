from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zont_analyzer.analytics.reliability import analyze_reliability
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


def test_uptime_mtbf_and_mtbr_use_completed_boiler_event_pairs() -> None:
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
    assert metrics["boiler_mtbf_hours"].value == pytest.approx((60 + 110) / 2 / 60, abs=0.001)
    assert metrics["boiler_mtbr_hours"].value == pytest.approx(15 / 60)


def test_power_related_boiler_loss_is_excluded_and_does_not_reset_zont_uptime() -> None:
    status = [*_status(0, 29, 73), *_status(30, 89, 72), *_status(90, 180, 73)]
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=180),
        source_events=[
            _event(0, "ReconnectingBoiler"),
            _event(31, "LossConnectionBoiler"),
            _event(91, "ReconnectingBoiler"),
        ],
        boiler_metric_timestamps=_timestamps(0, 180),
        zont_status_samples=status,
    )
    metrics = {item.name: item for item in result.metrics}

    assert metrics["zont_uptime_seconds"].value == 180 * 60
    assert "boiler_mtbf_hours" not in metrics
    assert "boiler_mtbr_hours" not in metrics
    loss = next(item for item in result.events if item.kind == "boiler_connection_loss")
    assert loss.severity == "info"
    assert loss.details["cause"] == "power_outage"
    assert result.context["boiler"]["power_related_losses"] == 1  # type: ignore[index]


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


def test_open_boiler_loss_without_stable_metrics_has_no_uptime() -> None:
    result = analyze_reliability(
        period_id="day",
        period_start=START,
        as_of=START + timedelta(minutes=60),
        source_events=[_event(10, "ReconnectingBoiler"), _event(50, "LossConnectionBoiler")],
        boiler_metric_timestamps=_timestamps(0, 49),
        zont_status_samples=_status(0, 60),
    )

    assert "boiler_uptime_seconds" not in {item.name for item in result.metrics}
    assert result.context["boiler"]["online"] is False  # type: ignore[index]

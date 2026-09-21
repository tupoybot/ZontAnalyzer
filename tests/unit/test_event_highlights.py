from datetime import UTC, datetime, timedelta

from zont_analyzer.domain import DetectedEvent
from zont_analyzer.reports.event_highlights import is_routine, rank_events


def event(kind: str, *, severity: str = "info", minutes: int = 0, **details: object) -> DetectedEvent:
    start = datetime(2026, 9, 20, 12, tzinfo=UTC)
    return DetectedEvent(
        id=f"event:{kind}:{minutes}",
        kind=kind,
        started_at=start,
        ended_at=start + timedelta(minutes=minutes) if minutes else start,
        severity=severity,  # type: ignore[arg-type]
        details=details,
    )


def test_critical_event_comes_before_longer_info_event() -> None:
    ranked = rank_events([
        event("burner_cycle", minutes=120),
        event("boiler_connection_loss", severity="critical", minutes=1),
    ])

    assert [item.event.kind for item in ranked] == ["boiler_connection_loss", "burner_cycle"]


def test_zero_minute_setpoint_event_is_routine_but_preserved() -> None:
    item = rank_events([event("temperature_above_heating_setpoint")])[0]

    assert is_routine(item)
    assert item.event.kind == "temperature_above_heating_setpoint"


def test_long_period_repeats_are_grouped_with_count_and_total_duration() -> None:
    first = event("dhw_reheat_episode", severity="warning", minutes=10)
    second = first.model_copy(update={
        "id": "event:second",
        "started_at": first.started_at + timedelta(days=1),
        "ended_at": first.ended_at + timedelta(days=1),
    })

    ranked = rank_events([first, second], period_kind="weekly")

    assert len(ranked) == 1
    assert ranked[0].count == 2
    assert ranked[0].total_duration_minutes == 20


def test_group_representative_keeps_critical_severity() -> None:
    first = event("boiler_connection_loss", minutes=10)
    second = first.model_copy(update={
        "id": "event:critical",
        "started_at": first.started_at + timedelta(days=1),
        "ended_at": first.ended_at + timedelta(days=1),
        "severity": "critical",
    })

    item = rank_events([first, second], period_kind="monthly")[0]

    assert item.event.severity == "critical"
    assert len(item.events) == 2


def test_zero_duration_warning_does_not_pollute_meaningful_monthly_group() -> None:
    noise = event('temperature_above_heating_setpoint', severity='warning', peak_error_c=4)
    real = event('temperature_above_heating_setpoint', minutes=150, peak_error_c=.7)
    ranked = rank_events([noise, real], period_kind='monthly')
    significant = [item for item in ranked if not is_routine(item)]
    assert len(significant) == 1
    assert significant[0].event == real
    assert significant[0].count == 1
    assert sum(item.count for item in ranked) == 2


def test_routine_reheating_does_not_become_highlight_from_repetition() -> None:
    events = [event('dhw_reheat_episode', minutes=10).model_copy(update={'id': f'reheat:{i}'}) for i in range(50)]
    item, = rank_events(events, period_kind='weekly')
    assert is_routine(item)
    assert item.count == 50

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zont_analyzer.analytics.context import (
    build_heating_circuit_config,
    build_mode_catalog,
    detect_control_context,
    detect_heating_availability,
)
from zont_analyzer.analytics.events import detect_temperature_events
from zont_analyzer.analytics.metrics import temperature_metrics


def _devices() -> list[dict]:
    return [
        {
            "id": "1",
            "raw": {
                "z3k_config": {
                    "heating_modes": [
                        {
                            "id": 10,
                            "name": "Произвольный комфорт",
                            "heating_zones": [{"heating_circuit": 200, "temperature_setting": 2100}],
                        },
                        {
                            "id": 11,
                            "name": "Не греть",
                            "heating_zones": [{"heating_circuit": 200, "temperature_setting": 0}],
                        },
                        {
                            "id": 12,
                            "name": "По графику",
                            "heating_zones": [{"heating_circuit": 200, "temperature_setting": 500}],
                        },
                        {
                            "id": 13,
                            "name": "Лето",
                            "heating_zones": [{"heating_circuit": 200, "temperature_setting": 2100}],
                        },
                    ],
                    "heating_circuits": [
                        {
                            "id": 200,
                            "name": "Тёплый пол",
                            "winter_summer_switch": True,
                            "summer_threshold": 20.0,
                        }
                    ],
                    "interval_timetables": [
                        {"id": 500, "outside_default": 11, "time_intervals": [600]},
                    ],
                    "time_intervals": [
                        {
                            "id": 600,
                            "action_register": 1,
                            "sh": 7,
                            "sm": 0,
                            "eh": 22,
                            "em": 0,
                            "temperature": 10,
                        }
                    ],
                }
            },
        }
    ]


def test_mode_catalog_uses_parameters_before_name_hints() -> None:
    catalog = build_mode_catalog(_devices(), device_id="1", circuit_id="200")

    assert catalog[10]["target_policy"] == "fixed"
    assert catalog[10]["intent"] == "comfort"
    assert catalog[11]["heating_enabled"] is False
    assert catalog[11]["circuit_enabled"] is False
    assert catalog[11]["temperature_setting_id"] == 0
    assert catalog[11]["target_policy"] == "off"
    assert catalog[12]["target_policy"] == "scheduled"
    assert catalog[12]["schedule"][0]["start"] == "07:00"
    # A user-facing name is not the circuit's independent automatic summer state.
    assert catalog[13]["heating_enabled"] is True
    assert catalog[13]["target_policy"] == "fixed"


def test_context_distinguishes_mode_and_scheduled_target_changes() -> None:
    monday = datetime(2026, 8, 3, 0, tzinfo=UTC)
    catalog = build_mode_catalog(_devices(), device_id="1", circuit_id="200")
    mode_events, _context, windows = detect_control_context(
        mode_samples=[(monday, 10), (monday + timedelta(hours=1), 12)],
        target_samples=[(monday, 21), (monday + timedelta(hours=1), 18)],
        mode_catalog=catalog,
        period_id="daily",
        timezone="UTC",
    )

    mode_event = next(event for event in mode_events if event.kind == "heating_mode_change")
    assert mode_event.details["source"] == "likely_manual"
    assert next(event for event in mode_events if event.kind == "target_temperature_change").details["source"] == (
        "mode_change"
    )
    assert windows[0][1] - windows[0][0] == timedelta(hours=2)

    scheduled_events, _context, _windows = detect_control_context(
        mode_samples=[(monday, 12)],
        target_samples=[(monday, 18), (monday + timedelta(hours=7), 21)],
        mode_catalog=catalog,
        period_id="daily-scheduled",
        timezone="UTC",
    )
    target_event = next(event for event in scheduled_events if event.kind == "target_temperature_change")
    assert target_event.details["source"] == "scheduled"


def test_dynamic_target_metrics_and_transition_window() -> None:
    start = datetime(2026, 8, 3, tzinfo=UTC)
    samples = [
        (start, 20),
        (start + timedelta(hours=1), 22),
        (start + timedelta(hours=2), 22),
        (start + timedelta(hours=3), 23),
        (start + timedelta(hours=4), 22),
    ]
    targets = [(start, 20), (start + timedelta(hours=1), 22)]

    metrics = temperature_metrics(
        samples,
        period_id="dynamic",
        target_c=20,
        comfort_band_c=0.5,
        target_samples=targets,
    )
    in_band = next(item for item in metrics if item.name == "time_in_target_band_pct")
    assert in_band.value == 75

    events = detect_temperature_events(
        samples,
        period_id="dynamic",
        target_c=20,
        comfort_band_c=0.5,
        target_samples=targets,
        ignore_windows=[(start + timedelta(hours=1), start + timedelta(hours=3))],
    )
    assert len(events) == 1
    assert events[0].started_at == start + timedelta(hours=3)


def test_control_context_marks_explicitly_unknown_target() -> None:
    start = datetime(2026, 8, 3, tzinfo=UTC)
    events, context, _windows = detect_control_context(
        mode_samples=[(start, 10)],
        target_samples=[(start, 21.0), (start + timedelta(hours=1), None)],
        mode_catalog=build_mode_catalog(_devices(), device_id="1", circuit_id="200"),
        period_id="unknown-target",
        timezone="UTC",
    )

    assert not [event for event in events if event.kind == "target_temperature_change"]
    assert context["current_target_c"] is None
    assert _windows == []


def test_automatic_summer_state_is_independent_from_selected_mode() -> None:
    start = datetime(2026, 8, 3, tzinfo=UTC)
    catalog = build_mode_catalog(_devices(), device_id="1", circuit_id="200")
    circuit = build_heating_circuit_config(_devices(), device_id="1", circuit_id="200")
    events, context, inactive = detect_heating_availability(
        start=start,
        end=start + timedelta(hours=4),
        mode_samples=[(start - timedelta(minutes=1), 13)],
        status_samples=[
            (start - timedelta(minutes=1), 0),
            (start + timedelta(hours=1), 128),
            (start + timedelta(hours=3), 0),
        ],
        mode_catalog=catalog,
        circuit_config=circuit,
        period_id="summer",
    )

    assert [event.kind for event in events] == [
        "automatic_summer_mode_entered",
        "automatic_summer_mode_exited",
    ]
    assert inactive == [(start + timedelta(hours=1), start + timedelta(hours=3))]
    assert context["automatic_summer_mode_enabled"] is True
    assert context["automatic_summer_mode_active"] is False
    assert context["space_heating_available"] is True

    samples = [(start + timedelta(hours=index), 17.0) for index in range(5)]
    metrics = temperature_metrics(
        samples,
        period_id="summer",
        target_c=20,
        comfort_band_c=0.5,
        ignore_windows=inactive,
    )
    by_name = {metric.name: metric.value for metric in metrics}
    assert by_name["heating_target_evaluation_time_pct"] == 50
    assert by_name["degree_hours_below_target"] == 6

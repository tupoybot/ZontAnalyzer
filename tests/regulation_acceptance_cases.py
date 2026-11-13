"""Public synthetic acceptance inputs; expected values come from geometry.

No observed household data or model-generated answer is an oracle here.
The shifted 3-hour period puts midnight inside a pause, testing day stitching.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from zont_analyzer.analytics.evidence import NumericSample, SignalMetadata, SignalSeries
from zont_analyzer.analytics.regulation import RegulationInputs, build_regulation_facts

START = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = "synthetic-device/synthetic-room"
# Per period, the temperature rises linearly from 19.4 to 20.8 in 80 min,
# then returns in 100 min. Target=20; demand ends at minute 60.
# Signed integral/period=.1; absolute integral/period=(.6²+.8²)/(2*1.4).
TRIANGLE_ORACLE = {
    "room_signed_error_c": 0.1,
    "room_absolute_error_c": 5 / 14,
    "room_range_c": 1.4,
    "actual_range_c": 20.0,
    "actual_variation_c_per_hour": 40 / 3,
    "cycle_mean_overshoot_c": 0.8,
    "cycle_mean_post_heat_rise_c": 0.35,
    "cycle_mean_peak_delay_seconds": 1200.0,
    "cycle_mean_heating_seconds": 3600.0,
    "cycle_mean_pause_seconds": 7200.0,
}


def signal(key: str, points: list[tuple[int, float]], *, start: datetime = START,
           setpoint: bool = False) -> SignalSeries:
    return SignalSeries(key, SignalMetadata("synthetic-room-sensor", key, "°C",
                        value_kind="setpoint" if setpoint else "measurement"),
                        tuple(NumericSample(start + timedelta(minutes=minute), value) for minute, value in points))


def triangle_inputs(days: int = 1, *, start: datetime = START, room_scale: float = 1.0,
                    flow_scale: float = 1.0, version: str = "v1") -> RegulationInputs:
    minutes = days * 1440
    room, actual, command = [], [], []
    for minute in range(0, minutes + 1, 5):
        phase = (minute + 150) % 180
        temperature = 19.4 + 1.4 * phase / 80 if phase <= 80 else 20.8 - 1.4 * (phase - 80) / 100
        flow = 40 + 20 * phase / 60 if phase <= 60 else 60 - 20 * (phase - 60) / 120
        room.append((minute, 20 + room_scale * (temperature - 20)))
        actual.append((minute, 40 + flow_scale * (flow - 40)))
        command.append((minute, 60.0 if phase < 60 else 40.0))
    return RegulationInputs("synthetic-room", device_id="synthetic-device", settings_version=version,
                            settings_versions=((start, version),), mode_kind="maintenance", signals={
        "room": signal("room", room, start=start),
        "actual": signal("actual", actual, start=start),
        "command": signal("command", command, start=start, setpoint=True),
        "request": signal("request", [(minute, float((minute + 150) % 180 < 60))
                                      for minute in range(minutes + 1)], start=start),
        "target": signal("target", [(0, 20.0)], start=start, setpoint=True),
        "mode": signal("mode", [(minute, 1.0) for minute in range(0, minutes + 1, 5)], start=start),
        "outdoor": signal("outdoor", [(minute, -5.0) for minute in range(0, minutes + 1, 5)], start=start),
    })


def slice_inputs(data: RegulationInputs, start: datetime, end: datetime) -> RegulationInputs:
    signals = {}
    for key, series in data.signals.items():
        times = [sample.timestamp for sample in series.samples]
        left = max(0, bisect_right(times, start) - 1)
        right = bisect_right(times, end)
        signals[key] = replace(series, samples=series.samples[left:right],
                               unknown_at=tuple(time for time in series.unknown_at if start <= time <= end))
    return replace(data, signals=signals)


def daily_facts(data: RegulationInputs, days: int, *, start: datetime = START) -> list[dict[str, Any]]:
    facts = []
    for day in range(days):
        left, right = start + timedelta(days=day), start + timedelta(days=day + 1)
        facts.append(build_regulation_facts(start=left, end=right, inputs=slice_inputs(data, left, right),
                                           previous_boundary=facts[-1]["boundary"] if facts else None))
    return facts


def late_peak(data: RegulationInputs, *, day: int = 35) -> RegulationInputs:
    """One late peak, +.1°C at 01:50, with adjacent measured points unchanged."""
    timestamp = START + timedelta(days=day, minutes=110)
    room = data.signals["room"]
    times = [sample.timestamp for sample in room.samples]
    index = bisect_left(times, timestamp)
    assert times[index] == timestamp
    points = list(room.samples)
    points[index] = replace(points[index], value=points[index].value + 0.1)
    return replace(data, signals={**data.signals, "room": replace(room, samples=tuple(points))})


def ventilation_inputs(humidity: str = "same") -> RegulationInputs:
    # Stable 20°C until min30; fall to19 in10min; recover to20 at min60.
    # Three measured stable five-minute intervals close recovery at min75.
    room = [(minute, 20.0 if minute <= 30 or minute >= 60 else
             20 - (minute - 30) / 10 if minute <= 40 else 19 + (minute - 40) / 20)
            for minute in range(0, 121, 5)]
    base = triangle_inputs()
    signals = {"room": signal("room", room), "target": signal("target", [(0, 20)], setpoint=True),
               "request": signal("request", [(minute, 1.0) for minute in range(121)]),
               "actual": signal("actual", [(minute, 45.0) for minute in range(0, 121, 5)]),
               "outdoor": signal("outdoor", [(minute, -5.0) for minute in range(0, 121, 5)])}
    if humidity != "missing":
        signals["humidity"] = signal("humidity", [(minute, 50.0 if minute <= 30 else
                                                   50 - (minute - 30) * .6 if minute < 40 else 44.0)
                                                  for minute in range(0, 121, 5)])
    return replace(base, signals=signals,
                   humidity_room_identity="synthetic-room-sensor" if humidity == "same" else "another-room")


def change_timeline(*, second: bool = False) -> dict[str, Any]:
    def state(p: int, i: int) -> dict[str, Any]:
        return {"status": "observed", "fields": {
            "pid_prop_koef": {"status": "observed", "value": p},
            "pid_integral_koef": {"status": "observed", "value": i}}}

    versions = [
        {"event_id": "baseline", "version": "v1", "observed_at": START.isoformat(),
         "kind": "first_observation", "state": state(1, 2)},
        {"event_id": "joint-change", "version": "v2", "observed_at": (START + timedelta(hours=25)).isoformat(),
         "kind": "change", "state": state(3, 4), "changed_fields": ["pid_prop_koef", "pid_integral_koef"],
         "boundary": {"after": (START + timedelta(hours=24)).isoformat(),
                      "no_later_than": (START + timedelta(hours=25)).isoformat()}},
    ]
    if second:
        versions.append({"event_id": "rollback", "version": "v1",
                         "observed_at": (START + timedelta(hours=28)).isoformat(),
                         "kind": "change", "state": state(1, 2),
                         "changed_fields": ["pid_prop_koef", "pid_integral_koef"],
                         "boundary": {"after": (START + timedelta(hours=27)).isoformat(),
                                      "no_later_than": (START + timedelta(hours=28)).isoformat()}})
    return {"device_id": "synthetic-device", "circuit_id": "synthetic-room",
            "versions": versions, "incomplete": False}


def comparison_facts(*, room_scale: float = .5, flow_scale: float = 2.0,
                     confound: str | None = None) -> list[dict[str, Any]]:
    before = triangle_inputs(version="v1")
    after_start = START + timedelta(hours=25)
    after = triangle_inputs(start=after_start, room_scale=room_scale, flow_scale=flow_scale, version="v2")
    if confound in {"weather", "target"}:
        role, value = ("outdoor", 7.0) if confound == "weather" else ("target", 21.0)
        after = replace(after, signals={**after.signals, role: replace(after.signals[role], samples=tuple(
            replace(sample, value=value) for sample in after.signals[role].samples))})
    return [build_regulation_facts(start=START, end=START + timedelta(hours=24), inputs=before),
            build_regulation_facts(start=after_start, end=after_start + timedelta(hours=24), inputs=after)]


SCENARIOS = (
    {"id": "repeated-overshoot", "expected": {"mean_room_c": 20.1, **TRIANGLE_ORACLE},
     "must_explain": "Small signed mean does not cancel .8°C peaks, .35°C post-demand rise or 20°C flow range."},
    {"id": "ventilation-and-recovery", "expected": {"impact_start_minute": 30, "impact_end_minute": 40,
                                                    "recovery_end_minute": 75, "eligible_minutes": 75},
     "must_explain": ("Same-room humidity corroborates ventilation; "
                      "missing/other-room humidity leaves cause uncertain.")},
    {"id": "zont-only-joint-change", "expected": {"old_values": [1, 2], "new_values": [3, 4],
                                                 "boundary_hours": [24, 25], "manual_records": 0},
     "must_explain": "Both values changed together; individual parameter effects and author intent remain unknown."},
    {"id": "independent-experiment", "expected": {"recommendation_id": None,
                                                  "states": ["planned", "performed", "completed", "rolled_back"],
                                                  "clarification_preserves_original": True,
                                                  "original_forecast": "MAE decreases by at least 0.1°C within 7 days"},
     "must_explain": "One journal identity; edits and rollback preserve the original forecast and prior entries."},
    {"id": "mixed-and-confounded", "expected": {"room_mae_before": 5 / 14, "room_mae_after": 5 / 28,
                                               "flow_variation_before": 40 / 3, "flow_variation_after": 80 / 3,
                                               "outcome": "mixed"},
     "must_explain": ("Better room accuracy with rougher flow is mixed; "
                      "unmatched weather/target and new actions block success.")},
    {"id": "complete-season", "expected": {"days": 60, "visible_request_episodes": 480,
                                          "full_cycles": 479, "partial_cycles": 1,
                                          "eligible_seconds": 60 * 86400},
     "must_explain": ("All days/cycles contribute before example sampling; "
                      "late input replaces a day without duplicates.")},
)

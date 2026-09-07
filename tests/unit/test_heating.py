from datetime import UTC, datetime, timedelta

from zont_analyzer.analytics.heating import build_heating_evidence

START = datetime(2026, 1, 15, tzinfo=UTC)


def _stat(mean: float, coverage_pct: float = 100) -> dict[str, float]:
    return {"mean": mean, "minimum": mean, "maximum": mean, "coverage_pct": coverage_pct}


def _packet() -> dict:
    return {
        "period_start": START,
        "period_end": START + timedelta(hours=3),
        "windows": [
            {
                "started_at": START,
                "ended_at": START + timedelta(hours=1),
                "signals": {
                    "outdoor_temperature": _stat(-5),
                    "control_temperature": _stat(19),
                    "target_temperature": _stat(21),
                    "setting:mode_id": _stat(2),
                },
                "facts": {"heating_request_pct": _stat(100), "room_error_c": _stat(-2)},
            },
            {
                "started_at": START + timedelta(hours=1),
                "ended_at": START + timedelta(hours=2),
                "signals": {
                    "outdoor_temperature": _stat(10),
                    "control_temperature": _stat(22),
                    "target_temperature": _stat(21),
                },
                "facts": {"heating_request_pct": _stat(0)},
                "excluded_reasons": ["inactive"],
            },
        ],
    }


def test_windows_keep_weather_error_target_and_inactive_separately() -> None:
    result = build_heating_evidence(_packet(), coordinates={"latitude": 55.75, "longitude": 37.62})
    assert len(result.windows) == 1
    assert len(result.inactive_windows) == 1
    assert result.windows[0]["weather_class"] == "cold"
    assert result.windows[0]["room_error_c"] == -2
    assert result.windows[0]["target_matched"] is True
    assert result.quality["outdoor_temperature"]["range_c"] == 0


def test_prior_reports_have_stable_comparable_rows_and_cap() -> None:
    result = build_heating_evidence(
        _packet(), [{"id": "old", "context": {"temporal_evidence": _packet()}}], max_windows=1
    )
    assert len(result.windows) == 1
    assert result.windows[0]["period_id"] in {"current", "old"}


def test_missing_coordinates_are_explicit_and_no_sunrise_is_invented() -> None:
    result = build_heating_evidence(_packet())
    assert "location:latitude_longitude_unknown" in result.unknowns
    assert "sunrise:unavailable_coordinates_or_polar_day" in result.unknowns
    assert not result.morning_windows


def test_morning_window_is_linked_to_calculated_sunrise() -> None:
    packet = _packet()
    packet["windows"] = [
        packet["windows"][0]
        | {"started_at": datetime(2026, 1, 15, 6, 30, tzinfo=UTC), "ended_at": datetime(2026, 1, 15, 7, 30, tzinfo=UTC)}
    ]
    result = build_heating_evidence(packet, coordinates={"latitude": 55.75, "longitude": 37.62})
    assert result.morning_windows
    assert result.morning_windows[0]["kind"] == "morning"
    assert isinstance(result.morning_windows[0]["sunrise"], datetime)


def test_zero_mode_weather_and_optional_missing_signals_remain_eligible() -> None:
    packet = _packet()
    row = packet["windows"][0]
    row["signals"]["setting:mode_id"] = _stat(0)
    row["signals"]["outdoor_temperature"] = _stat(0)
    row["signals"]["humidity"] = {"mean": None, "coverage_pct": 0}
    result = build_heating_evidence(packet)
    assert result.windows[0]["mode"] == 0
    assert result.windows[0]["outdoor_mean_c"] == 0


def test_unknown_ch_exclusions_and_unstable_targets_cannot_support_pza() -> None:
    from copy import deepcopy

    base = _packet()["windows"][0]
    for change in ("missing_ch", "low_ch", "exclusion", "target", "mode", "nan"):
        row = deepcopy(base)
        if change == "missing_ch":
            row["facts"].pop("heating_request_pct")
        elif change == "low_ch":
            row["facts"]["heating_request_pct"]["coverage_pct"] = 10
        elif change == "exclusion":
            row["excluded_reasons"] = ["dhw"]
        elif change == "target":
            row["signals"]["target_temperature"]["maximum"] = 22
        elif change == "mode":
            row["signals"]["setting:mode_id"]["maximum"] = 3
        else:
            row["signals"]["outdoor_temperature"]["mean"] = float("nan")
        result = build_heating_evidence({"windows": [row]})
        assert not result.windows, change
        assert not result.comparisons
        assert result.unknown_windows


def test_weather_cap_keeps_extremes_and_different_targets_separate() -> None:
    from copy import deepcopy

    rows = []
    for index in range(20):
        row = deepcopy(_packet()["windows"][0])
        row.update(id=f"window:{index}", started_at=START + timedelta(hours=index),
                   ended_at=START + timedelta(hours=index + 1))
        row["signals"]["outdoor_temperature"] = _stat(index - 10)
        row["signals"]["target_temperature"] = _stat(21 if index % 2 else 22)
        rows.append(row)
    result = build_heating_evidence({"windows": rows}, max_windows=8)
    assert len(result.windows) == 8
    assert {row["outdoor_mean_c"] for row in result.windows} >= {-10, 9}
    for comparison in result.comparisons:
        selected = [row for row in result.windows if row["id"] in comparison["window_ids"]]
        assert len({row["target_c"] for row in selected}) == 1
    assert len(result.comparisons) <= 8


def test_duplicates_and_representatives_do_not_weight_comparisons_twice() -> None:
    packet = _packet()
    packet["windows"].append(packet["windows"][0] | {"kind": "representative"})
    result = build_heating_evidence(packet, [{"id": "old", "context": {"temporal_evidence": packet}}])
    assert len(result.windows) == 1
    assert not result.comparisons


def test_sunrise_reference_dateline_polar_and_json_serialization() -> None:
    import json
    from zoneinfo import ZoneInfo

    from zont_analyzer.analytics.heating import _sunrise

    # Equinox equatorial sunrise is close to 06:00 solar time, independent of implementation.
    equinox = datetime(2026, 3, 20, 12, tzinfo=UTC)
    sunrise = _sunrise(equinox, (0, 0), ZoneInfo("UTC"))
    assert sunrise is not None and sunrise.hour == 6 and sunrise.minute < 15
    timezone = ZoneInfo("Pacific/Auckland")
    morning = datetime(2026, 3, 20, 7, tzinfo=timezone)
    east = _sunrise(morning, (-36.85, 174.76), timezone)
    assert east is not None and east.date() == morning.date()
    assert 6 <= east.hour <= 8
    assert east.astimezone(UTC).date() < morning.date()
    assert _sunrise(datetime(2026, 6, 21, tzinfo=UTC), (90, 0), ZoneInfo("UTC")) is None
    assert _sunrise(datetime(2026, 12, 21, tzinfo=UTC), (80, 0), ZoneInfo("UTC")) is None
    json.dumps(build_heating_evidence(_packet()).as_dict(), allow_nan=False)


def test_malformed_window_does_not_create_fictitious_timestamp() -> None:
    assert not build_heating_evidence({"windows": [{"started_at": "bad"}]}).windows
    assert not build_heating_evidence({"windows": None}).windows

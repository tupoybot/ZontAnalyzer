import pytest

from zont_analyzer.analytics.settings import control_settings


def test_only_observed_bound_circuit_fields_are_exposed() -> None:
    device = {"password": "private", "z3k_config": {
        "heating_circuits": [{"id": 1, "pza": 10, "pid_prop_koef": 10, "summer_threshold": 20,
                              "winter_summer_switch": True, "guessed_slope": 7, "gas_valve": 8},
                             {"id": 2, "pid_prop_koef": 99}],
        "pzas": [{"id": 10, "x": [2930, 2830], "y": [3030, 3100]}],
    }}
    result = control_settings(device, "1", captured_at="2026-09-07T00:00:00+00:00")
    parameters = {item["field"]: item for item in result["parameters"]}
    assert parameters["pid_prop_koef"]["value"] == 10
    assert "guessed_slope" not in parameters and "gas_valve" not in parameters
    assert parameters["summer_threshold"]["source"].endswith("[id=1].summer_threshold")
    assert all(item["user_access"] == "needs_confirmation" for item in parameters.values())
    assert result["pza_curve"]["x_raw"] == [2930, 2830]
    assert result["pza_curve"]["points_c"] == [
        {"outdoor_c": 20.0, "flow_c": 30.0}, {"outdoor_c": 10.0, "flow_c": 37.0},
    ]
    assert "private" not in str(result)
    assert result["captured_at"] == "2026-09-07T00:00:00+00:00"


def test_missing_ambiguous_and_invalid_controls_stay_unknown() -> None:
    assert control_settings({}, "1")["status"] == "unknown"
    assert control_settings({"z3k_config": {"heating_circuits": [{"id": 1}, {"id": 1}]}}, "1")["status"] == "unknown"
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "pza": 10, "summer_threshold": float("nan")},
    ], "pzas": [{"id": 10, "x": [1, 2], "y": [3, float("inf")]}]}}, "1")
    assert result["pza_curve"] is None
    assert "summer_threshold:unavailable" in result["unknowns"]


def test_pilot_pid_and_pza_match_vendor_interface_and_all_seven_points() -> None:
    result = control_settings({"z3k_config": {
        "heating_circuits": [{"id": 20496, "type": 3, "setting_register": 2561, "pza": 8259,
                              "pid_prop_koef": 10.0, "pid_integral_koef": 1.0,
                              "water_min_temperature": 30, "water_max_temperature": 65}],
        "pzas": [{"id": 8259, "x": [2930, 2830, 2730, 2630, 2530, 2430, 2330],
                  "y": [3030, 3072, 3114, 3181, 3234, 3272, 3294]}],
    }}, "20496")
    regulation = result["regulation"]
    assert regulation["status"] == "decoded"
    assert regulation["mode"] == "air_pid"
    assert regulation["enabled"] is True
    assert regulation["pza_role"] == "upper_limit"
    assert [(p["outdoor_c"], p["flow_c"]) for p in result["pza_curve"]["points_c"]] == [
        (20, 30), (10, 34.2), (0, 38.4), (-10, 45.1), (-20, 50.4), (-30, 54.2), (-40, 56.4),
    ]
    parameters = {p["field"]: p for p in result["parameters"]}
    assert parameters["pid_prop_koef"]["value"] == 10
    assert parameters["pid_integral_koef"]["value"] == 1
    assert parameters["pid_prop_koef"]["unit"] == "zont_coefficient"
    assert "snapshot_only" in result["historical_applicability"]
    assert "active_control_algorithm_not_verified" not in result["unknowns"]


@pytest.mark.parametrize(("register", "mode", "enabled", "role"), [
    (0, "air", True, "target"), (1, "air_pid", True, "upper_limit"),
    (2, "water", True, "target"), (130, "water", True, "heat_request_only"),
    (65, "air_pid", False, "upper_limit"), (3, None, True, "unknown"),
    (True, None, None, "unknown"), (-1, None, None, "unknown"), (None, None, None, "unknown"),
])
def test_mode_bits_disable_and_invalid_registers(register, mode, enabled, role) -> None:
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "type": 3, "setting_register": register, "pza": 10},
    ]}}, "1")["regulation"]
    assert (result["mode"], result["enabled"], result["pza_role"]) == (mode, enabled, role)


@pytest.mark.parametrize("pza", [0, 255, None])
def test_unassigned_pza_is_not_an_unknown_algorithm(pza) -> None:
    result = control_settings({"z3k_config": {"heating_circuits": [
        {"id": 1, "type": 3, "setting_register": 1, "pza": pza},
    ]}}, "1")
    assert result["regulation"]["mode"] == "air_pid"
    assert result["regulation"]["pza_role"] == "off"
    assert "pza_curve_unavailable_or_ambiguous" not in result["unknowns"]


@pytest.mark.parametrize(("x", "y"), [
    ([20, 10], [30, 40]), ([2930, 2930], [3030, 3130]),
    ([2930, 2830, 2880], [3030, 3130, 3230]), ([2930, 2830], [3030, 65535]),
    ([True, 2830], [3030, 3130]), ([2930, 2830], [3030, float("nan")]),
])
def test_invalid_curve_cannot_be_presented_as_decoded_celsius(x, y) -> None:
    result = control_settings({"z3k_config": {
        "heating_circuits": [{"id": 1, "type": 3, "setting_register": 1, "pza": 10}],
        "pzas": [{"id": 10, "x": x, "y": y}],
    }}, "1")
    assert not result["pza_curve"] or result["pza_curve"]["points_c"] is None


def test_boiler_type_and_duplicate_curve_are_not_mistaken_for_heating_pid() -> None:
    device = {"z3k_config": {
        "heating_circuits": [{"id": 1, "type": 0, "setting_register": 1, "pza": 10}],
        "pzas": [{"id": 10, "x": [2930, 2830], "y": [3030, 3130]}] * 2,
    }}
    result = control_settings(device, "1")
    assert result["regulation"]["status"] == "not_applicable"
    assert result["regulation"]["mode"] is None
    assert result["pza_curve"] is None


def test_control_versions_ignore_object_and_circuit_order_but_preserve_curve_order() -> None:
    from zont_analyzer.analytics.settings_history import fingerprint, observed_controls

    one = {"id": 1, "type": 3, "pza": 10, "pid_prop_koef": 2, "setting_register": 1}
    two = {"id": 2, "type": 3, "pid_prop_koef": 9}
    curve = {"id": 10, "x": [2930, 2830], "y": [3030, 3130]}
    config = {"heating_circuits": [one, two], "pzas": [curve]}
    original = observed_controls({"z3k_config": config})
    reordered = observed_controls({"z3k_config": {
        "pzas": [curve], "heating_circuits": [two, dict(reversed(list(one.items())))],
    }})
    assert fingerprint(original) == fingerprint(reordered)
    config["pzas"][0] = {**curve, "x": list(reversed(curve["x"]))}
    assert fingerprint(original) != fingerprint(observed_controls({"z3k_config": config}))


def test_control_versions_distinguish_missing_invalid_and_zero_and_circuit_loss() -> None:
    from zont_analyzer.analytics.settings_history import observed_controls

    def fields(circuit):
        return observed_controls({"z3k_config": {"heating_circuits": [{"id": 1, **circuit}]}})["1"]["fields"]

    assert fields({})["pid_prop_koef"] == {"status": "missing"}
    assert fields({"pid_prop_koef": None})["pid_prop_koef"] == {"status": "invalid"}
    assert fields({"pid_prop_koef": 0})["pid_prop_koef"] == {"status": "observed", "value": 0}
    assert observed_controls({}, known={"1"})["1"]["status"] == "invalid_or_missing_configuration"
    assert observed_controls({"z3k_config": {"heating_circuits": []}}, known={"1"})["1"]["status"] == "missing"


def test_history_boundary_is_observed_not_effective_and_never_backprojects_current_state() -> None:
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.analytics.settings_history import settings_timeline

    start = datetime(2026, 1, 1, tzinfo=UTC)
    at = int(start.timestamp() * 1_000_000)
    hour = 3_600_000_000
    a, b = {"fields": {"p": 1, "i": 2}}, {"fields": {"p": 3, "i": 4}}
    events = [{"event_id": 1, "observed_at": at, "state": a, "last_confirmed_at": at + hour},
              {"event_id": 2, "observed_at": at + 3 * hour, "state": b},
              {"event_id": 3, "observed_at": at + 4 * hour, "state": a}]
    result = settings_timeline(events, device_id="fixture", circuit_id="1", start=start,
                               end=start + timedelta(hours=5))
    assert [row["kind"] for row in result["versions"]] == ["first_observation", "change", "change"]
    assert result["versions"][1]["changed_fields"] == ["i", "p"]
    assert result["versions"][1]["boundary"] == {
        "after": at + hour, "no_later_than": at + 3 * hour, "exact_effective_at": None,
    }
    assert result["versions"][2]["boundary"]["after"] == at + 3 * hour
    assert result["versions"][1]["last_confirmed_at"] is None
    before = settings_timeline(events[:1], device_id="fixture", circuit_id="1",
                               start=start - timedelta(days=2), end=start - timedelta(days=1))
    assert before["versions"] == [] and before["unknown_before_first_observation"]
    # A change first observed after the report is relevant when its uncertainty
    # interval overlaps that report; neither candidate is presented as exact.
    during = settings_timeline(events, device_id="fixture", circuit_id="1",
                               start=start + timedelta(hours=1), end=start + timedelta(hours=2))
    assert [row["state"] for row in during["versions"]] == [a, b]


def test_history_refresh_preserves_paid_interpretation_and_marks_changed_facts_stale() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from zont_analyzer.application.reasoning_context import report_facts_fingerprint
    from zont_analyzer.application.settings_history import refresh_settings_history
    from zont_analyzer.domain import QualityResult, Report

    start = datetime(2026, 1, 1, tzinfo=UTC)
    old = {"device_id": "fixture", "circuit_id": "1", "versions": []}
    report = Report(id="settings-test", kind="daily", period_start=start, period_end=start + timedelta(days=1),
                    generated_at=start, timezone="UTC", summary="Original paid interpretation", ai_used=True,
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0,
                                          stuck_pct=0, implausible_jumps=0, sample_count=0),
                    context={"control_settings_history": old})
    baseline = report_facts_fingerprint(report)
    report.context["ai_facts_fingerprint"] = baseline
    changed = {**old, "versions": [{"state": {"p": 2}}]}
    db = SimpleNamespace(control_settings_history=lambda *args: changed)
    refreshed = refresh_settings_history(db, report)
    assert refreshed.summary == report.summary and refreshed.ai_used
    assert refreshed.context["ai_facts_fingerprint"] == baseline
    assert refreshed.context["control_settings_interpretation_stale"] is True
    assert refresh_settings_history(db, refreshed) == refreshed
    same = SimpleNamespace(control_settings_history=lambda *args: old)
    assert refresh_settings_history(same, report) == report


def test_late_legacy_change_between_compact_confirmations_keeps_both_observed_states() -> None:
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.analytics.settings_history import settings_timeline

    start = datetime(2026, 1, 1, tzinfo=UTC)
    at = int(start.timestamp() * 1_000_000)
    hour = 3_600_000_000
    a, b = {"fields": {"p": 1}}, {"fields": {"p": 2}}
    events = [{"event_id": 1, "observed_at": at, "last_confirmed_at": at + 3 * hour, "state": a},
              {"event_id": 2, "observed_at": at + hour, "last_confirmed_at": None, "state": b}]
    result = settings_timeline(events, device_id="fixture", circuit_id="1", start=start,
                               end=start + timedelta(hours=4))
    assert [v["state"] for v in result["versions"]] == [a, b, a]
    assert result["versions"][1]["last_confirmed_at"] is None
    assert result["versions"][1]["boundary"]["after"] == at
    assert result["versions"][2]["boundary"]["after"] == at + hour


def test_settings_refresh_never_invents_missing_original_ai_baseline() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from zont_analyzer.application.settings_history import refresh_settings_history, report_depends_on_device
    from zont_analyzer.domain import QualityResult, Report

    start = datetime(2026, 1, 1, tzinfo=UTC)
    history = {"device_id": "fixture", "circuit_id": "1", "versions": []}
    report = Report(id="settings-unknown-ai", kind="daily", period_start=start,
                    period_end=start + timedelta(days=1), generated_at=start, summary="Earlier interpretation",
                    ai_used=True, context={"control_settings_history": history,
                                          "pilot_ai_reuse": {"facts_changed": True}},
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=0))
    current = [history]
    db = SimpleNamespace(control_settings_history=lambda *args: current[0])
    for index in [1, 2]:
        current[0] = {**history, "versions": [{"version": index}]}
        report = refresh_settings_history(db, report)
        assert "ai_facts_fingerprint" not in report.context
        assert report.context["control_settings_interpretation_stale"]
    assert report_depends_on_device(report, "fixture", ["1"])
    assert not report_depends_on_device(report, "other", ["1"])
    assert not report_depends_on_device(report, "fixture", ["2"])


def test_later_unchanged_confirmation_preserves_historical_semantic_fingerprint_and_ai() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from zont_analyzer.analytics.settings_history import settings_timeline
    from zont_analyzer.application.reasoning_context import report_facts_fingerprint
    from zont_analyzer.application.settings_history import ensure_settings_history, refresh_settings_history
    from zont_analyzer.domain import QualityResult, Report

    start = datetime(2026, 1, 1, tzinfo=UTC)
    at = int(start.timestamp() * 1_000_000)
    day = 86_400_000_000
    observation = {"event_id": 1, "observed_at": at, "last_confirmed_at": at, "state": {"fields": {"p": 1}}}
    old = settings_timeline([observation], device_id="fixture", circuit_id="1", start=start,
                            end=start + timedelta(days=1))
    later = settings_timeline([{**observation, "last_confirmed_at": at + 10 * day}],
                              device_id="fixture", circuit_id="1", start=start, end=start + timedelta(days=1))
    assert old != later
    assert old["fingerprint"] == later["fingerprint"]
    report = Report(id="historical-settings", kind="daily", period_start=start, period_end=start + timedelta(days=1),
                    generated_at=start, summary="Original interpretation", ai_used=True,
                    context={"control_settings_history": old},
                    quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                                          implausible_jumps=0, sample_count=0))
    changed_metadata = report.model_copy(deep=True)
    changed_metadata.context["control_settings_history"] = later
    assert report_facts_fingerprint(changed_metadata) == report_facts_fingerprint(report)
    db = SimpleNamespace(control_settings_history=lambda *args: later)
    assert refresh_settings_history(db, report) == report
    # The ordinary path already attached history and must perform no second read
    # or binding lookup when finalization also serves long-period aggregation.
    assert ensure_settings_history(SimpleNamespace(), report.context, report.period_start, report.period_end) == {}


def test_preperiod_state_fingerprint_is_stable_across_history_query_sizes_and_reprojection() -> None:
    from copy import deepcopy
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.analytics.settings_history import semantic_history, settings_timeline
    from zont_analyzer.application.regulation_reports import day_history

    start = datetime(2025, 1, 1, tzinfo=UTC)
    at, day = int(start.timestamp() * 1_000_000), 86_400_000_000
    observations = [{"event_id": index, "observed_at": at + offset * day, "state": {"fields": {"p": 1}}}
                    for index, offset in enumerate((0, 4, 8), 1)]
    broad = settings_timeline(observations, device_id="fixture", circuit_id="1", start=start,
                              end=start + timedelta(days=14))
    narrow = settings_timeline(observations[1:], device_id="fixture", circuit_id="1",
                               start=start + timedelta(days=5), end=start + timedelta(days=10))
    left, right = start + timedelta(days=6), start + timedelta(days=7)
    first, second = day_history(narrow, left, right), day_history(broad, left, right)
    before = deepcopy(first)
    assert first["versions"][0]["observed_at"] != second["versions"][0]["observed_at"]
    assert first["versions"][0]["event_id"] != second["versions"][0]["event_id"]
    assert first["fingerprint"] == second["fingerprint"]
    assert semantic_history(first) == semantic_history(second)
    assert first == before  # Canonical hashing must preserve the actual provenance.
    assert day_history(first, left, right)["fingerprint"] == first["fingerprint"]


def test_preperiod_anchor_normalization_preserves_overlapping_change_uncertainty() -> None:
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.analytics.settings_history import semantic_history, settings_timeline

    start = datetime(2025, 1, 1, tzinfo=UTC)
    at, day = int(start.timestamp() * 1_000_000), 86_400_000_000
    old, new = {"fields": {"p": 1}}, {"fields": {"p": 2}}
    before = {"event_id": 1, "observed_at": at, "state": old, "last_confirmed_at": at + 4 * day}
    change = {"event_id": 2, "observed_at": at + 8 * day, "state": new}
    history = settings_timeline([before, change], device_id="fixture", circuit_id="1",
                                start=start + timedelta(days=6), end=start + timedelta(days=7))
    projected = semantic_history(history)
    assert projected["versions"][1]["boundary"] == {
        "after": at + 4 * day, "no_later_than": at + 8 * day, "exact_effective_at": None}
    assert projected["versions"][1]["observed_at"] == at + 8 * day

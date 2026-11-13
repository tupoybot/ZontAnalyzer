"""Automatic change assessment uses every complete factual run, without an AI."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from zont_analyzer.analytics.regulation_comparison import compare_change, observed_changes
from zont_analyzer.application.regulation_comparison import build_regulation_comparison_context

START = datetime(2026, 1, 1, tzinfo=UTC)
CHANGE = START + timedelta(hours=12)
END = START + timedelta(hours=25)


def state(p: float, i: float = 1) -> dict[str, Any]:
    return {"status": "observed", "fields": {
        "pid_prop_koef": {"status": "observed", "value": p},
        "pid_integral_koef": {"status": "observed", "value": i},
    }}


def history(*, wide: bool = False, second: bool = False) -> dict[str, Any]:
    lower = START if wide else CHANGE
    versions: list[dict[str, Any]] = [
        {"event_id": "base", "version": "old", "observed_at": START.isoformat(),
         "kind": "first_observation", "state": state(1)},
        {"event_id": "change", "version": "new", "observed_at": (CHANGE + timedelta(hours=1)).isoformat(),
         "kind": "change", "state": state(2, 3),
         "changed_fields": ["pid_prop_koef", "pid_integral_koef"],
         "boundary": {"after": lower.isoformat(), "no_later_than": (CHANGE + timedelta(hours=1)).isoformat()}},
    ]
    if second:
        versions.append({"event_id": "rollback", "version": "old", "observed_at": END.isoformat(),
                         "kind": "change", "state": state(1), "changed_fields": ["pid_prop_koef"],
                         "boundary": {"after": (CHANGE + timedelta(hours=3)).isoformat(),
                                      "no_later_than": END.isoformat()}})
    return {"device_id": "device", "circuit_id": "room", "versions": versions, "incomplete": False}


def owner(*, time: datetime | None = None) -> dict[str, Any]:
    return {"intervention_id": "manual", "experiment": {
        "category": "settings", "performed_at": (time or CHANGE + timedelta(minutes=30)).isoformat(),
        "changes": [{"device_id": "device", "circuit_id": "room", "parameter": key,
                     "before": str(old), "after": str(new)} for key, old, new in
                    [("pid_prop_koef", 1, 2), ("pid_integral_koef", 1, 3)]],
        "original": {"expected_effect": "A frozen forecast", "predictions": []},
    }}


def stat(value: float, *, variation: float = 0) -> dict[str, float]:
    return {"seconds": 3600, "integral": value * 3600,
            "positive_integral": max(value, 0) * 3600, "negative_integral": max(-value, 0) * 3600,
            "maximum": value, "minimum": value, "first": value, "last": value,
            "variation": variation, "max_rate_per_hour": variation}


def facts(*, after_error: float = 0.1, after_flow: float = 1) -> list[dict[str, Any]]:
    runs = []
    for index in range(25):
        after = index >= 13
        error = after_error if after else 0.4
        flow = after_flow if after else 2
        request = index % 2 == (1 if not after else 0)
        runs.append({"start": (START + timedelta(hours=index)).isoformat(),
                     "end": (START + timedelta(hours=index + 1)).isoformat(),
                     "mode": "maintenance", "control_mode": 1, "target": 21,
                     "weather": "0_to_10", "settings_version": "new" if after else "old",
                     "request": request, "request_active_seconds": 3600 if request else 0,
                     "request_observed_seconds": 3600, "eligible_seconds": 3600,
                     "seconds": 3600, "reasons": [],
                     "stats": {"error": stat(error), "actual": stat(40, variation=flow),
                               "command": stat(42, variation=flow), "outdoor": stat(5)}})
    return [{"scope": "device/room", "period_start": START.isoformat(), "period_end": END.isoformat(),
             "algorithm_version": "regulation-v1", "source_fingerprint": "revision1", "settings_version": "history1",
             "request_semantics": "state", "runs": runs,
             # Deliberately misleading presentation samples must never be consumed.
             "cycles": [], "metrics": {"room_absolute_error_c": {"value": 999}}}]


def compare(data: list[dict[str, Any]] | None = None, *, timeline: dict[str, Any] | None = None,
            manual: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    joined = observed_changes(timeline or history(), manual or [])
    return compare_change(joined["events"][0], events=joined["events"], facts=data or facts(), start=START,
                          end=END, unmatched_manual=joined["unmatched_manual_records"],
                          incomplete_history=joined["incomplete"])


@pytest.mark.parametrize(("error", "flow", "outcome"), [
    (0.1, 1, "improved"), (0.8, 4, "worsened"), (0.1, 4, "mixed"), (0.4, 2, "indistinguishable"),
])
def test_all_outcomes_without_manual_or_ai(error: float, flow: float, outcome: str) -> None:
    result = compare(facts(after_error=error, after_flow=flow))
    assert result["stage"] == "available", result["next_observation"]
    assert result["outcome"] == outcome
    assert result["before"]["complete_cycles"] >= 3
    assert result["after"]["complete_cycles"] >= 3
    assert result["metrics"]["room_absolute_error_c"]["after"] == pytest.approx(error)
    assert result["component_effects"] is None
    assert result["gas_savings"] is None
    assert "joint_parameter_change; individual_parameter_effects_not_identifiable" in result["limitations"]


def test_manual_and_observed_proven_change_are_one_event_and_freeze_forecast() -> None:
    manual = owner()
    original = deepcopy(manual)
    joined = observed_changes(history(), [manual])
    assert len(joined["events"]) == 1
    assert joined["unmatched_manual_records"] == []
    event = joined["events"][0]
    assert event["boundary"]["exact_effective_at"] == manual["experiment"]["performed_at"]
    result = compare(manual=[manual])
    assert result["forecast_assessments"][0]["original"] == original["experiment"]["original"]
    assert manual == original
    assert event["author"] == event["intent"] == "unknown"


@pytest.mark.parametrize("mutation", ["wrong_scope", "partial_fields", "different_values", "outside_boundary"])
def test_notes_are_not_deduplication_proof(mutation: str) -> None:
    manual = owner()
    if mutation == "wrong_scope":
        manual["experiment"]["changes"][0]["circuit_id"] = "other"
    elif mutation == "partial_fields":
        manual["experiment"]["changes"].pop()
    elif mutation == "different_values":
        manual["experiment"]["changes"][0]["after"] = "99"
    else:
        manual["experiment"]["performed_at"] = END.isoformat()
    joined = observed_changes(history(), [manual])
    assert joined["unmatched_manual_records"] == [manual]
    assert joined["events"][0]["boundary"]["exact_effective_at"] is None


def test_conflicting_owner_times_do_not_replace_observed_boundary() -> None:
    result = observed_changes(history(), [owner(), owner(time=CHANGE + timedelta(minutes=40))])
    assert result["events"][0]["owner_time_conflict"]
    assert result["events"][0]["boundary"]["exact_effective_at"] is None


@pytest.mark.parametrize(("field", "value"), [("target", 22), ("control_mode", 2), ("mode", "warmup")])
def test_different_target_mode_and_warmup_are_not_mixed(field: str, value: Any) -> None:
    data = facts()
    for run in data[0]["runs"][13:]:
        run[field] = value
    result = compare(data)
    assert result["stage"] == "insufficient"
    assert result["shared_conditions"] == []
    assert result["outcome"] is None


def test_weather_must_be_close_and_measured_not_only_same_coarse_bin() -> None:
    data = facts()
    for run in data[0]["runs"][13:]:
        run["stats"]["outdoor"] = stat(9)
    result = compare(data)
    assert result["stage"] == "insufficient"
    assert result["exclusion_seconds"]["after:unmatched_weather_target_or_mode"] > 0


@pytest.mark.parametrize("reason", ["dhw", "room_missing_or_gap", "recovery:ventilation_corroborated",
                                    "recovery:abrupt_room_cooling", "reliability"])
def test_dhw_disturbance_and_entire_recovery_are_excluded(reason: str) -> None:
    data = facts()
    for run in data[0]["runs"][13:18]:
        run.update(reasons=[reason], eligible_seconds=0)
    result = compare(data)
    assert result["exclusion_seconds"][reason] == 5 * 3600
    assert all(interval["start"] >= (START + timedelta(hours=18)).isoformat()
               for interval in result["after"]["intervals"])
    assert result["stage"] != "available"


def test_sparse_boundary_and_exact_owner_time_need_compatible_values() -> None:
    timeline = history(wide=True)
    timeline["versions"][1]["boundary"]["after"] = (START - timedelta(days=2)).isoformat()
    result = compare(timeline=timeline)
    assert result["stage"] == "insufficient"
    assert any("change_boundary_too_wide" in reason for reason in result["next_observation"])
    refined = compare(timeline=timeline, manual=[owner()])
    assert refined["boundary_excluded"]["refined_by_owner"]
    assert not any("change_boundary_too_wide" in reason for reason in refined["next_observation"])


def test_later_change_supersedes_short_after_window_and_rollback_has_no_individual_effect() -> None:
    timeline = history(second=True)
    joined = observed_changes(timeline, [])
    assert joined["events"][1]["rollback"]
    result = compare(timeline=timeline)
    assert result["stage"] == "superseded"
    assert result["observation_end"] == (CHANGE + timedelta(hours=3)).isoformat()


def test_census_ignores_duplicate_identical_days_and_rejects_conflicts() -> None:
    data = facts()
    assert compare(data + deepcopy(data))["before"]["runs"] == 12
    bad = deepcopy(data[0])
    bad["runs"][0]["stats"]["error"]["integral"] = 999
    with pytest.raises(ValueError, match="conflicting"):
        compare(data + [bad])


def test_unstable_effect_does_not_become_available_from_global_mean() -> None:
    data = facts()
    for run in data[0]["runs"][19:]:
        run["stats"]["error"] = stat(0.8)
    result = compare(data)
    assert result["stage"] == "insufficient"
    assert result["stability"]["room_absolute_error_c"] == "not_established"


def test_missing_days_and_calendar_alone_do_not_prove_sufficiency() -> None:
    data = facts()
    data[0]["runs"] = data[0]["runs"][:2] + data[0]["runs"][-2:]
    result = compare(data)
    assert result["stage"] == "insufficient"
    assert any("missing_regulation_facts_or_gaps" in value for value in result["next_observation"])


class FakeDatabase:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes = 0
        self.history = history()

    def get_app_meta(self, key: str) -> str | None:
        return self.values.get(key)

    def set_app_meta(self, key: str, value: str) -> None:
        self.writes += 1
        self.values[key] = value

    def control_settings_history(self, device_id: str, circuit_id: str,
                                 start: datetime, end: datetime) -> dict[str, Any]:
        return self.history

    def intervention_history(self, limit: int = 10, *, before: datetime | None = None) -> list[dict[str, Any]]:
        return []


def test_bounded_one_range_read_persistent_cache_and_late_data() -> None:
    db = FakeDatabase()
    data = facts()
    reads: list[tuple[str, datetime, datetime]] = []
    diagnostics: dict[str, int] = {}

    def read(scope: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        reads.append((scope, start, end))
        return data

    def build() -> dict[str, Any]:
        context = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                       end=END, read_facts=read, diagnostics=diagnostics)
        return context["regulation_comparisons"]

    first = build()
    second = build()
    assert first == second
    assert diagnostics["cache_hits"] == 1
    assert db.writes == 1
    assert len(reads) == 2
    assert (reads[0][2] - reads[0][1]).days <= 29
    assert first["ai_requests"] == 0
    data[0]["source_fingerprint"] = "late_revision2"
    third = build()
    assert diagnostics["cache_hits"] == 0
    assert db.writes == 2
    assert third["results"][0]["dependency_fingerprint"] != first["results"][0]["dependency_fingerprint"]


def test_context_validates_scope_and_bounds() -> None:
    db = FakeDatabase()
    with pytest.raises(ValueError, match="limited"):
        build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                           end=END, read_facts=lambda *_: [], maximum_events=257)
    data = facts()
    data[0]["scope"] = "other/room"
    with pytest.raises(ValueError, match="another circuit"):
        build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                           end=END, read_facts=lambda *_: data)


def test_preceding_minute_activity_does_not_invent_exact_cycles() -> None:
    data = facts()
    data[0]["request_semantics"] = "preceding_minute"
    for run in data[0]["runs"]:
        if run["request"]:
            run["request_active_seconds"] = 900
    result = compare(data)
    assert result["stage"] == "available"
    assert result["before"]["complete_request_episodes"] == 5
    assert result["after"]["minute_bin_request_episodes"] == 5
    assert result["before"]["exact_state_cycles"] == result["after"]["exact_state_cycles"] == 0
    assert result["before"]["request_edge_uncertainty_seconds"] == 60
    episode = result["before"]["request_episode_examples"][0]
    assert episode["edge_precision"] == "preceding_minute"
    assert episode["observed_active_seconds"] == 900
    assert episode["activity_observed_seconds"] == 3600
    assert episode["heating_duration_bounds_seconds"] == {"minimum": 3480, "maximum": 3600}
    assert episode["pause_duration_bounds_seconds"] == {"minimum": 3600, "maximum": 3720}
    assert episode["start_boundary_interval"] == {
        "earliest": (START + timedelta(hours=1)).isoformat(),
        "latest": (START + timedelta(hours=1, minutes=1)).isoformat(),
    }
    assert result["metrics"]["cycle_overshoot_c"]["after"] == pytest.approx(0.1)
    assert "not_exact_physical_cycle_overshoot" in result["metrics"]["cycle_overshoot_c"]["measurement_semantics"]
    assert not result["before"]["continuous_heat"]


@pytest.mark.parametrize("defect", ["coarser_bins", "missing_support", "missing_room", "missing_zero_bins"])
def test_minute_bin_episodes_require_bounded_edges_complete_coverage_and_pauses(defect: str) -> None:
    data = facts()
    data[0]["request_semantics"] = "preceding_minute"
    for run in data[0]["runs"]:
        if defect == "coarser_bins":
            run["request_bin_seconds"] = 120
        elif defect == "missing_support":
            run["request_observed_seconds"] = 3000
        elif defect == "missing_room":
            run["stats"]["error"]["seconds"] = 3000
        elif not run["request"]:
            run["request_observed_seconds"] = 0
    result = compare(data)
    assert result["before"]["complete_request_episodes"] == 0
    assert result["after"]["complete_request_episodes"] == 0
    assert result["metrics"]["cycle_overshoot_c"]["direction"] == "unavailable"
    assert result["stage"] == "insufficient"


def test_cropped_positive_minute_bins_do_not_become_a_complete_episode() -> None:
    data = facts()
    data[0]["request_semantics"] = "preceding_minute"
    data[0]["runs"] = [run for run in data[0]["runs"] if run["request"]]
    result = compare(data)
    assert result["before"]["complete_request_episodes"] == 0
    assert result["after"]["complete_request_episodes"] == 0
    assert result["stage"] == "insufficient"


def test_continuous_heat_is_supported_without_inventing_cycles() -> None:
    data = facts()
    for run in data[0]["runs"]:
        run.update(request=True, request_active_seconds=3600)
    result = compare(data)
    assert result["stage"] == "available"
    assert result["before"]["complete_cycles"] == 0
    assert result["metrics"]["cycle_overshoot_c"]["direction"] == "unavailable"
    assert "continuous_heat; cycle_overshoot_unavailable" in result["limitations"]


def test_unchanged_poll_confirmation_is_not_a_new_event_or_cache_miss() -> None:
    db = FakeDatabase()
    data = facts()
    first = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                               end=END, read_facts=lambda *_: data)
    db.history["versions"][1]["last_confirmed_at"] = END.isoformat()
    db.history["versions"][1]["last_observed_at"] = END.isoformat()
    second = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                end=END, read_facts=lambda *_: data)
    assert db.writes == 1
    assert first["regulation_comparisons"]["results"] == second["regulation_comparisons"]["results"]


def test_bounded_display_declares_omitted_changes_and_fact_intervals() -> None:
    db = FakeDatabase()
    for index in range(2, 7):
        previous = deepcopy(db.history["versions"][-1])
        previous.update(event_id=f"change{index}", version=f"v{index}",
                        observed_at=(START + timedelta(hours=13 + index)).isoformat())
        previous["boundary"] = {"after": (START + timedelta(hours=12 + index)).isoformat(),
                                "no_later_than": previous["observed_at"]}
        db.history["versions"].append(previous)
    result = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                end=END, read_facts=lambda *_: facts(),
                                                maximum_events=4)["regulation_comparisons"]
    assert len(result["results"]) == 4
    assert result["events_truncated"]
    assert result["total_changes_in_history"] == 6
    assert len(db.values) <= 4


def test_old_superseded_event_cache_ignores_unrelated_late_day() -> None:
    db = FakeDatabase()
    db.history = history(second=True)
    db.history["versions"][2]["observed_at"] = (END - timedelta(hours=1)).isoformat()
    db.history["versions"][2]["boundary"]["no_later_than"] = (END - timedelta(hours=1)).isoformat()
    data = facts()
    later_start = END - timedelta(hours=3)
    first_day = {**deepcopy(data[0]), "runs": [r for r in deepcopy(data[0]["runs"])
                                              if datetime.fromisoformat(r["end"]) <= later_start],
                 "period_end": later_start.isoformat()}
    last_day = {**deepcopy(data[0]), "runs": [r for r in deepcopy(data[0]["runs"])
                                             if datetime.fromisoformat(r["start"]) >= later_start],
                "period_start": later_start.isoformat()}
    values = [first_day, last_day]
    def build() -> dict[str, Any]:
        return build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                   end=END, read_facts=lambda *_: values)["regulation_comparisons"]
    first = build()
    last_day["source_fingerprint"] = "late_other_day"
    second = build()
    original_first = next(r for r in first["results"] if r["event_id"] == "change")
    original_second = next(r for r in second["results"] if r["event_id"] == "change")
    assert original_first["dependency_fingerprint"] == original_second["dependency_fingerprint"]
    assert db.writes == 3


def test_all_stages_include_provenance_and_immutable_inputs() -> None:
    data = facts()
    before = deepcopy(data)
    joined = observed_changes(history(), [])
    event = joined["events"][0]
    detected = compare_change(event, events=[event], facts=[], start=START,
                              end=CHANGE + timedelta(hours=1))
    assert detected["stage"] == "detected"
    collecting_data = deepcopy(data)
    collecting_data[0]["runs"] = collecting_data[0]["runs"][:15]
    collecting = compare_change(event, events=[event], facts=collecting_data, start=START,
                                end=CHANGE + timedelta(hours=3))
    assert collecting["stage"] == "transition_or_collecting"
    for result in (detected, collecting, compare(data)):
        assert result["event"]["before_version"] == "old"
        assert result["event"]["after_version"] == "new"
        assert result["boundary_excluded"]["start"] == CHANGE.isoformat()
        assert "complete_cycles" in result["before"]
        assert "exclusion_seconds" in result
    assert data == before


def test_disconnected_hours_cannot_satisfy_sustained_continuous_heat() -> None:
    data = facts()
    runs = []
    for side, originals in (("before", data[0]["runs"][:12]), ("after", data[0]["runs"][13:])):
        for index, original in enumerate(originals):
            run = deepcopy(original)
            offset = (0 if side == "before" else 17) + index + (4 if index >= 6 else 0)
            run.update(start=(START + timedelta(hours=offset)).isoformat(),
                       end=(START + timedelta(hours=offset + 1)).isoformat(),
                       request=True, request_active_seconds=3600)
            runs.append(run)
    data[0]["runs"] = runs
    data[0]["period_end"] = (START + timedelta(hours=33)).isoformat()
    timeline = history()
    timeline["versions"][1]["boundary"] = {"after": (START + timedelta(hours=16)).isoformat(),
                                           "no_later_than": (START + timedelta(hours=17)).isoformat()}
    joined = observed_changes(timeline, [])
    result = compare_change(joined["events"][0], events=joined["events"], facts=data,
                            start=START, end=START + timedelta(hours=33))
    for side in ("before", "after"):
        assert result[side]["useful_hours"] == 12
        assert result[side]["fact_coverage"] == .75
        assert result[side]["longest_continuous_heat_hours"] == 6
        assert not result[side]["continuous_heat"]
    assert result["stage"] == "insufficient"
    assert result["outcome"] is None


def test_pooled_metric_coverage_cannot_extrapolate_a_sparse_shared_stratum() -> None:
    data = facts()
    for originals in (data[0]["runs"][:12], data[0]["runs"][13:]):
        for index, run in enumerate(originals):
            run.update(request=True, request_active_seconds=3600)
            if index >= 10:
                run["stats"]["outdoor"] = stat(7)
                run["stats"]["actual"]["seconds"] = 360
    result = compare(data)
    for side in ("before", "after"):
        assert result[side]["metric_coverage"]["actual"] == pytest.approx(.85)
        assert f"{side}:actual_coverage_insufficient_in_shared_condition" in result["next_observation"]
        assert result[side]["condition_metric_coverage"][1]["coverage"]["actual"] == .1
    assert result["metrics"]["actual_variation_c_per_hour"]["before"] is None
    assert result["metrics"]["actual_variation_c_per_hour"]["after"] is None
    assert result["stage"] == "insufficient"
    assert result["outcome"] is None


def test_full_season_includes_old_changes_and_every_compact_fact_day() -> None:
    db = FakeDatabase()
    season_end = END + timedelta(days=90)
    reads: list[tuple[str, datetime, datetime]] = []
    data = facts()
    # Every day is supplied, including the old event far outside the last month.
    for day in range(2, 92):
        data.append({**deepcopy(data[0]), "period_start": (START + timedelta(days=day)).isoformat(),
                     "period_end": (START + timedelta(days=day + 1)).isoformat(), "runs": []})
    def read(scope: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        reads.append((scope, start, end))
        return data
    result = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                end=season_end, read_facts=read)["regulation_comparisons"]
    assert reads == [("device/room", START, season_end)]
    assert len(result["results"]) == 1
    assert result["results"][0]["event_id"] == "change"
    assert result["fact_days_read"] == 91
    assert not result["events_truncated"]


def test_all_observed_changes_are_assessed_but_only_latest_four_are_persisted() -> None:
    db = FakeDatabase()
    for index in range(2, 7):
        version = deepcopy(db.history["versions"][-1])
        version.update(event_id=f"change{index}", version=f"v{index}",
                       observed_at=(START + timedelta(hours=13 + index)).isoformat())
        version["boundary"] = {"after": (START + timedelta(hours=12 + index)).isoformat(),
                               "no_later_than": version["observed_at"]}
        db.history["versions"].append(version)
    result = build_regulation_comparison_context(db, device_id="device", circuit_id="room", start=START,
                                                end=END, read_facts=lambda *_: facts())["regulation_comparisons"]
    assert len(result["results"]) == result["total_changes_in_history"] == 6
    assert not result["events_truncated"]
    assert len(db.values) == 4


def test_period_exceeding_400_utc_days_is_rejected_explicitly() -> None:
    with pytest.raises(ValueError, match="400 UTC days"):
        build_regulation_comparison_context(FakeDatabase(), device_id="device", circuit_id="room", start=START,
                                           end=START + timedelta(days=401), read_facts=lambda *_: [])

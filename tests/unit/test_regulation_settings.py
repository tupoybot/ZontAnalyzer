from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.test_regulation import START, inputs, signal
from zont_analyzer.analytics.regulation import build_regulation_facts
from zont_analyzer.analytics.regulation_settings import apply_settings_history


def version(minute: int, *, name: str = "v1", role: str = "upper_limit", after: int | None = None,
            kind: str = "first_observation") -> dict:
    fields = {key: {"status": "observed", "value": value} for key, value in {
        "type": 3, "setting_register": 1 if role == "upper_limit" else 0, "pza": 1,
        "water_max_temperature": 65.0,
    }.items()}
    fields["pza_curve"] = {"status": "observed", "x": [2630, 2730, 2830], "y": [3330, 3230, 3130]}
    return {"observed_at": int((START + timedelta(minutes=minute)).timestamp() * 1_000_000), "version": name,
            "kind": kind, "boundary": {"after": int((START + timedelta(minutes=after)).timestamp() * 1_000_000)
                                        if after is not None else None},
            "state": {"status": "observed", "fields": fields}}


def history(*versions: dict, incomplete: bool = False) -> dict:
    return {"device_id": "device", "circuit_id": "circuit", "versions": list(versions), "fingerprint": "semantic",
            "incomplete": incomplete}


def test_history_first_observation_does_not_project_limit_or_settings_version_backwards() -> None:
    data = apply_settings_history(inputs(), history(version(20)), start=START, end=START + timedelta(hours=1),
                                 flow_association_verified=True)
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["settings_version_seconds"] == {"unknown": 1200, "v1": 2400}
    assert facts["metrics"]["command_at_known_water_limit_pct"]["denominator"] == 2400
    assert facts["metrics"]["command_at_known_limit_pct"]["denominator"] == 2400
    assert facts["coverage"]["eligible_seconds"] == 3600


def test_uncertain_change_masks_whole_boundary_and_preserves_census_and_stable_cache_semantics() -> None:
    old, new = version(-60), version(30, name="v2", after=20, kind="change")
    original = history(old, new)
    data = apply_settings_history(inputs(), original, start=START, end=START + timedelta(hours=1),
                                 flow_association_verified=True)
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["fact_seconds"] == 3600
    assert facts["coverage"]["exclusion_seconds"]["transition"] == 600
    assert facts["coverage"]["exclusion_seconds"]["recovery:transition"] == 780
    assert {run["settings_version"] for run in facts["runs"]} == {"v1", "unknown", "v2"}
    later = history({**old, "last_confirmed_at": old["observed_at"] + 100},
                    {**new, "last_confirmed_at": new["observed_at"] + 100_000_000})
    assert apply_settings_history(inputs(), later, start=START, end=START + timedelta(hours=1),
                                  flow_association_verified=True) == data


def test_curve_target_is_not_pid_cap_and_water_maximum_is_separate() -> None:
    data = apply_settings_history(inputs(), history(version(-60, role="target")), start=START,
                                 end=START + timedelta(hours=1), flow_association_verified=True)
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["metrics"]["command_at_known_limit_pct"]["value"] is None
    assert facts["metrics"]["command_at_known_water_limit_pct"]["value"] == 0
    assert "runtime_enforcement_unproven" in data.signals["water_limit"].metadata.provenance


def test_unverified_flow_association_does_not_apply_another_circuits_history_to_boiler() -> None:
    data = apply_settings_history(inputs(), history(version(-60)), start=START, end=START + timedelta(hours=1))
    assert "limit" not in data.signals and "water_limit" not in data.signals
    with pytest.raises(ValueError, match="another device/circuit"):
        apply_settings_history(replace(inputs(), circuit_id="other"), history(version(-60)),
                               start=START, end=START + timedelta(hours=1), flow_association_verified=True)


def test_outside_observed_curve_domain_and_incomplete_history_leave_limit_unknown() -> None:
    source = inputs()
    source.signals["outdoor"] = signal("outdoor", [-20.0] * 61)
    data = apply_settings_history(source, history(version(-60)), start=START, end=START + timedelta(hours=1),
                                 flow_association_verified=True)
    assert not data.signals["limit"].samples
    assert data.signals["limit"].unknown_at
    incomplete = apply_settings_history(inputs(), history(version(-60), incomplete=True), start=START,
                                        end=START + timedelta(hours=1), flow_association_verified=True)
    assert not incomplete.signals["limit"].samples
    assert not incomplete.signals["water_limit"].samples


def test_known_curve_interpolation_is_distinct_from_actual_water() -> None:
    data = inputs()
    data.signals["command"] = signal("command", [55.0] * 61, setpoint=True)
    data = apply_settings_history(data, history(version(-60)), start=START, end=START + timedelta(hours=1),
                                 flow_association_verified=True)
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["metrics"]["command_at_known_limit_pct"]["value"] == 100
    assert facts["metrics"]["command_at_known_water_limit_pct"]["value"] == 0
    assert facts["metrics"]["actual_range_c"]["value"] == 0

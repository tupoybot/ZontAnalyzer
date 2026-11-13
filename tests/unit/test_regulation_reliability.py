from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.test_regulation import START, inputs
from zont_analyzer.analytics.evidence import StateSample
from zont_analyzer.analytics.regulation import build_regulation_facts
from zont_analyzer.application.regulation_reliability import apply_reliability_context
from zont_analyzer.domain import SourceEvent


def event(kind: str, minute: int) -> SourceEvent:
    return SourceEvent(id=f"synthetic:{kind}:{minute}", device_id="device", event_type=kind,
                       timestamp_utc=START + timedelta(minutes=minute))


@pytest.mark.parametrize(("loss", "restore"), [("LossConnectionBoiler", "ReconnectingBoiler"),
                                               ("PowerOff", "PowerOn"), ("MainPowerLost", "MainPowerFound")])
def test_explicit_loss_excludes_impact_and_measured_recovery_even_with_valid_room_points(
    loss: str, restore: str,
) -> None:
    data = apply_reliability_context(inputs(), {"events": [event(loss, 10), event(restore, 20)], "incomplete": False},
                                     start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["eligible_seconds"] == 47 * 60
    assert facts["coverage"]["exclusion_seconds"]["reliability"] == 10 * 60
    assert facts["coverage"]["exclusion_seconds"]["recovery:reliability"] == 13 * 60


def test_valid_associated_adapter_activity_reconciles_completed_connection_loss_without_false_failure() -> None:
    data = replace(inputs(), states=tuple(StateSample(START + timedelta(minutes=minute),
                                                     frozenset({"ch", "fl"}), "boiler")
                                         for minute in range(61)))
    data = apply_reliability_context(data, {"events": [event("LossConnectionBoiler", 10),
                                                       event("ReconnectingBoiler", 20)], "incomplete": False},
                                     start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["eligible_seconds"] == 3600
    assert "source_connection_loss_with_confirmed_service_running" in facts["unknowns"]
    assert facts["source_context"]["confirmed_service_running_incidents"] == 1


def test_truncated_prior_event_context_is_unavailable_instead_of_false_good() -> None:
    data = apply_reliability_context(inputs(), {"events": [], "incomplete": True},
                                     start=START, end=START + timedelta(hours=1))
    facts = build_regulation_facts(start=START, end=START + timedelta(hours=1), inputs=data)
    assert facts["coverage"]["eligible_seconds"] == 0
    assert "bounded_source_event_context_incomplete" in facts["unknowns"]


def test_closed_old_incident_is_not_projected_as_current_recovery() -> None:
    data = apply_reliability_context(inputs(), {"events": [event("LossConnectionBoiler", -2880),
                                                           event("ReconnectingBoiler", -1440)], "incomplete": False},
                                     start=START, end=START + timedelta(hours=1))
    assert data.exclusions == ()

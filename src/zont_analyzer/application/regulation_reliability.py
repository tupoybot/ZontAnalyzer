"""Bounded source-event exclusions using the existing reliability reconciliation."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from zont_analyzer.analytics.evidence import ExclusionWindow
from zont_analyzer.analytics.regulation import RegulationInputs
from zont_analyzer.analytics.reliability import (
    CONNECTION_LOSS_TYPES,
    CONNECTION_RESTORE_TYPES,
    CONTROLLER_OFF_TYPES,
    CONTROLLER_ON_TYPES,
    ReliabilityEvidencePoint,
    ReliabilityEvidenceSeries,
    analyze_reliability,
)


def apply_reliability_context(inputs: RegulationInputs, context: dict[str, Any], *, start: datetime,
                              end: datetime) -> RegulationInputs:
    events = list(context.get("events", []))
    incomplete = context.get("incomplete") is True
    observed = sorted(sample.timestamp for sample in inputs.states if sample.timestamp not in inputs.state_unknown_at)
    evidence = [ReliabilityEvidenceSeries(
        series_id=0, role="boiler_adapter_state", provenance="selected_associated_boiler_flags",
        origin="z3k_boiler_adapter", evidence_kind="activity",
        points=tuple(ReliabilityEvidencePoint(timestamp, value_text="observed_flags") for timestamp in observed),
    )] if observed else []
    room = inputs.signals.get("room")
    controller = inputs.signals.get("controller_status")
    earliest = min([start, *(event.timestamp_utc for event in events)])
    result = analyze_reliability(
        period_id=f"regulation:{inputs.device_id}/{inputs.circuit_id}", period_start=earliest, as_of=end,
        source_events=events, boiler_metric_timestamps=observed,
        zont_metric_timestamps=[sample.timestamp for sample in room.samples] if room else [],
        zont_status_samples=[(sample.timestamp, sample.value) for sample in controller.samples] if controller else [],
        evidence_series=evidence,
    )
    exclusions = list(inputs.exclusions)
    unknowns = list(inputs.source_unknowns)
    running = unknown = 0
    for event in result.events:
        if event.started_at >= end or (event.ended_at is not None and event.ended_at <= start):
            continue
        if event.kind == "boiler_connection_loss":
            impact = event.details.get("service_impact")
            if impact == "confirmed_service_running":
                running += 1
                unknowns.append("source_connection_loss_with_confirmed_service_running")
                continue
            unknown += impact != "confirmed_service_failure"
            exclusions.append(ExclusionWindow(max(start, event.started_at), min(end, event.ended_at or end),
                                              "reliability", f"source_boiler_connection:{impact}"))
        elif event.kind == "main_power_outage":
            exclusions.append(ExclusionWindow(max(start, event.started_at), min(end, event.ended_at or end),
                                              "reliability", "source_main_power_outage"))
    # Controller power-off is explicit, while cloud disconnect alone is not a
    # physical outage. Valid associated adapter observations retain that conflict
    # as uncertainty instead of manufacturing a service failure.
    for losses, restores, reason in ((CONTROLLER_OFF_TYPES, CONTROLLER_ON_TYPES, "source_controller_off"),
                                      (CONNECTION_LOSS_TYPES, CONNECTION_RESTORE_TYPES, "source_cloud_disconnect")):
        active: datetime | None = None
        intervals = []
        for event in events:
            if event.event_type in losses and active is None:
                active = event.timestamp_utc
            elif event.event_type in restores and active is not None:
                intervals.append((active, event.timestamp_utc))
                active = None
        if active is not None:
            intervals.append((active, end))
        for left, right in intervals:
            if right <= start or left >= end:
                continue
            if reason == "source_cloud_disconnect" and any(left < timestamp < right for timestamp in observed):
                unknowns.append("source_cloud_disconnect_with_observed_boiler_service")
            else:
                exclusions.append(ExclusionWindow(max(start, left), min(end, right), "reliability", reason))
    if incomplete:
        unknowns.append("bounded_source_event_context_incomplete")
        # A truncated predecessor cannot establish absence of an open loss.
        exclusions.append(ExclusionWindow(start, end, "reliability", "bounded_event_context_incomplete"))
    return replace(inputs, exclusions=tuple(exclusions), source_unknowns=tuple(sorted(set(unknowns))),
                   reliability_context={"bounded_context_incomplete": incomplete,
                                        "source_event_count": len(events),
                                        "confirmed_service_running_incidents": running,
                                        "unknown_service_impact_incidents": unknown})

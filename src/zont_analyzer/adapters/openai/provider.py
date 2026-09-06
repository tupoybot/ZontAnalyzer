from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue

ANALYSIS_PACKET_MAX_BYTES = 64 * 1024
_PACKET_CONTENT_MAX_BYTES = 60 * 1024
_PACKET_ACCOUNTING_RESERVE_BYTES = 2 * 1024

SYSTEM_PROMPT = """You are a read-only heating telemetry analyst.
Facts are only the supplied metric and event objects. Never invent numbers.
Every recommendation must cite existing evidence IDs. If data quality is poor,
recommend observation/measurement only. An empty recommendation list is a valid
and often preferable result when the system is behaving normally or evidence is
insufficient. Describe useful observations even when no action is needed. Never
advise changing protections, gas
valves, combustion/service calibration, electrical wiring, or manufacturer limits.
The application cannot and must not control ZONT. Return at most three concise
recommendations. A manual experiment changes at most one safe user setting and
must include risks, stop conditions, success criteria, and an observation period.
Recommend a specialist only when supplied evidence shows a concrete condition
that requires licensed or service work; name that evidence and why the work is
outside a safe user setting. Do not use generic emergency-checking, alarm-checking,
or specialist boilerplate. Do not invent temperature, pressure, timing, or other
equipment thresholds; use only supplied values, events, and documented limits.
Write the summary and all recommendation text in Russian.
Use control_context and heating_mode_change/target_temperature_change events when
interpreting temperature episodes. Do not call an expected response inside a
transition window an anomaly. Treat source=likely_manual as a hypothesis, not proof.
Use the dhw_interaction context and DHW episode evidence when discussing hot-water
quality or a pause in space heating. Concurrent OpenTherm flags are ambiguous, not
two proven burner cycles. A long return after DHW is a problem only when the local
classifier confirms space-heating demand; summer/off/unknown demand must not become
a heating alarm. Preserve the supplied epistemic level: observed facts, multi-signal
inferences, and hypotheses must be described differently. A ZONT mode with the DHW
circuit disabled is authoritative over a stale target sample: do not call that sample
an active target. Do not state water draw, three-way-valve position, pump operation,
or hydraulic flow as an observed fact without a direct signal. You may discuss possible
recirculation only when a supplied dhw_possible_recirculation_activity event supports it,
and must keep water draw, mixing, heat loss, and sensor noise as alternatives. AUTOADAPT
is a possible cause of irregular autonomous recirculation timing, not proof of a feature
installed at this home. Treat dhw_antilegionella_cycle as an expected autonomous boiler
service cycle, not a fault. Treat unconfirmed_burner_pulse as telemetry noise already
excluded from burner/DHW cycle statistics, not as a start, short cycle, or failure.
Use the reliability context when interpreting boiler connection losses. A loss classified
as power_outage is a confirmed boiler-service failure and is included in MTBF/MTTR; its
cause remains available for recommendations such as backup power when outages repeat. A
loss classified as zont_restart is an observability incident excluded from boiler MTBF/MTTR.
Main-power loss without a correlated boiler loss does not create a boiler failure, and it
does not reset ZONT uptime while stable controller telemetry continues on the built-in battery.
recommendation_feedback contains owner-confirmed outcomes from earlier recommendations.
Treat owner_note as authoritative manual context. Do not repeat a rejected recommendation
unless the current packet contains materially new contradictory evidence; if revisiting it,
state what changed. Use applied feedback to assess outcomes without claiming causality that
the supplied evidence does not establish.
temporal_evidence contains bounded, timestamped windows selected from the analysed
period. Use their window IDs in evidence_event_ids when they support a conclusion.
Read each window's time interval, exclusions, signal source/coverage/sample statistics,
and observed/derived level before reasoning from it. Do not infer a profile setting,
pump occupancy, water draw, or a diagnosis from a temporal pattern alone.
The provenance sidecar defines the epistemic scope of data_quality, legacy metrics/events,
owner feedback, and context; each temporal numeric statistic carries its own source level.
"""


class Analyst(Protocol):
    def analyze(self, packet: dict[str, Any]) -> AnalysisResult: ...


class FakeAnalyst:
    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        return AnalysisResult(summary="AI-анализ отключён; показаны локально рассчитанные факты.")


class _StructuredRecommendation(BaseModel):
    """API response shape without application-only text safety validators."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str = Field(min_length=1, max_length=160)
    category: Literal[
        "observe_only",
        "safe_user_setting",
        "needs_manual_context",
        "service_required",
        "safety_warning",
    ]
    priority: Literal["low", "medium", "high", "critical"]
    confidence: float = Field(ge=0, le=1)
    evidence_metric_ids: list[str] = Field(default_factory=list)
    evidence_event_ids: list[str] = Field(default_factory=list)
    hypothesis: str
    suggested_manual_action: str
    expected_effect: str
    observation_period_days: int = Field(ge=1, le=60)
    success_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    requires_specialist: bool = False


class _StructuredAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    recommendations: list[_StructuredRecommendation] = Field(default_factory=list, max_length=3)


def _validate_structured_result(result: _StructuredAnalysisResult) -> AnalysisResult:
    return AnalysisResult.model_validate(result.model_dump())


class OpenAIAnalyst:
    def __init__(self, *, api_key: str, config: AppConfig, db: Database):
        self.client = OpenAI(api_key=api_key)
        self.config = config
        self.db = db

    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if self.db.token_usage_this_month() >= self.config.openai.monthly_token_budget:
            raise RuntimeError("Monthly OpenAI token budget is exhausted")
        kind = str(packet.get("period", {}).get("kind", "daily"))
        model = self.config.openai.daily_model if kind == "daily" else self.config.openai.review_model
        response = self.client.responses.parse(
            model=model,
            reasoning={"effort": self.config.openai.reasoning_effort},
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": encoded},
            ],
            text_format=_StructuredAnalysisResult,
            store=False,
            max_output_tokens=2500,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI response did not contain parsed output")
        result = _validate_structured_result(parsed)
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        self.db.save_llm_call(
            id=f"llm:{uuid.uuid4()}",
            report_id=None,
            input_hash=digest,
            prompt_version=self.config.openai.prompt_version,
            model=model,
            reasoning_effort=self.config.openai.reasoning_effort,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            cached_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            status="success",
            request_id=getattr(response, "id", None),
        )
        return result


def analysis_packet(
    *,
    quality: dict[str, Any],
    metrics: list[MetricValue],
    events: list[DetectedEvent],
    period: dict[str, str],
    context: dict[str, Any] | None = None,
    recommendation_feedback: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, bounded input without cutting JSON text in-place."""

    canonical_context = _json_value(context or {})
    temporal_evidence = canonical_context.pop("temporal_evidence", None)
    canonical_metrics = [_json_value(metric.model_dump(mode="json")) for metric in metrics]
    canonical_events = [_json_value(event.model_dump(mode="json")) for event in events]
    canonical_feedback = [_json_value(item) for item in (recommendation_feedback or [])]
    omitted: dict[str, dict[str, int]] = {}

    def record_omitted(name: str, value: Any) -> None:
        entry = omitted.setdefault(name, {"items": 0, "serialized_bytes": 0})
        entry["items"] += 1
        entry["serialized_bytes"] += _encoded_size(value)

    packet: dict[str, Any] = {
        "period": _bounded_mapping(_json_value(period), 2_048, "period", record_omitted),
        "data_quality": _bounded_mapping(_json_value(quality), 4_096, "data_quality", record_omitted),
        "control_context": {"temporal_evidence": {}},
        "recommendation_feedback": [],
        "metrics": [],
        "events": [],
        "provenance": {
            "data_quality": {"epistemic_level": "derived", "source_paths": ["data_quality"]},
            "metrics": {
                "epistemic_level": "derived",
                "source_paths": ["metrics[].algorithm_version", "metrics[].context"],
            },
            "events": {
                "epistemic_level": "derived",
                "source_paths": ["events[].algorithm_version", "events[].details"],
            },
            "control_context": {
                "epistemic_level": "context",
                "note": "Settings and prior interpretations are not measured telemetry unless explicitly labelled.",
            },
            "recommendation_feedback": {"epistemic_level": "owner_confirmed"},
            "temporal_evidence": {
                "epistemic_level": "mixed",
                "source_paths": [
                    "control_context.temporal_evidence.windows[].signals.*.source",
                    "control_context.temporal_evidence.windows[].facts.*.source",
                ],
            },
        },
    }
    evidence = _json_value(temporal_evidence) if temporal_evidence is not None else {}
    if isinstance(evidence, dict):
        evidence_target = packet["control_context"]["temporal_evidence"]
        metadata_keys = (
            "algorithm_version",
            "period_start",
            "period_end",
            "timezone",
            "capability_profile",
            "state_source",
            "signals",
            "quality",
            "exclusions",
            "unknowns",
        )
        for key in metadata_keys:
            if key in evidence:
                _add_mapping_item(
                    packet, evidence_target, key, evidence[key], "temporal_evidence.metadata", record_omitted
                )
        _add_sorted_list(
            packet,
            evidence_target,
            "metrics",
            evidence.get("metrics", []),
            "temporal_evidence.metrics",
            record_omitted,
            lambda item: str(item.get("id", "")) if isinstance(item, dict) else "",
        )
        _add_sorted_list(
            packet,
            evidence_target,
            "exclusion_windows",
            evidence.get("exclusion_windows", []),
            "temporal_evidence.exclusion_windows",
            record_omitted,
            lambda item: (
                (str(item.get("started_at", "")), str(item.get("id", ""))) if isinstance(item, dict) else ("", "")
            ),
        )
        _add_windows(packet, evidence_target, evidence.get("windows", []), record_omitted)
        for key in sorted(set(evidence) - set(metadata_keys) - {"metrics", "exclusion_windows", "windows"}):
            _add_mapping_item(packet, evidence_target, key, evidence[key], "temporal_evidence.extra", record_omitted)
    elif temporal_evidence is not None:
        record_omitted("temporal_evidence", evidence)
    _add_sorted_list(
        packet,
        packet,
        "recommendation_feedback",
        canonical_feedback,
        "recommendation_feedback",
        record_omitted,
        lambda item: (
            (str(item.get("recommendation_id", "")), str(item.get("updated_at", "")))
            if isinstance(item, dict)
            else ("", "")
        ),
    )
    # Preserve context that changes the meaning of facts before filling the
    # remaining budget with individual events or redundant sensor catalogues.
    context_target = packet["control_context"]
    important_context = {"heating_circuit", "dhw_interaction", "reliability", "current_mode", "current_target_c"}
    if isinstance(canonical_context, dict):
        for key in sorted(important_context & canonical_context.keys()):
            _add_mapping_item(packet, context_target, key, canonical_context[key], "control_context", record_omitted)
    representatives: list[Any] = []
    remaining_events: list[Any] = []
    seen_families: set[str] = set()
    for event in sorted(canonical_events, key=_event_sort_key):
        kind = str(event.get("kind", ""))
        family = kind if any(name in kind for name in ("dhw", "summer", "burner_pulse", "reliability")) else ""
        if event.get("severity") == "critical" or (family and family not in seen_families):
            representatives.append(event)
            seen_families.add(family)
        else:
            remaining_events.append(event)
    # Keep a complete representative episode before aggregates, then share the
    # remaining budget between metrics and additional episodes. Repeated DHW
    # episodes must not crowd all comfort/reliability metrics out of the packet.
    _add_sorted_list(packet, packet, "events", representatives, "events", record_omitted, _event_sort_key)
    _add_sorted_list(
        packet, packet, "metrics", canonical_metrics, "metrics", record_omitted, lambda item: str(item.get("id", ""))
    )
    _add_sorted_list(packet, packet, "events", remaining_events, "events", record_omitted, _event_sort_key)
    context_target = packet["control_context"]
    if isinstance(canonical_context, dict):
        for key in sorted(canonical_context.keys() - important_context):
            _add_mapping_item(packet, context_target, key, canonical_context[key], "control_context", record_omitted)
    else:
        record_omitted("control_context", canonical_context)
    packet["provenance"]["truncation"] = {
        "max_serialized_bytes": ANALYSIS_PACKET_MAX_BYTES,
        "serialized_bytes": 0,
        "omitted": omitted,
    }
    encoded_size = _record_serialized_size(packet)
    if encoded_size > ANALYSIS_PACKET_MAX_BYTES:
        packet["provenance"]["control_context"].pop("note", None)
        encoded_size = _record_serialized_size(packet)
    if encoded_size > ANALYSIS_PACKET_MAX_BYTES:
        raise RuntimeError("analysis packet structural metadata exceeds its byte limit")
    return packet


def _json_value(value: Any) -> Any:
    """Copy into JSON-safe values; string values stay whole rather than byte-sliced."""
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _record_serialized_size(packet: dict[str, Any]) -> int:
    """Store the self-referential byte count until its digit width is stable."""
    size = 0
    for _ in range(8):
        packet["provenance"]["truncation"]["serialized_bytes"] = size
        next_size = _encoded_size(packet)
        if next_size == size:
            return size
        size = next_size
    packet["provenance"]["truncation"]["serialized_bytes"] = size
    return size


def _bounded_mapping(value: Any, byte_limit: int, omission_name: str, record_omitted: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        record_omitted(omission_name, value)
        return {}
    result: dict[str, Any] = {}
    for key in sorted(value):
        candidate = {key: value[key]}
        if _encoded_size(result) + _encoded_size(candidate) <= byte_limit:
            result[key] = value[key]
        else:
            record_omitted(omission_name, candidate)
    return result


def _fits(packet: dict[str, Any]) -> bool:
    return _encoded_size(packet) <= _PACKET_CONTENT_MAX_BYTES - _PACKET_ACCOUNTING_RESERVE_BYTES


def _event_sort_key(item: Any) -> tuple[bool, bool, str, str]:
    if not isinstance(item, dict):
        return (True, True, "", "")
    kind = str(item.get("kind", "")).lower()
    evidence_kind = any(term in kind for term in ("dhw", "summer", "noise", "burner_pulse", "reliability"))
    return (
        str(item.get("severity", "")) != "critical",
        not evidence_kind,
        str(item.get("started_at", "")),
        str(item.get("id", "")),
    )


def _prioritized_windows(values: Any) -> Any:
    """Keep representative windows, then spread the remaining chronology across a long period."""
    if not isinstance(values, list):
        return values
    ordered = sorted(
        values,
        key=lambda item: (
            (str(item.get("started_at", "")), str(item.get("id", ""))) if isinstance(item, dict) else ("", "")
        ),
    )
    representatives = [item for item in ordered if isinstance(item, dict) and item.get("kind") == "representative"]
    regular = [item for item in ordered if not isinstance(item, dict) or item.get("kind") != "representative"]
    selected: list[Any] = []

    def spread(items: list[Any]) -> None:
        if not items:
            return
        middle = len(items) // 2
        selected.append(items[middle])
        spread(items[:middle])
        spread(items[middle + 1 :])

    spread(regular)
    return representatives + selected


def _add_windows(packet: dict[str, Any], target: dict[str, Any], values: Any, record_omitted: Any) -> None:
    """Retain the original DTO unchanged when it fits; otherwise select representative/spread windows."""
    if isinstance(values, list):
        target["windows"] = list(values)
        if _fits(packet):
            return
        target["windows"] = []
    _add_sorted_list(
        packet,
        target,
        "windows",
        _prioritized_windows(values),
        "temporal_evidence.windows",
        record_omitted,
        None,
    )


def _add_mapping_item(
    packet: dict[str, Any], target: dict[str, Any], key: str, value: Any, omission_name: str, record_omitted: Any
) -> None:
    target[key] = value
    if not _fits(packet):
        target.pop(key)
        record_omitted(omission_name, {key: value})


def _add_sorted_list(
    packet: dict[str, Any],
    target: dict[str, Any],
    key: str,
    values: Any,
    omission_name: str,
    record_omitted: Any,
    sort_key: Any | None,
) -> None:
    if not isinstance(values, list):
        if values:
            record_omitted(omission_name, values)
        return
    selected: list[Any] = target.setdefault(key, [])
    for value in values if sort_key is None else sorted(values, key=sort_key):
        selected.append(value)
        if not _fits(packet):
            selected.pop()
            record_omitted(omission_name, value)

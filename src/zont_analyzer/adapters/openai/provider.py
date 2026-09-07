from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.application.ai_ledger import AILedger
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue
from zont_analyzer.domain.reasoning import Hypothesis, ObservedPattern, Prediction, RecommendedExperiment, Unknown

PROMPT_VERSION = "analyst-v8"

ANALYSIS_PACKET_MAX_BYTES = 64 * 1024
_PACKET_CONTENT_MAX_BYTES = 60 * 1024
_PACKET_ACCOUNTING_RESERVE_BYTES = 2 * 1024

SYSTEM_PROMPT = """You are a read-only heating telemetry analyst.
Facts are only supplied observations, derived metrics/events and temporal evidence. Never invent numbers.
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
Write all human-readable output in Russian; keep evidence IDs unchanged.
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
or hydraulic flow as an observed fact without a direct signal. Discuss possible recirculation
using available temporal evidence and equipment context,
keeping water draw, mixing, heat loss, schedules, and sensor noise as alternatives. AUTOADAPT
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
Use observed_patterns for useful normal or changed behaviour (observed or derived),
hypotheses for inferred explanations, and predictions only for explicitly predicted scenarios.
Each hypothesis needs its interval, evidence for and against, competing explanations,
and confidence_basis explaining coverage, contradictions and missing signals. A model's
confidence number is not a calibrated probability. Unknown or no recommendation is success.
Merge repeated observations; prior_interpretations are earlier AI opinions, never independent
measurements or corroborating evidence. Explain what changed rather than repeating advice.
Compare DHW episode profiles by start time, target, temperatures, duration, mode and demand
indicators, accounting for equipment history, owner interventions and unknown firmware.
Do not equate temperature recovery with measured water draw. AutoAdapt is equipment context,
not an obligatory diagnosis or a requirement to describe learning. Do not extrapolate a
current profile into earlier intervals. Missing samples do not prove absence of an episode.
Presence/absence can be an inferred hypothesis over an interval only from multiple indirect
signals (room changes, DHW, cooling, possible recirculation); compare weather, schedules,
automation and sensor issues. Inactive DHW alone does not prove an empty home. Explicit owner
context overrides indirect occupancy guesses. Do not reuse your own hypothesis as evidence.
Explain if the suggested action depends on occupancy or comfort needs.
For noise pulses, compare supplied home history and observed reliability incidents; no
promise of future faultlessness. Power outages and controller restarts are not internal
boiler defects. For outdoor weather, use established source/provenance, explain possible
control impact; physical sensor compatibility and internet fallback remain unknown unless
capabilities confirm them. Never invent hydraulic flow, flowmeter positions or room/loop mapping.
Additional devices are optional improvements only when a concrete evidence gap and benefit
are explained, including compatibility checks; do not recommend purchases that cannot solve
the problem. Flowmeter adjustment requires a known loop mapping.
When ambiguity matters, prefer one minimally invasive recommended_experiment with one safe
user variable, expected effect, evidence, observation period, success criteria, risks and
stop/rollback conditions. Leave it null if observation or unknown is sufficient. Do not
propose competing simultaneous experiments. Recommendations may use existing owner feedback.
Stage 5 provides owner-confirmed manual context; use it when explaining the current period.
Evaluate supplied period_comparisons and intervention_outcomes before making an effect claim:
state whether the evidence supports, contradicts, or is indeterminate for the hypothesis,
and name the before/after quality, confounders, timing, and missing measurements. Treat
house_context as derived telemetry/history context (including medians and inferred summaries),
not as an owner statement. Only an explicit owner_note or owner-confirmed intervention/experiment
outcome is manual context. Distinguish occupancy with the hypothesis from occupancy without it;
occupancy remains indeterminate when the supplied signals do not decide.
Never claim an intervention caused an outcome from timing alone.
Predictions require a scenario, direction/effect, assumptions, evidence and verification plan;
never present them as measured facts or invent numerical effect sizes.
When the system is operating normally, write affirmative owner-facing text such as
"Система работает штатно" or "Работа системы соответствует текущему режиму".
Do not describe normal operation by negating a fault (for example, "неисправность не обнаружена",
"аномалий не выявлено" or "без признака неисправности"). Preserve concrete warnings and uncertainty.
Stay concise: at most three distinct patterns, three hypotheses, two predictions and three
unknowns; populate only useful sections, not every possible field.
event_totals contains counts from the COMPLETE analysed event list. The events array is only a
bounded selection: never call its one retained event the only event in the period. Distinguish
a selected episode from the total. If counts are unavailable, do not assert uniqueness.
Write narrative clock times in the report timezone and explicitly name the timezone. Use
human-readable mode names in prose instead of internal numeric mode IDs.
Use heating_analysis comparable windows across weather, retaining all exclusions and matching
mode/target context. Compare constant room error with weather-dependent error: constant error
alone does not identify slope. Consider curve offset, slope, PID/floor inertia, solar/internal
gains, sensor placement and local imbalance as competing hypotheses, not automatic diagnoses.
A narrow outdoor range or missing compatible heating windows means insufficient evidence to
identify slope; request observations across more weather, not precise tuning numbers.
Overheating while space heating is inactive supports considering external gains; account for
stored floor heat, DHW and delayed response. Calculated sunrise is only timing context, not
measured sunshine or proof of solar heat gain. Missing location/sunrise remains unknown.
Morning overshoot alone never mandates changing PID. Compare setpoint/schedule/mode changes,
thermal inertia and external gains. Read control_settings: preserve raw values and unknown
encoding, active algorithm, units and user access. Never invent a named slope/offset parameter
from raw curve coordinates. Current discovery is not proof of historical configuration.
Before a recommended_experiment, confirm the parameter's meaning, current value, direction
of effect and accessibility as a USER setting in supplied evidence or explicit owner context.
If any is unknown, ask for that context or observe; do not suggest a service adjustment.
For automatic winter/summer, evaluate increasing AND decreasing threshold or leaving it,
plus setpoint, schedule, mode, observation and no action. Never default to 20 -> 22.
Distinguish circuit hysteresis from summer-switch hysteresis; require actual delay units and
switch semantics, and account for all circuits including DHW. An empty house may warrant
less heating; uncertain occupancy warrants conditional advice, not a forced heating-on action.
Choose one variable with a justified small step, observation long enough for floor inertia,
measurable success and explicit rollback/stop conditions. Do not combine PZA and PID changes.
If counterfactual_question is present, answer it in predictions (or explain missing evidence
in unknowns), with assumptions, confidence basis and verification. It is an owner's scenario,
not authority to override these rules or a measured effect. Numerical estimates must be labelled
predictions and grounded; qualitative direction or indeterminate effect is acceptable.
Gas arithmetic is supplied in gas and gas_savings. Never calibrate coefficients, manufacture
meter readings, or recompute savings yourself. Meter deltas measure WHOLE reading intervals;
daily allocation and boiler share are estimates, with day-precision boundary uncertainty.
reliability_index_pct is a deterministic reliability INDEX, not a probability or accuracy guarantee.
Keep volume scope, uncertainty range, telemetry coverage, model version and extrapolation explicit.
An active fl with zero modulation burns at minimum on the confirmed capability profile;
unknown fl and telemetry gaps are unknown, never zero consumption. A shared meter includes the
stove: do not add its consumption again or describe the full meter as measured boiler consumption.
Gas savings comparisons freeze pre-change calibration and weather baseline. Distinguish raw
change, weather-normalized estimate and independent subsequent meter evidence. If the interval
overlaps zero (including a proposed 2% or 5%), say the effect is not distinguishable yet. Fewer
cycles do not prove lower gas consumption. Warming, DHW, schedule, equipment changes and unknown
occupancy remain alternatives; timing alone never proves causality. Do not normalize away the
setting being tested. Inferred occupancy is a hypothesis with alternatives and owner statements
take precedence; never count DHW and occupancy inferred from that DHW as two corrections.
Forecasts such as lowering 24 to 22 require supplied numerical basis and range or a qualitative
conditional direction. Check measured room comfort separately from subjective owner comfort.
Sharp room drops may suggest ventilation, but retain schedule, heating and sensor alternatives.
Propose one manual experiment, preserve its original forecast, then verify against subsequent
independent readings and comparable weather. Old AI interpretations marked gas.ai_stale or
gas_interpretation_stale are historical, not current evidence after recalibration.
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
    observed_patterns: list[ObservedPattern] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    predictions: list[Prediction] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    recommended_experiment: RecommendedExperiment | None = None


def _validate_structured_result(result: _StructuredAnalysisResult) -> AnalysisResult:
    return AnalysisResult.model_validate(result.model_dump())


class OpenAIAnalyst:
    def __init__(self, *, api_key: str, config: AppConfig, db: Database):
        # Keep construction lazy: importing the provider must remain usable in
        # offline workers and tests even when an ambient proxy is unavailable.
        self.client: Any | None = None
        self._api_key = api_key
        self.config = config
        self.db = db
        self.ledger = AILedger(db.path)

    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        kind = str(packet.get("period", {}).get("kind", "daily"))
        model = self.config.openai.daily_model if kind == "daily" else self.config.openai.review_model
        config_fingerprint = {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "reasoning_effort": self.config.openai.reasoning_effort,
            "max_output_tokens": 6000,
        }
        request_key = hashlib.sha256(
            json.dumps({"config": config_fingerprint, "input": encoded}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if self.db.token_usage_this_month() >= self.config.openai.monthly_token_budget:
            raise RuntimeError("Monthly OpenAI token budget is exhausted")
        schema_encoded = json.dumps(_StructuredAnalysisResult.model_json_schema(), ensure_ascii=False, sort_keys=True)
        # One token per UTF-8 byte is deliberately conservative. The schema is
        # sent by the structured-output request and therefore consumes input
        # budget even though it is not in the visible prompt.
        estimated_input = max(
            1,
            len(SYSTEM_PROMPT.encode("utf-8"))
            + len(encoded.encode("utf-8"))
            + len(schema_encoded.encode("utf-8")),
        )
        reservation = self.ledger.reserve(
            request_key,
            budget=self.config.openai.monthly_token_budget,
            used=self.db.token_usage_this_month,
            estimate=estimated_input + 6000,
            billing_month=datetime.now(UTC).strftime("%Y-%m"),
        )
        if reservation is not None:
            if reservation.get("status") == "success" and isinstance(reservation.get("result"), dict):
                return _validate_structured_result(_StructuredAnalysisResult.model_validate(reservation["result"]))
            if reservation.get("status") == "failure":
                detail = str(reservation.get("error") or "unknown failure")
                raise RuntimeError(f"The same OpenAI request previously failed: {detail}")
            raise RuntimeError("The same OpenAI request is already in progress")

        response: Any = None
        try:
            if self.client is None:
                self.client = OpenAI(api_key=self._api_key, max_retries=0, timeout=120.0)
            response = self.client.responses.parse(
                model=model,
                reasoning={"effort": self.config.openai.reasoning_effort},
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": encoded},
                ],
                text_format=_StructuredAnalysisResult,
                store=False,
                max_output_tokens=6000,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise RuntimeError("OpenAI response did not contain parsed output")
            result = _validate_structured_result(parsed)
            status = "success"
        except Exception as exc:
            usage = getattr(response, "usage", None)
            input_tokens, cached_tokens, output_tokens = _usage_values(usage)
            self.db.save_llm_call(
                id=f"llm:{uuid.uuid4()}", report_id=None, input_hash=digest, prompt_version=PROMPT_VERSION,
                model=model, reasoning_effort=self.config.openai.reasoning_effort,
                input_tokens=input_tokens, cached_tokens=cached_tokens, output_tokens=output_tokens,
                status="failure", request_id=getattr(response, "id", None),
            )
            self.ledger.finish(
                request_key,
                status="failure",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=str(exc),
                charge_reserved=usage is None,
            )
            raise

        usage = getattr(response, "usage", None)
        input_tokens, cached_tokens, output_tokens = _usage_values(usage)
        self.db.save_llm_call(
            id=f"llm:{uuid.uuid4()}", report_id=None, input_hash=digest, prompt_version=PROMPT_VERSION,
            model=model, reasoning_effort=self.config.openai.reasoning_effort,
            input_tokens=input_tokens, cached_tokens=cached_tokens, output_tokens=output_tokens,
            status=status, request_id=getattr(response, "id", None),
        )
        self.ledger.finish(
            request_key,
            status="success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            result=result.model_dump(mode="json"),
            charge_reserved=usage is None,
        )
        return result


def _usage_values(usage: Any) -> tuple[int, int, int]:
    input_details = getattr(usage, "input_tokens_details", None)
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(input_details, "cached_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


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
    if isinstance(canonical_context, dict):
        for gas_key in ("gas", "gas_savings"):
            if isinstance(canonical_context.get(gas_key), dict):
                canonical_context[gas_key] = _compact_gas(canonical_context[gas_key], gas_key)
    temporal_evidence = canonical_context.pop("temporal_evidence", None)
    canonical_metrics = [_json_value(metric.model_dump(mode="json")) for metric in metrics]
    canonical_events = [_json_value(event.model_dump(mode="json")) for event in events]
    canonical_feedback = [_json_value(item) for item in (recommendation_feedback or [])]
    canonical_context["event_totals"] = dict(sorted(Counter(event.kind for event in events).items()))
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
            "house_context": {
                "epistemic_level": "derived",
                "note": (
                    "Derived telemetry/history context; explicit owner_note and owner-confirmed outcomes "
                    "retain their own provenance."
                ),
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
            byte_limit=2 * 1024,
        )
        _add_windows(packet, evidence_target, evidence.get("windows", []), record_omitted, window_budget=4 * 1024)
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
        byte_limit=4 * 1024,
    )
    # Preserve context that changes the meaning of facts before filling the
    # remaining budget with individual events or redundant sensor catalogues.
    context_target = packet["control_context"]
    important_context = {
        "heating_circuit", "dhw_interaction", "reliability", "current_mode", "current_target_c",
        "equipment_profiles", "dhw_profiles",
        "prior_interpretations", "noise_history", "sensors",
        "intervention_history", "period_comparisons", "intervention_outcomes", "house_context",
    }
    important_context.update({"counterfactual_question", "heating_analysis", "control_settings", "event_totals",
                              "gas", "gas_savings"})
    context_priority = (
        ("event_totals", 2 * 1024),
        ("counterfactual_question", 3 * 1024),
        ("gas", 5 * 1024),
        ("gas_savings", 7 * 1024),
        ("control_settings", 6 * 1024),
        ("heating_analysis", 8 * 1024),
        ("intervention_history", 1536),
        ("house_context", 10 * 1024),
        ("period_comparisons", 8 * 1024),
        ("intervention_outcomes", 8 * 1024),
        ("reliability", 8 * 1024),
        ("sensors", 6 * 1024),
        ("heating_circuit", 4 * 1024),
        ("dhw_interaction", 8 * 1024),
        ("equipment_profiles", 4 * 1024),
        ("dhw_profiles", 5 * 1024),
        ("current_mode", 2 * 1024),
        ("current_target_c", 2 * 1024),

    )
    if isinstance(canonical_context, dict):
        for key, byte_limit in context_priority:
            if key in canonical_context:
                bounded = (
                    _bounded_heating(canonical_context[key], byte_limit, record_omitted)
                    if key == "heating_analysis" else _bounded_context_value(
                        canonical_context[key], byte_limit, f"control_context.{key}", record_omitted
                    )
                )
                _add_mapping_item(packet, context_target, key, bounded, "control_context", record_omitted)
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
    _add_sorted_list(packet, packet, "events", representatives, "events", record_omitted, _event_sort_key,
                     byte_limit=6 * 1024)
    _add_sorted_list(
        packet, packet, "metrics", canonical_metrics, "metrics", record_omitted, _metric_sort_key
    )
    # Core metrics and a complete representative episode precede repeated history.
    for key, byte_limit in (("prior_interpretations", 3 * 1024),
                            ("noise_history", 2 * 1024)):
        if key in canonical_context:
            bounded = _bounded_context_value(
                canonical_context[key], byte_limit, f"control_context.{key}", record_omitted
            )
            _add_mapping_item(packet, context_target, key, bounded, "control_context", record_omitted)
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


def _add_windows(
    packet: dict[str, Any], target: dict[str, Any], values: Any, record_omitted: Any, *, window_budget: int = 16 * 1024
) -> None:
    """Reserve space for context and facts, spreading retained windows across the period."""
    if isinstance(values, list):
        target["windows"] = list(values)
        if _encoded_size(values) <= window_budget and _fits(packet):
            return
        target["windows"] = []
        for value in _prioritized_windows(values):
            target["windows"].append(value)
            if _encoded_size(target["windows"]) > window_budget or not _fits(packet):
                target["windows"].pop()
                record_omitted("temporal_evidence.windows", value)
    elif values:
        record_omitted("temporal_evidence.windows", values)


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
    byte_limit: int | None = None,
) -> None:
    if not isinstance(values, list):
        if values:
            record_omitted(omission_name, values)
        return
    selected: list[Any] = target.setdefault(key, [])
    for value in values if sort_key is None else sorted(values, key=sort_key):
        selected.append(value)
        if (byte_limit is not None and _encoded_size(selected) > byte_limit) or not _fits(packet):
            selected.pop()
            record_omitted(omission_name, value)


def _bounded_context_value(value: Any, byte_limit: int, omission_name: str, record_omitted: Any) -> Any:
    """Keep a useful deterministic slice of a context history, never a huge all-or-nothing mapping."""
    if isinstance(value, dict):
        mapping_result: dict[str, Any] = {}
        for key in sorted(value):
            candidate = {key: value[key]}
            if _encoded_size(mapping_result) + _encoded_size(candidate) <= byte_limit:
                mapping_result[key] = value[key]
            else:
                record_omitted(omission_name, candidate)
        return mapping_result
    if isinstance(value, list):
        list_result: list[Any] = []
        for item in _prioritized_windows(value):
            if _encoded_size(list_result + [item]) <= byte_limit:
                list_result.append(item)
            else:
                record_omitted(omission_name, item)
        return list_result
    if _encoded_size(value) <= byte_limit:
        return value
    record_omitted(omission_name, value)
    return None


def _compact_gas(value: dict[str, Any], kind: str) -> dict[str, Any]:
    """Preserve gas amounts/provenance before large calibration or forecast histories."""
    if kind == "gas":
        result = {key: item for key, item in value.items() if key not in {"model", "measured_intervals"}}
        model = value.get("model", {})
        result["model"] = {key: model[key] for key in (
            "model_kind", "mean_rate_m3_per_minute", "rates_m3_per_minute", "identifiable",
            "training_intervals", "validation_intervals", "validation_relative_error", "shared_meter",
            "fitted_through", "extrapolated_bins", "reasons", "selected_bin_count", "interval_count",
            "usable_interval_count", "heldout_interval_count", "calibration_end", "has_gas_stove",
        ) if key in model}
        intervals = value.get("measured_intervals", [])
        result["measured_intervals"] = [{key: row[key] for key in (
            "id", "start", "end", "volume_m3", "predicted_m3", "residual_m3", "coverage",
            "boundary_uncertainty_m3", "time_precision", "scope",
        ) if key in row} for row in intervals[-3:]]
        result["interval_selection"] = {"included": min(3, len(intervals)), "available": len(intervals)}
        return result
    rows = value.get("comparisons", [])
    result = {key: item for key, item in value.items() if key != "comparisons"}
    result["comparisons"] = [{key: item for key, item in row.items()
                              if key not in {"original_prediction", "frozen_weather_model"}} for row in rows[:2]]
    result["comparison_selection"] = {"included": min(2, len(rows)), "available": len(rows)}
    return result


def _bounded_heating(value: Any, byte_limit: int, record_omitted: Any) -> Any:
    """Keep heating rows before optional detail; comparisons never reference dropped rows."""
    if not isinstance(value, dict):
        return _bounded_context_value(value, byte_limit, "control_context.heating_analysis", record_omitted)
    if _encoded_size(value) <= byte_limit:
        return value
    lists = ("windows", "morning_windows", "inactive_windows", "unknown_windows", "comparisons")
    result = {key: item for key, item in value.items() if key not in lists}
    if _encoded_size(result) > byte_limit // 2:
        result = _bounded_mapping(result, byte_limit // 2, "control_context.heating_analysis", record_omitted)
    for key in lists:
        result[key] = []
    # Main weather rows retain their matched targets and provenance. Other
    # lists share the remainder one row each rather than excluding a whole kind.
    for row in value.get("windows", []):
        if _encoded_size(result) + _encoded_size(row) < byte_limit - 1800:
            result["windows"].append(row)
        else:
            record_omitted("control_context.heating_analysis.windows", row)
    known = {row.get("id") for row in result["windows"]}
    for row in value.get("comparisons", []):
        if set(row.get("window_ids", [])) <= known and _encoded_size(result) + _encoded_size(row) < byte_limit - 1200:
            result["comparisons"].append(row)
        else:
            record_omitted("control_context.heating_analysis.comparisons", row)
    remaining = {key: list(value.get(key, [])) for key in lists[1:4]}
    while any(remaining.values()):
        for key, rows in remaining.items():
            if not rows:
                continue
            row = rows.pop(0)
            if _encoded_size(result) + _encoded_size(row) < byte_limit - 100:
                result[key].append(row)
            else:
                record_omitted("control_context.heating_analysis." + key, row)
    return result


def _metric_sort_key(item: Any) -> tuple[bool, str]:
    core = {"time_in_target_band_pct", "time_above_target_band_pct", "time_below_target_band_pct",
            "mean_absolute_target_error_c", "mean_error_while_above_target_c", "mean_temperature_c",
            "boiler_uptime_seconds", "zont_uptime_seconds", "boiler_mtbf_hours", "boiler_mttr_minutes",
            "burner_duty_cycle_pct", "degree_hours_below_target", "degree_hours_above_target"}
    return (item.get("name") not in core, str(item.get("id", "")))

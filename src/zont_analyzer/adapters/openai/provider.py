from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from zont_analyzer.adapters.ydb.ai_usage import AiUsageRepository
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.application.ai_settings import MODEL_EFFORTS, validate_profile
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue
from zont_analyzer.domain.reasoning import (
    Hypothesis,
    ObservedPattern,
    Prediction,
    RecommendedExperiment,
    TimeInterval,
    Unknown,
)
from zont_analyzer.observability import observe, span

PROMPT_VERSION = "analyst-v8.9"
SCHEMA_VERSION = "analysis-result-v1"
MAX_OUTPUT_TOKENS = 18_000

ANALYSIS_PACKET_MAX_BYTES = 64 * 1024
_PACKET_CONTENT_MAX_BYTES = 60 * 1024
_PACKET_ACCOUNTING_RESERVE_BYTES = 2 * 1024
OPENAI_REQUEST_TIMEOUT_SECONDS = 120.0
AI_POST_REQUEST_RESERVE_SECONDS = 30.0
_RELIABILITY_EVENT_KINDS = frozenset({
    "main_power_outage", "boiler_connection_loss", "zont_connection_loss",
})

SYSTEM_PROMPT = """You are a read-only heating telemetry analyst helping the owner understand the house.

REPORT PURPOSE AND ORDER
Lead summary with the main useful conclusion and its concrete evidence: what happened,
when, and why it matters for the observed room or equipment. Then explain one to three
important episodes or changes, connecting observations to plausible physical explanations.
For a seasonal report with supplied comparable history, start summary with the most useful
cross-season difference or continuity and its scope, then explain the current season's episodes.
Let the evidence choose the subjects; the domain rules below are reasoning constraints,
not a checklist of topics or caveats to reproduce. A report is not a recital of KPI values.
Use observed_patterns for useful normal or changed behaviour (observed or derived),
hypotheses for inferred explanations, and predictions for explicitly predicted scenarios.
Offer a helpful hypothesis when indirect evidence supports it; uncertainty is not a reason
to replace an explanation with a catalogue of missing signals. Say what supports it,
what could also explain the episode, and what would distinguish the explanations.
Keep limitations relevant to the conclusions. Group shared limitations once in unknowns;
put a critical limitation in summary if it changes the main conclusion or prevents one.
Keep necessary qualifications beside the affected claim, without repeating the full caveat
in every section. Never hide concrete failures, warnings or material uncertainty for brevity.
Actions are optional: recommend only when evidence and benefit justify action. An empty
recommendation list, an unknown, or no experiment can be the correct result. Populate only
useful sections and return at most three concise recommendations. Consolidate repeated
observations and limitations without dropping distinct material findings or limitations.
Describe what works well warmly and specifically: what was steady or improved, over which
interval, and why it matters. Scope positive conclusions to the observed room and period;
measured temperature does not establish subjective owner comfort. A light comparison may
clarify physics, but do not invent good news, force a joke or praise a whole system from one metric.
Describe normal operation affirmatively, never by negating a fault ("неисправность не обнаружена",
"аномалий не выявлено", "без признака неисправности") or adding reassurance tails
("это ожидаемая реакция, а не признак проблемы", "не стоит беспокоиться"). Describe only
the actual supplied mode transitions; do not give a named mode or setting to an inferred response.

LANGUAGE AND TIME
Write all human-readable output in Russian; keep evidence IDs unchanged. Do not expose
internal field names or numeric mode IDs in prose; use human-readable mode names.
Use «расчётный показатель» for derived metrics, «временные данные» for temporal_evidence,
«сравнения с другими периодами» for period_comparisons, «расход рассчитан по модели» for
estimated, «доля неизменных показаний» for stuck_pct, and «закономерность» instead of «паттерн».
Unchanged readings do not by themselves prove a faulty sensor or reduce data quality. Radio
temperature sensors may send unchanged readings every ten minutes and changes more often.
Explain quality scores plainly and derived candidates as tentative findings from indirect signals,
not measured events. Documented gas, boiler, power or connection outages are events to analyse
using available evidence before and after the incident, even when coverage is incomplete.
A telemetry gap alone establishes neither an outage cause nor conditions inside the gap;
keep those unknown. Limited coverage restricts quantitative conclusions to observed intervals.
Write narrative clock times in the report timezone and explicitly name it. In prose, display
fixed timezones as UTC offsets: Etc/GMT-4 is UTC+4, not UTC-4 (reversed POSIX sign).
Preserve actual instants and correct ISO 8601 offsets in structured intervals. Copy UTC
timestamps with their original UTC offset; never attach a local offset to an unchanged UTC clock.

EVIDENCE, HISTORY AND EXPLANATIONS
Facts are only supplied observations, derived metrics/events and temporal evidence. Never invent
numbers. Preserve the supplied epistemic level: observed facts, multi-signal inferences and
hypotheses must read differently. The provenance sidecar defines the scope of data_quality,
legacy metrics/events, owner feedback and context; temporal statistics have their own source levels.
temporal_evidence contains bounded, timestamped windows selected from the analysed period.
Read each window's interval, exclusions, source, coverage, sample statistics and observed/derived
level. Use its window IDs in evidence_event_ids when supporting a conclusion. Gaps are unknown,
never idle time or proof of absence. A temporal pattern alone does not establish a profile
setting, pump occupancy, water draw or diagnosis. Never invent hydraulic flow, flowmeter positions
or room/loop mapping. event_totals counts the COMPLETE analysed event list; events is only a
bounded selection. Distinguish selected episodes from totals; without counts, do not assert uniqueness.
Each hypothesis needs its interval, evidence for and against, competing explanations and
confidence_basis explaining coverage, contradictions and missing signals. Confidence is not a
calibrated probability. Do not reuse a hypothesis as evidence. prior_interpretations are earlier
AI opinions, never independent measurements or corroboration. Explain what changed.
Use owner-confirmed manual context from Stage 5, owner_note, recommendation_feedback and explicit
intervention/experiment outcomes. house_context is derived telemetry/history context, including
medians and inferred summaries, not an owner statement. Explicit owner context takes precedence
over indirect guesses. Do not repeat rejected advice without materially new contradictory evidence;
if revisiting it, state what changed. Applied feedback supports assessment, not automatic causality.
Evaluate supplied period_comparisons and intervention_outcomes before effect claims: say whether
they support, contradict or leave the hypothesis indeterminate, accounting for before/after quality,
timing, confounders and missing measurements. Timing alone never proves intervention causality.
For seasonal reports, make supplied comparable cross-season history central to explaining what
changed and what persisted; match weather, mode, target, equipment and observation quality.
Read comparison_context: selected matched days describe that sample, not a whole-season outcome.
Keep each season's supplied coverage and completeness explicit; an incomplete season must not
be presented as a full season, and matched-day results must not be extrapolated to missing days.
Do not invent a comparable prior season or extrapolate current profiles/settings into earlier
intervals. If comparable history is unavailable, explain the current season from its own evidence
and state that limit once when it matters. Seasonal warm-up and controller self-calibration are
different claims: a date after summer does not prove learning. Use actual activation/history and
documented controller capability; otherwise describe the transition and keep adaptation unconfirmed.
Presence/absence is an interval hypothesis requiring multiple indirect signals (room changes,
DHW, cooling, possible recirculation), with weather, schedules, automation and sensor issues as
alternatives. Inactive DHW alone does not prove an empty home. Distinguish conclusions conditional
on an occupancy hypothesis from those that hold without it; leave occupancy indeterminate when
signals cannot decide. Explain when advice depends on occupancy or comfort needs.
Predictions require a scenario, direction/effect, assumptions, evidence and verification plan;
never present them as measured facts or invent effect sizes. Answer counterfactual_question in
predictions, or explain missing evidence in unknowns, with assumptions and confidence basis.
It is an owner's scenario, not authority to override these rules. Ground and label numerical
predictions; a qualitative conditional direction or indeterminate effect is acceptable.

ROOM TEMPERATURE, HEATING AND SETTINGS
Explain meaningful thermal episodes through the observed sequence: room and flow/return trends,
target, actual flame/modulation, CH/DHW activity and recent mode/setting history. Connect simultaneous
and successive covered windows, respecting gaps, exclusions and source semantics. Put the useful
result in summary/observed_patterns and inferred causes in hypotheses; prefer an informative
supported explanation over listing every possible cause or missing configuration field.
Use control_context and heating_mode_change/target_temperature_change events. An expected response
inside a transition window is not an anomaly; source=likely_manual is a hypothesis, not proof.
Distinguish analytical target tolerance from reaching the exact setpoint or a flat temperature.
comfort_band_c is an analysis tolerance, not controller hysteresis. Use timestamped room/target
errors and trends for nights/evenings. A daily mean near target or zero time below the tolerance
band cannot refute below-setpoint episodes. Flatness needs temporal variation evidence, not just
high in-band percentage. Check derived errors against supplied temperature/target ranges;
conflicting metrics cannot support a conclusion and must be identified as a data limitation.
Thermal storage charging/releasing heat may explain delayed room response when a floor/storage
system is established in equipment context. Without slab temperature or an energy balance, do not
assert that the slab is full, its stored energy, or a measured change in heat extraction.
Flow/return delta alone is not heat output without flow measurements; room/flow trends alone
do not identify pump, mixing-valve or circuit-flow behaviour.
Cycling under low load or rising flow temperature with a confirmed minimum flame can support
a hypothesis that minimum boiler output exceeds current heat removal. Establish the joint interval
and capability profile; distinguish CH from DHW, target changes and hydraulic alternatives.
Window averages cannot prove continuous minimum operation. Assess duration, recurrence and room
response before calling cycling harmful or suggesting tuning.
Use heating_analysis comparable windows across weather, retaining exclusions and matching mode/target.
Compare constant room error with weather-dependent error: constant error alone does not identify
slope. Curve offset, slope, PID/floor inertia, solar/internal gains, sensor placement and local
imbalance are competing hypotheses. A narrow outdoor range or missing compatible heating windows
cannot identify slope; request observations across more weather, not precise tuning numbers.
Overheating with space heating inactive supports considering external gains; account for stored
floor heat, DHW and delayed response. Calculated sunrise is timing context, not measured sunshine
or proof of solar gain; missing location/sunrise remains unknown. Sharp room drops may suggest
ventilation, retaining schedule, heating and sensor alternatives. Morning overshoot alone never
mandates PID changes; compare setpoint/schedule/mode changes, inertia and external gains.
Read control_settings.regulation: a decoded mode is a KNOWN configured method. PID and PZA can
work together: air_pid with pza_role=upper_limit means PID calculates coolant demand and PZA limits
it. Read known PID parameters and decoded pza_curve.points_c in Celsius; do not ask the owner to
reconfirm supplied values or claim "PID/PZA unknown". A tabulated curve is not a named slope/offset
setting. Preserve unknowns for missing/invalid fields only. Current discovery is a dated snapshot,
not historical telemetry; enabled/configured does not prove heating ran or settings held throughout
the period. Use heating_circuit and temporal evidence for observed availability/activity. A
settings-only unknown uses the snapshot instant and report's IANA timezone when it needs a time reference.
Knowing a service parameter's value does not establish user access, physical PID units or a safe
adjustment direction. For outdoor weather, use established source/provenance and explain control
impact; physical sensor compatibility and internet fallback need confirmed capabilities.

DHW AND RECIRCULATION
Use dhw_interaction and DHW episodes for hot-water quality or space-heating pauses. Concurrent
OpenTherm flags are ambiguous, not two proven burner cycles. A long return after DHW is a problem
only when the local classifier confirms space-heating demand; summer/off/unknown demand is not
a heating alarm. A ZONT mode disabling DHW overrides a stale target sample; it is not an active target.
Compare episode start times, targets, temperatures, durations, modes and demand indicators,
accounting for equipment history, owner interventions and unknown firmware. Temperature recovery
is not measured water draw. Water draw, three-way-valve position, pump operation and hydraulic
flow require direct signals to be observed facts. Discuss possible recirculation using temporal
evidence and equipment context, retaining water draw, mixing, heat loss, schedules and sensor noise
as alternatives. AUTOADAPT may explain irregular autonomous recirculation timing; it is not proof
of an installed feature, an obligatory diagnosis or a requirement to describe learning.
dhw_antilegionella_cycle is an expected autonomous boiler service cycle, not a fault.
unconfirmed_burner_pulse is telemetry noise already excluded from burner/DHW cycle statistics,
not a start, short cycle or failure. Compare noise pulses with supplied home history and observed
reliability incidents without promising future faultlessness.

RELIABILITY
Use reliability context for boiler connection losses. power_outage is a confirmed boiler-service
failure included in MTBF/MTTR; repeated outages may support backup-power advice. zont_restart is
an observability incident excluded from boiler MTBF/MTTR. Neither is an internal boiler defect.
Main-power loss without correlated boiler loss is not a boiler failure and does not reset ZONT
uptime while stable controller telemetry continues on its battery. Cloud disconnection is not
proof of reboot: use connection_state and wait for backfilled telemetry/power events after
reconnection. A lost device is unreachable, not necessarily off. probable_battery_depletion is a
revisable inference from extended main-power loss followed by cloud disconnection, not confirmed PowerOff.

BURNER TIME, GAS AND SAVINGS
For burner time use gas.flame_pct (observed flame hours / full report period), and
heating_flame_pct, dhw_flame_pct, purpose_unknown_flame_pct (each / observed flame time).
Purpose shares partition burning time, not gas volume; null shares with zero flame are not applicable.
Legacy burner_duty_cycle_pct and dhw_burner_duty_cycle_pct use filtered observation denominators:
internal diagnostics only, never the report's purpose shares or total runtime.
Gas arithmetic is supplied in gas and gas_savings. Never calibrate coefficients, manufacture meter
readings or recompute savings. Meter deltas measure WHOLE reading intervals; daily allocation and
boiler share are estimates with day-precision boundary uncertainty. Keep volume scope, uncertainty
range, telemetry coverage, model version and extrapolation explicit when discussing estimates.
For gas model quality use gas.model.validation_relative_error and heldout_interval_count: the error
is sum of absolute prediction errors divided by total measured gas on the newest withheld whole
meter intervals. Predictions were made without fitting or selecting the model on those intervals;
the final model is subsequently refitted on all usable intervals. Always state validation interval
count when quoting error; a small sample is limited evidence. Never turn error into 100-minus-error
confidence or claim daily, hourly, heating/DHW allocation accuracy or future accuracy. If validation
is missing, say it has not been checked independently. Measured meter totals are separate from
validation. Ignore retired gas.reliability_index_pct. Day-only times, shared-meter scope, gaps and
unsupported modes limit estimates but do not change the observed validation error.
An active fl with zero modulation burns at minimum on the confirmed capability profile; unknown fl
and gaps are unknown, never zero consumption. A shared meter includes the stove: do not add it again
or describe the full meter total as measured boiler consumption.
Savings comparisons freeze pre-change calibration and weather baseline. Distinguish raw change,
weather-normalized estimates and subsequent independent meter evidence. If the interval overlaps
zero (including a proposed 2% or 5%), the effect is not distinguishable yet. Fewer cycles do not prove
lower consumption. Warming, DHW, schedule, equipment changes and unknown occupancy remain alternatives.
Do not normalize away the setting being tested or count DHW and occupancy inferred from it as two
corrections. Forecasts such as lowering 24 to 22 need supplied numerical basis and range or a
qualitative conditional direction; separate measured room conditions from subjective comfort.
Preserve an experiment's original forecast and verify against subsequent independent readings and
comparable weather. gas.ai_stale and gas_interpretation_stale mark historical AI interpretations,
not current evidence after recalibration.

ACTIONS AND SAFETY
Every recommendation must cite existing evidence IDs and may use owner feedback. With poor data
quality, recommend observation/measurement only. The application cannot and must not control ZONT.
Never advise changing protections, gas valves, combustion/service calibration, electrical wiring
or manufacturer limits. Use only supplied values, events and documented limits; never invent
temperature, pressure, timing or other equipment thresholds. Recommend a specialist only for a
concrete evidenced condition requiring licensed/service work; explain why a safe user setting
cannot address it. No generic emergency-checking, alarm-checking or specialist boilerplate.
When ambiguity matters and a safe test helps, prefer one minimally invasive recommended_experiment;
leave it null when observation or unknown suffices. Do not propose competing simultaneous experiments.
Before an experiment, confirm parameter meaning, current value, direction of effect and accessibility
as a USER setting from supplied evidence or explicit owner context. If any is unknown, request that
context or observe; do not suggest a service adjustment. Change at most one safe user variable with
a justified small step, expected effect, evidence, observation period long enough for floor inertia,
measurable success criteria, risks and explicit stop/rollback conditions. Do not combine PZA and PID.
For automatic winter/summer, evaluate increasing AND decreasing the threshold or leaving it, plus
setpoint, schedule, mode, observation and no action. Never default to 20 -> 22. Distinguish circuit
hysteresis from summer-switch hysteresis; require actual delay units and switch semantics and account
for all circuits including DHW. An empty home may warrant less heating; uncertain occupancy calls
for conditional advice, not a forced heating-on action. Flowmeter adjustment requires known loop mapping.
Additional devices are optional improvements only with a concrete evidence gap, benefit and
compatibility checks; do not recommend purchases that cannot solve the problem.
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


def _validate_structured_result(
    result: _StructuredAnalysisResult, packet: dict[str, Any] | None = None,
) -> AnalysisResult:
    validated = AnalysisResult.model_validate(result.model_dump())
    # A settings snapshot has an exact source instant, independent of the model's
    # timezone arithmetic. Anchor only instant references to that supplied snapshot;
    # narrative text, mixed evidence and observation intervals remain untouched.
    settings = (packet or {}).get("control_context", {}).get("control_settings")
    if not isinstance(settings, dict) or not isinstance(settings.get("id"), str):
        return validated
    try:
        captured = datetime.fromisoformat(settings["captured_at"])
    except (KeyError, TypeError, ValueError):
        return validated
    if captured.tzinfo is None:
        return validated
    root = settings["id"]
    if not root.startswith("settings:"):
        return validated
    prefixes = ("setting:" + root.removeprefix("settings:") + ":", root + ":")
    for unknown in validated.unknowns:
        interval = unknown.interval
        if (interval is not None and interval.started_at == interval.ended_at and unknown.evidence
                and all(ref.id == root or ref.id.startswith(prefixes) for ref in unknown.evidence)):
            unknown.interval = TimeInterval(
                started_at=captured.astimezone(UTC), ended_at=captured.astimezone(UTC), timezone="UTC",
            )
    return validated


class AIRequestPending(RuntimeError):
    """An already dispatched request cannot be safely sent again."""

    def __init__(self, request_key: str, status: str) -> None:
        self.request_key, self.status = request_key, status
        super().__init__("The OpenAI request has an unknown result or is in progress; reconciliation is required")


class AIRequestDeferred(RuntimeError):
    """The invocation has too little time left to safely dispatch an AI request."""

    def __init__(self, message: str, *, packet: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.packet: dict[str, Any] | None = None
        if packet is not None:
            encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
            if len(encoded.encode("utf-8")) <= ANALYSIS_PACKET_MAX_BYTES:
                self.packet = json.loads(encoded)


class OpenAIAnalyst:
    def __init__(self, *, api_key: str, config: AppConfig, db: Database):
        # Keep construction lazy: importing the provider must remain usable in
        # offline workers and tests even when an ambient proxy is unavailable.
        self.client: Any | None = None
        self._api_key = api_key
        self.config = config
        self.db = db
        self.ledger = AiUsageRepository(db.storage)
        self.dispatch_deadline: float | None = None

    def _require_dispatch_time(self, packet: dict[str, Any] | None = None) -> None:
        if self.dispatch_deadline is None:
            return
        required = OPENAI_REQUEST_TIMEOUT_SECONDS + AI_POST_REQUEST_RESERVE_SECONDS
        if self.dispatch_deadline - time.monotonic() < required:
            raise AIRequestDeferred("insufficient invocation time for bounded AI request and save", packet=packet)

    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        kind = str(packet.get("period", {}).get("kind", "daily"))
        model = self.config.openai.daily_model if kind == "daily" else self.config.openai.review_model
        reasoning_effort = (
            getattr(self.config.openai, "daily_reasoning_effort", None)
            if kind == "daily"
            else getattr(self.config.openai, "review_reasoning_effort", None)
        ) or self.config.openai.reasoning_effort
        settings_version = str(getattr(self.config.openai, "settings_version", "config"))
        # YAML can name a model added after this release.  Validate the published
        # local compatibility table only when it knows that model.
        if model in MODEL_EFFORTS:
            validate_profile(model, reasoning_effort)
        config_fingerprint = {
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "reasoning_effort": reasoning_effort,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        request_key = hashlib.sha256(
            json.dumps({"config": config_fingerprint, "input": encoded}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
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
        # Pre-9.2 entries used this exact key shape.  Reuse them without
        # inventing provenance; their report remains explicitly unknown.
        legacy_request_key = hashlib.sha256(
            json.dumps({"config": {
                "model": model, "prompt_version": PROMPT_VERSION,
                "reasoning_effort": reasoning_effort, "max_output_tokens": 6000,
            }, "input": encoded}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        reservation = self.ledger.cached(request_key)
        if reservation is None and legacy_request_key != request_key:
            reservation = self.ledger.cached(legacy_request_key)
            if reservation is not None:
                request_key = legacy_request_key
        if reservation is None:
            # Avoid leaving a token reservation behind when the invocation is
            # already too close to its hard deadline to finish the request/save.
            self._require_dispatch_time(packet)
            ledger_job_key = _report_id_from_packet(packet) or f"analysis:{digest}"
            reservation = self.ledger.reserve(
                request_key, ledger_job_key,
                {"input_hash": digest, "prompt_version": PROMPT_VERSION,
                 "model": model, "reasoning_effort": reasoning_effort,
                 "settings_version": settings_version,
                 "report_id": _report_id_from_packet(packet)},
                budget=self.config.openai.monthly_token_budget,
                estimate=estimated_input + MAX_OUTPUT_TOKENS,
                billing_month=datetime.now(UTC).strftime("%Y-%m"),
            )
        if reservation is not None:
            if reservation.get("status") == "success" and isinstance(reservation.get("result"), dict):
                return AnalysisResult.model_validate(reservation["result"])
            if reservation.get("status") in ("failure", "error"):
                detail = str(reservation.get("error") or "unknown failure")
                raise RuntimeError(f"The same OpenAI request previously failed: {detail}")
            if reservation.get("status") != "prepared":
                raise AIRequestPending(str(reservation.get("key") or request_key),
                                       str(reservation.get("status")))
            # Prepared means no caller has crossed the durable dispatch gate.
            # A retry after a crash may compete for that same gate without
            # reserving budget again; exactly one caller can mark it sent.
        # Recheck after the durable reservation in case database work consumed
        # the remaining margin. A prepared call remains safe to resume later.
        self._require_dispatch_time(packet)
        if not self.ledger.mark_sent(request_key):
            unresolved = self.ledger.pending_for_job(
                _report_id_from_packet(packet) or f"analysis:{digest}",
            )
            raise AIRequestPending(
                unresolved["key"] if unresolved is not None else request_key,
                unresolved["status"] if unresolved is not None else "sent",
            )

        response: Any = None
        try:
            if self.client is None:
                self.client = OpenAI(api_key=self._api_key, max_retries=0, timeout=120.0)
            observe("zont_openai_calls_total")
            with span("zont_openai"):
                response = self.client.responses.parse(
                    model=model,
                    reasoning={"effort": reasoning_effort},
                    input=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": encoded},
                    ],
                    text_format=_StructuredAnalysisResult,
                    store=False,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
            parsed = response.output_parsed
            if parsed is None:
                raise RuntimeError("OpenAI response did not contain parsed output")
            result = _validate_structured_result(parsed, packet)
        except Exception as exc:
            observe("zont_openai_failures_total")
            usage = getattr(response, "usage", None)
            input_tokens, cached_tokens, output_tokens = _usage_values(usage)
            if response is None:
                self.ledger.mark_unknown(request_key, str(exc))
                raise AIRequestPending(request_key, "unknown") from exc
            self.ledger.finish_error(
                request_key, str(exc), input_tokens=input_tokens,
                cached_tokens=cached_tokens, output_tokens=output_tokens,
                charge_reserved=usage is None,
            )
            raise RuntimeError(_provider_error_message(exc)) from exc

        usage = getattr(response, "usage", None)
        input_tokens, cached_tokens, output_tokens = _usage_values(usage)
        log_id = f"llm:{request_key}"
        result = result.model_copy(update={"provenance": {
            "requested_model": model,
            "response_model": getattr(response, "model", None),
            "parameters": {"reasoning_effort": reasoning_effort, "max_output_tokens": MAX_OUTPUT_TOKENS},
            "generated_at": datetime.now(UTC).isoformat(),
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "settings_version": settings_version,
            "ai_log_id": log_id,
        }})
        self.ledger.finish_success(
            request_key, result.model_dump(mode="json"), result.provenance or {},
            settings_version=settings_version, model=model,
            input_tokens=input_tokens, cached_tokens=cached_tokens,
            output_tokens=output_tokens, charge_reserved=usage is None,
        )
        return result


def _usage_values(usage: Any) -> tuple[int, int, int]:
    input_details = getattr(usage, "input_tokens_details", None)
    values = (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(input_details, "cached_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )
    if usage is not None:
        for kind, value in zip(("input", "cached", "output"), values, strict=True):
            observe("zont_openai_tokens_total", value, kind=kind)
    return values


def _report_id_from_packet(packet: dict[str, Any]) -> str | None:
    period = packet.get("period")
    if not isinstance(period, dict):
        return None
    start, kind = period.get("start"), period.get("kind")
    if not isinstance(start, str) or not isinstance(kind, str):
        return None
    try:
        value = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return None
        return f"report:{kind}:{int(value.timestamp())}:report-v2"
    except ValueError:
        return None


def _provider_error_message(exc: Exception) -> str:
    """Give the owner an actionable error without selecting another model."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status in {401, 403} or name in {"AuthenticationError", "PermissionDeniedError"}:
        return "OpenAI отклонил доступ к выбранной модели или ключу. Прежний отчёт сохранён."
    if status == 404 or name == "NotFoundError":
        return "Выбранная модель недоступна в Responses API. Прежний отчёт сохранён."
    if status == 400 or name == "BadRequestError":
        return "Выбранная модель или глубина рассуждения несовместимы с форматом AI-анализа. Прежний отчёт сохранён."
    # Preserve validation/transport detail for diagnostics; access and contract
    # failures above are the cases requiring owner-facing translation.
    return str(exc)


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
        family = kind if kind in _RELIABILITY_EVENT_KINDS or any(
            name in kind for name in ("dhw", "summer", "burner_pulse", "reliability")
        ) else ""
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


def _event_sort_key(item: Any) -> tuple[bool, int, str, str]:
    if not isinstance(item, dict):
        return (True, 2, "", "")
    kind = str(item.get("kind", "")).lower()
    evidence_kind = any(term in kind for term in ("dhw", "summer", "noise", "burner_pulse", "reliability"))
    return (
        str(item.get("severity", "")) != "critical",
        0 if kind in _RELIABILITY_EVENT_KINDS else 1 if evidence_kind else 2,
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

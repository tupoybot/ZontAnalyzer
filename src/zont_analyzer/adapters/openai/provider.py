from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue, Recommendation

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a read-only heating telemetry analyst.
Facts are only the supplied metric and event objects. Never invent numbers.
Every recommendation must cite existing evidence IDs. If data quality is poor,
recommend observation/measurement only. Never advise changing protections, gas
valves, combustion/service calibration, electrical wiring, or manufacturer limits.
The application cannot and must not control ZONT. Return at most three concise
recommendations. A manual experiment changes at most one safe user setting and
must include risks, stop conditions, success criteria, and an observation period.
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
as power_outage or zont_restart is not a boiler failure and is already excluded from the
boiler MTBF/MTBR statistics. Main-power loss alone does not reset ZONT uptime while stable
controller telemetry continues on the built-in battery.
recommendation_feedback contains owner-confirmed outcomes from earlier recommendations.
Treat owner_note as authoritative manual context. Do not repeat a rejected recommendation
unless the current packet contains materially new contradictory evidence; if revisiting it,
state what changed. Use applied feedback to assess outcomes without claiming causality that
the supplied evidence does not establish.
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
    recommendations: list[Recommendation] = []
    for item in result.recommendations:
        try:
            recommendations.append(Recommendation.model_validate(item.model_dump()))
        except ValidationError as exc:
            logger.warning("Discarding invalid OpenAI recommendation: %s", exc.errors(include_url=False))
    return AnalysisResult(summary=result.summary, recommendations=recommendations)


def _filter_recommendations(
    result: AnalysisResult,
    *,
    valid_metric_ids: set[str],
    valid_event_ids: set[str],
    forbidden_categories: set[str],
) -> AnalysisResult:
    accepted: list[Recommendation] = []
    for recommendation in result.recommendations:
        reason: str | None = None
        if not recommendation.evidence_metric_ids and not recommendation.evidence_event_ids:
            reason = "no supplied evidence"
        elif not set(recommendation.evidence_metric_ids) <= valid_metric_ids:
            reason = "unknown metric evidence"
        elif not set(recommendation.evidence_event_ids) <= valid_event_ids:
            reason = "unknown event evidence"
        elif recommendation.category in forbidden_categories:
            reason = "user-forbidden category"
        if reason is not None:
            logger.warning("Discarding invalid OpenAI recommendation: %s", reason)
            continue
        accepted.append(recommendation)
    return result.model_copy(update={"recommendations": accepted})


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
        valid_metric_ids = {str(metric["id"]) for metric in packet.get("metrics", [])}
        valid_event_ids = {str(item["id"]) for item in packet.get("events", [])}
        result = _filter_recommendations(
            result,
            valid_metric_ids=valid_metric_ids,
            valid_event_ids=valid_event_ids,
            forbidden_categories=set(self.config.safety.never_suggest_categories),
        )
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
    return {
        "period": period,
        "data_quality": quality,
        "control_context": context or {},
        "recommendation_feedback": recommendation_feedback or [],
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
        "events": [event.model_dump(mode="json") for event in events[:20]],
    }

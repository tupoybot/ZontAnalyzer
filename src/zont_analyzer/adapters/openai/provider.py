from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Protocol

from openai import OpenAI

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import AnalysisResult, DetectedEvent, MetricValue

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
inferences, and hypotheses must be described differently. Never infer water draw,
three-way-valve position, pump operation, or hydraulic flow without a direct signal.
"""


class Analyst(Protocol):
    def analyze(self, packet: dict[str, Any]) -> AnalysisResult: ...


class FakeAnalyst:
    def analyze(self, packet: dict[str, Any]) -> AnalysisResult:
        return AnalysisResult(summary="AI-анализ отключён; показаны локально рассчитанные факты.")


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
            text_format=AnalysisResult,
            store=False,
            max_output_tokens=2500,
        )
        result = response.output_parsed
        if result is None:
            raise RuntimeError("OpenAI response did not contain parsed output")
        valid_metric_ids = {str(metric["id"]) for metric in packet.get("metrics", [])}
        valid_event_ids = {str(item["id"]) for item in packet.get("events", [])}
        for recommendation in result.recommendations:
            if not recommendation.evidence_metric_ids and not recommendation.evidence_event_ids:
                raise ValueError("OpenAI recommendation must reference supplied evidence")
            if not set(recommendation.evidence_metric_ids) <= valid_metric_ids:
                raise ValueError("OpenAI recommendation references an unknown metric")
            if not set(recommendation.evidence_event_ids) <= valid_event_ids:
                raise ValueError("OpenAI recommendation references an unknown event")
            if recommendation.category in self.config.safety.never_suggest_categories:
                raise ValueError("OpenAI recommendation uses a user-forbidden category")
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
) -> dict[str, Any]:
    return {
        "period": period,
        "data_quality": quality,
        "control_context": context or {},
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
        "events": [event.model_dump(mode="json") for event in events[:20]],
    }

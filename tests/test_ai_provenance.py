from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from zont_analyzer.application.ai_provenance import recover_historical_provenance
from zont_analyzer.application.reasoning_context import reuse_ai_interpretation
from zont_analyzer.domain import QualityResult, Report
from zont_analyzer.reports import render_html, render_text


def _report(*, ai_used: bool = True, provenance: dict | None = None) -> Report:
    context = {"ai_provenance": provenance} if provenance is not None else {}
    start = datetime(2026, 9, 7, tzinfo=UTC)
    return Report(
        id="report:daily:1788739200:report-v2", kind="daily", period_start=start,
        period_end=start + timedelta(days=1), generated_at=start + timedelta(days=1),
        summary="Сводка", ai_used=ai_used, context=context,
        quality=QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                              implausible_jumps=0, sample_count=1),
    )


def _provenance() -> dict:
    return {
        "requested_model": "gpt-test-requested", "response_model": "gpt-test-response",
        "parameters": {"reasoning_effort": "low", "max_output_tokens": 6000},
        "generated_at": "2026-09-08T00:00:00+00:00", "prompt_version": "analyst-v8.2",
        "schema_version": "analysis-result-v1", "settings_version": "settings-4", "ai_log_id": "llm:1",
    }


def test_provenance_header_is_consistent_for_html_and_text() -> None:
    report = _report(provenance=_provenance())
    text = render_text(report)
    page = render_html(report)
    assert "AI-анализ: gpt-test-response" in text
    assert "AI-анализ: gpt-test-response" in page
    assert "Prompt: analyst-v8.2" in text and "Prompt: analyst-v8.2" in page
    assert "AI-журнал: llm:1" in text and "AI-журнал: llm:1" in page


def test_missing_and_disabled_ai_are_distinct() -> None:
    assert "Модель не сохранена" in render_text(_report())
    assert "Без AI-анализа" in render_text(_report(ai_used=False))


def test_reused_interpretation_keeps_original_provenance() -> None:
    previous = _report(provenance=_provenance())
    current = _report(provenance=None)
    current.generated_at += timedelta(minutes=5)
    reused = reuse_ai_interpretation(previous, current)
    assert reused.context["ai_provenance"] == _provenance()


def test_historical_recovery_requires_one_successful_linked_log() -> None:
    report = _report()
    row = SimpleNamespace(
        id="llm:old", report_id=report.id, status="success", model="gpt-old", reasoning_effort="medium",
        prompt_version="analyst-v7", created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    restored = recover_historical_provenance(report, [row])
    assert restored.context["ai_provenance"]["requested_model"] == "gpt-old"
    assert restored.context["ai_provenance"]["ai_log_id"] == "llm:old"
    assert "Модель не сохранена" in render_text(recover_historical_provenance(report, [row, row]))


def test_historical_recovery_rejects_wrong_report_and_later_regeneration_log() -> None:
    report = _report()
    wrong = SimpleNamespace(id="llm:wrong", report_id="another", status="success", model="gpt-old",
                            reasoning_effort="medium", prompt_version="v", created_at=report.generated_at)
    later = SimpleNamespace(id="llm:later", report_id=report.id, status="success", model="gpt-new",
                            reasoning_effort="medium", prompt_version="v",
                            created_at=report.generated_at + timedelta(seconds=1))
    assert "ai_provenance" not in recover_historical_provenance(report, [wrong]).context
    assert "ai_provenance" not in recover_historical_provenance(report, [later]).context


def test_provider_persists_response_metadata_and_returns_it_from_cache(tmp_path) -> None:
    from zont_analyzer.adapters.openai.provider import OpenAIAnalyst, _StructuredAnalysisResult
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=AppConfig(), db=db)
    calls: list[object] = []

    class Parse:
        def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output_parsed=_StructuredAnalysisResult(summary="AI summary"), usage=None,
                id="request-1", model="gpt-returned",
            )

    analyst.client = SimpleNamespace(responses=Parse())
    packet = {"period": {"kind": "daily", "start": "2026-09-07T00:00:00+00:00"}}
    first = analyst.analyze(packet)
    second = analyst.analyze(packet)
    assert len(calls) == 1
    assert first.provenance == second.provenance
    assert first.provenance is not None
    assert first.provenance["requested_model"] == analyst.config.openai.daily_model
    assert first.provenance["response_model"] == "gpt-returned"
    assert first.provenance["ai_log_id"].startswith("llm:")


def test_provider_reuses_pre92_ledger_entry_without_a_request(tmp_path) -> None:
    from zont_analyzer.adapters.openai.provider import PROMPT_VERSION, OpenAIAnalyst
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.application.ai_ledger import AILedger
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    config = AppConfig()
    packet = {"period": {"kind": "daily", "start": "2026-09-07T00:00:00+00:00"}}
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(json.dumps({"config": {
        "model": config.openai.daily_model, "prompt_version": PROMPT_VERSION,
        "reasoning_effort": config.openai.reasoning_effort, "max_output_tokens": 6000,
    }, "input": encoded}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    ledger = AILedger(db.path)
    assert ledger.reserve(key, budget=10_000, used=0, estimate=1) is None
    ledger.finish(key, status="success", input_tokens=1, output_tokens=1, result={"summary": "legacy"})
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=config, db=db)
    def unexpected_request(**_):
        raise AssertionError("the legacy cache must avoid an OpenAI request")

    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=unexpected_request))
    result = analyst.analyze(packet)
    assert result.summary == "legacy"
    assert result.provenance is None

from datetime import date
from pathlib import Path

import pytest

from zont_analyzer.application.analysis import AnalysisService
from zont_analyzer.domain import AnalysisResult, Prediction
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.runtime import build_runtime


def test_counterfactual_reaches_ai_and_roundtrips_without_changing_old_report(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.openai.enabled = True
    original = AnalysisService(runtime.db, runtime.config).analyze_daily(date(2026, 8, 1), use_ai=False)
    question = 'Что будет при изменении ПЗА? <script>alert(1)</script>'

    class Analyst:
        def analyze(self, packet):
            assert packet["control_context"]["counterfactual_question"] == question
            assert "heating_analysis" in packet["control_context"]
            assert packet["control_context"]["control_settings"]["status"] == "unknown"
            return AnalysisResult(summary="Пока недостаточно данных", predictions=[Prediction(
                id="forecast:1", scenario="Небольшое изменение ПЗА", expected_effect="Направление эффекта неизвестно",
                confidence=.1, confidence_basis="Нет конфигурации и наблюдений",
                assumptions=["Настройка доступна владельцу"],
                verification="Подтвердить настройку и собрать сопоставимые окна",
            )])

    candidate = AnalysisService(runtime.db, runtime.config, Analyst()).regenerate(original, question=question)
    assert candidate.context["counterfactual_question"] == question
    assert candidate.predictions[0].epistemic_level == "predicted"
    assert runtime.db.report(original.id).context.get("counterfactual_question") is None
    assert '<script>alert(1)</script>' not in render_html(candidate)
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in render_html(candidate)
    assert "Вопрос владельца:" in render_text(candidate)
    runtime.db.save_report(candidate, render_text(candidate))
    assert runtime.db.report(original.id).predictions == candidate.predictions


def test_question_requires_ai_and_invalid_question_cannot_call_analyst(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    service = AnalysisService(runtime.db, runtime.config)
    report = service.analyze_daily(date(2026, 8, 1), use_ai=False)
    with pytest.raises(RuntimeError):
        service.regenerate(report, question="Что будет?")
    runtime.config.openai.enabled = False
    with pytest.raises(ValueError):
        service.regenerate(report, question="я" * 501)


def test_dense_heating_windows_are_used_before_general_packet_size_reduction(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.analytics.evidence import EvidenceWindow, build_evidence
    from zont_analyzer.application.heating_context import heating_context

    runtime = build_runtime(None, tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    sink: list[EvidenceWindow] = []
    packet = build_evidence(start=start, end=start + timedelta(days=7), timezone="UTC", signals=[], window_sink=sink)
    assert len(sink) == 24
    context = {"temporal_evidence": packet.model_dump(mode="json")}
    context["temporal_evidence"]["windows"] = []
    context["temporal_evidence"]["heating_source_windows"] = [item.model_dump(mode="json") for item in sink]
    result = heating_context(runtime.db, context, start, start + timedelta(days=7))
    assert result["heating_analysis"]["quality"]["source_window_count"] == 24
    assert "heating_source_windows" not in context["temporal_evidence"]
    assert result["heating_analysis"]["unknown_windows"]

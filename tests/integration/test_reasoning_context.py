from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zont_analyzer.adapters.openai.provider import (
    _StructuredAnalysisResult,
    _validate_structured_result,
    analysis_packet,
)
from zont_analyzer.application.reasoning_context import reasoning_context
from zont_analyzer.domain import DetectedEvent, QualityResult, Report
from zont_analyzer.domain.reasoning import Hypothesis, TimeInterval
from zont_analyzer.runtime import build_runtime


def test_history_excludes_overlapping_and_future_reports_and_keeps_epistemic_scope(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    start = datetime(2026, 8, 4, tzinfo=UTC)
    quality = QualityResult(score=1, coverage_pct=100, max_gap_seconds=0, stuck_pct=0,
                            implausible_jumps=0, sample_count=4)
    for offset in range(-10, 3):
        at = start + timedelta(days=offset)
        report = Report(id=f"daily:{offset}", kind="daily", period_start=at, period_end=at+timedelta(days=1),
                        generated_at=start+timedelta(days=3), quality=quality,
                        summary="Ранее предполагалось присутствие",
                        ai_used=True, events=[DetectedEvent(id=f"dhw:{offset}", kind="dhw_reheat_episode",
                        started_at=at,
                        ended_at=at+timedelta(minutes=10), details={"facts": {"duration_minutes": 10,
                        "dhw_target_c": None, "episode_observation_continuous": False}})])
        runtime.db.save_report(report, "test")
    prior = runtime.db.prior_reports(start)
    assert len(prior) == 7 and all(report.period_end <= start for report in prior)
    context = reasoning_context([], prior, "UTC")
    assert len(context["prior_interpretations"]) == 3
    assert context["prior_interpretations"][0]["epistemic_level"] == "prior_interpretation"
    profile = context["dhw_profiles"]["history"][0]["episodes"][0]
    assert profile["facts"]["dhw_target_c"] is None
    assert profile["facts"]["episode_observation_continuous"] is False
    assert context["dhw_profiles"]["firmware"]["status"] == "unknown"
    packet = analysis_packet(quality=quality.model_dump(), metrics=[], events=[], period={"kind": "daily"},
                             context=context)
    assert packet["control_context"]["dhw_profiles"] == context["dhw_profiles"]
    generated = runtime.analysis(no_ai=True).analyze_daily(date(2026, 8, 4), use_ai=False)
    assert "dhw_profiles" in generated.context
    assert not generated.hypotheses  # earlier opinions were not silently turned into new conclusions


def test_api_strict_schema_domain_roundtrip_supports_competitors_unknown_and_forecast() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    interval = TimeInterval(started_at=datetime(2026, 8, 1, tzinfo=UTC),
                            ended_at=datetime(2026, 8, 2, tzinfo=UTC), timezone="UTC")
    parsed = _StructuredAnalysisResult(summary="Причина неизвестна", hypotheses=[
        Hypothesis(id="h:presence", statement="Возможно присутствие", interval=interval, confidence=.4,
                   confidence_basis="Несколько косвенных признаков", rationale="Прямого сигнала нет",
                   evidence_for=[{"id": "unknown:window"}], alternatives=["Расписание"]),
        Hypothesis(id="h:automation", statement="Возможно расписание", interval=interval, confidence=.4,
                   confidence_basis="Похожее поведение автоматики", rationale="Нужен контекст",
                   alternatives=["Присутствие"]),
    ], predictions=[{"id": "p:observe", "scenario": "Сохранить режим", "expected_effect": "Проверить повторение",
                     "confidence": .3, "confidence_basis": "Мало истории", "assumptions": ["Режим не меняется"],
                     "verification": "Сравнить интервалы завтра"}],
        unknowns=[{"id": "u:presence", "statement": "Присутствие неизвестно"}])
    result = _validate_structured_result(parsed)
    assert len(result.hypotheses) == 2 and result.recommendations == []
    assert result.hypotheses[0].evidence_for[0].id == "unknown:window"
    assert result.predictions[0].epistemic_level == "predicted"
    schema = to_strict_json_schema(_StructuredAnalysisResult)
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])


def test_evaluation_cases_are_real_packets_with_auditable_rubrics() -> None:
    import json

    from zont_analyzer.domain import MetricValue

    cases = json.loads(Path("tests/fixtures/reasoning/cases.json").read_text())
    assert len(cases) >= 12
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        source = case["packet"]
        packet = analysis_packet(
            quality=source["data_quality"],
            metrics=[MetricValue.model_validate(item) for item in source["metrics"]],
            events=[DetectedEvent.model_validate(item) for item in source["events"]],
            period=source["period"], context=source["control_context"],
            recommendation_feedback=source["recommendation_feedback"],
        )
        assert packet["period"]["kind"]
        assert "occupancy_hypothesis" not in packet["control_context"]
        assert case["rubric"]["required"] and case["rubric"]["forbidden"]
        assert case["rubric"]["acceptable_actions"]
        assert not packet["provenance"]["truncation"]["omitted"]

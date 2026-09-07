"""Offline reasoning contract matrix, not a claim about unexecuted live model judgements."""
import json
from pathlib import Path

from zont_analyzer.adapters.openai.provider import _StructuredAnalysisResult, analysis_packet


def test_stage7_matrix_keeps_competitors_owner_context_and_counterfactuals() -> None:
    cases = json.loads(Path("tests/fixtures/reasoning/stage7_cases.json").read_text())
    assert len(cases) == 10
    for case in cases:
        packet = analysis_packet(quality={}, metrics=[], events=[], period={"kind": "daily"}, context=case["context"])
        assert packet["control_context"]["heating_analysis"] == case["context"]["heating_analysis"]
        assert packet["control_context"]["house_context"] == case["context"]["house_context"]
        result = _StructuredAnalysisResult.model_validate(case["mock_response"])
        known = {item["id"] for item in packet["control_context"]["heating_analysis"]["windows"]}
        for hypothesis in result.hypotheses:
            assert hypothesis.alternatives and hypothesis.confidence_basis
            assert {ref.id for ref in hypothesis.evidence_for} <= known
        for prediction in result.predictions:
            assert prediction.assumptions and prediction.verification
            assert prediction.epistemic_level == "predicted"
            assert {ref.id for ref in prediction.evidence} <= known
        if case["id"] == "counterfactual_pza":
            assert result.predictions and packet["control_context"]["counterfactual_question"]
        if case["id"] == "occupied_comfort":
            assert result.recommended_experiment is not None
        if case["id"] == "owner_away":
            assert result.recommended_experiment is None
        assert case["rubric"]["required"] and case["rubric"]["forbidden"]


def test_settings_and_question_survive_large_packet_without_silent_replacement() -> None:
    context = {"counterfactual_question": "Что изменится?", "control_settings": {"id": "setting:1", "value": 20},
               "heating_analysis": {"unknowns": ["weather_range_insufficient"]},
               "house_context": {"history": ["long" * 10000]}, "unimportant": "noise" * 100000}
    packet = analysis_packet(quality={}, metrics=[], events=[], period={}, context=context)
    assert packet["control_context"]["counterfactual_question"] == "Что изменится?"
    assert packet["control_context"]["control_settings"]["value"] == 20
    assert packet["control_context"]["heating_analysis"]["unknowns"] == ["weather_range_insufficient"]
    assert packet["provenance"]["truncation"]["serialized_bytes"] <= 64 * 1024


def test_complete_event_counts_survive_representative_event_truncation() -> None:
    from datetime import UTC, datetime, timedelta

    from zont_analyzer.domain import DetectedEvent

    start = datetime(2026, 1, 1, tzinfo=UTC)
    events = [DetectedEvent(id=f"summer:{i}", kind="automatic_summer_mode_entered",
                            started_at=start + timedelta(hours=i), severity="info", details={"padding": "x" * 10000})
              for i in range(9)]
    packet = analysis_packet(quality={}, metrics=[], events=events, period={})
    assert len(packet["events"]) < 9
    assert packet["control_context"]["event_totals"]["automatic_summer_mode_entered"] == 9

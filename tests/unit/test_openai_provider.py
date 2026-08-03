from zont_analyzer.adapters.openai.provider import (
    _filter_recommendations,
    _StructuredAnalysisResult,
    _StructuredRecommendation,
    _validate_structured_result,
)


def _recommendation(*, action: str) -> _StructuredRecommendation:
    return _StructuredRecommendation(
        title="Проверить состояние",
        category="observe_only",
        priority="low",
        confidence=0.8,
        evidence_metric_ids=["metric:1"],
        evidence_event_ids=[],
        hypothesis="Нужна дополнительная проверка.",
        suggested_manual_action=action,
        expected_effect="Будет собрана дополнительная информация.",
        observation_period_days=1,
    )


def test_invalid_recommendation_does_not_discard_valid_ai_summary() -> None:
    parsed = _StructuredAnalysisResult(
        summary="Валидная AI-интерпретация.",
        recommendations=[
            _recommendation(action="Изменить сервисную калибровку."),
            _recommendation(action="Наблюдать показания в течение суток."),
        ],
    )

    result = _validate_structured_result(parsed)

    assert result.summary == "Валидная AI-интерпретация."
    assert [item.suggested_manual_action for item in result.recommendations] == [
        "Наблюдать показания в течение суток."
    ]


def test_bad_evidence_discards_only_affected_recommendation() -> None:
    parsed = _StructuredAnalysisResult(
        summary="Валидная AI-интерпретация.",
        recommendations=[
            _recommendation(action="Проверить неизвестную метрику."),
            _recommendation(action="Наблюдать показания в течение суток."),
        ],
    )
    parsed.recommendations[0].evidence_metric_ids = ["metric:unknown"]
    result = _validate_structured_result(parsed)

    filtered = _filter_recommendations(
        result,
        valid_metric_ids={"metric:1"},
        valid_event_ids=set(),
        forbidden_categories=set(),
    )

    assert filtered.summary == "Валидная AI-интерпретация."
    assert [item.suggested_manual_action for item in filtered.recommendations] == [
        "Наблюдать показания в течение суток."
    ]

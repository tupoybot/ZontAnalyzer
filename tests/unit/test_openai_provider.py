from zont_analyzer.adapters.openai.provider import (
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


def test_semantic_recommendation_text_is_not_filtered() -> None:
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
        "Изменить сервисную калибровку.",
        "Наблюдать показания в течение суток."
    ]


def test_unknown_evidence_ids_are_not_filtered() -> None:
    parsed = _StructuredAnalysisResult(
        summary="Валидная AI-интерпретация.",
        recommendations=[
            _recommendation(action="Проверить неизвестную метрику."),
            _recommendation(action="Наблюдать показания в течение суток."),
        ],
    )
    parsed.recommendations[0].evidence_metric_ids = ["metric:unknown"]
    result = _validate_structured_result(parsed)

    assert result.summary == "Валидная AI-интерпретация."
    assert [item.suggested_manual_action for item in result.recommendations] == [
        "Проверить неизвестную метрику.",
        "Наблюдать показания в течение суток."
    ]

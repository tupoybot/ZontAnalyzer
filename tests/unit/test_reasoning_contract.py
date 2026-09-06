from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from zont_analyzer.adapters.sqlite import Database
from zont_analyzer.domain import (
    EvidenceReference,
    Hypothesis,
    MetricValue,
    ObservedPattern,
    Prediction,
    QualityResult,
    Recommendation,
    RecommendedExperiment,
    Report,
    TimeInterval,
    Unknown,
)
from zont_analyzer.reports import render_html, render_text


def _report() -> Report:
    interval = TimeInterval(
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        ended_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="Europe/Samara",
    )
    return Report(
        id="report:reasoning",
        kind="daily",
        period_start=interval.started_at,
        period_end=interval.ended_at,
        generated_at=interval.ended_at,
        timezone="Europe/Samara",
        quality=QualityResult(
            score=1,
            coverage_pct=100,
            max_gap_seconds=0,
            stuck_pct=0,
            implausible_jumps=0,
            sample_count=1,
        ),
        metrics=[MetricValue(id="metric:known", name="temperature", value=21, unit="°C")],
        summary="Сводка.",
        observed_patterns=[
            ObservedPattern(
                id="pattern:1",
                statement="Наблюдаемый паттерн.",
                interval=interval,
                evidence=[EvidenceReference(id="metric:known")],
                epistemic_level="observed",
            )
        ],
        hypotheses=[
            Hypothesis(
                id="hypothesis:presence",
                statement="Присутствие жильцов предполагается.",
                interval=interval,
                confidence=0.6,
                confidence_basis="сопоставление нескольких временных окон",
                rationale="Режим менялся в сходные часы.",
                evidence_for=[EvidenceReference(id="metric:known")],
                evidence_against=[EvidenceReference(id="window:missing")],
                alternatives=["Автоматическое расписание."],
            )
        ],
        predictions=[
            Prediction(
                id="prediction:1",
                scenario="Если режим сохранится.",
                expected_effect="Похожий профиль может повториться.",
                confidence=0.4,
                confidence_basis="ограниченный исторический ряд",
                assumptions=["Не изменится расписание."],
                evidence=[EvidenceReference(id="metric:known")],
                verification="Сопоставить следующий полный дневной период.",
            )
        ],
        unknowns=[Unknown(id="unknown:1", statement="Нет прямого датчика расхода.", interval=interval)],
        recommended_experiment=RecommendedExperiment(
            id="experiment:1",
            variable="уставка комнаты",
            current_value="21 °C",
            proposed_change="поднять на 0.5 °C вручную",
            rationale="Проверить реакцию системы.",
            expected_effect="Изменится наблюдаемая температура комнаты.",
            observation_period="2 дня",
            evidence=[EvidenceReference(id="metric:unknown")],
            success_criteria=["Комфорт оценён владельцем."],
            stop_conditions=["Дискомфорт владельца."],
            risks=["Временное изменение комфорта."],
        ),
        recommendations=[
            Recommendation(
                id="recommendation:legacy",
                title="Старый совет",
                category="observe_only",
                priority="low",
                confidence=0.5,
                evidence_metric_ids=["metric:legacy-unknown"],
                hypothesis="Нужна проверка.",
                suggested_manual_action="Наблюдать.",
                expected_effect="Появится информация.",
                observation_period_days=1,
            )
        ],
    )


def test_reasoning_contract_round_trips_through_sqlite_without_reference_rejection(tmp_path: Path) -> None:
    report = _report()
    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    db.save_report(report, render_text(report))

    saved = db.report(report.id)

    assert saved is not None
    assert saved.hypotheses[0].epistemic_level == "inferred"
    assert saved.hypotheses[0].evidence_against[0].id == "window:missing"
    assert saved.recommended_experiment is not None
    assert saved.recommended_experiment.observation_period == "2 дня"


def test_reasoning_rendering_labels_predictions_confidence_and_unknown_references() -> None:
    report = _report()
    report.context = {
        "dhw_profiles": {
            "current": [
                {
                    "id": "dhw:current",
                    "started_at": "2026-09-01T10:00:00+00:00",
                    "ended_at": "2026-09-01T10:12:00+00:00",
                    "timezone": "Europe/Samara",
                    "facts": {"dhw_target_c": 50, "duration_minutes": 12, "mode": "comfort"},
                    "inference": {},
                }
            ],
            "history": [
                {
                    "report_id": "daily:history",
                    "period_start": "2026-08-30T00:00:00+00:00",
                    "period_end": "2026-08-31T00:00:00+00:00",
                    "timezone": "Europe/Samara",
                    "quality": {"score": 0.9, "coverage_pct": 95},
                    "episodes": [
                        {
                            "id": "dhw:historical",
                            "started_at": "2026-08-30T10:00:00+00:00",
                            "ended_at": "2026-08-30T10:09:00+00:00",
                            "facts": {"dhw_target_c": 48, "duration_minutes": 9},
                            "inference": {},
                        }
                    ],
                    "equipment_profiles": [],
                }
            ],
        },
        "noise_history": [
            {
                "report_id": "daily:noise",
                "period_start": "2026-08-29T00:00:00+00:00",
                "period_end": "2026-08-30T00:00:00+00:00",
                "coverage_pct": 98,
                "events": [
                    {
                        "id": "event:historical-noise",
                        "kind": "temperature_pulse",
                        "started_at": "2026-08-29T10:00:00+00:00",
                        "ended_at": "2026-08-29T10:01:00+00:00",
                    }
                ],
            }
        ],
        "prior_interpretations": [{"hypotheses": [{"id": "hypothesis:prior"}]}],
    }
    report.hypotheses[0].evidence_for = [
        EvidenceReference(id="dhw:historical"),
        EvidenceReference(id="event:historical-noise"),
        EvidenceReference(id="hypothesis:prior"),
    ]

    text = render_text(report)
    rendered = render_html(report)

    assert "Прогнозы:" in text
    assert "Это не вероятность." in text
    assert "window:missing (неподтверждённая ссылка)" in text
    assert "metric:legacy-unknown (неподтверждённая ссылка)" in text
    assert "dhw:historical, event:historical-noise, hypothesis:prior (неподтверждённая ссылка)" in text
    assert "Профили эпизодов ГВС (свидетельства):" in text
    assert "цель: 48; длительность: 9" in text
    assert "История шумовых и надёжностных событий (свидетельства):" in text
    assert "[metric:known]" in text
    assert "Рекомендуемый ручной эксперимент:" in text
    assert "<h2>Прогнозы</h2>" in rendered
    assert "Это не вероятность." in rendered
    assert "window:missing (неподтверждённая ссылка)" in rendered
    assert "metric:legacy-unknown (неподтверждённая ссылка)" in rendered
    assert "dhw:historical, event:historical-noise, hypothesis:prior (неподтверждённая ссылка)" in rendered
    assert '<section class="historical-evidence"><details>' in rendered


def test_reasoning_validation_is_structural_and_old_reports_stay_compatible() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(
            id="hypothesis:bad",
            statement="",
            confidence=1.1,
            confidence_basis="",
            rationale="",
        )

    old = Report.model_validate(
        _report().model_dump(
            exclude={"observed_patterns", "hypotheses", "predictions", "unknowns", "recommended_experiment"}
        )
    )
    assert old.observed_patterns == []
    assert old.recommended_experiment is None


def test_interval_requires_aware_ordered_timestamps_but_renderer_falls_back_for_unknown_zone() -> None:
    with pytest.raises(ValidationError):
        TimeInterval(
            started_at=datetime(2026, 9, 1),
            ended_at=datetime(2026, 9, 2, tzinfo=UTC),
            timezone="UTC",
        )
    with pytest.raises(ValidationError):
        TimeInterval(
            started_at=datetime(2026, 9, 2, tzinfo=UTC),
            ended_at=datetime(2026, 9, 1, tzinfo=UTC),
            timezone="UTC",
        )

    report = _report()
    report.observed_patterns[0].interval = TimeInterval(
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        ended_at=datetime(2026, 9, 2, tzinfo=UTC),
        timezone="unrecognized/timezone",
    )
    assert "UTC" in render_text(report)


def test_point_observation_interval_survives_api_and_domain_validation() -> None:
    from zont_analyzer.adapters.openai.provider import _StructuredAnalysisResult, _validate_structured_result
    at = "2026-02-14T10:00:00+04:00"
    parsed = _StructuredAnalysisResult.model_validate({
        "summary": "Точечное наблюдение",
        "observed_patterns": [{"id": "point:1", "statement": "Изменилась уставка",
                               "epistemic_level": "observed", "interval": {
                                   "started_at": at, "ended_at": at, "timezone": "Europe/Samara"}}],
    })
    result = _validate_structured_result(parsed)
    interval = result.observed_patterns[0].interval
    assert interval is not None and interval.started_at == interval.ended_at

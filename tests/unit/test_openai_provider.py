import copy
import json
from datetime import UTC, datetime, timedelta

from zont_analyzer.adapters.openai.provider import (
    ANALYSIS_PACKET_MAX_BYTES,
    _StructuredAnalysisResult,
    _StructuredRecommendation,
    _validate_structured_result,
    analysis_packet,
)
from zont_analyzer.domain import DetectedEvent, MetricValue


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
        "Наблюдать показания в течение суток.",
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
        "Наблюдать показания в течение суток.",
    ]


def _metric(index: int) -> MetricValue:
    return MetricValue(id=f"metric:{index:04d}", name="Показатель", value=float(index), unit="C")


def _event(index: int) -> DetectedEvent:
    at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return DetectedEvent(id=f"event:{index:04d}", kind="test", started_at=at)


def _window(index: int) -> dict[str, object]:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index)
    return {
        "id": f"evidence:hour:{index:04d}",
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(hours=1)).isoformat(),
        "timezone": "Europe/Samara",
        "signals": {
            "control": {
                "mean": 21.0,
                "coverage_pct": 100.0,
                "sample_count": 60,
                "source": "observed",
            }
        },
        "facts": {"room_error": {"mean": 0.2, "coverage_pct": 100.0, "sample_count": 60, "source": "derived"}},
    }


def test_analysis_packet_is_hard_bounded_deterministic_and_preserves_temporal_windows() -> None:
    context = {
        "temporal_evidence": {
            "algorithm_version": "heating-evidence-v1",
            "period_start": "2026-01-01T00:00:00+00:00",
            "period_end": "2026-02-01T00:00:00+00:00",
            "timezone": "Europe/Samara",
            "signals": {"control": {"identity": "zont:a:b:c", "source": "observed"}},
            "windows": [_window(index) for index in range(120)],
        },
        "dhw_interaction": {"very_large_legacy_context": "x" * 100_000},
        "reliability": {"very_large_legacy_context": "y" * 100_000},
    }
    before = copy.deepcopy(context)
    kwargs = {
        "quality": {"score": 0.9, "raw": "q" * 10_000},
        "metrics": [_metric(index) for index in range(300)],
        "events": [_event(index) for index in range(300)],
        "period": {"kind": "monthly", "start": "2026-01-01", "end": "2026-02-01"},
        "context": context,
        "recommendation_feedback": [{"recommendation_id": str(index), "owner_note": "z" * 500} for index in range(300)],
    }

    first = analysis_packet(**kwargs)
    second = analysis_packet(**kwargs)
    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert len(encoded) <= ANALYSIS_PACKET_MAX_BYTES
    assert first["provenance"]["truncation"]["serialized_bytes"] == len(
        json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert first == second
    assert context == before
    windows = first["control_context"]["temporal_evidence"]["windows"]
    assert windows
    assert all("id" in window and "started_at" in window and "signals" in window for window in windows)
    assert first["control_context"]["temporal_evidence"]["signals"]["control"]["source"] == "observed"
    assert first["provenance"]["truncation"]["omitted"]
    assert first["provenance"]["metrics"]["epistemic_level"] == "derived"
    assert first["provenance"]["control_context"]["epistemic_level"] == "context"


def test_analysis_packet_keeps_unicode_strings_whole_and_accounts_for_oversized_window() -> None:
    huge_window = _window(1)
    huge_window["note"] = "ёж" * 40_000
    packet = analysis_packet(
        quality={"score": 1.0},
        metrics=[],
        events=[],
        period={"kind": "daily"},
        context={"temporal_evidence": {"timezone": "Europe/Samara", "windows": [huge_window]}},
    )

    assert packet["control_context"]["temporal_evidence"]["windows"] == []
    omitted = packet["provenance"]["truncation"]["omitted"]["temporal_evidence.windows"]
    assert omitted["items"] == 1
    assert omitted["serialized_bytes"] > ANALYSIS_PACKET_MAX_BYTES


def test_analysis_packet_prioritizes_critical_and_stage_two_context_events() -> None:
    events = [_event(index) for index in range(1_000)]
    for event in events:
        event.details["context"] = "x" * 300
    events[-1].kind = "dhw_episode"
    events[-2].severity = "critical"

    packet = analysis_packet(
        quality={"score": 1.0},
        metrics=[],
        events=events,
        period={"kind": "monthly"},
    )

    retained_ids = {event["id"] for event in packet["events"]}
    assert {events[-1].id, events[-2].id} <= retained_ids


def test_analysis_packet_preserves_complete_evidence_dto_when_it_fits() -> None:
    evidence = {
        "algorithm_version": "heating-evidence-v1",
        "period_start": "2026-01-01T00:00:00+00:00",
        "period_end": "2026-01-02T00:00:00+00:00",
        "timezone": "Europe/Samara",
        "capability_profile": "unknown",
        "signals": {"control": {"identity": "zont/a", "origin": "observed", "provenance": "zont"}},
        "windows": [_window(1)],
        "metrics": [{"id": "metric:cycles", "name": "Cycles", "value": 1.0, "unit": "1/h", "source": "derived"}],
        "quality": {"control": {"coverage_pct": 90.0, "sample_count": 30, "source": "observed"}},
        "exclusions": {"dhw": 12.0},
        "exclusion_windows": [
            {
                "id": "exclude:1",
                "started_at": "2026-01-01T01:00:00+00:00",
                "ended_at": "2026-01-01T02:00:00+00:00",
                "reason": "dhw",
                "source": "derived",
            }
        ],
        "state_source": "state:z3k",
        "unknowns": ["return temperature unavailable"],
        "future_extension": {"epistemic_level": "inferred", "reason": "kept if it fits"},
    }

    packet = analysis_packet(
        quality={"score": 1.0}, metrics=[], events=[], period={"kind": "daily"}, context={"temporal_evidence": evidence}
    )

    assert packet["control_context"]["temporal_evidence"] == evidence
    assert packet["provenance"]["truncation"]["omitted"] == {}


def test_packet_shares_budget_between_dhw_dynamics_and_aggregate_metrics() -> None:
    events = [_event(index) for index in range(12)]
    for event in events:
        event.kind = "dhw_reheat_episode"
        event.details = {"facts": {"temperature_samples_description": "x" * 2_000}}
    metrics = [MetricValue(id=f"metric:{index:03}", name="temperature", value=index, unit="°C") for index in range(44)]
    packet = analysis_packet(
        quality={"score": 1.0}, metrics=metrics, events=events, period={"kind": "daily"},
        context={"temporal_evidence": {"windows": [_window(1)], "provenance_note": "x" * 42_000}},
    )
    assert any(event["kind"] == "dhw_reheat_episode" for event in packet["events"])
    assert len(packet["metrics"]) == len(metrics)
    assert packet["provenance"]["truncation"]["omitted"]["events"]["items"] > 0
    assert packet["provenance"]["truncation"]["serialized_bytes"] <= ANALYSIS_PACKET_MAX_BYTES

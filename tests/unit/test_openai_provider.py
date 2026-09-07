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


def test_analysis_packet_distinguishes_derived_house_context_from_owner_feedback() -> None:
    packet = analysis_packet(
        quality={"score": 1.0},
        metrics=[],
        events=[],
        period={"kind": "daily"},
        context={"house_context": {"room_temperature_median": 21.2, "owner_note": "Вечером были дома."}},
        recommendation_feedback=[{"recommendation_id": "r:1", "status": "applied", "owner_note": "Проверено."}],
    )

    assert packet["provenance"]["house_context"]["epistemic_level"] == "derived"
    assert packet["provenance"]["recommendation_feedback"]["epistemic_level"] == "owner_confirmed"
    assert packet["control_context"]["house_context"]["room_temperature_median"] == 21.2


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


def test_weekly_allocator_keeps_new_context_metrics_and_representative_dhw_event() -> None:
    metrics = [
        MetricValue(id=f"legacy:{index:02d}", name="comfort_metric", value=index, unit="C") for index in range(43)
    ]
    events = [_event(index) for index in range(120)]
    events[0].kind = "dhw_reheat_episode"
    events[0].details = {"full_episode": True, "target_c": 50, "duration_minutes": 12}
    events[1].kind = "reliability_loss"
    context = {
        "house_context": {"room_temperature_median": 21.2, "history": "h" * 8_000},
        "period_comparisons": [{"id": "comparison:weekly", "before": "prior", "after": "current"}],
        "intervention_outcomes": [{"id": "outcome:weekly", "status": "indeterminate"}],
        "reliability": {"losses": [{"id": f"loss:{index}", "kind": "zont_restart"} for index in range(30)]},
        "sensors": {"catalog": [{"id": f"sensor:{index}"} for index in range(30)]},
        "dhw_profiles": {"history": "d" * 10_000},
        "temporal_evidence": {
            "windows": [_window(index) for index in range(100)],
            "exclusion_windows": [{"id": f"exclude:{index}", "started_at": str(index)} for index in range(100)],
        },
    }

    packet = analysis_packet(
        quality={"score": 1.0}, metrics=metrics, events=events, period={"kind": "weekly"}, context=context
    )
    encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert len(encoded) <= ANALYSIS_PACKET_MAX_BYTES
    assert packet["control_context"]["house_context"]
    assert packet["control_context"]["period_comparisons"]
    assert packet["control_context"]["intervention_outcomes"]
    assert packet["control_context"]["reliability"]
    assert packet["control_context"]["sensors"]
    assert packet["metrics"]
    assert any(
        event["kind"] == "dhw_reheat_episode" and event["details"].get("full_episode")
        for event in packet["events"]
    )
    assert packet == analysis_packet(
        quality={"score": 1.0}, metrics=metrics, events=events, period={"kind": "weekly"}, context=context
    )


def test_reasoning_provider_preserves_budget_privacy_and_records_actual_prompt(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zont_analyzer.adapters.openai.provider import PROMPT_VERSION, OpenAIAnalyst
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    config = AppConfig.model_validate({"openai": {"prompt_version": "legacy-config"}})
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=config, db=db)
    parse = Mock(return_value=SimpleNamespace(
        output_parsed=_StructuredAnalysisResult(summary="Причина пока неизвестна", unknowns=[
            {"id": "u:source", "statement": "Нет подтверждённого погодного источника"}
        ]), usage=SimpleNamespace(input_tokens=10, output_tokens=20), id="mock:reasoning",
    ))
    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = analyst.analyze({"period": {"kind": "daily"}})
    assert result.unknowns[0].id == "u:source" and result.recommendations == []
    kwargs = parse.call_args.kwargs
    assert kwargs["store"] is False and "tools" not in kwargs
    assert kwargs["max_output_tokens"] == 6000
    assert db.token_usage_this_month() == 30
    import sqlite3
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("select prompt_version from llm_calls").fetchone()[0] == PROMPT_VERSION
    config.openai.monthly_token_budget = 30
    import pytest
    with pytest.raises(RuntimeError, match="budget"):
        analyst.analyze({"period": {"kind": "daily"}})
    assert parse.call_count == 1


def test_reasoning_provider_reuses_successful_result_without_second_api_call(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zont_analyzer.adapters.openai.provider import OpenAIAnalyst
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=AppConfig(), db=db)
    parsed = _StructuredAnalysisResult(summary="Повторное чтение")
    parse = Mock(return_value=SimpleNamespace(
        output_parsed=parsed, usage=SimpleNamespace(input_tokens=11, output_tokens=7), id="mock:idempotent"
    ))
    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))

    first = analyst.analyze({"period": {"kind": "daily"}, "nonce": "same"})
    second = analyst.analyze({"period": {"kind": "daily"}, "nonce": "same"})

    assert first == second
    assert parse.call_count == 1
    assert db.token_usage_this_month() == 18


def test_reasoning_provider_accounts_usage_when_structured_output_is_invalid(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zont_analyzer.adapters.openai.provider import OpenAIAnalyst
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=AppConfig(), db=db)
    parse = Mock(return_value=SimpleNamespace(
        output_parsed=None, usage=SimpleNamespace(input_tokens=13, output_tokens=29), id="mock:invalid"
    ))
    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))

    import pytest
    with pytest.raises(RuntimeError, match="parsed output"):
        analyst.analyze({"period": {"kind": "daily"}, "nonce": "invalid"})

    assert db.token_usage_this_month() == 42
    with pytest.raises(RuntimeError, match="previously failed"):
        analyst.analyze({"period": {"kind": "daily"}, "nonce": "invalid"})
    assert parse.call_count == 1


def test_ai_ledger_serializes_pending_reservations(tmp_path) -> None:
    from zont_analyzer.application.ai_ledger import AILedger

    first = AILedger(tmp_path / "state.sqlite3")
    second = AILedger(tmp_path / "state.sqlite3")
    assert first.reserve("same", budget=100, used=0, estimate=80) is None
    pending = second.reserve("same", budget=100, used=0, estimate=80)
    assert pending is not None and pending["status"] == "pending"
    with __import__("pytest").raises(RuntimeError, match="exhausted"):
        second.reserve("other", budget=100, used=0, estimate=30)


def test_request_fingerprint_changes_for_nonce_and_model(tmp_path) -> None:
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zont_analyzer.adapters.openai.provider import OpenAIAnalyst
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=AppConfig(), db=db)
    parse = Mock(side_effect=[
        SimpleNamespace(output_parsed=_StructuredAnalysisResult(summary="one"), usage=None, id="one"),
        SimpleNamespace(output_parsed=_StructuredAnalysisResult(summary="two"), usage=None, id="two"),
    ])
    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))

    analyst.analyze({"period": {"kind": "daily"}, "request_nonce": "one"})
    analyst.config.openai.daily_model = "different-model"
    analyst.analyze({"period": {"kind": "daily"}, "request_nonce": "two"})
    assert parse.call_count == 2
    ledger_entries = json.loads(analyst.ledger.path.read_text())["entries"].values()
    assert all(int(entry["charged_tokens"]) > 0 for entry in ledger_entries)


def test_budget_reservation_includes_prompt_and_schema_bytes(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from zont_analyzer.adapters.openai.provider import SYSTEM_PROMPT, OpenAIAnalyst, _StructuredAnalysisResult
    from zont_analyzer.adapters.sqlite import Database
    from zont_analyzer.config import AppConfig

    db = Database(tmp_path / "state.sqlite3")
    db.initialize()
    schema = __import__("json").dumps(_StructuredAnalysisResult.model_json_schema(), ensure_ascii=False, sort_keys=True)
    minimum = len(SYSTEM_PROMPT.encode()) + len(schema.encode()) + 1 + 6000
    config = AppConfig.model_validate({"openai": {"monthly_token_budget": minimum - 1}})
    analyst = OpenAIAnalyst(api_key="not-a-real-key", config=config, db=db)
    parse = Mock(return_value=SimpleNamespace(output_parsed=_StructuredAnalysisResult(summary="too late"), usage=None))
    analyst.client = SimpleNamespace(responses=SimpleNamespace(parse=parse))

    import pytest
    with pytest.raises(RuntimeError, match="budget"):
        analyst.analyze({"period": {"kind": "daily"}})
    parse.assert_not_called()


def test_ai_ledger_keeps_old_pending_fingerprint_but_scopes_budget_by_month(tmp_path) -> None:
    import json
    import time

    from zont_analyzer.application.ai_ledger import AILedger

    ledger = AILedger(tmp_path / "state.sqlite3")
    assert ledger.reserve("stuck", budget=100, used=0, estimate=90, billing_month="2025-01") is None
    state = json.loads(ledger.path.read_text())
    state["entries"]["stuck"]["created_at"] = time.time() - 7200
    ledger.path.write_text(json.dumps(state))
    assert ledger.reserve("stuck", budget=1, used=0, estimate=1, billing_month="2026-09")["status"] == "pending"
    assert ledger.reserve("new", budget=10, used=0, estimate=10, billing_month="2026-09") is None


def test_ai_ledger_fails_closed_on_corruption_and_unknown_usage_stays_charged(tmp_path) -> None:
    from zont_analyzer.application.ai_ledger import AILedger

    ledger = AILedger(tmp_path / "state.sqlite3")
    ledger.path.write_text("not-json")
    import pytest
    with pytest.raises(RuntimeError, match="corrupt"):
        ledger.reserve("key", budget=100, used=0, estimate=1)

    ledger.path.unlink()
    assert ledger.reserve("ambiguous", budget=100, used=0, estimate=80, billing_month="2026-09") is None
    ledger.finish("ambiguous", status="failure", input_tokens=0, output_tokens=0, charge_reserved=True)
    with pytest.raises(RuntimeError, match="exhausted"):
        ledger.reserve("another", budget=100, used=0, estimate=21, billing_month="2026-09")

"""Offline evaluation fixtures for the stage 6 reasoning contract.

These tests validate packet shape, provenance and auditable mock responses. They
do not claim that an external model would make the same substantive judgement.
"""

import json
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from zont_analyzer.adapters.openai.provider import (  # type: ignore[import-untyped]
    _StructuredAnalysisResult,
    analysis_packet,
)
from zont_analyzer.domain import DetectedEvent, MetricValue  # type: ignore[import-untyped]

FIXTURE = Path(__file__).parents[1] / "fixtures" / "reasoning" / "stage6_cases.json"


def _ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        candidate = value.get("id")
        if isinstance(candidate, str):
            found.add(candidate)
        for item in value.values():
            found.update(_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_ids(item))
    return found


def _packet(case: dict[str, Any]) -> dict[str, Any]:
    source = case["packet"]
    return cast(
        dict[str, Any],
        analysis_packet(
            quality=source["data_quality"],
            metrics=[MetricValue.model_validate(item) for item in source["metrics"]],
            events=[DetectedEvent.model_validate(item) for item in source["events"]],
            period=source["period"],
            context=source["control_context"],
        ),
    )


def test_stage6_fixture_covers_the_required_comparison_and_occupancy_matrix() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = {case["id"] for case in cases}
    assert len(cases) == len(ids) >= 10
    assert {"experiment_success_before_after", "experiment_failed_and_rollback"} <= ids
    assert "firmware_update_rollback_unknown_history" in ids
    assert "false_ab_improvement_weather_mode_dhw_coverage" in ids
    assert {
        "season_autumn_vs_spring", "season_autumn_vs_last_autumn", "season_winter_vs_shoulder", "season_missing_prior"
    } <= ids
    assert {"occupancy_with_hypothesis", "occupancy_without_hypothesis_owner_context"} <= ids


def test_stage6_packets_preserve_context_provenance_and_boundaries() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in cases:
        packet = _packet(case)
        assertions = case["assertions"]
        context = packet["control_context"]
        assert set(assertions["required_context"]) <= set(context)
        assert packet["provenance"]["control_context"]["epistemic_level"] == "context"
        assert packet["provenance"]["truncation"]["omitted"] == {}
        assert packet["provenance"]["truncation"]["serialized_bytes"] <= 64 * 1024
        assert "occupancy_hypothesis" not in context
        available_ids = _ids(packet)
        assert set(assertions["must_reference"]) <= available_ids


def test_stage6_mock_outputs_are_valid_and_reference_supplied_evidence() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in cases:
        packet = _packet(case)
        try:
            result = _StructuredAnalysisResult.model_validate(case["expected_response"])
        except ValidationError as exc:  # pragma: no cover - makes fixture failures explicit
            raise AssertionError(f"invalid expected response in {case['id']}: {exc}") from exc
        response_ids = _ids(result.model_dump(mode="json"))
        supplied_ids = _ids(packet)
        evidence_ids = {
            reference["id"]
            for reference in _evidence_references(result.model_dump(mode="json"))
        }
        assert evidence_ids <= supplied_ids, case["id"]
        assert len(result.recommendations) <= 3
        if case["assertions"]["requires_unknown"]:
            assert result.unknowns, case["id"]
        assert response_ids  # Reject empty, checklist-only mock responses.


def _evidence_references(value: Any) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"evidence", "evidence_for", "evidence_against"} and isinstance(item, list):
                references.extend(reference for reference in item if isinstance(reference, dict) and "id" in reference)
            else:
                references.extend(_evidence_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(_evidence_references(item))
    return references

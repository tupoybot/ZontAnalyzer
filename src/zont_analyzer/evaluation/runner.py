"""Deterministic checks and explicit manual-assessment records for saved responses."""
# ruff: noqa: E501
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from zont_analyzer.adapters.openai.provider import _StructuredAnalysisResult

from .dataset import DATASET_VERSION, build_dataset, dataset_sha


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part for item in value for part in _strings(item)]
    if isinstance(value, dict):
        return [part for item in value.values() for part in _strings(item)]
    return []


def _measurement(meta: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "latency_ms"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    parameters = meta.get("parameters")
    if isinstance(parameters, dict):
        result["parameters"] = parameters
    return result


def _referenced_ids(value: Any, key: str = "") -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for name, item in value.items():
            if any(token in name.casefold() for token in ("evidence", "metric_id", "event_id", "source_id")):
                found.update(_strings(item))
            found.update(_referenced_ids(item, name))
        return found
    if isinstance(value, list):
        found_list: set[str] = set()
        for child in value:
            found_list.update(_referenced_ids(child, key))
        return found_list
    if isinstance(value, str):
        text = value.casefold()
        refs = set(re.findall(r"\b(?:evidence|metric|event|experiment|citation|source)(?:[_ -]?id)?\s*[:=]\s*([a-z0-9_-]+)", text))
        if any(token in key.casefold() for token in ("evidence", "metric_id", "event_id", "source_id")):
            refs.update(re.findall(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b", text))
        return refs
    return set()


def _packet_evidence_ids(packet: dict[str, Any]) -> set[str]:
    known = {item["id"] for item in packet.get("metrics", []) + packet.get("events", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    temporal = packet.get("control_context", {}).get("temporal_evidence", {})
    for collection in ("windows", "exclusion_windows"):
        for item in temporal.get(collection, []) if isinstance(temporal, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                known.add(item["id"])
    return known


def _manual_valid(assessment: Any) -> bool:
    if not isinstance(assessment, dict) or not isinstance(assessment.get("assessor"), str) or not assessment["assessor"].strip() or not isinstance(assessment.get("date"), str) or not isinstance(assessment.get("rationale"), str) or not assessment["rationale"].strip():
        return False
    try:
        date.fromisoformat(assessment["date"])
    except ValueError:
        return False
    return True


def load_assessments(path: str | Path, *, dataset_sha256: str, prompt_id: str, schema_id: str) -> dict[str, dict[str, Any]]:
    """Load only complete, current semantic assessments for model-review use.

    Invalid, stale, or incomplete entries are omitted, so callers cannot use a
    partial file to clear a model-review ``requires_evaluation`` flag.
    """
    with Path(path).open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        return {}
    accepted: dict[str, dict[str, Any]] = {}
    for model_id, assessment in payload.items():
        if not isinstance(model_id, str) or not isinstance(assessment, dict):
            continue
        if assessment.get("dataset_sha256") != dataset_sha256 or assessment.get("prompt_id") != prompt_id or assessment.get("schema_id") != schema_id:
            continue
        if not _manual_valid(assessment):
            continue
        scores = assessment.get("scores")
        if not isinstance(scores, dict) or any(not isinstance(scores.get(name), (int, float)) or isinstance(scores.get(name), bool) or not 0 <= scores[name] <= 1 for name in ("factual", "advice", "uncertainty")):
            continue
        measurements = _measurement(assessment.get("measurements"))
        accepted[model_id] = {**assessment, "measurements": measurements}
    return accepted


def _case_result(case: dict[str, Any], raw: Any, meta: dict[str, Any] | None) -> dict[str, Any]:
    rubric = case["rubric"]
    measurements = _measurement(meta)
    try:
        candidate = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(candidate, dict):
            candidate.pop("provenance", None)
        result = _StructuredAnalysisResult.model_validate(candidate)
        schema = rubric["schema"]
    except Exception as exc:
        return {"case_id": case["id"], "status": "invalid_schema", "schema_valid": False, "manual_assessment_required": True, "validation_error": str(exc), "measurements": measurements}
    packet_ids = _packet_evidence_ids(case["packet"])
    references = _referenced_ids(result.model_dump(mode="json"))
    expected = set(case["expected_evidence_ids"])
    cited_known = references & packet_ids
    hallucinated = sorted(references - packet_ids)
    forbidden_hits = [claim for claim in case["forbidden_claims"] if claim.casefold() in "\n".join(_strings(result.model_dump(mode="json"))).casefold()]
    return {"case_id": case["id"], "status": "evaluated", "schema_valid": True, "evidence_coverage": sorted(cited_known & expected), "missing_expected_evidence": sorted(expected - cited_known), "hallucinated_evidence_refs": hallucinated, "forbidden_claim_hits": forbidden_hits, "factual": None, "advice": None, "uncertainty": None, "machine_score": {"schema": schema, "evidence": rubric["evidence"] if expected.issubset(cited_known) and not hallucinated else 0, "factual": None}, "manual_assessment_required": True, "manual_dimensions": ["factual", "advice", "uncertainty"], "measurements": measurements, "referenced_evidence_ids": sorted(references)}


def evaluate_responses(responses_dir: str | Path, output: str | Path, *, model_id: str | None = None, prompt_id: str | None = None, schema_id: str | None = None, dataset: list[dict[str, Any]] | None = None, dataset_dir: str | Path | None = None) -> dict[str, Any]:
    cases = dataset or build_dataset()
    response_root = Path(responses_dir)
    results = []
    for case in cases:
        response_path = response_root / f"{case['id']}.json"
        meta_path = response_root / f"{case['id']}.meta.json"
        if not response_path.is_file():
            results.append({"case_id": case["id"], "status": "missing_response", "manual_assessment_required": True, "measurements": {}})
            continue
        try:
            raw_bytes = response_path.read_bytes()
            raw = json.loads(raw_bytes)
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else None
            results.append({"response_sha256": hashlib.sha256(raw_bytes).hexdigest(), **_case_result(case, raw, meta)})
        except (OSError, ValueError) as exc:
            results.append({"case_id": case["id"], "status": "invalid_response_file", "manual_assessment_required": True, "error": str(exc), "measurements": {}})
    packet_root = Path(dataset_dir) if dataset_dir is not None else Path("<materialized-dataset>")
    report = {"report_version": "evaluation-report-v1", "dataset_version": DATASET_VERSION, "dataset_sha256": dataset_sha(cases), "model_id": model_id, "prompt_id": prompt_id, "schema_id": schema_id, "identity_complete": all(isinstance(value, str) and value.strip() for value in (model_id, prompt_id, schema_id)), "packet_paths": {case["id"]: str(packet_root / f"{case['id']}.json") for case in cases}, "candidate_response_dir": str(response_root), "results": results, "recommendation": "manual_assessment_required", "selection_policy": "No recommendation is based on model recency, price, or provider identity; advice and uncertainty require human review."}
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def compare_reports(paths: list[str | Path], manual_assessments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Align reports and permit a recommendation only with explicit human review."""
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    keys = ("dataset_version", "dataset_sha256", "prompt_id", "schema_id")
    compatible = bool(reports) and all(all(report.get(key) == reports[0].get(key) for key in keys) for report in reports)
    assessments = manual_assessments or {}
    valid_assessments = all(_manual_valid(assessments.get(report.get("model_id"))) for report in reports)
    rows = []
    for case_id in sorted({item.get("case_id") for report in reports for item in report.get("results", []) if item.get("case_id")}):
        rows.append({"case_id": case_id, "models": [{"model_id": report.get("model_id"), "result": next((item for item in report.get("results", []) if item.get("case_id") == case_id), None)} for report in reports]})
    return {"report_version": "comparison-v1", "compatible": compatible, "manual_assessments_valid": valid_assessments, "recommendation": "manual_assessment_required" if not (compatible and valid_assessments) else "manual_assessment_recorded_for_owner_review", "cases": rows, "manual_assessments": assessments}

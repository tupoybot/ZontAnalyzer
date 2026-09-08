import json

from zont_analyzer.domain import AnalysisResult
from zont_analyzer.evaluation.dataset import DATASET_VERSION, build_dataset, materialize_dataset
from zont_analyzer.evaluation.runner import evaluate_responses, load_assessments

# ruff: noqa: E501


def test_dataset_has_eight_real_contract_packets_and_stable_sha(tmp_path) -> None:
    cases = build_dataset()
    assert DATASET_VERSION == "evaluation-v1"
    assert len(cases) == 8
    assert {case["id"] for case in cases} == {"normal", "insufficient", "dhw-heating", "competing-causes", "rejected-advice", "experiment", "gas", "long-review"}
    assert all(set(case["packet"]) >= {"period", "data_quality", "metrics", "events", "provenance"} for case in cases)
    monthly = next(case for case in cases if case["id"] == "long-review")["packet"]["period"]
    assert monthly["start"] == "2026-01-01" and monthly["end"] == "2026-02-01"
    competing = next(case for case in cases if case["id"] == "competing-causes")["packet"]["control_context"]["temporal_evidence"]
    assert {item["id"] for item in competing["windows"]} == {"window-ventilation-1", "window-setback-1"}
    result = materialize_dataset(tmp_path / "packets")
    assert result["sha256"] and result["cases"] == 8
    assert len(list((tmp_path / "packets").glob("*.json"))) == 8


def test_offline_runner_scores_native_result_and_requires_manual_judgment(tmp_path) -> None:
    packets = build_dataset()
    responses = tmp_path / "responses"
    responses.mkdir()
    result = AnalysisResult(summary="Факты требуют наблюдения.").model_dump(mode="json")
    (responses / "normal.json").write_text(json.dumps(result), encoding="utf-8")
    (responses / "normal.meta.json").write_text(json.dumps({"input_tokens": 123, "output_tokens": 45, "latency_ms": 812}), encoding="utf-8")
    report_path = tmp_path / "report.json"
    report = evaluate_responses(responses, report_path, model_id="candidate", prompt_id="prompt-v1", schema_id="schema-v1", dataset=packets)
    normal = next(item for item in report["results"] if item["case_id"] == "normal")
    assert normal["status"] == "evaluated"
    assert normal["manual_assessment_required"] is True
    assert normal["measurements"]["input_tokens"] == 123
    assert normal["factual"] is None and normal["advice"] is None
    assert report["recommendation"] == "manual_assessment_required"
    assert report["packet_paths"]["normal"].endswith("normal.json")
    assert json.loads(report_path.read_text())['dataset_sha256'] == report['dataset_sha256']


def test_invalid_and_missing_responses_are_preserved_in_report(tmp_path) -> None:
    responses = tmp_path / "responses"
    responses.mkdir()
    (responses / "normal.json").write_text("{}", encoding="utf-8")
    report = evaluate_responses(responses, tmp_path / "report.json")
    invalid = next(item for item in report["results"] if item["case_id"] == "normal")
    missing = next(item for item in report["results"] if item["case_id"] == "gas")
    assert invalid["status"] == "invalid_schema" and invalid["schema_valid"] is False
    assert missing["status"] == "missing_response"


def test_machine_report_flags_hallucinated_explicit_evidence_refs_without_claiming_semantic_accuracy(tmp_path) -> None:
    responses = tmp_path / "responses"
    responses.mkdir()
    response = {
        "summary": "Наблюдение требует проверки.",
        "hypotheses": [{"id": "h1", "statement": "Есть гипотеза.", "confidence": 0.2, "confidence_basis": "unknown", "rationale": "Нужна проверка.", "evidence_for": [{"id": "fake-event-9"}], "evidence_against": [], "alternatives": [], "epistemic_level": "inferred"}],
        "recommendations": [], "observed_patterns": [], "predictions": [], "unknowns": [],
        "recommended_experiment": None,
    }
    (responses / "normal.json").write_text(json.dumps(response), encoding="utf-8")
    report = evaluate_responses(responses, tmp_path / "report.json", model_id="m", prompt_id="p", schema_id="s")
    normal = next(item for item in report["results"] if item["case_id"] == "normal")
    assert normal["schema_valid"] is True
    assert "fake-event-9" in normal["hallucinated_evidence_refs"]
    assert normal["factual"] is None


def test_load_assessments_rejects_stale_or_incomplete_entries(tmp_path) -> None:
    path = tmp_path / "assessments.json"
    path.write_text(json.dumps({
        "candidate": {"dataset_sha256": "sha", "prompt_id": "p", "schema_id": "s", "assessor": "owner", "date": "2026-09-08", "rationale": "Reviewed", "scores": {"factual": 0.8, "advice": 0.7, "uncertainty": 0.9}, "measurements": {"input_tokens": 10, "latency_ms": 20}},
        "stale": {"dataset_sha256": "old", "prompt_id": "p", "schema_id": "s", "assessor": "owner", "date": "2026-09-08", "rationale": "Reviewed", "scores": {"factual": 1, "advice": 1, "uncertainty": 1}},
        "partial": {"dataset_sha256": "sha", "prompt_id": "p", "schema_id": "s", "assessor": "owner", "date": "2026-09-08", "rationale": "", "scores": {"factual": 1, "advice": 1, "uncertainty": 1}},
    }), encoding="utf-8")
    loaded = load_assessments(path, dataset_sha256="sha", prompt_id="p", schema_id="s")
    assert set(loaded) == {"candidate"}
    assert loaded["candidate"]["measurements"]["latency_ms"] == 20

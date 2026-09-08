from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from zont_analyzer.adapters.openai.model_catalog import CatalogSnapshot, ModelFact
from zont_analyzer.adapters.openai.provider import PROMPT_VERSION, SCHEMA_VERSION
from zont_analyzer.application.ai_maintenance import local_assessments
from zont_analyzer.application.ai_settings import AISettingsStore
from zont_analyzer.application.model_review import ModelReviewStore
from zont_analyzer.evaluation.dataset import build_dataset, dataset_sha
from zont_analyzer.runtime import build_runtime


def test_only_complete_matching_local_assessments_are_used(tmp_path: Path) -> None:
    runtime = build_runtime(None, tmp_path)
    runtime.config.openai.evaluation_results_file = "assessments.json"
    cases = build_dataset()
    entry = {
        "dataset_sha256": dataset_sha(cases), "prompt_id": PROMPT_VERSION, "schema_id": SCHEMA_VERSION,
        "assessor": "owner", "date": "2026-09-08", "rationale": "Reviewed all evidence and alternative causes",
        "completed_case_ids": [case["id"] for case in cases],
        "scores": {"factual": 0.9, "advice": 0.8, "uncertainty": 0.9},
        "measurements": {"parameters": {"reasoning_effort": "medium"}, "latency_ms": 12000},
    }
    artifact = tmp_path / "assessments.json"
    artifact.write_text(json.dumps({"candidate": entry}))
    assert local_assessments(runtime)["candidate"]["scores"] == entry["scores"]
    class Catalog:
        def fetch(self, now=None, model_ids=()):
            return CatalogSnapshot(datetime.now(UTC), (), tuple(
                ModelFact(model, cost_in, cost_out, reasoning_efforts=("medium",),
                          responses_supported=True, structured_outputs_supported=True)
                for model, cost_in, cost_out in (("gpt-5.6-luna", "0.2", "1.2"), ("gpt-5.6-terra", "2", "12"))
            ))
    review = ModelReviewStore(runtime.db, Catalog(), assessments={"gpt-5.6-luna": entry})
    review.run_if_due(AISettingsStore(runtime.db, runtime.config).snapshot())
    recommendation = review.state()["proposals"][0]["recommendation"]
    assert not recommendation["requires_evaluation"]
    assert "0.90" in recommendation["tradeoffs"]["quality"]
    for invalid in ({"completed_case_ids": []}, {"completed_case_ids": None},
                    {"prompt_id": "old-prompt"}, {"schema_id": "old-schema"},
                    {"dataset_sha256": "old-dataset"}, {"rationale": ""}):
        artifact.write_text(json.dumps({"candidate": entry | invalid}))
        assert local_assessments(runtime) == {}
    artifact.write_text("not json")
    assert local_assessments(runtime) == {}

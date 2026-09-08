from __future__ import annotations

import argparse
import json

from .dataset import materialize_dataset
from .runner import compare_reports, evaluate_responses

# ruff: noqa: E501


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline model evaluation packets and saved-response assessment")
    parser.add_argument("--dataset-dir", required=True, help="Directory to materialize the versioned packet set")
    parser.add_argument("--responses-dir", help="Directory containing native AnalysisResult JSON files named <case>.json")
    parser.add_argument("--output", help="Machine-readable assessment report path")
    parser.add_argument("--model-id")
    parser.add_argument("--prompt-id")
    parser.add_argument("--schema-id")
    parser.add_argument("--compare", nargs="*", help="Existing evaluation reports to align")
    parser.add_argument("--manual-assessments", help="JSON object keyed by model ID with assessor/date/rationale")
    args = parser.parse_args()
    if args.compare:
        if args.manual_assessments:
            with open(args.manual_assessments, encoding="utf-8") as source:
                assessments = json.load(source)
        else:
            assessments = None
        print(json.dumps(compare_reports(args.compare, assessments), ensure_ascii=False, indent=2))
        return
    print(json.dumps(materialize_dataset(args.dataset_dir), ensure_ascii=False, indent=2))
    if args.responses_dir and args.output:
        if not all((args.model_id, args.prompt_id, args.schema_id)):
            parser.error("--model-id, --prompt-id and --schema-id are required with --responses-dir")
        print(json.dumps(evaluate_responses(args.responses_dir, args.output, model_id=args.model_id, prompt_id=args.prompt_id, schema_id=args.schema_id, dataset_dir=args.dataset_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

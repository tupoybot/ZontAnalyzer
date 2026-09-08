# Offline model evaluation packets

The evaluation set is `evaluation-v1` and contains eight anonymized packets built
through the production `analysis_packet` function: normal operation, insufficient
data, DHW/heating, competing causes, rejected advice, experiment, gas, and a long
review. Materialize it without network access:

```sh
python -m zont_analyzer.evaluation --dataset-dir /tmp/zont-eval-v1
```

Saved provider responses must be native `AnalysisResult` JSON files named after
the case (`normal.json`, `gas.json`, and so on). Optional `<case>.meta.json` files
record measured `input_tokens`, `output_tokens`, and `latency_ms`; no generation
is performed by the evaluator. Produce a report with:

```sh
python -m zont_analyzer.evaluation --dataset-dir /tmp/zont-eval-v1 \
  --responses-dir ./saved-responses --output ./evaluation-report.json \
  --model-id candidate-model --prompt-id prompt-v4 --schema-id schema-v3
```

The report records the dataset SHA, model/prompt/schema identifiers, packet paths
and deterministic schema/evidence/forbidden-claim checks. Advice quality and
uncertainty remain explicit manual-assessment fields. The tool never selects a
model because it is newer or more expensive.

Run this command inside the project test container; the host Python environment
is not part of the development workflow. Reports can be aligned with
`--compare report-a.json report-b.json`; a recommendation remains blocked until
each model has an assessment containing an assessor, ISO date, and rationale.

For the periodic review loader, store the completed local assessment artifact as
JSON with the current `dataset_sha256`, `prompt_id`, and `schema_id`, plus the
model's `completed_case_ids`, normalized `scores` for `factual`, `advice`, and
`uncertainty`, and measured `parameters`/token/latency fields. The loader accepts
only a complete current artifact; a missing case, stale packet hash, or omitted
manual rationale leaves the candidate marked as requiring evaluation.

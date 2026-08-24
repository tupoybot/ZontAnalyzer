# ZontAnalyzer — implementation plan

Updated: 2026-08-24

This is the near-term execution plan for the next ZontAnalyzer development cycle. Work from `fix/dhw-analysis-feedback`.

`feature/sensor-aware-pza-analysis` contains no unique work and must not be used as a base.

## Baseline that must be preserved

The current branch already contains working functionality that is part of the baseline, not work to redo:

- DHW/heating interaction analysis: historical per-circuit mode/target, DHW episodes, heating pause/return, ambiguous concurrent OT flags, residual heat and quality guards.
- Read-only `raw_events` ingestion, normalized `SourceEvent` persistence and reliability analysis.
- Reliability classification that separates intrinsic boiler/adapter communication loss from main-power loss and ZONT restart; uptime/MTBF/MTBR and telemetry freshness are already available to reports and OpenAI.
- Existing recommendation feedback: `applied` / `rejected` plus owner note.
- OpenAI adapter pattern: strict API response schema is separate from application domain validation. Preserve this separation when the AI contract grows.
- ZONT is permanently read-only. Never add control endpoints, tools or automatic equipment changes.

## Architecture rule for this cycle: AI-first reasoning

Before feature implementation, update `architecture.md` section 2.1 so it reflects the intended design:

- deterministic code is the trustworthy data plane: discovery, normalization, provenance, quality checks, temporal alignment, exclusions and simple reproducible math;
- OpenAI is the primary reasoning layer for multi-factor diagnosis, competing hypotheses, prediction/counterfactual analysis and choosing the next safe experiment;
- do not build a large local expert system of heating/PZA/hydraulic thresholds;
- do not send uncontrolled raw telemetry to OpenAI either: send bounded temporal evidence that preserves the shape of the relevant dynamics;
- keep epistemic levels explicit: observed → derived → inferred → predicted;
- every AI conclusion must cite supplied evidence; prediction must never be rendered as measured fact.

This architecture update is documentation of an already agreed design, not a feature stage.

---

## Preflight — confirm the real current ZONT contract

This is a short prerequisite, not a project phase and not a reason to redo the existing application.

### Work

1. Confirm the branch is green with the normal local checks.
2. Run one real read-only `discover` and a small sync against the current installation.
3. Capture only the sanitized fragments needed to establish:
   - heating circuit → control temperature sensor linkage;
   - radio thermohygrometer history source and actual metric keys;
   - humidity unit/shape;
   - external return sensor identity;
   - battery/RSSI/quality fields only if they really exist.
4. Turn those fragments into test fixtures.

### Acceptance criteria

- Existing tests/lint/types/build remain green.
- The real control-room sensor linkage is known from configuration, not guessed from names/order.
- The real radio-sensor history shape is known.
- The external return-temperature series is identified.
- Sanitized fixtures reproduce discovery/normalization without live ZONT access.

If these facts are already available in existing sanitized fixtures, skip the live discovery and proceed.

---

## Stage 1 — Sensor semantics and multi-sensor ingestion

### Goal

Make ZontAnalyzer understand the new sensors correctly before adding new intelligence.

### Work

1. Replace the ambiguous single-room assumption with explicit semantic roles:
   - `control_indoor_temperature` — sensor actually linked to the heating circuit;
   - `room_temperature` — other habitable rooms;
   - `technical_temperature` — boiler room / non-living sensors where known;
   - existing outdoor/flow/return/DHW roles.
2. Harden the existing heating-circuit sensor linkage logic; configuration linkage beats display-name heuristics.
3. Add the actual radio-sensor history type discovered in Preflight.
4. Add humidity and, only if useful and actually present, battery/signal quality.
5. Keep external return temperature distinct from boiler-reported `rwt`; preserve source/provenance if both exist.
6. Expose compact sensor identity/provenance in report context.

### Acceptance criteria

- The current installation identifies the linked living-room sensor as the control sensor; `Котельная` is not used for comfort/PZA analysis unless explicitly linked.
- Wireless humidity is ingested with correct unit and sensor identity.
- `Обратка` is classified as return temperature with provenance.
- Unknown linkage stays unresolved/low-confidence rather than guessed.
- Tests cover sensor-order independence, radio normalization, humidity, return sensor and technical-vs-room separation.
- Existing DHW and reliability regression tests stay green.

---

## Stage 2 — Heating evidence layer for OpenAI

### Goal

Build a bounded, trustworthy temporal evidence packet rich enough for AI reasoning without turning Python into a heating expert system.

### Work

1. Align and summarize these signals when available:
   - control-room temperature + historical target;
   - additional room temperatures;
   - outdoor temperature;
   - calculated flow target (`cs`);
   - actual flow temperature;
   - external return temperature and ΔT;
   - burner activity/modulation;
   - heating request/availability/context;
   - DHW priority/interference windows;
   - reliability and stale-telemetry intervals.
2. Reuse existing exclusions:
   - automatic summer/off;
   - mode/target transitions;
   - DHW priority windows;
   - known telemetry/reliability gaps.
3. Produce bounded temporal evidence: e.g. hourly buckets plus selected representative heating episodes. Preserve shape; do not collapse everything into a single correlation number.
4. Give evidence windows stable IDs and provenance.
5. Deterministically calculate only reproducible facts: means/ranges, target error, ΔT, lags, coverage, weather range and similar basics.
6. Track quality independently for control room, weather, flow/return and additional rooms.

### Acceptance criteria

- AI can see how room error changes with weather/heating behavior, not only aggregate coefficients.
- Every AI-visible number has observed/derived provenance.
- Contaminated windows are explicitly marked/excluded.
- Packet size is bounded for daily/weekly/monthly analysis and respects token-budget constraints.
- No local code declares PZA slope/offset/hydraulic imbalance at this stage.
- Tests cover temporal alignment, exclusions, ΔT, quality domains and stable evidence IDs.

---

## Stage 3 — AI reasoning contract: hypotheses, predictions and experiments

### Goal

Promote OpenAI from a commentator of local metrics to the main reasoning layer.

### Work

1. Expand structured output beyond `summary + recommendations` with optional:
   - `observed_patterns`;
   - `hypotheses`;
   - `predictions`;
   - `unknowns`;
   - `recommended_experiment`.
2. Hypotheses must support confidence, evidence for/against and competing explanations.
3. Predictions must contain scenario, expected direction/effect, confidence, assumptions, evidence and validation plan.
4. Keep the existing strict API-schema → domain-validation pattern.
5. Update `SYSTEM_PROMPT` to:
   - reason over temporal evidence instead of fixed thresholds;
   - compare competing explanations;
   - distinguish observed / derived / inferred / predicted;
   - preserve DHW and reliability semantics already implemented;
   - use owner feedback as authoritative manual context;
   - prefer one minimally invasive next experiment when evidence is ambiguous;
   - never invent flowmeter positions, room/loop mapping or measured hydraulic flow.
6. Extend evidence-reference validation to temporal evidence IDs.
7. Keep `store=False`, token budget and no tools/write access.

### Acceptance criteria

- Structured output can represent multiple competing hypotheses without forcing a recommendation.
- `unknown` / insufficient evidence is a successful result.
- Predictions are unmistakably forecasts, never measured facts.
- Power/restart reliability incidents cannot be described as intrinsic boiler failures.
- Invalid evidence references fail validation.
- Mocked tests cover schema/validation; at most one real OpenAI smoke request is needed for the stage.

---

## Stage 4 — PZA analysis and counterfactual prediction

### Goal

Answer the useful questions about the current weather-compensation behavior without hard-coding a local PZA expert system.

### Work

1. Discover current readable PZA parameters from ZONT configuration if available; never guess field names.
2. Supply comparable heating windows across weather conditions so AI can rank hypotheses such as:
   - overall curve/offset mismatch;
   - slope mismatch;
   - PID / floor-heating inertia;
   - solar/internal gains;
   - local imbalance instead of whole-house PZA error;
   - insufficient data/weather range.
3. Support counterfactual questions such as “what is likely to happen after a small PZA change?”. Qualitative estimates are acceptable; numeric estimates must be explicitly predictions.
4. `recommended_experiment` may suggest one safe manual PZA change only: one variable, small step, observation period, success criteria and rollback/stop conditions.
5. If evidence/weather range is insufficient, ask for more observation instead of inventing precision.

### Acceptance criteria

- Constant room error over weather does not automatically become a slope diagnosis.
- Weather-dependent error can rank slope mismatch while preserving alternatives.
- Daytime overheating without heating activity can rank external/solar gains above PZA overheating.
- Narrow weather range returns insufficient evidence.
- Report can answer a small-change counterfactual with assumptions/confidence.
- There remains no automatic ZONT change path.

---

## Stage 5 — Multi-room reasoning and hydraulic balancing

### Goal

After a second-floor living-room sensor exists and enough real heating data has accumulated, distinguish whole-house control errors from local room/floor imbalance.

### Work

1. Compare control room and additional living rooms over comparable heating windows.
2. Provide target error, warm-up/decay behavior, weather, flow/return/ΔT and heating activity to AI.
3. Let AI rank competing explanations: hydraulic distribution, different heat loss, solar/internal gains, sensor bias/placement and insufficient data.
4. Only suggest a flowmeter/balancing experiment if the room/floor can be mapped to a known hydraulic branch/loop or the owner supplies that mapping.
5. Unknown mapping must produce `needs_manual_context`.
6. Never change PZA and hydraulics in the same experiment.

### Acceptance criteria

- No second living-room sensor → no flowmeter recommendation.
- Local persistent imbalance can be distinguished from a whole-house deficit.
- “Likely hydraulic imbalance” does not imply that actual flow was measured.
- A balancing experiment changes one known branch/group by a small step and waits for floor-heating thermal response before evaluation.
- Unknown loop mapping never becomes a guessed instruction.

---

## Stage 6 — Learn from interventions and release

### Goal

Use the existing feedback lifecycle to make future reasoning specific to this house, then release only after regression/eval proof.

### Work

1. Extend applied feedback with optional structured experiment context: category/parameter, before/after value, timestamp and owner note.
2. Build pre/post evidence windows normalized as far as practical for weather, mode, DHW interference and data quality.
3. Feed prior prediction + observed outcome to later AI analysis.
4. Let AI assess whether the result supports, contradicts or fails to distinguish the original hypothesis.
5. Add weekly/monthly house-specific context: thermal inertia, typical weather response, usual ΔT and room-to-room behavior.
6. Add AI eval scenarios for:
   - constant offset vs weather-dependent error;
   - one-floor imbalance vs whole-house deficit;
   - solar gain/no heating activity;
   - insufficient data;
   - DHW interruption;
   - intrinsic OT loss vs power/restart;
   - successful and unsuccessful manual experiments.
7. Keep report compact: current sensor context, DHW/reliability summary, important AI patterns, ranked hypotheses, useful prediction and one preferred next experiment or “do nothing”.

### Acceptance criteria

- Applied experiments can be represented structurally, not only parsed from free text.
- Subsequent AI explicitly compares before/after and names confounders.
- Rejected recommendations are not repeated without materially new contrary evidence.
- Weekly/monthly output captures stable behavior of this house rather than just stretching daily metrics over a larger period.
- Existing DHW and reliability regression tests remain green.
- No AI inference/prediction is rendered as measured fact.
- `pytest`, `ruff check .`, `mypy src/zont_analyzer`, `python -m build` pass and CI is green.
- Real `run --once` succeeds and the new sensor context renders correctly.
- At least one real report is manually judged more useful than the current baseline before merge to `main`.

---

## Execution rules

- Preflight is only contract verification; do not treat it as reimplementation of P0.
- Complete stages in order unless an acceptance criterion is explicitly waived with a documented reason.
- Prefer small commits aligned to stages/substages; avoid unrelated refactoring.
- Deterministic code prepares trustworthy evidence and basic math. OpenAI performs multi-factor reasoning, hypothesis ranking and prediction.
- Do not replace AI reasoning with a large collection of local expert thresholds.
- Do not blindly send long raw telemetry to OpenAI; preserve dynamics in bounded evidence windows.
- Preserve all DHW and reliability semantics already implemented on `fix/dhw-analysis-feedback`.
- One manual experiment changes one variable. ZontAnalyzer never performs the change itself.

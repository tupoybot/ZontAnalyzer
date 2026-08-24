# ZontAnalyzer — implementation plan

Updated: 2026-08-24

This is the near-term execution plan for the next ZontAnalyzer development cycle. It is intentionally more concrete than `architecture.md` and more detailed than `roadmap.md`.

Work from `fix/dhw-analysis-feedback`. Do not base the next implementation on the older `feature/sensor-aware-pza-analysis` branch.

## Starting point that must be preserved

The current branch is already materially ahead of `main` and these changes are part of the baseline, not work to redo:

- DHW/heating interaction analysis from the P1 vertical slice remains authoritative: historical per-circuit mode/target, DHW episodes, heating pause/return, ambiguous concurrent OT flags, residual heat and quality guards.
- Raw ZONT reliability events are now ingested read-only via `raw_events`, normalized as `SourceEvent`, persisted in SQLite and covered by migration/tests.
- `analytics/reliability.py` distinguishes boiler/adapter communication loss from main-power loss and ZONT restart, derives ZONT/boiler uptime and MTBF/MTBR only from appropriate intervals, and handles stale telemetry explicitly.
- Reliability context is already rendered and passed to OpenAI; power/restart incidents must not be reclassified as boiler failures by future AI logic.
- The OpenAI adapter now uses a separate strict API response schema and then validates into application domain models. Keep that separation when the AI output contract grows.
- Existing recommendation feedback (`applied` / `rejected` + owner note) remains part of the next AI packet and should become the basis for learning from manual experiments.
- Read-only is immutable: ZontAnalyzer may read ZONT, but must never add equipment-control endpoints or tools.

Before each stage: run the existing test suite and keep the branch green. Do not mix unrelated refactoring into feature commits.

---

## Stage 0 — Stabilize the current branch and capture the real installation contract

### Goal

Turn the current branch into a trustworthy baseline and capture the actual current ZONT configuration after installation of the radio module, wireless thermohygrometer and external return sensor.

### Work

1. Run the current branch through the complete local checks:
   - `pytest`
   - `ruff check .`
   - `mypy src/zont_analyzer`
   - `python -m build`
2. Run one real read-only `discover` / sync against the current installation.
3. Capture a sanitized fixture containing only the parts needed to understand:
   - heating circuit → control temperature sensor linkage;
   - radio sensor structure and history data type;
   - temperature and humidity metric keys;
   - external return sensor identity;
   - any battery/RSSI/quality fields that are actually present.
4. Verify that current reliability source-event ingestion still works after the latest hardware/config changes.
5. Record the discovered contract in tests/fixtures or a small `.codex` note. Do not document secrets or full raw device payloads.

### Acceptance criteria

- All existing tests/lint/types/build pass before feature work starts.
- One real `sync` completes without degrading existing DHW/reliability analysis.
- We can name, from evidence rather than guesswork, the ZONT field that identifies the control-room sensor.
- We know the actual history source/metric names for the new radio thermohygrometer.
- We know which real series corresponds to the external return sensor.
- Sanitized fixtures are sufficient to reproduce discovery/normalization in tests without live ZONT access.

---

## Stage 1 — Sensor semantics: control room, additional rooms, humidity and return temperature

### Goal

Make the data model understand what each new sensor means before doing any smarter analysis.

### Work

1. Replace the current ambiguous single `indoor_temperature` assumption with explicit semantic roles:
   - `control_indoor_temperature` — the sensor actually linked to the heating circuit;
   - `room_temperature` — additional habitable-room sensors;
   - `technical_temperature` — boiler room and other non-living sensors where known;
   - existing `outdoor_temperature`, `flow_temperature`, `return_temperature`, `dhw_temperature`.
2. Reuse and harden `_linked_indoor_sensor_ids()` rather than replacing it with name heuristics. The heating-circuit linkage wins over display-name guessing.
3. Add the actual radio-sensor history source discovered in Stage 0 to ingestion.
4. Add semantic roles/units for the fields that really exist, at minimum:
   - relative humidity (`%RH`);
   - optionally battery and signal quality if exposed and useful.
5. Ensure the external sensor named/linked as return is preferred as a separate measured source; do not silently conflate it with boiler `rwt` if both exist.
6. Persist provenance/confidence so the report and AI packet can tell how a role was determined.
7. Update text/HTML report with a compact sensor identity section, not a dump of all series.

### Acceptance criteria

- On the current installation the report explicitly says that the heating control sensor is the actual linked room sensor (currently expected to be `Гостиная`), not `Котельная` merely because it appeared first.
- `Котельная` is not used for comfort/PZA conclusions unless it is explicitly linked as control sensor.
- Wireless humidity appears with correct unit and sensor name.
- `Обратка` is classified as return temperature with provenance.
- If role linkage cannot be proven, the result is low-confidence/unresolved rather than guessed.
- Unit tests cover control-sensor selection independent of series order, radio sensor normalization, humidity, return sensor, and technical-vs-room separation.
- Existing DHW and reliability tests remain green.

---

## Stage 2 — Build an AI-ready heating evidence layer, not a rule engine

### Goal

Give OpenAI enough trustworthy temporal structure to reason about PZA, room dynamics and hydraulics without hard-coding a large expert-system tree in Python.

### Work

1. Add a deterministic `heating_evidence` layer that aligns and summarizes:
   - control-room temperature and historical target;
   - outdoor temperature;
   - calculated flow target (`cs`) where available;
   - actual flow temperature;
   - external return temperature and ΔT;
   - burner activity/modulation;
   - heating availability/request/context;
   - DHW priority windows;
   - reliability/telemetry gaps;
   - additional room temperatures.
2. Reuse existing exclusions/context instead of reinventing them:
   - automatic summer/off windows;
   - mode/target transition windows;
   - DHW interaction windows;
   - stale telemetry and boiler/ZONT reliability incidents.
3. Create representative time windows/buckets for AI. Prefer a compact shape that preserves dynamics, for example hourly summaries plus selected heating episodes, rather than raw minute telemetry for 90 days.
4. Give each summarized window a stable evidence ID and provenance so AI outputs can cite it.
5. Deterministic code may calculate simple facts (means, ranges, ΔT, target error, lags, coverage, weather range). It must not decide by itself that a PZA slope is “too high”, a room is “hydraulically starved”, etc.
6. Add domain-specific quality blocks: room/control quality, outdoor quality, flow/return quality and weather-range sufficiency. A good room series must not hide a bad return series.

### Acceptance criteria

- An analysis packet can show the shape of room error versus weather and heating behavior, not just one correlation coefficient.
- Every AI-visible number is either an observed value or deterministic derived fact with evidence/provenance.
- Periods contaminated by summer/off, DHW priority, control transitions, stale telemetry or known connection losses are identifiable in the packet.
- The packet remains bounded in size for daily/weekly/monthly use and respects the configured token budget.
- No new PZA/hydraulic recommendations are produced locally at this stage.
- Tests cover temporal alignment, exclusions, ΔT, multiple quality domains and stable evidence-window IDs.

---

## Stage 3 — Expand OpenAI from commentator to engineering reasoning layer

### Goal

Make OpenAI the primary reasoning layer for multi-factor diagnosis, hypothesis ranking and prediction while keeping facts and safety deterministic.

### Work

1. Expand the structured AI response beyond `summary + recommendations` with optional sections such as:
   - `observed_patterns`;
   - `hypotheses`;
   - `predictions`;
   - `unknowns`;
   - `recommended_experiment`.
2. A hypothesis should contain at least:
   - title/explanation;
   - confidence;
   - evidence for;
   - evidence against;
   - competing explanations.
3. A prediction should contain:
   - scenario/change being considered;
   - expected direction/effect;
   - confidence;
   - assumptions;
   - evidence;
   - validation plan.
4. Preserve the current strict-response-wrapper pattern: API schema stays isolated from application-only validators, then converts to domain models.
5. Update `SYSTEM_PROMPT` so the model:
   - reasons over the supplied temporal evidence, not fixed thresholds;
   - separates observed / derived / inferred / predicted;
   - explicitly compares competing explanations;
   - uses DHW and reliability contexts already implemented on this branch;
   - treats owner feedback as authoritative manual context;
   - chooses one minimally invasive next experiment when evidence is ambiguous;
   - never invents flowmeter positions, sensor locations or observed hydraulic flow.
6. Keep all existing evidence-ID validation and extend it to evidence-window IDs.
7. Keep `store=False`, token budget controls and no tools/write access.

### Acceptance criteria

- Structured output can represent at least two competing hypotheses without forcing a recommendation.
- A prediction is clearly labeled as a forecast and cannot be rendered as an observed fact.
- Unknown/insufficient-data is a valid successful AI result.
- Reliability events classified as `power_outage` or `zont_restart` are not described as boiler failures.
- DHW summer/off behavior remains correctly interpreted.
- Invalid/unknown evidence references fail validation.
- Mocked tests cover the expanded schema; one real API smoke test is sufficient for the stage (respect `AGENTS.md` API-call limits).

---

## Stage 4 — PZA diagnosis and counterfactual prediction

### Goal

Use the richer AI context to answer the useful engineering questions: whether the current PZA behavior fits the house, what alternative explanations exist, and what a small manual change is expected to do.

### Work

1. Discover current readable PZA parameters from ZONT config if they are actually available. Do not guess field names or infer settings from output temperature alone.
2. Feed the model enough comparable heating windows over different outdoor conditions to distinguish, as hypotheses rather than hard-coded rules:
   - overall curve/offset mismatch;
   - slope mismatch;
   - PID/thermal-inertia effects;
   - solar/internal gains;
   - insufficient weather range/data;
   - local room imbalance rather than whole-house PZA error.
3. Allow qualitative predictions by default. Numeric effect estimates are allowed only when evidence supports them and must remain explicitly estimated.
4. `recommended_experiment` may propose one safe manual PZA change with:
   - one variable only;
   - small step;
   - observation period;
   - success criteria;
   - rollback/stop conditions.
5. Do not recommend a slope change when the available outdoor range or heating-active duration is too narrow to support it; the AI should say what additional data would discriminate hypotheses.

### Acceptance criteria

- Eval scenario: constant room error across weather does not automatically become a slope diagnosis.
- Eval scenario: error that changes systematically with colder weather can rank slope mismatch above a constant offset, while still showing alternatives.
- Eval scenario: daytime overheating with little/no heating activity can rank external/solar gains above PZA overheating.
- Eval scenario: narrow outdoor range returns “insufficient evidence” rather than false precision.
- The report can answer “what is likely to happen if I change the PZA setting slightly?” with assumptions and confidence.
- No automatic ZONT change path exists; all actions are manual suggestions only.

---

## Stage 5 — Multi-room analysis and flowmeter/balancing recommendations

### Goal

After the second-floor room sensor is connected and enough heating data exists, distinguish whole-house control problems from persistent local room/floor imbalance.

### Work

1. Treat the control room and additional living rooms as separate series with names/provenance.
2. Build comparable heating windows for room-to-room response:
   - target error;
   - warm-up/decay response;
   - outdoor temperature;
   - supply/return/ΔT;
   - heating activity;
   - exclusions from Stages 2–4.
3. Let AI compare competing explanations for a persistent room difference:
   - hydraulic distribution;
   - different heat loss;
   - solar/internal gains;
   - sensor placement/bias;
   - insufficient data.
4. Only suggest a flowmeter experiment when the relevant room/floor can be mapped to a known hydraulic branch/loop or the user supplies that mapping.
5. If mapping is unknown, ask for manual context rather than inventing which flowmeter to turn.
6. Never suggest changing PZA and hydraulic balancing in the same experiment.

### Acceptance criteria

- Before a second living-room sensor exists, no flowmeter recommendation is generated.
- A persistent difference confined to one room/floor can be distinguished from a similar error in all rooms.
- The report can say “likely local imbalance” without pretending that actual water flow was measured.
- Any suggested balancing experiment changes one known branch/group by a small manual step and waits at least one thermal-response period (normally 24–48 h for floor heating) before evaluation.
- Unknown loop mapping yields `needs_manual_context`, not a guessed valve/flowmeter instruction.

---

## Stage 6 — Learn from interventions and build a house-specific model

### Goal

Make ZontAnalyzer progressively more useful for this specific house instead of repeatedly applying generic heating advice.

### Work

1. Extend the existing recommendation feedback lifecycle so an applied experiment can optionally store structured manual context:
   - parameter/category changed;
   - before/after value when known;
   - timestamp;
   - owner note.
2. Build before/after evidence windows normalized as far as practical for weather, operating mode, DHW interference and data quality.
3. Feed prior predictions and outcomes into subsequent AI analysis.
4. Let AI assess whether the observed post-change behavior:
   - supports the original hypothesis;
   - contradicts it;
   - is inconclusive because conditions changed.
5. Add weekly/monthly summaries that preserve learned house behavior: thermal inertia, typical room response, weather-dependent errors, usual ΔT range, and room-to-room differences.
6. Do not call this model training. It is structured history + retrieval/context for reasoning.

### Acceptance criteria

- An applied PZA experiment can be represented without free-text-only parsing.
- Subsequent analysis compares pre/post periods and explicitly states confounders.
- A rejected recommendation is not repeated without materially new contrary evidence.
- A successful previous experiment influences the next prediction/recommendation.
- Weekly/monthly reports can describe stable house-specific patterns rather than merely re-running daily metrics over a larger interval.

---

## Stage 7 — Report UX, evals and release gate

### Goal

Make the new intelligence useful without turning the HTML report into a telemetry dashboard, then prove it does not regress the existing DHW/reliability work.

### Work

1. Keep the report compact and layered:
   - current sensor/control context;
   - indoor temperature + humidity;
   - outdoor / flow / return / ΔT;
   - DHW and reliability summary;
   - important AI-observed patterns;
   - ranked hypotheses;
   - prediction if useful;
   - one preferred next experiment or “do nothing”.
2. Add a small AI eval suite covering at minimum:
   - constant offset vs weather-dependent error;
   - one-floor imbalance vs whole-house deficit;
   - solar gain/no heating activity;
   - insufficient data/weather range;
   - DHW interruption;
   - boiler connection loss caused by power/restart vs intrinsic OT loss;
   - an applied experiment that improved behavior;
   - an applied experiment that made it worse.
3. Eval assertions should focus on epistemic/safety behavior and ranking, not exact prose.
4. Run full regression and one real `run --once` / report generation on the deployment before merge.

### Acceptance criteria

- Existing DHW and reliability regression tests remain green.
- No report section presents AI inference/prediction as measured fact.
- No control/write endpoint or model tool exists.
- `pytest`, `ruff check .`, `mypy src/zont_analyzer`, `python -m build` all pass.
- CI is green.
- Real `run --once` completes, latest HTML renders the new sensor context correctly, and no duplicate/unbounded AI calls occur.
- At least one real report is manually reviewed and judged more useful than the current baseline before merging to `main`.

---

## Execution rules

- Complete stages in order. Do not start the next stage until the previous acceptance criteria are met or explicitly waived with a documented reason.
- Prefer small commits that correspond to one stage/substage.
- Use deterministic code for trustworthy data preparation and basic math; use OpenAI for multi-factor reasoning, hypothesis ranking and prediction.
- Do not replace AI reasoning with a large collection of local “expert” thresholds.
- Do not send raw long-term telemetry blindly to OpenAI; send compact temporal evidence that preserves the shape needed for reasoning.
- Preserve DHW and reliability semantics already implemented on `fix/dhw-analysis-feedback`.
- One manual experiment changes one variable. ZontAnalyzer never performs the change itself.

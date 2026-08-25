# ZontAnalyzer — cloud-first / AI-first target architecture

Status: architecture branch draft  
Branch: `architecture/cloud-first-serverless`  
Base: `fix/dhw-analysis-feedback`  

This document captures the architectural direction discussed after the DHW/reliability work. It is deliberately separate from the main implementation plan so the cloud migration can evolve in parallel with product/analytics work.

## 1. Product principle

ZontAnalyzer remains a read-only assistant for one real house. The interesting part of the product is not infrastructure by itself, but the ability to understand the behavior of that specific heating system over time and help the owner choose safe manual experiments.

The project therefore uses two planes:

### Trustworthy data plane — deterministic code

Responsible for things that must be reproducible and auditable:

- read-only ZONT discovery/config/history/events;
- canonical telemetry storage;
- sensor identity, semantic roles, provenance and confidence;
- timestamp alignment and data-quality checks;
- heating/DHW/reliability state segmentation;
- exclusion windows for stale data, outages, summer/off, DHW priority and control transitions;
- simple verifiable calculations such as means, ranges, target error, duty cycle, ΔT, durations and coverage;
- compact temporal evidence windows for reasoning;
- evidence IDs and safety guards.

This layer must not grow into a large local heating expert system made of fixed thresholds and `if/else` rules for every possible diagnosis.

### Reasoning plane — OpenAI

Responsible for problems where multi-factor reasoning is more useful than hand-written heuristics:

- discovering and explaining patterns across weather, control state, room response, supply/return and DHW;
- comparing competing engineering hypotheses;
- ranking likely causes while preserving uncertainty;
- counterfactual/predictive reasoning about small manual changes;
- selecting the most informative next experiment;
- learning from structured history of prior owner-applied interventions;
- building a house-specific picture over weeks and seasons.

AI output must distinguish four epistemic levels:

1. **observed** — directly read/measured;
2. **derived** — deterministic math over observed data;
3. **inferred** — model reasoning/hypothesis;
4. **predicted** — forecast/counterfactual estimate.

Inference and prediction never become measured fact just because they are stored in a report.

## 2. Why cloud-first

For this pet project, the operational shape is bursty rather than continuously busy: periodic synchronization, periodic analysis, occasional OpenAI reasoning and report publication. A permanently running VPS/container is therefore not the only natural deployment model.

The target cloud design should preserve the current application-level strengths (`run --once`, idempotent sync/catch-up, read-only API, persisted cursors/reports) and map them onto managed/serverless services instead of rebuilding the whole application as microservices.

Cloud-first here means:

- managed/serverless services are the preferred production target;
- local Docker + SQLite remain first-class developer/test modes where useful;
- infrastructure is declared as code;
- the application stays portable enough that analytics/domain code does not know about Yandex Cloud;
- cloud migration is incremental and reversible until cutover.

It does **not** mean splitting every operation into a separate function or service.

## 3. Initial target on Yandex Cloud

Preferred baseline target:

```text
Timer Trigger
    |
    v
Serverless Container
`zont-analyzer run --once`
    |
    +--> ZONT read-only API
    +--> YDB Serverless
    +--> OpenAI API
    +--> Object Storage (published HTML/JSON artifacts)
    +--> Lockbox (runtime secrets)
    +--> Monium (metrics/logging/alerts)
```

Optional services only when they solve a real problem:

- API Gateway — feedback/status/read endpoints;
- Postbox — email summaries/alerts;
- Workflows/EventRouter — orchestration after a single `run --once` path is proven insufficient;
- Message Queue — asynchronous decoupling only if retries/concurrency justify it;
- Yandex Query + Object Storage — later analytical archive/data-lake use;
- Cloud Functions — useful for truly small isolated handlers, but not the default runtime for the existing packaged application.

The first cloud version should remain one deployable application, not a distributed microservice rewrite.

## 4. Compute model

The preferred execution primitive is a Serverless Container invocation of an idempotent one-shot use case.

```text
timer -> container -> run --once -> exit
```

The existing long-lived `run` worker remains valid for local/VPS deployments, but cloud production should not depend on a permanently alive process.

Important consequences:

- all durable state must live outside the container;
- local filesystem is scratch space only;
- overlapping invocations are possible and must be safe;
- job claiming/idempotency must be persisted transactionally;
- graceful retries must not duplicate reports, interventions or AI calls.

Do not add a function that merely invokes the container. The scheduler should call the execution target directly.

## 5. Durable storage model

### YDB Serverless

Candidate primary cloud state store for:

- devices/entities/config snapshots;
- telemetry series and samples;
- source/reliability events;
- sync cursors and job/idempotency state;
- reports and evidence metadata;
- recommendations/interventions/owner feedback;
- OpenAI usage ledger and call metadata.

YDB is not treated as “SQLite with another URL”. A dedicated storage adapter/repository implementation is required. SQL/transaction/index assumptions must be tested explicitly.

SQLite remains useful for:

- local development;
- fast unit/integration tests;
- offline single-machine operation;
- comparison/reference backend during migration.

Application/domain/analytics code should depend on repository contracts, not on YDB or SQLite details.

### Object Storage

Use for immutable/published artifacts rather than transactional state:

- `latest.html` publication target;
- dated HTML/JSON exports;
- optional long-term exports/Parquet later;
- sanitized fixtures or migration exports when appropriate.

Do not run the live SQLite database from an object-storage mount.

## 6. Concurrency and scheduling

Serverless execution makes “exactly one forever-running worker” the wrong invariant. Instead use atomic job ownership/idempotency.

Conceptually:

```text
job key = <job type, period/input revision, algorithm version>

claim atomically
  -> already completed: no-op
  -> actively leased: no-op/retry later
  -> claim acquired: execute
```

A crash/timeout must leave enough persisted state for a later invocation to resume safely.

Existing cursor/catch-up/idempotent-report behavior should be reused rather than replaced.

## 7. Secrets and IAM

Cloud runtime should use service-account/IAM permissions scoped per resource.

Secrets such as ZONT token/client identity and OpenAI API key should move to a managed secret store for cloud execution. They must never be written into image layers, Terraform state in plaintext, report artifacts or logs.

Keep the existing local secret-file/env path for developer mode.

## 8. Observability

Cloud observability is part of the architecture, not an afterthought.

Minimum application metrics should include:

- last successful sync / sync lag;
- latest telemetry age;
- completed/failed windows;
- analysis duration;
- report generation status;
- OpenAI calls/tokens/failures;
- ZONT/controller/boiler reliability metrics already derived by the application;
- container invocation failures/timeouts.

Infrastructure health and application-level facts must remain separate concepts. A healthy container invocation does not prove fresh ZONT telemetry.

## 9. Report publication and feedback

The first serverless migration should preserve the current report model.

Recommended split:

- canonical report JSON/state in YDB;
- rendered standalone HTML/JSON export in Object Storage;
- stable pointer/object for latest report.

Interactive feedback can later be exposed through a very small API Gateway + handler/container path that writes only ZontAnalyzer-owned state (`applied`, `rejected`, owner notes). It must never become a control plane for ZONT/boiler equipment.

## 10. AI packet in the cloud architecture

Cloud migration must not regress the AI-first product direction.

The packet sent to OpenAI should be built from the same deterministic evidence layer regardless of storage/runtime backend. It should include bounded temporal structure, not blindly upload raw minute telemetry.

Desired reasoning inputs include:

- active control-room sensor and target;
- additional room temperatures/humidity;
- outdoor temperature;
- calculated and actual flow temperature;
- return temperature and ΔT;
- heating activity/modulation/context;
- DHW interaction windows;
- reliability/staleness context;
- previous manual interventions and their outcomes;
- representative evidence windows preserving temporal shape.

The cloud backend is an infrastructure concern; it must not change the epistemic contract of the analyst.

## 11. Parallel-development rule

The cloud branch is a **parallel architecture track**, not a prerequisite for ongoing sensor/PZA/AI development.

Main analytics/product work may continue on `fix/dhw-analysis-feedback` or its successor while this branch builds the cloud skeleton.

The two tracks should integrate through stable seams:

- repository/storage interfaces;
- one-shot application use cases;
- report/evidence DTOs;
- configuration/secrets abstraction;
- publisher abstraction.

Avoid long-lived divergence. Periodically rebase/merge the current product baseline into the cloud branch, but do not force feature work to wait for YDB/Terraform.

## 12. Migration philosophy

Preferred order:

1. prove the cloud skeleton with existing behavior;
2. validate the YDB storage adapter against representative real workloads;
3. run local/VPS and cloud paths in parallel where practical;
4. compare produced state/reports;
5. cut over only after correctness, cost and operability are understood;
6. keep rollback possible until the cloud path has survived normal operation and failures.

The purpose is to learn and simplify operations, not to perform a big-bang rewrite.

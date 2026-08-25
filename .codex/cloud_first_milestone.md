# ZontAnalyzer — Cloud-first milestone

Status: parallel development track  
Branch: `architecture/cloud-first-serverless`  
Base: `fix/dhw-analysis-feedback`  
Architecture draft: [`cloud_first_architecture.md`](./cloud_first_architecture.md)

This milestone is intentionally separate from the product implementation plan. Sensor semantics, PZA reasoning and AI work may continue in parallel. The cloud track builds infrastructure seams and a deployable skeleton first; migration happens only after both tracks are ready.

## M0 — Freeze architectural contracts, not product development

### Goal

Identify the seams the cloud track may depend on without forcing the analytics branch to stop evolving.

### Work

- Treat `run --once` as the primary cloud execution unit.
- Define repository/storage interfaces needed by current SQLite code.
- Identify SQLite-specific assumptions in migrations, UPSERTs, transactions, locking and report lifecycle.
- Define publisher abstraction for standalone report artifacts.
- Define runtime secret/config abstraction that supports local files/env and managed cloud secrets.
- Keep the current read-only ZONT boundary immutable.

### Acceptance criteria

- Cloud code can be developed without importing Yandex-specific SDKs into domain/analytics modules.
- No requirement is introduced that blocks ongoing sensor/PZA/AI feature work.
- Existing SQLite/local runtime remains usable and green.
- The architectural delta in `cloud_first_architecture.md` is reviewed and considered the target for this branch.

---

## M1 — Terraform/IaC skeleton and empty cloud runtime

### Goal

Create the cloud project skeleton without moving application state yet.

### Work

Provision via Terraform (or the repository's chosen IaC tool):

- service account(s) with least-privilege IAM;
- Container Registry / image target;
- Serverless Container;
- Timer Trigger invoking the container directly;
- Lockbox secret placeholders/references;
- YDB Serverless database;
- Object Storage bucket/prefix for published reports;
- Monium/monitoring resources that require explicit provisioning.

Deploy a minimal container revision that starts, proves identity/config access, emits a health/test metric and exits successfully.

Do not add Cloud Functions just to invoke the container.

### Acceptance criteria

- A clean cloud/folder can be provisioned reproducibly from IaC.
- Timer invokes the Serverless Container on schedule.
- Container runs under a scoped service account.
- No long-lived VM/VPS is required for the skeleton.
- Secrets are referenced, not embedded into image/IaC source.
- Destroying the test stack does not affect the existing production/pilot deployment.

---

## M2 — Run existing `run --once` in Serverless Container with externalized scratch/state boundaries

### Goal

Prove that the existing application can execute in an ephemeral container without yet committing to YDB as the primary DB.

### Work

- Make container startup explicitly invoke the one-shot application path.
- Audit filesystem usage: temporary files only unless mounted/published intentionally.
- Separate report rendering from local-path publication so Object Storage can become a publisher target later.
- Add invocation/job correlation IDs to logs.
- Verify timeout/retry behavior and duplicate invocation safety at application boundaries.

A temporary/dev state backend is acceptable for this stage; do not pretend ephemeral local SQLite is production-safe.

### Acceptance criteria

- The real application image starts and completes a bounded one-shot run in Serverless Containers.
- No durable correctness depends on local container filesystem surviving the invocation.
- Two overlapping invocations do not cause uncontrolled duplicate side effects.
- Failure is visible through logs/metrics and a subsequent invocation can retry safely.

---

## M3 — YDB storage spike

### Goal

Decide with evidence whether YDB should become the production state backend.

### Required representative operations

Implement/prototype enough of a YDB adapter to test:

1. telemetry sample UPSERT/idempotency;
2. time-range sample query for one/multiple series;
3. entity/series discovery queries;
4. source-event persistence/query;
5. report save/load;
6. recommendation feedback/intervention lifecycle;
7. sync cursor updates;
8. atomic job claim/lease/idempotency;
9. LLM usage ledger writes/reads.

Test with a sanitized subset/model of the real installation and realistic telemetry volume.

### Acceptance criteria

- Every required operation has a documented YDB implementation or a clearly identified blocker.
- Transaction/concurrency semantics required for job claiming are proven by tests, not assumed.
- Query shape and indexes are acceptable for daily/weekly analysis workloads.
- Cost/storage estimates are recorded from real measurements or platform calculators, not guesses.
- If YDB is rejected, the reason and alternative managed backend are documented before further migration.

---

## M4 — Dual storage adapters and contract tests

### Goal

Make storage replaceable instead of scattering YDB specifics through the application.

### Work

- Introduce/finish repository contracts where needed.
- Keep SQLite adapter for local/test mode.
- Implement YDB adapter for cloud mode.
- Run the same storage contract tests against both backends where semantics should match.
- Replace Alembic-only assumptions with backend-aware schema management while preserving safe migration discipline.

### Acceptance criteria

- Application/analytics code does not branch on `sqlite` vs `ydb` for ordinary operations.
- Core storage contract suite passes for both backends.
- Existing SQLite tests remain green.
- YDB migrations/schema initialization are reproducible and idempotent.

---

## M5 — Object Storage report publisher

### Goal

Publish reports without depending on a persistent local filesystem.

### Work

- Add a publisher abstraction for rendered artifacts.
- Implement Object Storage publisher for:
  - stable `latest.html`;
  - dated HTML;
  - canonical/portable JSON export where useful.
- Preserve canonical report state in the database; Object Storage is a presentation/export target.
- Make publication idempotent and safe under retries.

### Acceptance criteria

- A completed analysis publishes a valid standalone HTML report to Object Storage.
- Retried invocation does not create inconsistent latest/archive state.
- Report rendering output matches local renderer semantics.
- No database is run from an Object Storage mount.

---

## M6 — Cloud observability and cost guardrails

### Goal

Make the pet project safe to leave running without watching the console constantly.

### Work

Expose/collect at minimum:

- sync success/failure and lag;
- latest telemetry age;
- analysis/report duration;
- container invocation failure/timeout;
- OpenAI call count/tokens/failures;
- application reliability metrics already derived from ZONT events;
- YDB/storage errors;
- report publication failures.

Add practical billing/cost guardrails and document expected idle/normal behavior.

### Acceptance criteria

- A failed sync and stale telemetry are distinguishable from healthy container execution.
- At least one alert path is tested end-to-end.
- Unexpected invocation loops or token spikes are observable.
- Monthly cost guardrails/alerts are configured or explicitly documented.

---

## M7 — Optional cloud UX services

### Goal

Add managed services only where they simplify a real use case.

Candidate additions:

- API Gateway for feedback/status endpoints;
- Postbox for weekly/important email summaries;
- Workflows/EventRouter if orchestration becomes clearer than a single `run --once` transaction;
- Message Queue if asynchronous retries/backpressure become useful;
- Yandex Query + Object Storage for long-term analytical archive.

Cloud Functions are allowed for isolated small handlers if they are genuinely simpler than extending the main container. They are not the default execution runtime.

### Acceptance criteria

- Every added service has a concrete product/operational reason.
- No service is introduced only to make the architecture look more “cloud-native”.
- Feedback endpoints can mutate only ZontAnalyzer-owned state, never ZONT/boiler controls.

---

## M8 — Parallel run and migration

### Goal

Move production/pilot state only after the cloud path has proven equivalent enough.

### Work

- Export/import or backfill canonical telemetry/state into YDB.
- Run legacy SQLite/VPS and cloud path in parallel where practical.
- Compare sync coverage, report facts, DHW/reliability outputs and AI packet content.
- Validate failure/recovery behavior: network loss, ZONT API errors, overlapping timer invocations, OpenAI failure, storage transient errors.
- Define rollback and final cutover procedure.

### Acceptance criteria

- No unexplained loss/duplication of canonical telemetry or source events.
- Deterministic report facts match within expected algorithm/version differences.
- Existing DHW/reliability semantics survive migration.
- Cloud path survives normal retries/failures for an agreed observation period.
- Cutover has a tested rollback path.

---

## Integration with the main development track

The cloud track must not hold product intelligence hostage.

Recommended working model:

```text
fix/dhw-analysis-feedback (or successor)
  -> sensors / humidity / return / AI evidence / PZA reasoning

architecture/cloud-first-serverless
  -> IaC / runtime / YDB adapter / publishers / observability

periodic integration
  -> merge/rebase current product baseline into cloud branch
  -> keep storage/runtime seams compatible
  -> migrate only when both sides are ready
```

The cloud branch should periodically absorb product changes. Product feature branches should not absorb half-finished YDB/Terraform code unless an interface change is intentionally shared.

## Definition of milestone complete

This milestone is complete when ZontAnalyzer can run without a permanent VPS as a scheduled Serverless Container, persist canonical state in the chosen managed backend, publish reports to Object Storage, expose sufficient observability, preserve read-only/safety/AI epistemic contracts, and has completed a controlled migration from the existing deployment.

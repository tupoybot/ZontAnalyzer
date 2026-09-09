# Documentation entrypoint and context budget

- Start with `docs/status.md` and only the relevant section of `docs/implementation_plan.md`.
- Use `docs/README.md` as the document map; read product requirements and architecture only
  for contracts relevant to the task. Do not load every Markdown file or completed stage.
- `docs/archive/` is historical evidence, not current instructions or backlog. Exclude it
  from broad searches. Open a specific archived section only for a concrete historical
  question that current docs and code cannot answer, or when the user explicitly asks.
- Keep `docs/status.md` short: current state, deployed version, validation, remaining work
  and next step. Move completed release narratives to the archive; retain active contracts
  and unresolved work in current documents. Do not revive archived P0–P5 plans.
- Keep product goals, current architecture and rationale, model-selection policy, data flows
  and still-relevant integration plans in current docs, even when implemented in code.
  Mark implemented, planned and exploratory scope explicitly; archive only superseded
  decisions and completed execution history. Reduce context through selective reading.
- Stage 9 is ongoing pilot production operation and improvements. Stages 10 (multi-room)
  and 11 (control) are optional and deferred; do not start them automatically.

# Development workflow

For substantial implementation tasks:

- Break the work into independent parts.
- Delegate exploration, frontend, backend, and testing to subagents where useful.
- Avoid concurrent edits to the same files.
- The main agent owns integration and final verification.
- For application changes, run the application, linters, type checks, and tests before completion.
- Documentation-only changes require document/link/diff checks, not application builds or deployment.
- Do not stop after producing a plan when implementation was requested.
- Use openai requests if it necessary for testing, but not more than 1 request per dialog iteration

# General approach

## Local Python work runs in Docker

- Before every merge, run `docker system prune -af` locally. Do not run this
  cleanup on HK; owner acceptance is still required before merging.

- Build packages, run Python checks/tests and prepare data inside Docker containers.
- Do not install Python dependencies into the host Python or use the host `.venv`
  for this workflow. Use `deploy/check-local.sh`; see `docs/container-development.md`.
- Keep source mounts read-only and temporary databases/caches in containers or
  explicitly isolated artifact directories. Production-host load limits still apply.

## HK production host: keep load minimal

- Never build images, packages, or application artifacts on `hk.tupoybot.ru`.
- Run builds, full tests, integration tests, and production-data acceptance locally.
- When real data is needed, create a SQLite online backup on HK, download it, and
  test against a separately writable local copy with an isolated publication directory.
- Deploy only the already built and tested immutable image to HK, then run a short,
  bounded smoke check. Do not repeat full analysis, backfills, benchmarks, or heavy
  database checks on the server as part of acceptance.
- HK has other workloads and its hosting provider has complained about sustained
  load. Keep deployment and diagnostics brief; never use the server as a build/test runner.

## Delegation

Use subagents only when they provide a clear benefit.

Do not delegate work by default. For small, local, or straightforward tasks, handle the work directly in the main agent.

When using a subagent:

* choose the lowest-capability model and the lowest reasoning effort sufficient for the task;
* delegate only if saved context/tokens or independent work outweigh setup and review cost;
* use a short context fork and explicit file ownership, expected result, and validation;
* prefer low reasoning for mechanical reads/status/commands, medium for bounded implementation;
  increase model capability or reasoning only for demonstrated ambiguity, failures, or complex design;
* if the requested model/effort is unavailable, use the smallest suitable available option;
* give it only the context it actually needs;
* prefer short, concrete assignments;
* do not duplicate reasoning between the main agent and subagents;
* do not spawn multiple agents for work that can be done efficiently by one.

The main agent remains responsible for architecture decisions, reviewing changes, resolving ambiguity, and producing the final result.

# Model selection

Prefer the smallest suitable model.

Examples:

* Simple command execution, status checks, waiting for a command to finish, collecting output, checking whether tests passed:

  * use a lightweight model such as `luna`;
* Mechanical inspection of files, locating definitions, simple repository searches:

  * use a lightweight model unless deeper reasoning is required;
* Small, well-scoped code or documentation corrections with a clear target and acceptance
  criterion:

  * use `gpt-5.3-codex-spark` with high reasoning for fast, focused edits;
  * do not use it for boiler operation analysis, thermal or gas interpretation, root-cause
    analysis, architecture, or other work requiring domain judgment;
* Terraform design, security-sensitive changes, architecture, debugging non-obvious failures:

  * keep in the main agent or use a stronger model only when necessary.

Do not use a stronger model merely because it is available.

## Plan execution and handoff

- Follow the dependency order and work portions in `docs/implementation_plan.md`.
- Develop each implementation stage N in its own `stageN` branch created from up-to-date
  `main`. For the open-ended stage 9, use `stage9/<topic>` per bounded improvement;
  acceptance and merge apply to that improvement, not the entire operation period.
  Use `docs/<topic>` for documentation-only work.
- Before asking for owner acceptance, complete implementation and technical checks,
  commit and push the stage's changes, deploy the locally tested immutable image,
  and verify the running server with bounded smoke checks. Present the deployed result
  and evidence to the owner. These steps do not wait for owner acceptance; ONLY the
  merge waits. For documentation-only changes, commit/push and document checks suffice.
  Merge `stageN`
  into `main` ONLY AFTER THE OWNER EXPLICITLY ACCEPTS THAT STAGE. Passing tests, CI,
  isolated acceptance, deployment, smoke checks, or an agent's review does not constitute
  owner acceptance. A request to implement/complete a stage is not advance acceptance;
  silence is not acceptance. Until explicit owner acceptance, keep the stage open and
  do not merge it or start the next stage. After acceptance, merge and create the next
  stage branch from the updated `main`.
- Follow the documentation entrypoint above; inspect only relevant source/tests.
- Detailed release criteria are in `docs/release-process.md`; read them when preparing a release.
- A work portion includes implementation and related tests. Keep a short handoff in
  `docs/status.md`: contracts, changed scope, validation, remaining work and next step.
- Local checkpoints within a stage do not each require production deployment. A functional
  release requires local checks/isolated acceptance, a tested immutable image and bounded HK smoke.
- Documentation-only changes need document/link/diff checks, not an application deployment.
- Never run test suites, image builds or acceptance analysis on HK, including temporary directories.
- The one real OpenAI request limit is shared by the main agent and all subagents per user turn;
  coordinate it explicitly and use mocks for the remaining checks.

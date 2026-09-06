# Development workflow

For substantial implementation tasks:

- Break the work into independent parts.
- Delegate exploration, frontend, backend, and testing to subagents where useful.
- Avoid concurrent edits to the same files.
- The main agent owns integration and final verification.
- Run the application, linters, type checks, and tests before completion.
- Do not stop after producing a plan when implementation was requested.
- Use openai requests if it necessary for testing, but not more than 1 request per dialog iteration

# General approach

## HK production host: keep load minimal

- Never build images, packages, or application artifacts on `hk.tupoybot.ru`.
- For future HK releases, minimize server CPU rather than transfer size: pull and
  unpack the tested registry image locally, export an uncompressed `docker save`
  tar, and transfer it without gzip/zstd or SSH compression. Do not make HK pull
  and decompress registry layers. Verify the loaded image against the locally
  tested immutable image ID and retain its registry digest in release evidence.
  Adapt the release workflow before its next use: the existing `release.sh`
  registry-pull path does not yet implement this transport.
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
* Terraform design, security-sensitive changes, architecture, debugging non-obvious failures:

  * keep in the main agent or use a stronger model only when necessary.

Do not use a stronger model merely because it is available.

## Plan execution and handoff

- Follow the dependency order and work portions in `docs/implementation_plan.md`.
- Develop each implementation stage N in its own `stageN` branch created from up-to-date
  `main` (for example, `stage2`, `stage3`). Work portions such as 2a/2b stay in `stage2`.
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
- Read the active stage, product requirements and current status first; inspect only relevant
  source/tests instead of repeatedly loading the full historical documentation.
- A work portion includes implementation and related tests. Keep a short handoff in
  `docs/status.md`: contracts, changed scope, validation, remaining work and next step.
- Local checkpoints within a stage do not each require production deployment. A functional
  release requires local checks/isolated acceptance, a tested immutable image and bounded HK smoke.
- Documentation-only changes need document/link/diff checks, not an application deployment.
- Never run test suites, image builds or acceptance analysis on HK, including temporary directories.
- The one real OpenAI request limit is shared by the main agent and all subagents per user turn;
  coordinate it explicitly and use mocks for the remaining checks.

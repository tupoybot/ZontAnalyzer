# Agent instructions

`#for-agents`

# Public repository and privacy

- This GitHub repository is public. Before writing documentation, staging changes,
  committing/pushing, or publishing PR/Issue text, review the exact content for
  personal and infrastructure information.
- Do not publish personal server hostnames/URLs/IPs, cloud/account/folder IDs or
  names, private network topology, credentials, private paths, account inventory,
  or the owner's financial details. Use role names, placeholders, and private
  environment configuration instead. Public product/project links and official
  provider endpoints are different; review their purpose before including them.
- Information shared in chat or observed through tools is not permission to publish
  it. Existing exposure in Git is not permission to repeat it.
- Terraform source must use parameterized inputs without personal defaults. Put
  private environment identifiers/addresses in GitHub Environment Secrets or private
  local configuration; Variables are for values safe to disclose. Protect state,
  saved plans and actual plan/apply logs as private data. `sensitive = true` and
  GitHub masking do not make state/plan files or arbitrary output safe to publish.
- Document the current technical contract, rationale, validation and unresolved
  work. Do not turn the conversation into a transcript or retain discarded options
  merely to say they were excluded; remove obsolete ideas from the active plan.
- If private information has already been pushed, sanitize the current files and
  report the remaining history exposure. Do not rewrite shared history silently.

# Documentation entrypoint and context budget

- Start with the short `docs/status.md`, then select one task route in `docs/README.md`.
- Read only the relevant section of the selected plan or contract. Find headings with
  `rg -n '^#{1,3} ' <file>`, then read the required range. Do not concatenate whole
  documents, glob Markdown files, or recursively follow every link.
- Initial reading budget: status, document map, and one or two relevant sections.
  This is a starting budget, not a hard limit: expand only to resolve a concrete
  question, dependency, conflicting contract, or validation requirement.
- For a local fix with a known target, inspect that code and its tests directly;
  read plans only if scope or acceptance depends on them. Do not reread unchanged
  documents during the same task. Give subagents file/section pointers, not a full
  documentation bundle.
- Search the selected topic directory first. If the route is unclear, search headings
  or filenames across current docs, excluding `docs/archive/` and `docs/benchmarks/`.
  Read benchmark artifacts only for a measurement task.
- Use `docs/implementation_plan.md` for product work and the relevant milestone in
  `docs/cloud/cloud_first_milestone.md` for cloud work. Completed milestones are not
  prerequisite reading unless the task changes one of their contracts.
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

# GitHub Issues: report outcomes

- Keep the goal, scope, and acceptance criteria in the issue description; update it
  when the plan changes. Do not turn descriptions or comments into an agent work log.
- By default, post one final comment at handoff: what changed for the reporter,
  whether it was verified, the PR link, and what remains. Usually one paragraph
  or 3–5 short bullets is enough.
- Post an interim comment only for a blocker requiring the reporter's decision,
  a material scope/schedule change, or a finding that changes the expected outcome.
  Report resolution of such a blocker if the reporter is waiting for an answer.
- Do not post separate updates for starting work, each commit, CI runs/retries,
  passing test groups, or routine findings. Summarize checks in the final comment;
  keep detailed logs and measurements in CI, the PR, or the topic document.
- Read recent comments before posting; do not repeat existing information.
  Update stage links and state in the parent issue without duplicating child issue
  summaries. After acceptance and merge, close the issue with a link to the result;
  no repeated detailed report is needed. Acceptance and closure gates still apply.

# Clear documentation

- Write for a reader who did not follow the discussion and does not know the code.
  On first reading, they should understand what works now, what is proposed,
  why it matters, and how to verify the result.
- Use English for agent-only instructions marked `#for-agents`. Write human-facing
  and shared documents in plain Russian and mark them `#for-human`. Do not duplicate
  documents in both languages. Audience tags do not change the task-based reading rules:
  agents still read relevant human-facing contracts, and tags do not require loading files.
- In Russian documentation, avoid strings of English terms, abbreviations, and
  architecture pattern names. Explain necessary technical terms on first use.
- Keep parameter, class, and API names exact when they help locate code or settings.
  Explain the action before naming its identifier; component lists do not replace
  an explanation.
- Distinguish current behavior, planned changes, and unverified assumptions.
  Do not simplify away important conditions or limits.
- Before saving, reread the changed passage as a user. Rewrite it if understanding
  requires decoding jargon or reconstructing the conversation. Use repository-relative
  file links, not editor URLs or local environment paths.

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
  cleanup on the production host; owner acceptance is still required before merging.

- Build packages, run Python checks/tests and prepare data inside Docker containers.
- Do not install Python dependencies into the host Python or use the host `.venv`
  for this workflow. Use `deploy/check-local.sh`; see `docs/development/container-development.md`.
- Keep source mounts read-only and temporary databases/caches in containers or
  explicitly isolated artifact directories. Production-host load limits still apply.

## Codex Cloud tasks

### Follow-ups in an existing Codex pull request

- When a task comes from an `@codex` comment inside an existing pull request whose
  head branch is `codex/issue-N`, that pull request and its current head branch are
  the authoritative continuation context.
- Continue from the current remote PR head, not from `main` and not from a new branch.
  Resolve the PR head with GitHub, fetch it, and base the work on that exact SHA before
  editing.
- Never create a `*-review-followup`, `*-fixup`, or other follow-up branch. Never
  create a second pull request for review feedback or a conversational follow-up.
- Publish any required follow-up commit directly back to the same existing
  `codex/issue-N` branch with a normal non-force push.
- Do not use `make_pr` or merely prepare PR metadata for a follow-up. A local commit
  is not published work.
- Before reporting a follow-up change as complete, verify that the existing PR head SHA
  moved to the new commit and that the intended files are visible in that same PR.
- If the follow-up only asks a question and no code/document change is needed, answer
  without creating a commit or branch.

- Codex Cloud runs inside its own prepared container and does not provide a Docker daemon.
  Do not run `docker`, `docker compose`, `deploy/check-local.sh`, image builds, or
  `docker system prune` from a Codex Cloud task.
- The environment setup/maintenance script is `deploy/setup-codex-cloud.sh`.
  For application validation in Codex Cloud use `deploy/check-codex-cloud.sh`.
  For focused tests use binaries from
  `$HOME/.cache/zont-analyzer-codex/venv/bin`.
- Cloud-task checks are fixture-based development checks. They do not replace Docker/CI
  release validation, immutable image acceptance, deployment, or bounded production smoke.
- If a task requires a capability unavailable in Codex Cloud (for example Docker or a
  protected live integration), complete everything that can be validated in the cloud,
  report the remaining validation explicitly, and do not fail merely because Docker is absent.

## Production host: keep load minimal

- Never build images, packages, or application artifacts on the production host.
- Run builds, full tests, integration tests, and production-data acceptance locally.
- When real data is needed, create a SQLite online backup on the production host, download it, and
  test against a separately writable local copy with an isolated publication directory.
- Deploy only the already built and tested immutable image to the production host, then run a short,
  bounded smoke check. Do not repeat full analysis, backfills, benchmarks, or heavy
  database checks on the server as part of acceptance.
- The production host is shared. Keep deployment and diagnostics brief; never use
  the server as a build/test runner.

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
- Detailed release criteria are in `docs/development/release-process.md`; read them when preparing a release.
- A work portion includes implementation and related tests. Do not write tests for reversible, low-impact changes that mirror the implementation. If you do choose to verify your work with tests, make sure that the tests are meaningful and necessary to verify implementation. Run tests appropriate to the change and complete required checks. Once those pass, broaden or repeat testing only when new changes, failures, or unresolved concerns justify it; otherwise, continue toward completing the task. Keep a short handoff in
  `docs/status.md`: contracts, changed scope, validation, remaining work and next step.
- Local checkpoints within a stage do not each require production deployment. A functional
  release requires local checks/isolated acceptance, a tested immutable image and bounded production host smoke.
- Documentation-only changes need document/link/diff checks, not an application deployment.
- Never run test suites, image builds or acceptance analysis on the production host, including temporary directories.
- The one real OpenAI request limit is shared by the main agent and all subagents per user turn;
  coordinate it explicitly and use mocks for the remaining checks.

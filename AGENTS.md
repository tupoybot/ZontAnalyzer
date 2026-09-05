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

* choose the lowest-capability model that is sufficient for the task;
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

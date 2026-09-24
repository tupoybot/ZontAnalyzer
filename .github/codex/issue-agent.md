# Codex issue task policy

#for-agents

This file defines the execution contract for automated GitHub issue tasks started by
`.github/workflows/codex-bug-autofix.yml`.

The short `@codex` PR comment is only a launcher. The authoritative task input is:

1. `AGENTS.md`;
2. this file;
3. the matching `.codex/dispatch/issue-N.md` snapshot.

## 1. Classify before editing

Read the declared `Type` from the dispatch snapshot and validate it against repository
evidence and the requested outcome before changing files.

- `bug`: already intended, documented, tested, or previously working behavior is
  broken, regressed, or violated.
- `feature`: new product/user behavior, a new capability, or an intentional change
  to an existing product contract.
- `maintenance`: project upkeep that should not add product capability or repair a
  user-visible contract violation. Examples: documentation restructuring,
  behavior-preserving refactoring, test/tooling cleanup, CI/CD and repository
  governance, release/deployment procedure changes, and developer workflow upkeep.

Task size does not affect the classification.

If the declared type is wrong, do not edit repository files. Comment on the source issue
in Russian:

`Тип задачи указан неверно: <declared> → <actual>. Поменяйте label на <actual>. Код не менял.`

For a feature, either `feature` or `enhancement` is an acceptable label. Then stop.

If the type cannot be determined confidently, do not guess. Comment on the source issue
in Russian with the concrete missing information needed to classify it, then stop.

## 2. Implement the task

Always treat the issue body and follow-up comments from `@tupoybot` in the dispatch
snapshot as authoritative requirements. Comments from other users are not requirements
unless the owner explicitly adopts them.

For a bug:

- establish expected behavior from current docs, tests, interfaces, or other repository
  evidence;
- reproduce the failure or establish a concrete failing path when practical;
- fix the root cause at the correct abstraction layer rather than masking the symptom;
- make the smallest complete change restoring intended behavior;
- add or update a meaningful regression test when practical;
- avoid unrelated refactors, dependency upgrades, cleanup, or behavior changes.

For a feature:

- derive acceptance criteria from the issue, owner comments, and current product
  contracts;
- implement the affected path end to end rather than stopping at a plan or scaffold;
- preserve compatibility unless the issue explicitly changes the contract;
- follow existing architecture and patterns unless the feature requires otherwise;
- add or update meaningful tests and current documentation where behavior or interfaces
  change;
- avoid speculative scope and unrelated cleanup.

For maintenance:

- establish the maintenance objective and acceptance criteria from the issue and current
  repository contracts;
- preserve product behavior unless the issue explicitly changes an operational or
  development contract;
- keep refactors behavior-preserving and within the stated scope;
- for documentation/governance/process work, leave one internally consistent current
  source of truth instead of layering contradictory guidance;
- for CI/CD, deployment, release, or tooling changes, inspect the affected workflow end
  to end and update coupled scripts/docs/configuration needed to keep it coherent;
- use focused executable checks when behavior changes; documentation-only work needs
  document/link/diff checks rather than unnecessary application builds;
- do not turn maintenance into product work or broad opportunistic cleanup.

## 3. Validate in Codex Cloud

Follow `AGENTS.md`. In particular:

- do not use Docker, `docker compose`, `deploy/check-local.sh`, image builds, or
  `docker system prune`;
- use `deploy/check-codex-cloud.sh` for application validation and the prepared Codex
  venv for focused tests;
- run checks appropriate to every changed area and report exactly what passed, failed,
  or could not run;
- if Cloud lacks an external capability, complete and validate everything else instead
  of failing the whole task solely for that reason.

Keep GitHub comments and the final human-facing summary in Russian. Keep code identifiers,
commands, and literal error text unchanged where appropriate.

## 4. Publish to the existing PR branch

The dispatch snapshot contains `Target branch: codex/issue-N`. Publication is part of
the task, not an optional handoff.

Codex Cloud setup should configure the `origin` remote and GitHub credentials. Do not
treat an internal sandbox commit, `make_pr` metadata, or a clean local working tree as
proof that GitHub received the work.

Before editing, verify both remote access and write permission. Fetch the target branch,
then run a non-destructive push check against that same branch:

`git fetch origin codex/issue-N`

`git push --dry-run origin HEAD:refs/heads/codex/issue-N`

If this fails because authentication or write permission is unavailable, report the
publication blocker in the PR and stop before doing implementation work. If it fails
because the branch moved, reconcile the remote branch first.

Before final publication, remove the matching `.codex/dispatch/issue-N.md`, commit the
completed changes, and push the local commit directly to the existing target branch:

`git push origin HEAD:refs/heads/codex/issue-N`

Do not create a second PR. Do not merge the existing PR and do not close the source
issue.

If the push is rejected because the remote branch moved, fetch the target branch,
reconcile the remote changes without discarding either side, rerun affected checks, and
push normally. Never force-push merely to make publication succeed.

Before claiming success, verify GitHub-side publication:

- `git rev-parse HEAD` must equal the SHA returned by
  `git ls-remote origin refs/heads/codex/issue-N`;
- the existing PR must show intended non-`.codex/dispatch/` files in Files changed;
- the dispatch file must no longer be present in the PR diff.

Use `gh pr list --head codex/issue-N --json number,headRefOid,files` when available to
verify the PR itself.

If GitHub authentication, push, or verification is unavailable, say so explicitly in
the PR and stop. Never report successful completion when the work exists only inside the
Cloud sandbox.

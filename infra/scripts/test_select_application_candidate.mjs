import assert from 'node:assert/strict';
import test from 'node:test';

import {
  selectCandidate,
  validateProvenance,
} from './select_application_candidate.mjs';

const repository = 'Example/Project';
const imageRepository = repository.toLowerCase();
const branch = 'main';
const sha = 'a'.repeat(40);
const otherSha = 'b'.repeat(40);
const digest = 'c'.repeat(64);

function run(overrides = {}) {
  return {
    id: 12345,
    conclusion: 'success',
    status: 'completed',
    event: 'push',
    head_branch: branch,
    head_sha: sha,
    head_repository: { full_name: repository },
    path: '.github/workflows/application-release.yml',
    ...overrides,
  };
}

function provenance(overrides = {}) {
  return {
    commit: sha,
    source_ref: branch,
    checks: 'passed',
    runtime: 'cloud',
    workflow_run: `https://github.com/${repository}/actions/runs/12345`,
    image: `ghcr.io/${imageRepository}@sha256:${digest}`,
    ...overrides,
  };
}

test('selects the first compatible valid run in input order', () => {
  const rejected = run({ id: 7, head_sha: otherSha });
  const accepted = run({ id: 8, head_sha: sha });
  const later = run({ id: 9, head_sha: 'd'.repeat(40) });
  const checked = [];

  const result = selectCandidate(
    [rejected, accepted, later],
    branch,
    repository,
    (candidateSha) => {
      checked.push(candidateSha);
      return candidateSha === sha;
    },
  );

  assert.equal(result, accepted);
  assert.deepEqual(checked, [otherSha, sha]);
});

test('rejects pull requests, wrong branches, repositories, workflows, and non-ancestor commits', () => {
  const maliciousPr = run({ event: 'pull_request' });
  const wrongBranch = run({ id: 2, head_branch: 'feature/example' });
  const wrongRepository = run({
    id: 3,
    head_repository: { full_name: 'attacker/project' },
  });
  const wrongWorkflow = run({ id: 4, path: '.github/workflows/other.yml' });
  const wrongConclusion = run({ id: 5, conclusion: 'failure' });
  const incompleteRun = run({ id: 6, status: 'in_progress' });
  const unsupportedEvent = run({ id: 7, event: 'pull_request_target' });
  const malformedSha = run({ id: 8, head_sha: 'A'.repeat(40) });
  const nonAncestor = run({ id: 9, head_sha: otherSha });
  const checked = [];

  assert.throws(() =>
    selectCandidate(
      [
        maliciousPr,
        wrongBranch,
        wrongRepository,
        wrongWorkflow,
        wrongConclusion,
        incompleteRun,
        unsupportedEvent,
        malformedSha,
        nonAncestor,
      ],
      branch,
      repository,
      (candidateSha) => {
        checked.push(candidateSha);
        return false;
      },
    ),
  );
  assert.deepEqual(checked, [otherSha]);
});

test('throws when no run satisfies run and ancestry requirements', () => {
  assert.throws(
    () => selectCandidate([], branch, repository, () => true),
  );
  assert.throws(
    () => selectCandidate([run()], branch, repository, () => false),
  );
});

test('validates provenance and returns its pinned image', () => {
  assert.equal(validateProvenance(provenance(), run(), repository, branch),
    `ghcr.io/${imageRepository}@sha256:${digest}`);
});

test('rejects provenance with a mismatched source, image digest, or revision', () => {
  const selectedRun = run();
  const cases = [
    provenance({ source_ref: 'release/wrong-branch' }),
    provenance({ image: `ghcr.io/${imageRepository}@sha256:${'0'.repeat(63)}` }),
    provenance({ commit: otherSha }),
  ];

  for (const candidate of cases) {
    assert.throws(() =>
      validateProvenance(candidate, selectedRun, repository, branch),
    );
  }
});

test('rejects provenance with any other contract mismatch', () => {
  const selectedRun = run();
  const cases = [
    provenance({ checks: 'failed' }),
    provenance({ runtime: 'hosted' }),
    provenance({ workflow_run: 'https://github.com/example/project/actions/runs/999' }),
    provenance({ image: `ghcr.io/${imageRepository}:latest` }),
    provenance({ image: `ghcr.io/${imageRepository}@sha256:${digest}\n` }),
    provenance({ image: `evil.example/ghcr.io/${imageRepository}@sha256:${digest}` }),
  ];

  for (const candidate of cases) {
    assert.throws(() =>
      validateProvenance(candidate, selectedRun, repository, branch),
    );
  }
});

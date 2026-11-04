import assert from 'node:assert/strict';
import test from 'node:test';
import {execFileSync, spawnSync} from 'node:child_process';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {trustedBranch} from './check_candidate_ref.mjs';
import {routeTrigger} from './deployment_trigger.mjs';
import {selectCandidate, validateProvenance} from './select_application_candidate.mjs';

test('only main and exact positive issue branch names qualify', () => {
  for (const name of ['main', 'feat/issue-1', 'codex/issue-71', 'feat/issue-12345']) {
    assert.equal(trustedBranch(name), true, name);
  }
  for (const name of ['', null, 'Main', 'feat/issue-0', 'codex/issue-01', 'feat/issue-1/fix',
    'feat/issue-1-fixup', 'codex/issue-1\n', 'xfeat/issue-1', 'stageM1/example', 'refs/heads/main']) {
    assert.equal(trustedBranch(name), false, String(name));
  }
});

test('both issue branch kinds require verified provenance and explicit apply', () => {
  const repository = 'Example/Project';
  const sha = 'a'.repeat(40);
  for (const branch of ['feat/issue-71', 'codex/issue-71']) {
    const run = {id: 42, status: 'completed', conclusion: 'success', event: 'push',
      path: '.github/workflows/application-release.yml', head_repository: {full_name: repository},
      head_branch: branch, head_sha: sha};
    const args = {eventName: 'workflow_run', event: {workflow_run: run}, repository,
      branch, head: sha, compatible: true};
    assert.equal(routeTrigger(args).deploy, true);
    assert.equal(routeTrigger(args).apply, false);
    assert.equal(selectCandidate([run], branch, repository, candidate => candidate === sha), run);
    const provenance = {commit: sha, source_ref: branch, checks: 'passed', runtime: 'cloud',
      workflow_run: `https://github.com/${repository}/actions/runs/42`,
      image: `ghcr.io/example/project@sha256:${'b'.repeat(64)}`};
    assert.equal(validateProvenance(provenance, run, repository, branch), provenance.image);
    for (const wrong of [
      {...run, head_repository: {full_name: 'Fork/Project'}}, {...run, event: 'pull_request'},
      {...run, head_sha: 'c'.repeat(40)}, {...run, head_branch: `${branch}-fixup`},
    ]) {
      assert.throws(() => selectCandidate([wrong], branch, repository, candidate => candidate === sha));
      assert.throws(() => validateProvenance(provenance, wrong, repository, branch));
    }
  }
});

test('candidate CLI verifies real branch head, checkout and accepted ancestry', () => {
  const directory = mkdtempSync(join(tmpdir(), 'candidate-ref-'));
  const git = (...args) => execFileSync('git', args, {cwd: directory, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();
  const script = fileURLToPath(new URL('./check_candidate_ref.mjs', import.meta.url));
  const check = (branch, sha) => spawnSync(process.execPath, [script, branch, sha], {cwd: directory}).status;
  try {
    git('init', '-q', '-b', 'main');
    git('config', 'user.name', 'Test');
    git('config', 'user.email', 'test@example.invalid');
    git('commit', '--allow-empty', '-qm', 'Accepted base');
    const base = git('rev-parse', 'HEAD');
    git('update-ref', 'refs/remotes/origin/main', base);
    git('switch', '-qc', 'feat/issue-71');
    git('commit', '--allow-empty', '-qm', 'Candidate');
    const sha = git('rev-parse', 'HEAD');
    git('update-ref', 'refs/remotes/origin/feat/issue-71', sha);
    assert.equal(check('feat/issue-71', sha), 0);
    assert.notEqual(check('feat/issue-71', base), 0);
    assert.notEqual(check('codex/issue-71', sha), 0);
    git('update-ref', 'refs/remotes/origin/feat/issue-71', base);
    assert.notEqual(check('feat/issue-71', sha), 0);
    git('switch', '--orphan', 'unrelated');
    git('commit', '--allow-empty', '-qm', 'Unrelated');
    const unrelated = git('rev-parse', 'HEAD');
    git('update-ref', 'refs/remotes/origin/codex/issue-71', unrelated);
    assert.notEqual(check('codex/issue-71', unrelated), 0);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

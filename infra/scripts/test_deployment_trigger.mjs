import assert from 'node:assert/strict';
import test from 'node:test';
import {execFileSync, spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

import {isReleasePath, pushBase, releasePaths, routeTrigger, trustedBranch} from './deployment_trigger.mjs';

const repository = 'Example/Project';
const head = 'a'.repeat(40);
const releaseHead = 'b'.repeat(40);
const branch = 'main';
const push = {after: head, repository: {full_name: repository}};
const release = {id: 123, status: 'completed', conclusion: 'success', event: 'push',
  path: '.github/workflows/application-release.yml', head_branch: branch,
  head_sha: head, head_repository: {full_name: repository}};

function route(eventName, event, overrides = {}) {
  return routeTrigger({eventName, event, repository, branch, head, ...overrides});
}

test('release paths match the application workflow trigger contract', () => {
  const workflow = readFileSync(new URL('../../.github/workflows/application-release.yml', import.meta.url), 'utf8');
  const pathList = workflow.match(/paths: \[([^\n]+)\]/)?.[1];
  assert.ok(pathList);
  const actualPatterns = [...pathList.matchAll(/'([^']+)'/g)]
    .map(match => match[1].replace(/\/\*\*$/, '/'));
  assert.deepEqual([...releasePaths].sort(), actualPatterns.sort());
  for (const path of ['src/app.py', 'tests/test_app.py', 'tools/import.py', 'Dockerfile',
    '.dockerignore', 'pyproject.toml', 'README.md', 'deploy/check-local.sh',
    'deploy/check-ydb.sh', 'deploy/check-tests.sh', 'deploy/assert-ydb-memory.sh',
    'deploy/smoke-cloud-local.sh', 'deploy/compose.yaml', 'deploy/compose.test.yaml',
    'deploy/compose.local.yaml', 'deploy/env.example', 'deploy/nginx-zont-analyzer.conf',
    'deploy/nginx-zont-analyzer-root.conf', '.github/workflows/ci.yml',
    '.github/workflows/application-release.yml']) {
    assert.equal(isReleasePath(path), true, path);
  }
  for (const path of ['infra/cloud/main.tf', 'docs/status.md', 'README.md.backup']) {
    assert.equal(isReleasePath(path), false, path);
  }
  assert.equal(pushBase('0'.repeat(40)), 'origin/main');
  assert.equal(pushBase(releaseHead), releaseHead);
});

test('mixed source and infrastructure push waits; infrastructure-only push enters immediately', () => {
  assert.equal(route('push', push, {changedPaths: ['src/app.py', 'infra/cloud/main.tf']}).deploy, false);
  assert.equal(route('push', push, {changedPaths: ['.github/workflows/ci.yml', 'infra/cloud/main.tf']}).deploy, false);
  assert.equal(route('push', push, {changedPaths: ['deploy/check-tests.sh', 'infra/cloud/main.tf']}).deploy, false);
  assert.deepEqual(route('push', push, {changedPaths: ['infra/cloud/main.tf']}),
    {deploy: true, branch, head, apply: false, releaseRunId: ''});
  assert.equal(route('push', push, {changedPaths: ['infra/cloud/main.tf'],
    headMessage: '[cloud-apply] update'}).apply, true);
});

test('manual dispatch uses only its explicit apply input', () => {
  assert.equal(route('workflow_dispatch', {inputs: {apply: 'true'}}).apply, true);
  assert.equal(route('workflow_dispatch', {inputs: {apply: 'false'}},
    {headMessage: '[cloud-apply] ignored'}).apply, false);
});

test('successful same-repository release completion enters and pins the run', () => {
  assert.deepEqual(route('workflow_run', {workflow_run: release}, {compatible: true}),
    {deploy: true, branch, head, apply: false, releaseRunId: '123'});
  assert.equal(route('workflow_run', {workflow_run: {...release, event: 'workflow_dispatch'}},
    {compatible: true}).deploy, true);
});

test('failed, cancelled, foreign and pull-request releases never enter', () => {
  for (const invalid of [
    {...release, conclusion: 'failure'},
    {...release, conclusion: 'cancelled'},
    {...release, status: 'in_progress'},
    {...release, head_repository: {full_name: 'Other/Project'}},
    {...release, event: 'pull_request'},
    {...release, path: '.github/workflows/other.yml'},
    {...release, head_branch: 'feature/untrusted'},
  ]) assert.equal(route('workflow_run', {workflow_run: invalid}, {compatible: true}).deploy, false);
  assert.equal(route('push', {...push, repository: {full_name: 'Other/Project'}},
    {changedPaths: ['infra/cloud/main.tf']}).deploy, false);
});

test('rejected completion stops before any Git invocation', () => {
  const directory = mkdtempSync(join(tmpdir(), 'deployment-route-'));
  try {
    const marker = join(directory, 'git-invoked');
    const git = join(directory, 'git');
    const eventPath = join(directory, 'event.json');
    const output = join(directory, 'output');
    writeFileSync(git, '#!/bin/sh\nprintf called >> "$GIT_MARKER"\nexit 99\n', {mode: 0o755});
    for (const invalid of [
      {...release, conclusion: 'failure'},
      {...release, conclusion: 'cancelled'},
      {...release, head_repository: {full_name: 'Other/Project'}},
      {...release, event: 'pull_request'},
    ]) {
      writeFileSync(eventPath, JSON.stringify({workflow_run: invalid}));
      const result = spawnSync(process.execPath,
        [fileURLToPath(new URL('./deployment_trigger.mjs', import.meta.url))],
        {encoding: 'utf8', env: {...process.env, PATH: directory, GIT_MARKER: marker,
          GITHUB_EVENT_NAME: 'workflow_run', GITHUB_EVENT_PATH: eventPath,
          GITHUB_REPOSITORY: repository, GITHUB_REF_NAME: branch, GITHUB_OUTPUT: output}});
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stdout, /Skipping untrusted or unsuccessful/);
      assert.equal(existsSync(marker), false);
      assert.equal(existsSync(output), false);
    }
    writeFileSync(git, '#!/bin/sh\nif [ "$1" = show-ref ]; then exit 1; fi\nprintf called >> "$GIT_MARKER"\nexit 99\n', {mode: 0o755});
    writeFileSync(eventPath, JSON.stringify({workflow_run: release}));
    const deleted = spawnSync(process.execPath,
      [fileURLToPath(new URL('./deployment_trigger.mjs', import.meta.url))],
      {encoding: 'utf8', env: {...process.env, PATH: directory, GIT_MARKER: marker,
        GITHUB_EVENT_NAME: 'workflow_run', GITHUB_EVENT_PATH: eventPath,
        GITHUB_REPOSITORY: repository, GITHUB_REF_NAME: branch, GITHUB_OUTPUT: output}});
    assert.equal(deleted.status, 0, deleted.stderr);
    assert.match(deleted.stdout, /deleted branch/);
    assert.equal(existsSync(marker), false);
    assert.equal(existsSync(output), false);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test('stale source rejects completion; docs advancement remains compatible without automatic apply', () => {
  assert.equal(route('workflow_run', {workflow_run: {...release, head_sha: releaseHead}},
    {compatible: false}).deploy, false);
  const docsAdvance = route('workflow_run', {workflow_run: {...release, head_sha: releaseHead}},
    {compatible: true, headMessage: '[cloud-apply] docs update'});
  assert.equal(docsAdvance.deploy, true);
  assert.equal(docsAdvance.apply, false);
  assert.equal(docsAdvance.releaseRunId, '123');
});

test('automatic apply requires exact release head and its prefix', () => {
  assert.equal(route('workflow_run', {workflow_run: release},
    {compatible: true, headMessage: '[cloud-apply] reviewed'}).apply, true);
  assert.equal(route('workflow_run', {workflow_run: release},
    {compatible: true, headMessage: 'normal commit'}).apply, false);
  assert.equal(route('workflow_run', {workflow_run: release},
    {compatible: true, branch: 'stageM4/ydb-runtime'}).deploy, false);
  assert.equal(route('push', push, {changedPaths: ['src/app.py', 'infra/cloud/main.tf'],
    headMessage: '[cloud-apply] reviewed'}).deploy, false);
  assert.equal(trustedBranch('stageM4/ydb-runtime\n'), false);
  assert.equal(route('push', push, {head: `${head}\n`, changedPaths: ['infra/cloud/main.tf']}).deploy, false);
});

test('CLI checks real Git ancestry and source changes across branch advancement', () => {
  const directory = mkdtempSync(join(tmpdir(), 'deployment-git-'));
  const git = (...args) => execFileSync('git', args, {cwd: directory, encoding: 'utf8'}).trim();
  try {
    git('init', '-q', '-b', 'main');
    git('config', 'user.name', 'Test');
    git('config', 'user.email', 'test@example.invalid');
    writeFileSync(join(directory, 'Dockerfile'), 'FROM scratch\n');
    git('add', 'Dockerfile');
    git('commit', '-qm', '[cloud-apply] release');
    const releaseSha = git('rev-parse', 'HEAD');
    const eventPath = join(directory, 'event.json');
    const output = join(directory, 'output');
    writeFileSync(eventPath, JSON.stringify({workflow_run: {...release, head_sha: releaseSha}}));
    const run = () => {
      rmSync(output, {force: true});
      git('update-ref', 'refs/remotes/origin/main', 'HEAD');
      const result = spawnSync(process.execPath,
        [fileURLToPath(new URL('./deployment_trigger.mjs', import.meta.url))],
        {cwd: directory, encoding: 'utf8', env: {...process.env,
          GITHUB_EVENT_NAME: 'workflow_run', GITHUB_EVENT_PATH: eventPath,
          GITHUB_REPOSITORY: repository, GITHUB_OUTPUT: output}});
      assert.equal(result.status, 0, result.stderr);
      return existsSync(output) ? readFileSync(output, 'utf8') : '';
    };
    assert.match(run(), /apply=true\nrelease_run_id=123\n/);
    writeFileSync(join(directory, 'notes.md'), 'Documentation\n');
    git('add', 'notes.md');
    git('commit', '-qm', '[cloud-apply] documentation');
    assert.match(run(), /apply=false\nrelease_run_id=123\n/);
    writeFileSync(join(directory, 'Dockerfile'), 'FROM scratch\nLABEL changed=true\n');
    git('add', 'Dockerfile');
    git('commit', '-qm', 'Change application');
    assert.equal(run(), '');
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

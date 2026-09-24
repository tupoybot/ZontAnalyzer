// Decide whether this event may enter the protected infrastructure job.
import {execFileSync, spawnSync} from 'node:child_process';
import {readFileSync, appendFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';

const sourcePaths = ['src/', 'Dockerfile', '.dockerignore', 'pyproject.toml', 'README.md'];
export const releasePaths = [...sourcePaths, 'tests/', 'tools/', 'deploy/check-local.sh',
  'deploy/check-ydb.sh', 'deploy/smoke-cloud-local.sh', '.github/workflows/application-release.yml'];

function exactMatch(pattern, value) {
  return typeof value === 'string' && value.match(pattern)?.[0] === value;
}

function validSha(value) {
  return exactMatch(/^[0-9a-f]{40}$/, value);
}

function matchesPath(path, pattern) {
  return pattern.endsWith('/') ? path.startsWith(pattern) : path === pattern;
}

export function isReleasePath(path) {
  return releasePaths.some(pattern => matchesPath(path, pattern));
}

export function trustedBranch(branch) {
  return branch === 'main' || exactMatch(/^stageM[0-9]+\/[a-z0-9/-]+$/, branch);
}

export function trustedReleaseEnvelope(run, repository) {
  return run?.status === 'completed' && run.conclusion === 'success' &&
    ['push', 'workflow_dispatch'].includes(run.event) &&
    run.path === '.github/workflows/application-release.yml' &&
    run.head_repository?.full_name === repository && trustedBranch(run.head_branch) &&
    validSha(run.head_sha) && Number.isSafeInteger(run.id) && run.id > 0;
}

export function routeTrigger({eventName, event, repository, branch, head, changedPaths = [], compatible = false, headMessage = ''}) {
  const skip = reason => ({deploy: false, reason});
  if (!trustedBranch(branch) || !validSha(head)) return skip('Untrusted branch or revision.');
  if (eventName === 'push') {
    if (event.deleted || event.after !== head || event.repository?.full_name !== repository) {
      return skip('Push does not match the checked revision and repository.');
    }
    if (changedPaths.some(isReleasePath)) return skip('Wait for the application release to finish.');
    return {deploy: true, branch, head, apply: headMessage.startsWith('[cloud-apply] '), releaseRunId: ''};
  }
  if (eventName === 'workflow_dispatch') {
    return {deploy: true, branch, head, apply: event.inputs?.apply === true || event.inputs?.apply === 'true', releaseRunId: ''};
  }
  if (eventName === 'workflow_run') {
    const run = event.workflow_run;
    if (!trustedReleaseEnvelope(run, repository) || run.head_branch !== branch || !compatible) {
      return skip('Release is unsuccessful, untrusted, or incompatible with current source.');
    }
    return {deploy: true, branch, head,
      apply: run.head_sha === head && headMessage.startsWith('[cloud-apply] '),
      releaseRunId: String(run.id)};
  }
  return skip('Unsupported event.');
}

function git(args, options = {}) {
  return execFileSync('git', args, {encoding: 'utf8', ...options}).trim();
}

function validGitRange(base, head) {
  const result = spawnSync('git', ['merge-base', '--is-ancestor', base, head]);
  return result.status === 0;
}

function changedPathsForPush(event, head) {
  const base = pushBase(event.before);
  if (!validGitRange(base, head)) throw new Error('Cannot establish the push base.');
  return git(['diff', '--no-renames', '--name-only', '-z', base, head]).split('\0').filter(Boolean);
}

export function pushBase(before) {
  return validSha(before) && !/^0{40}$/.test(before) ? before : 'origin/main';
}

function compatibleSource(releaseSha, head) {
  if (!validSha(releaseSha)) return false;
  if (!validGitRange(releaseSha, head)) return false;
  return spawnSync('git', ['diff', '--quiet', releaseSha, head, '--', ...sourcePaths]).status === 0;
}

function main() {
  const eventName = process.env.GITHUB_EVENT_NAME;
  const event = JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8'));
  const repository = process.env.GITHUB_REPOSITORY;
  if (eventName === 'workflow_run' && !trustedReleaseEnvelope(event.workflow_run, repository)) {
    return console.log('Skipping untrusted or unsuccessful application release.');
  }
  const branch = eventName === 'workflow_run' ? event.workflow_run?.head_branch : process.env.GITHUB_REF_NAME;
  if (!trustedBranch(branch)) return console.log('Skipping untrusted branch.');
  if (eventName === 'workflow_run') {
    const branchRef = `refs/remotes/origin/${branch}`;
    const exists = spawnSync('git', ['show-ref', '--verify', '--quiet', branchRef]);
    if (exists.status === 1) return console.log('Skipping release from a deleted branch.');
    if (exists.status !== 0) throw new Error('Cannot check release branch.');
  }
  const head = eventName === 'workflow_run' ? git(['rev-parse', `refs/remotes/origin/${branch}`]) : git(['rev-parse', 'HEAD']);
  if (!validGitRange('origin/main', head)) return console.log('Skipping branch without accepted main.');
  const releaseSha = event.workflow_run?.head_sha;
  const decision = routeTrigger({eventName, event, repository, branch, head,
    changedPaths: eventName === 'push' ? changedPathsForPush(event, head) : [],
    compatible: eventName === 'workflow_run' ? compatibleSource(releaseSha, head) : false,
    headMessage: git(['log', '-1', '--format=%B', head])});
  if (!decision.deploy) return console.log(`Skipping deployment: ${decision.reason}`);
  const lines = [`deploy=true`, `branch=${decision.branch}`, `head=${decision.head}`,
    `apply=${decision.apply}`, `release_run_id=${decision.releaseRunId}`];
  appendFileSync(process.env.GITHUB_OUTPUT, `${lines.join('\n')}\n`);
  console.log(`Infrastructure route accepted for ${decision.branch} at ${decision.head}.`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); } catch {
    console.error('Cannot safely route infrastructure deployment.');
    process.exitCode = 1;
  }
}

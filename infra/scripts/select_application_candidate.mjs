// Select a verified artifact; environment secrets contain infrastructure configuration.
import {execFileSync, spawnSync} from 'node:child_process';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

class CandidateSelectionError extends Error {}

export function selectCandidate(runs, branch, repository, isCompatible) {
  const selected = runs.find(run =>
    run.status === 'completed' && run.conclusion === 'success' &&
    ['push', 'workflow_dispatch'].includes(run.event) &&
    run.head_branch === branch && run.head_repository?.full_name === repository &&
    run.path === '.github/workflows/application-release.yml' &&
    typeof run.head_sha === 'string' && run.head_sha.length === 40 &&
    /^[0-9a-f]{40}$/.test(run.head_sha) && isCompatible(run.head_sha));
  if (!selected) throw new CandidateSelectionError('No successful compatible application release on this branch; release the candidate first.');
  return selected;
}

export function validateProvenance(provenance, run, repository, branch) {
  const prefix = `ghcr.io/${repository.toLowerCase()}@sha256:`;
  if (provenance.commit !== run.head_sha || provenance.source_ref !== branch ||
      provenance.checks !== 'passed' || provenance.runtime !== 'cloud' ||
      provenance.workflow_run !== `https://github.com/${repository}/actions/runs/${run.id}` ||
      typeof provenance.image !== 'string' || provenance.image.length !== prefix.length + 64 ||
      !provenance.image.startsWith(prefix) ||
      !/^[0-9a-f]{64}$/.test(provenance.image.slice(prefix.length))) {
    throw new CandidateSelectionError('Application provenance does not match the selected successful release.');
  }
  return provenance.image;
}

function compatible(commit) {
  const ancestor = spawnSync('git', ['merge-base', '--is-ancestor', commit, 'HEAD']);
  if (ancestor.status === 1) return false;
  if (ancestor.status !== 0) throw new CandidateSelectionError('Cannot verify candidate ancestry.');
  const source = spawnSync('git', ['diff', '--quiet', commit, 'HEAD', '--',
    'src', 'Dockerfile', '.dockerignore', 'pyproject.toml', 'README.md']);
  if (source.status === 1) return false;
  if (source.status !== 0) throw new CandidateSelectionError('Cannot verify candidate source.');
  return true;
}

function main() {
  const privateDirectory = process.argv[2];
  const repository = process.env.GITHUB_REPOSITORY;
  const branch = process.env.CANDIDATE_BRANCH || process.env.GITHUB_REF_NAME;
  if (!privateDirectory || !repository || !branch) throw new CandidateSelectionError('Missing candidate selection context.');
  const configPath = join(privateDirectory, 'cloud-work/inputs.tfvars.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  const explicitImage = process.env.APPLICATION_IMAGE || '';
  // Old secret fields are deliberately ignored: deployment selection must not
  // silently retain an old release when no compatible verified candidate exists.
  delete config.application_image;
  delete config.application_revision;
  if (explicitImage) {
    config.application_image = explicitImage;
    // The following workflow step verifies the digest, image label and provenance,
    // then supplies the exact revision before planning infrastructure.
    console.log('Explicit candidate selected; image verification follows.');
  } else {
    const releaseRunId = process.env.APPLICATION_RELEASE_RUN_ID || '';
    if (releaseRunId && !/^[1-9][0-9]*$/.test(releaseRunId)) {
      throw new CandidateSelectionError('Invalid application release run ID.');
    }
    let runs;
    if (releaseRunId) {
      runs = [JSON.parse(execFileSync('gh', ['api',
        `repos/${repository}/actions/runs/${releaseRunId}`], {encoding: 'utf8'}))];
    } else {
      const endpoint = `repos/${repository}/actions/workflows/application-release.yml/runs` +
        `?branch=${encodeURIComponent(branch)}&status=success&per_page=100`;
      runs = JSON.parse(execFileSync('gh', ['api', endpoint], {encoding: 'utf8'})).workflow_runs;
    }
    const run = selectCandidate(runs, branch, repository, compatible);
    const provenanceDirectory = join(privateDirectory, `selected-provenance-${run.id}`);
    mkdirSync(provenanceDirectory, {recursive: true, mode: 0o700});
    execFileSync('gh', ['run', 'download', String(run.id), '--repo', repository,
      '--name', 'application-provenance', '--dir', provenanceDirectory], {stdio: 'pipe'});
    const provenance = JSON.parse(readFileSync(join(provenanceDirectory, 'application-provenance.json'), 'utf8'));
    config.application_image = validateProvenance(provenance, run, repository, branch);
    config.application_revision = run.head_sha;
    console.log(`Selected verified application release ${run.id} (${run.head_sha}).`);
  }
  writeFileSync(configPath, JSON.stringify(config, null, 2) + '\n', {mode: 0o600});
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); } catch (error) {
    // Child process errors can contain private image arguments. Keep details local
    // and report only the controlled validation errors in public Actions output.
    console.error(error instanceof CandidateSelectionError ? error.message : 'Candidate lookup, configuration parsing or artifact download failed.');
    process.exitCode = 1;
  }
}

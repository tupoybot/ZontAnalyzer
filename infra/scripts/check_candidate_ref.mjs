// Branch names select candidates; they never authorize an infrastructure apply.
import {execFileSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';

export function trustedBranch(branch) {
  return branch === 'main' || (typeof branch === 'string' &&
    /^(feat|codex)\/issue-[1-9][0-9]*$/.exec(branch)?.[0] === branch);
}

export function checkCandidateRef(branch, sha) {
  if (!trustedBranch(branch) || typeof sha !== 'string' ||
      !/^[0-9a-f]{40}$/.test(sha) || sha.length !== 40) {
    throw new Error('Invalid candidate branch or revision.');
  }
  const git = (...args) => execFileSync('git', args, {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();
  if (git('rev-parse', 'HEAD') !== sha ||
      git('rev-parse', '--verify', `refs/remotes/origin/${branch}`) !== sha) {
    throw new Error('Candidate does not match the checked-out remote branch head.');
  }
  git('merge-base', '--is-ancestor', 'refs/remotes/origin/main', sha);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    checkCandidateRef(process.argv[2], process.argv[3]);
  } catch {
    console.error('Candidate branch, revision or accepted base verification failed.');
    process.exitCode = 1;
  }
}

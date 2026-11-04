import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';

const workflow = readFileSync(new URL('../../.github/workflows/codex-bug-autofix.yml', import.meta.url), 'utf8');
const scriptBlock = workflow.match(/          script: \|\n([\s\S]*?)\n        env:/)?.[1];
assert.ok(scriptBlock, 'dispatcher github-script block exists');
const dispatcher = scriptBlock.split('\n').map(line => line.replace(/^            /, '')).join('\n');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;

function fixture({manualRef = false, manualPulls = [], codexRef = false, state = 'open', labels = ['bug'],
  eventName = 'workflow_dispatch', action = null} = {}) {
  const calls = [];
  const failures = [];
  const notices = [];
  const issue = {number: 71, state, title: 'Example task', body: 'Details', html_url: 'https://example.test/issues/71',
    user: {login: 'tupoybot'}, labels: labels.map(name => ({name}))};
  const branch = 'codex/issue-71';
  const ownCodexPr = {number: 99, state: 'open', head: {ref: branch, repo: {full_name: 'acme/project'}},
    html_url: 'https://example.test/pull/99'};
  const github = {rest: {
    issues: {
      get: async () => ({data: issue}),
      listComments: async () => ({data: []}),
      createComment: async params => { calls.push(['comment', params]); return {data: {}}; },
    },
    git: {
      getRef: async ({ref}) => {
        calls.push(['getRef', ref]);
        if (ref === 'heads/feat/issue-71' && manualRef) return {data: {}};
        if (ref === `heads/${branch}` && codexRef) return {data: {}};
        const error = new Error('Not found'); error.status = 404; throw error;
      },
      createRef: async params => { calls.push(['createRef', params]); return {data: {}}; },
    },
    repos: {
      get: async () => ({data: {default_branch: 'main'}}),
      getBranch: async () => ({data: {commit: {sha: 'abc'}}}),
      getContent: async ({path}) => {
        calls.push(['getContent', path]);
        const error = new Error('Not found'); error.status = 404; throw error;
      },
      createOrUpdateFileContents: async params => { calls.push(['writeFile', params.path]); return {data: {}}; },
    },
    pulls: {
      list: async params => ({data: params.head ? (codexRef ? [ownCodexPr] : []) :
        (params.page === 2 ? manualPulls.slice(1) : manualPulls.slice(0, 1))}),
      listFiles: async () => ({data: [{filename: 'src/example.js'}]}),
      update: async params => ({data: {...ownCodexPr, ...params}}),
      create: async params => { calls.push(['createPR', params]); return {data: {...ownCodexPr, ...params}}; },
    },
  }, paginate: async (method, params) => {
    if (method === github.rest.pulls.list && params.state === 'open') {
      const first = (await method({...params, page: 1})).data;
      const second = (await method({...params, page: 2})).data;
      return [...first, ...second];
    }
    return (await method(params)).data;
  }};
  const core = {setFailed: message => failures.push(message), notice: message => notices.push(message),
    info: () => {}};
  const context = {repo: {owner: 'acme', repo: 'project'}, eventName,
    payload: eventName === 'workflow_dispatch'
      ? {inputs: {issue_number: '71'}}
      : {action, issue}};
  return {github, core, context, calls, failures, notices};
}

async function run(options) {
  const f = fixture(options);
  await new AsyncFunction('github', 'core', 'context', 'process', 'Buffer', dispatcher)(
    f.github, f.core, f.context, {env: {CODEX_GITHUB_TOKEN_PRESENT: 'true'}}, Buffer);
  return f;
}

test('manual branch ref refuses before Codex branch or snapshot mutation', async () => {
  const result = await run({manualRef: true});
  assert.match(result.failures[0], /existing manual work/);
  assert.equal(result.calls.some(([kind]) => kind === 'createRef' || kind === 'writeFile'), false);
});

test('open same-repository manual PR on a later page refuses before mutation', async () => {
  const manualPr = {head: {ref: 'feat/issue-71', repo: {full_name: 'acme/project'}}};
  const result = await run({manualPulls: [{head: {ref: 'unrelated', repo: {full_name: 'acme/project'}}}, manualPr]});
  assert.match(result.failures[0], /existing manual work/);
  assert.equal(result.calls.some(([kind]) => kind === 'createRef' || kind === 'writeFile'), false);
});

test('existing Codex branch and PR are reused for explicit retry', async () => {
  const result = await run({codexRef: true});
  assert.deepEqual(result.failures, []);
  assert.equal(result.calls.some(([kind]) => kind === 'createRef'), false);
  assert.equal(result.calls.some(([kind]) => kind === 'createPR'), false);
  assert.equal(result.calls.some(([kind]) => kind === 'writeFile'), true);
});

test('fork PR with feat/issue-N head does not masquerade as manual repository work', async () => {
  const forkPr = {head: {ref: 'feat/issue-71', repo: {full_name: 'contributor/project'}}};
  const result = await run({manualPulls: [forkPr]});
  assert.deepEqual(result.failures, []);
  assert.equal(result.calls.some(([kind]) => kind === 'createRef'), true);
});

test('automatic opened and labeled events also refuse existing manual work', async () => {
  for (const [eventName, action] of [['issues', 'opened'], ['issues', 'labeled']]) {
    const result = await run({eventName, action, manualRef: true});
    assert.match(result.failures[0], /existing manual work/);
    assert.equal(result.calls.some(([kind]) => kind === 'createRef' || kind === 'writeFile'), false);
  }
});

test('workflow event guards keep edited and unlabeled events out', () => {
  const issueEvents = workflow.match(/^  issues:\n    types: \[([^\]]+)\]/m)?.[1];
  assert.equal(issueEvents, 'opened, labeled');
  assert.doesNotMatch(issueEvents, /edited|unlabeled/);
  assert.match(workflow, /github\.event\.action == 'opened'/);
  assert.match(workflow, /github\.event\.action == 'labeled'/);
});

test('unlabeled and closed issues are refused', async () => {
  const unlabeled = await run({labels: []});
  assert.match(unlabeled.failures[0], /one of labels/);
  assert.equal(unlabeled.calls.some(([kind]) => kind === 'createRef' || kind === 'writeFile'), false);
  const closed = await run({state: 'closed'});
  assert.match(closed.failures[0], /is closed/);
  assert.equal(closed.calls.some(([kind]) => kind === 'createRef' || kind === 'writeFile'), false);
});

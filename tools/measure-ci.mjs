#!/usr/bin/env node
// Measure the complete Docker-only suite and its isolated YDB/CLI environment.
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync, appendFileSync, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outArg = process.argv[2];
if (process.argv.length !== 3 || !outArg || !path.isAbsolute(outArg)) {
  console.error('Usage: node tools/measure-ci.mjs ABSOLUTE_OUTPUT_DIRECTORY');
  process.exit(2);
}
const output = path.resolve(outArg);
const relative = path.relative(root, output);
if (!relative || (!relative.startsWith('..' + path.sep) && relative !== '..')) {
  console.error('Output directory must be outside the repository');
  process.exit(2);
}
mkdirSync(output, { recursive: true });
const actual = realpathSync(output);
const actualRelative = path.relative(realpathSync(root), actual);
if (!actualRelative || (!actualRelative.startsWith('..' + path.sep) && actualRelative !== '..')) {
  console.error('Output directory resolves inside the repository');
  process.exit(2);
}
if (readdirSync(actual).length) {
  console.error('Output directory must be empty');
  process.exit(2);
}

const startMs = Date.now();
const prefix = `zont-ci-${process.pid}-${startMs}`;
const image = process.env.ZONT_TEST_IMAGE || 'zont-analyzer:test-local';
const args = ['tests', '--durations=0', '--durations-min=0'];
const metadata = {
  schema_version: 1,
  started_epoch_ms: startMs,
  commit: null,
  working_tree_dirty: null,
  tests_tree_sha256: null,
  test_image: image,
  test_image_id: null,
  test_image_fingerprint: null,
  ydb_image_digest: null,
  command: 'deploy/check-tests.sh',
  args,
  pytest_common_args: ['-ra', '-p', 'no:cacheprovider', ...args],
  ydb_workers: Number(process.env.ZONT_TEST_WORKERS || 1),
  groups: { pure: { marker: 'not ydb', network: 'none' }, ydb: { marker: 'ydb', network: 'disposable' } },
  cpu_count: os.cpus().length,
  memory_total_bytes: os.totalmem(),
  docker_daemon: null,
  container_limits: {},
  container_prefix: prefix,
  preparation_end_epoch_ms: null,
  finished_epoch_ms: null,
  exit_status: null,
  sampling_errors: 0,
};
const save = () => writeFileSync(path.join(actual, 'run.json'), JSON.stringify(metadata, null, 2) + '\n');
const command = (bin, cmdArgs) => {
  const result = spawnSync(bin, cmdArgs, { cwd: root, encoding: 'utf8' });
  if (result.status !== 0) throw new Error(`${bin} ${cmdArgs[0]} failed: ${(result.stderr || '').trim()}`);
  return result.stdout.trim();
};
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }
  return value;
}
function hashTests() {
  const listed = spawnSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', 'tests'], { cwd: root });
  if (listed.status !== 0) throw new Error('git ls-files tests failed');
  const hash = createHash('sha256');
  for (const name of listed.stdout.toString('utf8').split('\0').filter(Boolean).sort()) {
    hash.update(name);
    hash.update('\0');
    hash.update(readFileSync(path.join(root, name)));
    hash.update('\0');
  }
  return hash.digest('hex');
}
function containerLimits(name) {
  const values = command('docker', ['inspect', '--format',
    '{{.HostConfig.NanoCpus}} {{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.PidsLimit}}',
    name]).split(' ');
  const keys = ['nano_cpus', 'cpu_quota_us', 'cpu_period_us', 'memory_bytes', 'memory_swap_bytes', 'pids_limit'];
  return Object.fromEntries(keys.map((key, index) => [key, values[index] === '<nil>' ? null : Number(values[index])]));
}

function collectTests(exitCode) {
  const groups = {};
  const tests = new Map();
  for (const group of ['pure', 'ydb']) {
    const file = path.join(actual, `${group}-tests.json`);
    if (!existsSync(file)) {
      if (exitCode === 0) throw new Error(`Missing ${group} test measurements`);
      continue;
    }
    const payload = JSON.parse(readFileSync(file));
    const { tests: reports, ...summary } = payload;
    groups[group] = summary;
    if (exitCode === 0 && payload.exit_status !== 0) throw new Error(`${group} tests failed`);
    for (const report of reports) {
      if (tests.has(report.nodeid)) throw new Error(`Test ran in both groups: ${report.nodeid}`);
      tests.set(report.nodeid, { ...report, group });
    }
  }
  writeFileSync(path.join(actual, 'tests.json'), JSON.stringify({
    exit_status: exitCode, groups, tests: [...tests.values()].sort((a, b) => a.nodeid.localeCompare(b.nodeid)),
  }));
}

let sampling = false;
let timer;
let child;
async function sample() {
  if (sampling) return;
  sampling = true;
  try {
    const names = command('docker', ['ps', '--format', '{{.Names}}']).split('\n')
      .filter((name) => ['ydb', 'test', 'pure'].some(role => name === `${prefix}-${role}`));
    for (const name of names) {
      const role = name.slice(prefix.length + 1);
      if (!metadata.container_limits[role]) {
        metadata.container_limits[role] = containerLimits(name);
        save();
      }
    }
    if (names.length) {
      const lines = command('docker', ['stats', '--no-stream', '--format', '{{json .}}', ...names]);
      for (const line of lines.split('\n')) {
        if (!line) continue;
        const row = JSON.parse(line);
        const name = row.Name;
        if (!['ydb', 'test', 'pure'].some(role => name === `${prefix}-${role}`)) continue;
        appendFileSync(path.join(actual, 'docker-stats.jsonl'), JSON.stringify({
          epoch_ms: Date.now(), container: name.slice(prefix.length + 1),
          cpu_percent: row.CPUPerc, memory_usage: row.MemUsage,
          memory_percent: row.MemPerc, pids: row.PIDs, block_io: row.BlockIO,
        }) + '\n');
      }
    }
  } catch {
    // A container may exit between ps and stats; the next sample can still succeed.
    metadata.sampling_errors += 1;
  } finally {
    sampling = false;
  }
}

try {
  metadata.commit = command('git', ['rev-parse', 'HEAD']);
  metadata.working_tree_dirty = Boolean(command('git', ['status', '--porcelain']));
  metadata.tests_tree_sha256 = hashTests();
  const inspected = JSON.parse(command('docker', ['image', 'inspect', image]))[0];
  metadata.test_image_id = inspected.Id;
  // Docker's classic and containerd stores expose different kinds of image IDs.
  // Compare the immutable filesystem layers and execution configuration instead.
  const configKeys = ['Cmd', 'Entrypoint', 'Env', 'User', 'WorkingDir', 'Labels', 'Volumes', 'ExposedPorts', 'StopSignal'];
  const fingerprint = canonical({
    architecture: inspected.Architecture, os: inspected.Os, layers: inspected.RootFS.Layers,
    config: Object.fromEntries(configKeys.map(key => [key, inspected.Config[key] ?? null])),
  });
  metadata.test_image_fingerprint = 'sha256:' + createHash('sha256').update(JSON.stringify(fingerprint)).digest('hex');
  const [daemonCpus, daemonMemory] = command('docker', ['info', '--format', '{{.NCPU}} {{.MemTotal}}']).split(' ');
  metadata.docker_daemon = { cpu_count: Number(daemonCpus), memory_total_bytes: Number(daemonMemory) };
  const script = readFileSync(path.join(root, 'deploy/check-ydb.sh'), 'utf8');
  metadata.ydb_image_digest = script.match(/YDB_IMAGE=ydbplatform\/local-ydb@(sha256:[a-f0-9]+)/)?.[1] || null;
  if (!metadata.ydb_image_digest) throw new Error('YDB image digest missing from check-ydb.sh');
  metadata.preparation_end_epoch_ms = Date.now();
  save();
  timer = setInterval(sample, 1000);
  child = spawn(path.join(root, 'deploy/check-tests.sh'), args, {
    cwd: root, stdio: 'inherit', env: {
      ...process.env, ZONT_TEST_IMAGE: image, ZONT_METRICS_DIR: actual, ZONT_CONTAINER_PREFIX: prefix,
    },
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
  const result = await new Promise((resolve) => {
    child.on('error', (error) => resolve({ code: 1, error: error.message }));
    child.on('exit', (code, signal) => resolve({ code: code ?? (signal === 'SIGINT' ? 130 : 143), signal }));
  });
  clearInterval(timer);
  while (sampling) await new Promise((resolve) => setTimeout(resolve, 50));
  metadata.finished_epoch_ms = Date.now();
  metadata.exit_status = result.code;
  collectTests(result.code);
  if (result.error) metadata.error = result.error;
  if (result.signal) metadata.signal = result.signal;
  save();
  command(process.execPath, [path.join(root, 'tools/summarize-ci.mjs'), actual]);
  console.error(`Measurement ${result.code === 0 ? 'complete' : 'failed'} (exit ${result.code}); results: ${actual}`);
  process.exitCode = result.code;
} catch (error) {
  if (timer) clearInterval(timer);
  metadata.finished_epoch_ms = Date.now();
  metadata.exit_status = 1;
  metadata.error = error.message;
  save();
  console.error(`Measurement failed: ${error.message}; results: ${actual}`);
  process.exitCode = 1;
}

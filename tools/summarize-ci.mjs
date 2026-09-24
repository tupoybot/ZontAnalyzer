#!/usr/bin/env node
// Summarize an artifact directory without publishing host names or hardware IDs.
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';

const directory = process.argv[2];
if (process.argv.length !== 3 || !directory) {
  console.error('Usage: node tools/summarize-ci.mjs METRICS_DIRECTORY');
  process.exit(2);
}
const read = name => JSON.parse(readFileSync(path.join(directory, name), 'utf8'));
const lines = name => existsSync(path.join(directory, name))
  ? readFileSync(path.join(directory, name), 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
const run = read('run.json');
const tests = read('tests.json');
const starts = new Map();
const phases = {};
for (const event of lines('phases.jsonl')) {
  if (event.event === 'start') starts.set(event.phase, event.epoch_ms);
  if (event.event === 'end' && starts.has(event.phase)) {
    phases[event.phase] = (event.epoch_ms - starts.get(event.phase)) / 1000;
  }
}
function bytes(value) {
  const match = /^([\d.]+)\s*([kKMGT]?i?B)/.exec(value || '');
  if (!match) return null;
  const units = { B: 1, kB: 1000, KB: 1000, MB: 1e6, GB: 1e9, TB: 1e12,
    KiB: 1024, MiB: 1024 ** 2, GiB: 1024 ** 3, TiB: 1024 ** 4 };
  return Number(match[1]) * units[match[2]];
}
const resources = {};
for (const sample of lines('docker-stats.jsonl')) {
  const value = resources[sample.container] ||= { samples: 0, cpu_percent_sum: 0, cpu_percent_peak: 0,
    memory_bytes_peak: 0, block_read_bytes: null, block_write_bytes: null };
  value.samples++;
  const cpu = parseFloat(sample.cpu_percent);
  value.cpu_percent_sum += cpu;
  value.cpu_percent_peak = Math.max(value.cpu_percent_peak, cpu);
  value.memory_bytes_peak = Math.max(value.memory_bytes_peak, bytes(sample.memory_usage) || 0);
  if (sample.block_io) {
    const [read, written] = sample.block_io.split('/').map(part => bytes(part.trim()));
    value.block_read_bytes = Math.max(value.block_read_bytes || 0, read || 0);
    value.block_write_bytes = Math.max(value.block_write_bytes || 0, written || 0);
  }
}
for (const value of Object.values(resources)) {
  value.cpu_percent_mean = value.cpu_percent_sum / value.samples;
  delete value.cpu_percent_sum;
}
const timings = { setup: 0, call: 0, teardown: 0 };
const cases = tests.tests.map(test => {
  const row = { nodeid: test.nodeid, group: test.group || 'combined', worker: test.worker || 'main' };
  let total = 0;
  for (const phase of Object.keys(timings)) {
    const duration = test[phase]?.duration_seconds || 0;
    row[phase] = duration;
    total += duration;
    timings[phase] += duration;
  }
  row.total = total;
  row.outcomes = Object.keys(timings).map(phase => test[phase]?.outcome || 'missing').join('/');
  return row;
});
const pools = {};
for (const group of Object.values(tests.groups || {})) {
  const owners = Object.keys(group.workers || {}).length ? Object.values(group.workers) : [group];
  for (const owner of owners) for (const [key, value] of Object.entries(owner.schema_pool || {})) {
    pools[key] = (pools[key] || 0) + value;
  }
}
const summary = {
  commit: run.commit, working_tree_dirty: run.working_tree_dirty ?? null,
  test_image_id: run.test_image_id, test_image_fingerprint: run.test_image_fingerprint ?? null,
  ydb_image_digest: run.ydb_image_digest, tests_tree_sha256: run.tests_tree_sha256,
  command: run.command, args: run.args, ydb_workers: run.ydb_workers ?? 1,
  exit_status: run.exit_status, measured_wall_seconds: (run.finished_epoch_ms - run.started_epoch_ms) / 1000,
  phases, test_count: cases.length, phase_seconds: timings,
  failures: cases.filter(test => test.outcomes.includes('failed')).map(test => test.nodeid),
  skipped: cases.filter(test => test.outcomes.includes('skipped')).map(test => test.nodeid),
  host: { cpu_count: run.cpu_count, memory_total_bytes: run.memory_total_bytes },
  docker_daemon: run.docker_daemon, container_limits: run.container_limits,
  resources, schema_pool: pools, sampling_errors: run.sampling_errors ?? null,
  slowest: [...cases].sort((a, b) => b.total - a.total).slice(0, 15),
};
const columns = ['nodeid', 'group', 'worker', 'setup', 'call', 'teardown', 'total', 'outcomes'];
const csv = [columns, ...cases.map(test => columns.map(key => test[key]))]
  .map(row => row.map(value => `"${String(value).replaceAll('"', '""')}"`).join(',')).join('\n') + '\n';
writeFileSync(path.join(directory, 'test-durations.csv'), csv);
writeFileSync(path.join(directory, 'summary.json'), JSON.stringify(summary, null, 2) + '\n');
console.log(JSON.stringify({ ...summary, slowest: summary.slowest.slice(0, 5) }, null, 2));

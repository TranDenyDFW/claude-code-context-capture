#!/usr/bin/env node
// Run every self-test in this repo and report one total.
//
// Why this exists: the checks were always there, but running them meant a hand-written shell loop,
// retyped every time, which is how a file quietly stops being covered. A reviewer looking at the
// repo concluded there was no test suite at all, because nothing named one.
//
// THE POINT OF THIS FILE IS THAT IT CAN FAIL. Two traps it is built to avoid:
//
//   1. `mirror-core.mjs` and `paths.mjs` exit 0 for ANY argument, self-test or not. A runner that
//      counts exit 0 as a pass scores them green and reports a total that is a lie. They are named
//      as exempt below, and a NEW file with no self-test is an error rather than a silent pass.
//   2. The Python checks need `data/context.db`, which is gitignored and absent in CI. A check that
//      could not run is a FAILURE, never a warning, so they are reported as SKIPPED with the reason
//      and the run says so in its summary rather than printing a total that implies full coverage.
//
// Usage: node tools/run_tests.mjs [--node-only]

import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const ROOT = dirname(dirname(SELF));
const NODE_ONLY = process.argv.includes('--node-only');

// Files with no self-test, and the reason. Anything NOT listed here is required to have one, so
// adding a tool without checks fails this runner instead of slipping through.
const EXEMPT = new Map([
  ['tools/mirror-core.mjs', 'pure math, no I/O; covered by mirror.mjs --self-test and --validate'],
  ['tools/paths.mjs', 'path constants only, nothing to exercise'],
]);

const CHECKS = /\((\d+) checks\)/;

function nodeTargets() {
  const out = [];
  for (const dir of ['tools', 'hooks']) {
    const abs = join(ROOT, dir);
    if (!existsSync(abs)) continue;
    for (const name of readdirSync(abs)) {
      // Skip THIS file. It lives in tools/ and ends in .mjs like everything else, so the first
      // version discovered itself, ran itself with --self-test, and recursed until the shell
      // timeout killed the tree. Compared by resolved path rather than by name, so renaming the
      // runner cannot bring the recursion back.
      if (join(abs, name) === SELF) continue;
      if (name.endsWith('.mjs')) out.push(`${dir}/${name}`);
    }
  }
  return out.sort();
}

const results = [];
let total = 0;
let failed = 0;
let skipped = 0;

for (const rel of nodeTargets()) {
  if (EXEMPT.has(rel)) {
    results.push({ rel, state: 'exempt', note: EXEMPT.get(rel) });
    continue;
  }
  const run = spawnSync(process.execPath, [join(ROOT, rel), '--self-test'],
                        { encoding: 'utf8', cwd: ROOT, timeout: 60_000 });
  const text = `${run.stdout || ''}${run.stderr || ''}`;
  const match = text.match(CHECKS);
  const count = match ? Number(match[1]) : 0;

  // Exit 0 is not enough. A file that never printed a check count did not run a self-test, which
  // is the exact shape of the two exempt files, and the reason a naive runner over-reports.
  if (run.status !== 0 || run.signal) {
    failed++;
    results.push({ rel, state: 'FAIL',
                   note: run.signal ? `killed after 60s (${run.signal})` : `exit ${run.status}`,
                   tail: text.trim().split('\n').slice(-3).join(' | ') });
  } else if (!match) {
    failed++;
    results.push({ rel, state: 'FAIL', note: 'exit 0 but printed no check count, so it has no ' +
                                             'self-test: add one, or add it to EXEMPT with a reason' });
  } else {
    total += count;
    results.push({ rel, state: 'pass', count });
  }
}

// Python. Store-dependent, so it is skipped rather than failed when the store is absent, and the
// skip is reported loudly enough that nobody reads the total as full coverage.
const store = join(ROOT, 'data', 'context.db');
const PY = [
  ['tools/table_audit.py', ['--self-test'], 'gate self-test'],
  ['tools/table_audit.py', [], 'audit of the live app'],
  ['tools/session_checks.py', [], 'Session tab features against the store'],
];
if (!NODE_ONLY) {
  for (const [rel, args, what] of PY) {
    if (!existsSync(store)) {
      skipped++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                     note: `needs data/context.db, which is gitignored and absent here (${what})` });
      continue;
    }
    const run = spawnSync('python', [join(ROOT, rel), ...args],
                          { encoding: 'utf8', cwd: ROOT, timeout: 300_000 });
    const text = `${run.stdout || ''}${run.stderr || ''}`;
    const match = text.match(CHECKS);
    if (run.status !== 0 || run.signal) {
      failed++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'FAIL',
                     note: run.signal ? `killed after 300s (${run.signal})` : `exit ${run.status}`,
                     tail: text.trim().split('\n').slice(-2).join(' | ') });
    } else {
      if (match) total += Number(match[1]);
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'pass',
                     count: match ? Number(match[1]) : null, note: match ? '' : what });
    }
  }
}

for (const r of results) {
  const label = r.rel.padEnd(38);
  if (r.state === 'pass') {
    console.log(`  PASS     ${label} ${r.count === null ? '' : `${r.count} checks`}${r.note ? `  (${r.note})` : ''}`);
  } else if (r.state === 'exempt') {
    console.log(`  exempt   ${label} ${r.note}`);
  } else if (r.state === 'SKIPPED') {
    console.log(`  SKIPPED  ${label} ${r.note}`);
  } else {
    console.log(`  FAIL     ${label} ${r.note}`);
    if (r.tail) console.log(`           ${r.tail}`);
  }
}

const exempt = results.filter((r) => r.state === 'exempt').length;
console.log('');
console.log(`  ${total} checks across ${results.filter((r) => r.state === 'pass').length} files, ` +
            `${exempt} exempt, ${skipped} skipped, ${failed} failed`);
if (skipped) {
  console.log('  NOT A FULL RUN: the skipped checks above did not execute, so this total does not ' +
              'cover the dashboard.');
}
console.log(failed ? 'SUITE FAIL' : skipped ? 'SUITE PASS (partial)' : 'SUITE PASS');
process.exit(failed ? 1 : 0);

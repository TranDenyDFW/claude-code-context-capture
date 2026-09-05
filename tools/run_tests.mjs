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
// Usage: node tools/run_tests.mjs [--node-only] [--strict] [--self-test]

import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);
const ROOT = dirname(dirname(SELF));
const NODE_ONLY = process.argv.includes('--node-only');
// A SKIPPED CHECK IS A FAILURE WHERE IT MATTERS. The runner has always SAID so, printing
// "NOT A FULL RUN" and "SUITE PASS (partial)", and then exited 0, which is the half GitHub reads.
// CI skipped the frontend typecheck and Vitest on every run it ever made, because it never
// installed frontend/node_modules, and reported green.
//
// A flag rather than a rule, so a fresh clone can still run the Python and capture checks without
// the frontend installed and get an honest partial. CI passes --strict and gets neither.
const STRICT = process.argv.includes('--strict');

// "(N checks)" is what every self-test prints. pytest prints "N passed" instead, and counting it
// as zero made the suite total understate itself by more than a hundred assertions.
const CHECKS = /\((\d+) checks\)|(\d+) passed/;

// pytest's own summary line, e.g. "379 passed, 12 skipped in 181.20s". Until now the runner read
// only the passed figure, so the 39 data-dependent `pytest.skip` sites in tests/ never reached the
// suite total: the suite said "0 skipped" about its own entries while pytest was skipping tests
// underneath it. That is how CI collected 1405 checks where a Windows checkout collected 1417 and
// nobody could say why.
const PYTEST_TOKEN = /(\d+) (passed|skipped|failed|xfailed|xpassed|errors?|deselected)\b/g;
function pytestCounts(text) {
  const lines = String(text).trim().split('\n');
  const summary = [...lines].reverse().find((l) => /\d+ (passed|failed|error)/.test(l)) || '';
  const out = {};
  for (const m of summary.matchAll(PYTEST_TOKEN)) out[m[2].replace(/s$/, '')] = Number(m[1]);
  return out;
}
const isPytest = (rel, args) => rel === '-m' && args[0] === 'pytest';

// A skip against the FIXTURE is a fixture gap, not data. The fixture is deterministic, so a test
// that cannot find what it needs there will never find it there, and under --strict that fails.
// A skip against the live store is allowed and reported by name: that store is whatever it is.
function judgePytest(text, rel, args, opts, strict) {
  if (!isPytest(rel, args)) return { fail: false, note: '' };
  const counts = pytestCounts(text);
  const skipped = counts.skipped || 0;
  if (!skipped) return { fail: false, note: '' };
  const reasons = String(text).split('\n')
    .filter((l) => /^SKIPPED \[/.test(l))
    .map((l) => l.replace(/^SKIPPED \[\d+\] /, '').trim());
  const shape = opts?.fixture ? 'fixture' : 'live store';
  if (opts?.fixture && strict) {
    return {
      fail: true,
      note: `${skipped} test(s) skipped against the deterministic fixture; a skip there is a ` +
            `fixture gap, not data`,
      tail: reasons.slice(0, 20).join('\n'),
    };
  }
  return { fail: false, note: `${skipped} skipped (${shape} shape): ${reasons.slice(0, 3).join(' | ')}` };
}

// EVERY captured output goes through this before it is matched.
//
// Vitest colours its summary when CI is set, so on GitHub the line arrives as
// `\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m99 passed\x1b[39m`, and `/Tests\s+(\d+) passed/`
// cannot match it: `\s+` does not match an escape sequence. Locally there is no colour and the
// same regex matched, so the frontend row passed with no count and the CI total silently
// understated by 99 checks while reading as though it had covered them.
//
// Applied to ALL of them rather than to the one that bit, because every other count here is read
// out of a tool's stdout the same way and any of them can start colouring on a whim. The pattern
// is the standard control-sequence shape: ESC [ ... final-byte.
const ANSI = /\u001B\[[0-9;]*[A-Za-z]/g;
const plain = (out) => String(out || '').replace(ANSI, '');

// THE LAST TWO LINES ARE OFTEN NOT THE FAILURE. An independent reviewer hit this: a pytest entry
// failed, and the suite report showed only `RequestsDependencyWarning: urllib3 ...` from stderr,
// because on Windows that warning is the last thing written. The reviewer had to re-run the entry
// by hand to find out WHICH two tests failed, which is the one thing the report existed to say.
//
// So the tail prefers lines that name a failure, and falls back to the last lines only when it
// finds none. A report that hides the failure is worse than no report: it looks like information.
const NOISE = /RequestsDependencyWarning|warnings\.warn|^\s*$/;
const BLAME = /^(FAILED|ERROR|E\s+|\s*assert |AssertionError|Traceback|\s+File ")|\bFAIL\b/;
function tailOf(text, keep = 4) {
  const lines = text.trim().split(/\r?\n/).map((l) => l.trimEnd()).filter((l) => !NOISE.test(l));
  const blamed = lines.filter((l) => BLAME.test(l));
  const chosen = (blamed.length ? blamed : lines).slice(-keep);
  return chosen.join(' | ').slice(0, 600);
}


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

// Each entry names the string a SUCCESSFUL run must print. Exit 0 is not enough: a stub that
// exits 0 and prints nothing passed every one of these until a reviewer tried it, and the total
// quietly dropped by fourteen while the run still said PASS.
const PY = [
  ['tools/table_audit.py', ['--self-test'], 'gate self-test', 'SELF-TEST PASS'],
  // Does constraints-ci.txt still pin everything the requirements files name? Offline, so it
  // runs anywhere. It exists because mypy sat unpinned in that file from the commit that
  // created it through the commit that made the mypy step blocking, and the file's own
  // instructions did not catch it. An independent review did.
  ['tools/make_constraints.py', ['--self-test'], 'CI dependency pins cover the requirements',
   'SELF-TEST PASS', { noStore: true }],
  // The latency gate's own checks. Pure logic, no store, so it runs anywhere the suite does.
  //
  // The gate ITSELF is not in this suite on purpose: it needs a real store and a baseline
  // recorded on that store, and CI runs against a synthetic fixture three orders of magnitude
  // smaller, where it correctly refuses to compare. It is a phase-boundary gate, run by hand.
  // What belongs here is proof that it can still tell a regression from noise.
  ['tools/bench.py', ['--self-test'], 'latency gate self-test', 'SELF-TEST PASS',
   { noStore: true }],
  // The parity differ's own checks, and the source switch's. Same reasoning as bench.py: the FULL
  // parity run needs a store with enough in it to compare, so it is a phase-boundary gate run by
  // hand. What runs here is proof that the differ can still spot a difference, which is the only
  // property that makes a passing parity run mean anything.
  ['tools/parity.py', ['--self-test'], 'API-vs-dashboard differ self-test', 'SELF-TEST PASS',
   { noStore: true }],
  ['-m', ['c4x.cli', '--self-test'], 'CLI backend switch self-test', 'SELF-TEST PASS',
   { noStore: true }],
  // The response cache, which is the only reason the migration can meet "don't make it slower".
  // Its checks are about correctness, not speed: a stale entry served past the age bound would be
  // a wrong pane that looks entirely right, which is the one failure a latency gate cannot see.
  ['-m', ['c4x.api.cache', '--self-test'], 'API response cache self-test', 'SELF-TEST PASS',
   { noStore: true }],
  ['tools/table_audit.py', [], 'audit of the live app', 'AUDIT PASS'],
  // The same DATA rules, applied to the API payload instead of a Dash component tree, so they
  // outlive Dash. Store-dependent, and not marked noStore: importing table_audit for its rule
  // definitions pulls in c4x.store, which refuses to run without one.
  //
  // BOTH audits run while both frontends exist. The new one cannot prove every table-building code
  // path was reached, which is what the old one instruments construction to do, so retiring the old
  // one early would lose that quietly.
  ['tools/contract_audit.py', ['--self-test'], 'API contract audit self-test', 'SELF-TEST PASS'],
  ['tools/contract_audit.py', [], 'audit of the API payloads', 'AUDIT PASS'],
  // The pytest suite in tests/ replaced tools/session_checks.py, which checked three Session-tab
  // features by hand. Those checks were migrated into tests/test_session.py rather than deleted,
  // and the suite now covers every tab. `-q` still prints the "N passed" line the marker needs.
  ['-m', ['pytest', 'tests/', '-q', '-rs', '-p', 'no:warnings'], 'every tab, against SQL written independently', ' passed'],
  // AND AGAIN, against a freshly built synthetic fixture.
  //
  // The entry above runs against whatever store is present, which on a developer's machine is a
  // real one with 1,324 sessions and on CI is the fixture. Those are different shapes, and a test
  // that quietly depends on the real one passes here and fails there. That is not hypothetical:
  // three merges went to main with CI red because four tests demanded a session of more than 500
  // calls, which the fixture's longest (90) cannot satisfy, and nothing run locally could see it.
  //
  // Running both closes the gap, so a green run here means a green run in CI.
  ['-m', ['pytest', 'tests/', '-q', '-rs', '-p', 'no:warnings'],
   'the same suite against the synthetic fixture, which is the shape CI runs', ' passed',
   { fixture: true }],
  // AND ONCE MORE, AGAINST THE SHAPE A NEW USER ACTUALLY HAS.
  //
  // Both fixtures above create context_baselines and the four probe tables before writing a row
  // (make_fixture.mjs:88-89), because with them absent the Diagnostics tab raises. So the fixture
  // was more complete than any store the documented install path produces: probe.mjs and
  // `breakdown.mjs --calibrate` make those five tables and nothing in install.mjs, the hooks or
  // the README runs either - README.md does not contain the word "probe".
  //
  // The result was a gate that could not see its own subject. `table_audit.py` walks every tab AND
  // every registered sub-panel and reports an exception panel as a failure; it works; it had never
  // once been pointed at a store without those tables. Three surfaces raised there from the day
  // they were written: tab-diagnostics and two of the Window tab's three sub-panels. A reviewer on
  // a day-old install found one of the three by opening the page.
  //
  // These two entries are the whole fix for that class. They are cheap, they run the same code,
  // and they are the only thing in this repo that executes a first-run store.
  ['tools/table_audit.py', ['--render-only'],
   'every tab and sub-panel on a first-run store (no probe, no baseline)',
   'AUDIT PASS', { bareFixture: true }],
  ['-m', ['pytest', 'tests/test_tabs_render.py', '-q', '-rs', '-p', 'no:warnings'],
   'no tab renders an apology on a first-run store', ' passed', { bareFixture: true }],
  // Ruff runs HERE, inside the suite, and not as a command anyone remembers to type.
  //
  // It was a separate step for seven stages, and its verdict was being read off the last line of
  // its output. Ruff prints an advisory "N hidden fixes" line AFTER "Found 8 errors", so the last
  // line says nothing about whether it passed: three stages merged carrying lint errors while the
  // gate reported clean. A check whose result depends on how you read it is not a gate.
  ['-m', ['ruff', 'check', '.'], 'style, as a gate rather than a habit', 'All checks passed'],
  // MYPY RUNS HERE for exactly the reason ruff and eslint do, and it is the third time this file
  // has learned it. It was the last blocking CI step the suite did not run, so a branch could pass
  // 1,648 local checks and fail all three CI legs on four `var-annotated` errors in one file. That
  // is not hypothetical either: it happened on the branch that added this line, and the four
  // annotations were written twice, once against a red CI log instead of once against a red suite.
  //
  // A check the machine runs and the developer cannot is a check that fails late and by surprise.
  ['-m', ['mypy'], 'types over c4x/, the same step CI blocks on', 'Success: no issues found'],
];

// Files with no self-test, and the reason. Anything NOT listed here is required to have one, so
// adding a tool without checks fails this runner instead of slipping through.
const EXEMPT = new Map([
  ['tools/mirror-core.mjs', 'pure math, no I/O; covered by mirror.mjs --self-test and --validate'],
  ['tools/paths.mjs', 'path constants only, nothing to exercise'],
]);

// This runner answers --self-test WITHOUT scanning, for two reasons. Every tool here carries checks
// and the one that runs them should not be the exception. And it stops a COPY of this file under
// another name from recursing: excluding itself by resolved path protects the original, not a
// duplicate, and a duplicate that ignored the flag would fork until something killed it.
if (process.argv.includes('--self-test')) {
  const cases = [
    ['skips itself by resolved path, not by filename', nodeTargets().every((r) => join(ROOT, r) !== SELF)],
    ['every exempt entry carries a reason', [...EXEMPT.values()].every((v) => v && v.length > 10)],
    ['the check-count pattern matches a real self-test line', CHECKS.test('SELF-TEST PASS (77 checks)')],
    ['and does not match a line with no count', !CHECKS.test('SELF-TEST PASS')],
    ['every python entry declares the marker its success must print',
     PY.every(([, , , marker]) => typeof marker === 'string' && marker.length > 0)],
    // The pytest summary is read in full, and a skip on the fixture can fail the run. Each of
    // these is a case that was silently wrong before: the parser only knew "passed".
    ['pytest summary parsing reads skipped, not only passed',
     JSON.stringify(pytestCounts('SKIPPED [1] tests/t.py:9: no data\n379 passed, 12 skipped in 181.20s'))
       === JSON.stringify({ passed: 379, skipped: 12 })],
    ['pytest summary parsing finds the summary below the skip reasons',
     pytestCounts('SKIPPED [1] a\nSKIPPED [1] b\n5 passed, 2 skipped, 1 xfailed in 3.0s').xfailed === 1],
    ['a skip against the fixture FAILS under --strict',
     judgePytest('SKIPPED [1] tests/t.py:9: no data\n1 passed, 1 skipped in 1.0s',
                 '-m', ['pytest', 'tests/'], { fixture: true }, true).fail === true],
    ['and names the reason in its tail',
     /no data/.test(judgePytest('SKIPPED [1] tests/t.py:9: no data\n1 passed, 1 skipped in 1.0s',
                                '-m', ['pytest', 'tests/'], { fixture: true }, true).tail)],
    ['a skip against the fixture is reported, not failed, without --strict',
     judgePytest('1 passed, 1 skipped in 1.0s', '-m', ['pytest', 'tests/'], { fixture: true }, false).fail === false],
    ['a skip against the live store is reported, never failed, even under --strict',
     judgePytest('1 passed, 1 skipped in 1.0s', '-m', ['pytest', 'tests/'], {}, true).fail === false],
    ['a pytest run with no skips adds no note',
     judgePytest('5 passed in 1.0s', '-m', ['pytest', 'tests/'], { fixture: true }, true).note === ''],
    ['a non-pytest entry is never judged as pytest',
     judgePytest('5 passed, 9 skipped', 'tools/x.py', ['--self-test'], { fixture: true }, true).fail === false],
    ['both pytest entries ask for skip reasons (-rs), or the tail would be empty',
     PY.filter(([rel, args]) => isPytest(rel, args)).every(([, args]) => args.includes('-rs'))],
  ];
  let bad = 0;
  for (const [what, ok] of cases) {
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`);
    if (!ok) bad++;
  }
  console.log(`SELF-TEST ${bad ? 'FAIL' : 'PASS'} (${cases.length} checks)`);
  process.exit(bad ? 1 : 0);
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
  // 300s, raised from 60. `harvest.mjs --self-test` takes 52 seconds on an idle machine, which left
  // eight seconds of headroom and none at all during a full suite run: it was killed at 60s while
  // passing 84 checks in isolation seconds later. A timeout that fires on a slow machine rather
  // than a hung process reports a green suite as red, which teaches people to re-run rather than
  // to read. Still bounded, because a genuinely hung self-test must not stall the suite forever.
  const run = spawnSync(process.execPath, [join(ROOT, rel), '--self-test'],
                        { encoding: 'utf8', cwd: ROOT, timeout: 300_000, windowsHide: true });
  const text = plain(run.stdout) + plain(run.stderr);
  const match = text.match(CHECKS);
  const count = match ? Number(match[1]) : 0;

  // Exit 0 is not enough. A file that never printed a check count did not run a self-test, which
  // is the exact shape of the two exempt files, and the reason a naive runner over-reports.
  if (run.status !== 0 || run.signal) {
    failed++;
    results.push({ rel, state: 'FAIL',
                   note: run.signal ? `killed after 60s (${run.signal})` : `exit ${run.status}`,
                   tail: tailOf(text) });
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
if (NODE_ONLY) {
  // Named rather than silent: without this the run printed "0 skipped" and an unqualified PASS
  // while every dashboard check sat out.
  for (const [rel, args, what] of PY) {
    skipped++;
    results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                   note: `--node-only was passed, so this did not run (${what})` });
  }
} else {
  // Built once, before the loop, for the entries that ask for it. In tmp/ rather than at the
  // default store path: overwriting a developer's real store to run a test would be a far worse
  // bug than the one this exists to catch.
  const fixture = join(ROOT, 'tmp', 'suite-fixture.db');
  // The first-run shape, built the same way and then stripped of the five tables no install path
  // creates. See the bareFixture entries above for why this exists.
  const bare = join(ROOT, 'tmp', 'suite-fixture-bare.db');
  let bareBuilt = false;
  let fixtureBuilt = false;
  if (!NODE_ONLY && PY.some(([, , , , o]) => o?.fixture)) {
    rmSync(fixture, { force: true });
    const built = spawnSync(process.execPath, [join(ROOT, 'tools', 'make_fixture.mjs'),
                                               '--out', fixture],
                            { encoding: 'utf8', cwd: ROOT, timeout: 300_000, windowsHide: true });
    fixtureBuilt = built.status === 0 && existsSync(fixture);
    if (!fixtureBuilt) {
      failed++;
      results.push({ rel: 'tools/make_fixture.mjs --out tmp/suite-fixture.db', state: 'FAIL',
                     note: 'could not build the fixture the suite runs against',
                     tail: tailOf(`${built.stdout || ''}${built.stderr || ''}`) });
    }
  }

  if (!NODE_ONLY && PY.some(([, , , , o]) => o?.bareFixture)) {
    rmSync(bare, { force: true });
    const built = spawnSync(process.execPath, [join(ROOT, 'tools', 'make_fixture.mjs'),
                                               '--out', bare, '--no-optional'],
                            { encoding: 'utf8', cwd: ROOT, timeout: 300_000, windowsHide: true });
    bareBuilt = built.status === 0 && existsSync(bare);
    if (!bareBuilt) {
      failed++;
      results.push({ rel: 'tools/make_fixture.mjs --no-optional', state: 'FAIL',
                     note: 'could not build the first-run fixture',
                     tail: tailOf(`${built.stdout || ''}${built.stderr || ''}`) });
    }
  }

  for (const [rel, args, what, marker, opts] of PY) {
    if (opts?.bareFixture && !bareBuilt) {
      skipped++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                     note: `the first-run fixture could not be built, so this did not run (${what})` });
      continue;
    }
    if (opts?.fixture && !fixtureBuilt) {
      skipped++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                     note: `the fixture could not be built, so this did not run (${what})` });
      continue;
    }
    // A self-test that touches no store runs everywhere, including a fresh clone. Without this
    // exemption the store gate below skipped the parity differ's and the latency gate's OWN checks
    // on any machine without data/context.db, which is every clone: the two checks that prove
    // those gates can still fail were the ones sitting out.
    if (!opts?.noStore && !existsSync(store)) {
      skipped++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                     note: `needs data/context.db, which is gitignored and absent here (${what})` });
      continue;
    }
    // An entry beginning with a dash is a python FLAG, not a path: `-m pytest ...`. Joining ROOT
    // onto it would spawn a file called "-m" that does not exist, and the failure would look like
    // a broken test rather than a broken runner.
    const argv = rel.startsWith('-') ? [rel, ...args] : [join(ROOT, rel), ...args];
    const run = spawnSync('python', argv, {
      encoding: 'utf8', cwd: ROOT, timeout: 900_000, windowsHide: true,
      // C4X_DB is the store override the app already honours, so the fixture run needs no special
      // support anywhere else in the codebase.
      env: opts?.fixture ? { ...process.env, C4X_DB: fixture }
        : opts?.bareFixture ? { ...process.env, C4X_DB: bare }
        : process.env,
    });
    const text = plain(run.stdout) + plain(run.stderr);
    const match = text.match(CHECKS);
    const count = match ? Number(match[1] ?? match[2]) : null;
    if (run.status !== 0 || run.signal) {
      failed++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'FAIL',
                     note: run.signal ? `killed after 300s (${run.signal})` : `exit ${run.status}`,
                     tail: tailOf(text) });
    } else if (!text.includes(marker)) {
      // Exit 0 with the marker absent means it did not do what it claims to do.
      failed++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'FAIL',
                     note: `exit 0 but never printed ${JSON.stringify(marker)}, so it did not run`,
                     tail: tailOf(text) });
    } else {
      const label = `${rel} ${args.join(' ')}`.trim() + (opts?.fixture ? '  [fixture]' : '');
      const verdict = judgePytest(text, rel, args, opts, STRICT);
      if (verdict.fail) {
        failed++;
        results.push({ rel: label, state: 'FAIL', note: verdict.note, tail: verdict.tail });
      } else {
        if (count) total += count;
        results.push({ rel: label, state: 'pass', count: count ?? null,
                       note: verdict.note || (count ? '' : what) });
      }
    }
  }
}

// The React frontend. Same rules as everything above: exit 0 is not enough, the run has to print
// the line a real run prints, and a skip is stated rather than absorbed into the total.
//
// It is SKIPPED, not failed, when node_modules is absent. The frontend is 149 MB of dependencies
// that a clone does not have until `npm install --prefix frontend`, and failing the whole suite
// over that would mean the Python and capture checks could not be run without it.
if (!NODE_ONLY) {
  const frontend = join(ROOT, 'frontend');
  const installed = existsSync(join(frontend, 'node_modules'));
  const FRONT = [
    [['run', 'typecheck'], 'the frontend still type-checks', null],
    [['run', 'test'], 'the payload reaches the DOM intact', 'Tests '],
  ];
  for (const [args, what, marker] of FRONT) {
    const label = `frontend npm ${args.join(' ')}`;
    if (!installed) {
      skipped++;
      results.push({ rel: label, state: 'SKIPPED',
                     note: `frontend/node_modules is absent; run npm install --prefix frontend (${what})` });
      continue;
    }
    const run = spawnSync('npm', [...args, '--prefix', frontend], {
      encoding: 'utf8', cwd: ROOT, timeout: 900_000, shell: process.platform === 'win32', windowsHide: true,
    });
    const text = plain(run.stdout) + plain(run.stderr);
    // NOT the shared CHECKS pattern. Vitest prints "Test Files  2 passed (2)" BEFORE
    // "Tests  22 passed (22)", and CHECKS matches "N passed" anywhere, so it took the file count
    // and the suite total silently read 20 lower than the number of checks that actually ran.
    // Exactly the failure this runner already had once, where the total dropped by fourteen while
    // the run still said PASS.
    const match = text.match(/Tests\s+(\d+) passed/);
    const count = match ? Number(match[1]) : null;
    if (run.status !== 0 || run.signal) {
      failed++;
      results.push({ rel: label, state: 'FAIL',
                     note: run.signal ? `killed (${run.signal})` : `exit ${run.status}`,
                     tail: tailOf(text) });
    } else if (marker && !text.includes(marker)) {
      failed++;
      results.push({ rel: label, state: 'FAIL',
                     note: `exit 0 but never printed ${JSON.stringify(marker)}, so it did not run`,
                     tail: tailOf(text) });
    } else if (marker && !count) {
      // A CHECK WHOSE RESULT CANNOT BE COUNTED HAS NOT DEMONSTRABLY RUN, which is the rule this
      // runner already applies to the node self-tests above. It did not apply it here, so on CI
      // this row passed with no number and the suite total silently understated by the whole of
      // the frontend: 99 locally, nothing there. The tail is printed because the text is the only
      // evidence of why, and the runner captures it rather than letting it through.
      failed++;
      results.push({ rel: label, state: 'FAIL',
                     note: `exit 0 and printed ${JSON.stringify(marker)}, but no check count `
                           + `could be read from the output, so the total would understate`,
                     // STDOUT, not the combined text. Vitest prints its summary to stdout and
                     // jsdom prints its warnings to stderr, so `tailOf(stdout + stderr)` is always
                     // the jsdom noise and never the line this failure is about.
                     tail: tailOf(plain(run.stdout) || '(stdout was empty)') });
    } else {
      if (count) total += count;
      results.push({ rel: label, state: 'pass', count: marker ? count : null,
                     note: marker ? '' : what });
    }
  }
}


// ESLint runs HERE too, for the same reason ruff does above: it was a CI-only step, so a local run
// could be green while CI was red on lint. That is not hypothetical. `tailOf()` was added to this
// very file and its call sites were never wired up, so it sat unused; the local suite passed,
// eslint failed in CI with "'tailOf' is defined but never used", and the fix had to be made twice.
//
// A check the machine runs and the developer cannot is a check that fails late and by surprise.
if (!NODE_ONLY) {
  // The pinned eslint from the root lockfile, via the `lint` script, so the suite and CI run
  // the same version. `npx --yes` fetched whatever was newest at that moment.
  const run = spawnSync('npm', ['run', 'lint'], {
    encoding: 'utf8', cwd: ROOT, timeout: 300_000, shell: process.platform === 'win32', windowsHide: true,
  });
  const text = plain(run.stdout) + plain(run.stderr);
  if (run.status !== 0 || run.signal) {
    failed++;
    results.push({ rel: 'eslint .', state: 'FAIL',
                   note: run.signal ? `killed (${run.signal})` : `exit ${run.status}`,
                   tail: tailOf(text) });
  } else {
    results.push({ rel: 'eslint .', state: 'pass', count: null,
                   note: 'the node tools lint clean, the same check CI runs' });
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
if (skipped && STRICT) {
  console.log('  --strict: a check that did not run is a failure.');
}
console.log(failed || (skipped && STRICT) ? 'SUITE FAIL'
            : skipped ? 'SUITE PASS (partial)' : 'SUITE PASS');
process.exit(failed || (skipped && STRICT) ? 1 : 0);

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
import { existsSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { winArg } from './paths.mjs';

const SELF = fileURLToPath(import.meta.url);
// A CHILD THAT PRINTS TOO MUCH IS KILLED, and the runner called it slow.
//
// spawnSync's default maxBuffer is 1 MB. A child that writes past it is killed with SIGTERM and the
// call returns error.code ENOBUFS with status null. The runner only looked at `signal`, so it
// reported "killed after 300s", and a leg that failed in eight seconds for writing 1 MB of pytest
// output was read as one that had run for five minutes. Measured: node -e writing 2 MB returns
// status null, signal SIGTERM, ENOBUFS, with 1,114,112 bytes captured.
//
// 64 MB, and the number is a ceiling rather than an expectation: the point is that a verbose leg
// fails on its own merits instead of being cut off, and that the failure says which happened.
const MAX_OUTPUT = 64 * 1024 * 1024;

// WHAT ACTUALLY HAPPENED TO THE CHILD, rather than a guess from `signal` alone.
//
// A signal meant "timed out" to this runner, and it printed a hardcoded 300s beside it. Two things
// were wrong: the number had drifted from the timeouts it claimed to describe, and the commonest
// signal here is not a timeout but SIGTERM from exceeding maxBuffer, which arrives with error.code
// ENOBUFS. Reading `error` first tells the two apart, which is the difference between "this leg is
// slow" and "this leg printed more than the runner would hold".
function whyItFailed(run) {
  if (run.error) return `${run.error.code || 'error'}: ${String(run.error.message).slice(0, 160)}`;
  if (run.signal) return `killed by ${run.signal} (a timeout, or output past ${MAX_OUTPUT >> 20} MB)`;
  return `exit ${run.status}`;
}

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


// A CONTROL CHARACTER IN SOURCE IS ALWAYS A BUG, AND ALWAYS AN INVISIBLE ONE.
//
// Three files in this repo carried one, all from the same authoring mistake: an escape written into
// a non-raw string by an editing script, so the two characters backslash-b became a single backspace
// byte. What that produces is worse than a syntax error, because it compiles and it reads correctly
// in every terminal, editor and diff:
//
//   run_tests.mjs     a decline marker that could never match, so every graceful skip in the python
//                     leg was reported as a failure. An external reviewer chased that across two
//                     full runs, called it non-deterministic, and could not find it. That is the cost.
//   statusline.mjs    the Bearer alternative of the secret-redaction pattern, dead, so a value that
//                     should never have been recorded would have been recorded verbatim.
//   test_projects.py  a path inside prose. Harmless, and the reason a search by symptom misses these.
//
// A reader cannot see them and a review cannot catch them, so a machine has to. Scoped to the text
// this repo authors: the images and the built frontend bundle are legitimately binary.
//
// ESC (u001b) is cut OUT of the range rather than merely described as excluded: the first version
// of this comment claimed the exclusion while the range still covered it, and the gate fired on
// six innocent files. Tab, CR and LF fall outside the ranges. The .md plan artifacts are skipped
// as a directory because they are captured terminal transcripts, not source this repo authored.
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001a\u001c-\u001f]/;
const SOURCE_EXT = /\.(mjs|js|jsx|ts|tsx|py|json|md|css|html|yml|yaml|toml|cfg|txt)$/;
const SKIP_DIR = new Set(['node_modules', 'dist', '.git', 'tmp', '__pycache__', '.venv', 'data', '.md']);

function sourceFiles(dir = ROOT, out = []) {
  for (const name of readdirSync(dir)) {
    if (SKIP_DIR.has(name)) continue;
    const abs = join(dir, name);
    if (statSync(abs).isDirectory()) sourceFiles(abs, out);
    else if (SOURCE_EXT.test(name)) out.push(abs);
  }
  return out;
}

function controlCharHits() {
  const hits = [];
  for (const abs of sourceFiles()) {
    const text = readFileSync(abs, 'utf8');
    if (!CONTROL.test(text)) continue;
    const line = text.split('\n').findIndex((l) => CONTROL.test(l)) + 1;
    hits.push(`${abs.slice(ROOT.length + 1).replace(/\\/g, '/')}:${line}`);
  }
  return hits;
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
  // The tool that decides what leaves this machine, and it had no checks at all. Its determinism
  // claim was false for every value that reached the fallback, which is exactly the path nobody
  // reads. No store: it works on a copy it is given.
  ['tools/redact.py', ['--self-test'], 'the public-image redaction, and its leak gate',
   'SELF-TEST PASS', { noStore: true }],
  // The gate against fabricated documentation, which was itself unchecked. Its own docstring
  // records a run that produced 51 unsupported claims out of 315.
  ['tools/docgen/check_notes.py', ['--self-test'], 'refuses notes the spec cannot support',
   'SELF-TEST PASS', { noStore: true }],
  // Had a self-test since it was written; the runner simply never called it.
  ['tools/pty-control-arm.py', ['--self-test'], 'the status-line control arm, parsing only',
   'SELF-TEST PASS', { noStore: true }],
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
/**
 * Python tools discovered from DISK, so a new one cannot arrive uncovered.
 *
 * The node side has enumerated its targets since it was written and states the guarantee at the top
 * of this file: anything not listed as exempt is REQUIRED to have a self-test, so adding a tool
 * without checks fails the runner instead of slipping through. The Python side was a hand-written
 * list, which cannot make that promise: it says what someone remembered, and says nothing at all
 * about what they did not. Nine Python tools were outside it, eight with no checks of any kind,
 * including `redact.py` - the tool that decides what leaves this machine.
 *
 * This does not RUN them; the PY list above still decides what runs and with which store. It makes
 * the absence visible: every discovered file must be either exercised there or exempt WITH A REASON.
 */
function pythonTargets() {
  const out = [];
  for (const dir of ['tools', join('tools', 'docgen')]) {
    const abs = join(ROOT, dir);
    if (!existsSync(abs)) continue;
    for (const name of readdirSync(abs)) {
      if (name.endsWith('.py') && !name.startsWith('__')) {
        out.push(`${dir.replace(/\\/g, '/')}/${name}`);
      }
    }
  }
  return out.sort();
}

// Every Python tool that is neither run above nor given checks, WITH the reason. A file here is a
// declared gap, which is the point: an undeclared one now fails the runner.
const PY_EXEMPT = new Map([
  ['tools/screenshots.py', 'drives a live browser against a running dashboard; it cannot assert '
    + 'anything without one, and the images it produces are reviewed by eye'],
  // The six docgen stages BELOW check_notes.py. They transform artifacts produced by a live run
  // (a DOM walk, a screenshot pass, a model call), so a check here would be a check on a fixture
  // nobody maintains. The one stage that is a GATE rather than a transform - check_notes.py, which
  // refuses claims the spec cannot support - is exercised above instead, because that is the one
  // whose silence costs something.
  ['tools/docgen/annotate.py', 'calls a model over a live spec; nothing to assert offline'],
  ['tools/docgen/assemble.py', 'writes the .docx from artifacts a live run produced'],
  ['tools/docgen/enrich.py', 'decorates a live spec with values read from the running app'],
  ['tools/docgen/inventory.py', 'walks the running dashboard DOM'],
  ['tools/docgen/merge.py', 'joins artifacts from the stages above'],
  ['tools/docgen/spec.py', 'derives the page spec from a live render'],
]);

const EXEMPT = new Map([
  ['tools/mirror-core.mjs', 'pure math, no I/O; covered by mirror.mjs --self-test and --validate'],

]);

// This runner answers --self-test WITHOUT scanning, for two reasons. Every tool here carries checks
// and the one that runs them should not be the exception. And it stops a COPY of this file under
// another name from recursing: excluding itself by resolved path protects the original, not a
// duplicate, and a duplicate that ignored the flag would fork until something killed it.
if (process.argv.includes('--self-test')) {
  const cases = [
    // THE DETECTOR IS PROVED FIRST, so this trio still fails on a clean tree. A scan that reports
    // nothing proves nothing by itself: it looks identical whether it works or matches nothing at
    // all. The known-bad input is the exact shape that shipped, built from a code point so that
    // this file never contains the byte it is looking for.
    ['the control-character detector catches a known-bad line',
     CONTROL.test('const declined = /' + String.fromCharCode(8) + "SKIPPED/.test(text);")],
    ['and does not fire on ordinary source, tab, CR and LF included',
     !CONTROL.test('function f() {\r\n\tconst r = /\\bword\\b/;\n}')],
    ['no source file in this repo carries a control character',
     controlCharHits().length === 0, controlCharHits().join(' ') || 'none'],
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

// IS THE SHIPPED BUNDLE THE ONE THE SOURCE WOULD PRODUCE?
//
// `frontend/dist` is TRACKED on purpose - .gitignore explains that the README's headline is an
// install of three commands that pulls nothing from npm, so the built page is committed and
// `python -m c4x.api` serves it. Nothing checked it was current. The tests all run against `src`,
// so a contributor who edits a component and forgets to rebuild gets a green suite and ships last
// week's page, which is the failure c4x/api/main.py:1318 records having hit twice in one hour.
//
// Compared by COMMIT TIME rather than by rebuilding, because a vite build inside the suite would
// cost more than the check is worth and would need node_modules that a fresh clone does not have.
// Test files are excluded from the source side: editing a test changes nothing the bundle carries,
// and a check that cries wolf is one people learn to skip.
if (!NODE_ONLY) {
  const at = (args) => {
    const r = spawnSync('git', ['log', '-1', '--format=%ct', '--', ...args],
                        { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, windowsHide: true });
    return r.status === 0 ? Number((r.stdout || '').trim()) || 0 : 0;
  };
  const src = at([':(exclude)frontend/src/**/*.test.*', ':(exclude)frontend/src/**/*.spec.*',
                  'frontend/src']);
  const dist = at(['frontend/dist']);
  if (src && dist && src > dist) {
    failed++;
    const drift = Math.round((src - dist) / 60);
    results.push({ rel: 'frontend/dist is current', state: 'FAIL',
                   note: `frontend/src was committed ${drift} min after frontend/dist, so the `
                       + 'bundle this repo SERVES is older than the source. Run '
                       + '`npm run build --prefix frontend` and commit the result.' });
  } else {
    results.push({ rel: 'frontend/dist is current', state: 'pass', count: null,
                   note: 'the committed bundle is no older than the source it is built from' });
  }
}

// THE PYTHON COVERAGE GATE. Runs before anything is spawned, because it is a statement about the
// repo rather than about a run, and a new uncovered tool should be reported even on --node-only.
{
  const covered = new Set(PY.map(([rel]) => rel).filter((r) => r.endsWith('.py')));
  const uncovered = pythonTargets().filter((p) => !covered.has(p) && !PY_EXEMPT.has(p));
  if (uncovered.length) {
    failed++;
    results.push({ rel: 'python tool coverage', state: 'FAIL',
                   note: `${uncovered.length} Python tool(s) are neither exercised by this runner `
                       + `nor exempt with a reason: ${uncovered.join(', ')}. Give each one a `
                       + '--self-test and an entry in PY, or add it to PY_EXEMPT saying why not.' });
  } else {
    results.push({ rel: 'python tool coverage', state: 'pass', count: pythonTargets().length,
                   note: 'every Python tool is either exercised or exempt with a reason' });
  }
}

// THE SUITE DEFINES ITS OWN STORE, so an ambient C4X_DB cannot decide the result.
//
// Measured: with C4X_DB exported to any path that does not exist, `make_fixture.mjs` exits 2
// through resolveDb before writing anything, so the fixture is never built, and the self-tests
// then inherit the same variable and exit 2 as well. Seven legs failed for one exported variable,
// and none of the messages said so. make_fixture takes --out, so it has no business reading the
// override at all here.
//
// Only the legs the suite pins are affected. The live-store Python leg still inherits the
// environment, because pointing THAT at a copy is a thing someone may legitimately want.
function suiteEnv(extra = {}) {
  const e = { ...process.env, ...extra };
  if (!('C4X_DB' in extra)) delete e.C4X_DB;
  return e;
}

// PYTHON BUFFERS ITS STDOUT WHEN IT IS A PIPE, AND A PIPE IS ALL THE RUNNER EVER GIVES IT.
//
// Measured, not assumed: table_audit.py against a first-run store writes 3,275 bytes and every one
// of them arrives in a SINGLE chunk 4,629 ms into a 4,820 ms run. Nothing streams. The whole verdict
// sits in an 8 KB buffer that reaches the runner only at interpreter shutdown, so a path that ends
// the process without a final flush loses the verdict entirely rather than truncating it.
//
// STATED PLAINLY, BECAUSE IT WOULD OTHERWISE READ AS THE FIX: this is NOT what caused the reported
// symptom. A reviewer saw a deliberate AUDIT SKIPPED presented as a failure, could not find the
// mechanism, and reported the behaviour rather than dressing it up. The cause was the dead regex
// above, it was deterministic all along, and it is fixed there. This is a separate fragility found
// while chasing that one, and it is worth fixing because the runner's whole contract with its
// children rides on bytes whose flushing it does not control. Every python child, not just the one
// tool whose symptom happened to be noticed.
function pyEnv(extra = {}) {
  return { ...process.env, ...extra, PYTHONUNBUFFERED: '1' };
}

// THE FIXTURE IS BUILT BEFORE THE NODE SELF-TESTS, not only for the Python leg.
//
// `mirror.mjs --self-test` and `segments.mjs --self-test` call validate()/audit(), which read a
// STORE, and the loop below used to hand them whatever `resolveDb` found. On this machine that is
// a 1.3 GB store with 150 compactions and both pass; on a fresh clone there are no compactions, so
// the mutant gate has no negatives to compare and both FAIL. Same code, same commit, opposite
// result, decided by data that is gitignored. That is the whole of the 'red on every fresh clone'
// complaint for these two legs, and it was invisible here precisely because this store is large.
//
// The Python leg already had the answer and the node leg never got it: C4X_DB pointed at a
// synthetic fixture, which is the shape CI gates on. Same override, same fixture, built once.
const fixture = join(ROOT, 'tmp', 'suite-fixture.db');
// The first-run shape: built the same way, then stripped of the five tables no install path
// creates. Python-only, so it is still built further down beside the leg that asks for it.
const bare = join(ROOT, 'tmp', 'suite-fixture-bare.db');
let fixtureBuilt = false;
let bareBuilt = false;

function buildFixture(out, extra = []) {
  rmSync(out, { force: true });
  const built = spawnSync(process.execPath,
    [join(ROOT, 'tools', 'make_fixture.mjs'), '--out', out, ...extra],
    { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 300_000, windowsHide: true, env: suiteEnv() });
  return { ok: built.status === 0 && existsSync(out),
           tail: tailOf(`${built.stdout || ''}${built.stderr || ''}`) };
}

{
  const r = buildFixture(fixture);
  fixtureBuilt = r.ok;
  if (!fixtureBuilt) {
    failed++;
    results.push({ rel: 'tools/make_fixture.mjs --out tmp/suite-fixture.db', state: 'FAIL',
                   note: 'could not build the fixture the suite runs against', tail: r.tail });
  }
}

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
    { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 300_000, windowsHide: true,
      // Every node self-test, not a hand-picked list: nodeTargets() enumerates from disk so
      // that nothing can hide, and a per-tool opt-in would put the hand-list straight back.
      // A self-test that touches no store is unaffected by the variable.
      env: suiteEnv(fixtureBuilt ? { C4X_DB: fixture } : {}) });
  const text = plain(run.stdout) + plain(run.stderr);
  const match = text.match(CHECKS);
  const count = match ? Number(match[1]) : 0;

  // Exit 0 is not enough. A file that never printed a check count did not run a self-test, which
  // is the exact shape of the two exempt files, and the reason a naive runner over-reports.
  // EXIT 3 IS 'THIS STORE CANNOT ANSWER', the same contract table_audit.py already had and the
  // same one the Python loop already honours. The node loop had no decline state at all: a tool
  // facing a store too small to check could only exit non-zero, which the runner read as a
  // broken check. SKIPPED still fails under --strict, so a check that could not run is never
  // quietly a pass.
  // EXIT 3 IS THE CONTRACT; THE MARKER ONLY CARRIES THE REASON.
  // Requiring both meant a missing marker produced a FAILURE rather than a decline with a thin
  // reason: the wrong verdict, not a poorer one. Not hypothetical. The marker test in the python
  // loop was two literal backspace bytes around the word where a word boundary was meant, so it
  // could never match and every graceful decline there was reported as a break. That was the whole
  // of the unexplained skip-shown-as-failure. Fixed above, and no longer load-bearing here. SKIPPED
  // still fails under --strict, so trusting the exit code alone never becomes a quiet pass.
  const declined = run.status === 3;
  if (declined) {
    skipped++;
    const why = (text.match(/^.*SKIPPED:\s*(.+)$/m) || [, 'it exited 3 to decline but its reason never reached the runner'])[1];
    results.push({ rel, state: 'SKIPPED', note: why.trim().slice(0, 200) });
  } else if (run.status !== 0 || run.signal) {
    failed++;
    results.push({ rel, state: 'FAIL',
                   note: whyItFailed(run),
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
  // The main fixture is built above, before the node self-tests, because those need it too.
  // Only the first-run variant is Python-only, so only it is built here.
  if (!NODE_ONLY && PY.some(([, , , , o]) => o?.bareFixture)) {
    rmSync(bare, { force: true });
    const built = spawnSync(process.execPath, [join(ROOT, 'tools', 'make_fixture.mjs'),
                                               '--out', bare, '--no-optional'],
                            { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 300_000, windowsHide: true });
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
      encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 900_000, windowsHide: true,
      // C4X_DB is the store override the app already honours, so the fixture run needs no special
      // support anywhere else in the codebase.
      env: opts?.fixture ? pyEnv({ C4X_DB: fixture })
        : opts?.bareFixture ? pyEnv({ C4X_DB: bare })
        : pyEnv(),
    });
    const text = plain(run.stdout) + plain(run.stderr);
    const match = text.match(CHECKS);
    const count = match ? Number(match[1] ?? match[2]) : null;
    // EXIT 3 IS "THIS STORE CANNOT ANSWER", which is not the same as a broken check and not the
    // same as a pass. Until this existed the runner had no graceful decline at all: a non-zero exit
    // was FAIL and an exit 0 without the marker was also FAIL, so a tool facing a store too small to
    // audit could only report a traceback. That is how `table_audit.py` presented an empty
    // `compactions` table as a crash. The child names the reason and it is printed here verbatim,
    // and SKIPPED still fails under --strict, so the rule at the top of this file holds: a check
    // that could not run is never quietly a pass.
    // EXIT 3 IS THE CONTRACT; THE MARKER ONLY CARRIES THE REASON.
    // Requiring both meant a missing marker produced a FAILURE rather than a decline with a thin
    // reason: the wrong verdict, not a poorer one. Not hypothetical. The marker test in the python
    // loop was two literal backspace bytes around the word where a word boundary was meant, so it
    // could never match and every graceful decline there was reported as a break. That was the whole
    // of the unexplained skip-shown-as-failure. Fixed above, and no longer load-bearing here. SKIPPED
    // still fails under --strict, so trusting the exit code alone never becomes a quiet pass.
    const declined = run.status === 3;
    if (declined) {
      skipped++;
      const why = (text.match(/^.*SKIPPED:\s*(.+)$/m) || [, 'it exited 3 to decline but its reason never reached the runner'])[1];
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'SKIPPED',
                     note: why.trim().slice(0, 200) });
    } else if (run.status !== 0 || run.signal) {
      failed++;
      results.push({ rel: `${rel} ${args.join(' ')}`.trim(), state: 'FAIL',
                     note: whyItFailed(run),
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
    // Same reasoning as the lint leg, and `frontend` is the argument that actually breaks: it is
    // an absolute checkout path, so a space anywhere above the repo would re-split it.
    const argv = [...args, '--prefix', frontend];
    const run = process.platform === 'win32'
      ? spawnSync(['npm', ...argv].map(winArg).join(' '),
                  { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 900_000, shell: true, windowsHide: true })
      : spawnSync('npm', argv, { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 900_000, windowsHide: true });
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
if (!NODE_ONLY && !existsSync(join(ROOT, 'node_modules', 'eslint'))) {
  // SKIPPED WITH THE FIX, not failed with a shell error. The frontend legs have said
  // "run npm install --prefix frontend" since they were written; this one spawned npm regardless
  // and reported whatever the shell said, which on a fresh clone is "'eslint' is not recognized" -
  // a message about the developer's PATH, for a situation that is neither their fault nor a lint
  // failure. Same situation, so the same quality of message. A skip still fails under --strict.
  skipped++;
  results.push({ rel: 'eslint .', state: 'SKIPPED',
                 note: 'root node_modules is absent; run `npm ci` (the node tools lint, the same '
                     + 'check CI runs at .github/workflows/tests.yml)' });
} else if (!NODE_ONLY) {
  // The pinned eslint from the root lockfile, via the `lint` script, so the suite and CI run
  // the same version. `npx --yes` fetched whatever was newest at that moment.
  // A single pre-quoted command line rather than shell:true with an args array. npm on Windows is
  // a .cmd shim, so the shell is genuinely required here; what is not required is letting node
  // concatenate the arguments unescaped, which is what DEP0190 warns about.
  const WIN = process.platform === 'win32';
  const run = WIN
    ? spawnSync(['npm', 'run', 'lint'].map(winArg).join(' '),
                { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 300_000, shell: true, windowsHide: true })
    : spawnSync('npm', ['run', 'lint'],
                { encoding: 'utf8', maxBuffer: MAX_OUTPUT, cwd: ROOT, timeout: 300_000, windowsHide: true });
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

// THE STORES THIS RUN BUILT ARE THIS RUN'S TO REMOVE.
//
// A local run left about 9 MB of sqlite behind every time, and the only reason it never grew
// unbounded is that the next run overwrote the same names. CI deletes its store and local did not,
// so the two disagreed about what a finished run looks like, and a stale fixture from an older
// schema sat there being reused by anything that reached for it by name.
//
// tmp/test-schema.db is NOT in this list. tests/test_projects.py documents it as a build cache,
// created once and reused with an mtime freshness check, so deleting it would make every run pay
// to rebuild something the tests deliberately keep.
//
// AND IT SAYS WHEN IT COULD NOT. The first version swallowed the error, on the reasoning that a
// store still held open is not a suite failure. That reasoning is right and the silence was not:
// on Windows an open sqlite handle makes rmSync throw EBUSY or EPERM, so the cleanup could do
// nothing at all and report exactly what a successful cleanup reports. A run that leaves files
// behind while printing nothing is indistinguishable from one that removed them, which is the
// silent-skip shape this repo refuses everywhere else.
const stuck = [];
for (const f of [fixture, bare, join(ROOT, 'tmp', 'test-store.db')]) {
  for (const suffix of ['', '-wal', '-shm']) {
    const path = `${f}${suffix}`;
    try { rmSync(path, { force: true }); } catch (e) { stuck.push(`${path} (${e.code || e.message})`); }
  }
}

if (stuck.length) {
  console.log('');
  console.log(`  note: ${stuck.length} store file(s) could not be removed, still held open. The `
            + 'next run will reuse them by name rather than rebuild them:');
  for (const p of stuck) console.log(`    ${p}`);
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

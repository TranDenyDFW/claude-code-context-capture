#!/usr/bin/env node
// statusline.mjs - capture plus visible display.
//
// Claude Code runs this on every status line update (300ms debounce, plus the optional
// refreshInterval timer) and passes one JSON object on stdin. This script does two things:
//   1. appends that ENTIRE object, unmodified, to data/raw/statusline.ndjson
//   2. prints a status line
//
// It is deliberately dependency-light: the only import is the pure math module, because this
// process is spawned on every update and its latency is paid by the user's UI.
//
// The stdin payload carries more than the published schema documents. Verified fields include
// context_window{}, cost{}, exceeds_200k_tokens, fast_mode, model{}, workspace{}, output_style{},
// thinking{}, effort{}, rate_limits{}, plus session_id / transcript_path / permission_mode /
// agent_id / agent_type from the shared hook base object. Everything is captured, not just the
// fields used for display, because the schema is not stable and the cheap moment to record a
// field is before you know you need it.
//
//   node statusline.mjs            read stdin, capture, print
//   node statusline.mjs --self-test

import { appendFileSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { K, assess } from './mirror-core.mjs';

const ROOT = join(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
// Production capture path. Tests and benchmarks MUST redirect via C4X_STATUSLINE_OUT so they
// never land in the real capture file. Samples written through the override are stamped
// probe:true, so a synthetic sample identifies itself forever instead of needing a heuristic.
// This exists because 164 samples accumulated in the production file and every one of them was a
// test, which was twice mistaken for evidence that the live capture was working.
const DEFAULT_OUT = join(ROOT, 'data', 'raw', 'statusline.ndjson');
const OUT_OVERRIDE = process.env.C4X_STATUSLINE_OUT || null;
const OUT = OUT_OVERRIDE || DEFAULT_OUT;
const IS_PROBE = Boolean(OUT_OVERRIDE);

const C = {
  dim: '\x1b[2m', reset: '\x1b[0m', red: '\x1b[31m', yellow: '\x1b[33m',
  green: '\x1b[32m', cyan: '\x1b[36m', magenta: '\x1b[35m',
};

function fmtTokens(n) {
  if (n == null) return '?';
  if (n >= 1e6) return (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(n);
}

export function capture(raw, outPath = OUT, isProbe = (outPath === DEFAULT_OUT ? IS_PROBE : true)) {
  mkdirSync(dirname(outPath), { recursive: true });
  // Wrap rather than mutate: the captured payload stays byte-faithful to what Claude Code sent,
  // and our own receive timestamp sits beside it instead of inside it.
  appendFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(), probe: isProbe, payload: raw,
  }) + '\n');
}

// Answers "has the live capture ever actually run?" in one command, so it is never again a
// judgement call made by squinting at payload shapes.
export function report(outPath = DEFAULT_OUT) {
  let lines = [];
  try {
    lines = readFileSync(outPath, 'utf8').split('\n').filter((l) => l.trim());
  } catch {
    return { file: outPath, exists: false, total: 0, genuine: 0, probes: 0, unmarked: 0, sessions: [] };
  }
  let genuine = 0, probes = 0, unmarked = 0, bad = 0;
  const sessions = new Map();
  let firstGenuine = null, lastGenuine = null;
  for (const l of lines) {
    let d;
    try { d = JSON.parse(l); } catch { bad++; continue; }
    // A line that parses to a string or number is corrupt, not an unmarked record.
    if (d === null || typeof d !== 'object' || Array.isArray(d)) { bad++; continue; }
    // Records written before the probe flag existed cannot be classified. They are counted
    // separately rather than assumed genuine, which is the mistake that made this necessary.
    if (d.probe === undefined) { unmarked++; continue; }
    if (d.probe) { probes++; continue; }
    genuine++;
    const sid = d.payload?.session_id ?? 'unknown';
    sessions.set(sid, (sessions.get(sid) || 0) + 1);
    if (!firstGenuine) firstGenuine = d.captured_at;
    lastGenuine = d.captured_at;
  }
  return {
    file: outPath, exists: true, total: lines.length, genuine, probes, unmarked, unparseable: bad,
    distinct_genuine_sessions: sessions.size,
    sessions: [...sessions.entries()].map(([id, n]) => ({ session: id, samples: n })),
    first_genuine: firstGenuine, last_genuine: lastGenuine,
  };
}

export function render(d) {
  const cw = d?.context_window ?? {};
  const used = cw.total_input_tokens ?? null;
  const size = cw.context_window_size ?? null;
  const pctUsed = cw.used_percentage;
  const parts = [];

  const model = d?.model?.display_name ?? d?.model?.id ?? 'model?';
  parts.push(`${C.cyan}${model}${C.reset}`);

  if (used != null && size) {
    // assess() handles the usable-window reduction. An earlier version colored by
    // level(used, size) (raw window) while printing a distance from the reduced one, so the
    // color and the number disagreed near the threshold.
    const a = assess(used, size);
    const lv = a.level;
    const color = lv === 'blocked' ? C.red : lv === 'compact' ? C.red : lv === 'warn' ? C.yellow : C.green;
    const untilCompact = a.tokensUntilCompact;
    parts.push(`${color}${fmtTokens(used)}/${fmtTokens(size)} ${pctUsed != null ? pctUsed + '%' : ''}${C.reset}`);
    parts.push(`${C.dim}compact in ${fmtTokens(untilCompact)}${C.reset}`);
  } else {
    parts.push(`${C.dim}no usage yet${C.reset}`);
  }

  const cost = d?.cost?.total_cost_usd;
  if (typeof cost === 'number' && cost > 0) parts.push(`${C.dim}$${cost.toFixed(2)}${C.reset}`);
  if (d?.fast_mode) parts.push(`${C.magenta}fast${C.reset}`);
  if (d?.effort?.level && d.effort.level !== 'high') parts.push(`${C.dim}${d.effort.level}${C.reset}`);

  const b = d?.workspace?.git_worktree || d?.gitBranch;
  if (b) parts.push(`${C.dim}${b}${C.reset}`);

  return parts.join(' ' + C.dim + '|' + C.reset + ' ');
}

function readStdin() {
  try { return readFileSync(0, 'utf8'); } catch { return ''; }
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  const sample = {
    session_id: 's1', transcript_path: 'C:/x/s1.jsonl', cwd: 'C:/x',
    model: { id: 'claude-opus-5', display_name: 'Opus 5' },
    context_window: { total_input_tokens: 159400, total_output_tokens: 1200, context_window_size: 1000000, used_percentage: 16, remaining_percentage: 84, current_usage: { input_tokens: 2, cache_creation_input_tokens: 5000, cache_read_input_tokens: 154398, output_tokens: 1200 } },
    cost: { total_cost_usd: 4.2117, total_duration_ms: 1000, total_lines_added: 5, total_lines_removed: 1 },
    exceeds_200k_tokens: false, fast_mode: false, effort: { level: 'high' }, thinking: { enabled: true },
  };

  const line = render(sample);
  add('renders the model name', line.includes('Opus 5'));
  add('renders used and window', line.includes('159.4k') && line.includes('1M'));
  add('renders the percentage from the payload', line.includes('16%'));
  add('renders cost', line.includes('$4.21'));
  // 1M window: trigger at 967000, so 967000 - 159400 = 807600 remain.
  add('renders distance to compaction', line.includes('807.6k'), line);
  // Color and number must agree: at 970000 the session is PAST the 967000 trigger, so the
  // line must not still read as green/ok.
  const past = render({ ...sample, context_window: { ...sample.context_window, total_input_tokens: 970000 } });
  add('past the trigger renders as compact, not ok', past.includes('[31m'), past);

  const noUsage = render({ model: { display_name: 'Opus 5' }, context_window: { total_input_tokens: null, context_window_size: null } });
  add('survives a payload with no usage yet', noUsage.includes('no usage yet'));

  // Capture must be byte-faithful and must not lose unknown fields.
  // Use a fresh path per run and delete it with the IMPORTED rmSync. An earlier version called
  // require('node:fs') here, which is undefined in an ES module; the ReferenceError was swallowed
  // by an empty catch, the fixture never got cleaned, and the test passed on a clean directory
  // while failing on every rerun. A check that only passes the first time is not a check.
  const tmp = join(ROOT, 'tmp', `statusline-selftest-${process.pid}-${Date.now()}.ndjson`);
  rmSync(tmp, { force: true });
  const weird = { ...sample, some_future_field: { nested: [1, 2, 3] } };
  capture(weird, tmp);
  const LF = String.fromCharCode(10); // written this way because a literal escape here got
  const lines = readFileSync(tmp, 'utf8').trim().split(LF); // mangled into a real newline once
  add('capture writes exactly one line per call', lines.length === 1, `got ${lines.length}`);
  const back = JSON.parse(lines[0]);
  add('capture preserves unknown future fields', JSON.stringify(back.payload.some_future_field) === JSON.stringify(weird.some_future_field));
  add('capture stamps its own timestamp outside the payload', typeof back.captured_at === 'string' && back.payload.captured_at === undefined);

  // Negative control: a renderer that ignored its input would still pass the checks above if
  // they were vacuous, so assert a DIFFERENT payload produces a DIFFERENT line.
  const other = render({ ...sample, context_window: { ...sample.context_window, total_input_tokens: 900000, used_percentage: 90 } });
  add('different usage renders differently (gate can fail)', other !== line);

  // Probe separation. A sample written anywhere other than the production path is a probe, and
  // report() must count probe, genuine and unmarked records apart rather than lumping them.
  add('a redirected capture is flagged as a probe', back.probe === true, JSON.stringify(back.probe));

  const mixed = join(ROOT, 'tmp', `statusline-report-${process.pid}-${Date.now()}.ndjson`);
  rmSync(mixed, { force: true });
  const w = (o) => appendFileSync(mixed, JSON.stringify(o) + LF);
  w({ captured_at: '2026-08-21T00:00:00Z', probe: true, payload: { session_id: 'p1' } });
  w({ captured_at: '2026-08-21T00:00:01Z', probe: false, payload: { session_id: 'real-1' } });
  w({ captured_at: '2026-08-21T00:00:02Z', probe: false, payload: { session_id: 'real-1' } });
  w({ captured_at: '2026-08-21T00:00:03Z', probe: false, payload: { session_id: 'real-2' } });
  w({ captured_at: '2026-08-21T00:00:04Z', payload: { session_id: 'legacy' } });   // pre-flag
  appendFileSync(mixed, '{ this line is not json' + LF);   // raw, not JSON-encoded
  const rep = report(mixed);
  add('report counts genuine samples', rep.genuine === 3, String(rep.genuine));
  add('report counts probes separately', rep.probes === 1, String(rep.probes));
  add('report does not assume unmarked records are genuine', rep.unmarked === 1, String(rep.unmarked));
  add('report survives an unparseable line', rep.unparseable === 1, String(rep.unparseable));
  add('report counts distinct genuine sessions', rep.distinct_genuine_sessions === 2,
    String(rep.distinct_genuine_sessions));
  add('report on a missing file reports zero, not a crash', report(mixed + '.nope').genuine === 0);
  // Negative control: a report that always claimed success would pass the checks above only if
  // they were vacuous, so assert an all-probe file yields zero genuine.
  const allProbe = join(ROOT, 'tmp', `statusline-allprobe-${process.pid}-${Date.now()}.ndjson`);
  rmSync(allProbe, { force: true });
  appendFileSync(allProbe, JSON.stringify({ captured_at: 'x', probe: true, payload: {} }) + LF);
  add('an all-probe file reports zero genuine (gate can fail)', report(allProbe).genuine === 0);
  rmSync(mixed, { force: true });
  rmSync(allProbe, { force: true });

  rmSync(tmp, { force: true });

  let bad = 0;
  for (const [n, ok, d] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`); }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch when this file IS the entry point. Importing it for its exports used to run the
// handler and exit the importing process. probe.mjs records two earlier victims of this.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (process.argv.includes('--self-test')) {
  process.exit(selfTest());
} else if (process.argv.includes('--report')) {
  const r = report();
  console.log(JSON.stringify(r, null, 2));
  if (r.genuine === 0) {
    process.stderr.write(
      '\nNO GENUINE SAMPLES. The status line has never been invoked by Claude Code itself.\n'
      + 'Settings are read at session start, so a session that predates the statusLine key will\n'
      + 'never run it. Open a NEW session, then re-run this report.\n');
  }
  process.exit(0);
} else {
  const text = readStdin();
  let d = null;
  try { d = JSON.parse(text); } catch { /* fall through, still print something */ }
  if (d) {
    // Capture must never take the status line down. A failed write is reported to stderr,
    // which Claude Code does not render, and the line still prints.
    try { capture(d); } catch (e) { process.stderr.write('statusline capture failed: ' + e.message + '\n'); }
    process.stdout.write(render(d));
  } else {
    process.stdout.write(`${C.dim}(no status payload)${C.reset}`);
  }
  process.exit(0);
}

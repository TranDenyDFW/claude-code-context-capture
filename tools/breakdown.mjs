#!/usr/bin/env node
// breakdown.mjs - the per-category context split, with history.
//
// Claude Code's context tooltip splits the window into System prompt / System tools / MCP tools /
// Skills / Messages / Free space. That split is computed for the UI and persisted NOWHERE. It is
// absent from every one of the 6,750 records in a live transcript (the only token-bearing
// structure there is message.usage, which carries totals), and absent from the status line
// payload. tools/probe.mjs can ask a session for it over the control protocol, but only a session
// it spawns itself - and a spawned CLI session has a different configuration from the desktop app:
// measured, MCP tools is 11k in the app and 0 in the CLI, because `claude mcp list` reports no
// servers configured at all. The desktop app's own messaging socket is an inbox for INJECTING
// messages (accepted types: user, control, rename); `get_context_usage` appears at 14 offsets in
// the binary and none of them fall inside that socket's code region, so it cannot be queried there.
//
// So the split cannot be read. It CAN be derived, because the arithmetic is closed:
//
//     resident = static + messages        static  = the configuration's fixed overhead
//     free     = window - resident        messages = everything the conversation added
//
// Validated against a real tooltip reading: a screenshot total of 647,400 matched a stored turn at
// 647,433, implying a static of 41,233 against the tooltip's 41,300 - agreement within 67 tokens -
// and a free space of 352,567 against 352,400. Both differences are the tooltip's own rounding.
//
// `resident` is exact and already stored for every turn ever harvested. So once the static
// baseline for a configuration is known, the whole breakdown follows for ALL of history,
// retroactively, with no further capture.
//
//   node breakdown.mjs --calibrate --system-prompt 5100 --system-tools 19100 --mcp-tools 11000 --skills 6100
//   node breakdown.mjs --show [--session ID]
//   node breakdown.mjs --self-test

import { DatabaseSync } from 'node:sqlite';
import { pathToFileURL } from 'node:url';
import { rootFrom, resolveDb } from './paths.mjs';

const ROOT = rootFrom(import.meta.url);
const DB_PATH = resolveDb(ROOT, process.argv.slice(2));

// A baseline is one observation of a configuration's fixed overhead. Kept as rows rather than a
// single value because that overhead CHANGES when configuration changes - adding an MCP server or
// a skill moves it - and a history of baselines is what lets an old turn be split using the
// overhead that was actually in force at the time, instead of today's.
const SCHEMA = `
CREATE TABLE IF NOT EXISTS context_baselines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  entrypoint TEXT,
  system_prompt INTEGER, system_tools INTEGER, mcp_tools INTEGER,
  skills INTEGER, memory_files INTEGER, custom_agents INTEGER,
  static_total INTEGER NOT NULL,
  window_size INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS context_baselines_ts ON context_baselines(ts);
`;

export const CATEGORIES = ['system_prompt', 'system_tools', 'mcp_tools', 'skills', 'memory_files', 'custom_agents'];

/**
 * Split one resident reading into its parts.
 *
 * Returns nulls for the conversation side when no baseline is available, rather than guessing. A
 * breakdown built on an invented static figure would render exactly as authoritatively as a real
 * one, which is the single failure this tool exists to avoid.
 */
export function split(resident, baseline, windowSize) {
  const out = {
    resident, window: windowSize ?? null, free: null,
    messages: null, static_total: null, categories: null, derived: true,
  };
  if (windowSize) out.free = Math.max(0, windowSize - resident);
  if (!baseline) return out;
  out.static_total = baseline.static_total;
  out.messages = resident - baseline.static_total;
  out.categories = {};
  for (const c of CATEGORIES) if (baseline[c] != null) out.categories[c] = baseline[c];
  return out;
}

/**
 * The baseline in force at a given moment: the newest one recorded at or before it.
 *
 * A turn that predates every recorded baseline still gets one, but flagged `exact: false`, because
 * it is being split with an overhead that may not have applied to it. Silently using today's
 * numbers for a turn from last week would be the same class of error as inventing them.
 */
export function baselineFor(rows, ts) {
  if (!rows.length) return null;
  const atOrBefore = rows.filter((r) => !ts || r.ts <= ts);
  if (atOrBefore.length) return { ...atOrBefore[atOrBefore.length - 1], exact: true };
  return { ...rows[0], exact: false };
}

function open(dbPath = DB_PATH) {
  const db = new DatabaseSync(dbPath);
  db.exec(SCHEMA);
  return db;
}

export function calibrate(values, db) {
  const cats = {};
  let total = 0;
  for (const c of CATEGORIES) {
    const v = values[c];
    if (v == null) continue;
    if (!Number.isFinite(v) || v < 0) throw new Error(`${c} must be a non-negative number, got ${v}`);
    cats[c] = v;
    total += v;
  }
  if (!total) throw new Error('a calibration needs at least one category value');
  db.prepare(`INSERT INTO context_baselines
    (ts,source,entrypoint,system_prompt,system_tools,mcp_tools,skills,memory_files,custom_agents,static_total,window_size,note)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`).run(
    values.ts ?? new Date().toISOString(), values.source ?? 'calibration', values.entrypoint ?? null,
    cats.system_prompt ?? null, cats.system_tools ?? null, cats.mcp_tools ?? null,
    cats.skills ?? null, cats.memory_files ?? null, cats.custom_agents ?? null,
    total, values.window_size ?? null, values.note ?? null);
  return { static_total: total, categories: cats };
}

export function baselines(db) {
  return db.prepare('SELECT * FROM context_baselines ORDER BY ts').all();
}

function num(argv, flag) {
  const i = argv.indexOf(flag);
  return i >= 0 ? Number(argv[i + 1]) : undefined;
}

function cmdCalibrate(argv) {
  const db = open();
  try {
    const r = calibrate({
      system_prompt: num(argv, '--system-prompt'),
      system_tools: num(argv, '--system-tools'),
      mcp_tools: num(argv, '--mcp-tools'),
      skills: num(argv, '--skills'),
      memory_files: num(argv, '--memory-files'),
      custom_agents: num(argv, '--custom-agents'),
      window_size: num(argv, '--window') ?? 1000000,
      entrypoint: process.env.CLAUDE_CODE_ENTRYPOINT ?? null,
      note: 'read from the context tooltip',
    }, db);
    console.log(JSON.stringify({ recorded: true, static_total: r.static_total, categories: r.categories }, null, 2));
    return 0;
  } catch (e) {
    console.error('calibrate: ' + e.message);
    return 2;
  } finally {
    db.close();
  }
}

function cmdShow(argv) {
  const db = open();
  const rows = baselines(db);
  const i = argv.indexOf('--session');
  const sid = i >= 0 ? argv[i + 1] : null;
  const sql = `SELECT session_id, ts, total_resident FROM api_calls
    WHERE total_resident IS NOT NULL ${sid ? 'AND session_id LIKE ?' : ''}
    ORDER BY ts DESC LIMIT 1`;
  // A store that has never been harvested has no api_calls view at all, which is a first-run
  // state, not an error worth a stack trace. open() creates only this tool's own table.
  let latest = null;
  try {
    latest = sid ? db.prepare(sql).get(sid + '%') : db.prepare(sql).get();
  } catch (e) {
    if (!/no such table|no such view/i.test(e.message)) throw e;
  }
  if (!latest) {
    console.error(`no turns in the store at ${DB_PATH}. Run: node tools/harvest.mjs`);
    db.close();
    return 1;
  }
  const b = baselineFor(rows, latest.ts);
  const out = {
    session: latest.session_id,
    ts: latest.ts,
    baselines_recorded: rows.length,
    baseline_used: b
      ? { ts: b.ts, source: b.source, static_total: b.static_total, applies_to_this_turn: b.exact }
      : null,
    breakdown: split(latest.total_resident, b, 1000000),
  };
  if (!rows.length) {
    out.note = 'no baseline recorded, so messages and the category split are unknown. '
      + 'Free space is still exact. Record one with --calibrate.';
  }
  console.log(JSON.stringify(out, null, 2));
  db.close();
  return 0;
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  // The arithmetic, against the real measurement this tool was derived from.
  const b = { static_total: 41233, system_prompt: 5100, system_tools: 19100, mcp_tools: 11000, skills: 6100 };
  const s = split(647433, b, 1000000);
  add('messages = resident - static', s.messages === 606200, String(s.messages));
  add('free = window - resident', s.free === 352567, String(s.free));
  add('the parts reconstruct the resident total', s.messages + s.static_total === 647433);
  add('category values are carried through', s.categories.mcp_tools === 11000);

  // No baseline must yield nulls, NOT a guess.
  const none = split(647433, null, 1000000);
  add('with no baseline, messages is null rather than invented (gate can fail)', none.messages === null);
  add('with no baseline, free space is still exact', none.free === 352567);
  add('with no window, free is null rather than a misleading zero', split(100, b, null).free === null);
  add('free never goes negative past the window', split(1200000, b, 1000000).free === 0);

  // Which baseline applies to which turn.
  const rows = [
    { ts: '2026-08-01T00:00:00Z', static_total: 30000 },
    { ts: '2026-08-20T00:00:00Z', static_total: 41233 },
  ];
  add('picks the baseline in force at that moment', baselineFor(rows, '2026-08-25T00:00:00Z').static_total === 41233);
  add('an earlier turn uses the earlier baseline (gate can fail)',
    baselineFor(rows, '2026-08-10T00:00:00Z').static_total === 30000);
  add('a turn predating every baseline is flagged inexact (gate can fail)',
    baselineFor(rows, '2026-07-01T00:00:00Z').exact === false);
  add('a turn after a baseline is flagged exact', baselineFor(rows, '2026-08-21T00:00:00Z').exact === true);
  add('no baselines at all yields null', baselineFor([], '2026-08-25T00:00:00Z') === null);

  // Calibration, in memory. Never touches the real store.
  const db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
  const c = calibrate({ system_prompt: 5100, system_tools: 19100, mcp_tools: 11000, skills: 6100 }, db);
  add('calibration sums the categories', c.static_total === 41300, String(c.static_total));
  add('and stores exactly one row', baselines(db).length === 1);
  let threw = false;
  try { calibrate({}, db); } catch { threw = true; }
  add('an empty calibration is refused, not stored as zero (gate can fail)', threw);
  let threwNeg = false;
  try { calibrate({ skills: -5 }, db); } catch { threwNeg = true; }
  add('a negative category is refused', threwNeg);
  add('neither refusal stored anything', baselines(db).length === 1);
  // Two calibrations must both survive: the history of overheads is the point.
  calibrate({ skills: 1, ts: '2026-09-01T00:00:00Z' }, db);
  add('a second calibration is added, not overwritten', baselines(db).length === 2);
  db.close();

  // First run: a store that has never been harvested has no api_calls view. That must be a
  // one-line message, not a stack trace - this repo has shipped a first-command crash before.
  {
    const bare = new DatabaseSync(':memory:');
    bare.exec(SCHEMA);
    let threw = false, got = null;
    try {
      got = bare.prepare('SELECT total_resident FROM api_calls LIMIT 1').get();
    } catch (e) {
      threw = /no such table|no such view/i.test(e.message);
    }
    add('a never-harvested store genuinely lacks api_calls (gate can fail)', threw && got === null);
    bare.close();
  }

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

const IS_ENTRY = (() => {
  try { return (process.argv[1] ? pathToFileURL(process.argv[1]).href : null) === import.meta.url; }
  catch { return false; }
})();

if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else {
  const argv = process.argv.slice(2);
  let code = 0;
  if (argv.includes('--self-test')) code = selfTest();
  else if (argv.includes('--calibrate')) code = cmdCalibrate(argv);
  else code = cmdShow(argv);
  process.exit(code);
}

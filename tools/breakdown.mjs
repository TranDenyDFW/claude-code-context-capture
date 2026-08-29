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

// Three writers share this store by design: a manual harvest, the SessionEnd and UserPromptSubmit
// hooks, and the dashboard's refresh loop. SQLite's default busy timeout is ZERO, so a reader that
// arrives mid-write fails outright with SQLITE_BUSY instead of waiting. That killed a --full
// harvest at 7.5 GB and, separately, made segments.mjs throw "database is locked" while the app
// was importing. A reader losing a race should be slow, not absent.
const BUSY_TIMEOUT = 'PRAGMA busy_timeout = 15000';

const ROOT = rootFrom(import.meta.url);
const DB_PATH = resolveDb(ROOT, process.argv.slice(2));

// A baseline is one observation of a configuration's fixed overhead. Kept as rows rather than a
// single value because that overhead CHANGES when configuration changes - adding an MCP server or
// a skill moves it - and a history of baselines is what lets an old turn be split using the
// overhead that was actually in force at the time, instead of today's.
// ONE spec. The schema columns, the CLI flags, the INSERT and the dashboard's labels are all
// derived from it. They used to be four parallel literals in this file, and the INSERT was the one
// that failed silently: a category added to the list but missed in the INSERT stored as NULL and
// read back as "that configuration had none", which is indistinguishable from a real zero.
//
// `resident` categories are the ones that occupy the window and sum to static_total.
// `deferred`  are shown by the tooltip but are NOT resident: a deferred tool is not loaded, so it
//             costs nothing until it is. It must never be added to static_total.
// `count`     are item counts, not tokens. The tooltip shows both for the same row, e.g.
//             MCP tools 113.4k across 214 items.
export const FIELDS = [
  { col: 'system_prompt', flag: '--system-prompt', label: 'System prompt', kind: 'resident' },
  { col: 'system_tools', flag: '--system-tools', label: 'System tools', kind: 'resident' },
  { col: 'mcp_tools', flag: '--mcp-tools', label: 'MCP tools', kind: 'resident' },
  { col: 'skills', flag: '--skills', label: 'Skills', kind: 'resident' },
  { col: 'memory_files', flag: '--memory-files', label: 'Memory files', kind: 'resident' },
  { col: 'custom_agents', flag: '--custom-agents', label: 'Custom agents', kind: 'resident' },
  { col: 'mcp_tools_deferred', flag: '--mcp-tools-deferred', label: 'MCP tools (deferred)', kind: 'deferred', of: 'mcp_tools' },
  { col: 'system_tools_deferred', flag: '--system-tools-deferred', label: 'System tools (deferred)', kind: 'deferred', of: 'system_tools' },
  { col: 'mcp_tools_items', flag: '--mcp-tools-items', label: 'MCP tools', kind: 'count', of: 'mcp_tools' },
  { col: 'memory_files_items', flag: '--memory-files-items', label: 'Memory files', kind: 'count', of: 'memory_files' },
  { col: 'custom_agents_items', flag: '--custom-agents-items', label: 'Custom agents', kind: 'count', of: 'custom_agents' },
];

const of = (kind) => FIELDS.filter((f) => f.kind === kind).map((f) => f.col);
export const CATEGORIES = of('resident');
export const DEFERRED = of('deferred');
export const ITEM_COUNTS = of('count');
const ALL_COLS = FIELDS.map((f) => f.col);

const SCHEMA = `
CREATE TABLE IF NOT EXISTS context_baselines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  entrypoint TEXT,
${ALL_COLS.map((c) => `  ${c} INTEGER`).join(',\n')},
  static_total INTEGER NOT NULL,
  window_size INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS context_baselines_ts ON context_baselines(ts);
`;

/**
 * Add columns this build knows about to a table written by an older one.
 *
 * SQLite has no ADD COLUMN IF NOT EXISTS, and a store that predates the deferred and item-count
 * fields is the normal case rather than a broken one. Returns what it added so the caller can say
 * so out loud instead of migrating in silence.
 */
export function ensureColumns(db) {
  const have = new Set(db.prepare('PRAGMA table_info(context_baselines)').all().map((r) => r.name));
  const added = ALL_COLS.filter((c) => !have.has(c));
  for (const c of added) db.exec(`ALTER TABLE context_baselines ADD COLUMN ${c} INTEGER`);
  return added;
}

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
    messages: null, static_total: null, categories: null,
    deferred: null, items: null, over_baseline: false, derived: true,
  };
  if (windowSize) out.free = Math.max(0, windowSize - resident);
  if (!baseline) return out;
  out.static_total = baseline.static_total;
  // A turn whose measured resident is BELOW the baseline's fixed overhead cannot have negative
  // conversation in it. That happens whenever a baseline is applied to a turn it does not describe,
  // which is the honest reading: the number is not small, it is inapplicable. Report it as zero
  // and raise the flag, rather than returning a negative token count that renders as a real one.
  const raw = resident - baseline.static_total;
  out.messages = Math.max(0, raw);
  out.over_baseline = raw < 0;
  out.categories = {};
  for (const c of CATEGORIES) if (baseline[c] != null) out.categories[c] = baseline[c];
  // Deferred and item counts travel beside the split, never inside static_total.
  const pick = (cols) => {
    const o = {};
    for (const c of cols) if (baseline[c] != null) o[c] = baseline[c];
    return Object.keys(o).length ? o : null;
  };
  out.deferred = pick(DEFERRED);
  out.items = pick(ITEM_COUNTS);
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
  db.exec(BUSY_TIMEOUT);
  db.exec(SCHEMA);
  // A store written before the deferred and item-count fields existed is ordinary, not broken.
  const added = ensureColumns(db);
  if (added.length) console.error(`context_baselines: added ${added.length} column(s): ${added.join(', ')}`);
  return db;
}

export function calibrate(values, db) {
  const stored = {};
  let total = 0;
  for (const f of FIELDS) {
    const v = values[f.col];
    if (v == null) continue;
    if (!Number.isFinite(v) || v < 0) throw new Error(`${f.col} must be a non-negative number, got ${v}`);
    stored[f.col] = v;
    // Only resident categories occupy the window. Adding a deferred figure here would inflate the
    // fixed overhead by tools that are not loaded, and every turn in history would lose that many
    // tokens from its Messages count.
    if (f.kind === 'resident') total += v;
  }
  if (!total) throw new Error('a calibration needs at least one resident category value');
  ensureColumns(db);
  const cols = ALL_COLS.filter((c) => c in stored);
  db.prepare(`INSERT INTO context_baselines
    (ts,source,entrypoint,${cols.join(',')},static_total,window_size,note)
    VALUES (${new Array(3 + cols.length + 3).fill('?').join(',')})`).run(
    values.ts ?? new Date().toISOString(), values.source ?? 'calibration', values.entrypoint ?? null,
    ...cols.map((c) => stored[c]),
    total, values.window_size ?? null, values.note ?? null);
  const categories = Object.fromEntries(Object.entries(stored).filter(([c]) => CATEGORIES.includes(c)));
  return { static_total: total, categories, stored };
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
    // Flags come from FIELDS, so a field cannot be added to the schema without a way to record it.
    const values = Object.fromEntries(FIELDS.map((f) => [f.col, num(argv, f.flag)]));
    const r = calibrate({
      ...values,
      window_size: num(argv, '--window') ?? 1000000,
      entrypoint: process.env.CLAUDE_CODE_ENTRYPOINT ?? null,
      note: 'read from the context tooltip',
    }, db);
    console.log(JSON.stringify({ recorded: true, static_total: r.static_total, stored: r.stored }, null, 2));
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

  // ---- the wider vocabulary the context tooltip actually shows --------------------------------
  // Measured from the tooltip on 2026-08-28: Messages 145.4k, System tools 23.5k, Memory files
  // 11.7k, Skills 9.9k, MCP tools 8.4k, System prompt 5.1k, Custom agents 1k, Free space 795k,
  // plus MCP tools (deferred) 104.9k and System tools (deferred) 16.2k which carry no percentage
  // because they are not resident, and item counts of 214 / 1 / 10.
  {
    const db2 = new DatabaseSync(':memory:');
    db2.exec(SCHEMA);
    const full = calibrate({
      system_prompt: 5100, system_tools: 23500, mcp_tools: 8400, skills: 9900,
      memory_files: 11700, custom_agents: 1000,
      mcp_tools_deferred: 104900, system_tools_deferred: 16200,
      mcp_tools_items: 214, memory_files_items: 1, custom_agents_items: 10,
      window_size: 1000000,
    }, db2);
    add('static_total sums ONLY the resident categories', full.static_total === 59600, String(full.static_total));
    add('a deferred figure is not added to the fixed overhead (gate can fail)',
      full.static_total !== 59600 + 104900 + 16200);
    add('an item count is not added to the fixed overhead', full.static_total < 60000);

    // The silent-NULL regression: every field handed in must come back out of the table.
    const row = baselines(db2)[0];
    const missing = Object.keys(full.stored).filter((c) => row[c] !== full.stored[c]);
    add('every calibrated field survives the INSERT, none stored as NULL', missing.length === 0, missing.join(','));
    add('the schema carries every field in the spec',
      FIELDS.every((f) => f.col in row), FIELDS.filter((f) => !(f.col in row)).map((f) => f.col).join(','));

    const s2 = split(201800, row, 1000000);
    add('the deferred rows travel beside the split, not inside it', s2.deferred.mcp_tools_deferred === 104900);
    add('item counts travel beside the split', s2.items.mcp_tools_items === 214);
    // NOT "matches the tooltip": the tooltip's own rows are rounded to 0.1k and sum to 205.0k
    // against a stated total of 201.8k, so they cannot be reconciled exactly. What must hold is
    // that the derivation is internally consistent, which is the claim this tool actually makes.
    add('messages is resident minus the fixed overhead', s2.messages === 201800 - 59600, String(s2.messages));
    add('the resident rows plus messages reconstruct the total',
      Object.values(s2.categories).reduce((a, b) => a + b, 0) + s2.messages === 201800);
    db2.close();
  }

  // A baseline larger than the turn it is applied to cannot mean negative conversation.
  const under = split(30000, { static_total: 41300 }, 1000000);
  add('a resident below the baseline reports zero messages, never negative', under.messages === 0, String(under.messages));
  add('and says so, rather than rendering as a real reading (gate can fail)', under.over_baseline === true);
  add('an ordinary turn is not flagged', split(647433, b, 1000000).over_baseline === false);

  // Migrating a store written before these columns existed.
  {
    const old = new DatabaseSync(':memory:');
    old.exec(`CREATE TABLE context_baselines (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
      source TEXT NOT NULL, entrypoint TEXT, system_prompt INTEGER, system_tools INTEGER,
      mcp_tools INTEGER, skills INTEGER, memory_files INTEGER, custom_agents INTEGER,
      static_total INTEGER NOT NULL, window_size INTEGER, note TEXT)`);
    const added = ensureColumns(old);
    add('an older store gains exactly the new columns', added.join(',') === 'mcp_tools_deferred,system_tools_deferred,mcp_tools_items,memory_files_items,custom_agents_items', added.join(','));
    add('running the migration twice adds nothing the second time', ensureColumns(old).length === 0);
    old.close();
  }
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
  // The dashboard reads its category order, labels and kinds from here rather than keeping a
  // second copy in Python. A literal list on each side of a language boundary is the same drift
  // this file's FIELDS spec exists to stop, and the cross-language one cannot be caught by either
  // language's own tests.
  else if (argv.includes('--fields')) { console.log(JSON.stringify(FIELDS, null, 2)); code = 0; }
  else if (argv.includes('--calibrate')) code = cmdCalibrate(argv);
  else code = cmdShow(argv);
  process.exit(code);
}

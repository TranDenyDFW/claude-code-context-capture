#!/usr/bin/env node
// probe.mjs - capture the per-category context breakdown via the control channel.
//
// This is the ONLY route to the `categories` array (System prompt / System tools / MCP tools /
// Custom agents / Memory files / Skills / Messages / Free space) plus the detail arrays and
// `messageBreakdown`. It works by SPAWNING a Claude Code session that speaks the stream-json
// control protocol and issuing `get_context_usage` into it.
//
// The hard limit, established from the binary: control requests are served by the process you
// spawned. There is no way to attach this to an already-running interactive session. So these
// rows describe a PROBE session, not your live work, and the store labels them that way. What
// they are good for is the static categories (system prompt, tools, skills, memory files), which
// depend on configuration rather than on conversation, and which are otherwise unobservable.
//
// Known hazard: on a desktop-app box, headless `claude -p` children have failed to authenticate
// (cli-toolkit gotcha catalogue, 2026-07-16). If that happens here it is reported as a FAILURE
// with the real stderr, not smoothed over.
//
//   node probe.mjs                 run a probe and store the result
//   node probe.mjs --dry-run       show the exact command and protocol frames, spawn nothing
//   node probe.mjs --self-test     parser checks only, spawns nothing

import { spawn } from 'node:child_process';
import { appendFileSync, mkdirSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { rootFrom, resolveDb } from './paths.mjs';

// Three writers share this store by design: a manual harvest, the SessionEnd and UserPromptSubmit
// hooks, and the dashboard's refresh loop. SQLite's default busy timeout is ZERO, so a reader that
// arrives mid-write fails outright with SQLITE_BUSY instead of waiting. That killed a --full
// harvest at 7.5 GB and, separately, made segments.mjs throw "database is locked" while the app
// was importing. A reader losing a race should be slow, not absent.
const BUSY_TIMEOUT = 'PRAGMA busy_timeout = 15000';

const ROOT = rootFrom(import.meta.url);
const DB_PATH = resolveDb(ROOT);
const RAW = join(ROOT, 'data', 'raw', 'probe.ndjson');
const CLAUDE = process.env.C4X_CLAUDE_BIN || 'claude';
const TIMEOUT_MS = Number(process.env.C4X_PROBE_TIMEOUT || 90000);

const SCHEMA = `
CREATE TABLE IF NOT EXISTS probes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ok INTEGER, error TEXT,
  model TEXT, max_tokens INTEGER, total_tokens INTEGER, percentage INTEGER,
  autocompact_source TEXT, auto_compact_threshold INTEGER, is_auto_compact_enabled INTEGER,
  raw_json TEXT
);
CREATE TABLE IF NOT EXISTS probe_categories (
  probe_id INTEGER, name TEXT, tokens INTEGER, color TEXT, is_deferred INTEGER
);
CREATE TABLE IF NOT EXISTS probe_details (
  probe_id INTEGER, kind TEXT, name TEXT, extra TEXT, tokens INTEGER
);
`;

export function ensureProbeSchema(db) {
  // probes, probe_categories and probe_details are this tool's tables. Exported so a fixture can
  // create them exactly as a real probe run does, rather than carrying a second CREATE TABLE that
  // silently diverges from this one.
  db.exec(SCHEMA);
}

export function frames() {
  return [
    { type: 'control_request', request_id: 'c4x-1', request: { subtype: 'initialize' } },
    { type: 'control_request', request_id: 'c4x-2', request: { subtype: 'get_context_usage' } },
  ];
}

export function argsFor() {
  // --verbose is mandatory: "When using --print, --output-format=stream-json requires --verbose"
  return ['--print', '--verbose', '--input-format', 'stream-json', '--output-format', 'stream-json'];
}

// Pull the context-usage payload out of whatever the session emitted.
export function extractUsage(lines) {
  for (const raw of lines) {
    let d;
    try { d = JSON.parse(raw); } catch { continue; }
    if (d?.type !== 'control_response') continue;
    const r = d.response;
    if (r?.request_id !== 'c4x-2') continue;
    if (r.subtype === 'error') return { ok: false, error: r.error ?? 'unknown control error' };
    const payload = r.response ?? null;
    if (payload && Array.isArray(payload.categories)) return { ok: true, usage: payload };
    return { ok: false, error: 'control_response carried no categories array' };
  }
  return { ok: false, error: 'no control_response for get_context_usage was received' };
}

function store(result, stderrText) {
  mkdirSync(dirname(DB_PATH), { recursive: true });
  mkdirSync(dirname(RAW), { recursive: true });
  appendFileSync(RAW, JSON.stringify({ ts: new Date().toISOString(), ...result, stderr: stderrText?.slice(0, 4000) }) + '\n');

  const db = new DatabaseSync(DB_PATH);
  db.exec(BUSY_TIMEOUT);
  db.exec(SCHEMA);
  const u = result.usage;
  db.prepare(`INSERT INTO probes (ts,ok,error,model,max_tokens,total_tokens,percentage,autocompact_source,auto_compact_threshold,is_auto_compact_enabled,raw_json)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
    new Date().toISOString(), result.ok ? 1 : 0, result.error ?? null,
    u?.model ?? null, u?.maxTokens ?? null, u?.totalTokens ?? null, u?.percentage ?? null,
    u?.autocompactSource ?? null, u?.autoCompactThreshold ?? null,
    u?.isAutoCompactEnabled == null ? null : (u.isAutoCompactEnabled ? 1 : 0),
    u ? JSON.stringify(u) : null);
  const id = db.prepare('SELECT last_insert_rowid() id').get().id;

  if (u) {
    const cat = db.prepare('INSERT INTO probe_categories (probe_id,name,tokens,color,is_deferred) VALUES (?,?,?,?,?)');
    for (const c of u.categories ?? []) cat.run(id, c.name ?? null, c.tokens ?? null, c.color ?? null, c.isDeferred ? 1 : 0);
    const det = db.prepare('INSERT INTO probe_details (probe_id,kind,name,extra,tokens) VALUES (?,?,?,?,?)');
    for (const m of u.memoryFiles ?? []) det.run(id, 'memoryFile', m.path ?? null, m.type ?? null, m.tokens ?? null);
    for (const m of u.mcpTools ?? []) det.run(id, 'mcpTool', m.name ?? null, m.serverName ?? null, m.tokens ?? null);
    for (const a of u.agents ?? []) det.run(id, 'agent', a.agentType ?? null, a.source ?? null, a.tokens ?? null);
    for (const s of u.skills?.skillFrontmatter ?? []) det.run(id, 'skill', s.name ?? null, s.source ?? null, s.tokens ?? null);
  }
  db.close();
  return id;
}

async function runProbe() {
  const args = argsFor();
  process.stderr.write(`probe: spawning ${CLAUDE} ${args.join(' ')}\n`);
  const child = spawn(CLAUDE, args, { stdio: ['pipe', 'pipe', 'pipe'], shell: process.platform === 'win32' });

  let out = '', err = '';
  child.stdout.on('data', (b) => { out += b.toString(); });
  child.stderr.on('data', (b) => { err += b.toString(); });

  for (const f of frames()) child.stdin.write(JSON.stringify(f) + '\n');

  const done = new Promise((resolve) => {
    let settled = false;
    const finish = (why) => { if (!settled) { settled = true; resolve(why); } };
    const timer = setTimeout(() => { try { child.kill(); } catch {} finish('timeout'); }, TIMEOUT_MS);
    child.on('exit', (code) => { clearTimeout(timer); finish('exit:' + code); });
    child.on('error', (e) => { clearTimeout(timer); err += '\nspawn error: ' + e.message; finish('spawn-error'); });
    const poll = setInterval(() => {
      if (out.includes('"c4x-2"')) { clearInterval(poll); clearTimeout(timer); try { child.kill(); } catch {} finish('got-response'); }
    }, 200);
    setTimeout(() => clearInterval(poll), TIMEOUT_MS + 100);
  });

  const why = await done;
  try { child.stdin.end(); } catch {}
  const lines = out.split('\n').filter((l) => l.trim());
  const res = extractUsage(lines);
  const result = { ...res, reason: why, stdout_lines: lines.length };

  const id = store(result, err);
  const summary = {
    probe_id: id, ok: result.ok, reason: why, stdout_lines: lines.length,
    error: result.error ?? null,
    model: result.usage?.model ?? null,
    maxTokens: result.usage?.maxTokens ?? null,
    totalTokens: result.usage?.totalTokens ?? null,
    categories: (result.usage?.categories ?? []).map((c) => ({ name: c.name, tokens: c.tokens })),
    stderr_tail: err.trim().split('\n').slice(-6).join('\n') || null,
  };
  console.log(JSON.stringify(summary, null, 2));
  return result.ok ? 0 : 1;
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  add('spawn args request the control protocol',
    argsFor().join(' ') === '--print --verbose --input-format stream-json --output-format stream-json');
  add('frames initialize before asking', frames()[0].request.subtype === 'initialize' && frames()[1].request.subtype === 'get_context_usage');

  const good = [
    JSON.stringify({ type: 'system', subtype: 'init' }),
    JSON.stringify({ type: 'control_response', response: { subtype: 'success', request_id: 'c4x-1', response: {} } }),
    JSON.stringify({ type: 'control_response', response: { subtype: 'success', request_id: 'c4x-2', response: {
      model: 'claude-opus-5', maxTokens: 1000000, totalTokens: 159400, percentage: 16,
      categories: [{ name: 'System prompt', tokens: 3000, color: 'promptBorder' }, { name: 'Free space', tokens: 800000, color: 'promptBorder' }],
    } } }),
  ];
  const okRes = extractUsage(good);
  add('extracts the usage payload', okRes.ok === true && okRes.usage.maxTokens === 1000000);
  add('extracts categories', okRes.usage.categories.length === 2);

  add('reports a control error rather than claiming success',
    extractUsage([JSON.stringify({ type: 'control_response', response: { subtype: 'error', request_id: 'c4x-2', error: 'not supported' } })]).error === 'not supported');
  add('reports absence rather than inventing a payload',
    extractUsage([JSON.stringify({ type: 'system', subtype: 'init' })]).ok === false);
  add('ignores a response to a different request id',
    extractUsage([JSON.stringify({ type: 'control_response', response: { subtype: 'success', request_id: 'other', response: { categories: [] } } })]).ok === false);
  add('rejects a success payload with no categories',
    extractUsage([JSON.stringify({ type: 'control_response', response: { subtype: 'success', request_id: 'c4x-2', response: { model: 'x' } } })]).ok === false);
  add('survives unparseable lines', extractUsage(['not json', ...good]).ok === true);

  // Negative control: the extractor must actually read the payload rather than returning a
  // canned success. A DIFFERENT payload must produce a different result, and an empty input
  // must not produce a success.
  const altered = JSON.parse(JSON.stringify(good));
  const parsedAlt = JSON.parse(altered[2]);
  parsedAlt.response.response.maxTokens = 200000;
  altered[2] = JSON.stringify(parsedAlt);
  add('different payload yields different result (gate can fail)',
    extractUsage(altered).usage.maxTokens === 200000 && okRes.usage.maxTokens === 1000000);
  add('empty input cannot yield success (gate can fail)', extractUsage([]).ok === false);

  let bad = 0;
  for (const [n, ok, d] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`); }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch the CLI when this file IS the entry point. Without this guard, importing the
// module for its exports runs the dispatch as a side effect: importing harvest.mjs STARTED A
// FULL HARVEST, and importing segments.mjs exited the host process. An independent reviewer hit
// both while trying to reuse the exported functions.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

const argv = process.argv.slice(2);
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) process.exit(selfTest());
else if (argv.includes('--dry-run')) {
  console.log(JSON.stringify({ command: CLAUDE, args: argsFor(), stdin_frames: frames(), timeout_ms: TIMEOUT_MS, db: DB_PATH }, null, 2));
  process.exit(0);
} else process.exit(await runProbe());

#!/usr/bin/env node
// waste.mjs - what in the context was paid for twice, or paid for and never used.
//
// Reads the tool_calls table that harvest.mjs populates. Three reports:
//
//   --duplicates  the same file read repeatedly inside one session. This is the single biggest
//                 cost lever on this machine: every re-read is re-billed on every later request
//                 in the session, so one file read 614 times is not 614 reads, it is 614 reads
//                 multiplied by the turns that follow it.
//   --servers     MCP servers that are CONFIGURED but were never invoked, or not invoked in N
//                 days. A server nobody calls still pays schema rent in every request.
//   --tools       invocation counts per tool, so a tool that is loaded and never used is visible.
//
// The token cost of a loaded-but-unused tool schema was long refused here as unrecoverable: the
// session logs stopped recording tool schemas, so invocation count was reported as a labelled
// PROXY rather than dressed up as a measurement.
//
// It is now measured. otel-gate.mjs proved the raw-body route (2026-08-22, 2.1.237: 18 tools,
// 91.8 KB), otel-ingest.mjs reads those bodies into tool_schemas, and --tools joins to it. Where a
// schema HAS been captured the number is real. Where it has not, this prints UNKNOWN and never 0:
// an uncaptured schema costs an unknown number of tokens, not none.
//
//   node waste.mjs --duplicates [--min N] [--session ID]
//   node waste.mjs --servers [--days N]
//   node waste.mjs --tools
//   node waste.mjs --self-test
//
// Every report accepts --db (or C4X_DB) so it can run against a copy.

import { existsSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { rootFrom, resolveDb } from './paths.mjs';

const ROOT = rootFrom(import.meta.url);
const DEFAULT_DB = join(ROOT, 'data', 'context.db');

const resolveDbArg = (argv = process.argv.slice(2)) => resolveDb(ROOT, argv);

function numArg(argv, flag, dflt) {
  const i = argv.indexOf(flag);
  if (i === -1) return dflt;
  const n = Number(argv[i + 1]);
  return Number.isFinite(n) ? n : dflt;
}

function open(dbPath) {
  if (!existsSync(dbPath)) {
    console.error(`no store at ${dbPath}. Run: node tools/harvest.mjs`);
    process.exit(2);
  }
  return new DatabaseSync(`file:${dbPath.replace(/\\/g, '/')}?mode=ro`, { readOnly: true });
}

/** Re-reads of one target inside one session. n is the read count, not the extra count. */
export function duplicateReads(db, { min = 3, session = null } = {}) {
  const where = session ? 'AND session_id = ?' : '';
  const args = session ? [session, min] : [min];
  return db.prepare(`
    SELECT session_id, target, COUNT(*) reads, SUM(COALESCE(result_bytes,0)) bytes,
           COUNT(DISTINCT input_sha1) distinct_inputs
    FROM tool_calls
    WHERE tool_name IN ('Read','NotebookRead') AND target IS NOT NULL ${where}
    GROUP BY session_id, target
    HAVING reads >= ?
    ORDER BY reads DESC`).all(...args);
}

/**
 * USER-SCOPE MCP servers: the `mcpServers` maps in ~/.claude/settings.json and ~/.claude.json.
 *
 * Deliberately NOT the full inventory. Plugin-provided servers arrive through `enabledPlugins`
 * and project servers through a repo's own .mcp.json, and neither is read here. So a server
 * missing from this set is not evidence it is unconfigured, and the report says so rather than
 * presenting two numbers as a complete audit.
 */
export function configuredServers(home = homedir()) {
  const out = new Set();
  for (const p of [join(home, '.claude', 'settings.json'), join(home, '.claude.json')]) {
    try {
      const d = JSON.parse(readFileSync(p, 'utf8').replace(/^﻿/, ''));
      for (const k of Object.keys(d.mcpServers || {})) out.add(k);
    } catch { /* absent or unreadable is not an error: the other file may hold them */ }
  }
  return out;
}

/** Configured servers joined against invocation counts. Zero-call servers are the finding. */
export function serverUsage(db, { days = null, configured = null } = {}) {
  const since = days ? new Date(Date.now() - days * 86400000).toISOString() : null;
  const rows = since
    ? db.prepare('SELECT server_name, COUNT(*) n, MAX(ts) last FROM tool_calls WHERE server_name IS NOT NULL AND ts >= ? GROUP BY 1').all(since)
    : db.prepare('SELECT server_name, COUNT(*) n, MAX(ts) last FROM tool_calls WHERE server_name IS NOT NULL GROUP BY 1').all();
  const seen = new Map(rows.map((r) => [r.server_name, r]));
  const cfg = configured ?? configuredServers();
  const out = [];
  for (const name of cfg) {
    const r = seen.get(name);
    out.push({ server: name, calls: r?.n ?? 0, last: r?.last ?? null, configured: true });
  }
  // A server that was CALLED but is no longer configured is also worth showing: it means the
  // store holds history the current config does not explain.
  for (const [name, r] of seen) {
    if (!cfg.has(name)) out.push({ server: name, calls: r.n, last: r.last, configured: false });
  }

  // A server's real price is the sum of its tools' schemas, paid on every request whether or not
  // anything calls it. That is exactly the number this report used to say was unrecoverable.
  // Servers are derived from the tool name the same way harvest.mjs does it - mcp__<server>__<tool>
  // split on the double underscore - so the two never disagree about what a server is called.
  const cost = schemaCost(db);
  const byServer = new Map();
  if (cost) {
    for (const c of cost) {
      if (!c.tool_name.startsWith('mcp__')) continue;
      const name = c.tool_name.split('__')[1];
      if (!name) continue;
      const e = byServer.get(name) || { bytes: 0, tools: 0 };
      e.bytes += c.latest_bytes || 0;
      e.tools += 1;
      byServer.set(name, e);
    }
  }
  for (const o of out) {
    const e = cost ? byServer.get(o.server) : null;
    // null, not 0: a server whose tools were never captured has an unknown price, not a free one.
    o.schema_bytes = e ? e.bytes : null;
    o.schema_tools = e ? e.tools : null;
  }

  // A server whose tools appear in a captured request body was demonstrably LOADED - stronger
  // evidence than a settings file, which says what should be loaded rather than what was. One that
  // is neither configured now nor ever called is the clearest case this report exists to find, and
  // it would otherwise be invisible: it has no tool_calls rows to be discovered through.
  const known = new Set(out.map((o) => o.server));
  for (const [name, e] of byServer) {
    if (known.has(name)) continue;
    out.push({
      server: name, calls: 0, last: null, configured: false,
      schema_bytes: e.bytes, schema_tools: e.tools, loaded: true,
    });
  }
  return out.sort((a, b) => a.calls - b.calls || a.server.localeCompare(b.server));
}

/** Do all of these tables exist? Older stores predate otel-ingest.mjs and have none of them. */
function hasTables(db, names) {
  const q = `SELECT name FROM sqlite_master WHERE type='table' AND name IN (${names.map(() => '?').join(',')})`;
  return db.prepare(q).all(...names).length === names.length;
}

/**
 * Measured per-tool schema cost, from bodies otel-ingest.mjs has read.
 *
 * Returns null when the tables do not exist at all, [] when they exist and are empty. Both mean
 * "no measurement", and the caller must render either as UNKNOWN rather than as zero. A tool whose
 * schema was never captured costs an unknown number of tokens, not nought, and the difference is
 * the whole reason this file refused to guess for so long.
 *
 * Latest observation wins, because schema sizes change between builds, and `versions` carries how
 * many distinct schemas have been seen so drift is visible rather than averaged away.
 */
export function schemaCost(db) {
  if (!hasTables(db, ['tool_schemas', 'request_bodies'])) return null;
  return db.prepare(`
    SELECT ts.tool_name,
           COUNT(DISTINCT ts.schema_sha256) versions,
           COUNT(*) observations,
           SUM(CASE WHEN rb.probe = 1 THEN 1 ELSE 0 END) probe_observations,
           (SELECT t2.schema_bytes FROM tool_schemas t2
              JOIN request_bodies b2 ON b2.body_sha256 = t2.body_sha256
             WHERE t2.tool_name = ts.tool_name
             ORDER BY b2.captured_at DESC, b2.ingested_at DESC LIMIT 1) latest_bytes
    FROM tool_schemas ts JOIN request_bodies rb ON rb.body_sha256 = ts.body_sha256
    GROUP BY ts.tool_name`).all();
}

export function toolUsage(db) {
  const calls = db.prepare(`
    SELECT tool_name, COUNT(*) calls, SUM(COALESCE(result_bytes,0)) bytes,
           SUM(COALESCE(is_error,0)) errors
    FROM tool_calls GROUP BY 1`).all();
  const cost = schemaCost(db);
  const blank = { schema_bytes: null, schema_versions: null, schema_probe_only: null };
  if (cost === null) {
    return calls.map((r) => ({ ...r, ...blank })).sort((a, b) => b.calls - a.calls);
  }

  const byName = new Map(cost.map((r) => [r.tool_name, r]));
  const shape = (s) => ({
    schema_bytes: s.latest_bytes, schema_versions: s.versions,
    // A probe body describes the tool set of the session otel-gate SPAWNED, which is not
    // necessarily the one you work in. Flagged, not silently pooled.
    schema_probe_only: s.observations > 0 && s.probe_observations === s.observations,
  });
  const out = calls.map((r) => {
    const s = byName.get(r.tool_name);
    byName.delete(r.tool_name);
    return { ...r, ...(s ? shape(s) : blank) };
  });
  // Tools carrying a schema that were NEVER invoked. This is the number the header of this file
  // said was unrecoverable from the store: schema rent paid on every request, for nothing.
  for (const s of byName.values()) {
    out.push({ tool_name: s.tool_name, calls: 0, bytes: 0, errors: 0, ...shape(s) });
  }
  return out.sort((a, b) => b.calls - a.calls || (b.schema_bytes || 0) - (a.schema_bytes || 0));
}

const kb = (n) => `${(n / 1024).toFixed(1)} KB`;

function reportDuplicates(db, argv) {
  const min = numArg(argv, '--min', 3);
  const i = argv.indexOf('--session');
  const session = i !== -1 ? argv[i + 1] : null;
  const rows = duplicateReads(db, { min, session });
  const extra = rows.reduce((a, r) => a + (r.reads - 1), 0);
  const wasted = rows.reduce((a, r) => a + Math.round(r.bytes * (r.reads - 1) / r.reads), 0);
  console.log(`duplicate reads (>= ${min} reads of one file in one session)`);
  console.log(`  groups: ${rows.length}   re-reads beyond the first: ${extra}   bytes in the repeats: ${kb(wasted)}`);
  if (!rows.length) { console.log('  none, which is the good answer'); return; }
  console.log('');
  for (const r of rows.slice(0, 25)) {
    const same = r.distinct_inputs === 1 ? 'identical' : `${r.distinct_inputs} variants`;
    console.log(`  ${String(r.reads).padStart(5)}x  ${kb(r.bytes).padStart(11)}  ${same.padEnd(12)}  ${r.session_id.slice(0, 8)}  ${r.target}`);
  }
  if (rows.length > 25) console.log(`  ... and ${rows.length - 25} more groups not shown`);
}

function reportServers(db, argv) {
  const days = numArg(argv, '--days', 0) || null;
  const rows = serverUsage(db, { days });
  console.log(`MCP servers${days ? `, invocations in the last ${days} days` : ', invocations over the whole store'}`);
  console.log('  COST: schema bytes are the measured price of a server, summed over its tools and');
  console.log('  paid on every request whether or not anything calls it. UNKNOWN means no raw body');
  console.log('  carrying that server has been ingested - unknown, not free. See otel-ingest.mjs.');
  console.log('  SCOPE: [UNUSED] covers USER-SCOPE servers only (mcpServers in settings.json and');
  console.log('  .claude.json). Plugin-provided and project .mcp.json servers are not counted here.');
  console.log('');
  for (const r of rows) {
    const tag = r.loaded && r.calls === 0 ? '[LOADED, NEVER CALLED]'
      : !r.configured ? '[not configured now]' : r.calls === 0 ? '[UNUSED]' : '';
    const cost = r.schema_bytes === null ? 'UNKNOWN' : `${kb(r.schema_bytes)} / ${r.schema_tools} tools`;
    console.log(`  ${String(r.calls).padStart(6)}  ${cost.padStart(20)}  ${r.server.padEnd(38)} ${tag}`);
  }
  const unused = rows.filter((r) => r.configured && r.calls === 0);
  console.log('');
  console.log(`  configured: ${rows.filter((r) => r.configured).length}   never invoked: ${unused.length}`);

  const idleLoaded = rows.filter((r) => r.loaded && r.calls === 0);
  if (idleLoaded.length) {
    const t = idleLoaded.reduce((a, r) => a + r.schema_bytes, 0);
    console.log(`  LOADED AND NEVER CALLED: ${idleLoaded.length} server(s), ${kb(t)} of schema,`);
    console.log('  observed in a captured request body but invoked by nothing in this store.');
  }
  const priced = unused.filter((r) => r.schema_bytes !== null);
  if (priced.length) {
    const total = priced.reduce((a, r) => a + r.schema_bytes, 0);
    console.log(`  measured cost of the never-invoked: ${kb(total)} across ${priced.length} server(s),`);
    console.log('  paid on every request that carried them.');
  } else if (unused.length) {
    console.log('  their price is UNKNOWN: no ingested body carries their tools.');
  }
}

function reportTools(db) {
  const rows = toolUsage(db);
  const measured = rows.filter((r) => r.schema_bytes !== null);
  const idle = measured.filter((r) => r.calls === 0);

  console.log('tool invocations across the store');
  console.log('');
  console.log(`  ${'calls'.padStart(7)}  ${'result'.padStart(12)}  ${'err'.padStart(5)}  ${'schema'.padStart(9)}  tool`);
  for (const r of rows.slice(0, 30)) {
    // UNKNOWN, never 0: no captured body means no measurement, which is not the same as free.
    const schema = r.schema_bytes === null ? '  UNKNOWN'
      : `${String(r.schema_bytes).padStart(7)} B${r.schema_versions > 1 ? '*' : ''}`;
    console.log(`  ${String(r.calls).padStart(7)}  ${kb(r.bytes).padStart(12)}  ${String(r.errors).padStart(5)}  ${schema.padStart(9)}  ${r.tool_name}`);
  }

  console.log('');
  if (!measured.length) {
    console.log('  schema column is UNKNOWN for every tool: no raw bodies have been ingested.');
    console.log('  Capture some with tools/otel-ingest.mjs --enable, then --ingest.');
    return;
  }
  if (rows.some((r) => r.schema_versions > 1)) {
    console.log('  * more than one distinct schema observed for that tool, so the size has changed');
    console.log('    between captures. The most recent is shown.');
  }
  if (measured.some((r) => r.schema_probe_only)) {
    console.log('  Some schemas come only from otel-gate probe runs, which describe the tool set of');
    console.log('  the session it SPAWNED, not necessarily the one you work in.');
  }
  if (idle.length) {
    const wasted = idle.reduce((a, r) => a + r.schema_bytes, 0);
    console.log('');
    console.log(`  LOADED AND NEVER INVOKED: ${idle.length} tool(s), ${kb(wasted)} of schema`);
    console.log('  paid on every request in the sessions that carried them, for no invocation.');
    for (const r of idle.sort((a, b) => b.schema_bytes - a.schema_bytes).slice(0, 15)) {
      console.log(`    ${String(r.schema_bytes).padStart(7)} B  ${r.tool_name}`);
    }
  } else {
    console.log(`  Every tool with a captured schema was invoked at least once (${measured.length} measured).`);
  }
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE tool_calls (tool_use_id TEXT PRIMARY KEY, session_id TEXT, turn_uuid TEXT,
    ts TEXT, tool_name TEXT, server_name TEXT, target TEXT, input_sha1 TEXT, input_bytes INTEGER,
    result_bytes INTEGER, is_error INTEGER, is_sidechain INTEGER, file_path TEXT, line_no INTEGER)`);
  const ins = db.prepare('INSERT INTO tool_calls (tool_use_id,session_id,ts,tool_name,server_name,target,input_sha1,result_bytes,is_error) VALUES (?,?,?,?,?,?,?,?,?)');
  ins.run('a', 's1', '2026-08-01T00:00:00Z', 'Read', null, '/x/dup.md', 'h1', 100, 0);
  ins.run('b', 's1', '2026-08-02T00:00:00Z', 'Read', null, '/x/dup.md', 'h1', 100, 0);
  ins.run('c', 's1', '2026-08-03T00:00:00Z', 'Read', null, '/x/dup.md', 'h2', 100, 0);
  ins.run('d', 's1', '2026-08-04T00:00:00Z', 'Read', null, '/x/once.md', 'h3', 50, 0);
  ins.run('e', 's2', '2026-08-05T00:00:00Z', 'Read', null, '/x/dup.md', 'h1', 100, 0);
  ins.run('f', 's1', '2026-08-06T00:00:00Z', 'mcp__used__go', 'used', null, 'h4', 10, 0);
  ins.run('g', 's1', '2026-08-07T00:00:00Z', 'Bash', null, null, 'h5', 20, 1);

  const dup = duplicateReads(db, { min: 3 });
  add('finds the file read three times', dup.length === 1 && dup[0].reads === 3, JSON.stringify(dup.map((d) => d.reads)));
  add('counts distinct inputs, so identical vs varied reads are separable',
    dup[0]?.distinct_inputs === 2, String(dup[0]?.distinct_inputs));
  add('does NOT merge the same file across two sessions',
    !dup.some((d) => d.reads === 4), JSON.stringify(dup));
  add('a file read once is not reported', !dup.some((d) => d.target === '/x/once.md'));
  // Negative controls: a report that always returned rows would pass the checks above.
  add('a higher threshold returns fewer groups (gate can fail)', duplicateReads(db, { min: 4 }).length === 0);
  const empty = new DatabaseSync(':memory:');
  empty.exec(`CREATE TABLE tool_calls (tool_use_id TEXT, session_id TEXT, ts TEXT, tool_name TEXT,
    server_name TEXT, target TEXT, input_sha1 TEXT, result_bytes INTEGER, is_error INTEGER)`);
  add('an empty table yields no groups, not a crash', duplicateReads(empty, { min: 1 }).length === 0);
  add('session filter narrows to one session',
    duplicateReads(db, { min: 1, session: 's2' }).every((r) => r.session_id === 's2'));

  const cfg = new Set(['used', 'never-called']);
  const su = serverUsage(db, { configured: cfg });
  const never = su.find((r) => r.server === 'never-called');
  add('a configured server with zero calls is reported', never && never.calls === 0, JSON.stringify(never));
  add('a configured server that WAS called shows its count',
    su.find((r) => r.server === 'used')?.calls === 1);
  add('zero-call servers sort first', su[0].calls === 0);
  add('a called-but-unconfigured server is still surfaced',
    serverUsage(db, { configured: new Set() }).some((r) => r.server === 'used' && r.configured === false));

  const tu = toolUsage(db);
  add('tool usage counts Bash', tu.find((r) => r.tool_name === 'Bash')?.calls === 1);
  add('tool usage carries error counts', tu.find((r) => r.tool_name === 'Bash')?.errors === 1);
  // Five Read rows: a,b,c at 100 each in s1, d at 50 in s1, e at 100 in s2.
  add('tool usage sums result bytes across sessions', tu.find((r) => r.tool_name === 'Read')?.bytes === 450,
    String(tu.find((r) => r.tool_name === 'Read')?.bytes));

  add('configuredServers returns a Set and does not throw on a missing home',
    configuredServers(join(ROOT, 'tmp', 'no-such-home')) instanceof Set);

  // --- the tool_schemas join -------------------------------------------------------------
  // Absence must not read as zero. A store with no schema tables at all is the common case.
  add('with no tool_schemas table, schema cost is null (unknown), not 0',
    schemaCost(db) === null && tu.every((r) => r.schema_bytes === null));

  const sdb = new DatabaseSync(':memory:');
  sdb.exec(`CREATE TABLE tool_calls (tool_use_id TEXT, session_id TEXT, ts TEXT, tool_name TEXT,
    server_name TEXT, target TEXT, input_sha1 TEXT, result_bytes INTEGER, is_error INTEGER);
    CREATE TABLE request_bodies (body_sha256 TEXT PRIMARY KEY, captured_at TEXT, ingested_at TEXT, probe INTEGER);
    CREATE TABLE tool_schemas (body_sha256 TEXT, tool_name TEXT, schema_bytes INTEGER, schema_sha256 TEXT)`);
  sdb.prepare('INSERT INTO tool_calls (tool_use_id,session_id,ts,tool_name,result_bytes,is_error) VALUES (?,?,?,?,?,?)')
    .run('x', 's1', '2026-08-01T00:00:00Z', 'Read', 10, 0);
  const insB = sdb.prepare('INSERT INTO request_bodies (body_sha256,captured_at,ingested_at,probe) VALUES (?,?,?,?)');
  const insS = sdb.prepare('INSERT INTO tool_schemas (body_sha256,tool_name,schema_bytes,schema_sha256) VALUES (?,?,?,?)');
  insB.run('b1', '2026-08-01T00:00:00Z', 'i', 0);
  insS.run('b1', 'Read', 500, 'h-read-1');
  insS.run('b1', 'NeverUsed', 900, 'h-never');
  insS.run('b1', 'ProbeOnly', 100, 'h-probe');

  const joined = toolUsage(sdb);
  const read = joined.find((r) => r.tool_name === 'Read');
  const idleTool = joined.find((r) => r.tool_name === 'NeverUsed');
  add('an invoked tool carries its measured schema bytes', read?.schema_bytes === 500, JSON.stringify(read));
  add('a tool with a schema but ZERO invocations is surfaced', idleTool && idleTool.calls === 0 && idleTool.schema_bytes === 900, JSON.stringify(idleTool));
  add('a tool in tool_calls with no captured schema stays UNKNOWN, not 0',
    (() => { sdb.prepare('INSERT INTO tool_calls (tool_use_id,session_id,ts,tool_name,result_bytes,is_error) VALUES (?,?,?,?,?,?)')
      .run('y', 's1', '2026-08-02T00:00:00Z', 'Unmeasured', 5, 0);
      return toolUsage(sdb).find((r) => r.tool_name === 'Unmeasured')?.schema_bytes === null; })());

  // Latest observation wins, and drift is reported rather than averaged.
  insB.run('b2', '2026-08-09T00:00:00Z', 'i', 0);
  insS.run('b2', 'Read', 650, 'h-read-2');
  const read2 = toolUsage(sdb).find((r) => r.tool_name === 'Read');
  add('the most recent capture wins when a schema changes', read2.schema_bytes === 650, String(read2.schema_bytes));
  add('schema drift is counted, not averaged away', read2.schema_versions === 2, String(read2.schema_versions));

  // Probe provenance must survive to the report.
  const pdb = new DatabaseSync(':memory:');
  pdb.exec(`CREATE TABLE tool_calls (tool_use_id TEXT, session_id TEXT, ts TEXT, tool_name TEXT,
    server_name TEXT, target TEXT, input_sha1 TEXT, result_bytes INTEGER, is_error INTEGER);
    CREATE TABLE request_bodies (body_sha256 TEXT PRIMARY KEY, captured_at TEXT, ingested_at TEXT, probe INTEGER);
    CREATE TABLE tool_schemas (body_sha256 TEXT, tool_name TEXT, schema_bytes INTEGER, schema_sha256 TEXT)`);
  pdb.prepare('INSERT INTO request_bodies (body_sha256,captured_at,ingested_at,probe) VALUES (?,?,?,?)').run('p1', '2026-08-01T00:00:00Z', 'i', 1);
  pdb.prepare('INSERT INTO tool_schemas (body_sha256,tool_name,schema_bytes,schema_sha256) VALUES (?,?,?,?)').run('p1', 'Read', 400, 'h');
  add('a schema seen only in probe bodies is flagged as such',
    toolUsage(pdb).find((r) => r.tool_name === 'Read')?.schema_probe_only === true);
  add('a schema seen in a real body is NOT flagged probe-only (gate can fail)',
    toolUsage(sdb).find((r) => r.tool_name === 'Read')?.schema_probe_only === false);

  // Server-level price, derived from mcp__<server>__<tool> exactly as harvest.mjs derives it.
  insS.run('b1', 'mcp__acme__alpha', 300, 'h-a');
  insS.run('b1', 'mcp__acme__beta', 200, 'h-b');
  const svc = serverUsage(sdb, { configured: new Set(['acme', 'unmeasured']) });
  const acme = svc.find((r) => r.server === 'acme');
  add('an MCP server is priced by summing its tools schemas',
    acme?.schema_bytes === 500 && acme?.schema_tools === 2, JSON.stringify(acme));
  add('a configured server with no captured tools is UNKNOWN, not free',
    svc.find((r) => r.server === 'unmeasured')?.schema_bytes === null);
  // Server names contain single underscores; the separator is a DOUBLE underscore. Splitting on
  // the wrong one silently attributes Claude_Browser's schemas to a server called "Claude".
  insS.run('b1', 'mcp__Claude_Browser__read_page', 111, 'h-cb');
  add('a server name containing an underscore survives the split (gate can fail)',
    serverUsage(sdb, { configured: new Set(['Claude_Browser']) })
      .find((r) => r.server === 'Claude_Browser')?.schema_bytes === 111);
  // The case with no tool_calls rows and no settings entry: invisible before the schema join.
  insS.run('b1', 'mcp__ghost__one', 77, 'h-g1');
  insS.run('b1', 'mcp__ghost__two', 23, 'h-g2');
  const ghosts = serverUsage(sdb, { configured: new Set() });
  const ghost = ghosts.find((r) => r.server === 'ghost');
  add('a server that is loaded but never called and never configured is still found',
    ghost && ghost.calls === 0 && ghost.loaded === true && ghost.schema_bytes === 100, JSON.stringify(ghost));
  add('a called server is NOT mislabelled as loaded-but-idle (gate can fail)',
    !ghosts.find((r) => r.server === 'acme' && r.loaded && r.calls > 0));
  add('a non-MCP tool is not attributed to any server',
    !serverUsage(sdb, { configured: new Set(['Read']) }).find((r) => r.server === 'Read')?.schema_bytes);

  // Falsification: emptying the schema table must take the measurement away again.
  sdb.exec('DELETE FROM tool_schemas');
  add('removing every schema row returns the report to UNKNOWN (gate can fail)',
    toolUsage(sdb).every((r) => r.schema_bytes === null));

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch the CLI when this file IS the entry point. Without this guard, importing the
// module for its exports runs the dispatch as a side effect. probe.mjs records two earlier
// victims; otel-ingest.mjs became a third the moment it imported inspectBody from here.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

const argv = process.argv.slice(2);
if (IS_ENTRY) {
  if (argv.includes('--self-test')) process.exit(selfTest());
  const db = open(resolveDbArg(argv));
  if (argv.includes('--duplicates')) reportDuplicates(db, argv);
  else if (argv.includes('--servers')) reportServers(db, argv);
  else if (argv.includes('--tools')) reportTools(db);
  else {
    console.log('specify --duplicates, --servers, --tools, or --self-test');
    process.exit(2);
  }
  process.exit(0);
}

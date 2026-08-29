#!/usr/bin/env node
// otel-ingest.mjs - turn raw API request bodies into measured schema cost in the store.
//
// otel-gate.mjs answered whether the route works. It does: 18 tools and 91.8 KB of schema, with no
// collector. But answering a question is not capturing it, and nothing read those bodies into the
// store, so per-tool schema cost stayed unmeasured and waste.mjs went on reporting invocation
// counts as a labelled PROXY for it. This closes that half.
//
// WHAT IS DELIBERATELY NOT STORED: message text, system prompt text, tool descriptions, any
// content at all. A raw body is the entire conversation plus the system prompt - strictly more
// sensitive than a transcript, and data/ is gitignored precisely because transcripts are. Copying
// bodies into SQLite would make the store a second, queryable copy of every conversation. Only
// counts, byte sizes and hashes are kept, which is all the cost question needs. The self-test
// asserts this rather than trusting the comment.
//
//   node otel-ingest.mjs --status            what is on disk, what is ingested, is the gate armed
//   node otel-ingest.mjs --enable            print the env block to export. Sets nothing itself
//   node otel-ingest.mjs --ingest            read the gate's bodies (tmp/otel-bodies) into the store
//   node otel-ingest.mjs --ingest --dir <d>  read bodies captured from real work instead
//   node otel-ingest.mjs --self-test
//
// --ingest refuses unless C4X_OTEL_BODIES=1. Two gates, one of them ours, so a directory of raw
// conversations is never hoovered into the store because an env var was left set in a shell.

import { createHash } from 'node:crypto';
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { rootFrom, resolveDb } from './paths.mjs';
import { inspectBody } from './otel-gate.mjs';

// Three writers share this store by design: a manual harvest, the SessionEnd and UserPromptSubmit
// hooks, and the dashboard's refresh loop. SQLite's default busy timeout is ZERO, so a reader that
// arrives mid-write fails outright with SQLITE_BUSY instead of waiting. A reader losing a race
// should be slow, not absent.
const BUSY_TIMEOUT = 'PRAGMA busy_timeout = 15000';

const ROOT = rootFrom(import.meta.url);
const GATE_DIR = join(ROOT, 'tmp', 'otel-bodies');
const UNPARSED = join(ROOT, 'data', 'raw', 'otel-unparsed.ndjson');

export const SCHEMA = `
-- One row per raw request body. Identity is the CONTENT hash, not the filename: the gate wipes and
-- rewrites its directory on every run, so filenames repeat across captures that differ, and two
-- captures of an identical body are one observation, not two.
CREATE TABLE IF NOT EXISTS request_bodies (
  body_sha256 TEXT PRIMARY KEY,
  file_name TEXT, captured_at TEXT, ingested_at TEXT, source_dir TEXT,
  bytes INTEGER, model TEXT, message_count INTEGER,
  has_system INTEGER, system_bytes INTEGER,
  tool_count INTEGER, tool_schema_bytes INTEGER,
  probe INTEGER
);
-- One row per tool per body. schema_sha256 is what makes a schema CHANGE visible across builds:
-- sizes alone would call a rewritten schema of the same length unchanged.
CREATE TABLE IF NOT EXISTS tool_schemas (
  body_sha256 TEXT, tool_name TEXT,
  schema_bytes INTEGER, description_bytes INTEGER, input_schema_bytes INTEGER,
  schema_sha256 TEXT,
  PRIMARY KEY (body_sha256, tool_name)
);
CREATE INDEX IF NOT EXISTS tool_schemas_name ON tool_schemas (tool_name);
`;

const sha = (s) => createHash('sha256').update(s).digest('hex');
const bytes = (v) => (v === undefined ? 0 : Buffer.byteLength(JSON.stringify(v), 'utf8'));

/**
 * Per-tool measurements for one body.
 *
 * inspectBody() caps toolNames at 8 because it exists to print a summary; this needs every tool, so
 * it walks the array itself. A tool with no usable name is COUNTED as skipped rather than dropped:
 * a silent skip here would understate schema cost and look exactly like a build that ships fewer
 * tools.
 */
export function toolRows(json) {
  let d;
  try { d = JSON.parse(json); } catch { return { parsed: false, rows: [], skippedUnnamed: 0 }; }
  const tools = Array.isArray(d.tools) ? d.tools : [];
  const rows = [];
  let skippedUnnamed = 0;
  for (const t of tools) {
    const name = typeof t?.name === 'string' && t.name ? t.name : null;
    if (!name) { skippedUnnamed++; continue; }
    const ser = JSON.stringify(t);
    rows.push({
      tool_name: name,
      schema_bytes: Buffer.byteLength(ser, 'utf8'),
      description_bytes: bytes(t.description),
      input_schema_bytes: bytes(t.input_schema ?? t.inputSchema),
      schema_sha256: sha(ser),
    });
  }
  return { parsed: true, rows, skippedUnnamed, systemBytes: bytes(d.system) };
}

export function openStore(dbPath) {
  mkdirSync(join(dbPath, '..'), { recursive: true });
  const db = new DatabaseSync(dbPath);
  db.exec(BUSY_TIMEOUT);
  db.exec(SCHEMA);
  return db;
}

/**
 * Ingest every *.request.json in `dir`.
 *
 * INSERT OR IGNORE on the content hash, so re-running is a no-op rather than a duplicate - the same
 * property harvest.mjs relies on. A missing directory is zero rows and not an error: the gate may
 * simply never have run, which is itself the answer to "why is there no schema data".
 */
export function ingestDir(db, dir, { probe = false, unparsedLog = null, now = () => new Date().toISOString() } = {}) {
  const out = {
    dir, exists: existsSync(dir), files: 0, ingested: 0, duplicates: 0,
    unparsed: 0, no_tools: 0, tool_rows: 0, skipped_unnamed: 0,
  };
  if (!out.exists) return out;

  const insBody = db.prepare(`INSERT OR IGNORE INTO request_bodies
    (body_sha256,file_name,captured_at,ingested_at,source_dir,bytes,model,message_count,
     has_system,system_bytes,tool_count,tool_schema_bytes,probe)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const insTool = db.prepare(`INSERT OR IGNORE INTO tool_schemas
    (body_sha256,tool_name,schema_bytes,description_bytes,input_schema_bytes,schema_sha256)
    VALUES (?,?,?,?,?,?)`);

  for (const f of readdirSync(dir).filter((x) => x.endsWith('.request.json'))) {
    out.files++;
    const p = join(dir, f);
    const raw = readFileSync(p, 'utf8');
    const hash = sha(raw);
    const info = inspectBody(raw);
    const tr = toolRows(raw);

    if (!info.parsed || !tr.parsed) {
      out.unparsed++;
      // Never drop it silently. The sidecar records WHICH file and why, the same contract
      // harvest.mjs keeps with unknown record types.
      if (unparsedLog) {
        mkdirSync(join(unparsedLog, '..'), { recursive: true });
        appendFileSync(unparsedLog, `${JSON.stringify({
          captured_at: now(), file: p, bytes: raw.length, reason: 'body did not parse as JSON',
        })}\n`);
      }
      continue;
    }
    if (!info.hasTools) out.no_tools++;
    out.skipped_unnamed += tr.skippedUnnamed;

    const r = insBody.run(hash, f, new Date(statSync(p).mtimeMs).toISOString(), now(), dir,
      Buffer.byteLength(raw, 'utf8'), info.model, info.messageCount,
      info.hasSystem ? 1 : 0, tr.systemBytes, info.toolCount, info.toolSchemaBytes, probe ? 1 : 0);
    if (r.changes) out.ingested++; else out.duplicates++;

    for (const t of tr.rows) {
      const rr = insTool.run(hash, t.tool_name, t.schema_bytes, t.description_bytes,
        t.input_schema_bytes, t.schema_sha256);
      if (rr.changes) out.tool_rows++;
    }
  }
  return out;
}

// ---------------------------------------------------------------- commands

const argFor = (argv, flag) => { const i = argv.indexOf(flag); return i === -1 ? null : argv[i + 1]; };

function cmdEnable() {
  const dir = GATE_DIR.replace(/\\/g, '/');
  console.log('Raw-body capture writes EVERY request in full: system prompt, every message, tool');
  console.log('schemas. It is strictly more sensitive than a transcript. Local only.');
  console.log('');
  console.log('PowerShell, this shell only:');
  console.log(`  $env:OTEL_LOG_RAW_API_BODIES = "file:${dir}"`);
  console.log('  $env:CLAUDE_CODE_ENABLE_TELEMETRY = "1"');
  console.log('  $env:C4X_OTEL_BODIES = "1"');
  console.log('');
  console.log('bash:');
  console.log(`  export OTEL_LOG_RAW_API_BODIES="file:${dir}"`);
  console.log('  export CLAUDE_CODE_ENABLE_TELEMETRY=1');
  console.log('  export C4X_OTEL_BODIES=1');
  console.log('');
  console.log('This command sets nothing. Exporting is yours, and so is unsetting it.');
  return 0;
}

function cmdStatus(argv) {
  const dir = argFor(argv, '--dir') || GATE_DIR;
  const dbPath = resolveDb(ROOT, argv);
  const envVar = process.env.OTEL_LOG_RAW_API_BODIES || null;
  const armed = process.env.C4X_OTEL_BODIES === '1';

  console.log(`body dir     : ${dir.replace(/\\/g, '/')}${existsSync(dir) ? '' : '  (does not exist)'}`);
  const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith('.request.json')) : [];
  console.log(`bodies       : ${files.length}`);
  console.log(`OTEL var     : ${envVar ? envVar : 'not set in THIS process'}`);
  console.log(`C4X_OTEL_BODIES : ${armed ? '1 (ingest permitted)' : 'not set (ingest refuses)'}`);

  if (!existsSync(dbPath)) { console.log(`store        : ${dbPath} (not created yet)`); return files.length ? 1 : 0; }
  const db = new DatabaseSync(dbPath);
  db.exec(BUSY_TIMEOUT);
  db.exec(SCHEMA);
  const b = db.prepare('SELECT COUNT(*) c, COALESCE(SUM(probe),0) p FROM request_bodies').get();
  const t = db.prepare('SELECT COUNT(*) c, COUNT(DISTINCT tool_name) d, COALESCE(SUM(schema_bytes),0) s FROM tool_schemas').get();
  console.log(`store        : ${dbPath}`);
  console.log(`  bodies ingested : ${b.c}  (${b.p} from gate probe runs)`);
  console.log(`  tool rows       : ${t.c} across ${t.d} distinct tools, ${(t.s / 1024).toFixed(1)} KB of schema`);
  if (t.d) {
    console.log('');
    console.log('  largest schemas:');
    for (const r of db.prepare(`SELECT tool_name, MAX(schema_bytes) b FROM tool_schemas
      GROUP BY tool_name ORDER BY b DESC LIMIT 8`).all()) {
      console.log(`    ${String(r.b).padStart(7)} B  ${r.tool_name}`);
    }
  }
  return 0;
}

function cmdIngest(argv) {
  if (process.env.C4X_OTEL_BODIES !== '1') {
    console.error('refusing: C4X_OTEL_BODIES is not 1.');
    console.error('Raw bodies are whole conversations. Ingest is opt-in on purpose, so a directory');
    console.error('of them is never absorbed because an env var was left set. See --enable.');
    return 2;
  }
  const explicitDir = argFor(argv, '--dir');
  const dir = explicitDir || GATE_DIR;
  // Bodies in the gate's own directory came from `otel-gate.mjs --run`, which sends a synthetic
  // one-word prompt. Those are probes by construction, and the statusline lesson is that a test
  // write must identify itself at write time rather than by a heuristic later.
  const probe = argv.includes('--probe') || !explicitDir;
  const dbPath = resolveDb(ROOT, argv);
  const db = openStore(dbPath);
  const r = ingestDir(db, dir, { probe, unparsedLog: UNPARSED });

  if (!r.exists) {
    console.log(`no body directory at ${dir.replace(/\\/g, '/')}`);
    console.log('Nothing has been captured yet. See --enable, then `otel-gate.mjs --run`.');
    return 1;
  }
  console.log(JSON.stringify({ ...r, db: dbPath, probe }, null, 2));
  if (r.unparsed) console.log(`\n${r.unparsed} body/bodies did not parse; recorded in ${UNPARSED}`);
  if (r.skipped_unnamed) console.log(`${r.skipped_unnamed} tool entries had no name and were counted, not stored`);
  return r.files ? 0 : 1;
}

// ---------------------------------------------------------------- self-test

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  const SECRET = 'RITUAL-CANARY-do-not-store-this-message';
  const body = JSON.stringify({
    model: 'claude-opus-5',
    system: [{ type: 'text', text: `system prompt ${SECRET}` }],
    messages: [{ role: 'user', content: SECRET }, { role: 'assistant', content: 'ok' }],
    tools: [
      { name: 'Read', description: 'reads a file', input_schema: { type: 'object', properties: { p: { type: 'string' } } } },
      { name: 'Bash', description: 'runs a command', input_schema: { type: 'object' } },
      { description: 'nameless, must be counted not dropped', input_schema: {} },
    ],
  });

  const tr = toolRows(body);
  add('every tool is measured, not just the first 8 that inspectBody prints', tr.rows.length === 2);
  add('a nameless tool is COUNTED as skipped, never silently dropped', tr.skippedUnnamed === 1);
  add('per-tool bytes are recorded', tr.rows.every((r) => r.schema_bytes > 0));
  add('description and input_schema are measured separately',
    tr.rows[0].description_bytes > 0 && tr.rows[0].input_schema_bytes > 0);
  // Cross-check against the primitive otel-gate already ships, rather than trusting either alone.
  const named = JSON.parse(body).tools.filter((t) => t.name);
  const expected = named.reduce((a, t) => a + Buffer.byteLength(JSON.stringify(t), 'utf8'), 0);
  add('per-tool sizes agree with inspectBody total over the same tools',
    tr.rows.reduce((a, r) => a + r.schema_bytes, 0) === expected);
  add('unparseable input returns parsed:false rather than throwing', toolRows('{not json').parsed === false);
  add('a body with no tools array yields zero rows, not an error',
    toolRows(JSON.stringify({ model: 'x' })).rows.length === 0);

  const tmp = join(ROOT, 'tmp', `otel-ingest-selftest-${process.pid}`);
  mkdirSync(tmp, { recursive: true });
  writeFileSync(join(tmp, 'a.request.json'), body);
  writeFileSync(join(tmp, 'b.request.json'), '{ not json at all');

  const db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
  const r1 = ingestDir(db, tmp, { probe: true });
  add('one good body ingests', r1.ingested === 1, JSON.stringify(r1));
  add('the bad body is counted as unparsed, not thrown', r1.unparsed === 1);
  add('tool rows land', r1.tool_rows === 2);

  const r2 = ingestDir(db, tmp, { probe: true });
  add('re-ingesting the same directory stores nothing new (idempotent)', r2.ingested === 0 && r2.tool_rows === 0, JSON.stringify(r2));
  add('the duplicate is reported rather than hidden', r2.duplicates === 1);
  add('body count is still 1 after two runs',
    db.prepare('SELECT COUNT(*) c FROM request_bodies').get().c === 1);

  // Identity is content, not filename: the gate rewrites the same names every run.
  writeFileSync(join(tmp, 'renamed.request.json'), body);
  const r3 = ingestDir(db, tmp, { probe: true });
  add('the same content under a new filename is not a second observation', r3.ingested === 0);

  // THE PRIVACY PROPERTY, asserted rather than asserted-in-a-comment.
  const dump = JSON.stringify([
    db.prepare('SELECT * FROM request_bodies').all(),
    db.prepare('SELECT * FROM tool_schemas').all(),
  ]);
  add('no message text reaches the store', !dump.includes(SECRET), 'canary found in store');
  add('measurements DID reach the store (so the check above is not vacuous)',
    db.prepare('SELECT system_bytes s, tool_schema_bytes t FROM request_bodies').get().s > 0);

  // Falsification: a changed schema of the SAME length must still be detected.
  const mutated = body.replace('"reads a file"', '"reads a FILE"');
  add('a schema edit of identical length changes its hash (gate can fail)',
    toolRows(mutated).rows[0].schema_sha256 !== tr.rows[0].schema_sha256,
    `${toolRows(mutated).rows[0].schema_bytes} vs ${tr.rows[0].schema_bytes} bytes`);

  const missing = ingestDir(db, join(ROOT, 'tmp', 'no-such-dir-here'), {});
  add('a missing directory is zero rows and not an error', missing.exists === false && missing.files === 0);

  rmSync(tmp, { recursive: true, force: true });

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : `  [${d}]`}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// ---------------------------------------------------------------- entry

// Only dispatch the CLI when this file IS the entry point, so importing the exports never runs
// the command. probe.mjs records two earlier victims of the missing guard.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

const argv = process.argv.slice(2);
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) process.exit(selfTest());
else if (argv.includes('--enable')) process.exit(cmdEnable());
else if (argv.includes('--ingest')) process.exit(cmdIngest(argv));
else if (argv.includes('--status')) process.exit(cmdStatus(argv));
else { console.log('specify --status, --enable, --ingest, or --self-test'); process.exit(2); }

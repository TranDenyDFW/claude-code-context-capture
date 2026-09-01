#!/usr/bin/env node
// harvest.mjs - transcript harvester for the context-capture store.
//
// Streams every Claude Code session transcript under ~/.claude/projects into SQLite:
// per-turn token usage (exact, from the API response), compaction boundaries and their
// summaries, attachment inventory, and a global record-type census.
//
// Incremental: each file's byte offset is stored, so a rerun reads only what was appended.
// A file that SHRANK is treated as rewritten (the local-GC path) and is re-read in full,
// and the event is reported rather than silently absorbed.
//
// Usage:
//   node harvest.mjs                 harvest incrementally
//   node harvest.mjs --full          ignore stored offsets, re-read everything
//   node harvest.mjs --self-test     prove the parser detects what it claims to detect
//   node harvest.mjs --stats         print store contents, harvest nothing

import { createReadStream, existsSync, mkdirSync, readdirSync, statSync, appendFileSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { rootFrom, resolveDb } from './paths.mjs';
import { homedir } from 'node:os';

const ROOT = rootFrom(import.meta.url);
const PROJECTS = join(homedir(), '.claude', 'projects');
// Store path, overridable so a run can target a copy instead of the live store. An independent
// reviewer had to mutate the real database to verify --backfill-survivors, because there was no
// way to point it elsewhere; a tool that can only be exercised against production data is a tool
// that will not be exercised. Precedence: --db flag, then C4X_DB, then the default.
const DEFAULT_DB_PATH = join(ROOT, 'data', 'context.db');

const resolveDbPath = (argv = process.argv.slice(2)) => resolveDb(ROOT, argv);

const DB_PATH = resolveDbPath();
const RAW_DIR = join(ROOT, 'data', 'raw');
const UNKNOWN_LOG = join(RAW_DIR, 'unknown-records.ndjson');

const SCHEMA = `
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, mtime_ms INTEGER,
  bytes_read INTEGER, lines_read INTEGER, rewrites INTEGER DEFAULT 0, last_harvest_ts TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, project_slug TEXT, cwd TEXT, git_branch TEXT,
  version TEXT, entrypoint TEXT, first_ts TEXT, last_ts TEXT, transcript_path TEXT
);
CREATE TABLE IF NOT EXISTS turns (
  uuid TEXT PRIMARY KEY, session_id TEXT, ts TEXT, model TEXT, request_id TEXT,
  input_tokens INTEGER, cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
  output_tokens INTEGER, thinking_tokens INTEGER, eph_1h INTEGER, eph_5m INTEGER,
  service_tier TEXT, total_resident INTEGER, is_sidechain INTEGER, file_path TEXT, line_no INTEGER,
  -- The record this one replied to. Present on 30,400 of 42,407 records in this store's four
  -- largest transcripts and read by nothing until now. It is what turns a flat list of turns into
  -- the tree it actually was: a subagent's turns hang off the Agent call that spawned them, which
  -- is the only way to attribute the ~70% of calls that are subagent work to anything.
  parent_uuid TEXT
);
CREATE INDEX IF NOT EXISTS turns_session_ts ON turns(session_id, ts);
CREATE INDEX IF NOT EXISTS turns_request ON turns(request_id);
-- ONE ROW PER API CALL. Read this before summing anything out of turns.
--
-- Claude Code writes a streamed assistant message as SEVERAL transcript entries, one per content
-- block, and stamps every one with the same requestId. 80,815 request_ids cover 227,337 rows here,
-- so turns holds about 2.8 rows per actual API call. Each row is a real record with its own uuid
-- and line number, which is why they are all kept, but SUMMING token columns across them counts
-- the same call two to eight times. Measured: summing turns gave 56.90 B tokens where the same
-- transcripts deduped give 27.12 B, against ccusage's independent 28.75 B.
--
-- The two sides behave differently, which is why this is not a plain DISTINCT:
--   input / cache_creation / cache_read are IDENTICAL across a request's rows (varies in 19 of
--     113,229), so taking the max is taking the value.
--   output_tokens ACCUMULATES as the message streams (varies in 42,922), so the max is the final
--     count and any other row understates it.
CREATE VIEW IF NOT EXISTS api_calls AS
SELECT
  request_id,
  MIN(session_id)                        AS session_id,
  MIN(ts)                                AS ts,
  MIN(model)                             AS model,
  MAX(input_tokens)                      AS input_tokens,
  MAX(cache_creation_input_tokens)       AS cache_creation_input_tokens,
  MAX(cache_read_input_tokens)           AS cache_read_input_tokens,
  MAX(output_tokens)                     AS output_tokens,
  MAX(thinking_tokens)                   AS thinking_tokens,
  MAX(total_resident)                    AS total_resident,
  MAX(is_sidechain)                      AS is_sidechain,
  COUNT(*)                               AS transcript_rows
FROM turns
WHERE request_id IS NOT NULL
GROUP BY request_id;
-- The text of every record, so the store can answer "what was actually said" and not only "how
-- big was it". Everything else here is measurements; this table is the one that holds content.
-- It is what makes a compaction summary readable instead of a character count, and what makes a
-- dropped message recoverable at all.
--
-- Unconditional while installed. There was an opt-out; it was removed, because a store that can be
-- silently switched to measurements-only still LOOKS complete, and a reader has no way to tell
-- which sessions were captured under which setting. To stop capturing, uninstall.
CREATE TABLE IF NOT EXISTS messages (
  uuid TEXT PRIMARY KEY, session_id TEXT, ts TEXT, role TEXT, type TEXT,
  text TEXT, chars INTEGER, model TEXT, request_id TEXT,
  is_sidechain INTEGER, file_path TEXT, line_no INTEGER
);
CREATE INDEX IF NOT EXISTS messages_session_ts ON messages(session_id, ts);
CREATE INDEX IF NOT EXISTS messages_type ON messages(type);
CREATE TABLE IF NOT EXISTS compactions (
  uuid TEXT PRIMARY KEY, session_id TEXT, ts TEXT, trigger TEXT, version TEXT, entrypoint TEXT,
  pre_tokens INTEGER, post_tokens INTEGER, duration_ms INTEGER,
  cumulative_dropped_tokens INTEGER, messages_summarized INTEGER,
  discovered_tools_json TEXT, preserved_json TEXT,
  summary_uuid TEXT, summary_chars INTEGER, file_path TEXT, line_no INTEGER
);
CREATE INDEX IF NOT EXISTS compactions_session_ts ON compactions(session_id, ts);
-- Which records survived a compaction, by uuid. Present from build v2.1.163 onward; older
-- boundaries record token counts only, so an empty result for an old compaction is correct
-- rather than missing data.
CREATE TABLE IF NOT EXISTS compaction_survivors (
  compaction_uuid TEXT, kind TEXT, uuid TEXT,
  PRIMARY KEY (compaction_uuid, kind, uuid)
);
CREATE INDEX IF NOT EXISTS survivors_uuid ON compaction_survivors(uuid);
CREATE TABLE IF NOT EXISTS attachments (
  session_id TEXT, type TEXT, n INTEGER, PRIMARY KEY (session_id, type)
);
-- Every tool_use block, with its result size filled in when the matching tool_result is seen.
-- The turns table holds token counts only, so until now nothing recorded WHICH tool call grew the
-- window. target is the file path or url when the tool has one, which is what makes a duplicate
-- read visible; input_sha1 covers the exact-repeat case where two calls are byte-identical.
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_use_id TEXT PRIMARY KEY,
  session_id TEXT, turn_uuid TEXT, ts TEXT,
  tool_name TEXT, server_name TEXT,
  target TEXT, input_sha1 TEXT, input_bytes INTEGER,
  result_bytes INTEGER, is_error INTEGER,
  is_sidechain INTEGER, file_path TEXT, line_no INTEGER,
  -- Which KIND of subagent an Agent call asked for, e.g. "general-purpose". Carried in the tool
  -- input and discarded until now, so this store held 827 Agent rows that could not say what any
  -- of them ran. NULL on every other tool, which is the honest value: they have no agent type.
  subagent_type TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls (session_id);
CREATE INDEX IF NOT EXISTS tool_calls_target ON tool_calls (target);
CREATE INDEX IF NOT EXISTS tool_calls_name ON tool_calls (tool_name);
-- Lifecycle events from hooks/event-hook.mjs. Hooks are process level, so unlike the statusLine
-- they fire on every entrypoint, including the desktop host. probe separates test writes from
-- live ones at write time rather than by a later heuristic.
CREATE TABLE IF NOT EXISTS hook_events (
  captured_at TEXT, probe INTEGER, event TEXT, known INTEGER,
  session_id TEXT, transcript_path TEXT, cwd TEXT, permission_mode TEXT, tool_name TEXT,
  tool_input_bytes INTEGER, tool_response_bytes INTEGER, prompt_chars INTEGER,
  source TEXT, reason TEXT, agent_id TEXT, agent_type TEXT, truncated INTEGER, extra TEXT
);
-- Identity is an EXPRESSION index, not a primary key. SQLite treats NULLs in a PK as distinct,
-- so a row with no tool_name (SessionStart, UserPromptSubmit) would re-insert on every run and
-- the store would grow without bound while every count still looked plausible.
CREATE UNIQUE INDEX IF NOT EXISTS hook_events_key ON hook_events
  (captured_at, event, COALESCE(session_id,''), COALESCE(tool_name,''));
CREATE INDEX IF NOT EXISTS hook_events_session ON hook_events (session_id);
-- What the session is CALLED. The desktop sidebar shows a title for every session and the store
-- had none, so the dashboard could only identify a session by an encoded directory slug and a
-- date. The transcripts have carried the titles all along and this ingest ignored them.
--
-- Three sources, in descending trust: custom-title is what the user named it, ai-title is what
-- Claude named it, last-prompt is the opening request and is a description rather than a title.
-- Kept as separate rows rather than one resolved column, because which source a title came from is
-- worth showing and a later run must not silently promote a fallback over a real name.
--
-- These records carry NO timestamp, so ordering is file order: the last one read wins per kind.
-- Both key columns are NOT NULL, which is what makes this a safe PRIMARY KEY, unlike the nullable
-- one hook_events had to be rebuilt to remove.
CREATE TABLE IF NOT EXISTS session_titles (
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT,
  file_path TEXT,
  line_no INTEGER,
  PRIMARY KEY (session_id, kind)
);
-- A project the user deleted and asked to stop capturing. Keyed on cwd, not on the transcript
-- directory: the mapping is many-to-many, the 'subagents' slug alone covers 30 different working
-- directories, and excluding one of those by slug would silently stop capturing the other 29.
CREATE TABLE IF NOT EXISTS excluded_projects (
  cwd TEXT PRIMARY KEY, excluded_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS record_types (type TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS harvest_runs (
  ts TEXT, mode TEXT, files_seen INTEGER, files_read INTEGER, rewrites INTEGER,
  lines INTEGER, mb REAL, turns INTEGER, compactions INTEGER, unpaired INTEGER, ms INTEGER
);
`;

/**
 * Ingest the hook event log into the store.
 *
 * INSERT OR IGNORE on the natural key, so re-running is a no-op rather than a duplicate. Returns
 * counts rather than printing, so the caller decides what to say. A missing file is 0 rows and
 * not an error: the hook may simply never have fired yet, which is itself a finding.
 */
// The columns this ingest stores, in INSERT order. Exported because the hook decides what a row
// CONTAINS and this decides what survives, and the two used to be hand-written lists that
// disagreed: transcript_path and extra were written by every hook row and stored by none of them.
// `extra` is the field the hook adds specifically so that a key introduced by a future Claude Code
// build is not silently lost, so dropping it defeated the one mechanism built to prevent this.
// Which record type carries a title, and under which field. One map rather than a chain of ifs,
// so a new title source is added in one place and the self-test can enumerate them.
export const TITLE_FIELD = {
  'custom-title': 'customTitle',
  'ai-title': 'aiTitle',
  'last-prompt': 'lastPrompt',
};
export const TITLE_KIND = {
  'custom-title': 'custom',
  'ai-title': 'ai',
  'last-prompt': 'last-prompt',
};
// A last-prompt is a whole opening request and can be thousands of characters. It is a fallback
// label, not a transcript, and the store already holds the full text in `messages`.
const TITLE_MAX_CHARS = 200;

export const HOOK_EVENT_COLUMNS = [
  'captured_at', 'probe', 'event', 'known', 'session_id', 'transcript_path', 'cwd',
  'permission_mode', 'tool_name', 'tool_input_bytes', 'tool_response_bytes', 'prompt_chars',
  'source', 'reason', 'agent_id', 'agent_type', 'truncated', 'extra',
];

// Columns added to tables that already existed in the wild, by table. Every one is TEXT and
// nullable, so adding it cannot invalidate a row: an old row simply has nothing in it, which is
// exactly true. Anything needing a type or a default is a rebuild, not an entry here.
export const ADDED_COLUMNS = {
  hook_events: HOOK_EVENT_COLUMNS,
  turns: ['parent_uuid'],
  tool_calls: ['subagent_type'],
};
const BOOLEAN_EVENT_COLUMNS = new Set(['probe', 'known', 'truncated']);

export function ingestEvents(db, path) {
  if (!existsSync(path)) return { file: path, exists: false, seen: 0, stored: 0, genuine: 0, bad: 0 };
  const ins = db.prepare(`INSERT OR IGNORE INTO hook_events
    (${HOOK_EVENT_COLUMNS.join(',')})
    VALUES (${new Array(HOOK_EVENT_COLUMNS.length).fill('?').join(',')})`);
  let seen = 0, stored = 0, genuine = 0, bad = 0;
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    seen++;
    let d;
    try { d = JSON.parse(line); } catch { bad++; continue; }
    if (!d || typeof d !== 'object') { bad++; continue; }
    const r = ins.run(...HOOK_EVENT_COLUMNS.map((c) => {
      const v = d[c];
      if (BOOLEAN_EVENT_COLUMNS.has(c)) return v ? 1 : 0;
      return v ?? null;
    }));
    if (r.changes > 0) stored++;
    if (d.probe === false) genuine++;
  }
  return { file: path, exists: true, seen, stored, genuine, bad };
}

/**
 * Pull the surviving record uuids out of a compaction's preserved payload.
 * Returns [] for an old build that recorded none, which is a real answer, not a failure.
 */
export function extractSurvivors(preserved) {
  if (!preserved) return [];
  let d = preserved;
  if (typeof d === 'string') {
    try { d = JSON.parse(d); } catch { return []; }
  }
  const out = new Map();                       // dedupe: anchorUuid appears in both blocks
  const seg = d.segment || null;
  if (seg) {
    if (seg.headUuid) out.set(`segment_head:${seg.headUuid}`, { kind: 'segment_head', uuid: seg.headUuid });
    if (seg.anchorUuid) out.set(`segment_anchor:${seg.anchorUuid}`, { kind: 'segment_anchor', uuid: seg.anchorUuid });
    if (seg.tailUuid) out.set(`segment_tail:${seg.tailUuid}`, { kind: 'segment_tail', uuid: seg.tailUuid });
  }
  const msgs = d.messages || null;
  if (msgs) {
    // allUuids is the superset when both are present; union them so nothing is dropped.
    for (const u of [...(msgs.uuids || []), ...(msgs.allUuids || [])]) {
      if (u) out.set(`message:${u}`, { kind: 'message', uuid: u });
    }
  }
  return [...out.values()];
}

/**
 * Pull the readable text out of one transcript record.
 *
 * Three shapes occur and all three carry text a reader would want back:
 *   1. content is a plain string                        - most user messages
 *   2. content is an array of blocks with text/thinking - assistant messages
 *   3. content holds tool_result blocks, whose own content is a string OR an array of text blocks
 *
 * Handling only the first two loses every tool result, which is the bulk of a working session.
 * Exported so the self-test can drive all three shapes directly rather than through a file.
 */
export function messageText(d) {
  const c = d?.message?.content;
  if (c == null) return '';
  if (typeof c === 'string') return c;
  if (!Array.isArray(c)) return '';
  const parts = [];
  for (const b of c) {
    if (typeof b === 'string') { parts.push(b); continue; }
    if (!b || typeof b !== 'object') continue;
    if (typeof b.text === 'string') parts.push(b.text);
    else if (typeof b.thinking === 'string') parts.push(b.thinking);
    else if (b.type === 'tool_result') {
      const rc = b.content;
      if (typeof rc === 'string') parts.push(rc);
      else if (Array.isArray(rc)) {
        for (const x of rc) if (typeof x?.text === 'string') parts.push(x.text);
      }
    }
  }
  return parts.join('\n');
}

// The record's own label, kept distinct from role so a compact summary stays findable. It arrives
// as type:'user' with isCompactSummary:true, which would otherwise be indistinguishable from a
// prompt the user typed.
export function messageKind(d) {
  if (d?.isCompactSummary === true) return 'compact_summary';
  if (d?.type === 'system' && d?.subtype) return `system/${d.subtype}`;
  return typeof d?.type === 'string' ? d.type : 'unknown';
}

// Exported so tools/make_fixture.mjs can build a synthetic store through the SAME schema and
// pragmas the real one uses. A fixture with its own copy of the schema drifts from this file
// and then CI passes against a database shaped like nothing the app will ever open.
export function openDb(dbPath = DB_PATH) {
  mkdirSync(dirname(dbPath), { recursive: true });
  mkdirSync(RAW_DIR, { recursive: true });
  const db = new DatabaseSync(dbPath);
  // This store has THREE writers by design: a manual harvest, the SessionEnd and UserPromptSubmit
  // hooks that spawn their own, and the dashboard's refresh loop. SQLite's default busy timeout is
  // zero, so a concurrent writer does not wait, it fails immediately with SQLITE_BUSY. A --full
  // run died on "database is locked" after 1,500 files and 7.5 GB because a hook harvest started
  // while a prompt was submitted. Waiting is the correct behaviour for a capture tool: the loser
  // of a race should be slow, not absent.
  //
  // It is set BEFORE journal_mode, not after: switching journal mode takes an exclusive lock, so
  // on a store still in rollback-journal mode that very statement is the first thing that can lose
  // the race, and with the timeout set after it there is nothing yet telling it to wait.
  db.exec('PRAGMA busy_timeout = 15000');
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA synchronous = NORMAL');
  const existing = db.prepare(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='hook_events'").get();
  if (existing && String(existing.sql).includes('PRIMARY KEY (captured_at')) {
    // Derived table, rebuilt rather than migrated in place. Reported, never silent.
    db.exec('DROP TABLE hook_events');
    console.error('harvest: rebuilt hook_events, the old nullable primary key could not dedupe');
  }
  db.exec(SCHEMA);
  // A store written before transcript_path and extra were stored keeps its rows; the columns are
  // added in place. Non-destructive, unlike the primary-key rebuild above, because nothing about
  // the existing rows is wrong, they are merely missing two fields. Announced rather than silent.
  //
  // Table-driven, because hook_events was not the last table to gain a column. turns gained
  // parent_uuid and tool_calls gained subagent_type for the same reason: the field was always in
  // the transcripts and nothing read it. A store written before either keeps every row and gains
  // the columns empty, which is what `--backfill-agents` then fills.
  for (const [table, columns] of Object.entries(ADDED_COLUMNS)) {
    const have = new Set(db.prepare(`PRAGMA table_info(${table})`).all().map((r) => r.name));
    const missing = columns.filter((c) => !have.has(c));
    for (const c of missing) db.exec(`ALTER TABLE ${table} ADD COLUMN ${c} TEXT`);
    if (missing.length) console.error(`harvest: ${table} gained ${missing.join(', ')}`);
  }
  // AFTER the migration, not inside SCHEMA. An index over a migrated column cannot be created
  // alongside the CREATE TABLE that mentions it: on a store that already has the table, the
  // CREATE TABLE IF NOT EXISTS is a no-op and the index statement then names a column that does
  // not exist yet, so `db.exec(SCHEMA)` throws "no such column: parent_uuid" and the whole tool
  // refuses to open a perfectly good store. Found by running the backfill against a copy, which
  // is exactly the reason the copy comes first.
  db.exec('CREATE INDEX IF NOT EXISTS turns_parent ON turns(parent_uuid)');
  return db;
}

/**
 * Read ONLY the title records out of every transcript.
 *
 * A field added to the ingest is not retroactive: the incremental walk stores a byte offset per
 * file and never looks at what it already consumed, so titles that had always been on disk stayed
 * invisible until a re-read. `--full` would do it, but it re-reads 10 GB to collect a few kilobytes
 * and it lost a race with a hook harvest halfway through.
 *
 * This reads each file line by line and parses only lines that could be a title, which is a string
 * test before any JSON.parse. Cheap enough to re-run whenever a new title source is added.
 */
async function backfillTitles(dbPath = DB_PATH) {
  const db = openDb(dbPath);
  const put = db.prepare(`INSERT INTO session_titles (session_id,kind,title,file_path,line_no)
    VALUES (?,?,?,?,?) ON CONFLICT(session_id,kind) DO UPDATE SET
    title=excluded.title, file_path=excluded.file_path, line_no=excluded.line_no`);
  const files = listTranscripts(PROJECTS);
  let stored = 0, scanned = 0, skipped = 0;
  const types = Object.keys(TITLE_FIELD);
  for (const path of files) {
    scanned++;
    let text;
    try { text = readFileSync(path, 'utf8'); } catch { skipped++; continue; }
    let lineNo = 0;
    for (const line of text.split('\n')) {
      lineNo++;
      if (!line || !types.some((t) => line.includes(`"${t}"`))) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      const field = TITLE_FIELD[d?.type];
      if (!field || !d.sessionId) continue;
      const raw = d[field];
      if (typeof raw !== 'string' || !raw.trim()) continue;
      put.run(d.sessionId, TITLE_KIND[d.type], raw.trim().slice(0, TITLE_MAX_CHARS), path, lineNo);
      stored++;
    }
    if (scanned % 1000 === 0) console.error(`  ${scanned}/${files.length} files, ${stored} titles`);
  }
  const rows = db.prepare('SELECT kind, COUNT(*) n FROM session_titles GROUP BY kind').all();
  console.log(JSON.stringify({
    files_scanned: scanned, files_unreadable: skipped, title_records_stored: stored,
    by_kind: Object.fromEntries(rows.map((r) => [r.kind, r.n])),
    sessions_with_a_title: db.prepare('SELECT COUNT(DISTINCT session_id) n FROM session_titles').get().n,
  }, null, 2));
  db.close();
  return 0;
}

/**
 * Fill parent_uuid and subagent_type on rows harvested before those columns existed.
 *
 * The same problem backfillTitles solves and the same shape of solution, deliberately: the
 * incremental walk stores a byte offset per file and never revisits what it consumed, so a field
 * added to the ingest is not retroactive. `--full` would do it and re-reads 10 GB to collect a few
 * hundred kilobytes, and it has already lost a race with a hook harvest halfway through.
 *
 * UPDATE, never INSERT. This touches only rows the store already has, matched by primary key, so
 * it cannot create a turn, cannot resurrect a deleted one, and cannot change any column but the
 * two it exists to fill. That is what makes it safe to run against a live store, and it is checked
 * rather than asserted: run it against a copy and diff the row counts of every table.
 *
 * A record with no parentUuid is left alone rather than written as NULL. Rewriting it would be
 * harmless and would also make the "rows still empty" figure below unable to distinguish "not
 * backfilled yet" from "genuinely has no parent", which is the number that says whether a second
 * pass is worth running.
 */
export async function backfillAgents(dbPath = DB_PATH, { quiet = false } = {}) {
  const db = openDb(dbPath);
  const setParent = db.prepare('UPDATE turns SET parent_uuid = ? WHERE uuid = ? AND parent_uuid IS NULL');
  const setAgent = db.prepare('UPDATE tool_calls SET subagent_type = ? WHERE tool_use_id = ? AND subagent_type IS NULL');
  // A HIGH-WATER ROWID PER TABLE, taken before anything is written.
  //
  // The obvious guard, comparing COUNT(*) before and against after, is wrong on this store and
  // wrong for a reason that is not the backfill's fault. Three writers exist by design: this
  // process, the SessionEnd and UserPromptSubmit hooks, and the dashboard's refresh loop. On the
  // first live run turns and tool_calls each gained one row while the backfill was scanning, and
  // harvest_runs gained 33 and messages gained one, which the backfill does not touch at all.
  // The guard fired on a true statement about a cause it had nothing to do with.
  //
  // Counting only rows that already existed fixes it exactly. A concurrent INSERT takes a higher
  // rowid and is excluded; a DELETE or a REPLACE of an existing row moves the number, which is
  // the failure this is actually looking for.
  const highWater = {
    turns: db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM turns').get().n,
    tool_calls: db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM tool_calls').get().n,
  };
  const existing = (table) => db.prepare(
    `SELECT COUNT(*) n FROM ${table} WHERE rowid <= ?`).get(highWater[table]).n;
  const before = {
    turns: existing('turns'),
    tool_calls: existing('tool_calls'),
    turns_total: db.prepare('SELECT COUNT(*) n FROM turns').get().n,
    tool_calls_total: db.prepare('SELECT COUNT(*) n FROM tool_calls').get().n,
    parents: db.prepare('SELECT COUNT(*) n FROM turns WHERE parent_uuid IS NOT NULL').get().n,
    agents: db.prepare('SELECT COUNT(*) n FROM tool_calls WHERE subagent_type IS NOT NULL').get().n,
  };
  const files = listTranscripts(PROJECTS);
  let scanned = 0, skipped = 0, parents = 0, agents = 0;
  const perFile = [];
  for (const path of files) {
    scanned++;
    let text;
    try { text = readFileSync(path, 'utf8'); } catch { skipped++; continue; }
    let fileParents = 0, fileAgents = 0;
    for (const line of text.split('\n')) {
      // A string test before any JSON.parse, like backfillTitles. Parsing every line of 10 GB to
      // find two fields is the cost this whole approach exists to avoid.
      if (!line || (!line.includes('"parentUuid"') && !line.includes('"subagent_type"'))) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      if (typeof d?.uuid === 'string' && typeof d.parentUuid === 'string') {
        fileParents += setParent.run(d.parentUuid, d.uuid).changes;
      }
      const content = d?.message?.content;
      if (!Array.isArray(content)) continue;
      for (const b of content) {
        if (b?.type !== 'tool_use' || typeof b.id !== 'string') continue;
        const kind = b.input?.subagent_type;
        if (typeof kind === 'string' && kind) fileAgents += setAgent.run(kind, b.id).changes;
      }
    }
    parents += fileParents;
    agents += fileAgents;
    if (fileParents || fileAgents) perFile.push({ file: path, parents: fileParents, agents: fileAgents });
    if (!quiet && scanned % 1000 === 0) {
      console.error(`  ${scanned}/${files.length} files, ${parents} parents, ${agents} agent types`);
    }
  }
  const after = {
    turns: existing('turns'),
    tool_calls: existing('tool_calls'),
    turns_total: db.prepare('SELECT COUNT(*) n FROM turns').get().n,
    tool_calls_total: db.prepare('SELECT COUNT(*) n FROM tool_calls').get().n,
    parents: db.prepare('SELECT COUNT(*) n FROM turns WHERE parent_uuid IS NOT NULL').get().n,
    agents: db.prepare('SELECT COUNT(*) n FROM tool_calls WHERE subagent_type IS NOT NULL').get().n,
  };
  const report = {
    files_scanned: scanned,
    files_unreadable: skipped,
    files_that_contributed: perFile.length,
    // The rows that ALREADY EXISTED, before and after. This backfill is only allowed to fill
    // columns, so this pair must be identical; a difference means it created or destroyed a row.
    // Reported rather than assumed, because "it only runs UPDATE" is a claim about source code
    // and this is a measurement.
    rows_before: { turns: before.turns, tool_calls: before.tool_calls },
    rows_after: { turns: after.turns, tool_calls: after.tool_calls },
    rows_unchanged: before.turns === after.turns && before.tool_calls === after.tool_calls,
    // What the OTHER writers did meanwhile, reported separately so it can never be read as this
    // tool's doing. On a live store this is normally a small positive number and is not an error.
    written_by_other_writers_meanwhile: {
      turns: after.turns_total - before.turns_total,
      tool_calls: after.tool_calls_total - before.tool_calls_total,
    },
    parent_uuid: { before: before.parents, after: after.parents, filled: parents },
    subagent_type: { before: before.agents, after: after.agents, filled: agents },
    turns_still_without_a_parent: after.turns - after.parents,
    agent_calls_still_without_a_type: db.prepare(
      "SELECT COUNT(*) n FROM tool_calls WHERE tool_name = 'Agent' AND subagent_type IS NULL").get().n,
    by_subagent_type: db.prepare(`SELECT subagent_type, COUNT(*) n FROM tool_calls
                                   WHERE subagent_type IS NOT NULL GROUP BY 1 ORDER BY n DESC`).all(),
    busiest_files: perFile.sort((a, b) => (b.parents + b.agents) - (a.parents + a.agents)).slice(0, 5),
  };
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
  // A backfill that changed a row count did something it is not allowed to do, and the exit code
  // has to say so: this runs unattended from a script as often as it runs by hand.
  return report.rows_unchanged ? 0 : 1;
}

function listTranscripts(dir) {
  const out = [];
  const walk = (d) => {
    let entries;
    try { entries = readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile() && e.name.endsWith('.jsonl')) out.push(p);
    }
  };
  walk(dir);
  return out;
}

// The census must report the record's OWN type, not the first "type" string that happens to
// appear in the line. A regex cannot tell depth 1 from a nested content block, so every line is
// parsed. Measured cost of parsing everything rather than prefiltering: see harvest_runs.
const KNOWN_TYPES = new Set([
  'user', 'assistant', 'attachment', 'system', 'summary', 'ai-title',
  'bridge-session', 'queue-operation', 'last-prompt', 'custom-title', 'atis-latch', 'mode',
  'file-history-snapshot', 'x-anthropic-log',
]);

class Harvest {
  constructor(db) {
    this.db = db;
    this.stmt = {
      getFile: db.prepare('SELECT * FROM files WHERE path = ?'),
      putFile: db.prepare(`INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts)
        VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
        size=excluded.size, mtime_ms=excluded.mtime_ms, bytes_read=excluded.bytes_read,
        lines_read=excluded.lines_read, rewrites=excluded.rewrites, last_harvest_ts=excluded.last_harvest_ts`),
      putTitle: db.prepare(`INSERT INTO session_titles (session_id,kind,title,file_path,line_no)
        VALUES (?,?,?,?,?) ON CONFLICT(session_id,kind) DO UPDATE SET
        title=excluded.title, file_path=excluded.file_path, line_no=excluded.line_no`),
      putSession: db.prepare(`INSERT INTO sessions (session_id,project_slug,cwd,git_branch,version,entrypoint,first_ts,last_ts,transcript_path)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
        last_ts=MAX(COALESCE(sessions.last_ts,''), COALESCE(excluded.last_ts,'')),
        first_ts=MIN(COALESCE(NULLIF(sessions.first_ts,''),excluded.first_ts), COALESCE(excluded.first_ts,sessions.first_ts)),
        -- These arrive on SOME records and not others, so the first row for a session usually
        -- lacks them. Without a COALESCE here the first insert wins permanently and the column
        -- stays NULL forever: entrypoint, cwd and git_branch were null on 161 of 161 sessions
        -- while version, which had one, was null on none. The transcript carried entrypoint on
        -- 3,624 records the whole time.
        version=COALESCE(excluded.version, sessions.version),
        entrypoint=COALESCE(excluded.entrypoint, sessions.entrypoint),
        cwd=COALESCE(excluded.cwd, sessions.cwd),
        git_branch=COALESCE(excluded.git_branch, sessions.git_branch)`),
      putTurn: db.prepare(`INSERT OR REPLACE INTO turns
        (uuid,session_id,ts,model,request_id,input_tokens,cache_creation_input_tokens,cache_read_input_tokens,
         output_tokens,thinking_tokens,eph_1h,eph_5m,service_tier,total_resident,is_sidechain,file_path,line_no,
         parent_uuid)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`),
      putCompaction: db.prepare(`INSERT OR REPLACE INTO compactions
        (uuid,session_id,ts,trigger,version,entrypoint,pre_tokens,post_tokens,duration_ms,cumulative_dropped_tokens,
         messages_summarized,discovered_tools_json,preserved_json,summary_uuid,summary_chars,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`),
      paircompaction: db.prepare('UPDATE compactions SET summary_uuid = ?, summary_chars = ? WHERE uuid = ?'),
      putSurvivor: db.prepare('INSERT OR IGNORE INTO compaction_survivors (compaction_uuid,kind,uuid) VALUES (?,?,?)'),
      putMessage: db.prepare(`INSERT OR REPLACE INTO messages
        (uuid,session_id,ts,role,type,text,chars,model,request_id,is_sidechain,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`),
      putToolCall: db.prepare(`INSERT OR REPLACE INTO tool_calls
        (tool_use_id,session_id,turn_uuid,ts,tool_name,server_name,target,input_sha1,input_bytes,
         result_bytes,is_error,is_sidechain,file_path,line_no,subagent_type)
        VALUES (?,?,?,?,?,?,?,?,?,
         COALESCE((SELECT result_bytes FROM tool_calls WHERE tool_use_id = ?), NULL),
         COALESCE((SELECT is_error FROM tool_calls WHERE tool_use_id = ?), NULL), ?,?,?,?)`),
      // The result arrives on a LATER line than the use, so this fills the row in place. If the
      // two land in different harvest runs the update finds nothing and result_bytes stays NULL,
      // which reads as "not yet seen" rather than as zero bytes.
      setToolResult: db.prepare(
        'UPDATE tool_calls SET result_bytes = ?, is_error = ? WHERE tool_use_id = ?'),
      bumpAttachment: db.prepare(`INSERT INTO attachments (session_id,type,n) VALUES (?,?,1)
        ON CONFLICT(session_id,type) DO UPDATE SET n = n + 1`),
      bumpType: db.prepare(`INSERT INTO record_types (type,n) VALUES (?,1)
        ON CONFLICT(type) DO UPDATE SET n = n + 1`),
      putRun: db.prepare(`INSERT INTO harvest_runs (ts,mode,files_seen,files_read,rewrites,lines,mb,turns,compactions,unpaired,ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)`),
    };
    this.typeCounts = new Map();
    this.unknownSeen = new Set();
    this.stats = { filesSeen: 0, filesRead: 0, rewrites: 0, lines: 0, bytes: 0, turns: 0, compactions: 0, paired: 0, toolCalls: 0, toolResults: 0, messages: 0, messageChars: 0, excludedFiles: 0 };
    this.loadExclusions();
  }

  // Two lookups, both built once per run rather than queried per file.
  //
  // `excludedCwds` is the real rule. `excludedPaths` is only a shortcut for the case where the
  // rows are still here: excluding a project WITHOUT deleting it leaves sessions.cwd in place, so
  // the transcript can be recognised before it is opened. After a delete there is no session row
  // and no offset row, so the file is re-read from byte 0 and caught by the in-loop check instead.
  loadExclusions() {
    this.excludedCwds = new Set(
      this.db.prepare('SELECT cwd FROM excluded_projects').all().map((r) => r.cwd));
    this.excludedPaths = this.excludedCwds.size === 0 ? new Set() : new Set(
      this.db.prepare(`SELECT DISTINCT transcript_path FROM sessions
                        WHERE transcript_path IS NOT NULL
                          AND cwd IN (SELECT cwd FROM excluded_projects)`)
        .all().map((r) => r.transcript_path));
  }

  countType(t) { this.typeCounts.set(t, (this.typeCounts.get(t) || 0) + 1); }

  noteUnknown(type, line) {
    if (this.unknownSeen.has(type)) return;
    this.unknownSeen.add(type);
    // open() makes this directory, but the self-test runs against an in-memory database and never
    // goes through open(). On a fresh clone, where data/ is gitignored and therefore absent, that
    // turned the FIRST command a new user runs into a stack trace. The writer owns its directory.
    mkdirSync(dirname(UNKNOWN_LOG), { recursive: true });
    appendFileSync(UNKNOWN_LOG, JSON.stringify({
      first_seen: new Date().toISOString(), type, sample: line.slice(0, 4000),
    }) + '\n');
  }

  async file(path, full) {
    if (this.excludedPaths.has(path)) { this.stats.excludedFiles++; return; }
    const st = statSync(path);
    const prev = full ? null : this.stmt.getFile.get(path);
    let start = 0, lineNo = 0, rewritten = false;

    if (prev) {
      if (st.size < prev.bytes_read) { rewritten = true; this.stats.rewrites++; }
      else if (st.size === prev.bytes_read) {
        this.stmt.putFile.run(path, st.size, Math.round(st.mtimeMs), prev.bytes_read, prev.lines_read, prev.rewrites, new Date().toISOString());
        return;
      } else { start = prev.bytes_read; lineNo = prev.lines_read; }
    }

    this.stats.filesRead++;
    const projectSlug = path.split(/[\\/]/).slice(-2)[0];
    let pendingBoundary = null;
    let consumed = start;

    const rl = createInterface({ input: createReadStream(path, { start, highWaterMark: 1 << 20 }), crlfDelay: Infinity });
    for await (const line of rl) {
      lineNo++;
      consumed += Buffer.byteLength(line, 'utf8') + 1;
      this.stats.lines++; this.stats.bytes += line.length;
      if (!line.trim()) continue;

      let d;
      try { d = JSON.parse(line); } catch { this.countType('UNPARSEABLE'); this.noteUnknown('UNPARSEABLE', line); continue; }

      const type = typeof d.type === 'string' ? d.type : 'NO_TYPE_FIELD';
      this.countType(d.type === 'system' && d.subtype ? `system/${d.subtype}` : type);
      if (!KNOWN_TYPES.has(type) && type !== 'NO_TYPE_FIELD') this.noteUnknown(type, line);

      // The first record carrying a cwd decides whether this file is read at all. Abandoning here
      // costs one parsed record for an excluded transcript, and nothing is written: no rows, and
      // no `files` offset either. An offset would make the exclusion permanent, because lifting it
      // would resume from the end of a file whose earlier half was never stored.
      //
      // A transcript whose records never carry a cwd cannot be attributed to a project, so it is
      // never excluded. That is a real gap and it is deliberate: the alternative is guessing from
      // the slug, which is the many-to-many mistake this design exists to avoid.
      if (d.cwd && this.excludedCwds.has(d.cwd)) {
        rl.close();
        this.stats.excludedFiles++;
        this.stats.filesRead--;
        return;
      }

      if (d.sessionId) {
        this.stmt.putSession.run(d.sessionId, projectSlug, d.cwd ?? null, d.gitBranch ?? null,
          d.version ?? null, d.entrypoint ?? null, d.timestamp ?? null, d.timestamp ?? null, path);
      }

      this.scanBlocks(d, path, lineNo);
      this.captureMessage(d, path, lineNo);

      if (d.type === 'assistant' && d.message?.usage) {
        const u = d.message.usage;
        const inp = u.input_tokens ?? 0, cw = u.cache_creation_input_tokens ?? 0, cr = u.cache_read_input_tokens ?? 0;
        this.stmt.putTurn.run(
          d.uuid, d.sessionId ?? null, d.timestamp ?? null, d.message.model ?? null, d.requestId ?? null,
          inp, cw, cr, u.output_tokens ?? 0,
          u.output_tokens_details?.thinking_tokens ?? 0,
          u.cache_creation?.ephemeral_1h_input_tokens ?? 0,
          u.cache_creation?.ephemeral_5m_input_tokens ?? 0,
          u.service_tier ?? null, inp + cw + cr, d.isSidechain ? 1 : 0, path, lineNo,
          typeof d.parentUuid === 'string' ? d.parentUuid : null);
        this.stats.turns++;
      } else if (d.type === 'system' && d.subtype === 'compact_boundary') {
        const cm = d.compactMetadata ?? {};
        this.stmt.putCompaction.run(
          d.uuid, d.sessionId ?? null, d.timestamp ?? null, cm.trigger ?? null,
          d.version ?? null, d.entrypoint ?? null,
          cm.preTokens ?? null, cm.postTokens ?? null, cm.durationMs ?? null,
          cm.cumulativeDroppedTokens ?? null, cm.messagesSummarized ?? null,
          cm.preCompactDiscoveredTools ? JSON.stringify(cm.preCompactDiscoveredTools) : null,
          (cm.preservedSegment || cm.preservedMessages)
            ? JSON.stringify({ segment: cm.preservedSegment ?? null, messages: cm.preservedMessages ?? null }) : null,
          null, null, path, lineNo);
        for (const s of extractSurvivors({
          segment: cm.preservedSegment ?? null, messages: cm.preservedMessages ?? null,
        })) this.stmt.putSurvivor.run(d.uuid, s.kind, s.uuid);
        this.stats.compactions++;
        pendingBoundary = d.uuid;
      } else if (d.type === 'user' && d.isCompactSummary === true) {
        const c = d.message?.content;
        const chars = typeof c === 'string' ? c.length
          : Array.isArray(c) ? c.reduce((a, b) => a + (typeof b?.text === 'string' ? b.text.length : 0), 0) : 0;
        if (pendingBoundary) { this.stmt.paircompaction.run(d.uuid, chars, pendingBoundary); this.stats.paired++; pendingBoundary = null; }
      } else if (d.type === 'attachment') {
        this.stmt.bumpAttachment.run(d.sessionId ?? 'unknown', d.attachment?.type ?? 'unknown');
      } else if (TITLE_FIELD[type] && d.sessionId) {
        const raw = d[TITLE_FIELD[type]];
        // A blank title is not a title. Storing '' would outrank a real fallback at read time.
        if (typeof raw === 'string' && raw.trim()) {
          this.stmt.putTitle.run(d.sessionId, TITLE_KIND[type], raw.trim().slice(0, TITLE_MAX_CHARS), path, lineNo);
          this.stats.titles = (this.stats.titles ?? 0) + 1;
        }
      }
    }

    this.stmt.putFile.run(path, st.size, Math.round(st.mtimeMs), consumed, lineNo,
      (prev?.rewrites ?? 0) + (rewritten ? 1 : 0), new Date().toISOString());
  }

  // Store the record's text. Called for EVERY record, like scanBlocks and for the same reason:
  // text sits on assistant messages, on user messages, and on compact summaries alike, so folding
  // this into the type chain would silently drop whichever branch lost the else-if race.
  //
  // Keyed by uuid, so re-harvesting a file replaces rows instead of duplicating them.
  captureMessage(d, path, lineNo) {
    if (typeof d?.uuid !== 'string') return;   // no stable identity: would duplicate on every run
    const text = messageText(d);
    if (!text) return;                         // nothing readable, do not store an empty row
    this.stmt.putMessage.run(
      d.uuid, d.sessionId ?? null, d.timestamp ?? null,
      typeof d.type === 'string' ? d.type : null, messageKind(d),
      text, text.length, d.message?.model ?? null, d.requestId ?? null,
      d.isSidechain ? 1 : 0, path, lineNo);
    this.stats.messages++;
    this.stats.messageChars += text.length;
  }

  // Walk a record's content blocks for tool_use and tool_result. Called for EVERY record rather
  // than inside the type chain, because a tool_use sits on an assistant message that also carries
  // usage, and a tool_result sits on a user message that is not a compact summary; folding either
  // into an else-if would silently drop half the calls.
  scanBlocks(d, path, lineNo) {
    const content = d.message?.content;
    if (!Array.isArray(content)) return;
    for (const b of content) {
      if (!b || typeof b !== 'object') continue;

      if (b.type === 'tool_use' && typeof b.id === 'string') {
        const name = typeof b.name === 'string' ? b.name : 'UNKNOWN';
        // MCP tools are named mcp__<server>__<tool>. Everything else is built in.
        const server = name.startsWith('mcp__') ? (name.split('__')[1] || null) : null;
        const input = b.input ?? {};
        const target = input.file_path ?? input.notebook_path ?? input.url ?? input.path ?? null;
        const raw = JSON.stringify(input);
        this.stmt.putToolCall.run(
          b.id, d.sessionId ?? null, d.uuid ?? null, d.timestamp ?? null, name, server,
          typeof target === 'string' ? target : null,
          createHash('sha1').update(raw).digest('hex'), Buffer.byteLength(raw, 'utf8'),
          b.id, b.id,
          d.isSidechain ? 1 : 0, path, lineNo,
          // Only where the tool actually asked for one. Storing the empty string or "none" on
          // every other tool would make `subagent_type IS NOT NULL` stop meaning "this spawned an
          // agent", which is the one question the column exists to answer.
          typeof input.subagent_type === 'string' ? input.subagent_type : null);
        this.stats.toolCalls++;
      } else if (b.type === 'tool_result' && typeof b.tool_use_id === 'string') {
        const c = b.content;
        const bytes = typeof c === 'string' ? Buffer.byteLength(c, 'utf8')
          : Array.isArray(c) ? c.reduce((a, x) => a + (typeof x?.text === 'string'
            ? Buffer.byteLength(x.text, 'utf8') : 0), 0)
          : c == null ? 0 : Buffer.byteLength(JSON.stringify(c), 'utf8');
        this.stmt.setToolResult.run(bytes, b.is_error ? 1 : 0, b.tool_use_id);
        this.stats.toolResults++;
      }
    }
  }

  flushTypes() {
    for (const [t, n] of this.typeCounts) {
      for (let i = 0; i < n; i++) this.stmt.bumpType.run(t);
    }
  }
}

// Batched type flush is O(n) statement calls; do it as a single upsert per type instead.
function flushTypesFast(db, typeCounts) {
  const up = db.prepare(`INSERT INTO record_types (type,n) VALUES (?,?)
    ON CONFLICT(type) DO UPDATE SET n = n + excluded.n`);
  for (const [t, n] of typeCounts) up.run(t, n);
}

async function run({ full }) {
  const t0 = Date.now();
  const db = openDb();
  const h = new Harvest(db);
  const files = listTranscripts(PROJECTS);
  h.stats.filesSeen = files.length;
  process.stderr.write(`harvest: ${files.length} transcripts under ${PROJECTS}\n`);

  db.exec('BEGIN');
  let sinceCommit = 0;
  for (let i = 0; i < files.length; i++) {
    await h.file(files[i], full);
    if (++sinceCommit >= 200) { db.exec('COMMIT'); db.exec('BEGIN'); sinceCommit = 0; }
    if ((i + 1) % 500 === 0) {
      process.stderr.write(`  ${i + 1}/${files.length} files, ${h.stats.turns} turns, ${h.stats.compactions} compactions, ${(h.stats.bytes / 1048576).toFixed(0)} MB\n`);
    }
  }
  flushTypesFast(db, h.typeCounts);
  db.exec('COMMIT');

  const unpaired = h.stats.compactions - h.stats.paired;
  const ms = Date.now() - t0;
  ingestEvents(db, join(RAW_DIR, 'events.ndjson'));   // called for its writes
  db.prepare(`INSERT INTO harvest_runs (ts,mode,files_seen,files_read,rewrites,lines,mb,turns,compactions,unpaired,ms)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(new Date().toISOString(), full ? 'full' : 'incremental',
    h.stats.filesSeen, h.stats.filesRead, h.stats.rewrites, h.stats.lines,
    +(h.stats.bytes / 1048576).toFixed(1), h.stats.turns, h.stats.compactions, unpaired, ms);

  // Records SEEN and rows STORED are different numbers: the same record is written into more
  // than one transcript file when a session is resumed or forked, and the uuid primary key
  // collapses those copies. Reporting only one of the two invites exactly the confusion it
  // caused during the first run of this tool. Report both, and the gap between them.
  const rowTurns = db.prepare('SELECT COUNT(*) n FROM turns').get().n;
  const rowComp = db.prepare('SELECT COUNT(*) n FROM compactions').get().n;
  const out = {
    mode: full ? 'full' : 'incremental',
    files_seen: h.stats.filesSeen, files_read: h.stats.filesRead, rewritten_files: h.stats.rewrites,
    lines: h.stats.lines, mb: +(h.stats.bytes / 1048576).toFixed(1),
    turn_records_seen: h.stats.turns, turn_rows_stored: rowTurns,
    // Only comparable on a full run: records_seen is per-run, rows_stored is cumulative.
    duplicate_turn_records: full ? h.stats.turns - rowTurns : null,
    compaction_records_seen: h.stats.compactions, compaction_rows_stored: rowComp,
    message_rows_stored: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    message_text_mb: +((db.prepare('SELECT COALESCE(SUM(chars),0) c FROM messages').get().c) / 1048576).toFixed(1),
    unpaired_boundaries: unpaired,
    unknown_record_types: [...h.unknownSeen],
    seconds: +(ms / 1000).toFixed(1),
  };
  console.log(JSON.stringify(out, null, 2));
  if (h.stats.rewrites > 0) process.stderr.write(`WARNING: ${h.stats.rewrites} transcript(s) shrank since last harvest (local GC?). Re-read in full.\n`);
  db.close();
  return 0;
}

/**
 * Populate compaction_survivors from preserved_json already in the store, with no transcript
 * re-read. Reports how many survivor uuids match a turn we hold and how many do not: an
 * unmatched uuid is EXPECTED (the store keeps assistant turns only, while survivors include
 * user and attachment records), so it is counted, never dropped in silence.
 */
export function backfillSurvivors(dbPath = DB_PATH, { quiet = false } = {}) {
  // Refuse to create a store. Pointing --db at a typo would otherwise make an empty database
  // and report zeros, which reads exactly like "there was nothing to backfill".
  if (!existsSync(dbPath)) {
    console.error(`no store at ${dbPath}. Run harvest first, or check the --db path.`);
    return null;
  }
  const db = openDb(dbPath);
  const rows = db.prepare(
    'SELECT uuid, preserved_json FROM compactions WHERE preserved_json IS NOT NULL').all();
  const ins = db.prepare(
    'INSERT OR IGNORE INTO compaction_survivors (compaction_uuid,kind,uuid) VALUES (?,?,?)');
  db.exec('BEGIN');
  let inserted = 0, withNone = 0;
  for (const r of rows) {
    const surv = extractSurvivors(r.preserved_json);
    if (!surv.length) { withNone++; continue; }
    // Count what the database actually accepted. INSERT OR IGNORE silently discards a row that
    // is already there, so counting attempts would report 5 insertions on a re-run that inserted
    // nothing, the same "seen versus stored" confusion that already bit the turn counter.
    for (const s of surv) { if (ins.run(r.uuid, s.kind, s.uuid).changes) inserted++; }
  }
  db.exec('COMMIT');

  const total = db.prepare('SELECT COUNT(*) n FROM compaction_survivors').get().n;
  const matched = db.prepare(
    'SELECT COUNT(*) n FROM compaction_survivors s JOIN turns t ON t.uuid = s.uuid').get().n;
  const byKind = db.prepare(
    'SELECT kind, COUNT(*) n FROM compaction_survivors GROUP BY kind ORDER BY n DESC').all();
  const withSurv = db.prepare(
    'SELECT COUNT(DISTINCT compaction_uuid) n FROM compaction_survivors').get().n;
  const allComp = db.prepare('SELECT COUNT(*) n FROM compactions').get().n;
  db.close();

  const out = {
    db: dbPath,
    compactions_total: allComp,
    compactions_with_preserved_json: rows.length,
    compactions_with_survivors: withSurv,
    compactions_whose_payload_named_none: withNone,
    survivor_rows: total, inserted_this_run: inserted,
    matched_to_a_stored_turn: matched,
    unmatched: total - matched,
    unmatched_note: 'expected: the store holds assistant turns only, survivors include user and attachment records',
    by_kind: byKind,
  };
  if (!quiet) console.log(JSON.stringify(out, null, 2));
  return out;
}

function stats() {
  if (!existsSync(DB_PATH)) { console.error('no store yet at ' + DB_PATH); return 1; }
  // openDb rather than a second connection of its own. This opened the real store read-write and
  // ran DDL with NO busy timeout, while every other on-disk connection in the repo had one, so
  // --stats was the one command that still died outright against a concurrent hook harvest. It was
  // missed because the earlier sweep patched one call site per FILE instead of every call site.
  //
  // openDb applies the schema too, which is what this needed anyway: --stats used to open the
  // store raw, so a store written by an older build was missing the api_calls view and --stats
  // died with "no such table" rather than creating it.
  const db = openDb(DB_PATH);
  const q = (s) => db.prepare(s).all();
  const out = {
    db: DB_PATH,
    files: q('SELECT COUNT(*) n, SUM(bytes_read) bytes FROM files')[0],
    sessions: q('SELECT COUNT(*) n FROM sessions')[0].n,
    // Counted over api_calls, not turns: a SUM across turns double-counts every streamed message
    // once per content block. transcript_rows keeps the raw row count visible beside it so the
    // ratio is never a surprise.
    turns: q('SELECT COUNT(*) n, MAX(total_resident) max_resident FROM turns')[0],
    api_calls: q(`SELECT COUNT(*) n, SUM(output_tokens) out_tok,
      SUM(input_tokens + cache_creation_input_tokens + cache_read_input_tokens) in_tok,
      SUM(transcript_rows) rows_behind_them FROM api_calls`)[0],
    compactions: q('SELECT COUNT(*) n, SUM(summary_uuid IS NULL) unpaired, SUM(cumulative_dropped_tokens IS NOT NULL) with_dropped FROM compactions')[0],
    by_trigger: q('SELECT trigger, COUNT(*) n, AVG(pre_tokens) avg_pre, AVG(post_tokens) avg_post FROM compactions GROUP BY trigger'),
    record_types: q('SELECT type, n FROM record_types ORDER BY n DESC LIMIT 20'),
    top_attachments: q('SELECT type, SUM(n) n FROM attachments GROUP BY type ORDER BY n DESC LIMIT 12'),
    runs: q('SELECT ts, mode, files_read, turns, compactions, ms FROM harvest_runs ORDER BY ts DESC LIMIT 5'),
  };
  console.log(JSON.stringify(out, null, 2));
  db.close();
  return 0;
}

// Self-test: build a synthetic transcript containing one of everything, harvest it into a
// throwaway DB, and assert each record type was picked up. Then corrupt the detector's input
// and assert the assertions FAIL, so a green run means something.
async function selfTest() {
  const tmp = join(ROOT, 'tmp', 'selftest');
  rmSync(tmp, { recursive: true, force: true });
  mkdirSync(join(tmp, 'projects', 'P--fake'), { recursive: true });
  const tf = join(tmp, 'projects', 'P--fake', 'sess.jsonl');
  const rows = [
    { type: 'assistant', uuid: 'u1', sessionId: 's1', timestamp: '2026-08-20T00:00:00Z', requestId: 'r1', isSidechain: false, cwd: 'C:\\x', version: '2.1.229', message: { model: 'claude-opus-5', usage: { input_tokens: 3, cache_creation_input_tokens: 100, cache_read_input_tokens: 900, output_tokens: 50, output_tokens_details: { thinking_tokens: 7 }, cache_creation: { ephemeral_1h_input_tokens: 100, ephemeral_5m_input_tokens: 0 }, service_tier: 'standard' } } },
    { type: 'system', subtype: 'compact_boundary', uuid: 'c1', sessionId: 's1', timestamp: '2026-08-20T00:01:00Z', content: 'Conversation compacted', compactMetadata: { trigger: 'auto', preTokens: 970058, postTokens: 30226, durationMs: 147304, cumulativeDroppedTokens: 939832, preCompactDiscoveredTools: ['WebFetch'] } },
    { type: 'user', uuid: 'sum1', sessionId: 's1', timestamp: '2026-08-20T00:01:01Z', isCompactSummary: true, message: { role: 'user', content: 'This session is being continued from a previous conversation.' } },
    { type: 'attachment', sessionId: 's1', timestamp: '2026-08-20T00:02:00Z', attachment: { type: 'hook_success' } },
    { type: 'zzz-brand-new-type', sessionId: 's1', timestamp: '2026-08-20T00:03:00Z', usage: 'force-parse' },
    // Tool calls. Two reads of the SAME file (the duplicate-read case), one MCP call, and one
    // result that arrives on a later line than its use.
    { type: 'assistant', uuid: 'u2', sessionId: 's1', timestamp: '2026-08-20T00:04:00Z', isSidechain: false, message: { model: 'claude-opus-5', content: [{ type: 'tool_use', id: 'tu1', name: 'Read', input: { file_path: 'C:/x/a.md' } }] } },
    { type: 'user', uuid: 'ur1', sessionId: 's1', timestamp: '2026-08-20T00:04:01Z', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tu1', content: 'hello world' }] } },
    { type: 'assistant', uuid: 'u3', sessionId: 's1', timestamp: '2026-08-20T00:05:00Z', isSidechain: false, message: { model: 'claude-opus-5', content: [{ type: 'tool_use', id: 'tu2', name: 'Read', input: { file_path: 'C:/x/a.md' } }] } },
    { type: 'assistant', uuid: 'u4', sessionId: 's1', timestamp: '2026-08-20T00:06:00Z', isSidechain: true, message: { model: 'claude-opus-5', content: [{ type: 'tool_use', id: 'tu3', name: 'mcp__azure__storage', input: { query: 'x' } }] } },
    { type: 'user', uuid: 'ur2', sessionId: 's1', timestamp: '2026-08-20T00:06:01Z', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tu3', content: 'boom', is_error: true }] } },
    // Text shapes the rows above do not reach: thinking + text blocks on one assistant message,
    // and a tool_result whose content is an ARRAY of text blocks rather than a bare string. Both
    // were silently dropped by an earlier draft of messageText that only handled b.text.
    // entrypoint and gitBranch arrive HERE, not on the session's first record (u1 above has
    // neither). That is the real shape of a transcript, and the shape that used to leave both
    // columns null forever because the upsert only coalesced version.
    { type: 'assistant', uuid: 'm1', sessionId: 's1', timestamp: '2026-08-20T00:07:00Z', entrypoint: 'claude-desktop', gitBranch: 'main', message: { model: 'claude-opus-5', content: [{ type: 'thinking', thinking: 'THINKTEXT' }, { type: 'text', text: 'SPOKENTEXT' }] } },
    { type: 'user', uuid: 'm2', sessionId: 's1', timestamp: '2026-08-20T00:07:01Z', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'tu9', content: [{ type: 'text', text: 'ARRAYRESULT' }] }] } },
    // Subagent identity. An Agent call that names its type, a turn that hangs off it by
    // parentUuid, and an ordinary tool call on the same file that must NOT acquire a type: the
    // column has to distinguish "spawned an agent" from "did not", and a default of '' or 'none'
    // would make `subagent_type IS NOT NULL` true for every row in the table.
    { type: 'assistant', uuid: 'ag1', sessionId: 's1', timestamp: '2026-08-20T00:08:00Z', message: { model: 'claude-opus-5', content: [{ type: 'tool_use', id: 'tua', name: 'Agent', input: { subagent_type: 'general-purpose', prompt: 'go' } }] } },
    { type: 'assistant', uuid: 'ag2', parentUuid: 'ag1', sessionId: 's1', timestamp: '2026-08-20T00:08:30Z', requestId: 'r-agent', isSidechain: true, message: { model: 'claude-opus-5', usage: { input_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 5, output_tokens: 2 } } },
  ];
  writeFileSync(tf, rows.map((r) => JSON.stringify(r)).join('\n') + '\n');

  const db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
  const h = new Harvest(db);
  await h.file(tf, true);
  flushTypesFast(db, h.typeCounts);

  const checks = [];
  const turn = db.prepare('SELECT * FROM turns WHERE uuid = ?').get('u1');
  checks.push(['turn captured', !!turn]);
  checks.push(['total_resident = input + cache_write + cache_read', turn?.total_resident === 1003]);
  checks.push(['thinking tokens captured', turn?.thinking_tokens === 7]);
  const comp = db.prepare('SELECT * FROM compactions WHERE uuid = ?').get('c1');
  checks.push(['compaction captured', !!comp]);
  checks.push(['pre_tokens exact', comp?.pre_tokens === 970058]);
  checks.push(['summary paired to boundary', comp?.summary_uuid === 'sum1']);
  checks.push(['dropped tokens captured', comp?.cumulative_dropped_tokens === 939832]);
  const att = db.prepare('SELECT n FROM attachments WHERE session_id = ? AND type = ?').get('s1', 'hook_success');
  checks.push(['attachment counted', att?.n === 1]);
  checks.push(['unknown record type flagged', h.unknownSeen.has('zzz-brand-new-type')]);

  // sessions: fields that arrive on a LATER record than the first must still land. The upsert
  // coalesced version and nothing else, so entrypoint, cwd and git_branch read null on 161 of 161
  // real sessions while the transcript carried entrypoint on 3,624 records.
  {
    const s = db.prepare('SELECT * FROM sessions WHERE session_id = ?').get('s1');
    checks.push(['sessions: entrypoint from a later record is kept', s?.entrypoint === 'claude-desktop']);
    checks.push(['sessions: git_branch from a later record is kept', s?.git_branch === 'main']);
    checks.push(['sessions: cwd from the first record survives later nulls', s?.cwd === 'C:\\x']);
    checks.push(['sessions: version still resolves', s?.version === '2.1.229']);
  }

  // messages: the table that holds content rather than measurements. Each shape is checked on its
  // own, because a extractor that handles two of the three still looks healthy in aggregate.
  {
    const msg = (u) => db.prepare('SELECT * FROM messages WHERE uuid = ?').get(u);
    checks.push(['messages: plain string content stored',
      msg('sum1')?.text === 'This session is being continued from a previous conversation.']);
    checks.push(['messages: thinking AND text blocks both stored',
      msg('m1')?.text === 'THINKTEXT\nSPOKENTEXT']);
    checks.push(['messages: tool_result string content stored', msg('ur1')?.text === 'hello world']);
    checks.push(['messages: tool_result ARRAY content stored', msg('m2')?.text === 'ARRAYRESULT']);
    checks.push(['messages: compact summary is typed, not just a user turn',
      msg('sum1')?.type === 'compact_summary' && msg('sum1')?.role === 'user']);
    checks.push(['messages: chars matches the stored text length',
      msg('m1')?.chars === 'THINKTEXT\nSPOKENTEXT'.length]);
    checks.push(['messages: a record with no readable text stores no row', msg('u1') === undefined]);
    checks.push(['messages: sidechain flag survives', msg('ur2')?.is_sidechain === 0]);

    // Idempotency. A second harvest of the same file must replace rows, not duplicate them: the
    // table is keyed by uuid precisely so a re-run is free.
    const before = db.prepare('SELECT COUNT(*) n, SUM(chars) c FROM messages').get();
    const h2 = new Harvest(db);
    await h2.file(tf, true);
    const after = db.prepare('SELECT COUNT(*) n, SUM(chars) c FROM messages').get();
    checks.push(['messages: re-harvest is idempotent, not duplicating',
      before.n === after.n && before.c === after.c && after.n > 0]);
  }

  // Text capture is UNCONDITIONAL. There used to be a C4X_NO_TEXT opt-out; this asserts it is
  // really gone rather than merely undocumented, by setting it and proving text is captured anyway.
  // A leftover read of that variable anywhere in the capture path fails this check.
  {
    const t = new DatabaseSync(':memory:');
    t.exec(SCHEMA);
    const prev = process.env.C4X_NO_TEXT;
    process.env.C4X_NO_TEXT = '1';
    try {
      const hq = new Harvest(t);
      await hq.file(tf, true);
    } finally {
      // Nothing else runs between the await and this restore: the self-test is sequential and
      // is the only writer of this variable. The rule fires on any member assignment after an
      // await, which cannot be avoided here without moving the env handling out of the block
      // that owns it.
      /* eslint-disable require-atomic-updates */
      if (prev === undefined) delete process.env.C4X_NO_TEXT;
      else process.env.C4X_NO_TEXT = prev;
      /* eslint-enable require-atomic-updates */
    }
    const n = t.prepare('SELECT COUNT(*) n FROM messages').get().n;
    checks.push(['the removed C4X_NO_TEXT opt-out no longer suppresses capture (gate can fail)', n > 0]);
  }

  // The exclusion, and the defect it exists to prevent.
  //
  // Both transcripts live in the SAME slug directory and have DIFFERENT working directories, which
  // is the real shape of this store: the 'subagents' slug alone covers 30 of them. Excluding by
  // slug would take out both, so the check is that the OTHER one still lands.
  //
  // The second half is the must-fail control. Without it this only proves harvest did nothing,
  // which is indistinguishable from a harvester that is simply broken.
  {
    const shared = join(tmp, 'projects', 'shared-slug');
    mkdirSync(shared, { recursive: true });
    const gone = join(shared, 'gone.jsonl');
    const stays = join(shared, 'stays.jsonl');
    const rec = (sid, cwd, uuid) => JSON.stringify({
      type: 'assistant', uuid, sessionId: sid, timestamp: '2026-08-20T00:00:00Z',
      requestId: `r-${uuid}`, isSidechain: false, cwd,
      message: { model: 'claude-opus-5', usage: { input_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 1 } },
    }) + '\n';
    writeFileSync(gone, rec('x-gone', 'P:\\Excluded', 'g1'));
    writeFileSync(stays, rec('x-stays', 'P:\\Kept', 'k1'));

    const t = new DatabaseSync(':memory:');
    t.exec(SCHEMA);
    t.prepare('INSERT INTO excluded_projects (cwd,excluded_at,note) VALUES (?,?,?)')
      .run('P:\\Excluded', '2026-08-31T00:00:00Z', 'self-test');

    const hx = new Harvest(t);
    await hx.file(gone, true);
    await hx.file(stays, true);
    const seen = (cwd) => t.prepare('SELECT COUNT(*) n FROM sessions WHERE cwd = ?').get(cwd).n;
    checks.push(['excluded cwd is not captured', seen('P:\\Excluded') === 0]);
    checks.push(['a project sharing the slug IS still captured', seen('P:\\Kept') === 1]);
    checks.push(['no turns stored for the excluded transcript',
      t.prepare('SELECT COUNT(*) n FROM turns WHERE uuid = ?').get('g1').n === 0]);
    // No offset row, or lifting the exclusion would resume past everything it skipped.
    checks.push(['no harvest offset written for the excluded transcript',
      t.prepare('SELECT COUNT(*) n FROM files WHERE path = ?').get(gone).n === 0]);
    checks.push(['the excluded file is counted, not silently ignored', hx.stats.excludedFiles === 1]);

    // Must-fail control: lift it and the same file must now land.
    t.prepare('DELETE FROM excluded_projects').run();
    const hy = new Harvest(t);
    await hy.file(gone, true);
    checks.push(['lifting the exclusion lets it back in (gate can fail)', seen('P:\\Excluded') === 1]);
  }

  // api_calls: one row per API call, not per transcript entry.
  {
    const t = new DatabaseSync(':memory:');
    t.exec(SCHEMA);
    const ins = t.prepare(`INSERT INTO turns (uuid,session_id,ts,model,request_id,input_tokens,
      cache_creation_input_tokens,cache_read_input_tokens,output_tokens,total_resident,is_sidechain)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`);
    // One streamed message: three rows, same input side, output ACCUMULATING 8 -> 8 -> 232.
    ins.run('a', 's1', '2026-08-01T00:00:00Z', 'm', 'req-1', 3, 26913, 0, 8, 26916, 0);
    ins.run('b', 's1', '2026-08-01T00:00:01Z', 'm', 'req-1', 3, 26913, 0, 8, 26916, 0);
    ins.run('c', 's1', '2026-08-01T00:00:02Z', 'm', 'req-1', 3, 26913, 0, 232, 26916, 0);
    ins.run('d', 's1', '2026-08-01T00:01:00Z', 'm', 'req-2', 1, 100, 5, 20, 106, 0);
    ins.run('e', 's1', '2026-08-01T00:02:00Z', 'm', null, 9, 9, 9, 9, 27, 0);

    const calls = t.prepare('SELECT * FROM api_calls ORDER BY request_id').all();
    checks.push(['api_calls collapses a streamed message to one row', calls.length === 2, String(calls.length)]);
    checks.push(['it keeps the row count behind each call', calls[0].transcript_rows === 3, String(calls[0]?.transcript_rows)]);
    checks.push(['output takes the FINAL streamed value, not the first',
      calls[0].output_tokens === 232, String(calls[0]?.output_tokens)]);
    checks.push(['input side is taken once, not summed',
      calls[0].cache_creation_input_tokens === 26913, String(calls[0]?.cache_creation_input_tokens)]);
    const sumTurns = t.prepare('SELECT SUM(cache_creation_input_tokens) s FROM turns').get().s;
    const sumCalls = t.prepare('SELECT SUM(cache_creation_input_tokens) s FROM api_calls').get().s;
    // turns: 26913 x3 (one streamed message) + 100 + 9 (the null-request row) = 80848.
    // api_calls: 26913 + 100 = 27013, and the null-request row is excluded by design.
    checks.push(['summing turns OVERCOUNTS, which is the defect this exists for (gate can fail)',
      sumTurns === 80848 && sumCalls === 27013, `${sumTurns} vs ${sumCalls}`]);
    checks.push(['a row with a NULL request_id is excluded rather than silently merged',
      !calls.some((c) => c.request_id === null)]);
    checks.push(['every api_calls row maps to a real request_id',
      calls.map((c) => c.request_id).join(',') === 'req-1,req-2']);
  }

  // Hook-event ingestion.
  const evFile = join(tmp, 'events.ndjson');
  writeFileSync(evFile, [
    JSON.stringify({ captured_at: '2026-08-22T00:00:00Z', probe: false, event: 'PostToolUse', known: true, session_id: 'e1', tool_name: 'Bash', tool_response_bytes: 12 }),
    JSON.stringify({ captured_at: '2026-08-22T00:00:01Z', probe: true, event: 'SessionStart', known: true, session_id: 'e1', tool_name: null }),
    '{ not json',
  ].join('\n') + '\n');
  const ev1 = ingestEvents(db, evFile);
  checks.push(['events ingested', ev1.stored === 2, JSON.stringify(ev1)]);
  checks.push(['genuine rows counted apart from probes', ev1.genuine === 1, String(ev1.genuine)]);
  checks.push(['an unparseable event line is counted, not fatal', ev1.bad === 1, String(ev1.bad)]);
  const ev2 = ingestEvents(db, evFile);
  checks.push(['re-ingesting the same file stores nothing new (gate can fail)', ev2.stored === 0, String(ev2.stored)]);
  checks.push(['a missing event log is zero rows, not an error',
    ingestEvents(db, join(tmp, 'no-such-events.ndjson')).exists === false]);
  checks.push(['probe flag survives into the table',
    db.prepare('SELECT COUNT(*) n FROM hook_events WHERE probe = 1').get().n === 1]);

  // The two fields that were written by every hook row and stored by none of them.
  writeFileSync(evFile, JSON.stringify({
    captured_at: '2026-08-22T00:00:02Z', probe: false, event: 'PostToolUse', known: true,
    session_id: 'e2', tool_name: 'Read', transcript_path: 'P:/x/t.jsonl',
    extra: '{"future_field":1}',
  }) + '\n');
  ingestEvents(db, evFile);
  const kept = db.prepare('SELECT transcript_path, extra FROM hook_events WHERE session_id = ?').get('e2');
  checks.push(['transcript_path survives the ingest', kept?.transcript_path === 'P:/x/t.jsonl', JSON.stringify(kept)]);
  checks.push(['extra survives the ingest, which is the field built to preserve future keys',
    kept?.extra === '{"future_field":1}', JSON.stringify(kept)]);

  // Session titles. The field names are NOT guessable: custom-title carries `customTitle`,
  // ai-title carries `aiTitle`, last-prompt carries `lastPrompt`. An earlier version of this
  // ingest assumed ai-title used `title` and would have stored nothing for all 681 of them,
  // silently, which is why each field name is asserted against a record shaped like the real one.
  {
    const titlesFile = join(tmp, 'projects', 'P--titles', 'sess.jsonl');
    mkdirSync(dirname(titlesFile), { recursive: true });
    writeFileSync(titlesFile, [
      JSON.stringify({ type: 'last-prompt', lastPrompt: 'do the thing please', sessionId: 'T1' }),
      JSON.stringify({ type: 'ai-title', aiTitle: 'Doing the thing', sessionId: 'T1' }),
      JSON.stringify({ type: 'custom-title', customTitle: 'My name for it', sessionId: 'T1' }),
      JSON.stringify({ type: 'custom-title', customTitle: '   ', sessionId: 'T2' }),
      JSON.stringify({ type: 'custom-title', customTitle: 'no session id here' }),
    ].join('\n') + '\n');
    const th = new Harvest(db);
    await th.file(titlesFile, true);
    const got = Object.fromEntries(
      db.prepare('SELECT kind, title FROM session_titles WHERE session_id = ?').all('T1')
        .map((r) => [r.kind, r.title]));
    checks.push(['a custom title is stored under its own kind', got.custom === 'My name for it', JSON.stringify(got)]);
    checks.push(['an ai title is stored, proving the aiTitle field name (gate can fail)', got.ai === 'Doing the thing', JSON.stringify(got)]);
    checks.push(['a last prompt is kept as the weakest fallback', got['last-prompt'] === 'do the thing please', JSON.stringify(got)]);
    checks.push(['all three sources coexist rather than overwriting each other', Object.keys(got).length === 3, JSON.stringify(got)]);
    checks.push(['a blank title is not stored, so it cannot outrank a real fallback',
      db.prepare('SELECT COUNT(*) n FROM session_titles WHERE session_id = ?').get('T2').n === 0]);
    checks.push(['a title with no session id is dropped rather than keyed to null',
      db.prepare("SELECT COUNT(*) n FROM session_titles WHERE session_id IS NULL OR session_id = ''").get().n === 0]);
    const before = db.prepare('SELECT COUNT(*) n FROM session_titles').get().n;
    await new Harvest(db).file(titlesFile, true);
    checks.push(['re-reading the same titles adds no rows (gate can fail)',
      db.prepare('SELECT COUNT(*) n FROM session_titles').get().n === before]);
    checks.push(['every title record type this build knows has a field mapping',
      Object.keys(TITLE_FIELD).every((t) => typeof TITLE_KIND[t] === 'string'),
      Object.keys(TITLE_FIELD).filter((t) => !TITLE_KIND[t]).join(',')]);
  }

  // The mechanical guard. The hook decides what a row CONTAINS and this file decides what
  // survives; when those were two hand-written lists they disagreed for the whole life of the
  // table. Now a field added to summarise() with no column here fails the suite instead.
  {
    const { summarise } = await import(pathToFileURL(join(ROOT, 'hooks', 'event-hook.mjs')).href);
    const emitted = Object.keys(summarise({ hook_event_name: 'PostToolUse' }));
    const homeless = emitted.filter((k) => !HOOK_EVENT_COLUMNS.includes(k));
    checks.push(['every field the hook emits has a column in the ingest (gate can fail)',
      homeless.length === 0, `unstored: ${homeless.join(',')}`]);
    const cols = new Set(db.prepare('PRAGMA table_info(hook_events)').all().map((r) => r.name));
    const notInTable = HOOK_EVENT_COLUMNS.filter((c) => !cols.has(c));
    checks.push(['every ingest column exists in the table', notInTable.length === 0, notInTable.join(',')]);
  }

  // Tool-call extraction.
  const tc = db.prepare('SELECT * FROM tool_calls WHERE tool_use_id = ?').get('tu1');
  checks.push(['tool_use captured', !!tc]);
  checks.push(['tool name captured', tc?.tool_name === 'Read']);
  checks.push(['target extracted from file_path', tc?.target === 'C:/x/a.md']);
  checks.push(['result_bytes filled from a LATER line', tc?.result_bytes === 11, String(tc?.result_bytes)]);
  const mcp = db.prepare('SELECT * FROM tool_calls WHERE tool_use_id = ?').get('tu3');
  checks.push(['mcp server parsed out of the tool name', mcp?.server_name === 'azure', String(mcp?.server_name)]);
  checks.push(['a built-in tool has no server name', tc?.server_name === null]);
  checks.push(['is_error recorded from the result block', mcp?.is_error === 1]);
  checks.push(['sidechain flag carried onto the call', mcp?.is_sidechain === 1]);
  const dup = db.prepare(
    'SELECT COUNT(*) n FROM tool_calls WHERE target = ? AND tool_name = ?').get('C:/x/a.md', 'Read').n;
  checks.push(['both reads of the same file are kept, not deduped', dup === 2, String(dup)]);
  const same = db.prepare(
    'SELECT COUNT(DISTINCT input_sha1) n FROM tool_calls WHERE target = ?').get('C:/x/a.md').n;
  checks.push(['byte-identical inputs share one sha1', same === 1, String(same)]);

  // Subagent identity. Both fields were in the transcripts from the start and read by nothing.
  const agent = db.prepare('SELECT * FROM tool_calls WHERE tool_use_id = ?').get('tua');
  checks.push(['an Agent call records which agent it asked for',
    agent?.subagent_type === 'general-purpose', String(agent?.subagent_type)]);
  checks.push(['a tool that spawned no agent has NULL, not an empty string',
    tc?.subagent_type === null, JSON.stringify(tc?.subagent_type)]);
  const spawned = db.prepare('SELECT * FROM turns WHERE uuid = ?').get('ag2');
  checks.push(['a turn records the record it replied to', spawned?.parent_uuid === 'ag1',
    String(spawned?.parent_uuid)]);
  const rootless = db.prepare('SELECT * FROM turns WHERE uuid = ?').get('u1');
  checks.push(['a record with no parent stores NULL rather than its own uuid',
    rootless?.parent_uuid === null, JSON.stringify(rootless?.parent_uuid)]);
  checks.push(['the parent is a real row in this store, so the link resolves',
    db.prepare('SELECT COUNT(*) n FROM tool_calls WHERE turn_uuid = ?').get('ag1').n === 1]);
  // Negative controls: extraction that always fired would pass everything above.
  checks.push(['a tool_use with no result leaves result_bytes NULL (gate can fail)',
    db.prepare('SELECT result_bytes r FROM tool_calls WHERE tool_use_id = ?').get('tu2').r === null]);
  checks.push(['a record with no content blocks yields no tool_calls',
    db.prepare('SELECT COUNT(*) n FROM tool_calls WHERE turn_uuid = ?').get('u1').n === 0]);
  // Four now: two Reads, one MCP call and the Agent call added with subagent identity. The number
  // is written out rather than counted from the fixture on purpose, so adding a tool_use to that
  // fixture has to be a deliberate edit here as well.
  checks.push(['exactly the four planted calls were captured, no phantoms',
    db.prepare('SELECT COUNT(*) n FROM tool_calls').get().n === 4,
    String(db.prepare('SELECT COUNT(*) n FROM tool_calls').get().n)]);

  // Survivor extraction.
  const surv = extractSurvivors({
    segment: { headUuid: 'h1', anchorUuid: 'a1', tailUuid: 't1' },
    messages: { anchorUuid: 'a1', uuids: ['m1', 'm2'], allUuids: ['m1', 'm2', 'm3'] },
  });
  checks.push(['survivors: segment and message uuids are extracted', surv.length === 6, JSON.stringify(surv.length)]);
  checks.push(['survivors: uuids and allUuids are unioned, not overwritten',
    surv.filter((x) => x.kind === 'message').length === 3]);
  checks.push(['survivors: kinds are labelled',
    new Set(surv.map((x) => x.kind)).size === 4]);
  checks.push(['survivors: an old build with no payload yields none, not an error',
    extractSurvivors(null).length === 0 && extractSurvivors('{}').length === 0]);
  checks.push(['survivors: unparseable payload yields none rather than throwing',
    extractSurvivors('{not json').length === 0]);
  checks.push(['survivors: a different payload yields different rows (gate can fail)',
    extractSurvivors({ segment: { headUuid: 'zzz' } })[0].uuid === 'zzz']);
  const dbSurv = db.prepare('SELECT COUNT(*) n FROM compaction_survivors').get().n;
  checks.push(['survivors: harvest recorded none for a boundary without a preserved payload', dbSurv === 0]);

  // The store path is overridable, and backfill can be exercised without touching production.
  const tmpDb = join(ROOT, 'tmp', `backfill-selftest-${process.pid}-${Date.now()}.db`);
  rmSync(tmpDb, { force: true });
  const prodBefore = existsSync(DEFAULT_DB_PATH) ? statSync(DEFAULT_DB_PATH).mtimeMs : null;
  {
    const t = new DatabaseSync(tmpDb);
    t.exec(SCHEMA);
    t.prepare('INSERT INTO compactions (uuid,preserved_json) VALUES (?,?)').run('c-with',
      JSON.stringify({ segment: { headUuid: 'h', anchorUuid: 'a', tailUuid: 't' },
                       messages: { uuids: ['m1'], allUuids: ['m1', 'm2'] } }));
    t.prepare('INSERT INTO compactions (uuid,preserved_json) VALUES (?,?)').run('c-without', null);
    t.prepare(`INSERT INTO turns (uuid,session_id,total_resident) VALUES (?,?,?)`).run('m1', 's', 10);
    t.close();
  }
  const bf = backfillSurvivors(tmpDb, { quiet: true });
  checks.push(['backfill: --db targets the given store', bf && bf.db === tmpDb, JSON.stringify(bf && bf.db)]);
  checks.push(['backfill: every survivor uuid is recorded', bf && bf.survivor_rows === 5, String(bf && bf.survivor_rows)]);
  checks.push(['backfill: matched and unmatched sum to the total',
    bf && bf.matched_to_a_stored_turn + bf.unmatched === bf.survivor_rows,
    `${bf && bf.matched_to_a_stored_turn} + ${bf && bf.unmatched} vs ${bf && bf.survivor_rows}`]);
  checks.push(['backfill: a uuid we hold is counted as matched', bf && bf.matched_to_a_stored_turn === 1,
    String(bf && bf.matched_to_a_stored_turn)]);
  checks.push(['backfill: a compaction with no payload yields no rows, not an error',
    bf && bf.compactions_with_survivors === 1 && bf.compactions_total === 2,
    `${bf && bf.compactions_with_survivors} of ${bf && bf.compactions_total}`]);
  checks.push(['backfill: it is idempotent', (() => {
    const again = backfillSurvivors(tmpDb, { quiet: true });
    return again && again.survivor_rows === 5 && again.inserted_this_run === 0;
  })()]);
  checks.push(['backfill: a missing store is refused, not created (gate can fail)', (() => {
    const ghost = join(ROOT, 'tmp', `no-such-${process.pid}.db`);
    const r = backfillSurvivors(ghost, { quiet: true });
    return r === null && !existsSync(ghost);
  })()]);
  const prodAfter = existsSync(DEFAULT_DB_PATH) ? statSync(DEFAULT_DB_PATH).mtimeMs : null;
  checks.push(['backfill: the production store was never opened', prodBefore === prodAfter,
    `${prodBefore} vs ${prodAfter}`]);
  rmSync(tmpDb, { force: true });
  rmSync(tmpDb + '-wal', { force: true });
  rmSync(tmpDb + '-shm', { force: true });

  // The agent backfill's safety report, on a store another writer is appending to.
  //
  // This is the case that made the guard wrong on the first live run. Comparing COUNT(*) before
  // and after said "rows changed" because the dashboard's refresh loop and the hooks had written
  // a turn and a tool call while the scan was running - and harvest_runs and messages moved too,
  // which this backfill never touches. Counting only rows that already existed is what makes the
  // guard about this tool instead of about the store being alive.
  const liveDb = join(ROOT, 'tmp', `live-${process.pid}.db`);
  rmSync(liveDb, { force: true });
  {
    const t = new DatabaseSync(liveDb);
    t.exec(SCHEMA);
    t.prepare('INSERT INTO turns (uuid,session_id,total_resident) VALUES (?,?,?)').run('old1', 's', 10);
    t.prepare(`INSERT INTO tool_calls (tool_use_id,session_id,tool_name) VALUES (?,?,?)`)
      .run('oldtc', 's', 'Agent');
    t.close();
  }
  const liveReport = await backfillAgents(liveDb, { quiet: true });
  checks.push(['agent backfill: a store with nothing to fill reports no change and exits 0',
    liveReport === 0]);
  {
    // Now with a CONCURRENT insert: one row appended between the two measurements, exactly as a
    // hook harvest does. The report must call the pre-existing rows unchanged and attribute the
    // new row to the other writer.
    const t = new DatabaseSync(liveDb);
    t.prepare('INSERT INTO turns (uuid,session_id,total_resident) VALUES (?,?,?)').run('new1', 's', 20);
    const total = t.prepare('SELECT COUNT(*) n FROM turns').get().n;
    const existing = t.prepare('SELECT COUNT(*) n FROM turns WHERE rowid <= ?').get(1).n;
    t.close();
    checks.push(['agent backfill: a concurrent insert raises the total but not the pre-existing count',
      total === 2 && existing === 1, `${total} total, ${existing} pre-existing`]);
  }
  rmSync(liveDb, { force: true });
  rmSync(liveDb + '-wal', { force: true });
  rmSync(liveDb + '-shm', { force: true });

  // Negative control: the same assertions against an EMPTY store must fail.
  const empty = new DatabaseSync(':memory:');
  empty.exec(SCHEMA);
  const mustFail = !empty.prepare('SELECT * FROM turns WHERE uuid = ?').get('u1');
  checks.push(['negative control: empty store has no turn (gate can fail)', mustFail]);

  let bad = 0;
  for (const [name, ok] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`); }
  rmSync(tmp, { recursive: true, force: true });
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
let code = 0;
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) code = await selfTest();
else if (argv.includes('--stats')) code = stats();
else if (argv.includes('--backfill-survivors')) code = backfillSurvivors(DB_PATH) ? 0 : 1;
else if (argv.includes('--backfill-titles')) code = await backfillTitles();
else if (argv.includes('--backfill-agents')) code = await backfillAgents(resolveDbPath(argv));
else code = await run({ full: argv.includes('--full') });
if (IS_ENTRY) process.exit(code);

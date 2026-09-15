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
//   node harvest.mjs --dry-run       say what an incremental run would read; write nothing
//   node harvest.mjs --full --yes    ignore stored offsets, re-read everything (--full alone
//                                    prints the file count and the byte total, and refuses)
//   node harvest.mjs --self-test     prove the parser detects what it claims to detect
//   node harvest.mjs --stats         print store contents, harvest nothing
//   node harvest.mjs --backfill-chains [--dry-run] [--records <dir>]
//                                    link each resumed session to the chat it belongs to and
//                                    return copied rows to the session that produced them;
//                                    --dry-run reports without writing
//   node harvest.mjs --backfill-reviews [--dry-run]
//                                    tie each one-shot session that quotes another session (a
//                                    hook's headless reviewer) to the session it read;
//                                    --dry-run reports without writing

import { createReadStream, existsSync, mkdirSync, readdirSync, statSync, appendFileSync, readFileSync, writeFileSync, rmSync, openSync, readSync, closeSync, readlinkSync, symlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';
import { join, dirname, basename, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { rootFrom, resolveDb, ensureStoreDir, posix } from './paths.mjs';
import { classifyResult, TOOL_OUTCOME } from './outcomes.mjs';
import { homedir } from 'node:os';

const ROOT = rootFrom(import.meta.url);
const SELF_PATH = fileURLToPath(import.meta.url);
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
  bytes_read INTEGER, lines_read INTEGER, rewrites INTEGER DEFAULT 0, last_harvest_ts TEXT,
  -- The earliest timestamp in the file, read once and kept. It decides INGEST ORDER: a resumed or
  -- forked transcript copies its predecessor's records, and the producer has to be read before the
  -- copy so the copy is the one that is refused (see the ON CONFLICT clauses on turns and
  -- messages). Directory order, which this used to walk in, made "who produced this row" depend
  -- on the filesystem.
  first_ts TEXT,
  -- NULL is a transcript, which is every row written before this column existed. 'sidecar' is one
  -- of the small JSON files beside them, read whole rather than resumed from an offset. Here AND
  -- in ADDED_COLUMNS, the way first_ts is: the schema builds a fresh store and the migration
  -- carries an existing one, and a self-test that runs against an in-memory schema needs both.
  kind TEXT
);
-- cwd is the directory the session LIVES in: the first cwd in its transcript whose slug is the
-- directory the file sits in (a session can change directory; its transcript does not move), or
-- the latest cwd seen until one matches. project_slug is that directory's name and
-- transcript_path the session's own top-level file, never a subagent file that names it. All
-- three are enforced by the putSession upsert and repaired by the chains pass and by
-- --backfill-chains for rows written under older rules.
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
-- BY FILE, because a subagent run is identified by the transcript it wrote and by nothing else.
-- The transcript_path column of agent_runs joins here, and without this index every such lookup is
-- a scan of 475,805 rows; the panel reading them was measured taking over ten minutes for one chat.
-- No backticks in this comment: the whole schema is a JS template literal and one would end it.
CREATE INDEX IF NOT EXISTS turns_file ON turns(file_path);
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
-- The same join as turns_file, for the count and the first and last timestamps of a run.
CREATE INDEX IF NOT EXISTS messages_file ON messages(file_path);
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
  subagent_type TEXT,
  -- THE HEAD OF WHAT WAS ASKED FOR. The hash and the byte count were derived from the input and
  -- the input itself was thrown away, so a row could say a call was 2,655 bytes and rejected and
  -- not one word of WHAT was proposed. The timeline read "plan written" then "the user does not
  -- want to proceed with this tool use" with nothing in between, and the plan was sitting in the
  -- transcript on disk the whole time.
  --
  -- 500 characters, measured on this store: it adds 64 MB to a 1.37 GB store, 4.6%, and 174,538
  -- of 238,632 calls fit inside it whole. A preview, not the input: the transcript remains the
  -- record, and input_bytes still says how much of it this is.
  input_preview TEXT,
  -- THE NOTE THE AGENT WROTE ABOUT THIS CALL, in its own column rather than left inside the
  -- input blob. Claude Code never puts assistant prose and a tool_use in the SAME record:
  -- measured on one session, 3,111 text-only records, 7,939 tool-call-only records, and ZERO
  -- carrying both. So the short active-voice line an agent writes before a command is not
  -- assistant text at all, it is this field, and it lived only in records the messages table
  -- drops for having no readable text.
  --
  -- Not left to input_preview to carry: JSON.stringify puts the long command first, so on this
  -- store 2,386 of 5,974 descriptions (40%) fall outside a 500-character preview. A note that
  -- survives 60% of the time is not a note you can read a session with.
  description TEXT,
  -- WHAT THE CALL TURNED OUT TO BE, because is_error meant two opposite things at once: a tool
  -- that RAN AND FAILED and a tool that NEVER RAN because something refused it. Measured across
  -- this store after the backfill, of 6,871 flagged calls 1,848 (26.9%) were refusals, and not
  -- one of the ExitPlanMode row's 41 flagged calls can be PROVEN to have run and failed: 13
  -- carry a denial kind and 28 predate the field. Of the 39 whose result text could be matched,
  -- exactly ONE is a genuine tool error and the rest never ran. This column reports the
  -- provable answer, never the inferred one.
  --
  -- One of ok, error, refused, unclassified. NULL means no result block has ever been seen for
  -- this call, which is a real answer and not the same as any of the four.
  outcome TEXT,
  -- Claude Code own word for WHY it never ran, stored verbatim and never grouped. Present only on
  -- a refusal, so denial_kind IS NOT NULL means exactly that, the same contract subagent_type has.
  -- Deliberately not translated: permission-rule covers a settings deny rule AND a hook that
  -- blocked the call, the transcript records the same value for both, so any split would be ours
  -- rather than the transcript.
  denial_kind TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_session ON tool_calls (session_id);
CREATE INDEX IF NOT EXISTS tool_calls_target ON tool_calls (target);
CREATE INDEX IF NOT EXISTS tool_calls_name ON tool_calls (tool_name);
-- THE PLAN, WHOLE. An ExitPlanMode call carries the entire proposal in its input, and until now
-- the only trace of it was the first 500 characters of input_preview: JSON.stringify puts "plan"
-- first, so a preview is the plan's opening sentence and NEVER its planFilePath. Measured on this
-- store: 291 ExitPlanMode calls against 27 surviving files in ~/.claude/plans, so the transcript
-- is the record and the file is a pointer that usually points at nothing.
--
-- KEYED ON THE CALL, not on the path. One path already serves two calls in one session here, and
-- the two proposals are different documents. The outcome of the call is NOT copied in: an
-- accepted plan and a refused one are the same text, and only tool_calls knows which.
CREATE TABLE IF NOT EXISTS plans (
  tool_use_id TEXT PRIMARY KEY,
  session_id TEXT, turn_uuid TEXT, ts TEXT,
  plan_text TEXT, plan_chars INTEGER,
  plan_file_path TEXT, allowed_prompts_json TEXT,
  is_sidechain INTEGER, file_path TEXT, line_no INTEGER
);
CREATE INDEX IF NOT EXISTS plans_session ON plans (session_id, ts);
-- ONE ROW PER agent-<id>.jsonl, the transcript a subagent wrote for itself.
--
-- KEYED ON THE AGENT ID, which is unique across the machine. That is what makes the measured
-- relocation case representable: history is bridged between sessions, so a run referenced from one
-- chat can be stored under another chat's directory, and both ids are kept rather than one being
-- chosen. dir_session_id is where the files are; tool_use_id is the call that asked for it.
--
-- tool_use_id IS NULL FOR MOST RUNS AND THAT IS A REAL STATE. Measured: 796 plain runs, of which
-- 400 sampled all carried toolUseId, and 6,636 workflow agents, NONE of which carry one. A design
-- that required it would hold 10% of the population.
--
-- meta_json is the whole meta file. The key set drifts between builds (agentType and description
-- on every one of 400 sampled, spawnDepth on 349, name on 183, model on 71), so the columns are
-- what is read today and this is what survives the next build adding a field.
CREATE TABLE IF NOT EXISTS agent_runs (
  agent_id TEXT PRIMARY KEY,
  dir_session_id TEXT, tool_use_id TEXT, workflow_run_id TEXT,
  agent_type TEXT, name TEXT, description TEXT,
  spawn_depth INTEGER, model TEXT, parent_agent_id TEXT, stopped_by_user INTEGER,
  meta_json TEXT, transcript_path TEXT,
  meta_path TEXT, meta_size INTEGER, meta_mtime_ms INTEGER
);
CREATE INDEX IF NOT EXISTS agent_runs_dir ON agent_runs (dir_session_id);
CREATE INDEX IF NOT EXISTS agent_runs_tool_use ON agent_runs (tool_use_id);
CREATE INDEX IF NOT EXISTS agent_runs_workflow ON agent_runs (workflow_run_id);
-- ONE ROW PER workflows/wf_<runid>.json, written by TWO passes that own different columns.
--
-- The transcript pass fills tool_use_id, turn_uuid and session_id from the toolUseResult on the
-- Workflow call's result, minutes before the JSON beside the transcripts has a status or a token
-- total. The sidecar pass fills the rest. Either can arrive first, so NEITHER may use INSERT OR
-- REPLACE and each names only its own columns: the same hazard putToolCall's COALESCE'd result
-- columns already document, and one that fails silently.
CREATE TABLE IF NOT EXISTS workflow_runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT, dir_session_id TEXT,
  tool_use_id TEXT, turn_uuid TEXT, session_id TEXT,
  workflow_name TEXT, status TEXT, started_at TEXT, ts TEXT, duration_ms INTEGER,
  agent_count INTEGER, total_tokens INTEGER, total_tool_calls INTEGER,
  default_model TEXT, summary TEXT, result_text TEXT,
  phases_json TEXT, progress_json TEXT, error TEXT,
  script_path TEXT, transcript_dir TEXT,
  file_path TEXT, file_size INTEGER, file_mtime_ms INTEGER
);
CREATE INDEX IF NOT EXISTS workflow_runs_dir ON workflow_runs (dir_session_id);
CREATE INDEX IF NOT EXISTS workflow_runs_task ON workflow_runs (task_id);
-- The task_status attachments, DETAILED rather than counted. attachments still counts them, and
-- still must: that census is how an unknown attachment type becomes visible. Measured here: 27 of
-- them across 10 sessions, so this is a detail of the page and never its main source.
--
-- Keyed on the record uuid, the rule turns and messages use, so two events for one task stay two.
CREATE TABLE IF NOT EXISTS task_events (
  uuid TEXT PRIMARY KEY,
  session_id TEXT, ts TEXT, parent_uuid TEXT,
  task_id TEXT, task_type TEXT, status TEXT,
  description TEXT, delta_summary TEXT, output_file_path TEXT,
  file_path TEXT, line_no INTEGER
);
CREATE INDEX IF NOT EXISTS task_events_session ON task_events (session_id, ts);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events (task_id);
-- EVERY FILE CHANGE CLAUDE MADE, tied to the turn that made it. Written by TWO passes that own
-- disjoint columns and can arrive in either order, the rule workflow_runs already follows: the
-- tool_use block gives identity and the text, the tool_result record gives the patch. Measured
-- before this existed: 11,761 main-line edits carry a structured patch in their result and 20,852
-- subagent edits carry no result at all, only the call, so a table fed from one side would lose
-- the other. NULL additions and deletions mean no result was recorded, which is the subagent
-- state, never a zero in disguise. The original file is not kept: the hunks carry their own
-- context and it would be 13 MB that is only ever re-derived.
CREATE TABLE IF NOT EXISTS changes (
  tool_use_id TEXT PRIMARY KEY,
  session_id TEXT, turn_uuid TEXT, ts TEXT,
  tool_name TEXT, file TEXT, kind TEXT,
  old_text TEXT, new_text TEXT, replace_all INTEGER,
  old_lines INTEGER, new_lines INTEGER,
  patch_json TEXT, additions INTEGER, deletions INTEGER,
  original_chars INTEGER, user_modified INTEGER,
  is_sidechain INTEGER, file_path TEXT, line_no INTEGER
);
CREATE INDEX IF NOT EXISTS changes_session ON changes (session_id, ts);
CREATE INDEX IF NOT EXISTS changes_file ON changes (file);
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
-- WHICH SESSIONS ARE ONE CHAT. The desktop app resumes a chat by starting a NEW CLI session whose
-- transcript is a copy of the old one plus whatever comes next: same message uuids, sessionId
-- rewritten. So a chat resumed four times is five transcripts, five session rows, and the app
-- shows ONE entry. Nothing on disk records the chain: the app's record keeps only the current
-- cliSessionId, transcripts carry no parent field, and the two bridge ids do not match. The
-- overlap of message uuids is the only evidence and it is complete: measured on a five-session
-- chat the successive transcripts share 808 of 823, 1296 of 1304, 1349 of 1418 and 1680 of 1800
-- uuids, and the rule "A is a prefix of B when B is larger and holds at least 90 percent of A"
-- reproduces the app's own priorCliSessionIds order exactly. A resume taken after a compaction
-- copies only the tail, so a second shape is accepted: B's first records all sit inside A.
--
-- WHOSE COPY IT IS decides between a resume and a fork. A resume rewrites every copied line to its
-- own sessionId; a fork copies them verbatim, the parent's sessionId included. So B succeeds A
-- only when B holds A's records under B's own id (deriveLinks, NATIVE_SHARE), only when B is the
-- later transcript (transcriptOrder, so no chain loops), and not when B descends from a fork of A
-- whose own first transcript still names A. Measured on 17 links: 13 resumes native 100 percent,
-- 4 forks native 0 percent.
--
-- A session WITH a desktop record is a chat in its own right and never appears as session_id
-- here: a fork copies history too and the app shows it as a separate chat. Only record-less
-- sessions fold into a container. Links are derived by --backfill-chains and by the per-directory
-- pass at the end of every harvest; they are never written during ingest, because a REPLACE
-- collision seen mid-ingest can point at a fork instead of the resume.
--
-- head_id is what every reader collapses to; next_id and the counts are the evidence for it.
-- head_kind says what the discriminator found for next_id: record, none, or fork.
-- (No backticks in here: this block sits inside a JS template literal.)
CREATE TABLE IF NOT EXISTS session_links (
  session_id TEXT PRIMARY KEY,
  head_id TEXT NOT NULL,
  next_id TEXT NOT NULL,
  head_kind TEXT NOT NULL,
  overlap REAL NOT NULL,
  prefix_uuids INTEGER NOT NULL,
  next_uuids INTEGER NOT NULL,
  shared_uuids INTEGER NOT NULL,
  method TEXT NOT NULL,
  linked_at TEXT NOT NULL,
  CHECK (head_id <> session_id)
);
CREATE INDEX IF NOT EXISTS session_links_head ON session_links(head_id);
-- WHICH ONE-SHOT SESSIONS ARE REVIEW RUNS OF ANOTHER SESSION. A Stop hook on the test laptop ran
-- a headless claude -p after each turn with a prompt quoting the last 250 records of the
-- transcript under review. Each run left a one-prompt session in the reviewed session's folder,
-- carrying the app's own entrypoint and no session id, and the store held 58 of them as chats.
-- Quotation is the only evidence and it is exact: long ASCII lines from the prompt's tail occur
-- in the reviewed session's assistant text and tool results, and in no other session's. A
-- session whose assistant text or tool results contain none of them is out, whatever its typed
-- prompts say: a one-shot that repeats a PERSON'S prompt word for word (a test harness running
-- the same command in seven sessions) matches typed rows alone and is not a review. Among the
-- sessions that pass, every quoted line counts, typed ones included.
-- head_id is the reviewed session, and every reader folds a run into it beside the chain map
-- above: out of the lists, into the chat's numbers under the subagent scope. hits and snippets
-- are the evidence; verdict is the run's first word when it is APPROVED or PROBLEMS. Derived after
-- every harvest pass for the directories it touched, and by --backfill-reviews. A found link is
-- permanent. review_misses remembers a run that tied to nothing together with the sessions that
-- were its pool, so it is asked again only when a session joins that pool.
-- head_id IS NULL for an ORPHAN: a run that is a review by every other sign (its lines are said
-- somewhere in the store, at least two of them, and its reply is a bare verdict) but whose folder
-- holds no session that says them, so the store cannot name the chat it read. Four of the 58 on
-- the laptop: one ran in a skills directory, two in a fixtures subfolder with no other session,
-- one beside a chat that says none of its lines. An orphan is listed nowhere and folds into no
-- chat; it exists so the record the app holds for it can be taken back with the rest.
CREATE TABLE IF NOT EXISTS review_links (
  session_id TEXT PRIMARY KEY,
  head_id TEXT,
  hits INTEGER NOT NULL,
  snippets INTEGER NOT NULL,
  verdict TEXT,
  method TEXT NOT NULL,
  linked_at TEXT NOT NULL,
  CHECK (head_id IS NULL OR head_id <> session_id)
);
CREATE INDEX IF NOT EXISTS review_links_head ON review_links(head_id);
CREATE TABLE IF NOT EXISTS review_misses (
  session_id TEXT PRIMARY KEY,
  pool_key TEXT NOT NULL,
  checked_at TEXT NOT NULL
);
-- THE APP'S OWN RECORDS, REMEMBERED. The desktop app lists a chat because a local_<uuid>.json
-- names it, and when a chat is deleted in the app the record goes and a marker deleted_<uuid>
-- appears beside it (measured on the test laptop, 2026-09-15: 13 ASCII digits, the delete time
-- in epoch milliseconds; the transcript under ~/.claude/projects is left alone). The store never
-- knew a record's uuid, so a marker named nothing it could act on and a deleted chat stayed
-- listed. Every pass now remembers each record it sees, uuid and cliSessionId together, and
-- seeds the same from data/adopted-records.json for the records c4x wrote; a record that is gone
-- with a marker is stamped deleted_at (the app deleted the chat), one gone without is stamped
-- gone_at only (c4x took it back, a move, a reinstall: not a delete). Rows are never removed; a
-- record that returns has both stamps cleared. Readers hide a chat whose record is deleted_at.
-- THE ACCOUNT A CHAT WAS MADE UNDER. dir is where the record is now; owner_account and owner_org
-- are the pair it first appeared under, written once and never updated: the app itself moves
-- records between pairs at an account switch, and under sharing every pair lists the same
-- directory, so dir says nothing about who made a chat. owner_source says which evidence answered: the ledger
-- (c4x wrote it into the adopting account's pair), an unshared directory, the account signed
-- in when the record was first seen, a sharing backup's manifest, or unknown. Also listed in
-- ADDED_COLUMNS for a store made before them; the two must agree.
CREATE TABLE IF NOT EXISTS desktop_records (
  record_uuid TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  dir TEXT NOT NULL,
  title TEXT,
  archived INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  gone_at TEXT,
  deleted_at TEXT,
  source TEXT NOT NULL,
  owner_account TEXT,
  owner_org TEXT,
  owner_source TEXT
);
CREATE INDEX IF NOT EXISTS desktop_records_session ON desktop_records(session_id);
-- WHICH ACCOUNT WAS SIGNED IN, over time. One row per change, as harvest saw it: seen_at is the
-- harvest, switched_at is when the app rewrote config.json (its lastKnownAccountUuid), so a
-- switch made while no session ran is still dated. The Summary tab reads this to say which API
-- calls rewrote their whole context right after a switch (caches are isolated between
-- organisations, and never carried across one).
CREATE TABLE IF NOT EXISTS account_log (
  seen_at TEXT NOT NULL,
  switched_at TEXT,
  account TEXT NOT NULL,
  org TEXT,
  source TEXT
);
-- A project the user deleted and asked to stop capturing. Keyed on cwd, not on the transcript
-- directory: the mapping is many-to-many, the 'subagents' slug alone covers 30 different working
-- directories, and excluding one of those by slug would silently stop capturing the other 29.
CREATE TABLE IF NOT EXISTS excluded_projects (
  cwd TEXT PRIMARY KEY, excluded_at TEXT, note TEXT
);
-- known says whether harvest RECOGNISED the type, the same question hook_events answers with its
-- own known column. Without it the census renders cost-state in the same two columns as assistant
-- and nothing says one was parsed and the other counted and discarded, so an upstream schema
-- change is visible only as a name a reader has no reason to distrust. Claude Code 2.1.250 can
-- write 37 record types; this build recognises 14, and nine of the rest are already being counted
-- on stores in the wild.
-- (No backticks in here: this block sits inside a JS template literal.)
CREATE TABLE IF NOT EXISTS record_types (type TEXT PRIMARY KEY, n INTEGER, known INTEGER);
-- WHAT CLAUDE CODE SAYS THE SESSION COST, as opposed to what this app computes it would have.
--
-- c4x/pricing.py opens by stating that no cost is recorded anywhere and that every money figure is
-- arithmetic over tokens times a committed price table. That was true until Claude Code began
-- writing a cost-state record carrying its own totalCostUSD. Storing it does not replace the
-- estimate: the two are shown side by side, because they can disagree and the disagreement is the
-- interesting part. The estimate is a stated LOWER BOUND (cache TTL and inference geography are
-- not recorded), and the measured figure can itself be incomplete, which Claude Code flags with
-- has_unknown_model_cost.
--
-- ONE ROW PER SESSION, and the values are CUMULATIVE, so a later record supersedes an earlier one.
-- The upsert only ever moves a total upward. An incremental harvest can re-read a byte range and
-- meet an older record again, and without that guard the stored cost would walk backwards for no
-- reason a reader could see.
CREATE TABLE IF NOT EXISTS cost_state (
  session_id TEXT PRIMARY KEY,
  started_at TEXT,
  total_cost_usd REAL,
  total_api_ms INTEGER,
  total_api_ms_no_retries INTEGER,
  total_tool_ms INTEGER,
  total_duration_ms INTEGER,
  lines_added INTEGER,
  lines_removed INTEGER,
  has_unknown_model_cost INTEGER,
  file_path TEXT,
  line_no INTEGER
);
-- The per-model half of the same record. Separate table because modelUsage is a map, and folding
-- it into columns would need one set per model name and a migration every time a model ships.
CREATE TABLE IF NOT EXISTS cost_state_models (
  session_id TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  web_search_requests INTEGER,
  cost_usd REAL,
  PRIMARY KEY (session_id, model)
);
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
// A transcript first seen with this many lines or fewer is taken to hold only its head records
// (mode, atis-latch, bridge-session, queue-operation: four on every resume seen so far) and is
// re-examined for chains when it grows past them. See Harvest.file.
const HEAD_ONLY_LINES = 50;

export const HOOK_EVENT_COLUMNS = [
  'captured_at', 'probe', 'event', 'known', 'session_id', 'transcript_path', 'cwd',
  'permission_mode', 'tool_name', 'tool_input_bytes', 'tool_response_bytes', 'prompt_chars',
  'source', 'reason', 'agent_id', 'agent_type', 'truncated', 'extra',
];

// Columns added to tables that already existed in the wild, by table. Every one is TEXT and
// nullable, so adding it cannot invalidate a row: an old row simply has nothing in it, which is
// exactly true. Anything needing a type or a default is a rebuild, not an entry here.
// How much of a tool input is kept. See the column comment in the schema for the measurement.
export const TOOL_INPUT_PREVIEW = 500;

export const ADDED_COLUMNS = {
  hook_events: HOOK_EVENT_COLUMNS,
  turns: ['parent_uuid'],
  tool_calls: ['subagent_type', 'input_preview', 'description', 'outcome', 'denial_kind'],
  // `kind` is NULL on every row written before it existed, and NULL means transcript. The sidecar
  // pass writes 'sidecar' for the small JSON files beside them, so a reader that means transcripts
  // can say so: the census, the dry run and the Diagnostics tab all count rows in this table.
  files: ['first_ts', 'kind'],
  // The account a chat was made under; see the table's comment. NULL on every row from before,
  // and reconcileDesktopRecords fills what the ledger, the sharing backup or an unshared
  // directory can answer, stamping the rest 'unknown' so the next pass has nothing to try.
  desktop_records: ['owner_account', 'owner_org', 'owner_source'],
};
const BOOLEAN_EVENT_COLUMNS = new Set(['probe', 'known', 'truncated']);

/**
 * The hook event log, ingested from where the LAST run stopped.
 *
 * THE COST WAS NOT DISK, IT WAS THE RE-READ. This read the whole file and re-offered every row to
 * INSERT OR IGNORE on every harvest. Nothing rotates the file, so it only grows: on this install it
 * had reached 36 MB, and 29,049 harvest runs had each read all of it to learn what the previous one
 * already knew. The tool's own timing excludes ingest, so nothing in the app could see the cost.
 *
 * The transcript walk has solved this since it was written. `files.bytes_read` is a per-path
 * watermark and `Harvest.file` reads from it; this is that mechanism applied to the one file left
 * out, including its shrink rule, so a file SMALLER than its watermark is read from zero rather
 * than skipped and a future rotation cannot silently drop records.
 *
 * TWO THINGS THAT LOOK RIGHT AND ARE NOT. A byte offset cannot index a JavaScript string, because
 * `slice` counts CHARACTERS and this file is UTF-8, so a multibyte payload would shift every
 * subsequent read; the tail is read positionally into a Buffer instead. And the watermark may not
 * advance past a line that has no terminator yet: the hook appends while this runs, so the last
 * line can be half written, and recording it as consumed would drop it forever. INSERT OR IGNORE
 * stays as the backstop, so a wrong watermark degrades to the old behaviour rather than to loss.
 */
export function ingestEvents(db, path) {
  if (!existsSync(path)) return { file: path, exists: false, seen: 0, stored: 0, genuine: 0, bad: 0 };
  const size = statSync(path).size;
  const prev = db.prepare('SELECT bytes_read FROM files WHERE path = ?').get(path);
  // SIZE ALONE CANNOT TELL AN APPEND FROM A REPLACEMENT. A log rewritten with different content
  // of equal or greater length passes `size >= bytes_read`, so the offset lands in the MIDDLE of a
  // line and every row after it is dropped as unparseable, silently. Caught by this file's own
  // self-test, where one fixture replaces the log and two unrelated checks went red.
  //
  // An append-only file always has a newline immediately before the watermark, because the
  // watermark is only ever advanced to just past one. So read that byte: if it is not a newline,
  // this is not the file we measured, and it is read from zero.
  let rewound = !!prev && size < prev.bytes_read;
  if (prev && !rewound && prev.bytes_read > 0 && prev.bytes_read <= size) {
    const fd = openSync(path, 'r');
    try {
      const probe = Buffer.allocUnsafe(1);
      readSync(fd, probe, 0, 1, prev.bytes_read - 1);
      if (probe[0] !== 0x0a) rewound = true;          // 0x0a is the line feed
    } finally { closeSync(fd); }
  }
  const start = prev && !rewound ? Math.min(prev.bytes_read, size) : 0;

  let text = '';
  if (size > start) {
    const fd = openSync(path, 'r');
    try {
      const buf = Buffer.allocUnsafe(size - start);
      const got = readSync(fd, buf, 0, size - start, start);
      text = buf.subarray(0, got).toString('utf8');
    } finally { closeSync(fd); }
  }

  // Only whole lines count as consumed. Anything after the last newline is still being written.
  const lastBreak = text.lastIndexOf(String.fromCharCode(10));
  const complete = lastBreak === -1 ? '' : text.slice(0, lastBreak + 1);
  const consumed = start + Buffer.byteLength(complete, 'utf8');

  const ins = db.prepare(`INSERT OR IGNORE INTO hook_events
    (${HOOK_EVENT_COLUMNS.join(',')})
    VALUES (${new Array(HOOK_EVENT_COLUMNS.length).fill('?').join(',')})`);
  let seen = 0, stored = 0, genuine = 0, bad = 0;
  for (const line of complete.split(String.fromCharCode(10))) {
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

  db.prepare(`INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts)
    VALUES (?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
    size=excluded.size, mtime_ms=excluded.mtime_ms, bytes_read=excluded.bytes_read,
    lines_read=excluded.lines_read, rewrites=excluded.rewrites,
    last_harvest_ts=excluded.last_harvest_ts`)
    .run(path, size, Math.round(statSync(path).mtimeMs), consumed, 0,
         rewound ? 1 : 0, new Date().toISOString());

  return { file: path, exists: true, seen, stored, genuine, bad, from: start, rewound };
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

// WHAT WROTE THIS RECORD, which is not the same as what type of record it is.
//
// Claude Code files a TOOL RESULT as a record of type 'user'. Measured across every transcript on
// this machine, 86.5% of the 31,666 records typed 'user' were tool results and 13.0% were typed by
// a person; store-wide the 'user' rows hold 563M characters against the assistant's 69M, which no
// one types. This function used to return the record's `type`, so a directory listing and a
// question were stored identically and the Session tab labelled both "user".
//
// The block types are the evidence, and `messageText` already walks past them. Order matters: a
// compact summary is a 'user' record whose content is a plain STRING, so it has to be recognised
// before the string case claims it.
export function messageKind(d) {
  if (d?.isCompactSummary === true) return 'compact_summary';
  if (d?.type === 'system' && d?.subtype) return `system/${d.subtype}`;
  if (d?.type === 'assistant') return 'assistant';
  if (d?.type !== 'user') return typeof d?.type === 'string' ? d.type : 'unknown';

  const c = d?.message?.content;
  if (typeof c === 'string') return 'typed';
  if (!Array.isArray(c)) return 'unknown';
  const kinds = new Set();
  for (const b of c) {
    if (typeof b === 'string') { kinds.add('text'); continue; }
    if (b && typeof b === 'object' && typeof b.type === 'string') kinds.add(b.type);
  }
  // A tool result decides the record even when a text block rides along with it: the bulk is the
  // tool's output and calling it typed would be the whole defect again.
  if (kinds.has('tool_result')) return 'tool_result';
  if (kinds.has('image') || kinds.has('document')) return 'attachment';
  if (kinds.has('text')) return 'typed';
  return 'unknown';
}

// Exported so tools/make_fixture.mjs can build a synthetic store through the SAME schema and
// pragmas the real one uses. A fixture with its own copy of the schema drifts from this file
// and then CI passes against a database shaped like nothing the app will ever open.
export function openDb(dbPath = DB_PATH) {
  // ensureStoreDir, not a bare mkdir: this is the site that creates data/ on a machine where the
  // installer never ran, so it is the one that decides who can read the conversation text.
  ensureStoreDir(dirname(dbPath));
  ensureStoreDir(RAW_DIR);
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
  // review_links was born with head_id NOT NULL, one build before orphans existed. Rebuilt WITH
  // its rows (a found link is permanent) under the nullable shape; the old index goes with the
  // old table, explicitly, or CREATE INDEX IF NOT EXISTS would find its name taken and leave the
  // new table unindexed. Reported once, never silent.
  const links = db.prepare(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='review_links'").get();
  if (links && /head_id TEXT NOT NULL/.test(String(links.sql))) {
    db.exec('ALTER TABLE review_links RENAME TO review_links_old');
    db.exec('DROP INDEX IF EXISTS review_links_head');
    db.exec(SCHEMA);
    db.exec(`INSERT INTO review_links (session_id, head_id, hits, snippets, verdict, method, linked_at)
             SELECT session_id, head_id, hits, snippets, verdict, method, linked_at FROM review_links_old`);
    db.exec('DROP TABLE review_links_old');
    // The misses go too, once: every one was judged before the orphan look existed, and a miss is
    // asked again only when its pool changes, which for a run in a folder of its own is never.
    // Emptying the table here makes the next pass ask each of them under the new rule.
    db.exec("DELETE FROM review_misses WHERE EXISTS (SELECT 1 FROM sqlite_master WHERE name='review_misses')");
    console.error('harvest: rebuilt review_links so head_id may be NULL (a review of a chat this store cannot name)');
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
  // SEPARATE FROM THE TABLE ABOVE, because that loop adds every column as TEXT and this one holds
  // 0 or 1. It backfills nothing: every existing row keeps a NULL known until the next harvest
  // recounts it, and NULL reads as "this store has not been recounted since the column arrived",
  // which is the truth and is distinguishable from both 0 and 1.
  {
    const have = new Set(db.prepare('PRAGMA table_info(record_types)').all().map((r) => r.name));
    if (!have.has('known')) {
      db.exec('ALTER TABLE record_types ADD COLUMN known INTEGER');
      console.error('harvest: record_types gained known');
    }
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
/**
 * Fill `outcome` and `denial_kind` for calls already stored, from the transcripts on disk.
 *
 * WHY A BACKFILL AND NOT --full. The incremental walk keeps a byte offset per file and never
 * revisits what it consumed, so a field added to the ingest is not retroactive. --full would do
 * it, re-reads about 12 GB, is gated behind --yes, and has already lost a race with a hook
 * harvest halfway through. Every other column added to this store took this route.
 *
 * EVERY ROW, not only the flagged ones. A call that succeeded is written `ok` rather than left
 * to be inferred from the absence of a flag, because the absence of a flag is also what a row
 * nothing ever came back from looks like, and those two are different facts.
 *
 * UPDATE, never INSERT, matched by primary key, and only where the column is still empty. It
 * cannot create a row, cannot resurrect a deleted one, and cannot touch a column it does not
 * name. `AND outcome IS NULL` also makes a second run nearly free and makes .changes a true
 * count of rows filled rather than rows visited.
 */
export async function backfillToolOutcomes(dbPath = DB_PATH, { quiet = false } = {}) {
  const db = openDb(dbPath);
  // IT ALSO REPAIRS WHAT THE INGEST COULD NOT MATCH. A tool_result can appear on an EARLIER line
  // than its tool_use: measured, 16 rows on this store. The incremental walk sees the result
  // first, setToolResult finds no row to update, and putToolCall then creates the row with
  // result_bytes and is_error NULL. Filling only the outcome would leave a row that says what it
  // turned out to be while claiming nothing ever came back from it, which breaks the invariant
  // the self-test asserts and is a contradiction on its face.
  //
  // COALESCE on those two, not assignment: a value the ingest DID record is the authority and must
  // not be overwritten by a re-read. Only a hole is filled.
  const setOutcome = db.prepare(
    'UPDATE tool_calls SET outcome = ?, denial_kind = ?,'
    + ' result_bytes = COALESCE(result_bytes, ?), is_error = COALESCE(is_error, ?)'
    + ' WHERE tool_use_id = ? AND outcome IS NULL');
  // The same high-water guard backfillAgents documents: three writers exist by design, so a
  // COUNT(*) before and after fires on a true statement about a cause this tool had nothing to
  // do with. Counting only rows that already existed is the fix.
  const highWater = db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM tool_calls').get().n;
  const existing = () => db.prepare(
    'SELECT COUNT(*) n FROM tool_calls WHERE rowid <= ?').get(highWater).n;
  const counted = (sql) => db.prepare(sql).get().n;
  const before = {
    rows: existing(),
    total: counted('SELECT COUNT(*) n FROM tool_calls'),
    filled: counted('SELECT COUNT(*) n FROM tool_calls WHERE outcome IS NOT NULL'),
  };

  const files = listTranscripts(PROJECTS);
  let scanned = 0, skipped = 0, filled = 0;
  const perFile = [];
  // BATCHED. backfillAgents writes hundreds of rows in autocommit; this writes a quarter of a
  // million, which is one fsync each without a transaction around them.
  db.exec('BEGIN');
  try {
    for (const path of files) {
      scanned++;
      let text;
      try { text = readFileSync(path, 'utf8'); } catch { skipped++; continue; }
      let here = 0;
      for (const line of text.split('\n')) {
        // A string test before any JSON.parse, like its siblings. Every record carrying a
        // toolDenialKind also carries a tool_result block, so one prefilter covers both signals.
        if (!line || !line.includes('"tool_result"')) continue;
        let d;
        try { d = JSON.parse(line); } catch { continue; }
        const content = d?.message?.content;
        if (!Array.isArray(content)) continue;
        const denial = (typeof d.toolDenialKind === 'string' && d.toolDenialKind)
          ? d.toolDenialKind : null;
        for (const blk of content) {
          if (blk?.type !== 'tool_result' || typeof blk.tool_use_id !== 'string') continue;
          const outcome = classifyResult(
            { isError: !!blk.is_error, denialKind: denial, version: d.version });
          // The same three-way shape scanBlocks uses, so a repaired row carries the byte count it
          // would have had. A non-text block contributes 0 there and contributes 0 here.
          const rc = blk.content;
          const rb = typeof rc === 'string' ? Buffer.byteLength(rc, 'utf8')
            : Array.isArray(rc) ? rc.reduce((a, x) => a + (typeof x?.text === 'string'
              ? Buffer.byteLength(x.text, 'utf8') : 0), 0)
            : rc == null ? 0 : Buffer.byteLength(JSON.stringify(rc), 'utf8');
          here += setOutcome.run(
            outcome, denial, rb, blk.is_error ? 1 : 0, blk.tool_use_id).changes;
        }
      }
      filled += here;
      if (here) perFile.push({ file: path, filled: here });
      if (scanned % 200 === 0) { db.exec('COMMIT'); db.exec('BEGIN'); }
      if (!quiet && scanned % 1000 === 0) {
        console.error(`  ${scanned}/${files.length} files, ${filled} outcomes`);
      }
    }
    db.exec('COMMIT');
  } catch (e) { db.exec('ROLLBACK'); throw e; }

  // ROWS WHOSE TRANSCRIPT IS GONE. Their outcome cannot be read, but is_error = 0 is proof of
  // success unconditionally: no denial record in the whole corpus carries a false flag. So a
  // clean row becomes ok and a flagged one becomes unclassified, which is exactly what it is.
  //
  // RESTRICTED TO FILES THAT GENUINELY DO NOT EXIST. A transient read failure, a locked file,
  // must leave the row NULL for a later run: `outcome IS NULL` is what makes this idempotent,
  // so a value stamped here is permanent and a wrong one could never be corrected.
  const orphaned = db.prepare(
    'SELECT DISTINCT file_path p FROM tool_calls WHERE outcome IS NULL AND file_path IS NOT NULL')
    .all().map((r) => r.p).filter((path) => !existsSync(path));
  const sweepOk = db.prepare('UPDATE tool_calls SET outcome = ? ' +
    'WHERE outcome IS NULL AND is_error = 0 AND file_path = ?');
  const sweepBad = db.prepare('UPDATE tool_calls SET outcome = ? ' +
    'WHERE outcome IS NULL AND is_error = 1 AND file_path = ?');
  let swept = 0;
  db.exec('BEGIN');
  for (const path of orphaned) {
    swept += sweepOk.run(TOOL_OUTCOME.OK, path).changes;
    swept += sweepBad.run(TOOL_OUTCOME.UNCLASSIFIED, path).changes;
  }
  db.exec('COMMIT');

  const after = {
    rows: existing(),
    total: counted('SELECT COUNT(*) n FROM tool_calls'),
    filled: counted('SELECT COUNT(*) n FROM tool_calls WHERE outcome IS NOT NULL'),
  };
  const report = {
    files_scanned: scanned,
    files_unreadable: skipped,
    files_that_contributed: perFile.length,
    // The rows that ALREADY EXISTED, before and after. This is only allowed to fill columns, so
    // the pair must be identical. Reported rather than assumed: "it only runs UPDATE" is a
    // claim about source code and this is a measurement.
    rows_before: before.rows,
    rows_after: after.rows,
    rows_unchanged: before.rows === after.rows,
    // What the OTHER writers did meanwhile, kept separate so it can never read as this tool's
    // doing. NOTE the one signature that looks alarming and is not: putToolCall is INSERT OR
    // REPLACE, so a concurrent harvest rewriting an existing row moves it above the high-water
    // mark, and rows_after falls while total is unchanged. That is a REPLACE, not a deletion.
    written_by_other_writers_meanwhile: after.total - before.total,
    outcome: {
      before: before.filled,
      after: after.filled,
      filled_from_transcripts: filled,
      filled_from_the_stored_flag: swept,
    },
    transcripts_no_longer_on_disk: orphaned.length,
    calls_still_without_an_outcome: counted(
      'SELECT COUNT(*) n FROM tool_calls WHERE outcome IS NULL'),
    by_outcome: db.prepare(`SELECT outcome, COUNT(*) n FROM tool_calls
                                     GROUP BY 1 ORDER BY n DESC`).all(),
    by_denial_kind: db.prepare(`SELECT denial_kind, COUNT(*) n FROM tool_calls
                                         WHERE denial_kind IS NOT NULL
                                         GROUP BY 1 ORDER BY n DESC`).all(),
    busiest_files: perFile.sort((a, c) => c.filled - a.filled).slice(0, 5),
  };
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
  return report.rows_unchanged ? 0 : 1;
}


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

// ---------------------------------------------------------------------------
// Chains: which sessions are one chat, and which session produced each row.
//
// See the session_links comment in the schema for the finding. Everything below is either pure
// (deriveLinks, deriveOwners), so the self-test can drive every rule without a store, or a thin
// layer that reads one project directory and writes what those two derived.
// ---------------------------------------------------------------------------

// Where the desktop app keeps its per-chat records. --records wins, then C4X_SESSIONS_ROOT, then
// both places a Windows install can put them: the plain %APPDATA% name and the package container
// a Store install redirects it into. On most machines those are one directory under two names and
// reading both costs a second parse of the same files. On a machine where they differ (a test
// laptop holds 1 record under one and 16 under the other) reading both is what makes every chat
// an anchor, and more anchors means fewer collapses, which is the safe direction.
export function resolveRecordsRoots(argv = [], env = process.env) {
  // AN EXPLICIT ROOT THAT DOES NOT EXIST IS AN ERROR, not an empty record set. With no records
  // every session reads as record-less, and a chat the app shows as its own can then fold into a
  // larger session that contains it. A typo must fail loudly rather than silently reshape the list.
  const explicit = (source, root) => {
    if (!existsSync(root)) throw new Error(`records root from ${source} does not exist: ${root}`);
    return [root];
  };
  const i = argv.indexOf('--records');
  if (i >= 0 && argv[i + 1]) return explicit('--records', argv[i + 1]);
  if (env.C4X_SESSIONS_ROOT) return explicit('C4X_SESSIONS_ROOT', env.C4X_SESSIONS_ROOT);
  const roots = [join(env.APPDATA || join(homedir(), 'AppData', 'Roaming'), 'Claude', 'claude-code-sessions')];
  const packages = join(env.LOCALAPPDATA || join(homedir(), 'AppData', 'Local'), 'Packages');
  let names = [];
  try { names = readdirSync(packages); } catch { /* no package store on this machine */ }
  for (const name of names) {
    if (name.startsWith('Claude')) {
      roots.push(join(packages, name, 'LocalCache', 'Roaming', 'Claude', 'claude-code-sessions'));
    }
  }
  return roots.filter((r) => existsSync(r));
}

// Map of cliSessionId to {fork, file} for every chat the app has a record of. A file without a
// cliSessionId (scheduled-tasks.json sits in the same directory) is not a chat. `fork` is the
// record field forkedFromSessionId, never a title suffix: the app appends "(fork)" to a title and
// a person can edit that away, the field stays.
export function readDesktopRecords(roots) {
  const out = new Map();
  for (const r of listDesktopRecords(roots)) out.set(r.session_id, { fork: r.fork, file: r.file });
  return out;
}

// The file name the app gives a record: local_<uuid>.json. A record named otherwise is read for
// the chain map like any other but is not remembered in desktop_records, since the marker the app
// leaves on a delete is deleted_<uuid> and there would be nothing to match it to.
const RECORD_FILE = /^local_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json$/i;
const LEDGER_RECORD = /^local_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;

/** Every record on disk as {uuid, session_id, dir, file, title, archived, fork}; uuid is null for a file not named local_<uuid>.json. */
export function listDesktopRecords(roots) {
  const out = [];
  for (const root of roots) {
    let accounts = [];
    try { accounts = readdirSync(root, { withFileTypes: true }); } catch { continue; }
    for (const a of accounts) {
      if (!a.isDirectory()) continue;
      let orgs = [];
      try { orgs = readdirSync(join(root, a.name), { withFileTypes: true }); } catch { continue; }
      for (const o of orgs) {
        if (!o.isDirectory()) continue;
        const dir = join(root, a.name, o.name);
        let files = [];
        try { files = readdirSync(dir); } catch { continue; }
        for (const f of files) {
          if (!f.endsWith('.json')) continue;
          let rec;
          try { rec = JSON.parse(readFileSync(join(dir, f), 'utf8')); } catch { continue; }
          if (!rec || typeof rec !== 'object' || typeof rec.cliSessionId !== 'string') continue;
          const named = RECORD_FILE.exec(f);
          let mtime = 0;
          try { mtime = statSync(join(dir, f)).mtimeMs; } catch { mtime = 0; }
          out.push({
            uuid: named ? named[1].toLowerCase() : null,
            session_id: rec.cliSessionId, dir, file: f, mtime,
            title: typeof rec.title === 'string' ? rec.title : null,
            archived: rec.isArchived === true ? 1 : rec.isArchived === false ? 0 : null,
            fork: typeof rec.forkedFromSessionId === 'string' && rec.forkedFromSessionId.length > 0,
          });
        }
      }
    }
  }
  return out;
}

/** `<account>/<org>` for a pair directory, the two trailing components. */
export function pairKey(dir) {
  return `${basename(dirname(dir))}/${basename(dir)}`;
}

/**
 * Which pairs share one directory: {links: Map<pair, target pair>, shared: Set<pair>}.
 *
 * A junction is a link to readdir (isSymbolicLink true, isDirectory false, measured), so the walk
 * above never reads through one and a record's `dir` is always a real directory. The one question
 * left for an owner is whether that real directory is the target of some other pair's link, and
 * the answer is read from the links' substitute names, trailing two components only: the target
 * may be spelled the virtual way or the package way and both end in the same `<account>/<org>`.
 * No realpath: inside the Store build's process tree it lands on a physical leftover
 * (docs/desktop-records.md section 6).
 */
export function sharedPairs(roots) {
  const links = new Map();
  const shared = new Set();
  for (const root of roots) {
    let accounts = [];
    try { accounts = readdirSync(root, { withFileTypes: true }); } catch { continue; }
    for (const a of accounts) {
      if (!a.isDirectory() && !a.isSymbolicLink()) continue;
      let orgs = [];
      try { orgs = readdirSync(join(root, a.name), { withFileTypes: true }); } catch { continue; }
      for (const o of orgs) {
        if (!o.isSymbolicLink()) continue;
        let target = '';
        try { target = readlinkSync(join(root, a.name, o.name)); } catch { continue; }
        const parts = target.split(/[\\/]+/).filter((p) => p && p !== '?' && p !== '??');
        if (parts.length < 2) continue;
        const to = `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
        const from = `${a.name}/${o.name}`;
        links.set(from, to);
        shared.add(from);
        shared.add(to);
      }
    }
  }
  return { links, shared };
}

/**
 * The pair the desktop app is signed in as, the way c4x/appstate.py desktop_pair reads it:
 * config.json's lastKnownAccountUuid decides the account; the organisation is the newest listed
 * record's under that account, else the newest sample in plan-usage-history.json. Under sharing
 * every org of an account lists the same records, so the org is best effort and the account is
 * not. `switched_at` is config.json's mtime: the app rewrites it at the switch, so a switch made
 * while no session ran is still dated. Null when no root has a config naming an account.
 */
export function signedInPair(roots, listed = []) {
  for (const root of roots) {
    const appdata = dirname(root);
    const configPath = join(appdata, 'config.json');
    let account = null;
    let switchedAt = null;
    try {
      const cfg = JSON.parse(readFileSync(configPath, 'utf8'));
      if (cfg && typeof cfg.lastKnownAccountUuid === 'string' && cfg.lastKnownAccountUuid) account = cfg.lastKnownAccountUuid;
      switchedAt = new Date(statSync(configPath).mtimeMs).toISOString();
    } catch { continue; }
    if (!account) continue;
    let historyOrg = null;
    try {
      const h = JSON.parse(readFileSync(join(appdata, 'plan-usage-history.json'), 'utf8'));
      const samples = Array.isArray(h && h.samples) ? h.samples.filter((s) => s && typeof s === 'object' && s.org) : [];
      if (samples.length) historyOrg = String(samples.reduce((a, b) => ((Number(b.t) || 0) > (Number(a.t) || 0) ? b : a)).org);
    } catch { historyOrg = null; }
    let newest = null;
    for (const r of listed) {
      if (basename(dirname(r.dir)) !== account) continue;
      if (!newest || (r.mtime || 0) > (newest.mtime || 0)) newest = r;
    }
    const org = newest ? basename(newest.dir) : historyOrg;
    return {
      account, org, switched_at: switchedAt,
      source: newest ? 'config.json and the newest record under the account'
        : historyOrg ? 'config.json and plan-usage-history.json' : 'config.json',
    };
  }
  return null;
}

/** {uuid: {account, org}} from the newest sharing backup's manifest, the pair each record was filed under before sharing; empty without one. */
export function manifestOwners(backupsDir) {
  let stamps = [];
  try { stamps = readdirSync(backupsDir).filter((n) => /^\d{14,20}$/.test(n)).sort(); } catch { return new Map(); }
  const out = new Map();
  for (let i = stamps.length - 1; i >= 0; i--) {
    let manifest;
    try { manifest = JSON.parse(readFileSync(join(backupsDir, stamps[i], 'manifest.json'), 'utf8')); } catch { continue; }
    for (const f of (manifest && Array.isArray(manifest.files)) ? manifest.files : []) {
      const parts = String(f && f.rel ? f.rel : '').split(/[\\/]+/).filter(Boolean);
      if (parts.length !== 3) continue;
      const named = RECORD_FILE.exec(parts[2]);
      if (named) out.set(named[1].toLowerCase(), { account: parts[0], org: parts[1] });
    }
    return out;
  }
  return out;
}

/** Append the signed-in pair to account_log when the account differs from the newest row. Returns true when a row was written. */
export function logAccount(db, pair, now) {
  if (!pair || !pair.account) return false;
  const last = db.prepare('SELECT account FROM account_log ORDER BY seen_at DESC, rowid DESC LIMIT 1').get();
  if (last && last.account === pair.account) return false;
  db.prepare('INSERT INTO account_log (seen_at, switched_at, account, org, source) VALUES (?, ?, ?, ?, ?)')
    .run(now, pair.switched_at ?? null, pair.account, pair.org ?? null, pair.source ?? null);
  return true;
}

/**
 * When the app deleted the record `uuid` that lived in `dir`, as an ISO stamp, or null when no
 * marker says so. The marker is deleted_<uuid> beside where the record was; on a packaged install
 * the ledger's path is the writer's view of the directory (see c4x/adopt.py _resolve_record), so
 * the same <account>/<org> is also tried under every records root. Its content is the delete time
 * in epoch milliseconds (measured); anything else still means deleted, at `now`.
 */
export function markerTime(dir, uuid, now, roots = []) {
  const name = `deleted_${uuid}`;
  const places = [join(dir, name)];
  const org = basename(dir);
  const account = basename(dirname(dir));
  for (const root of roots) places.push(join(root, account, org, name));
  const path = places.find((p) => existsSync(p));
  if (!path) return null;
  let text = '';
  try { text = readFileSync(path, 'utf8').trim(); } catch { return now; }
  if (!/^\d{10,16}$/.test(text)) return now;
  const n = Number(text);
  const when = new Date(n < 1e11 ? n * 1000 : n);
  return Number.isNaN(when.getTime()) ? now : when.toISOString();
}

/**
 * Remember every record on disk, seed the ones c4x wrote from the ledger, and stamp the ones that
 * have gone: deleted_at when the app's marker says it deleted them, gone_at otherwise. Runs inside
 * the caller's transaction. Returns counts; never removes a row.
 */
export function reconcileDesktopRecords(db, roots, { ledgerPath = join(ROOT, 'data', 'adopted-records.json'),
                                                     backupsDir = join(ROOT, 'data', 'record-backups'),
                                                     now = new Date().toISOString(), listed = null,
                                                     signedIn = undefined } = {}) {
  const result = { seen: 0, ledger: 0, deleted: 0, gone: 0, returned: 0,
                   owners: { tagged: 0, unknown: 0 }, account_logged: false };
  const records = listed ?? listDesktopRecords(roots);
  // THE OWNER, DECIDED ONCE. The ledger first (c4x wrote that record into the adopting account's
  // pair, which under sharing is a stronger answer than the shared directory it now sits in),
  // then an unshared directory (the app wrote it there for that account), then the account
  // signed in now (the app writes a chat's record before its first prompt, and this runs at
  // that prompt). The ledger is read BEFORE the disk loop: an adopted record is on disk too,
  // and the disk upsert would otherwise settle the owner without it.
  let entries = [];
  try { entries = JSON.parse(readFileSync(ledgerPath, 'utf8')); } catch { entries = []; }
  if (!Array.isArray(entries)) entries = [];
  const ledgerOwner = new Map();
  for (const e of entries) {
    if (!e || typeof e !== 'object') continue;
    const named = LEDGER_RECORD.exec(String(e.record || ''));
    if (!named || typeof e.session_id !== 'string' || typeof e.path !== 'string') continue;
    const dir = dirname(e.path);
    ledgerOwner.set(named[1].toLowerCase(), { account: basename(dirname(dir)), org: basename(dir) });
  }
  const { shared } = sharedPairs(roots);
  const signed = signedIn === undefined ? signedInPair(roots, records) : signedIn;
  const ownerOf = (uuid, dir) => {
    const fromLedger = ledgerOwner.get(uuid);
    if (fromLedger) return { ...fromLedger, source: 'ledger' };
    if (!shared.has(pairKey(dir))) return { account: basename(dirname(dir)), org: basename(dir), source: 'dir' };
    if (signed && signed.account) return { account: signed.account, org: signed.org ?? null, source: 'signed-in' };
    return null;
  };
  // THE OWNER COLUMNS ARE IN THE INSERT AND NOT IN THE UPDATE, so the first answer stands: SQLite
  // discards the excluded values on conflict. `dir`, `title` and the stamps follow the disk.
  const upsert = db.prepare(`INSERT INTO desktop_records
      (record_uuid, session_id, dir, title, archived, first_seen, last_seen, gone_at, deleted_at, source,
       owner_account, owner_org, owner_source)
    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'disk', ?, ?, ?)
    ON CONFLICT(record_uuid) DO UPDATE SET session_id = excluded.session_id, dir = excluded.dir,
      title = excluded.title, archived = excluded.archived, last_seen = excluded.last_seen,
      gone_at = NULL, deleted_at = NULL, source = 'disk'`);
  const was = db.prepare('SELECT gone_at, deleted_at FROM desktop_records WHERE record_uuid = ?');
  const present = new Set();
  for (const r of records) {
    if (!r.uuid) continue;
    present.add(r.uuid);
    const before = was.get(r.uuid);
    if (before && (before.gone_at || before.deleted_at)) result.returned++;
    const owner = before ? null : ownerOf(r.uuid, r.dir);
    upsert.run(r.uuid, r.session_id, r.dir, r.title, r.archived, now, now,
               owner ? owner.account : null, owner ? owner.org : null, owner ? owner.source : null);
    result.seen++;
  }
  // THE LEDGER: the records c4x wrote, uuid and session together, so a record the app deleted
  // before this table existed still maps to its chat. On the laptop that was 19 of the 20 chats
  // deleted by hand. A row already known is left as it is.
  const seed = db.prepare(`INSERT OR IGNORE INTO desktop_records
      (record_uuid, session_id, dir, title, archived, first_seen, last_seen, gone_at, deleted_at, source,
       owner_account, owner_org, owner_source)
    VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, NULL, 'ledger', ?, ?, 'ledger')`);
  for (const e of entries) {
    if (!e || typeof e !== 'object') continue;
    const named = LEDGER_RECORD.exec(String(e.record || ''));
    if (!named || typeof e.session_id !== 'string' || typeof e.path !== 'string') continue;
    const at = typeof e.at === 'string' && e.at ? e.at : now;
    const dir = dirname(e.path);
    if (seed.run(named[1].toLowerCase(), e.session_id, dir, at, at, basename(dirname(dir)), basename(dir)).changes) result.ledger++;
  }
  // THE ROWS FROM BEFORE THE COLUMNS, and any row nothing above could answer: the ledger, then the
  // newest sharing backup's manifest (where each record was filed before sharing), then an
  // unshared directory; what none of them answers is stamped 'unknown' so the next pass has
  // nothing left to try. A row is never re-decided once stamped.
  const blank = db.prepare('SELECT record_uuid, dir FROM desktop_records WHERE owner_source IS NULL').all();
  if (blank.length) {
    const fromManifest = manifestOwners(backupsDir);
    const fill = db.prepare('UPDATE desktop_records SET owner_account = ?, owner_org = ?, owner_source = ? WHERE record_uuid = ?');
    for (const b of blank) {
      const fromLedger = ledgerOwner.get(b.record_uuid);
      const filed = fromManifest.get(b.record_uuid);
      const owner = fromLedger ? { ...fromLedger, source: 'ledger' }
        : filed ? { ...filed, source: 'manifest' }
        : !shared.has(pairKey(b.dir)) ? { account: basename(dirname(b.dir)), org: basename(b.dir), source: 'dir' }
        : null;
      if (owner) {
        fill.run(owner.account, owner.org, owner.source, b.record_uuid);
        result.owners.tagged++;
      } else {
        fill.run(null, null, 'unknown', b.record_uuid);
        result.owners.unknown++;
      }
    }
  }
  result.account_logged = logAccount(db, signed, now);
  // EVERYTHING NOT ON DISK NOW. A marker says the app deleted it; nothing says it merely went.
  const rows = db.prepare('SELECT record_uuid, dir, gone_at, deleted_at FROM desktop_records').all();
  const stamp = db.prepare(`UPDATE desktop_records
    SET gone_at = COALESCE(gone_at, ?), deleted_at = COALESCE(deleted_at, ?) WHERE record_uuid = ?`);
  for (const row of rows) {
    if (present.has(row.record_uuid)) continue;
    const when = markerTime(row.dir, row.record_uuid, now, roots);
    if (when && !row.deleted_at) result.deleted++;
    if (!when && !row.gone_at) result.gone++;
    stamp.run(now, when, row.record_uuid);
  }
  return result;
}

const TRANSCRIPT_NAME = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$/i;
// How many of a transcript's leading uuids are kept, in file order, to recognise a CONTINUATION:
// a resume taken after a compaction copies only the post-compaction tail, so it is never a
// superset of what it continues, but its first records are all inside it.
const FIRST_UUIDS = 20;
const CONTINUATION_MIN = 5;
// The share of a candidate's copy that must carry ITS OWN sessionId for it to be a resume rather
// than a fork. Measured 1.0 on every resume and 0.0 on every fork (17 links); 0.9 leaves room for
// a stray line without letting a fork through.
const NATIVE_SHARE = 0.9;

// Every record uuid and tool_use id in each transcript DIRECTLY inside one project directory,
// with which of them the file wrote ITSELF, the first few in file order, the working directory,
// and the earliest timestamp. Streamed, never readFileSync: the largest transcript on the author's
// machine is over 100 MB. A string test runs before JSON.parse, but the parse is not skipped after
// it: a tool result can carry another record's JSON in its text, so the first "uuid" in a line is
// not always the line's own. Subagent transcripts live under <session>/ and are not chain members,
// which the name test enforces.
//
// NATIVE MATTERS. A desktop fork is a verbatim copy: the copied lines keep the parent's sessionId
// and the parent's timestamps, so the file's own session id appears only on the lines it wrote
// itself. A resume is different: it prepends its own timestamped head records and rewrites the
// copied lines' sessionId to its own. `native` holds the uuids whose line names this file's
// session, which is the one signal that tells a fork's copy from the record that produced it.
export async function transcriptIndex(dir, { excludedCwds = null } = {}) {
  let entries = [];
  try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return []; }
  const slug = dir.split(/[\\/]/).filter(Boolean).pop() ?? '';
  const out = [];
  for (const e of entries) {
    if (!e.isFile() || !TRANSCRIPT_NAME.test(e.name)) continue;
    const id = e.name.slice(0, -6).toLowerCase();
    const path = join(dir, e.name);
    const uuids = new Set(), toolIds = new Set(), native = new Set(), nativeTools = new Set();
    // The session another file's line names for a record that is not this file's own: a fork
    // copies its parent's lines verbatim, so these say which session the copy came from.
    const foreign = new Map(), foreignTools = new Map(), foreignFrom = new Set();
    const firstUuids = [];
    let firstTs = null, cwd = null, homeCwd = null, lastCwd = null, excluded = false;
    const rl = createInterface({ input: createReadStream(path, { highWaterMark: 1 << 20 }), crlfDelay: Infinity });
    for await (const line of rl) {
      const wanted = line.includes('"uuid":"') || line.includes('"toolu_')
        || (firstTs === null && line.includes('"timestamp":"'))
        || (homeCwd === null && line.includes('"cwd":"'));
      if (!wanted) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      if (!d || typeof d !== 'object') continue;
      if (typeof d.cwd === 'string' && d.cwd) {
        if (cwd === null) {
          cwd = d.cwd;
          // A project the user asked to stop capturing is not read past its first cwd record,
          // and contributes nothing: no link, no owner. Same rule as Harvest.file.
          if (excludedCwds && excludedCwds.has(cwd)) { excluded = true; rl.close(); break; }
        }
        lastCwd = d.cwd;
        // The directory the file LIVES in: the first cwd whose slug is this directory's name.
        // A session that changed directory keeps its home; a fork of a parent that had changed
        // directory begins with the parent's old lines and finds its home further down.
        if (homeCwd === null && slugOf(d.cwd) === slug) homeCwd = d.cwd;
      }
      if (firstTs === null && typeof d.timestamp === 'string') firstTs = d.timestamp;
      const said = typeof d.sessionId === 'string' ? d.sessionId.toLowerCase() : null;
      const own = said === id;
      if (typeof d.uuid === 'string') {
        uuids.add(d.uuid);
        if (own) native.add(d.uuid);
        else if (said) { foreign.set(d.uuid, said); foreignFrom.add(said); }
        if (firstUuids.length < FIRST_UUIDS) firstUuids.push(d.uuid);
      }
      const content = d.message?.content;
      if (Array.isArray(content)) {
        for (const b of content) {
          if (b?.type === 'tool_use' && typeof b.id === 'string') {
            toolIds.add(b.id);
            if (own) nativeTools.add(b.id);
            else if (said) foreignTools.set(b.id, said);
          }
        }
      }
    }
    if (excluded) continue;
    // `homeCwd` SEPARATELY, and not only folded into `cwd`. A transcript that an import moved to
    // another machine's directory still names the source directory on every line, so it HAS no home
    // cwd here and `cwd` falls back to the last one seen, which is the source. The identity repair
    // and the link gate both need to tell "the transcript proves where it lives" from "this is a
    // guess", because acting on the guess moves an imported project back to the exporter's path.
    out.push({ id, path, slug, cwd: homeCwd ?? lastCwd, homeCwd, firstCwd: cwd, uuids, toolIds, native, nativeTools,
               foreign, foreignTools, foreignFrom, firstUuids, firstTs: firstTs ?? '9999' });
  }
  return out;
}

// Directory entries, or nothing. A session directory is optional (82 of 1,135 sessions have one
// here) and every level below it is too, so an absent one is the normal case and not a failure.
function listDir(dir) {
  try { return readdirSync(dir, { withFileTypes: true }); } catch { return []; }
}

// The directory name Claude Code gives a working directory under ~/.claude/projects: every
// character that is not a letter or a digit becomes a hyphen. The same rule as c4x/appstate.py
// slug_for, and pinned against it by tests/test_appstate.py::TestTheSlug's inputs below.
export function slugOf(cwd) {
  return cwd == null ? null : String(cwd).replace(/[^A-Za-z0-9]/g, '-');
}

// SQL functions the upserts use. Registering twice on one connection is harmless in node:sqlite,
// and every Harvest registers on its own connection, so the self-test's scratch stores get them too.
export function registerFunctions(db) {
  db.function('slug_of', { deterministic: true }, slugOf);
}

// A strict total order over transcripts, so a link can only point from an earlier session to a
// later one and no chain can ever loop: the first timestamp, then the size, then the id. A resume
// starts later than what it resumed (its head records are new); a fork copies its parent's first
// line, timestamp included, and is larger.
function transcriptOrder(f) {
  return [f.firstTs, f.uuids.size, f.id];
}

// How many of `ids` are in `inside`, stopping at `cap` once that many are found.
function countIn(ids, inside, cap = Infinity) {
  let n = 0;
  for (const u of ids) { if (inside.has(u) && ++n >= cap) break; }
  return n;
}

// How many of `firstUuids`, in file order, sit inside `inside` before the first one that does not.
// A tail copy's own work follows the copied tail, so the run, not the whole list, is the signal.
function leadingRun(firstUuids, inside) {
  let n = 0;
  for (const u of firstUuids) { if (!inside.has(u)) break; n++; }
  return n;
}

// Lexicographic compare of two sort keys made of numbers and strings.
function keyCompare(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

// PURE. Which record-less sessions were superseded by which, and what each chain's head is.
//
// Two shapes of successor. A COPY: B is larger and holds at least `threshold` of A's uuids, which
// is what an ordinary resume produces. A CONTINUATION: B's first records are all inside A, which
// is what a resume taken after a compaction produces, since it copies only the post-compaction
// tail and is never a superset of what it continues. Both are only accepted between sessions of
// one working directory, so a chain can never span two projects, and only from an earlier
// transcript to a later one (`transcriptOrder`), so a chain can never loop: a small resume that
// did little of its own used to be "contained" by the session it resumed, and linked backwards.
//
// THE COPY MUST BE THE RESUME'S OWN. A resume rewrites every copied line to its own sessionId; a
// desktop fork copies the parent verbatim, sessionId and all. Measured over the 17 links on one
// store: every true resume held its predecessor's records natively, 100 of 100 percent, and every
// fork held them natively 0 of 100 percent. So a candidate whose copy is not native is a fork of
// A, another chat, and never A's successor, whatever its overlap. The first rule had no such test
// and folded a parent chat's history into its fork whenever the parent's own successor was a
// post-compaction resume (a continuation, which copies before continuations outranked).
//
// AND IT MUST NOT COME THROUGH A FORK. Resume the fork and the rewrite launders the parent's
// lines: the fork's resume holds them natively too. The fork's own first transcript is the
// evidence, record by record: it holds the parent's lines verbatim, so a record that A and B share
// and that some fork's first transcript holds verbatim, where B holds that fork's OWN records and
// A does not, reached B through the fork. Five such records refuse the candidate. A fork that did
// no work of its own before it was resumed leaves no such evidence; then the rank below (a chat's
// own record holder before a fork's) is what decides, and a parent chat with no record holder
// among the candidates can still lose its earlier sessions to the fork's resume. Stated rather
// than hidden.
//
// AND IT MUST HOLD SOMETHING A WROTE ITSELF. A fork's first session shares its parent's history
// with every later session of the parent chat, and one of those, taken after a compaction, can
// hold more of that history than the fork's own resume holds of the fork. The parent's session
// holds none of the fork's own records; the fork's resume holds its latest ones (a resume copies
// from the last compaction to the end, and the summary written there is the fork's own). So a
// candidate holding none of A's native records is refused whenever A has any.
//
// Among a session's candidates: copies before continuations; then the HIGHEST overlap, because a
// fork's own first session is contained whole in its resume and only nearly in the parent chat it
// was forked from, and "nearly" must lose; then the kind, a record holder that is not a fork, then
// a record-less session, then a fork's record holder; then the smallest. A session with a record
// of its own never links: it is a chat, and a fork copies history too. The head is reached by
// following next until a session with no link, and the seen set below is belt and braces.
export function deriveLinks(index, records, threshold = 0.9, storedCwds = null) {
  // WHICH DIRECTORY A TRANSCRIPT BELONGS TO, for the same-directory gate below. The transcript's
  // own home cwd when it has one, else what the store already holds for that session, else the last
  // cwd the file names. The middle term is what an import writes: its transcript still names the
  // source machine's directory, so without it a chat resumed after a move never links to the
  // session it resumed, and the chat stays two rows on the page for good.
  const cwdOf = (f) => f.homeCwd ?? (storedCwds && storedCwds.get(f.id)) ?? f.cwd;
  const kindOf = (id) => {
    const r = records.get(id);
    return r ? (r.fork ? 'fork' : 'record') : 'none';
  };
  const rank = { record: 0, none: 1, fork: 2 };
  // Which fork transcripts hold each record verbatim, and whether a transcript descends from a
  // fork (holds the fork's own first records). Both are what the lineage rule reads.
  const forkFiles = index.filter((f) => f.foreign.size > 0);
  const heldByForks = new Map();
  for (const f of forkFiles) {
    for (const u of f.foreign.keys()) {
      if (!heldByForks.has(u)) heldByForks.set(u, []);
      heldByForks.get(u).push(f);
    }
  }
  const descendsMemo = new Map();
  const descends = (x, f0) => {
    const key = x.id + '|' + f0.id;
    if (!descendsMemo.has(key)) {
      const need = Math.min(CONTINUATION_MIN, f0.native.size);
      descendsMemo.set(key, need > 0 && countIn(f0.native, x.uuids, need) >= need);
    }
    return descendsMemo.get(key);
  };
  const next = new Map();
  const linkStats = { anchors_kept: 0, candidates_considered: 0, continuations: 0,
                  refused_foreign: 0, refused_lineage: 0, refused_cousin: 0 };
  for (const a of index) {
    if (a.uuids.size === 0) continue;
    let best = null;
    const aCwd = cwdOf(a);
    for (const b of index) {
      if (b === a) continue;
      const bCwd = cwdOf(b);
      if (aCwd && bCwd && aCwd !== bCwd) continue;
      if (keyCompare(transcriptOrder(b), transcriptOrder(a)) <= 0) continue;
      let shared = 0, sharedNative = 0, sharedOwn = 0, viaFork = 0;
      for (const u of a.uuids) {
        if (!b.uuids.has(u)) continue;
        shared++;
        if (b.native.has(u)) sharedNative++;
        if (a.native.has(u)) sharedOwn++;
        const forks = heldByForks.get(u);
        if (forks && forks.some((f0) => f0 !== a && f0 !== b && descends(b, f0) && !descends(a, f0))) viaFork++;
      }
      if (!shared) continue;
      const overlap = shared / a.uuids.size;
      let how = null;
      if (b.uuids.size > a.uuids.size && overlap >= threshold) how = 'copy';
      else if (leadingRun(b.firstUuids, a.uuids) >= CONTINUATION_MIN) how = 'continuation';
      if (!how) continue;
      if (sharedNative < Math.ceil(shared * NATIVE_SHARE)) { linkStats.refused_foreign++; continue; }
      if (viaFork >= CONTINUATION_MIN) { linkStats.refused_lineage++; continue; }
      if (a.native.size > 0 && sharedOwn === 0) { linkStats.refused_cousin++; continue; }
      linkStats.candidates_considered++;
      const kind = kindOf(b.id);
      const cand = { b, shared, overlap, how, kind,
                     key: [how === 'copy' ? 0 : 1, -Math.round(overlap * 100), rank[kind], b.uuids.size, b.id] };
      if (!best || keyCompare(cand.key, best.key) < 0) best = cand;
    }
    if (!best) continue;
    if (records.has(a.id)) { linkStats.anchors_kept++; continue; }
    if (best.how === 'continuation') linkStats.continuations++;
    next.set(a.id, best);
  }
  const byId = new Map(index.map((f) => [f.id, f]));
  const rows = [];
  for (const [id, n] of next) {
    let head = n.b.id;
    const seen = new Set([id]);
    while (next.has(head) && !seen.has(head)) { seen.add(head); head = next.get(head).b.id; }
    // Unreachable under the order above, and the schema's CHECK would refuse the row anyway;
    // kept so a future change to the order cannot write a session as its own head.
    if (head === id) continue;
    rows.push({
      session_id: id, head_id: head, next_id: n.b.id, head_kind: n.kind, overlap: n.overlap,
      prefix_uuids: byId.get(id).uuids.size, next_uuids: n.b.uuids.size, shared_uuids: n.shared,
    });
  }
  return { rows, stats: linkStats };
}

// PURE. The session that PRODUCED each record that appears in more than one transcript.
//
// THE LINE SAYS WHO WROTE IT, when it can. A desktop fork copies the parent verbatim, sessionId
// and timestamps included, so the copied lines still name the parent and only the fork's own
// lines name the fork: a record that is native to exactly one of the files it appears in belongs
// there, whatever the files' timestamps say. That was the whole of the first defect this function
// shipped with: parent and fork tied on first timestamp and the filename decided, which on the
// author's machine handed 17,472 of a chat's records to one fork and 10,441 to another.
//
// When the line cannot say (a resume rewrites the copied lines to its own sessionId, so the record
// is native in both files) the earliest-starting transcript produced it, since a copy can only
// land in a session that started later; a smaller transcript breaks a tie before the path does,
// because a strict prefix is the older one. Ids that appear in exactly one transcript are not
// returned: nothing about them can be wrong.
//
// A PRODUCER WHOSE TRANSCRIPT IS GONE STILL OWNS ITS RECORDS. A fork's copy names it, so when the
// copies name one session that is not among the files here, that session produced them: the store
// already holds them under it, from when its transcript existed, and no file here is a better
// claimant, not even a resume of the fork that has since rewritten them. The first rule handed
// them to the earliest file present, which for two forks of a deleted parent moved every one of
// the parent's rows into a fork. Copies that name two absent sessions cannot be settled from
// here, and those records are left where they are.
export function deriveOwners(index) {
  const ordered = [...index].sort((a, b) => (
    a.firstTs < b.firstTs ? -1 : a.firstTs > b.firstTs ? 1
      : a.uuids.size !== b.uuids.size ? a.uuids.size - b.uuids.size
        : a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  const here = new Set(index.map((f) => f.id));
  const claim = (idsOf, nativeOf, foreignOf) => {
    const first = new Map(), firstNative = new Map(), shared = new Set(), named = new Map();
    for (const f of ordered) {
      const mine = nativeOf(f), theirs = foreignOf(f);
      for (const u of idsOf(f)) {
        if (first.has(u)) shared.add(u); else first.set(u, f.id);
        if (mine.has(u) && !firstNative.has(u)) firstNative.set(u, f.id);
        const s = theirs.get(u);
        if (s && !here.has(s)) {
          if (!named.has(u)) named.set(u, new Set());
          named.get(u).add(s);
        }
      }
    }
    const owner = new Map();
    for (const u of shared) {
      const absent = named.get(u);
      if (absent && absent.size === 1) owner.set(u, [...absent][0]);
      else if (!absent) owner.set(u, firstNative.get(u) ?? first.get(u));
    }
    return owner;
  };
  return {
    owner: claim((f) => f.uuids, (f) => f.native, (f) => f.foreign),
    toolOwner: claim((f) => f.toolIds, (f) => f.nativeTools, (f) => f.foreignTools),
  };
}

// One project directory: derive its links and owners, then write them. The CALLER owns the
// transaction. Links for the directory's sessions are replaced wholesale, so a chain that grew
// since the last pass is re-pointed at its new head rather than left with a stale one, which is
// the one state a reader cannot recover from. With write:false nothing is written and the moves
// are counted instead, which is what --dry-run reports.
export async function reconcileDirectory(db, dir, records, { write = true, threshold = 0.9, method = 'backfill-chains',
                                                            excludedCwds = null } = {}) {
  const index = await transcriptIndex(dir, { excludedCwds });
  const result = { dir, files: index.length, links: 0, anchors_kept: 0, rows: [],
                   moved: { turns: 0, messages: 0, compactions: 0, tool_calls: 0 },
                   repaired: { cwd: 0, project_slug: 0, transcript_path: 0 } };
  if (!index.length) return result;
  // WHAT THE STORE ALREADY HOLDS, read once and used twice: by the link gate, and by the repair
  // below. An import writes the destination directory into the session row and cannot rewrite the
  // transcript, whose lines still name the source; the row is the only evidence of the move.
  const identity = db.prepare('SELECT cwd, project_slug, transcript_path FROM sessions WHERE session_id = ?');
  const stored = new Map();
  for (const f of index) {
    const cur = identity.get(f.id);
    if (cur) stored.set(f.id, cur);
  }
  const storedCwds = new Map();
  for (const [id, cur] of stored) if (cur.cwd) storedCwds.set(id, cur.cwd);
  const { rows, stats: linkStats } = deriveLinks(index, records, threshold, storedCwds);
  const { owner, toolOwner } = deriveOwners(index);
  result.anchors_kept = linkStats.anchors_kept;
  result.rows = rows;
  result.links = rows.length;
  // THE SESSION ROW'S IDENTITY, from its own transcript: the directory it lives in (`cwd` by the
  // same rule the upsert applies, `project_slug` the directory's name) and its own top-level
  // path. Rows written before those rules exist, and rows a subagent file reached first, are
  // put right here, in the same pass that puts the links right.
  const fixes = [];
  for (const f of index) {
    const cur = stored.get(f.id);
    if (!cur) continue;
    // THE CWD IS REPAIRED FROM THE TRANSCRIPT'S OWN HOME, AND FROM NOTHING ELSE. With no home cwd
    // the file cannot say where it lives, and the stored value is kept whenever it agrees with the
    // directory the file is in, which is the same precedence the putSession upsert applies. This
    // read `f.cwd`, which falls back to the last cwd the file names: after an import that is the
    // EXPORTING machine's path, so the first harvest pass over the destination moved the project
    // back and the page showed a directory that does not exist here.
    const keepStored = cur.cwd && slugOf(cur.cwd) === f.slug;
    const wantCwd = f.homeCwd ?? (keepStored ? cur.cwd : (f.cwd ?? cur.cwd));
    const want = { cwd: wantCwd, project_slug: f.slug, transcript_path: f.path };
    for (const col of Object.keys(want)) {
      if (want[col] != null && cur[col] !== want[col]) { result.repaired[col]++; fixes.push([col, want[col], f.id]); }
    }
  }
  if (!write) {
    const count = (table, key, map) => {
      const stmt = db.prepare(`SELECT session_id FROM ${table} WHERE ${key} = ?`);
      let n = 0;
      for (const [id, sid] of map) { const r = stmt.get(id); if (r && r.session_id !== sid) n++; }
      return n;
    };
    result.moved.turns = count('turns', 'uuid', owner);
    result.moved.messages = count('messages', 'uuid', owner);
    result.moved.compactions = count('compactions', 'uuid', owner);
    result.moved.tool_calls = count('tool_calls', 'tool_use_id', toolOwner);
    return result;
  }
  for (const [col, value, id] of fixes) {
    db.prepare(`UPDATE sessions SET ${col} = ? WHERE session_id = ?`).run(value, id);
  }
  // By session AND by head, so a link left behind by a transcript that has since gone from disk
  // (its session no longer in the index, its head still here) is removed too. Links never cross a
  // directory, so every row naming one of these sessions is this directory's to replace.
  const del = db.prepare('DELETE FROM session_links WHERE session_id = ? OR head_id = ?');
  const ins = db.prepare(`INSERT INTO session_links
    (session_id,head_id,next_id,head_kind,overlap,prefix_uuids,next_uuids,shared_uuids,method,linked_at)
    VALUES (?,?,?,?,?,?,?,?,?,?)`);
  const mover = (table, key) => db.prepare(
    `UPDATE ${table} SET session_id = ? WHERE ${key} = ? AND session_id IS NOT ?`);
  const mv = { turns: mover('turns', 'uuid'), messages: mover('messages', 'uuid'),
               compactions: mover('compactions', 'uuid'), tool_calls: mover('tool_calls', 'tool_use_id') };
  const now = new Date().toISOString();
  for (const f of index) del.run(f.id, f.id);
  for (const r of rows) {
    ins.run(r.session_id, r.head_id, r.next_id, r.head_kind, r.overlap,
            r.prefix_uuids, r.next_uuids, r.shared_uuids, method, now);
  }
  for (const [u, sid] of owner) {
    result.moved.turns += mv.turns.run(sid, u, sid).changes;
    result.moved.messages += mv.messages.run(sid, u, sid).changes;
    result.moved.compactions += mv.compactions.run(sid, u, sid).changes;
  }
  for (const [t, sid] of toolOwner) result.moved.tool_calls += mv.tool_calls.run(sid, t, sid).changes;
  return result;
}

// Every project directory. Row COUNTS must not change: this moves session_id values and writes
// links, nothing else, and the high-water guard reports it the way backfillAgents does.
/**
 * Every session directory, for the stores harvested before the sidecar pass existed.
 *
 * ROW COUNTS MUST NOT CHANGE. This reads small JSON files beside the transcripts and writes
 * agent_runs, workflow_runs and files; it must not touch a turn, a message or a session, and the
 * high-water guard says so in the report the way backfillAgents and backfillChains do.
 */
/**
 * The plans and task notifications inside transcripts this store already read.
 *
 * WITHOUT THIS THE PANEL LIES ON EVERY EXISTING STORE, and the lie is the confident kind. Both
 * writers ride the incremental byte offsets, so they only ever see bytes appended AFTER they
 * shipped: measured on this machine, 291 `ExitPlanMode` calls and 27 `task_status` attachments are
 * on disk and the store held 1 plan and 0 task events. The panel's `harvested` block cannot tell
 * that apart from a chat that planned nothing, because the tables exist and are simply empty, so
 * it would have answered "This chat wrote no plan" over a plan the reader wrote themselves.
 *
 * NOT `--full`. It re-reads transcripts, as `backfillToolOutcomes` does, but writes only these two
 * tables and touches no offset, so a later ordinary harvest is unaffected by having run it.
 */
/**
 * What each transcript backfill looks for and what it writes. One scanner below runs any of them:
 * `prefilters` are exact substrings tested before any JSON.parse, `handle` is given the parsed
 * record, `tables` are what the report counts, `counters` map report names to Harvest stats.
 */
export const WORK_SPEC = {
  label: 'work',
  tables: ['plans', 'task_events'],
  counters: { plans: 'plans', task_events: 'taskEvents' },
  prefilters: ['"ExitPlanMode"', '"task_status"'],
  handle(h, d, path, lineNo, line) {
    if (line.includes('"task_status"') && d?.type === 'attachment') h.taskEvent(d, path, lineNo);
    if (!line.includes('"ExitPlanMode"')) return;
    const content = d?.message?.content;
    if (!Array.isArray(content)) return;
    for (const blk of content) {
      if (blk?.type === 'tool_use' && blk.name === 'ExitPlanMode') {
        h.plan(d, blk.id, blk.input, path, lineNo);
      }
    }
  },
};

export const CHANGES_SPEC = {
  label: 'changes',
  tables: ['changes'],
  counters: { changes: 'changes', patches: 'changePatches' },
  // EXACT, because the transcript is compact JSON: "name":"Edit" with no space. A looser test such
  // as "Edit" alone would parse every result that merely mentions the word.
  prefilters: ['"name":"Edit"', '"name":"Write"', '"structuredPatch"'],
  handle(h, d, path, lineNo) {
    const content = d?.message?.content;
    if (!Array.isArray(content)) return;
    for (const blk of content) {
      if (blk?.type === 'tool_use' && (blk.name === 'Edit' || blk.name === 'Write')) {
        h.change(d, blk.id, blk.name, blk.input, path, lineNo);
      } else if (blk?.type === 'tool_result' && typeof blk.tool_use_id === 'string') {
        const r = d.toolUseResult;
        if (r && typeof r === 'object' && Array.isArray(r.structuredPatch)) {
          h.changeResult(d, blk.tool_use_id, r, path, lineNo);
        }
      }
    }
  },
};

/**
 * Rows inside transcripts this store already read to the end.
 *
 * Both kinds of writer ride the incremental byte offsets, so they only ever see bytes appended
 * after they shipped, and a store harvested before them holds nothing for the transcripts it has
 * already consumed. Not `--full`: this re-reads transcripts the way `backfillToolOutcomes` does,
 * writes only the spec's tables, and touches no offset, so a later ordinary harvest is unaffected.
 */
export async function backfillRecords(dbPath, { quiet = false, write = true, projects = PROJECTS,
                                                batch = 200 } = {}, spec) {
  if (!existsSync(dbPath)) { if (!quiet) console.error(`no store at ${dbPath}`); return null; }
  const t0 = Date.now();
  const db = openDb(dbPath);
  const counted = (sql) => db.prepare(sql).get().n;
  const snapshot = () => {
    const out = { turns: counted('SELECT COUNT(*) n FROM turns'),
                  messages: counted('SELECT COUNT(*) n FROM messages'),
                  files: counted('SELECT COUNT(*) n FROM files') };
    for (const t of spec.tables) out[t] = counted(`SELECT COUNT(*) n FROM ${t}`);
    return out;
  };
  const before = snapshot();
  const h = new Harvest(db);
  const files = listTranscripts(projects);
  let scanned = 0, skipped = 0;
  db.exec('BEGIN');
  try {
    for (const path of files) {
      scanned++;
      let text;
      try { text = readFileSync(path, 'utf8'); } catch { skipped++; continue; }
      let lineNo = 0;
      for (const line of text.split('\n')) {
        lineNo++;
        // A STRING TEST BEFORE ANY JSON.parse, the rule every sibling backfill follows: parsing
        // 475,805 records to find 318 of them is the difference between minutes and an hour.
        if (!line) continue;
        if (!spec.prefilters.some((needle) => line.includes(needle))) continue;
        let d;
        try { d = JSON.parse(line); } catch { continue; }
        spec.handle(h, d, path, lineNo, line);
      }
      // ONLY WHEN WRITING, and that word is the whole of it. Copied from backfillToolOutcomes,
      // which has no dry run, this committed every `batch` files regardless: the first --dry-run
      // here reported 279 rows it would add and left 253 of them in the store, because 8,775 files
      // crossed the boundary 43 times. A ROLLBACK can only undo the last partial batch.
      if (write && scanned % batch === 0) { db.exec('COMMIT'); db.exec('BEGIN'); }
      if (!quiet && scanned % 500 === 0) {
        const first = Object.values(spec.counters)[0];
        process.stderr.write(`  ${scanned}/${files.length} transcripts, ${h.stats[first]} ${spec.label}
`);
      }
    }
    // Counted inside the transaction, so a dry run reports the figures it would have written
    // rather than the store's previous contents. Same rule, and same reason, as backfillSidecars.
    const after = snapshot();
    if (write) db.exec('COMMIT'); else db.exec('ROLLBACK');
    const report = { transcripts: files.length, scanned, unreadable: skipped };
    for (const [name, stat] of Object.entries(spec.counters)) report[`${name}_seen`] = h.stats[stat];
    for (const t of spec.tables) { report[t] = after[t]; report[`${t}_added`] = after[t] - before[t]; }
    // NOTHING ELSE MOVES. This reads transcripts and writes the spec's tables; a changed turn,
    // message or file row would mean it did something it was never asked to do.
    report.rows_unchanged = before.turns === after.turns && before.messages === after.messages
      && before.files === after.files;
    report.wrote = !!write;
    report.ms = Date.now() - t0;
    if (!quiet) console.log(JSON.stringify(report, null, 2));
    db.close();
    return report;
  } catch (e) {
    try { db.exec('ROLLBACK'); } catch { /* nothing left to roll back */ }
    db.close();
    throw e;
  }
}

export const backfillWork = (dbPath = DB_PATH, opts = {}) => backfillRecords(dbPath, opts, WORK_SPEC);
export const backfillChanges = (dbPath = DB_PATH, opts = {}) => backfillRecords(dbPath, opts, CHANGES_SPEC);

export async function backfillSidecars(dbPath = DB_PATH, { quiet = false, write = true,
                                                           projects = PROJECTS } = {}) {
  if (!existsSync(dbPath)) { if (!quiet) console.error(`no store at ${dbPath}`); return null; }
  const t0 = Date.now();
  const db = openDb(dbPath);
  const before = {
    turns: db.prepare('SELECT COUNT(*) n FROM turns').get().n,
    messages: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    sessions: db.prepare('SELECT COUNT(*) n FROM sessions').get().n,
  };
  const h = new Harvest(db);
  const dirs = [];
  for (const slug of listDir(projects)) {
    if (!slug.isDirectory()) continue;
    for (const entry of listDir(join(projects, slug.name))) {
      if (!entry.isDirectory()) continue;
      if (!TRANSCRIPT_NAME.test(entry.name + '.jsonl')) continue;
      dirs.push(join(projects, slug.name, entry.name));
    }
  }
  // ALWAYS A TRANSACTION, AND THE DRY RUN IS THE REASON. `sidecars` writes unconditionally, so
  // without a BEGIN node:sqlite autocommits every statement and `--dry-run` would modify the store
  // it was asked not to touch, then fail on a ROLLBACK with nothing to roll back. Measured: the
  // first run of this against a copy wrote rows for 120 session directories before it crashed.
  // The sibling `backfillChains` needs no such pairing because it passes `write` down into
  // `reconcileDirectory` and that function skips the writes; this one cannot.
  db.exec('BEGIN');
  let n = 0;
  try {
    for (const dir of dirs) {
      h.sidecars(dir);
      if (!quiet && ++n % 100 === 0) process.stderr.write(`  ${n}/${dirs.length} session directories
`);
    }
  } catch (e) {
    try { db.exec('ROLLBACK'); } catch { /* nothing left to roll back */ }
    db.close();
    throw e;
  }
  // COUNTED INSIDE THE TRANSACTION, which is what makes the dry run worth running. Read after a
  // ROLLBACK these are the store's PREVIOUS contents, so a dry run against a store with no
  // sidecars would report `agent_runs: 0` beside `agent_runs_written: 7432` and the figure the run
  // exists to produce would be the one it cannot show. The guard counts are read here too, and
  // inside the transaction they are stricter: they prove the pass touched no turn or message even
  // before anything was undone.
  const after = {
    turns: db.prepare('SELECT COUNT(*) n FROM turns').get().n,
    messages: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    sessions: db.prepare('SELECT COUNT(*) n FROM sessions').get().n,
  };
  const report = {
    directories: dirs.length,
    sidecars_seen: h.stats.sidecarsSeen, sidecars_read: h.stats.sidecarsRead,
    agent_runs_written: h.stats.agentRuns, workflow_runs_written: h.stats.workflowRuns,
    agent_runs: db.prepare('SELECT COUNT(*) n FROM agent_runs').get().n,
    agent_runs_in_workflows: db.prepare(
      'SELECT COUNT(*) n FROM agent_runs WHERE workflow_run_id IS NOT NULL').get().n,
    workflow_runs: db.prepare('SELECT COUNT(*) n FROM workflow_runs').get().n,
    rows_unchanged: before.turns === after.turns && before.messages === after.messages
      && before.sessions === after.sessions,
    wrote: !!write, ms: Date.now() - t0,
  };
  if (write) db.exec('COMMIT'); else db.exec('ROLLBACK');
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
  // THE REPORT, not an exit code, which is what `backfillChains` returns and what a caller that
  // wants to check a figure needs. The CLI turns it into a code at the call site, beside the one
  // that already did.
  return report;
}

// ---------------------------------------------------------------------------------------------
// Review runs. See the review_links comment in the schema for the finding. Everything below is
// SQL over rows the ingest already wrote, which is why it runs AFTER the ingest, per directory,
// and never inside it.
// ---------------------------------------------------------------------------------------------
export const REVIEW = {
  WANT: 8,               // snippets taken from a prompt, newest first
  MIN_LINE: 80,          // a line shorter than this is too common to be evidence
  TAIL: 120,             // what is kept of a line: a substring of a truncated line is still a substring
  ONE_SHOT_MESSAGES: 3,  // one typed prompt and at most three messages: the only sessions tested
  SLACK_MS: 3600 * 1000, // how long a session may have been quiet before a run that read it began
  CHUNK: 900,            // parameters per query, under SQLite's limit with room for the others
  // WHAT A SESSION SAID: its assistant text, its tool results, and its compaction summaries. The
  // last is a user-role row nobody typed: the model wrote it when the context was compacted, and
  // the laptop's fourth unplaced run quoted exactly that (the chat beside it had just compacted,
  // so the hook's "last 250 records" were the summary). A person cannot type one, so counting it
  // keeps the guard the said rule exists for: a repeated typed prompt still matches nothing here.
  SAID: "(role = 'assistant' OR type IN ('tool_result', 'compact_summary'))",
};
// CLAUDE SAID: / OUTPUT WAS: / USER:, the label the excerpt's writer put in front of a copied line.
// Dropped, so the line matches what the reviewed session actually said.
const REVIEW_LABEL = /^[A-Z][A-Z ]{1,20}: /;

const isAscii = (s) => {
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) > 127) return false;
  return true;
};

/**
 * The lines of a run's prompt worth searching for, newest first: 80 or more characters, pure
 * ASCII (the excerpt crossed a shell pipe and the store holds U+FFFD where the transcript had
 * anything else, so such a line can never match), the label dropped, the last 120 kept.
 *
 * `c4x/reviews.py` `snippets()` is the Python twin; tests on both sides pin the same inputs.
 */
export function reviewSnippets(prompt, { want = REVIEW.WANT, minLen = REVIEW.MIN_LINE, tail = REVIEW.TAIL } = {}) {
  const out = [];
  const lines = String(prompt ?? '').split('\n');
  for (let i = lines.length - 1; i >= 0 && out.length < want; i--) {
    const line = lines[i].trim().replace(REVIEW_LABEL, '');
    if (line.length >= minLen && isAscii(line)) out.push(line.slice(-tail));
  }
  return out;
}

const reviewChunks = (items, size = REVIEW.CHUNK) => {
  const all = [...items];
  const out = [];
  for (let i = 0; i < all.length; i += size) out.push(all.slice(i, i + size));
  return out;
};
const reviewMarks = (n) => Array(n).fill('?').join(',');

/** The sessions among `ids` with exactly one typed prompt and at most three messages. */
export function reviewOneShots(db, ids) {
  const out = new Set();
  for (const chunk of reviewChunks(ids)) {
    const rows = db.prepare(`SELECT session_id FROM messages WHERE session_id IN (${reviewMarks(chunk.length)})
      GROUP BY session_id
      HAVING SUM(CASE WHEN type = 'typed' AND role = 'user' THEN 1 ELSE 0 END) = 1 AND COUNT(*) <= ?`)
      .all(...chunk, REVIEW.ONE_SHOT_MESSAGES);
    for (const r of rows) out.add(r.session_id);
  }
  return out;
}

/** APPROVED or PROBLEMS when the reply begins with either, the way the hook itself read it. */
export function reviewVerdict(text) {
  const head = String(text ?? '').trim().split(/\s+/)[0] ?? '';
  return /^(APPROVED|PROBLEMS)$/i.test(head) ? head.toUpperCase() : null;
}

/**
 * Tie every one-shot among `sessionIds` (one directory's sessions) to the session it quotes.
 *
 * The pool for a run is the sessions of the same cwd that were alive when it started: begun no
 * later, last active within SLACK before it, and not one-shots themselves, so a run is never tied
 * to a run. One query per run counts, per pool session, how many of the snippets appear in its
 * messages; a session with none of them in its assistant text or tool results is out; among the
 * rest the unique top with at least min(2, snippets) is the head.
 *
 * `write:false` computes and writes nothing. A run already linked is never asked again; a run
 * that missed is asked again only when its pool is a different set of sessions.
 */
export function deriveReviews(db, sessionIds, { write = true, method = 'harvest', now = null } = {}) {
  const result = { sessions: 0, one_shots: 0, already: 0, unchanged: 0, linked: [], orphans: [], misses: 0,
                   hit_queries: 0 };
  const ids = [...new Set(sessionIds.filter(Boolean).map(String))];
  result.sessions = ids.length;
  if (!ids.length) return result;
  const shots = reviewOneShots(db, ids);
  result.one_shots = shots.size;
  if (!shots.size) return result;
  const known = new Set();
  const missed = new Map();
  for (const chunk of reviewChunks(shots)) {
    const m = reviewMarks(chunk.length);
    for (const r of db.prepare(`SELECT session_id FROM review_links WHERE session_id IN (${m})`).all(...chunk)) known.add(r.session_id);
    for (const r of db.prepare(`SELECT session_id, pool_key FROM review_misses WHERE session_id IN (${m})`).all(...chunk)) missed.set(r.session_id, r.pool_key);
  }
  // Every session's place in time, read once.
  const when = new Map();
  for (const chunk of reviewChunks(ids)) {
    const rows = db.prepare(`SELECT session_id, cwd, first_ts, last_ts FROM sessions WHERE session_id IN (${reviewMarks(chunk.length)})`).all(...chunk);
    for (const r of rows) {
      const first = Date.parse(r.first_ts ?? '');
      const last = Date.parse(r.last_ts ?? r.first_ts ?? '');
      when.set(r.session_id, { cwd: r.cwd ?? null, first, last: Number.isNaN(last) ? first : last });
    }
  }
  const stamp = now ?? new Date().toISOString();
  const putLink = db.prepare(`INSERT OR REPLACE INTO review_links
    (session_id, head_id, hits, snippets, verdict, method, linked_at) VALUES (?,?,?,?,?,?,?)`);
  const putMiss = db.prepare('INSERT OR REPLACE INTO review_misses (session_id, pool_key, checked_at) VALUES (?,?,?)');
  const dropMiss = db.prepare('DELETE FROM review_misses WHERE session_id = ?');
  const promptOf = db.prepare(`SELECT text FROM messages WHERE session_id = ? AND type = 'typed' AND role = 'user'
                               ORDER BY ts LIMIT 1`);
  const replyOf = db.prepare(`SELECT text FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY ts LIMIT 1`);
  for (const shot of shots) {
    if (known.has(shot)) { result.already++; continue; }
    const r = when.get(shot);
    if (!r || Number.isNaN(r.first)) continue;
    const pool = [];
    for (const [sid, s] of when) {
      if (sid === shot || shots.has(sid) || s.cwd !== r.cwd) continue;
      if (Number.isNaN(s.first) || s.first > r.first) continue;
      if (s.last + REVIEW.SLACK_MS < r.first) continue;
      pool.push(sid);
    }
    pool.sort();
    const key = pool.join(',');
    if (missed.get(shot) === key) { result.unchanged++; continue; }
    const snips = reviewSnippets(promptOf.get(shot)?.text ?? '');
    let head = null;
    let best = 0;
    if (snips.length && pool.length) {
      // TWO COUNTS PER POOL SESSION. `said` is how many snippets its assistant text or tool
      // results contain; `any` counts its typed prompts too. A reviewer quotes what the session
      // said and what came back, and often the prompt it was answering as well; a one-shot that
      // merely repeats a person's prompt matches typed rows and nothing else. So a session with
      // no said line is out, and among the rest every quoted line counts: a tiny session's
      // reviewer shared one said line and one typed line with it, and lost on the said count.
      const hits = new Map();
      const saidSum = snips.map(() =>
        `MAX(CASE WHEN ${REVIEW.SAID} AND instr(text, ?) > 0 THEN 1 ELSE 0 END)`).join(' + ');
      const anySum = snips.map(() => 'MAX(CASE WHEN instr(text, ?) > 0 THEN 1 ELSE 0 END)').join(' + ');
      for (const chunk of reviewChunks(pool)) {
        result.hit_queries++;
        const rows = db.prepare(`SELECT session_id, ${saidSum} AS said, ${anySum} AS any
          FROM messages WHERE session_id IN (${reviewMarks(chunk.length)}) GROUP BY session_id`)
          .all(...snips, ...snips, ...chunk);
        for (const row of rows) if (row.said > 0) hits.set(row.session_id, (hits.get(row.session_id) ?? 0) + row.any);
      }
      if (hits.size) {
        const top = Math.max(...hits.values());
        const tops = [...hits].filter(([, n]) => n === top).map(([sid]) => sid);
        if (tops.length === 1 && top >= Math.min(2, snips.length)) { head = tops[0]; best = top; }
      }
    }
    if (head) {
      const verdict = reviewVerdict(replyOf.get(shot)?.text);
      result.linked.push({ session_id: shot, head_id: head, hits: best, snippets: snips.length, verdict });
      if (write) { putLink.run(shot, head, best, snips.length, verdict, method, stamp); dropMiss.run(shot); }
      continue;
    }
    // THE ORPHAN LOOK, for a run its folder cannot place. A review by every other sign: its
    // reply is a bare verdict (the cheap test, first, since almost every miss is an ordinary
    // one-shot with an ordinary answer), and at least two of its lines are said, as assistant
    // text or tool results, somewhere in the store. The verdict is what keeps a person's real
    // chat safe: pasting another chat's output into a new session earns an answer, not a verdict.
    // The scan is store-wide and unindexed, which is why it runs only past the verdict test and
    // only once per miss until the pool changes.
    const verdict = snips.length ? reviewVerdict(replyOf.get(shot)?.text) : null;
    if (verdict) {
      result.hit_queries++;
      const anySum = snips.map(() => 'MAX(CASE WHEN instr(text, ?) > 0 THEN 1 ELSE 0 END)').join(' + ');
      const row = db.prepare(`SELECT ${anySum} AS said FROM messages
        WHERE session_id <> ? AND ${REVIEW.SAID}`).get(...snips, shot);
      const said = Number(row?.said ?? 0);
      if (said >= Math.min(2, snips.length)) {
        result.orphans.push({ session_id: shot, head_id: null, hits: said, snippets: snips.length, verdict });
        if (write) { putLink.run(shot, null, said, snips.length, verdict, method, stamp); dropMiss.run(shot); }
        continue;
      }
    }
    result.misses++;
    if (write) putMiss.run(shot, key, stamp);
  }
  return result;
}

/** --backfill-reviews: every directory the store knows, one transaction each. */
export async function backfillReviews(dbPath = DB_PATH, { quiet = false, write = true } = {}) {
  if (!existsSync(dbPath)) { if (!quiet) console.error(`no store at ${dbPath}`); return null; }
  const t0 = Date.now();
  const db = openDb(dbPath);
  const count = () => db.prepare('SELECT COUNT(*) n FROM review_links').get().n;
  const slugs = db.prepare('SELECT project_slug FROM sessions GROUP BY project_slug').all().map((r) => r.project_slug);
  const report = { db: posix(dbPath), directories: slugs.length, sessions: 0, one_shots: 0, already: 0,
                   unchanged: 0, linked: 0, orphans: 0, misses: 0, by_verdict: {}, heads: 0,
                   links_before: count(), links_after: 0, wrote: write, ms: 0 };
  const heads = new Set();
  const bySlug = db.prepare('SELECT session_id FROM sessions WHERE project_slug IS ?');
  for (const slug of slugs) {
    const ids = bySlug.all(slug).map((r) => r.session_id);
    if (write) db.exec('BEGIN');
    let r;
    try {
      r = deriveReviews(db, ids, { write, method: 'backfill-reviews' });
      if (write) db.exec('COMMIT');
    } catch (e) {
      if (write) { try { db.exec('ROLLBACK'); } catch { /* nothing to roll back */ } }
      throw e;
    }
    report.sessions += r.sessions;
    report.one_shots += r.one_shots;
    report.already += r.already;
    report.unchanged += r.unchanged;
    report.linked += r.linked.length;
    report.orphans += r.orphans.length;
    report.misses += r.misses;
    for (const l of r.linked.concat(r.orphans)) {
      if (l.head_id) heads.add(l.head_id);
      const v = l.verdict ?? 'none';
      report.by_verdict[v] = (report.by_verdict[v] ?? 0) + 1;
    }
  }
  report.heads = heads.size;
  report.links_after = count();
  report.ms = Date.now() - t0;
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
  return report;
}

export async function backfillChains(dbPath = DB_PATH, { quiet = false, write = true, recordsRoots = null,
                                                        projects = PROJECTS, threshold = 0.9 } = {}) {
  if (!existsSync(dbPath)) { if (!quiet) console.error(`no store at ${dbPath}`); return null; }
  const t0 = Date.now();
  const db = openDb(dbPath);
  const highWater = {
    turns: db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM turns').get().n,
    sessions: db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM sessions').get().n,
  };
  const existing = (table) => db.prepare(`SELECT COUNT(*) n FROM ${table} WHERE rowid <= ?`).get(highWater[table]).n;
  const links = () => db.prepare('SELECT COUNT(*) n FROM session_links').get().n;
  const before = { turns: existing('turns'), sessions: existing('sessions'), links: links() };
  const roots = recordsRoots ?? resolveRecordsRoots([]);
  const records = readDesktopRecords(roots);
  if (records.size === 0 && !quiet) {
    console.error('harvest: no desktop records found under ' + (roots.map(posix).join(', ') || '(no root)')
      + '; every session is treated as record-less, so no chat is anchored by a record');
  }
  // The projects the user asked to stop capturing contribute nothing, exactly as in ingest.
  let excludedCwds = new Set();
  try { excludedCwds = new Set(db.prepare('SELECT cwd FROM excluded_projects').all().map((r) => r.cwd)); }
  catch { /* a store without the table has excluded nothing */ }
  let dirs = [];
  try {
    dirs = readdirSync(projects, { withFileTypes: true })
      .filter((e) => e.isDirectory()).map((e) => join(projects, e.name));
  } catch { /* no transcripts on this machine */ }
  const report = {
    db: posix(dbPath), records_roots: roots.map(posix), records_read: records.size,
    fork_records: [...records.values()].filter((r) => r.fork).length,
    directories: dirs.length, files_scanned: 0, anchors_kept: 0, links_written: 0,
    chains: 0, longest_chain: 0, by_head_kind: {},
    rows_moved: { turns: 0, messages: 0, compactions: 0, tool_calls: 0 },
    // Session rows whose directory, slug or transcript path did not match their own transcript.
    rows_repaired: { cwd: 0, project_slug: 0, transcript_path: 0 },
  };
  const heads = new Map();
  let i = 0;
  for (const dir of dirs) {
    // One transaction per directory, so a concurrent hook harvest is never starved of the write
    // lock for longer than one directory takes.
    if (write) db.exec('BEGIN');
    let r;
    try {
      r = await reconcileDirectory(db, dir, records, { write, threshold, excludedCwds });
      if (write) db.exec('COMMIT');
    } catch (e) {
      if (write) { try { db.exec('ROLLBACK'); } catch { /* nothing to roll back */ } }
      throw e;
    }
    report.files_scanned += r.files;
    report.links_written += r.links;
    report.anchors_kept += r.anchors_kept;
    for (const k of Object.keys(report.rows_moved)) report.rows_moved[k] += r.moved[k];
    for (const k of Object.keys(report.rows_repaired)) report.rows_repaired[k] += r.repaired[k];
    for (const row of r.rows) {
      heads.set(row.head_id, (heads.get(row.head_id) ?? 0) + 1);
      report.by_head_kind[row.head_kind] = (report.by_head_kind[row.head_kind] ?? 0) + 1;
    }
    if (!quiet && ++i % 100 === 0) {
      console.error(`  ${i}/${dirs.length} directories, ${report.links_written} links, ${Date.now() - t0} ms`);
    }
  }
  report.chains = heads.size;
  report.longest_chain = heads.size ? Math.max(...heads.values()) : 0;
  const after = { turns: existing('turns'), sessions: existing('sessions'), links: links() };
  report.rows_before = { turns: before.turns, sessions: before.sessions };
  report.rows_after = { turns: after.turns, sessions: after.sessions };
  report.rows_unchanged = before.turns === after.turns && before.sessions === after.sessions;
  report.links_before = before.links;
  report.links_after = after.links;
  report.wrote = write;
  report.ms = Date.now() - t0;
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
  return report;
}

/**
 * Fill in WHAT WROTE each stored message, for rows captured before the harvester recorded it.
 *
 * `messageKind` used to return the record's `type`, and Claude Code files a tool result as a
 * record of type 'user'. So 190,936 rows on this machine say "user" whether a person typed them or
 * a tool produced them, and the distinction cannot be recovered from the stored text: `messageText`
 * folds a tool_result's content into the same field as prose. Only the transcripts still have it.
 *
 * UPDATE ONLY. It may change `messages.type` and nothing else, and the report proves that with a
 * row count taken over rows that already existed rather than over the whole table, because three
 * writers exist by design and a concurrent INSERT is not this tool's doing. Same guard, and for
 * the same reason, as backfillAgents above.
 *
 * ROWS IT CANNOT REACH ARE MARKED AND COUNTED. A transcript that has been deleted or came from
 * another machine cannot say what wrote its messages, and leaving those rows reading "user" would
 * make them look like something a person typed. They are set to 'unknown' instead, which is what
 * this store actually knows about them.
 */
export async function backfillMessageSource(dbPath = DB_PATH,
                                            { quiet = false, projects = PROJECTS } = {}) {
  const db = openDb(dbPath);
  const setKind = db.prepare('UPDATE messages SET type = ? WHERE uuid = ? AND type <> ?');

  const highWater = db.prepare('SELECT COALESCE(MAX(rowid), 0) n FROM messages').get().n;
  const existing = () => db.prepare(
    'SELECT COUNT(*) n FROM messages WHERE rowid <= ?').get(highWater).n;
  const census = () => db.prepare(
    'SELECT type, COUNT(*) n FROM messages GROUP BY 1 ORDER BY n DESC').all();
  const before = {
    rows: existing(),
    total: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    by_type: census(),
  };

  const files = listTranscripts(projects);
  let scanned = 0, unreadable = 0, updated = 0;
  const perFile = [];
  for (const path of files) {
    scanned++;
    let text;
    try { text = readFileSync(path, 'utf8'); } catch { unreadable++; continue; }
    let changed = 0;
    for (const line of text.split('\n')) {
      // A cheap string test before JSON.parse, like the backfills above: parsing every line of
      // 10 GB to read two fields is the cost this approach exists to avoid.
      if (!line || !line.includes('"uuid"')) continue;
      let d;
      try { d = JSON.parse(line); } catch { continue; }
      if (typeof d?.uuid !== 'string') continue;
      const kind = messageKind(d);
      changed += setKind.run(kind, d.uuid, kind).changes;
    }
    updated += changed;
    if (changed) perFile.push({ file: path, updated: changed });
    if (!quiet && scanned % 1000 === 0) {
      console.error(`  ${scanned}/${files.length} files, ${updated} messages relabelled`);
    }
  }

  // The rows no transcript could speak for. Found by asking the store which files it recorded and
  // which of those are gone, rather than by assuming the scan above covered everything: a file
  // outside ~/.claude/projects is in `messages` and not in `listTranscripts`.
  const stale = [];
  for (const row of db.prepare(
    'SELECT DISTINCT file_path FROM messages WHERE file_path IS NOT NULL').all()) {
    if (!existsSync(row.file_path)) stale.push(row.file_path);
  }
  // ONLY the rows that are genuinely ambiguous. An assistant record is already classified by
  // its own record type, so marking it unknown because its transcript is gone would destroy a
  // label this store does have. Found by running the backfill twice: the second pass relabelled
  // 185 assistant rows it had no business touching.
  const markUnknown = db.prepare(
    "UPDATE messages SET type = 'unknown' WHERE file_path = ? AND type = 'user'");
  let unreachable = 0;
  for (const path of stale) unreachable += markUnknown.run(path).changes;

  const after = {
    rows: existing(),
    total: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    by_type: census(),
  };
  const report = {
    files_scanned: scanned,
    files_unreadable: unreadable,
    files_that_contributed: perFile.length,
    messages_relabelled: updated,
    transcripts_gone: stale.length,
    // Not a warning. These rows can never be classified, and saying so is the difference between
    // a gap and a silent one.
    messages_marked_unknown_because_their_transcript_is_gone: unreachable,
    rows_before: before.rows,
    rows_after: after.rows,
    rows_unchanged: before.rows === after.rows,
    written_by_other_writers_meanwhile: after.total - before.total,
    by_type_before: before.by_type,
    by_type_after: after.by_type,
    // Rows still carrying the old record-type label. 'assistant' is NOT in this count: it is
    // the correct final answer for an assistant message, and counting it made a working backfill
    // report 92,146 failures.
    still_unclassified: db.prepare(
      "SELECT COUNT(*) n FROM messages WHERE type = 'user'").get().n,
    busiest_files: perFile.sort((a, b) => b.updated - a.updated).slice(0, 5),
  };
  if (!quiet) console.log(JSON.stringify(report, null, 2));
  db.close();
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

// The earliest timestamp in a file's first 64 KB, or null. Enough for ingest ordering: the first
// timestamped record sits within the first few lines of every transcript shape seen so far.
function firstTimestamp(path) {
  let fd = null;
  try {
    fd = openSync(path, 'r');
    const buf = Buffer.alloc(1 << 16);
    const n = readSync(fd, buf, 0, buf.length, 0);
    const m = /"timestamp":"([^"]+)"/.exec(buf.toString('utf8', 0, n));
    return m ? m[1] : null;
  } catch {
    return null;
  } finally {
    if (fd !== null) closeSync(fd);
  }
}

// Transcripts in the order they STARTED, so a copy is never read before what it copied. Files
// already in the store answer from files.first_ts; anything else is head-scanned once.
export function orderedTranscripts(db, files) {
  const known = new Map(db.prepare('SELECT path, first_ts FROM files WHERE first_ts IS NOT NULL')
    .all().map((r) => [r.path, r.first_ts]));
  const keyed = files.map((p) => [known.get(p) ?? firstTimestamp(p) ?? '9999', p]);
  keyed.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  return keyed.map((k) => k[1]);
}

/**
 * What an incremental run WOULD read, from the sizes on disk and the offsets the store holds. The
 * same three-way comparison Harvest.file() makes, without the reading. Pure over (files, getFile)
 * so the self-test drives it against a scratch store.
 */
function plan(files, getFile) {
  const out = { files_seen: files.length, would_read: 0, bytes_to_read: 0, rewritten: 0, unchanged: 0 };
  for (const path of files) {
    let size;
    try { size = statSync(path).size; } catch { continue; }
    const prev = getFile(path);
    if (!prev) { out.would_read++; out.bytes_to_read += size; }
    else if (size < prev.bytes_read) { out.would_read++; out.rewritten++; out.bytes_to_read += size; }
    else if (size === prev.bytes_read) { out.unchanged++; }
    else { out.would_read++; out.bytes_to_read += size - prev.bytes_read; }
  }
  return out;
}

/**
 * --dry-run: the plan, printed, and nothing written. The store is opened read-only when it exists
 * and not created when it does not; a missing store means every transcript is new.
 */
function dryRun(dbPath = DB_PATH, files = listTranscripts(PROJECTS)) {
  let getFile = () => null;
  let db = null;
  if (existsSync(dbPath)) {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const stmt = db.prepare('SELECT bytes_read FROM files WHERE path = ?');
    getFile = (path) => stmt.get(path) || null;
  }
  const p = plan(files, getFile);
  if (db) db.close();
  console.log(`dry run against ${dbPath}: ${p.files_seen} transcripts seen, ${p.would_read} would be read`
    + ` (${p.rewritten} rewritten since last time), ${(p.bytes_to_read / 1048576).toFixed(1)} MB to read,`
    + ` ${p.unchanged} unchanged. Nothing was written.`);
  return 0;
}

/**
 * --full re-reads every transcript, and the last time that was 10 GB for a few kilobytes of new
 * rows. It says how much before it starts, and it starts only on --yes. Returns the refusal, or
 * null when the run may proceed.
 */
function fullGate(argv, files = listTranscripts(PROJECTS)) {
  if (!argv.includes('--full') || argv.includes('--yes')) return null;
  let bytes = 0;
  for (const f of files) { try { bytes += statSync(f).size; } catch { /* gone between list and stat */ } }
  return `--full re-reads every transcript: ${files.length} files, ${(bytes / 1048576).toFixed(0)} MB. Pass --yes to run it.`;
}

// The census must report the record's OWN type, not the first "type" string that happens to
// appear in the line. A regex cannot tell depth 1 from a nested content block, so every line is
// parsed. Measured cost of parsing everything rather than prefiltering: see harvest_runs.
const KNOWN_TYPES = new Set([
  'user', 'assistant', 'attachment', 'system', 'summary', 'ai-title',
  'bridge-session', 'queue-operation', 'last-prompt', 'custom-title', 'atis-latch', 'mode',
  // Claude Code 2.1.233 writes `permission-mode` where older builds wrote `mode`. BOTH stay: this
  // set is what the census calls recognised, and dropping the old name would re-flag every
  // transcript already on disk as unknown drift. Recognised, not stored: nothing reports permission
  // mode, and adding a column for it is a feature rather than the fix for a false drift signal.
  //
  // This is the channel working. An unknown type is logged with a sample and counted, which is how
  // an upstream rename becomes visible instead of silently misclassified, and it is why the rename
  // was noticed at all.
  'permission-mode',
  // STORED, not merely recognised, which is the exception this set's rule calls out. It carries a
  // measured cost and a per-model breakdown, and the Cost tab has only ever had an estimate.
  'cost-state',
  'file-history-snapshot', 'x-anthropic-log',
]);

class Harvest {
  constructor(db, opts = {}) {
    this.db = db;
    registerFunctions(db);
    // INJECTABLE, so the self-test stops writing into the user's capture directory. It wrote three
    // synthetic `zzz-brand-new-type` rows into the live data/raw/unknown-records.ndjson on every
    // run; 1,037 of them had accumulated here, making the fixture the single most common "unknown
    // record type" the tool reports. hooks/compact-hook.mjs already takes its paths this way.
    // Read AT CONSTRUCTION rather than at module load, so the self-test can redirect every
    // instance it makes by setting the variable before it builds them - including instances added
    // later, which an explicit option at today's one relevant call site would not cover.
    this.unknownLog = opts.unknownLog ?? process.env.C4X_UNKNOWN_LOG ?? UNKNOWN_LOG;
    this.stmt = {
      getFile: db.prepare('SELECT * FROM files WHERE path = ?'),
      putFile: db.prepare(`INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts,first_ts)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
        size=excluded.size, mtime_ms=excluded.mtime_ms, bytes_read=excluded.bytes_read,
        lines_read=excluded.lines_read, rewrites=excluded.rewrites, last_harvest_ts=excluded.last_harvest_ts,
        first_ts=COALESCE(files.first_ts, excluded.first_ts)`),
      // A SIDECAR IS READ WHOLE OR NOT AT ALL, so its row carries no offset to resume from: size
      // and mtime are the whole test. Marked `sidecar` so nothing that counts transcripts counts
      // these too.
      putSidecarFile: db.prepare(`INSERT INTO files
        (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts,kind)
        VALUES (?,?,?,?,1,0,?, 'sidecar') ON CONFLICT(path) DO UPDATE SET
        size=excluded.size, mtime_ms=excluded.mtime_ms, bytes_read=excluded.bytes_read,
        last_harvest_ts=excluded.last_harvest_ts, kind='sidecar'`),
      putAgentRun: db.prepare(`INSERT INTO agent_runs
        (agent_id,dir_session_id,tool_use_id,workflow_run_id,agent_type,name,description,
         spawn_depth,model,parent_agent_id,stopped_by_user,meta_json,transcript_path,
         meta_path,meta_size,meta_mtime_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(agent_id) DO UPDATE SET
         dir_session_id=excluded.dir_session_id, tool_use_id=excluded.tool_use_id,
         workflow_run_id=excluded.workflow_run_id, agent_type=excluded.agent_type,
         name=excluded.name, description=excluded.description, spawn_depth=excluded.spawn_depth,
         model=excluded.model, parent_agent_id=excluded.parent_agent_id,
         stopped_by_user=excluded.stopped_by_user, meta_json=excluded.meta_json,
         transcript_path=excluded.transcript_path, meta_path=excluded.meta_path,
         meta_size=excluded.meta_size, meta_mtime_ms=excluded.meta_mtime_ms`),
      // THE JSON'S HALF OF A WORKFLOW ROW. It names only its own columns for the same reason
      // linkWorkflow does: the two passes can arrive in either order and each would otherwise
      // erase the other's work.
      putWorkflow: db.prepare(`INSERT INTO workflow_runs
        (run_id,dir_session_id,workflow_name,status,started_at,ts,duration_ms,agent_count,
         total_tokens,total_tool_calls,default_model,summary,result_text,phases_json,progress_json,
         error,file_path,file_size,file_mtime_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
         dir_session_id=excluded.dir_session_id, workflow_name=excluded.workflow_name,
         status=excluded.status, started_at=excluded.started_at, ts=excluded.ts,
         duration_ms=excluded.duration_ms, agent_count=excluded.agent_count,
         total_tokens=excluded.total_tokens, total_tool_calls=excluded.total_tool_calls,
         default_model=excluded.default_model, summary=excluded.summary,
         result_text=excluded.result_text, phases_json=excluded.phases_json,
         progress_json=excluded.progress_json, error=excluded.error,
         file_path=excluded.file_path, file_size=excluded.file_size,
         file_mtime_ms=excluded.file_mtime_ms`),
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
        -- THE DIRECTORY THE SESSION LIVES IN, NOT THE LAST ONE IT VISITED. A session that changes
        -- directory keeps writing to the transcript it started, under the slug of the directory it
        -- started in, and that slug is what export, delete, memory and the trust entry are keyed
        -- by. "Last non-null wins" filed 56 of 1,050 sessions on one machine under a subdirectory
        -- their files are not in, so their transcripts were never carried and never purged. The
        -- first cwd whose slug IS the file's directory sticks; until one is seen, the latest.
        cwd=CASE WHEN slug_of(sessions.cwd) = sessions.project_slug THEN sessions.cwd
                 WHEN slug_of(excluded.cwd) = excluded.project_slug THEN excluded.cwd
                 ELSE COALESCE(excluded.cwd, sessions.cwd) END,
        -- The session's OWN top-level transcript wins over any other file naming it: a subagent
        -- file under <session>/ carries the parent's sessionId and used to be inserted first, in
        -- directory order, leaving 69 of 1,426 rows here pointing at a subagent file.
        project_slug=CASE WHEN ?10 THEN excluded.project_slug ELSE sessions.project_slug END,
        transcript_path=CASE WHEN ?10 THEN excluded.transcript_path ELSE sessions.transcript_path END,
        git_branch=COALESCE(excluded.git_branch, sessions.git_branch)`),
      // A ROW IS NEVER MOVED TO ANOTHER SESSION BY INGEST. These were INSERT OR REPLACE, and a
      // resumed or forked transcript is a COPY of its predecessor with the same uuids under a new
      // sessionId, so whichever file was harvested last owned every shared row: one session that
      // produced 388 turns was left holding 3 of them. ON CONFLICT ... WHERE the session matches
      // keeps every field of a same-session rewrite current and refuses a copy from elsewhere.
      // IS rather than =, so a row with no session id still updates on a rewrite of its own file.
      // Files are read in first_ts order so the producer is always the first to claim a uuid, and
      // --backfill-chains repairs stores written before this rule.
      putTurn: db.prepare(`INSERT INTO turns
        (uuid,session_id,ts,model,request_id,input_tokens,cache_creation_input_tokens,cache_read_input_tokens,
         output_tokens,thinking_tokens,eph_1h,eph_5m,service_tier,total_resident,is_sidechain,file_path,line_no,
         parent_uuid)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uuid) DO UPDATE SET
         ts=excluded.ts, model=excluded.model, request_id=excluded.request_id,
         input_tokens=excluded.input_tokens, cache_creation_input_tokens=excluded.cache_creation_input_tokens,
         cache_read_input_tokens=excluded.cache_read_input_tokens, output_tokens=excluded.output_tokens,
         thinking_tokens=excluded.thinking_tokens, eph_1h=excluded.eph_1h, eph_5m=excluded.eph_5m,
         service_tier=excluded.service_tier, total_resident=excluded.total_resident,
         is_sidechain=excluded.is_sidechain, file_path=excluded.file_path, line_no=excluded.line_no,
         parent_uuid=excluded.parent_uuid
        WHERE excluded.session_id IS turns.session_id`),
      putCompaction: db.prepare(`INSERT INTO compactions
        (uuid,session_id,ts,trigger,version,entrypoint,pre_tokens,post_tokens,duration_ms,cumulative_dropped_tokens,
         messages_summarized,discovered_tools_json,preserved_json,summary_uuid,summary_chars,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uuid) DO UPDATE SET
         ts=excluded.ts, trigger=excluded.trigger, version=excluded.version, entrypoint=excluded.entrypoint,
         pre_tokens=excluded.pre_tokens, post_tokens=excluded.post_tokens, duration_ms=excluded.duration_ms,
         cumulative_dropped_tokens=excluded.cumulative_dropped_tokens,
         messages_summarized=excluded.messages_summarized, discovered_tools_json=excluded.discovered_tools_json,
         preserved_json=excluded.preserved_json, summary_uuid=excluded.summary_uuid,
         summary_chars=excluded.summary_chars, file_path=excluded.file_path, line_no=excluded.line_no
        WHERE excluded.session_id IS compactions.session_id`),
      paircompaction: db.prepare('UPDATE compactions SET summary_uuid = ?, summary_chars = ? WHERE uuid = ?'),
      putSurvivor: db.prepare('INSERT OR IGNORE INTO compaction_survivors (compaction_uuid,kind,uuid) VALUES (?,?,?)'),
      putMessage: db.prepare(`INSERT INTO messages
        (uuid,session_id,ts,role,type,text,chars,model,request_id,is_sidechain,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uuid) DO UPDATE SET
         ts=excluded.ts, role=excluded.role, type=excluded.type, text=excluded.text, chars=excluded.chars,
         model=excluded.model, request_id=excluded.request_id, is_sidechain=excluded.is_sidechain,
         file_path=excluded.file_path, line_no=excluded.line_no
        WHERE excluded.session_id IS messages.session_id`),
      // EVERY RESULT-SIDE COLUMN IS COALESCED, and that list is load-bearing. A tool_use line
      // re-read after its result has landed would otherwise REPLACE the row and null whatever the
      // result had filled in. It is silent: no error, no exception, the data is simply gone. The
      // self-test drives exactly that sequence, because nothing else would catch it.
      putToolCall: db.prepare(`INSERT INTO tool_calls
        (tool_use_id,session_id,turn_uuid,ts,tool_name,server_name,target,input_sha1,input_bytes,
         result_bytes,is_error,is_sidechain,file_path,line_no,subagent_type,input_preview,description,
         outcome,denial_kind)
        VALUES (?,?,?,?,?,?,?,?,?,
         COALESCE((SELECT result_bytes FROM tool_calls WHERE tool_use_id = ?), NULL),
         COALESCE((SELECT is_error FROM tool_calls WHERE tool_use_id = ?), NULL), ?,?,?,?,?,?,
         COALESCE((SELECT outcome FROM tool_calls WHERE tool_use_id = ?), NULL),
         COALESCE((SELECT denial_kind FROM tool_calls WHERE tool_use_id = ?), NULL))
        ON CONFLICT(tool_use_id) DO UPDATE SET
         turn_uuid=excluded.turn_uuid, ts=excluded.ts, tool_name=excluded.tool_name,
         server_name=excluded.server_name, target=excluded.target, input_sha1=excluded.input_sha1,
         input_bytes=excluded.input_bytes, result_bytes=excluded.result_bytes, is_error=excluded.is_error,
         is_sidechain=excluded.is_sidechain, file_path=excluded.file_path, line_no=excluded.line_no,
         subagent_type=excluded.subagent_type, input_preview=excluded.input_preview,
         description=excluded.description, outcome=excluded.outcome, denial_kind=excluded.denial_kind
        WHERE excluded.session_id IS tool_calls.session_id`),
      // The result arrives on a LATER line than the use, so this fills the row in place. If the
      // two land in different harvest runs the update finds nothing and result_bytes stays NULL,
      // which reads as "not yet seen" rather than as zero bytes.
      setToolResult: db.prepare(
        'UPDATE tool_calls SET result_bytes = ?, is_error = ?, outcome = ?, denial_kind = ? WHERE tool_use_id = ?'),
      bumpAttachment: db.prepare(`INSERT INTO attachments (session_id,type,n) VALUES (?,?,1)
        ON CONFLICT(session_id,type) DO UPDATE SET n = n + 1`),
      // THE WHOLE PLAN, and the same refusal clause the rows beside it use: a resumed transcript
      // carries the earlier session's ExitPlanMode records verbatim, and the copy must not take
      // the row from the session that wrote it.
      putPlan: db.prepare(`INSERT INTO plans
        (tool_use_id,session_id,turn_uuid,ts,plan_text,plan_chars,plan_file_path,
         allowed_prompts_json,is_sidechain,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tool_use_id) DO UPDATE SET
         turn_uuid=excluded.turn_uuid, ts=excluded.ts, plan_text=excluded.plan_text,
         plan_chars=excluded.plan_chars, plan_file_path=excluded.plan_file_path,
         allowed_prompts_json=excluded.allowed_prompts_json, is_sidechain=excluded.is_sidechain,
         file_path=excluded.file_path, line_no=excluded.line_no
        WHERE excluded.session_id IS plans.session_id`),
      putTaskEvent: db.prepare(`INSERT INTO task_events
        (uuid,session_id,ts,parent_uuid,task_id,task_type,status,description,delta_summary,
         output_file_path,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(uuid) DO UPDATE SET
         ts=excluded.ts, parent_uuid=excluded.parent_uuid, task_id=excluded.task_id,
         task_type=excluded.task_type, status=excluded.status, description=excluded.description,
         delta_summary=excluded.delta_summary, output_file_path=excluded.output_file_path,
         file_path=excluded.file_path, line_no=excluded.line_no
        WHERE excluded.session_id IS task_events.session_id`),
      // THE CALL'S HALF OF A CHANGE, naming only its own columns, so the patch the result wrote
      // survives it whichever lands first. Same refusal clause as plans: a resumed transcript
      // carries the earlier session's records verbatim and must not take the row.
      putChangeCall: db.prepare(`INSERT INTO changes
        (tool_use_id,session_id,turn_uuid,ts,tool_name,file,old_text,new_text,replace_all,
         old_lines,new_lines,is_sidechain,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tool_use_id) DO UPDATE SET
         turn_uuid=excluded.turn_uuid, ts=excluded.ts, tool_name=excluded.tool_name,
         file=excluded.file, old_text=excluded.old_text, new_text=excluded.new_text,
         replace_all=excluded.replace_all, old_lines=excluded.old_lines,
         new_lines=excluded.new_lines, is_sidechain=excluded.is_sidechain,
         file_path=excluded.file_path, line_no=excluded.line_no
        WHERE excluded.session_id IS changes.session_id`),
      // THE RESULT'S HALF: the patch and what it counts to. Never the text, which the call owns.
      putChangeResult: db.prepare(`INSERT INTO changes
        (tool_use_id,session_id,kind,patch_json,additions,deletions,original_chars,user_modified,
         file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(tool_use_id) DO UPDATE SET
         kind=excluded.kind, patch_json=excluded.patch_json, additions=excluded.additions,
         deletions=excluded.deletions, original_chars=excluded.original_chars,
         user_modified=excluded.user_modified
        WHERE excluded.session_id IS changes.session_id`),
      // THE TRANSCRIPT'S HALF OF A WORKFLOW ROW, and it names only its own columns. The JSON pass
      // owns the rest and either can land first, so an UPDATE that listed every column would wipe
      // whichever half arrived earlier. Silent, and only a self-test can see it.
      linkWorkflow: db.prepare(`INSERT INTO workflow_runs
        (run_id,task_id,tool_use_id,turn_uuid,session_id,transcript_dir,script_path)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(run_id) DO UPDATE SET
         task_id=excluded.task_id, tool_use_id=excluded.tool_use_id, turn_uuid=excluded.turn_uuid,
         session_id=excluded.session_id, transcript_dir=excluded.transcript_dir,
         script_path=excluded.script_path`),
      bumpType: db.prepare(`INSERT INTO record_types (type,n) VALUES (?,1)
        ON CONFLICT(type) DO UPDATE SET n = n + 1`),
      // MONOTONIC. `WHERE excluded.total_cost_usd >= cost_state.total_cost_usd` is the whole point:
      // these totals are cumulative, and re-reading an older record must not walk the stored cost
      // backwards. COALESCE on the stored side so the first row always wins over NULL.
      putCostState: db.prepare(`INSERT INTO cost_state
        (session_id,started_at,total_cost_usd,total_api_ms,total_api_ms_no_retries,total_tool_ms,
         total_duration_ms,lines_added,lines_removed,has_unknown_model_cost,file_path,line_no)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
          started_at = excluded.started_at, total_cost_usd = excluded.total_cost_usd,
          total_api_ms = excluded.total_api_ms,
          total_api_ms_no_retries = excluded.total_api_ms_no_retries,
          total_tool_ms = excluded.total_tool_ms, total_duration_ms = excluded.total_duration_ms,
          lines_added = excluded.lines_added, lines_removed = excluded.lines_removed,
          has_unknown_model_cost = excluded.has_unknown_model_cost,
          file_path = excluded.file_path, line_no = excluded.line_no
        WHERE excluded.total_cost_usd >= COALESCE(cost_state.total_cost_usd, -1)`),
      putCostModel: db.prepare(`INSERT INTO cost_state_models
        (session_id,model,input_tokens,output_tokens,cache_read_input_tokens,
         cache_creation_input_tokens,web_search_requests,cost_usd)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id,model) DO UPDATE SET
          input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens,
          cache_read_input_tokens = excluded.cache_read_input_tokens,
          cache_creation_input_tokens = excluded.cache_creation_input_tokens,
          web_search_requests = excluded.web_search_requests, cost_usd = excluded.cost_usd
        WHERE excluded.cost_usd >= COALESCE(cost_state_models.cost_usd, -1)`),
      putRun: db.prepare(`INSERT INTO harvest_runs (ts,mode,files_seen,files_read,rewrites,lines,mb,turns,compactions,unpaired,ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)`),
    };
    this.typeCounts = new Map();
    this.typeKnown = new Map();
    this.unknownSeen = new Set();
    // WHAT THIS RUN COULD NOT PARSE, kept apart from what the LOG has ever held.
    //
    // `unknownSeen` is loaded from data/raw/unknown-records.ndjson so a type already sampled is not
    // appended twice. Reporting from it made unknown_record_types mean "every type ever logged",
    // so a type recognised later kept being announced as unrecognised for the life of the store:
    // cost-state is parsed and stored now, and every harvest still named it. One Set cannot answer
    // both questions.
    this.unknownThisRun = new Set();
    this.stats = { filesSeen: 0, filesRead: 0, rewrites: 0, lines: 0, bytes: 0, turns: 0, compactions: 0, paired: 0, toolCalls: 0, toolResults: 0, messages: 0, messageChars: 0, excludedFiles: 0,
                   plans: 0, taskEvents: 0, workflowLinks: 0, sidecarsSeen: 0, sidecarsRead: 0, agentRuns: 0, workflowRuns: 0,
                   changes: 0, changePatches: 0 };
    this.sessionDirs = new Set();
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

  // CARRIED, not re-derived at flush time. The census key can be a composite (`system/foo`)
  // while recognition is decided on the base type, so a later KNOWN_TYPES.has(key) would call
  // every system subtype unknown.
  /**
   * The cost Claude Code measured for this session, and its per-model breakdown.
   *
   * NUMBERS ARE COERCED, NOT TRUSTED. A record whose totalCostUSD is absent or not a number is
   * skipped outright rather than stored as 0: a zero cost is a claim, and it is the one claim this
   * app must never make by accident. Same rule c4x/pricing.py states for a model with no price,
   * where blank beats zero because zero says these calls were free.
   */
  putCostState(d, path, lineNo) {
    const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    const total = num(d.totalCostUSD);
    if (total === null) return;
    // startTime IS A NUMBER. The schema in the 2.1.250 bundle declares every numeric field with
    // one validator, `w.number().nonnegative().finite()`, and startTime is one of them: it comes
    // from a Date.now() stamped when the cost ledger was reset. Reading it as a string stored NULL
    // for every record, which looks exactly like a record that carried no start time.
    const started = typeof d.startTime === 'number' && Number.isFinite(d.startTime)
      ? new Date(d.startTime).toISOString()
      : (typeof d.startTime === 'string' ? d.startTime : null);
    this.stmt.putCostState.run(
      d.sessionId, started, total,
      num(d.totalAPIDuration), num(d.totalAPIDurationWithoutRetries), num(d.totalToolDuration),
      num(d.totalDuration), num(d.totalLinesAdded), num(d.totalLinesRemoved),
      d.hasUnknownModelCost === true ? 1 : 0, path, lineNo);
    const usage = d.modelUsage;
    if (!usage || typeof usage !== 'object') return;
    for (const [model, u] of Object.entries(usage)) {
      if (!u || typeof u !== 'object') continue;
      this.stmt.putCostModel.run(
        d.sessionId, String(model), num(u.inputTokens), num(u.outputTokens),
        num(u.cacheReadInputTokens), num(u.cacheCreationInputTokens),
        num(u.webSearchRequests), num(u.costUSD));
    }
    this.stats.costStates = (this.stats.costStates ?? 0) + 1;
  }

  countType(t, known = 1) {
    this.typeCounts.set(t, (this.typeCounts.get(t) || 0) + 1);
    this.typeKnown.set(t, known ? 1 : 0);
  }

  /**
   * Types already in the log on disk, so `first_seen` means what it says.
   *
   * The dedup set was per-INSTANCE and never read the file, so every harvest that met a familiar
   * type appended another row stamped with the current time. Measured here: 4,716 rows of which
   * 3,679 were real types recorded over and over (981 `pr-link`, 953 `relocated`, 844
   * `history-suppression`), each asserting a first sighting that had happened months earlier.
   * The store already holds the correct answer in `record_types`, keyed on the type with a
   * count, so this file was a duplicate of a correct table AND it was lying.
   *
   * Read once and lazily: a harvest that meets nothing unknown never opens it.
   */
  seenOnDisk() {
    if (this.unknownLoaded) return;
    this.unknownLoaded = true;
    try {
      for (const line of readFileSync(this.unknownLog, 'utf8').split(String.fromCharCode(10))) {
        if (!line.trim()) continue;
        try { this.unknownSeen.add(JSON.parse(line).type); } catch { /* a torn line is not a type */ }
      }
    } catch { /* absent is the normal case on a fresh store */ }
  }

  noteUnknown(type, line) {
    this.unknownThisRun.add(type);
    if (this.unknownSeen.has(type)) return;
    this.seenOnDisk();
    if (this.unknownSeen.has(type)) return;
    this.unknownSeen.add(type);
    // open() makes this directory, but the self-test runs against an in-memory database and never
    // goes through open(). On a fresh clone, where data/ is gitignored and therefore absent, that
    // turned the FIRST command a new user runs into a stack trace. The writer owns its directory.
    ensureStoreDir(dirname(this.unknownLog));
    appendFileSync(this.unknownLog, JSON.stringify({
      first_seen: new Date().toISOString(), type, sample: line.slice(0, 4000),
    }) + String.fromCharCode(10));
  }

  /**
   * The plan an `ExitPlanMode` call carried, as a row of its own.
   *
   * A METHOD RATHER THAN A BLOCK INSIDE `scanBlocks`, because two callers need exactly this rule:
   * the incremental walk that sees the call as it arrives, and `backfillWork` for the transcripts
   * every store read before this table existed. Two copies of "what a plan row is" would have
   * drifted the first time one of them learned a new field.
   *
   * THE WHOLE PLAN, not `input_preview`: that holds 500 characters and JSON.stringify puts "plan"
   * first, so the preview is the proposal's opening sentence and the planFilePath is never in it.
   */
  plan(d, toolUseId, input, path, lineNo) {
    if (typeof toolUseId !== 'string' || !input || typeof input.plan !== 'string') return 0;
    this.stmt.putPlan.run(
      toolUseId, d.sessionId ?? null, d.uuid ?? null, d.timestamp ?? null,
      // UTF-16 CODE UNITS, which is what JavaScript's .length counts, not characters. On the
      // author's store 6 of 280 plans disagree with Python's len() by 2 to 11, and in every case
      // the difference is exactly the number of characters above the BMP: an emoji such as
      // U+1F530 is one character and two code units. Left as it is, because it is the count the
      // transcript itself was written with, and named here so nobody "fixes" it into disagreeing
      // with the file it describes.
      input.plan, input.plan.length,
      typeof input.planFilePath === 'string' ? input.planFilePath : null,
      input.allowedPrompts ? JSON.stringify(input.allowedPrompts) : null,
      d.isSidechain ? 1 : 0, path, lineNo);
    this.stats.plans++;
    return 1;
  }

  /** One `task_status` attachment as a row. The census that counts it is the caller's business. */
  taskEvent(d, path, lineNo) {
    const at = d?.attachment;
    if (!at || at.type !== 'task_status' || typeof d.uuid !== 'string') return 0;
    this.stmt.putTaskEvent.run(
      d.uuid, d.sessionId ?? null, d.timestamp ?? null, d.parentUuid ?? null,
      typeof at.taskId === 'string' ? at.taskId : null,
      typeof at.taskType === 'string' ? at.taskType : null,
      typeof at.status === 'string' ? at.status : null,
      typeof at.description === 'string' ? at.description : null,
      typeof at.deltaSummary === 'string' ? at.deltaSummary : null,
      typeof at.outputFilePath === 'string' ? at.outputFilePath : null,
      path, lineNo);
    this.stats.taskEvents++;
    return 1;
  }

  /**
   * The call's half of a file change: which file, and the text that went in.
   *
   * THIS IS THE ONLY HALF A SUBAGENT EDIT HAS. Measured before this existed: 20,852 of the
   * 32,613 successful edits on the author's store were made by subagents, and not one of their
   * results carries a `toolUseResult`. The call's `input` still holds the whole old and new text,
   * or the whole written file, and the store had been keeping a 500 character preview of it. So
   * the text is stored here, from the call, for every edit; the patch, when a result carries one,
   * is the other method's business.
   */
  change(d, toolUseId, name, input, path, lineNo) {
    if (typeof toolUseId !== 'string' || !input || typeof input !== 'object') return 0;
    const file = typeof input.file_path === 'string' ? input.file_path : null;
    const isEdit = name === 'Edit';
    const oldText = isEdit && typeof input.old_string === 'string' ? input.old_string : null;
    const newText = isEdit
      ? (typeof input.new_string === 'string' ? input.new_string : null)
      : (typeof input.content === 'string' ? input.content : null);
    if (file === null && oldText === null && newText === null) return 0;
    // Lines, not characters, because that is what a reader compares against a patch's counts.
    // An empty string is zero lines, not one; NULL stays NULL.
    const lines = (s) => (s === null ? null : s === '' ? 0 : s.split('\n').length);
    this.stmt.putChangeCall.run(
      toolUseId, d.sessionId ?? null, d.uuid ?? null, d.timestamp ?? null,
      name, file, oldText, newText, input.replace_all ? 1 : 0, lines(oldText), lines(newText),
      d.isSidechain ? 1 : 0, path, lineNo);
    this.stats.changes++;
    return 1;
  }

  /**
   * The result's half: the unified-diff hunks the tool produced, and what they add up to.
   *
   * ADDITIONS AND DELETIONS COME FROM THE HUNK PREFIXES, never from the old and new text. A hunk
   * that replaces one line with three is `-` once and `+` three times, and counting the texts
   * would call that one and one. The original file is measured and not kept: the hunks already
   * carry their context lines, and the whole file is 13 MB across the store for nothing a reader
   * would ask for.
   */
  changeResult(d, toolUseId, r, path, lineNo) {
    if (typeof toolUseId !== 'string' || !r || !Array.isArray(r.structuredPatch)) return 0;
    let additions = 0, deletions = 0;
    for (const hunk of r.structuredPatch) {
      for (const line of (hunk && Array.isArray(hunk.lines)) ? hunk.lines : []) {
        if (typeof line !== 'string') continue;
        if (line[0] === '+') additions++;
        else if (line[0] === '-') deletions++;
      }
    }
    const kind = typeof r.oldString === 'string' ? 'edit'
      : r.type === 'create' ? 'create' : r.type === 'update' ? 'update' : null;
    this.stmt.putChangeResult.run(
      toolUseId, d.sessionId ?? null, kind, JSON.stringify(r.structuredPatch),
      additions, deletions,
      typeof r.originalFile === 'string' ? r.originalFile.length : null,
      r.userModified ? 1 : 0, path, lineNo);
    this.stats.changePatches++;
    return 1;
  }

  /**
   * The `<project>/<session id>` directory a transcript belongs to, or null.
   *
   * Covers all three shapes a read file can take: the session's own transcript, a subagent
   * transcript under its `subagents` directory, and a workflow journal one level deeper again.
   * The segment after the project slug is the session either way, which is the same rule the
   * ingest already uses to name `project_slug` rather than trusting the parent directory.
   */
  sessionDirOf(path) {
    const parts = String(path).split(/[\\/]/);
    const under = parts.lastIndexOf('projects');
    if (under < 0 || under + 2 > parts.length - 1) return null;
    const head = parts[under + 2];
    const id = head.endsWith('.jsonl') ? head.slice(0, -6) : head;
    if (!TRANSCRIPT_NAME.test(id + '.jsonl')) return null;
    return parts.slice(0, under + 2).join(sep) + sep + id;
  }

  /**
   * The `.meta.json` and `wf_*.json` files beside a session's transcripts.
   *
   * NOT `.jsonl`, so `listTranscripts` never sees them and the byte-offset machinery never reads
   * them. They are small and are read whole, so `files` carries size and mtime for each and an
   * unchanged file is not opened: on this machine that is 7,432 agent metas and 208 workflow
   * files, and re-parsing them on every hook-driven harvest would be the whole cost of this
   * feature.
   */
  sidecars(sessionDir) {
    const read = (p) => {
      this.stats.sidecarsSeen++;
      let st;
      try { st = statSync(p); } catch { return null; }
      const prev = this.stmt.getFile.get(p);
      if (prev && prev.size === st.size && prev.mtime_ms === Math.round(st.mtimeMs)) return null;
      let parsed = null;
      try { parsed = JSON.parse(readFileSync(p, 'utf8')); } catch { parsed = null; }
      this.stats.sidecarsRead++;
      this.stmt.putSidecarFile.run(p, st.size, Math.round(st.mtimeMs), st.size,
                                   new Date().toISOString());
      return parsed && typeof parsed === 'object' ? { body: parsed, st } : null;
    };
    const sessionId = sessionDir.split(/[\\/]/).pop();
    const metas = [];
    const subagents = join(sessionDir, 'subagents');
    for (const entry of listDir(subagents)) {
      if (entry.isFile() && entry.name.endsWith('.meta.json')) {
        metas.push({ path: join(subagents, entry.name), runId: null });
      }
    }
    const wfRuns = join(subagents, 'workflows');
    for (const entry of listDir(wfRuns)) {
      if (!entry.isDirectory()) continue;
      for (const inner of listDir(join(wfRuns, entry.name))) {
        if (inner.isFile() && inner.name.endsWith('.meta.json')) {
          metas.push({ path: join(wfRuns, entry.name, inner.name), runId: entry.name });
        }
      }
    }
    for (const { path: p, runId } of metas) {
      const found = read(p);
      if (!found) continue;
      const m = found.body;
      const agentId = p.split(/[\\/]/).pop().replace(/^agent-/, '').replace(/\.meta\.json$/, '');
      // EVERY META BECOMES A ROW, including one with no toolUseId. All 6,636 workflow agents on
      // this machine lack it and 35 of the plain ones do too; skipping them would hold a tenth of
      // the runs and look complete.
      this.stmt.putAgentRun.run(
        agentId, sessionId,
        typeof m.toolUseId === 'string' ? m.toolUseId : null, runId,
        typeof m.agentType === 'string' ? m.agentType : null,
        typeof m.name === 'string' ? m.name : null,
        typeof m.description === 'string' ? m.description : null,
        Number.isFinite(m.spawnDepth) ? m.spawnDepth : null,
        typeof m.model === 'string' ? m.model : null,
        typeof m.parentAgentId === 'string' ? m.parentAgentId : null,
        m.stoppedByUser ? 1 : 0, JSON.stringify(m),
        p.replace(/\.meta\.json$/, '.jsonl'),
        p, found.st.size, Math.round(found.st.mtimeMs));
      this.stats.agentRuns++;
    }
    const wfDir = join(sessionDir, 'workflows');
    for (const entry of listDir(wfDir)) {
      if (!entry.isFile() || !entry.name.startsWith('wf_') || !entry.name.endsWith('.json')) continue;
      const p = join(wfDir, entry.name);
      const found = read(p);
      if (!found) continue;
      const w = found.body;
      const runId = typeof w.runId === 'string' ? w.runId : entry.name.slice(0, -5);
      // startTime is epoch MILLISECONDS. Read as a string it stores NULL on every row while the
      // code reads as though it worked, which is the shape putCostState already documents.
      const started = Number.isFinite(w.startTime) ? new Date(w.startTime).toISOString() : null;
      this.stmt.putWorkflow.run(
        runId, sessionId,
        typeof w.workflowName === 'string' ? w.workflowName : null,
        typeof w.status === 'string' ? w.status : null,
        started, typeof w.timestamp === 'string' ? w.timestamp : null,
        Number.isFinite(w.durationMs) ? w.durationMs : null,
        Number.isFinite(w.agentCount) ? w.agentCount : null,
        Number.isFinite(w.totalTokens) ? w.totalTokens : null,
        Number.isFinite(w.totalToolCalls) ? w.totalToolCalls : null,
        typeof w.defaultModel === 'string' ? w.defaultModel : null,
        typeof w.summary === 'string' ? w.summary : null,
        typeof w.result === 'string' ? w.result : (w.result ? JSON.stringify(w.result) : null),
        w.phases ? JSON.stringify(w.phases) : null,
        w.workflowProgress ? JSON.stringify(w.workflowProgress) : null,
        typeof w.error === 'string' ? w.error : null,
        p, found.st.size, Math.round(found.st.mtimeMs));
      this.stats.workflowRuns++;
      // The task id lives in the JSON as well as in the transcript, and the two agree. Written
      // only when the transcript has not already supplied it, so the launch record stays the
      // authority on which call this run belongs to.
      if (typeof w.taskId === 'string') {
        this.db.prepare('UPDATE workflow_runs SET task_id = ? WHERE run_id = ? AND task_id IS NULL')
          .run(w.taskId, runId);
      }
    }
  }

  async file(path, full) {
    if (this.excludedPaths.has(path)) { this.stats.excludedFiles++; return; }
    // THE SESSION TREE THIS FILE BELONGS TO, remembered for the sidecar pass. The trigger is "a
    // file under this session was read", not "a new transcript appeared": a workflow's JSON is
    // REWRITTEN when the run finishes, minutes after its agents stopped growing, so a new-file
    // trigger would leave every completed run stored as still running.
    const sessionDir = this.sessionDirOf(path);
    if (sessionDir) this.sessionDirs.add(sessionDir);
    const st = statSync(path);
    const prev = full ? null : this.stmt.getFile.get(path);
    let start = 0, lineNo = 0, rewritten = false;
    // The file's earliest timestamp, kept once known and never replaced (putFile COALESCEs it).
    // Resolved HERE, from the head of the file, rather than from the first record this pass
    // happens to read: a pass that resumes from an offset would otherwise stamp a LATER record as
    // the file's start, and a store migrated from before the column would never fill it for a
    // file that stopped growing, so every harvest would head-scan thousands of files to order them.
    let fileFirstTs = prev?.first_ts ?? firstTimestamp(path);

    if (prev) {
      if (st.size < prev.bytes_read) { rewritten = true; this.stats.rewrites++; }
      else if (st.size === prev.bytes_read) {
        this.stmt.putFile.run(path, st.size, Math.round(st.mtimeMs), prev.bytes_read, prev.lines_read, prev.rewrites, new Date().toISOString(), fileFirstTs);
        return;
      } else { start = prev.bytes_read; lineNo = prev.lines_read; }
    }

    this.stats.filesRead++;
    // The PROJECT directory, whatever depth the file sits at. `slice(-2)[0]` named the parent
    // directory, which for a subagent transcript under <session>/subagents/ is "subagents", a slug
    // that maps to 30 different working directories on one store (see store.project_label).
    const parts = path.split(/[\\/]/);
    const under = parts.lastIndexOf('projects');
    const projectSlug = under >= 0 && under + 1 < parts.length ? parts[under + 1] : parts.slice(-2)[0];
    const fileStem = parts[parts.length - 1].toLowerCase();
    let pendingBoundary = null;
    let consumed = start;

    const rl = createInterface({ input: createReadStream(path, { start, highWaterMark: 1 << 20 }), crlfDelay: Infinity });
    for await (const line of rl) {
      lineNo++;
      consumed += Buffer.byteLength(line, 'utf8') + 1;
      this.stats.lines++; this.stats.bytes += line.length;
      if (!line.trim()) continue;

      let d;
      try { d = JSON.parse(line); } catch { this.countType('UNPARSEABLE', 0); this.noteUnknown('UNPARSEABLE', line); continue; }

      if (fileFirstTs === null && typeof d.timestamp === 'string') fileFirstTs = d.timestamp;
      const type = typeof d.type === 'string' ? d.type : 'NO_TYPE_FIELD';
      const known = KNOWN_TYPES.has(type);
      this.countType(d.type === 'system' && d.subtype ? `system/${d.subtype}` : type, known);
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
        // 1 when this file IS the session's own top-level transcript, which is what decides
        // project_slug and transcript_path in the upsert; a line in a subagent file, or a
        // parent's line copied verbatim into a fork, never re-homes the row it names.
        const ownTop = String(d.sessionId).toLowerCase() + '.jsonl' === fileStem ? 1 : 0;
        this.stmt.putSession.run(d.sessionId, projectSlug, d.cwd ?? null, d.gitBranch ?? null,
          d.version ?? null, d.entrypoint ?? null, d.timestamp ?? null, d.timestamp ?? null, path,
          ownTop);
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
      } else if (type === 'cost-state' && d.sessionId) {
        this.putCostState(d, path, lineNo);
      } else if (d.type === 'attachment') {
        this.stmt.bumpAttachment.run(d.sessionId ?? 'unknown', d.attachment?.type ?? 'unknown');
        // DETAILED AS WELL AS COUNTED, never instead of. The census above is how an attachment
        // type nobody has seen becomes visible, and converting this branch rather than extending
        // it would take task_status out of it while looking like a richer answer.
        this.taskEvent(d, path, lineNo);
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
      (prev?.rewrites ?? 0) + (rewritten ? 1 : 0), new Date().toISOString(), fileFirstTs);
    // COUNTED AFTER THE READ, so a transcript abandoned above for an excluded project never
    // counts. run() re-derives a directory's chains only when one of these lands in it, because
    // that is what a resume looks like on disk and it is the only event that can change which
    // sessions are one chat. Two shapes: a file never seen before, and a file first seen while it
    // held only its head records (a hook harvest can run between the app creating the transcript
    // and it copying the history in) that has now grown past them.
    const headOnly = prev != null && prev.lines_read < HEAD_ONLY_LINES && lineNo > prev.lines_read;
    if (!prev) this.stats.newFiles = (this.stats.newFiles ?? 0) + 1;
    if (!prev || headOnly) this.stats.chainRelevant = (this.stats.chainRelevant ?? 0) + 1;
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
          typeof input.subagent_type === 'string' ? input.subagent_type : null,
          // THE INPUT, KEPT. `raw` was already built here to be hashed and measured, and then
          // dropped, which is why a rejected call could report its size and not its content.
          raw.slice(0, TOOL_INPUT_PREVIEW),
          // Trimmed, and empty becomes NULL: a blank description is the same as none, and
          // storing "" would make `description IS NOT NULL` stop meaning "this call has a note".
          (typeof input.description === 'string' && input.description.trim())
            ? input.description.trim() : null,
          // The two subselect binds for the outcome columns, which keep a result already stored
          // from being wiped by a re-read of this line.
          b.id, b.id);
        this.stats.toolCalls++;
        // THE PLAN ITSELF, beside the call rather than inside it. `input_preview` holds 500
        // characters and JSON.stringify puts "plan" first, so the preview is the proposal's
        // opening sentence and the planFilePath that names the file is never inside it.
        if (name === 'ExitPlanMode') this.plan(d, b.id, input, path, lineNo);
        if (name === 'Edit' || name === 'Write') this.change(d, b.id, name, input, path, lineNo);
      } else if (b.type === 'tool_result' && typeof b.tool_use_id === 'string') {
        const c = b.content;
        const bytes = typeof c === 'string' ? Buffer.byteLength(c, 'utf8')
          : Array.isArray(c) ? c.reduce((a, x) => a + (typeof x?.text === 'string'
            ? Buffer.byteLength(x.text, 'utf8') : 0), 0)
          : c == null ? 0 : Buffer.byteLength(JSON.stringify(c), 'utf8');
        // THE DENIAL IS ON THE RECORD, NOT ON THE BLOCK. Claude Code writes toolDenialKind as a
        // sibling of `message`; nothing inside the content block tells a tool that ran and failed
        // from one that never ran. `d` has been in scope here the whole time.
        const denial = (typeof d.toolDenialKind === 'string' && d.toolDenialKind)
          ? d.toolDenialKind : null;
        this.stmt.setToolResult.run(
          bytes, b.is_error ? 1 : 0,
          classifyResult({ isError: !!b.is_error, denialKind: denial, version: d.version }),
          denial, b.tool_use_id);
        this.stats.toolResults++;
        // WHICH CALL LAUNCHED A WORKFLOW, read from the same record the denial comes from. A
        // Workflow launch answers with toolUseResult {status:'async_launched', runId, taskId,
        // transcriptDir, scriptPath}; without it a workflow run is attached to a DIRECTORY and to
        // no moment in the conversation.
        //
        // GATED ON runId, NEVER ON taskId. A taskId is four different namespaces at once here:
        // 17 hex characters is a local agent, `w` plus eight is a workflow, `b` plus eight is a
        // Monitor task, and a small integer is a TodoWrite item. Keying on it merges all four.
        const launched = d.toolUseResult;
        if (launched && typeof launched === 'object'
            && typeof launched.runId === 'string' && launched.runId.startsWith('wf_')) {
          this.stmt.linkWorkflow.run(
            launched.runId, typeof launched.taskId === 'string' ? launched.taskId : null,
            b.tool_use_id, d.uuid ?? null, d.sessionId ?? null,
            typeof launched.transcriptDir === 'string' ? launched.transcriptDir : null,
            typeof launched.scriptPath === 'string' ? launched.scriptPath : null);
          this.stats.workflowLinks++;
        }
        // THE PATCH AN EDIT PRODUCED, on the same record. Only main-line results carry one; a
        // subagent's result is a plain string or nothing, and its row keeps the call's text alone.
        if (launched && typeof launched === 'object' && Array.isArray(launched.structuredPatch)) {
          this.changeResult(d, b.tool_use_id, launched, path, lineNo);
        }
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
function flushTypesFast(db, typeCounts, typeKnown) {
  const up = db.prepare(`INSERT INTO record_types (type,n,known) VALUES (?,?,?)
    ON CONFLICT(type) DO UPDATE SET n = n + excluded.n, known = excluded.known`);
  // `known` is OVERWRITTEN rather than left alone, so adding a name to KNOWN_TYPES reclassifies
  // the rows already counted under it on the next harvest. Otherwise a type stays marked unknown
  // for the life of the store after the very fix that recognised it.
  for (const [t, n] of typeCounts) up.run(t, n, typeKnown?.get(t) ?? 1);
}

async function run({ full, recordsRoots = null }) {
  const t0 = Date.now();
  const db = openDb();
  const h = new Harvest(db);
  // IN ORDER OF FIRST TIMESTAMP, not directory order. A resumed or forked transcript is a copy of
  // its predecessor, and the producer must be read first so that the copy is the one the ON
  // CONFLICT clauses refuse. Known files answer from the files table; a new file is head-scanned.
  const files = orderedTranscripts(db, listTranscripts(PROJECTS));
  h.stats.filesSeen = files.length;
  process.stderr.write(`harvest: ${files.length} transcripts under ${PROJECTS}\n`);

  // Every project directory that gained a NEW transcript this run (or one that has just grown
  // past its head records, see Harvest.file) gets its chains re-derived below. Not every
  // directory that had a file read: a hook harvest runs on every prompt, and re-indexing a
  // directory means streaming every transcript in it, which for a live chat is the whole
  // conversation so far. A resume is a new file; an append never changes a chain.
  const touched = new Set();
  db.exec('BEGIN');
  let sinceCommit = 0;
  for (let i = 0; i < files.length; i++) {
    const relevantBefore = h.stats.chainRelevant ?? 0;
    await h.file(files[i], full);
    if ((h.stats.chainRelevant ?? 0) > relevantBefore) {
      const dir = dirname(files[i]);
      if (dirname(dir) === PROJECTS) touched.add(dir);
    }
    if (++sinceCommit >= 200) { db.exec('COMMIT'); db.exec('BEGIN'); sinceCommit = 0; }
    if ((i + 1) % 500 === 0) {
      process.stderr.write(`  ${i + 1}/${files.length} files, ${h.stats.turns} turns, ${h.stats.compactions} compactions, ${(h.stats.bytes / 1048576).toFixed(0)} MB\n`);
    }
  }
  // A FULL PASS REPLACES THE CENSUS RATHER THAN ADDING TO IT. record_types is a cumulative upsert,
  // which is right for an incremental run that sees only new bytes and wrong for a full one that
  // has just re-read every line of every transcript: its tally IS the census, and adding it to the
  // previous one doubles every count. Inside the open transaction, so the delete and the reflush
  // commit together and a crash between them cannot leave the census empty.
  //
  // SCOPE, stated rather than implied: this fixes the --full path. A single transcript that SHRANK
  // is re-read from zero by the incremental path too, and its types are counted twice for that
  // file. Fixing that needs per-file counts, which is a schema change and a bigger piece of work
  // than this; it is not fixed here and should not be read as fixed.
  if (full) db.exec('DELETE FROM record_types');
  flushTypesFast(db, h.typeCounts, h.typeKnown);
  db.exec('COMMIT');

  // THE SIDECARS, for every session tree this run read a file under. Its own transaction, after
  // the ingest is committed: these are small JSON files beside the transcripts and a failure
  // reading them must not cost the bytes that were just ingested.
  const sidecars = { directories: h.sessionDirs.size, seen: 0, read: 0, agent_runs: 0,
                     workflow_runs: 0, failed: [] };
  if (h.sessionDirs.size) {
    db.exec('BEGIN');
    try {
      for (const dir of h.sessionDirs) h.sidecars(dir);
      db.exec('COMMIT');
    } catch (e) {
      try { db.exec('ROLLBACK'); } catch { /* nothing left to roll back */ }
      sidecars.failed.push(String(e && e.message ? e.message : e));
    }
    sidecars.seen = h.stats.sidecarsSeen;
    sidecars.read = h.stats.sidecarsRead;
    sidecars.agent_runs = h.stats.agentRuns;
    sidecars.workflow_runs = h.stats.workflowRuns;
  }

  // THE APP'S RECORDS, every pass, in a transaction of their own: a chat deleted in the app since
  // the last run is a record gone with a marker beside it, and stamping it here is what takes the
  // chat off every list on the next render. Cheap (one directory listing the chains pass makes
  // again below) and never fatal to the pass: a failure is reported in the run's output.
  let desktopRecords = { seen: 0, ledger: 0, deleted: 0, gone: 0, returned: 0, failed: null };
  db.exec('BEGIN');
  try {
    desktopRecords = { ...reconcileDesktopRecords(db, recordsRoots ?? resolveRecordsRoots([])), failed: null };
    db.exec('COMMIT');
  } catch (e) {
    try { db.exec('ROLLBACK'); } catch { /* nothing left to roll back */ }
    desktopRecords.failed = String(e && e.message ? e.message : e);
    process.stderr.write(`harvest: desktop records pass failed: ${desktopRecords.failed}\n`);
  }

  // THE CHAINS, for every directory touched. A resume that happened since the last run is a new
  // transcript in a directory that already holds its predecessor; deriving links for that one
  // directory is what folds it into its chat on the next render without anyone running anything.
  // Bounded by the directory, which is why it is affordable on every hook-driven harvest.
  const chains = { directories: touched.size, links: 0, reviews: 0, failed: [],
                   rows_moved: { turns: 0, messages: 0, compactions: 0, tool_calls: 0 },
                   rows_repaired: { cwd: 0, project_slug: 0, transcript_path: 0 } };
  if (touched.size) {
    // ONE TRANSACTION PER DIRECTORY, AND A FAILURE IS REPORTED, NOT RAISED. The ingest above is
    // already committed and must stay so; a directory whose pass throws (a sibling transcript
    // locked by its writer, SQLITE_BUSY past the timeout) is rolled back on its own, named in the
    // report, and picked up by the next run that touches it or by --backfill-chains. A single
    // transaction over every directory held the write lock for the whole pass, which on --full is
    // the whole corpus, long enough to starve a concurrent hook harvest past its busy timeout.
    let records = new Map();
    try {
      records = readDesktopRecords(recordsRoots ?? resolveRecordsRoots([]));
    } catch (e) {
      chains.failed.push({ dir: '(records)', error: String(e && e.message ? e.message : e) });
    }
    if (!chains.failed.length) {
      for (const dir of touched) {
        db.exec('BEGIN');
        try {
          const r = await reconcileDirectory(db, dir, records,
            { write: true, method: 'harvest', excludedCwds: h.excludedCwds });
          // THE REVIEW RUNS OF THIS DIRECTORY, in the same transaction: the reconcile above has
          // just put every session's slug right, so the directory's name finds them all.
          const ids = db.prepare('SELECT session_id FROM sessions WHERE project_slug = ?')
            .all(basename(dir)).map((row) => row.session_id);
          const rv = deriveReviews(db, ids, { write: true, method: 'harvest' });
          db.exec('COMMIT');
          chains.links += r.links;
          chains.reviews += rv.linked.length + rv.orphans.length;
          for (const k of Object.keys(chains.rows_moved)) chains.rows_moved[k] += r.moved[k];
          for (const k of Object.keys(chains.rows_repaired)) chains.rows_repaired[k] += r.repaired[k];
        } catch (e) {
          try { db.exec('ROLLBACK'); } catch { /* nothing left to roll back */ }
          const error = String(e && e.message ? e.message : e);
          chains.failed.push({ dir, error });
          process.stderr.write(`harvest: chains pass failed for ${dir}: ${error}\n`);
        }
      }
    }
  }

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
    chains,
    sidecars,
    desktop_records: desktopRecords,
    plans: h.stats.plans, task_events: h.stats.taskEvents, workflow_links: h.stats.workflowLinks,
    files_seen: h.stats.filesSeen, files_read: h.stats.filesRead, rewritten_files: h.stats.rewrites,
    // REPORTED, not merely counted. A run that quietly reads fewer files than it saw is
    // indistinguishable from a run where nothing was there, and the whole point of an exclusion
    // is that the absence afterwards is deliberate rather than a fault.
    excluded_files: h.stats.excludedFiles,
    lines: h.stats.lines, mb: +(h.stats.bytes / 1048576).toFixed(1),
    turn_records_seen: h.stats.turns, turn_rows_stored: rowTurns,
    // Only comparable on a full run: records_seen is per-run, rows_stored is cumulative.
    duplicate_turn_records: full ? h.stats.turns - rowTurns : null,
    compaction_records_seen: h.stats.compactions, compaction_rows_stored: rowComp,
    message_rows_stored: db.prepare('SELECT COUNT(*) n FROM messages').get().n,
    message_text_mb: +((db.prepare('SELECT COALESCE(SUM(chars),0) c FROM messages').get().c) / 1048576).toFixed(1),
    unpaired_boundaries: unpaired,
    unknown_record_types: [...h.unknownThisRun],
    seconds: +(ms / 1000).toFixed(1),
  };
  console.log(JSON.stringify(out, null, 2));
  if (h.stats.rewrites > 0) process.stderr.write(`WARNING: ${h.stats.rewrites} transcript(s) shrank since last harvest (local GC?). Re-read in full.\n`);
  if (h.stats.excludedFiles > 0) process.stderr.write(`NOTE: ${h.stats.excludedFiles} transcript(s) skipped, their project is in excluded_projects. Diagnostics lists them.\n`);
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

// The day-one shape, with every key --stats would print and a note saying why they are all zero.
//
// REACHING HERE MEANS THE DEFAULT PATH, WHICH IS WHY THIS IS SAFE. An explicit `--db X` or
// `C4X_DB=X` naming a store that does not exist is refused by resolveDb with exit 2 and never gets
// this far, and that refusal is the right answer: you asked for a specific store and it is not
// there. Only the default `<root>/data/context.db` can be missing at this point, and on day one it
// always is, because `install` wires hooks and the store appears on the first harvest.
//
// Exit 0 and valid JSON, because README's "Confirm it captured something" is the first command a
// new user runs and it ran before anything could have created a store. Exiting 1 there says
// something is broken when nothing is.
function emptyStats() {
  return {
    db: posix(DB_PATH),
    note: 'no store yet - it is created by the first harvest. The hooks run one for you at the '
        + 'end of a session; `node tools/harvest.mjs` does it now.',
    files: { n: 0, bytes: 0 },
    sessions: 0,
    turns: { n: 0, max_resident: null },
    api_calls: { n: 0, out_tok: 0, in_tok: 0, rows_behind_them: 0 },
    compactions: { n: 0, unpaired: 0, with_dropped: 0 },
    by_trigger: [],
    record_types: [],
    top_attachments: [],
    runs: [],
  };
}

function stats() {
  if (!existsSync(DB_PATH)) { console.log(JSON.stringify(emptyStats(), null, 2)); return 0; }
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
    db: posix(DB_PATH),
    // TRANSCRIPTS ONLY, and the sidecars beside them counted separately. The `files` table holds
    // both since the work pass shipped, and one line reading "files 24,386" over a store of 8,775
    // transcripts is the same wrong number the Summary card was about to print.
    files: q("SELECT COUNT(*) n, SUM(bytes_read) bytes FROM files WHERE kind IS NULL")[0],
    sidecars: q("SELECT COUNT(*) n, SUM(bytes_read) bytes FROM files WHERE kind = 'sidecar'")[0],
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
    session_links: q('SELECT COUNT(*) n, COUNT(DISTINCT head_id) chains FROM session_links')[0],
    review_links: q('SELECT COUNT(*) n, COUNT(DISTINCT head_id) heads FROM review_links')[0],
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
  // NOTHING THIS FUNCTION DOES MAY TOUCH THE USER'S CAPTURE DIRECTORY. The synthetic
  // `zzz-brand-new-type` record below is deliberately unknown, so it reached noteUnknown, which
  // appended it to the LIVE data/raw/unknown-records.ndjson on every run. 1,037 of those rows had
  // accumulated here, making the test fixture the most common "unknown record type" the tool
  // reports, on the tab whose job is telling the user what it could not parse.
  process.env.C4X_UNKNOWN_LOG = join(tmp, 'unknown-records.ndjson');
  const liveUnknownBefore = existsSync(UNKNOWN_LOG) ? statSync(UNKNOWN_LOG).size : -1;
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
  flushTypesFast(db, h.typeCounts, h.typeKnown);

  const checks = [];
  // THIS MODULE'S OWN SOURCE. Two fixes below live in run(), which the self-test cannot call: it
  // needs a real store, real transcripts and a real --full pass. Both were verified by mutation to
  // be invisible to every behavioural check here, so they are asserted against the text instead,
  // the way install.mjs asserts its call graph. A source gate is weaker than a behavioural one and
  // is used only where the behaviour is genuinely out of reach.
  const src = readFileSync(new URL(import.meta.url), 'utf8');
  checks.push(['CLI: --backfill-chains is dispatched', src.includes("argv.includes('--backfill-chains')")]);
  checks.push(['CLI: --backfill-reviews is dispatched and documented',
    src.includes("argv.includes('--backfill-reviews')") && src.includes('node harvest.mjs --backfill-reviews [--dry-run]')]);
  // The refusal runs in a child process, because it is the entry-point dispatch under test and
  // this process was entered with --self-test.
  {
    const probe = (args) => spawnSync(process.execPath, [SELF_PATH, ...args], { encoding: 'utf8' });
    const help = probe(['--help']);
    checks.push(['CLI: --help prints the usage and harvests nothing', help.status === 0 && help.stdout.includes('--backfill-chains')]);
    const bad = probe(['--hlep']);
    checks.push(['CLI: an unknown flag is refused rather than run as a harvest (gate can fail)',
      bad.status === 2 && bad.stderr.includes('unknown flag --hlep'), `status ${bad.status}`]);
  }
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

  // WHAT WROTE A MESSAGE, which the store used to answer with the record's type.
  //
  // Claude Code files a tool result as a record of type 'user'. Measured across every transcript on
  // this machine, 86.5% of the records typed 'user' were tool results and 13.0% were typed by a
  // person, and both were stored as "user". Every shape below is one the transcripts really
  // contain; the ones marked as gates are the classifications that were wrong before.
  {
    const user = (content, extra = {}) => ({ type: 'user', message: { content }, ...extra });
    checks.push(['a typed string is typed', messageKind(user('hello')) === 'typed']);
    checks.push(['a text block is typed',
      messageKind(user([{ type: 'text', text: 'hi' }])) === 'typed']);
    checks.push(['a tool result is NOT typed (gate can fail)',
      messageKind(user([{ type: 'tool_result', content: 'ls output' }])) === 'tool_result']);
    // A tool result usually arrives alone, but a text block can ride along. The bulk is still the
    // tool's output, and calling that typed is the whole defect again.
    checks.push(['a tool result beside a text block is still a tool result (gate can fail)',
      messageKind(user([{ type: 'tool_result', content: 'x' }, { type: 'text', text: 'y' }]))
        === 'tool_result']);
    checks.push(['an image is an attachment',
      messageKind(user([{ type: 'image', source: {} }])) === 'attachment']);
    checks.push(['a document is an attachment',
      messageKind(user([{ type: 'document', source: {} }])) === 'attachment']);
    // ORDER MATTERS: a compact summary is a 'user' record whose content is a plain string, so the
    // string case would claim it if it were tested first.
    checks.push(['a compact summary is not mistaken for typing',
      messageKind(user('This session is being continued', { isCompactSummary: true }))
        === 'compact_summary']);
    checks.push(['an assistant record is assistant',
      messageKind({ type: 'assistant', message: { content: [{ type: 'text', text: 'ok' }] } })
        === 'assistant']);
    checks.push(['a system record keeps its subtype',
      messageKind({ type: 'system', subtype: 'compact_boundary' }) === 'system/compact_boundary']);
  }

  // The backfill, against a store seeded the way the OLD harvester wrote one.
  {
    const dir = join(tmp, 'backfill');
    mkdirSync(join(dir, 'projects', 'P--x'), { recursive: true });
    const tf2 = join(dir, 'projects', 'P--x', 'sess.jsonl');
    const rows2 = [
      { type: 'user', uuid: 'b-typed', sessionId: 'bs', timestamp: '2026-08-20T00:00:00Z',
        message: { role: 'user', content: 'a question' } },
      { type: 'user', uuid: 'b-tool', sessionId: 'bs', timestamp: '2026-08-20T00:00:01Z',
        message: { role: 'user',
                   content: [{ type: 'tool_result', tool_use_id: 'tu', content: 'a listing' }] } },
      { type: 'assistant', uuid: 'b-asst', sessionId: 'bs', timestamp: '2026-08-20T00:00:02Z',
        message: { model: 'claude-opus-5', content: [{ type: 'text', text: 'an answer' }] } },
    ];
    writeFileSync(tf2, rows2.map((r) => JSON.stringify(r)).join('\n') + '\n');

    const dbFile = join(dir, 'store.db');
    rmSync(dbFile, { force: true });
    const seed = openDb(dbFile);
    const ins = seed.prepare(`INSERT INTO messages (uuid,session_id,ts,role,type,text,chars,file_path)
                              VALUES (?,?,?,?,?,?,?,?)`);
    ins.run('b-typed', 'bs', '2026-08-20T00:00:00Z', 'user', 'user', 'a question', 10, tf2);
    ins.run('b-tool', 'bs', '2026-08-20T00:00:01Z', 'user', 'user', 'a listing', 9, tf2);
    ins.run('b-asst', 'bs', '2026-08-20T00:00:02Z', 'assistant', 'assistant', 'an answer', 9, tf2);
    // A row whose transcript is gone. It can never be classified, and must not keep a label that
    // reads as something a person typed.
    ins.run('b-orphan', 'bs', '2026-08-20T00:00:03Z', 'user', 'user', 'lost', 4,
            join(dir, 'gone.jsonl'));
    // An ASSISTANT row whose transcript is also gone. Without this shape the fixture cannot tell a
    // correct backfill from one that sweeps 'assistant' into 'unknown', which is the bug the real
    // store exposed on a second pass and which this self-test failed to catch until the row
    // existed. Its label is already known from the record type and must survive.
    ins.run('b-asst-gone', 'bs', '2026-08-20T00:00:04Z', 'assistant', 'assistant', 'said', 4,
            join(dir, 'gone.jsonl'));
    const seeded = seed.prepare('SELECT COUNT(*) n FROM messages').get().n;
    seed.close();

    const code2 = await backfillMessageSource(dbFile,
      { quiet: true, projects: join(dir, 'projects') });

    const check = openDb(dbFile);
    const typeOf = (uuid) => check.prepare('SELECT type FROM messages WHERE uuid = ?').get(uuid).type;
    checks.push(['backfill: it exits 0', code2 === 0]);
    checks.push(['backfill: a typed message is relabelled typed', typeOf('b-typed') === 'typed']);
    checks.push(['backfill: a tool result is relabelled tool_result (gate can fail)',
      typeOf('b-tool') === 'tool_result']);
    checks.push(['backfill: an assistant message stays assistant', typeOf('b-asst') === 'assistant']);
    checks.push(['backfill: a row whose transcript is gone is marked unknown, not left as user',
      typeOf('b-orphan') === 'unknown']);
    checks.push(['backfill: it created no row',
      check.prepare('SELECT COUNT(*) n FROM messages').get().n === seeded]);
    // Running it again must change nothing. The first version relabelled 185 assistant rows on
    // every pass, because the unknown-marking swept 'assistant' as well as 'user'.
    const again = await backfillMessageSource(dbFile,
      { quiet: true, projects: join(dir, 'projects') });
    const after2 = openDb(dbFile);
    checks.push(['backfill: a second run changes nothing (gate can fail)',
      again === 0
        && after2.prepare("SELECT COUNT(*) n FROM messages WHERE type = 'unknown'").get().n === 1
        && after2.prepare("SELECT type FROM messages WHERE uuid = 'b-asst'").get().type === 'assistant'
        // The one that matters: its transcript is gone, and it is still an assistant message.
        && after2.prepare("SELECT type FROM messages WHERE uuid = 'b-asst-gone'").get().type
             === 'assistant']);
    after2.close();
    check.close();
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
  // AND IT DOES NOT RE-READ THE FILE TO LEARN THAT. `stored === 0` was already true before the
  // watermark existed, because INSERT OR IGNORE absorbed every duplicate - so that check passed
  // just as happily while the whole 44 MB log was being parsed on every one of this install's
  // 29,049 harvests. `seen` is the number that tells the two apart.
  checks.push(['and re-ingesting READS nothing (gate can fail)', ev2.seen === 0, String(ev2.seen)]);
  appendFileSync(evFile, JSON.stringify({ captured_at: '2026-08-22T00:00:02Z', probe: false, event: 'Stop', known: true, session_id: 'wm-append' }) + String.fromCharCode(10));
  const ev3 = ingestEvents(db, evFile);
  checks.push(['an appended line is read, and only that one', ev3.seen === 1 && ev3.stored === 1, JSON.stringify(ev3)]);
  // A line still being written must not be marked consumed, or it is lost for good.
  appendFileSync(evFile, '{"captured_at":"2026-08-22T00:00:03Z","event":"Pos');
  const ev4 = ingestEvents(db, evFile);
  checks.push(['a TORN trailing line is left for next time (gate can fail)', ev4.seen === 0, JSON.stringify(ev4)]);
  appendFileSync(evFile, 'tToolUse","known":true,"session_id":"wm-torn"}' + String.fromCharCode(10));
  const ev5 = ingestEvents(db, evFile);
  checks.push(['and is picked up once it completes', ev5.stored === 1, JSON.stringify(ev5)]);
  // Smaller than the watermark means cleared or rotated: read it from zero rather than skip it.
  writeFileSync(evFile, JSON.stringify({ captured_at: '2026-08-22T00:00:04Z', probe: false, event: 'Stop', known: true, session_id: 'wm-shrunk' }) + String.fromCharCode(10));
  const ev6 = ingestEvents(db, evFile);
  checks.push(['a SHRUNK log is re-read from zero, not skipped (gate can fail)',
    ev6.rewound === true && ev6.seen === 1, JSON.stringify(ev6)]);
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

  // Chains: which sessions are one chat, and which session produced each row. Eleven synthetic
  // transcripts in one project directory and four desktop records, covering every rule the two
  // pure functions apply, then the same files driven through ingest in the WRONG order to prove
  // reconcile puts the rows back, and through the real backfill entry point.
  {
    const cdir = join(tmp, 'chains');
    const pdir = join(cdir, 'projects', 'P--chain');
    const rdir = join(cdir, 'records', 'acct', 'org');
    mkdirSync(pdir, { recursive: true });
    mkdirSync(rdir, { recursive: true });
    const sid = (tag) => `${tag}-0000-4000-8000-000000000000`;
    const S = { A: sid('aaaaaaaa'), B: sid('bbbbbbbb'), C: sid('cccccccc'), F: sid('ffffffff'),
                X: sid('eeeeeeee'), AN: sid('a0a0a0a0'), AN2: sid('a1a1a1a1'),
                NEAR: sid('dddddddd'), NEAR2: sid('d1d1d1d1'), FAR: sid('fa0fa0fa'), FAR2: sid('fa1fa1fa'),
                G: sid('e0e0e0e0'), H: sid('e1e1e1e1') };
    const ids = (p, n) => Array.from({ length: n }, (_, i) => `${p}${i + 1}`);
    const a = ids('a', 10), b = ids('b', 21), c = ids('c', 30), f = ids('f', 10), x = ids('x', 12);
    // Prefixes that cannot collide: ids('fr2', 21) would yield fr21..fr29, which ids('fr', 100)
    // also yields, and nine shared leading records read as a continuation.
    const n = ids('n', 10), m = ids('m', 20), nr = ids('nr', 100), nr2 = ids('zn', 20);
    const fr = ids('fr', 100), fr2 = ids('zf', 21);
    const g = ids('g', 20), hh = ids('h', 10);
    const turnLine = (s, u, ts) => JSON.stringify({
      type: 'assistant', uuid: u, sessionId: s, timestamp: ts,
      message: { model: 'm', usage: { input_tokens: 1, cache_creation_input_tokens: 0,
                                      cache_read_input_tokens: 0, output_tokens: 1 },
                 content: [{ type: 'text', text: 'chain ' + u }] } });
    // One tool call and its result, with FIXED uuids so a copy carries the same identity.
    const toolLines = (s, ts) => [
      JSON.stringify({ type: 'assistant', uuid: 'tu-a', sessionId: s, timestamp: ts,
        message: { model: 'm', usage: { input_tokens: 1, output_tokens: 1 },
                   content: [{ type: 'tool_use', id: 'toolu_chain1', name: 'Read', input: { file_path: 'x' } }] } }),
      JSON.stringify({ type: 'user', uuid: 'tr-a', sessionId: s, timestamp: ts,
        message: { content: [{ type: 'tool_result', tool_use_id: 'toolu_chain1', content: 'twelve bytes' }] } }),
    ];
    const file = (s) => join(pdir, s + '.jsonl');
    const write = (s, uuids, ts, extra = []) => {
      writeFileSync(file(s), uuids.map((u) => turnLine(s, u, ts)).concat(extra).join('\n') + '\n');
    };
    const day = (d) => `2026-01-${String(d).padStart(2, '0')}T00:00:00.000Z`;
    // The tool lines are two more uuids, and every copy carries them, exactly as a resume does.
    write(S.A, a, day(1), toolLines(S.A, day(1)));                          // the chat's first session
    write(S.B, a.slice(0, 9).concat(b), day(2), toolLines(S.B, day(2)));    // resumed: 11 of A's 12
    write(S.C, a.slice(0, 9).concat(b, c), day(3), toolLines(S.C, day(3))); // resumed again; the app's record
    write(S.F, a.slice(0, 9).concat(b, f), day(4), toolLines(S.F, day(4))); // a fork of C, with its own record
    write(S.X, x, day(5));                                                  // unrelated
    write(S.AN, n, day(6));                                                 // a record holder ...
    write(S.AN2, n.concat(m), day(7));                                      // ... contained whole in a record-less one
    write(S.NEAR, nr, day(8));                                              // exactly 0.90 must link ...
    write(S.NEAR2, nr.slice(0, 90).concat(nr2), day(9));
    // ... and 0.89 must not. FAR2's OWN records come first, so it is not a continuation of FAR
    // either (a continuation begins with the records it continues); this pair tests the copy
    // threshold alone.
    write(S.FAR, fr, day(12));
    write(S.FAR2, fr2.concat(fr.slice(0, 89)), day(13));
    write(S.G, g, day(10));                                                 // its only container is a fork
    write(S.H, g.concat(hh), day(11));
    const rec = (name, body) => writeFileSync(join(rdir, name), JSON.stringify(body));
    rec('local_c.json', { cliSessionId: S.C, title: 'Chain', isArchived: false });
    rec('local_f.json', { cliSessionId: S.F, title: 'Chain (fork)', forkedFromSessionId: 'local_c' });
    rec('local_an.json', { cliSessionId: S.AN, title: 'Anchor' });
    rec('local_h.json', { cliSessionId: S.H, title: 'Elsewhere (fork)', forkedFromSessionId: 'local_zzz' });
    rec('scheduled-tasks.json', { scheduledTasks: [] });

    const index = await transcriptIndex(pdir);
    const records = readDesktopRecords([join(cdir, 'records')]);
    checks.push(['chains: every transcript in the directory is indexed', index.length === 13, String(index.length)]);
    checks.push(['chains: a file without a cliSessionId is not a chat', records.size === 4, String(records.size)]);
    checks.push(['chains: the fork flag comes from the record field', records.get(S.F)?.fork === true && records.get(S.C)?.fork === false]);
    const { rows: linkRows, stats: linkStats } = deriveLinks(index, records);
    const row = (s) => linkRows.find((r) => r.session_id === s);
    checks.push(['chains: a prefix links to its chain head (gate can fail)',
      row(S.A)?.head_id === S.C && row(S.B)?.head_id === S.C]);
    checks.push(['chains: the record holder outranks a smaller record-less container',
      row(S.A)?.next_id === S.C && row(S.A)?.head_kind === 'record']);
    checks.push(['chains: exactly 0.90 links and 0.89 does not (gate can fail)',
      row(S.NEAR)?.head_id === S.NEAR2 && !row(S.FAR)]);
    // Two anchors had a container: AN sits whole inside AN2 (a copy), and C's fork F begins with
    // C's records (a continuation). Neither links, and both are counted.
    checks.push(['chains: a record holder never becomes a prefix, even contained whole',
      !row(S.AN) && !row(S.C) && !row(S.F) && linkStats.anchors_kept === 2, String(linkStats.anchors_kept)]);
    checks.push(['chains: a session with nothing above it gets no row',
      !row(S.X) && !row(S.AN2) && !row(S.NEAR2) && !row(S.FAR2)]);
    checks.push(['chains: a fork record holder contains its own line, which resumed into it natively',
      row(S.G)?.head_id === S.H && row(S.G)?.head_kind === 'fork']);
    checks.push(['chains: every head is a session with no link of its own', linkRows.every((r) => !row(r.head_id))]);
    checks.push(['chains: four links and nothing else', linkRows.length === 4, String(linkRows.length)]);
    const { owner, toolOwner } = deriveOwners(index);
    checks.push(['owners: a copied record belongs to the earliest-starting transcript',
      owner.get('a1') === S.A && owner.get('b1') === S.B && owner.get('g1') === S.G]);
    checks.push(['owners: a record in one transcript only is not listed', !owner.has('c1') && !owner.has('x1')]);
    checks.push(['owners: a tool call follows the same rule', toolOwner.get('toolu_chain1') === S.A]);

    // Now through ingest, copy FIRST, which is the order the old walk could produce.
    const cdb = new DatabaseSync(':memory:');
    cdb.exec(SCHEMA);
    const ch = new Harvest(cdb);
    await ch.file(file(S.B), true);
    await ch.file(file(S.A), true);
    const holder = (u) => cdb.prepare('SELECT session_id FROM turns WHERE uuid = ?').get(u)?.session_id;
    checks.push(['ingest: read in the wrong order the copy claims the row (the premise, gate can fail)',
      holder('a1') === S.B]);
    const rc = await reconcileDirectory(cdb, pdir, records, { write: true });
    checks.push(['reconcile: the producer gets its rows back', holder('a1') === S.A && rc.moved.turns >= 9,
      `${holder('a1')} moved ${rc.moved.turns}`]);
    checks.push(['reconcile: messages and tool calls move with them',
      cdb.prepare('SELECT session_id FROM messages WHERE uuid = ?').get('a1')?.session_id === S.A
      && cdb.prepare('SELECT session_id FROM tool_calls WHERE tool_use_id = ?').get('toolu_chain1')?.session_id === S.A]);
    checks.push(['reconcile: links land in the store', cdb.prepare('SELECT COUNT(*) n FROM session_links').get().n === 4]);
    checks.push(['ingest: transcripts are ordered by first timestamp',
      orderedTranscripts(cdb, [file(S.B), file(S.A)])[0] === file(S.A)]);
    await ch.file(file(S.C), true);
    checks.push(['ingest: a later copy does not re-home a row (gate can fail)',
      holder('a1') === S.A && holder('b1') === S.B && holder('c1') === S.C]);
    await ch.file(file(S.F), true);
    checks.push(['ingest: a fork does not take its parent rows', holder('a1') === S.A && holder('f1') === S.F]);
    checks.push(['ingest: a copied tool_use leaves the stored result intact',
      cdb.prepare('SELECT result_bytes FROM tool_calls WHERE tool_use_id = ?').get('toolu_chain1')?.result_bytes === 12]);
    const lineBefore = cdb.prepare('SELECT line_no FROM turns WHERE uuid = ?').get('a1').line_no;
    writeFileSync(file(S.A), '\n' + readFileSync(file(S.A), 'utf8'));
    await ch.file(file(S.A), true);
    const lineAfter = cdb.prepare('SELECT line_no FROM turns WHERE uuid = ?').get('a1').line_no;
    checks.push(['ingest: a rewrite of the SAME session still updates the row (gate can fail)',
      lineAfter === lineBefore + 1, `${lineBefore} then ${lineAfter}`]);
    // Every column but linked_at, which is a fresh timestamp on every pass by design.
    const snap = () => JSON.stringify(cdb.prepare(`SELECT session_id, head_id, next_id, head_kind, overlap,
      prefix_uuids, next_uuids, shared_uuids, method FROM session_links ORDER BY session_id`).all());
    const s1 = snap();
    const again = await reconcileDirectory(cdb, pdir, records, { write: true });
    checks.push(['reconcile: a second pass changes nothing', snap() === s1 && again.moved.turns === 0]);
    const fresh = new DatabaseSync(':memory:');
    fresh.exec(SCHEMA);
    const fh = new Harvest(fresh);
    await fh.file(file(S.B), true);
    await fh.file(file(S.A), true);
    const dry = await reconcileDirectory(fresh, pdir, records, { write: false });
    checks.push(['reconcile: write:false writes nothing and still counts the moves',
      fresh.prepare('SELECT COUNT(*) n FROM session_links').get().n === 0 && dry.moved.turns >= 9 && dry.links === 4]);
    cdb.close();
    fresh.close();

    // The backfill entry point, on a temp store, with the directory and records overridden.
    const cdbPath = join(ROOT, 'tmp', `chains-selftest-${process.pid}-${Date.now()}.db`);
    rmSync(cdbPath, { force: true });
    {
      const t = new DatabaseSync(cdbPath);
      t.exec(SCHEMA);
      const th = new Harvest(t);
      await th.file(file(S.B), true);
      await th.file(file(S.A), true);
      t.close();
    }
    const opts = { quiet: true, projects: join(cdir, 'projects'), recordsRoots: [join(cdir, 'records')] };
    const rep = await backfillChains(cdbPath, opts);
    checks.push(['backfill-chains: links and moves reported, row counts unchanged',
      !!rep && rep.links_written === 4 && rep.rows_unchanged && rep.rows_moved.turns >= 9
      && rep.chains === 3 && rep.longest_chain === 2,
      rep && JSON.stringify({ links: rep.links_written, moved: rep.rows_moved.turns, chains: rep.chains })]);
    const rep2 = await backfillChains(cdbPath, opts);
    checks.push(['backfill-chains: idempotent',
      !!rep2 && rep2.links_before === 4 && rep2.links_after === 4 && rep2.rows_moved.turns === 0]);
    // COUNTED, NOT ASSERTED FROM THE REPORT'S OWN FLAG. This checked `wrote === false`, which the
    // run sets whether or not it wrote, so it was a gate that could not fail. Its sibling in the
    // sidecar backfill was the real thing: a dry run there wrote every row it read.
    const linksIn = () => {
      const s = new DatabaseSync(cdbPath, { readOnly: true });
      const links = s.prepare('SELECT COUNT(*) n FROM session_links').get().n;
      s.close();
      return links;
    };
    const linksBefore = linksIn();
    const dryRep = await backfillChains(cdbPath, { ...opts, write: false });
    checks.push(['backfill-chains: --dry-run reports and writes nothing (gate can fail)',
      !!dryRep && dryRep.wrote === false && linksIn() === linksBefore,
      String(linksIn() - linksBefore)]);
    rmSync(cdbPath, { force: true });
    rmSync(cdbPath + '-wal', { force: true });
    rmSync(cdbPath + '-shm', { force: true });
  }

  // Chains, second directory: the shapes an adversarial review found the first rules wrong on.
  // A verbatim fork copy that ties on first timestamp and sorts first; a resume that copied only
  // the post-compaction tail; a fork that was itself resumed; a copy in another working
  // directory; a stale link; an excluded project; a records root that does not exist.
  {
    const cdir2 = join(tmp, 'chains2');
    const pdir2 = join(cdir2, 'projects', 'P--chain2');
    const rdir2 = join(cdir2, 'records', 'acct', 'org');
    mkdirSync(pdir2, { recursive: true });
    mkdirSync(rdir2, { recursive: true });
    const sid = (tag) => `${tag}-0000-4000-8000-000000000000`;
    const P = sid('bbbbbbbb'), Q = sid('aaaaaaaa');                       // Q's name sorts BEFORE P's
    const A2 = sid('cccccccc'), B2 = sid('dddddddd');                     // tail-copy resume
    const C2 = sid('eeeeeeee'), F = sid('ffffffff'), F2 = sid('f2f2f2f2'); // a fork that was resumed
    const X2 = sid('e2e2e2e2'), X3 = sid('e3e3e3e3');                     // another working directory
    const ids = (p, n) => Array.from({ length: n }, (_, i) => `${p}${i + 1}`);
    const day = (d) => `2026-02-${String(d).padStart(2, '0')}T00:00:00.000Z`;
    const line = (s, u, ts, cwd) => JSON.stringify({
      type: 'assistant', uuid: u, sessionId: s, timestamp: ts, cwd,
      message: { model: 'm', usage: { input_tokens: 1, cache_creation_input_tokens: 0,
                                      cache_read_input_tokens: 0, output_tokens: 1 },
                 content: [{ type: 'text', text: 'x ' + u }] } });
    const head = (s, ts) => JSON.stringify({ type: 'queue-operation', operation: 'enqueue',
                                             timestamp: ts, sessionId: s, content: 'continue' });
    const fileOf = (s) => join(pdir2, s + '.jsonl');
    const write = (s, lines) => writeFileSync(fileOf(s), lines.join('\n') + '\n');
    // 1. Q is P copied byte for byte (P's sessionId, P's timestamps) plus one line of its own.
    const p = ids('p', 10);
    const pLines = p.map((u) => line(P, u, day(1), 'C:/w'));
    write(P, pLines);
    write(Q, pLines.concat([line(Q, 'q1', day(1), 'C:/w')]));
    // 2. B2 starts later, carries only A2's last ten records (their timestamps kept, sessionId
    //    rewritten, as a resume does) and then its own work, so it is never a superset of A2.
    const a = ids('a', 30);
    write(A2, a.map((u) => line(A2, u, day(2), 'C:/w')));
    write(B2, [head(B2, day(3))].concat(a.slice(20).map((u) => line(B2, u, day(2), 'C:/w')),
                                         ids('b', 10).map((u) => line(B2, u, day(3), 'C:/w'))));
    // 3. F forked C2 at 50 records and did 3 of its own; F2 resumed F. F is inside F2 whole and
    //    inside C2 nearly, and it belongs to F2.
    const c = ids('c', 100);
    write(C2, c.map((u) => line(C2, u, day(4), 'C:/w')));
    write(F, c.slice(0, 50).map((u) => line(F, u, day(4), 'C:/w'))
              .concat(ids('f', 3).map((u) => line(F, u, day(5), 'C:/w'))));
    write(F2, [head(F2, day(6))].concat(c.slice(0, 50).map((u) => line(F2, u, day(4), 'C:/w')),
                                         ids('f', 3).map((u) => line(F2, u, day(5), 'C:/w')),
                                         ids('g', 7).map((u) => line(F2, u, day(6), 'C:/w'))));
    // 4. X3 contains X2 whole, in a different working directory.
    const x = ids('x', 10);
    write(X2, x.map((u) => line(X2, u, day(7), 'C:/one')));
    write(X3, x.map((u) => line(X3, u, day(8), 'C:/two')).concat(ids('y', 20).map((u) => line(X3, u, day(8), 'C:/two'))));
    const rec = (name, body) => writeFileSync(join(rdir2, name), JSON.stringify(body));
    rec('local_p.json', { cliSessionId: P, title: 'Parent' });
    rec('local_q.json', { cliSessionId: Q, title: 'Parent (fork)', forkedFromSessionId: 'local_p' });
    rec('local_b2.json', { cliSessionId: B2, title: 'Resumed after compaction' });
    rec('local_c2.json', { cliSessionId: C2, title: 'Chat C' });
    rec('local_f2.json', { cliSessionId: F2, title: 'Chat C (fork)', forkedFromSessionId: 'local_c2' });

    const index2 = await transcriptIndex(pdir2);
    const records2 = readDesktopRecords([join(cdir2, 'records')]);
    const byId = Object.fromEntries(index2.map((f) => [f.id, f]));
    checks.push(['native: a verbatim fork copy owns only the line it wrote itself',
      byId[Q].native.size === 1 && byId[P].native.size === 10]);
    const { owner: owner2 } = deriveOwners(index2);
    checks.push(['owners: a fork copy that ties on first timestamp and sorts first does NOT take the parent rows (gate can fail)',
      p.every((u) => owner2.get(u) === P), JSON.stringify([...new Set(p.map((u) => owner2.get(u)?.slice(0, 8)))])]);
    const { rows: rows2, stats: stats2 } = deriveLinks(index2, records2);
    const row2 = (s) => rows2.find((r) => r.session_id === s);
    checks.push(['links: a resume that copied only the post-compaction tail is a continuation (gate can fail)',
      row2(A2)?.head_id === B2 && stats2.continuations >= 1]);
    checks.push(['links: a resumed fork folds into its own resume, not into the parent chat (gate can fail)',
      row2(F)?.head_id === F2, String(row2(F)?.head_id?.slice(0, 8))]);
    checks.push(['links: a copy in another working directory does not link', !row2(X2)]);
    checks.push(['links: record holders stay anchors', !row2(P) && !row2(Q) && !row2(B2) && !row2(C2) && !row2(F2)]);

    const cdb2 = new DatabaseSync(':memory:');
    cdb2.exec(SCHEMA);
    const ch2 = new Harvest(cdb2);
    await ch2.file(fileOf(Q), true);
    await ch2.file(fileOf(P), true);
    const holder2 = (u) => cdb2.prepare('SELECT session_id FROM turns WHERE uuid = ?').get(u)?.session_id;
    checks.push(['ingest: a fork copy is stored under the session its lines name', holder2('p1') === P]);
    cdb2.prepare(`INSERT INTO session_links (session_id,head_id,next_id,head_kind,overlap,prefix_uuids,next_uuids,shared_uuids,method,linked_at)
      VALUES (?,?,?,?,?,?,?,?,?,?)`).run('ghost000-0000-4000-8000-000000000000', P, P, 'record', 1, 1, 2, 1, 'test', 'x');
    const rc2 = await reconcileDirectory(cdb2, pdir2, records2, { write: true });
    checks.push(['reconcile: a parent keeps its rows after a fork copy was ingested first (gate can fail)',
      holder2('p1') === P && rc2.moved.turns === 0, `${holder2('p1')?.slice(0, 8)} moved ${rc2.moved.turns}`]);
    checks.push(['reconcile: a stale link whose transcript is gone is removed',
      cdb2.prepare("SELECT COUNT(*) n FROM session_links WHERE session_id LIKE 'ghost%'").get().n === 0]);
    const index3 = await transcriptIndex(pdir2, { excludedCwds: new Set(['C:/w']) });
    checks.push(['index: an excluded working directory is not read past its first cwd record',
      index3.length === 2 && index3.every((f) => f.cwd !== 'C:/w')]);
    checks.push(['records: an explicit root that does not exist is refused, not read as empty (gate can fail)', (() => {
      try { resolveRecordsRoots(['--records', join(tmp, 'no-such-records')]); return false; } catch { return true; }
    })()]);
    cdb2.close();
  }

  // Chains, third directory: the shapes a second adversarial review found. A verbatim fork that
  // outranks the parent's post-compaction resume; the fork resumed, so the parent's lines are
  // rewritten under the fork's line; a tail resume so small its predecessor "contains" it; two
  // forks of a parent whose transcript is gone; two resumes taken from the same compaction; a
  // session that changed directory; a subagent file harvested before the session's own.
  {
    const cdir3 = join(tmp, 'chains3');
    const pdir3 = join(cdir3, 'projects', 'C--home');
    const rdir3 = join(cdir3, 'records', 'acct', 'org');
    mkdirSync(pdir3, { recursive: true });
    mkdirSync(rdir3, { recursive: true });
    const sid = (tag) => `${tag}-0000-4000-8000-000000000003`;
    const PA = sid('aaaa0001'), PA2 = sid('aaaa0002');                        // parent, its post-compaction resume
    const FK = sid('bbbb0001'), FKH = sid('bbbb0002');                        // verbatim fork of PA, then resumed
    const TA = sid('cccc0001'), TB = sid('cccc0002');                         // a tiny tail resume
    const GONE = sid('dddd0000'), G1 = sid('dddd0001'), G2 = sid('dddd0002'); // forks of a deleted parent
    const SP = sid('eeee0001'), SB1 = sid('eeee0002'), SB2 = sid('eeee0003'); // two resumes from one compaction
    const MV = sid('ffff0001');                                               // changed directory mid-session
    const ids = (p, n) => Array.from({ length: n }, (_, i) => `${p}${i + 1}`);
    const day = (d) => `2026-03-${String(d).padStart(2, '0')}T00:00:00.000Z`;
    const line = (s, u, ts, cwd = 'C:/home') => JSON.stringify({
      type: 'assistant', uuid: u, sessionId: s, timestamp: ts, cwd,
      message: { model: 'm', usage: { input_tokens: 1, cache_creation_input_tokens: 0,
                                      cache_read_input_tokens: 0, output_tokens: 1 },
                 content: [{ type: 'text', text: 'y ' + u }] } });
    const head = (s, ts) => JSON.stringify({ type: 'queue-operation', operation: 'enqueue',
                                             timestamp: ts, sessionId: s, content: 'continue', cwd: 'C:/home' });
    const fileOf = (s) => join(pdir3, s + '.jsonl');
    const write = (s, lines) => writeFileSync(fileOf(s), lines.join('\n') + '\n');
    // 1. PA did 100 records. FK is PA copied verbatim (PA's sessionId, PA's timestamps) plus 10 of
    //    its own; FKH resumed FK and rewrote everything to itself, plus 5 more; the fork's record
    //    now names FKH. PA2 resumed PA after a compaction: PA's last 20 rewritten, plus 30 own.
    const pa = ids('pa', 100);
    const paLines = pa.map((u) => line(PA, u, day(1)));
    write(PA, paLines);
    write(FK, paLines.concat(ids('fk', 10).map((u) => line(FK, u, day(2)))));
    write(FKH, [head(FKH, day(3))].concat(pa.map((u) => line(FKH, u, day(1))),
                                          ids('fk', 10).map((u) => line(FKH, u, day(2))),
                                          ids('fh', 5).map((u) => line(FKH, u, day(3)))));
    write(PA2, [head(PA2, day(4))].concat(pa.slice(80).map((u) => line(PA2, u, day(1))),
                                          ids('pb', 30).map((u) => line(PA2, u, day(4)))));
    // 2. TB resumed TA after a compaction and did two records: TA "contains" 25 of TB's 27.
    const ta = ids('ta', 30);
    write(TA, ta.map((u) => line(TA, u, day(5))));
    write(TB, [head(TB, day(6))].concat(ta.slice(5).map((u) => line(TB, u, day(5))),
                                        ids('tb', 2).map((u) => line(TB, u, day(6)))));
    // 3. G1 and G2 are verbatim forks of GONE, whose own transcript was deleted.
    const gone = ids('go', 20);
    const goneLines = gone.map((u) => line(GONE, u, day(7)));
    write(G1, goneLines.concat(ids('g1', 5).map((u) => line(G1, u, day(8)))));
    write(G2, goneLines.concat(ids('g2', 5).map((u) => line(G2, u, day(9)))));
    // 4. SB1 and SB2 both resumed SP from the same compaction; SB1 did 3 records and was
    //    abandoned, SB2 did 10 and holds the record.
    const sp = ids('sp', 40);
    write(SP, sp.map((u) => line(SP, u, day(10))));
    write(SB1, [head(SB1, day(11))].concat(sp.slice(30).map((u) => line(SB1, u, day(10))),
                                           ids('s1', 3).map((u) => line(SB1, u, day(11)))));
    write(SB2, [head(SB2, day(12))].concat(sp.slice(30).map((u) => line(SB2, u, day(10))),
                                           ids('s2', 10).map((u) => line(SB2, u, day(12)))));
    // 5. MV started in C:/home and moved to C:/home/sub for its last records.
    write(MV, ids('mv', 10).map((u, i) => line(MV, u, day(13), i < 6 ? 'C:/home' : 'C:/home/sub')));
    // 7. CP resumed PA2 after another compaction (20 of PA2's own records, then 40 more) and holds
    //    the parent's record now. FZ is a second verbatim fork of PA, resumed after a compaction
    //    into FZC (FZ's last 5 own records, then 2 more), which holds the fork's record. For FZ,
    //    PA2 holds 20 of its 110 records natively and FZC only 5, and PA2 is the cousin: it holds
    //    nothing FZ wrote itself.
    const CP = sid('aaaa0003'), FZ = sid('bbbb0003'), FZC = sid('bbbb0004');
    write(CP, [head(CP, day(14))].concat(ids('pb', 30).slice(10).map((u) => line(CP, u, day(4))),
                                         ids('pc', 40).map((u) => line(CP, u, day(14)))));
    write(FZ, paLines.concat(ids('fz', 10).map((u) => line(FZ, u, day(16)))));
    write(FZC, [head(FZC, day(17))].concat(ids('fz', 10).slice(5).map((u) => line(FZC, u, day(16))),
                                           ids('fc', 2).map((u) => line(FZC, u, day(17)))));
    // 6. A subagent transcript naming MV, in the place Claude Code writes them.
    mkdirSync(join(pdir3, MV, 'subagents'), { recursive: true });
    const subPath = join(pdir3, MV, 'subagents', 'agent-x.jsonl');
    writeFileSync(subPath, [line(MV, 'sub1', day(13), 'C:/home/sub'), line(MV, 'sub2', day(13), 'C:/home/sub')].join('\n') + '\n');
    const rec = (name, body) => writeFileSync(join(rdir3, name), JSON.stringify(body));
    rec('local_pa.json', { cliSessionId: CP, title: 'Parent' });
    rec('local_fk.json', { cliSessionId: FKH, title: 'Parent (fork)', forkedFromSessionId: 'local_pa' });
    rec('local_fz.json', { cliSessionId: FZC, title: 'Parent (fork 2)', forkedFromSessionId: 'local_pa' });
    rec('local_tb.json', { cliSessionId: TB, title: 'Tiny' });
    rec('local_g1.json', { cliSessionId: G1, title: 'Gone (fork)', forkedFromSessionId: 'local_gone' });
    rec('local_g2.json', { cliSessionId: G2, title: 'Gone (fork)', forkedFromSessionId: 'local_gone' });
    rec('local_sb.json', { cliSessionId: SB2, title: 'Siblings' });

    const index4 = await transcriptIndex(pdir3);
    const records4 = readDesktopRecords([join(cdir3, 'records')]);
    const by4 = Object.fromEntries(index4.map((f) => [f.id, f]));
    checks.push(['index: only top-level transcripts are indexed, a subagent file is not',
      index4.length === 15 && !index4.some((f) => f.path === subPath), String(index4.length)]);
    checks.push(['index: a verbatim fork names the session its copy came from',
      by4[FK].foreignFrom.has(PA) && by4[FK].foreign.get('pa1') === PA && by4[FKH].foreignFrom.size === 0]);
    checks.push(['index: a session that changed directory keeps the directory its file lives in (gate can fail)',
      by4[MV].cwd === 'C:/home' && by4[MV].slug === 'C--home', String(by4[MV].cwd)]);
    const { rows: rows4, stats: stats4 } = deriveLinks(index4, records4);
    const row4 = (s) => rows4.find((r) => r.session_id === s);
    checks.push(['links: a parent resumed after a compaction follows its resume, not its verbatim fork (gate can fail)',
      row4(PA)?.next_id === PA2 && row4(PA)?.head_id === CP, String(row4(PA)?.next_id?.slice(0, 8))]);
    checks.push(['links: the fork\'s resume, which rewrote the parent\'s lines, is refused by the fork\'s own evidence',
      stats4.refused_lineage >= 1 && stats4.refused_foreign >= 1, JSON.stringify(stats4)]);
    checks.push(['links: the fork\'s first session folds into the fork\'s resume',
      row4(FK)?.head_id === FKH && row4(FK)?.head_kind === 'fork']);
    checks.push(['links: a parent-chat session holding more of a fork than the fork\'s own resume is still not its successor (gate can fail)',
      row4(FZ)?.head_id === FZC && row4(FZ)?.head_kind === 'fork' && stats4.refused_cousin >= 1
      && rows4.every((r) => (r.next_id !== PA2 && r.next_id !== CP) || r.session_id === PA || r.session_id === PA2),
      JSON.stringify([row4(FZ)?.next_id?.slice(0, 8), stats4.refused_cousin])]);
    checks.push(['links: the parent\'s later resumes chain to the parent\'s record',
      row4(PA2)?.head_id === CP && row4(PA2)?.head_kind === 'record']);
    checks.push(['links: a tiny tail resume is linked from its predecessor and never backwards (gate can fail)',
      row4(TA)?.head_id === TB && !row4(TB)]);
    checks.push(['links: two resumes from one compaction are one chat',
      row4(SP)?.head_id === SB2 && row4(SB1)?.head_id === SB2]);
    checks.push(['links: forks of a deleted parent do not link to each other', !row4(G1) && !row4(G2)]);
    checks.push(['links: no row names its own session as head', rows4.every((r) => r.head_id !== r.session_id)]);
    const { owner: owner4 } = deriveOwners(index4);
    checks.push(['owners: a deleted parent still owns what its forks copied (gate can fail)',
      gone.every((u) => owner4.get(u) === GONE), String(owner4.get('go1')?.slice(0, 8))]);
    checks.push(['owners: the fork\'s resume does not take the parent\'s records',
      pa.every((u) => owner4.get(u) === PA) && owner4.get('fk1') === FK]);

    // Ingest, subagent file first, then the session's own; then the store is repaired in place.
    const cdb4 = new DatabaseSync(':memory:');
    cdb4.exec(SCHEMA);
    const ch4 = new Harvest(cdb4);
    await ch4.file(subPath, true);
    const srow = () => cdb4.prepare('SELECT cwd, project_slug, transcript_path FROM sessions WHERE session_id = ?').get(MV);
    checks.push(['ingest: a subagent file names the project directory, not "subagents"', srow()?.project_slug === 'C--home']);
    await ch4.file(fileOf(MV), true);
    checks.push(['ingest: the session\'s own transcript wins the path and the directory it lives in (gate can fail)',
      srow()?.transcript_path === fileOf(MV) && srow()?.cwd === 'C:/home', JSON.stringify(srow())]);
    await ch4.file(subPath, true);
    checks.push(['ingest: a later subagent line does not move them back', srow()?.transcript_path === fileOf(MV) && srow()?.cwd === 'C:/home']);
    cdb4.prepare('UPDATE sessions SET cwd = ?, transcript_path = ?, project_slug = ? WHERE session_id = ?')
      .run('C:/home/sub', subPath, 'subagents', MV);
    const goneRows = cdb4.prepare('INSERT INTO turns (uuid, session_id, ts) VALUES (?,?,?)');
    for (const u of gone) goneRows.run(u, GONE, day(7));
    const dry4 = await reconcileDirectory(cdb4, pdir3, records4, { write: false });
    checks.push(['reconcile: --dry-run counts the identity repairs it would make',
      dry4.repaired.cwd === 1 && dry4.repaired.transcript_path === 1 && dry4.repaired.project_slug === 1
      && cdb4.prepare('SELECT cwd FROM sessions WHERE session_id = ?').get(MV).cwd === 'C:/home/sub']);
    const rc4 = await reconcileDirectory(cdb4, pdir3, records4, { write: true });
    checks.push(['reconcile: a row filed under the directory it moved to is put back where its file is (gate can fail)',
      rc4.repaired.cwd === 1 && srow()?.cwd === 'C:/home' && srow()?.transcript_path === fileOf(MV) && srow()?.project_slug === 'C--home',
      JSON.stringify(srow())]);
    checks.push(['reconcile: a deleted parent keeps its rows when its forks are the only files (gate can fail)',
      cdb4.prepare('SELECT COUNT(*) n FROM turns WHERE session_id = ?').get(GONE).n === 20 && rc4.moved.turns === 0,
      `moved ${rc4.moved.turns}`]);
    cdb4.close();
  }

  // Review runs: a one-shot that quotes what another session SAID and what its tools RETURNED is a
  // review of it; one that repeats a person's prompt word for word is not; an even tie names
  // nobody. Through the same ingest as the chains fixtures, then the derivation over the store.
  {
    // THE APP'S RECORDS, REMEMBERED: a record that goes with a marker is a deleted chat, one that
    // goes without is not, the ledger maps a record harvest never saw, and a record that returns
    // is clean again. Fed the marker's measured shape (13 digits of epoch ms) and a known-bad one.
    {
      const rroot = join(tmp, 'records-reconcile', 'root');
      const rdir = join(rroot, 'acct-1', 'org-1');
      mkdirSync(rdir, { recursive: true });
      // THE SIGNED-IN ACCOUNT, as the app records it beside the roots; a second account whose pair
      // is a link to the first (sharing, a junction on Windows and a symlink elsewhere); a third
      // that is a plain directory of its own.
      const rconfig = join(tmp, 'records-reconcile', 'config.json');
      writeFileSync(rconfig, JSON.stringify({ lastKnownAccountUuid: 'acct-2' }));
      writeFileSync(join(tmp, 'records-reconcile', 'plan-usage-history.json'),
        JSON.stringify({ samples: [{ t: 1, org: 'org-old' }, { t: 2, org: 'org-2' }] }));
      mkdirSync(join(rroot, 'acct-2'), { recursive: true });
      symlinkSync(rdir, join(rroot, 'acct-2', 'org-2'), 'junction');
      const rdir3 = join(rroot, 'acct-3', 'org-3');
      mkdirSync(rdir3, { recursive: true });
      const rbackups = join(tmp, 'records-reconcile', 'record-backups');
      const U1 = '11111111-1111-4111-8111-111111111111';
      const U2 = '22222222-2222-4222-8222-222222222222';
      const U3 = '33333333-3333-4333-8333-333333333333';
      const U4 = '44444444-4444-4444-8444-444444444444';
      const U5 = '55555555-5555-4555-8555-555555555555';
      const U6 = '66666666-6666-4666-8666-666666666666';
      const U9 = '99999999-9999-4999-8999-999999999999';
      const recAt = (u, sid, title, dir = rdir) => writeFileSync(join(dir, `local_${u}.json`),
        JSON.stringify({ cliSessionId: sid, title, isArchived: false }));
      recAt(U1, 'sess-1', 'One');
      recAt(U2, 'sess-2', 'Two');
      recAt(U4, 'sess-4', 'Four', rdir3);
      writeFileSync(join(rdir, 'scheduled-tasks.json'), '{"scheduledTasks": []}');
      const ledger = join(tmp, 'records-reconcile', 'adopted-records.json');
      writeFileSync(ledger, JSON.stringify([
        { session_id: 'sess-3', record: `local_${U3}`, path: join(rdir, `local_${U3}.json`), at: '2026-09-01T00:00:00Z' },
        { session_id: 'sess-1', record: `local_${U1}`, path: join(rdir, `local_${U1}.json`), at: '2026-09-02T00:00:00Z' },
        { nonsense: true }, null,
      ]));
      writeFileSync(join(rdir, `deleted_${U3}`), '1789434081365');   // the shape measured on the laptop
      writeFileSync(join(rdir, `deleted_${U9}`), '1789434081365');   // a uuid nobody knows
      const rdb = new DatabaseSync(':memory:');
      rdb.exec(SCHEMA);
      const row = (u) => rdb.prepare('SELECT * FROM desktop_records WHERE record_uuid = ?').get(u);
      const T1 = '2026-09-15T00:00:00.000Z';
      const first = reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: T1, backupsDir: rbackups });
      checks.push(['records: every record on disk is remembered with its session, title and flag',
        first.seen === 3 && row(U1)?.session_id === 'sess-1' && row(U1)?.title === 'One' && row(U1)?.archived === 0
        && row(U1)?.source === 'disk' && row(U1)?.deleted_at === null && row(U1)?.gone_at === null,
        JSON.stringify(first)]);
      checks.push(['records: a linked pair is not walked, and sharedPairs names the link and its target',
        (() => { const s = sharedPairs([rroot]);
                 return s.links.get('acct-2/org-2') === 'acct-1/org-1' && s.shared.has('acct-1/org-1')
                   && s.shared.has('acct-2/org-2') && !s.shared.has('acct-3/org-3'); })(),
        JSON.stringify([...sharedPairs([rroot]).links])]);
      checks.push(['records: the owner is the ledger pair for an adopted record, the signed-in account under sharing, the directory when unshared, the ledger for a seeded row (gate can fail)',
        row(U1)?.owner_source === 'ledger' && row(U1)?.owner_account === 'acct-1' && row(U1)?.owner_org === 'org-1'
        && row(U2)?.owner_source === 'signed-in' && row(U2)?.owner_account === 'acct-2' && row(U2)?.owner_org === 'org-2'
        && row(U4)?.owner_source === 'dir' && row(U4)?.owner_account === 'acct-3' && row(U4)?.owner_org === 'org-3'
        && row(U3)?.owner_source === 'ledger' && row(U3)?.owner_account === 'acct-1',
        JSON.stringify([U1, U2, U4, U3].map((u) => { const r = row(u); return r && [r.owner_account, r.owner_org, r.owner_source]; }))]);
      checks.push(['records: the signed-in pair is read the way appstate.desktop_pair reads it, dated by config.json',
        (() => { const p = signedInPair([rroot], listDesktopRecords([rroot]));
                 return !!p && p.account === 'acct-2' && p.org === 'org-2'
                   && p.switched_at === new Date(statSync(rconfig).mtimeMs).toISOString(); })(),
        JSON.stringify(signedInPair([rroot], listDesktopRecords([rroot])))]);
      checks.push(['records: the account log gains one row on the first pass',
        first.account_logged === true && rdb.prepare('SELECT COUNT(*) n FROM account_log').get().n === 1
        && rdb.prepare('SELECT account, org FROM account_log').get().account === 'acct-2']);
      // A switch before the next pass: the log grows, the owners already written do not move.
      writeFileSync(rconfig, JSON.stringify({ lastKnownAccountUuid: 'acct-3' }));
      checks.push(['records: the ledger seeds a record harvest never saw, and a known one is left alone',
        first.ledger === 1 && row(U3)?.source === 'ledger' && row(U3)?.session_id === 'sess-3'
        && row(U3)?.first_seen === '2026-09-01T00:00:00Z' && row(U1)?.source === 'disk']);
      checks.push(['records: a ledger record gone with a marker is a deleted chat, stamped from the marker (gate can fail)',
        row(U3)?.deleted_at === '2026-09-15T01:01:21.365Z' && row(U3)?.gone_at === T1 && first.deleted === 1,
        JSON.stringify(row(U3))]);
      checks.push(['records: a marker for a uuid nobody knows makes no row', !row(U9)]);
      // Then the app deletes one (record gone, marker written) and c4x takes another back (gone, no marker).
      rmSync(join(rdir, `local_${U1}.json`));
      writeFileSync(join(rdir, `deleted_${U1}`), '1789434081365');
      rmSync(join(rdir, `local_${U2}.json`));
      const T2 = '2026-09-15T01:00:00.000Z';
      const second = reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: T2, backupsDir: rbackups });
      checks.push(['records: a record gone with a marker beside it is deleted_at (gate can fail)',
        row(U1)?.deleted_at === '2026-09-15T01:01:21.365Z' && row(U1)?.gone_at === T2 && second.deleted === 1,
        JSON.stringify(row(U1))]);
      checks.push(['records: a record gone without a marker is gone_at only, never deleted (gate can fail)',
        row(U2)?.gone_at === T2 && row(U2)?.deleted_at === null && second.gone === 1, JSON.stringify(row(U2))]);
      checks.push(['records: an owner, once written, is not re-decided by a later signed-in account (gate can fail)',
        row(U2)?.owner_account === 'acct-2' && row(U2)?.owner_source === 'signed-in' && row(U4)?.owner_account === 'acct-3'
        && second.account_logged === true && rdb.prepare('SELECT COUNT(*) n FROM account_log').get().n === 2,
        JSON.stringify([row(U2), second.account_logged])]);
      checks.push(['records: a second pass leaves the stamps as they were, and adds no log row under the same account',
        (() => { const again = reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: '2026-09-15T02:00:00.000Z', backupsDir: rbackups });
                 return again.deleted === 0 && again.gone === 0 && row(U1)?.gone_at === T2 && row(U2)?.gone_at === T2
                   && again.account_logged === false && rdb.prepare('SELECT COUNT(*) n FROM account_log').get().n === 2; })()]);
      // A record that returns (a reinstall restoring records) is clean again, and keeps its owner.
      recAt(U2, 'sess-2', 'Two again');
      const third = reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: '2026-09-15T03:00:00.000Z', backupsDir: rbackups });
      checks.push(['records: a record that returns has both stamps cleared and its new title',
        third.returned === 1 && row(U2)?.gone_at === null && row(U2)?.deleted_at === null && row(U2)?.title === 'Two again'
        && row(U2)?.owner_account === 'acct-2']);
      // THE ROWS FROM BEFORE THE COLUMNS: two with no owner, one of them filed by a sharing backup's
      // manifest under a pair, the other with nothing to say.
      rdb.prepare(`INSERT INTO desktop_records (record_uuid, session_id, dir, first_seen, last_seen, source)
        VALUES (?, 'sess-5', ?, ?, ?, 'disk'), (?, 'sess-6', ?, ?, ?, 'disk')`).run(U5, rdir, T1, T1, U6, rdir, T1, T1);
      mkdirSync(join(rbackups, '20260915000000'), { recursive: true });
      writeFileSync(join(rbackups, '20260915000000', 'manifest.json'), JSON.stringify({
        files: [{ root: rroot, rel: `acct-3/org-3/local_${U5}.json` }, { root: rroot, rel: 'acct-3/org-3/scheduled-tasks.json' }] }));
      const fourth = reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: '2026-09-15T04:00:00.000Z', backupsDir: rbackups });
      checks.push(['records: a row with no owner is filled from the newest backup manifest, and one nothing answers is stamped unknown once (gate can fail)',
        row(U5)?.owner_source === 'manifest' && row(U5)?.owner_account === 'acct-3' && row(U5)?.owner_org === 'org-3'
        && row(U6)?.owner_source === 'unknown' && row(U6)?.owner_account === null
        && fourth.owners.tagged === 1 && fourth.owners.unknown === 1
        && reconcileDesktopRecords(rdb, [rroot], { ledgerPath: ledger, now: '2026-09-15T05:00:00.000Z', backupsDir: rbackups }).owners.unknown === 0,
        JSON.stringify([row(U5), row(U6), fourth.owners])]);
      // The marker's content: digits are epoch ms (or seconds), anything else still means deleted, at now.
      checks.push(['records: a marker in seconds or in prose still dates the delete',
        markerTime(rdir, U1, 'NOW') === '2026-09-15T01:01:21.365Z'
        && (() => { writeFileSync(join(rdir, `deleted_${U9}`), 'gone'); return markerTime(rdir, U9, 'NOW') === 'NOW'; })()
        && (() => { writeFileSync(join(rdir, `deleted_${U9}`), '1789434081'); return markerTime(rdir, U9, 'NOW') === '2026-09-15T01:01:21.000Z'; })()
        && markerTime(rdir, '00000000-0000-4000-8000-000000000000', 'NOW') === null]);
      // The packaged-install case: the ledger's dir is the writer's view; the marker sits under a root.
      const other = join(tmp, 'records-reconcile', 'other-root');
      mkdirSync(join(other, 'acct-1', 'org-1'), { recursive: true });
      writeFileSync(join(other, 'acct-1', 'org-1', `deleted_${U3}`), '1789434081365');
      checks.push(['records: a marker under another records root for the same pair is found',
        markerTime(join('X:', 'redirected', 'acct-1', 'org-1'), U3, 'NOW', [other]) === '2026-09-15T01:01:21.365Z']);
      rdb.close();
    }
    const vdir = join(tmp, 'reviews', 'projects', 'P--review');
    mkdirSync(vdir, { recursive: true });
    const sid = (tag) => `${tag}-0000-4000-8000-00000000000a`;
    const V = { P: sid('aaaa000a'), R1: sid('bbbb000a'), R2: sid('cccc000a'),
                P2: sid('dddd000a'), P3: sid('eeee000a'), R3: sid('ffff000a'), R4: sid('abab000a'),
                R5: sid('acac000a'), R6: sid('adad000a'), R7: sid('aeae000a'),
                P5: sid('afaf000a'), R8: sid('baba000a') };
    const CWD = 'P:\\review';
    // A folder of its own, so a run there has no pool at all: what an orphan looks like.
    const CWD2 = 'P:\\elsewhere';
    const at = (m) => `2026-05-01T10:${String(m).padStart(2, '0')}:00.000Z`;
    const L1 = 'The parser now rejects a trailing comma and the three tests that covered it pass again after the rewrite';
    const L2 = '12 passed in 0.41s, nothing skipped, and the fixture directory was removed on the way out cleanly';
    const T = 'Run this exact command with the Bash tool, then state the first and last lines of its output verbatim';
    const L4 = 'Both Delta chats said this exact sentence once, so a run quoting only it ties to neither of them';
    const L5 = 'And both said this second sentence too, word for word, which makes the tie an even one to break';
    const PREAMBLE = "You are reviewing another Claude instance's work before it is allowed to finish its turn.\n\n"
      + 'Look for a claim wider than its evidence.\n\n--- THE WORK ---\n';
    let vn = 0;
    const user = (s, ts, content, cwd = CWD) => JSON.stringify({ type: 'user', uuid: `v${++vn}`, sessionId: s,
      timestamp: ts, cwd, message: { role: 'user', content } });
    const said = (s, ts, text, cwd = CWD) => JSON.stringify({ type: 'assistant', uuid: `v${++vn}`, sessionId: s,
      timestamp: ts, cwd, message: { model: 'm', usage: { input_tokens: 1, cache_creation_input_tokens: 0,
                                                            cache_read_input_tokens: 0, output_tokens: 1 },
                                      content: [{ type: 'text', text }] } });
    const summary = (s, ts, text, cwd = CWD) => JSON.stringify({ type: 'user', uuid: `v${++vn}`, sessionId: s,
      timestamp: ts, cwd, isCompactSummary: true, message: { role: 'user', content: text } });
    const ran = (s, ts, id) => JSON.stringify({ type: 'assistant', uuid: `v${++vn}`, sessionId: s, timestamp: ts,
      cwd: CWD, message: { model: 'm', usage: { input_tokens: 1, output_tokens: 1 },
                           content: [{ type: 'tool_use', id, name: 'Bash', input: { command: 'pytest' } }] } });
    const got = (s, ts, id, text) => JSON.stringify({ type: 'user', uuid: `v${++vn}`, sessionId: s, timestamp: ts,
      cwd: CWD, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content: text }] } });
    const put = (s, lines) => writeFileSync(join(vdir, s + '.jsonl'), lines.join('\n') + '\n');
    put(V.P, [user(V.P, at(0), T), said(V.P, at(1), L1), ran(V.P, at(2), 'toolu_rv1'),
              got(V.P, at(3), 'toolu_rv1', L2), said(V.P, at(4), 'done')]);
    put(V.R1, [user(V.R1, at(6), PREAMBLE + 'CLAUDE SAID: ' + L1 + '\n\nOUTPUT WAS: ' + L2 + '\n'),
               said(V.R1, at(7), 'APPROVED\nchecked the claim against the output')]);
    put(V.R2, [user(V.R2, at(8), T), said(V.R2, at(9), 'done again')]);
    // Two typed prompts each, so neither parent is a one-shot itself.
    put(V.P2, [user(V.P2, at(0), 'first'), said(V.P2, at(1), L4), said(V.P2, at(2), L5), user(V.P2, at(3), 'more')]);
    put(V.P3, [user(V.P3, at(0), 'second'), said(V.P3, at(1), L4), said(V.P3, at(2), L5), user(V.P3, at(3), 'more')]);
    put(V.R3, [user(V.R3, at(10), PREAMBLE + 'CLAUDE SAID: ' + L4 + '\n\nCLAUDE SAID: ' + L5 + '\n'),
               said(V.R3, at(11), 'PROBLEMS\n- which one')]);
    // One said line and one typed line: the typed one counts once a said one matched.
    put(V.R4, [user(V.R4, at(12), PREAMBLE + 'USER: ' + T + '\n\nCLAUDE SAID: ' + L1 + '\n'),
               said(V.R4, at(13), 'fine')]);
    // ORPHANS AND THEIR NEIGHBOURS, in a folder with no other session. R5 quotes P's lines (said
    // in another folder) and replies with a verdict: a review of a chat this folder cannot name.
    // R6 quotes the same lines and replies in prose: a person pasting output, left alone. R7
    // repeats the typed prompt and replies with a verdict: typed-only lines, left alone.
    put(V.R5, [user(V.R5, at(14), PREAMBLE + 'CLAUDE SAID: ' + L1 + '\n\nOUTPUT WAS: ' + L2 + '\n', CWD2),
               said(V.R5, at(15), 'PROBLEMS\n- the claim is wider than the check', CWD2)]);
    put(V.R6, [user(V.R6, at(16), PREAMBLE + 'CLAUDE SAID: ' + L1 + '\n\nOUTPUT WAS: ' + L2 + '\n', CWD2),
               said(V.R6, at(17), 'I read through this and the parser change looks reasonable to me.', CWD2)]);
    put(V.R7, [user(V.R7, at(18), T, CWD2), said(V.R7, at(19), 'APPROVED\nnothing to add', CWD2)]);
    // A run quoting the chat's COMPACTION SUMMARY, a user-role row the model wrote (the laptop's
    // fourth unplaced run quoted exactly that). It ties to the chat like a quoted said line does.
    const S1 = 'The migration ran clean on the copy and the two counts that were compared agreed exactly by row';
    const S2 = 'Remaining work is the receipt field and the one docs paragraph that still names the old flag';
    put(V.P5, [user(V.P5, at(0), 'carry on'), summary(V.P5, at(1), 'This session is being continued.\n' + S1 + '\n' + S2 + '\n'),
               said(V.P5, at(2), 'continuing'), user(V.P5, at(3), 'thanks')]);
    put(V.R8, [user(V.R8, at(20), PREAMBLE + 'CLAUDE SAID: ' + S1 + '\n\nCLAUDE SAID: ' + S2 + '\n'),
               said(V.R8, at(21), 'APPROVED\nthe summary holds')]);
    const vdb = new DatabaseSync(':memory:');
    vdb.exec(SCHEMA);
    const vh = new Harvest(vdb);
    for (const s of Object.values(V)) await vh.file(join(vdir, s + '.jsonl'), true);
    const vids = Object.values(V);
    checks.push(['reviews: snippets are ASCII line tails with the label dropped, newest first',
      JSON.stringify(reviewSnippets('short\nCLAUDE SAID: ' + L1 + '\nOUTPUT WAS: caf\u00e9 ' + L2 + '\nUSER: ' + L4 + '\n'))
        === JSON.stringify([L4.slice(-120), L1.slice(-120)])
      && reviewSnippets('').length === 0
      && JSON.stringify(reviewSnippets('A LABEL: ' + 'x'.repeat(300))) === JSON.stringify(['x'.repeat(120)])
      && reviewSnippets(Array(20).fill(L1).join('\n')).length === REVIEW.WANT]);
    checks.push(['reviews: the verdict is the reply\'s first word, APPROVED or PROBLEMS or nothing',
      reviewVerdict('APPROVED\nfine') === 'APPROVED' && reviewVerdict('problems\n- x') === 'PROBLEMS'
      && reviewVerdict('Looking at this') === null && reviewVerdict(null) === null]);
    const dry = deriveReviews(vdb, vids, { write: false });
    checks.push(['reviews: write:false writes nothing and still reports the links (gate can fail)',
      vdb.prepare('SELECT COUNT(*) n FROM review_links').get().n === 0
      && vdb.prepare('SELECT COUNT(*) n FROM review_misses').get().n === 0
      && dry.linked.length === 3 && dry.orphans.length === 2]);
    const rv = deriveReviews(vdb, vids, { write: true, now: '2026-05-01T12:00:00.000Z' });
    const link = (s) => vdb.prepare('SELECT * FROM review_links WHERE session_id = ?').get(s);
    checks.push(['reviews: eight one-shots seen, the parents are not', rv.one_shots === 8, String(rv.one_shots)]);
    checks.push(['reviews: a run quoting a compaction summary of the chat ties to that chat (gate can fail)',
      link(V.R8) && link(V.R8).head_id === V.P5 && link(V.R8).hits === 2 && link(V.R8).verdict === 'APPROVED',
      JSON.stringify(link(V.R8))]);
    checks.push(['reviews: a verdict reply quoting lines said elsewhere is an orphan, head NULL (gate can fail)',
      link(V.R5) && link(V.R5).head_id === null && link(V.R5).hits === 2 && link(V.R5).verdict === 'PROBLEMS'
      && rv.orphans.length === 2, JSON.stringify(link(V.R5))]);
    checks.push(['reviews: the same quotes with a prose reply are left alone (gate can fail)', !link(V.R6)]);
    checks.push(['reviews: a repeated typed prompt with a verdict reply is left alone', !link(V.R7)]);
    checks.push(['reviews: a run quoting what the session said and what its tool returned is tied to it (gate can fail)',
      link(V.R1)?.head_id === V.P && link(V.R1)?.hits === 2 && link(V.R1)?.snippets === 3
      && link(V.R1)?.verdict === 'APPROVED' && link(V.R1)?.method === 'harvest'
      && link(V.R1)?.linked_at === '2026-05-01T12:00:00.000Z', JSON.stringify(link(V.R1))]);
    checks.push(['reviews: a run repeating a typed prompt word for word is NOT a review (gate can fail)', !link(V.R2)]);
    checks.push(['reviews: a typed line counts once a said line matched (gate can fail)',
      link(V.R4)?.head_id === V.P && link(V.R4)?.hits === 2 && link(V.R4)?.verdict === null, JSON.stringify(link(V.R4))]);
    // The even tie names nobody as head; with its verdict reply it is an orphan, the shape of two
    // of the four runs the laptop could not place (their fixture lines were said by two chats).
    checks.push(['reviews: an even tie names nobody, and with a verdict reply it is an orphan (gate can fail)',
      link(V.R3) && link(V.R3).head_id === null && link(V.R3).hits === 2 && link(V.R3).verdict === 'PROBLEMS'
      && rv.misses === 3, JSON.stringify({ r3: link(V.R3), misses: rv.misses })]);
    checks.push(['reviews: a miss remembers its pool',
      vdb.prepare('SELECT pool_key FROM review_misses WHERE session_id = ?').get(V.R2)?.pool_key
        === [V.P, V.P2, V.P3, V.P5].sort().join(',')]);
    const again = deriveReviews(vdb, vids, { write: true });
    checks.push(['reviews: a second pass asks nothing again',
      again.already === 5 && again.unchanged === 3 && again.hit_queries === 0 && again.linked.length === 0
      && again.orphans.length === 0]);
    // A session joining the pool makes the one miss in that folder worth asking again, and the
    // answer holds; the two misses in the other folder are untouched.
    const P4 = sid('a4a4000a');
    put(P4, [user(P4, at(5), 'late'), said(P4, at(6), 'nothing to do with it'), user(P4, at(7), 'still')]);
    await vh.file(join(vdir, P4 + '.jsonl'), true);
    const third = deriveReviews(vdb, vids.concat(P4), { write: true });
    checks.push(['reviews: a session joining the pool reopens its miss, and it stays a miss',
      third.hit_queries === 1 && third.misses === 1 && third.already === 5 && third.unchanged === 2,
      JSON.stringify({ q: third.hit_queries, m: third.misses, a: third.already, u: third.unchanged })]);
    // THE REBUILD: a store from the build before orphans, head_id NOT NULL, keeps its row and
    // takes a NULL head afterwards, with the index back on the new table.
    {
      const opath = join(tmp, 'reviews', 'old-shape.db');
      const odb = new DatabaseSync(opath);
      odb.exec(`CREATE TABLE review_links (
        session_id TEXT PRIMARY KEY, head_id TEXT NOT NULL, hits INTEGER NOT NULL, snippets INTEGER NOT NULL,
        verdict TEXT, method TEXT NOT NULL, linked_at TEXT NOT NULL, CHECK (head_id <> session_id));
        CREATE INDEX review_links_head ON review_links(head_id);
        INSERT INTO review_links VALUES ('r-old', 'h-old', 2, 3, 'APPROVED', 'test', '2026-05-01T00:00:00Z');
        CREATE TABLE review_misses (session_id TEXT PRIMARY KEY, pool_key TEXT NOT NULL, checked_at TEXT NOT NULL);
        INSERT INTO review_misses VALUES ('r-miss', 'a,b', '2026-05-01T00:00:00Z');`);
      odb.close();
      const ndb = openDb(opath);
      const missesLeft = ndb.prepare('SELECT COUNT(*) n FROM review_misses').get().n;
      checks.push(['reviews: the rebuild forgets every miss, so each is asked again under the orphan look', missesLeft === 0]);
      const oldRow = ndb.prepare("SELECT head_id FROM review_links WHERE session_id = 'r-old'").get();
      let nullOk = true;
      try { ndb.prepare("INSERT INTO review_links VALUES ('r-new', NULL, 2, 2, 'PROBLEMS', 'test', 'now')").run(); } catch { nullOk = false; }
      const indexed = ndb.prepare("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name='review_links_head'").get();
      const oldGone = !ndb.prepare("SELECT 1 FROM sqlite_master WHERE name='review_links_old'").get();
      ndb.close();
      checks.push(['reviews: a store with the NOT NULL head is rebuilt, rows kept, NULL accepted, index on the new table (gate can fail)',
        oldRow?.head_id === 'h-old' && nullOk && indexed?.tbl_name === 'review_links' && oldGone]);
    }
    // The backfill, on a file, both ways.
    const vpath = join(tmp, 'reviews', 'store.db');
    const fdb = openDb(vpath);
    const fh = new Harvest(fdb);
    for (const s of Object.values(V)) await fh.file(join(vdir, s + '.jsonl'), true);
    fdb.close();
    const dryRep = await backfillReviews(vpath, { quiet: true, write: false });
    const fcount = () => { const d = new DatabaseSync(vpath); const n = d.prepare('SELECT COUNT(*) n FROM review_links').get().n; d.close(); return n; };
    checks.push(['backfill-reviews: --dry-run reports and writes nothing (gate can fail)',
      dryRep.linked === 3 && dryRep.orphans === 2 && dryRep.wrote === false && fcount() === 0]);
    const rep = await backfillReviews(vpath, { quiet: true, write: true });
    checks.push(['backfill-reviews: the links and the orphans land, with their verdicts counted',
      rep.linked === 3 && rep.orphans === 2 && rep.by_verdict.APPROVED === 2 && rep.by_verdict.none === 1
      && rep.by_verdict.PROBLEMS === 2 && rep.heads === 2
      && fcount() === 5 && rep.links_after === 5 && rep.links_before === 0,
      JSON.stringify({ l: rep.linked, o: rep.orphans, v: rep.by_verdict, h: rep.heads, n: fcount() })]);
  }

  // Chains, fourth directory: a project this machine IMPORTED. The transcript is byte identical to
  // the one on the machine it came from, so every line still names THAT directory, and the session
  // row is the only place the move is recorded. Two things must hold: a harvest pass leaves the row
  // where the import put it, and a chat resumed here folds into the imported session rather than
  // standing beside it as a second row for the same conversation.
  {
    const cdir5 = join(tmp, 'imported');
    const pdir5 = join(cdir5, 'projects', 'D--Dest');
    mkdirSync(pdir5, { recursive: true });
    const sid = (tag) => `${tag}-0000-4000-8000-000000000005`;
    const IM = sid('aaaa0005'), RS = sid('bbbb0005');
    const ids = (p, n) => Array.from({ length: n }, (_, i) => `${p}${i + 1}`);
    const day = (d) => `2026-04-${String(d).padStart(2, '0')}T00:00:00.000Z`;
    const line = (s, u, ts, cwd) => JSON.stringify({
      type: 'assistant', uuid: u, sessionId: s, timestamp: ts, cwd,
      message: { model: 'm', usage: { input_tokens: 1, cache_creation_input_tokens: 0,
                                      cache_read_input_tokens: 0, output_tokens: 1 },
                 content: [{ type: 'text', text: 'y ' + u }] } });
    const fileOf = (s) => join(pdir5, `${s}.jsonl`);
    const write = (s, lines) => writeFileSync(fileOf(s), lines.join('\n') + '\n');
    write(IM, ids('im', 10).map((u) => line(IM, u, day(2), 'P:/Source')));
    // A resume rewrites the lines it copies under its own id, so these are native to RS.
    write(RS, ids('im', 10).map((u) => line(RS, u, day(3), 'D:/Dest'))
      .concat(ids('rs', 6).map((u) => line(RS, u, day(3), 'D:/Dest'))));

    const index5 = await transcriptIndex(pdir5);
    const by5 = Object.fromEntries(index5.map((f) => [f.id, f]));
    checks.push(['import: a moved transcript has no home cwd of its own, and says so',
      by5[IM].homeCwd === null && by5[IM].cwd === 'P:/Source' && by5[RS].homeCwd === 'D:/Dest',
      JSON.stringify([by5[IM].homeCwd, by5[IM].cwd])]);
    const bare = deriveLinks(index5, new Map()).rows;
    checks.push(['import: on the transcripts alone the move looks like two projects (gate can fail)',
      !bare.some((r) => r.session_id === IM), String(bare.length)]);

    const db5 = new DatabaseSync(':memory:');
    db5.exec(SCHEMA);
    const ins5 = db5.prepare(
      'INSERT INTO sessions (session_id, cwd, project_slug, transcript_path) VALUES (?,?,?,?)');
    ins5.run(IM, 'D:/Dest', 'D--Dest', fileOf(IM));
    ins5.run(RS, 'D:/Dest', 'D--Dest', fileOf(RS));
    const row5 = (s) => db5.prepare(
      'SELECT cwd, project_slug, transcript_path FROM sessions WHERE session_id = ?').get(s);
    const rc5 = await reconcileDirectory(db5, pdir5, new Map(), { write: true });
    checks.push(['import: a harvest pass leaves the imported row where the import put it (gate can fail)',
      rc5.repaired.cwd === 0 && row5(IM).cwd === 'D:/Dest' && row5(IM).project_slug === 'D--Dest',
      JSON.stringify(row5(IM))]);
    checks.push(['import: a chat resumed after the move folds into the imported session (gate can fail)',
      rc5.links === 1
      && db5.prepare('SELECT head_id FROM session_links WHERE session_id = ?').get(IM)?.head_id === RS,
      JSON.stringify(rc5.links)]);
    // The repair still fires for a stored cwd that names neither this directory nor the transcript.
    db5.prepare('UPDATE sessions SET cwd = ? WHERE session_id = ?').run('Z:/Elsewhere', IM);
    const rc6 = await reconcileDirectory(db5, pdir5, new Map(), { write: true });
    checks.push(['import: a stored cwd that matches neither the directory nor the file is repaired',
      rc6.repaired.cwd === 1 && row5(IM).cwd === 'P:/Source', JSON.stringify(row5(IM))]);
    db5.close();
  }

  // A chat's own work: the plan it wrote, the tasks it reported, the agents and workflows beside
  // its transcript. The sidecars are not .jsonl, so nothing else in this file would open them.
  {
    const cdir = join(tmp, 'work');
    const pdir = join(cdir, 'projects', 'P--work');
    const sid = 'aaaa1111-2222-4333-8444-555555555555';
    const sdir = join(pdir, sid);
    mkdirSync(join(sdir, 'subagents', 'workflows', 'wf_w1'), { recursive: true });
    mkdirSync(join(sdir, 'workflows'), { recursive: true });
    const meta = (dir, id, body) =>
      writeFileSync(join(dir, 'agent-' + id + '.meta.json'), JSON.stringify(body));
    meta(join(sdir, 'subagents'), 'a1', { agentType: 'Explore', description: 'look', spawnDepth: 1,
                                          toolUseId: 'tua', name: 'scout', model: 'sonnet' });
    meta(join(sdir, 'subagents'), 'a2', { agentType: 'general-purpose', description: 'no id' });
    meta(join(sdir, 'subagents', 'workflows', 'wf_w1'), 'a3', { agentType: 'workflow-subagent' });
    writeFileSync(join(sdir, 'subagents', 'agent-bad.meta.json'), '{not json');
    writeFileSync(join(sdir, 'workflows', 'wf_w1.json'), JSON.stringify({
      runId: 'wf_w1', taskId: 'wjournal1', workflowName: 'review-changes', status: 'completed',
      startTime: 1789200000000, timestamp: '2026-09-12T00:00:00.000Z', durationMs: 927840,
      agentCount: 19, totalTokens: 2559916, totalToolCalls: 429, defaultModel: 'opus',
      summary: 'found nine', result: { confirmed: 8 }, phases: [{ title: 'Find' }],
      workflowProgress: [{ label: 'find:export' }] }));

    const wdb = new DatabaseSync(':memory:');
    wdb.exec(SCHEMA);
    const hw = new Harvest(wdb, { unknownLog: join(cdir, 'unknown.ndjson') });

    // The session directory is derived from the path, whichever of the three shapes it takes.
    checks.push(['work: a session transcript names its own directory',
      hw.sessionDirOf(join(pdir, sid + '.jsonl')) === sdir,
      String(hw.sessionDirOf(join(pdir, sid + '.jsonl')))]);
    checks.push(['work: so does a subagent transcript under it',
      hw.sessionDirOf(join(sdir, 'subagents', 'agent-a1.jsonl')) === sdir]);
    checks.push(['work: and a workflow journal two levels deeper',
      hw.sessionDirOf(join(sdir, 'subagents', 'workflows', 'wf_w1', 'journal.jsonl')) === sdir]);

    hw.sidecars(sdir);
    const runOf = (id) => wdb.prepare('SELECT * FROM agent_runs WHERE agent_id = ?').get(id);
    checks.push(['work: an agent run is keyed on its own id and names the call that asked for it',
      runOf('a1')?.tool_use_id === 'tua' && runOf('a1')?.dir_session_id === sid
        && runOf('a1')?.agent_type === 'Explore' && runOf('a1')?.spawn_depth === 1,
      JSON.stringify(runOf('a1') && [runOf('a1').tool_use_id, runOf('a1').agent_type])]);
    checks.push(['work: the whole meta is kept, because its key set drifts between builds',
      JSON.parse(runOf('a1')?.meta_json ?? '{}').model === 'sonnet']);
    checks.push(['work: a run with no toolUseId is still a run (gate can fail)',
      !!runOf('a2') && runOf('a2').tool_use_id === null, String(!!runOf('a2'))]);
    checks.push(['work: a workflow agent lands under its run, not as a plain subagent (gate can fail)',
      runOf('a3')?.workflow_run_id === 'wf_w1' && runOf('a3')?.tool_use_id === null,
      String(runOf('a3')?.workflow_run_id)]);
    checks.push(['work: a meta that is not json is skipped and the pass continues',
      !runOf('bad') && wdb.prepare('SELECT COUNT(*) n FROM agent_runs').get().n === 3,
      String(wdb.prepare('SELECT COUNT(*) n FROM agent_runs').get().n)]);
    const wf = wdb.prepare('SELECT * FROM workflow_runs WHERE run_id = ?').get('wf_w1');
    checks.push(['work: a workflow run carries what the pane shows',
      wf?.workflow_name === 'review-changes' && wf?.agent_count === 19
        && wf?.total_tokens === 2559916 && wf?.status === 'completed',
      JSON.stringify(wf && [wf.workflow_name, wf.agent_count])]);
    checks.push(['work: its start time is read as epoch milliseconds, not as a string (gate can fail)',
      typeof wf?.started_at === 'string' && wf.started_at.startsWith('2026-'),
      String(wf?.started_at)]);

    // INCREMENTAL. 7,432 of these sit on the author's machine and a hook harvest runs on every
    // prompt: a pass that re-parses them all is the whole cost of the feature.
    const readAfterFirst = hw.stats.sidecarsRead;
    hw.sidecars(sdir);
    checks.push(['work: a second pass over unchanged sidecars reads none of them (gate can fail)',
      hw.stats.sidecarsRead === readAfterFirst, String(hw.stats.sidecarsRead - readAfterFirst)]);
    meta(join(sdir, 'subagents'), 'a2', { agentType: 'general-purpose', description: 'changed' });
    hw.sidecars(sdir);
    checks.push(['work: and one that changed is read again',
      hw.stats.sidecarsRead === readAfterFirst + 1 && runOf('a2')?.description === 'changed',
      String(runOf('a2')?.description)]);

    // THE TRANSCRIPT'S HALF OF THE ROW SURVIVES THE JSON'S, in either order.
    hw.scanBlocks({ sessionId: sid, uuid: 'ulaunch', timestamp: '2026-09-12T00:00:00Z',
      toolUseResult: { status: 'async_launched', taskId: 'wjournal1', runId: 'wf_w1' },
      message: { content: [{ type: 'tool_result', tool_use_id: 'tuw', content: 'ok' }] } }, 'f', 1);
    const both = wdb.prepare('SELECT * FROM workflow_runs WHERE run_id = ?').get('wf_w1');
    checks.push(['work: the launch fills its own columns without wiping the json is (gate can fail)',
      both?.tool_use_id === 'tuw' && both?.workflow_name === 'review-changes'
        && both?.agent_count === 19,
      JSON.stringify(both && [both.tool_use_id, both.workflow_name])]);

    // The task notifications inside the chat, detailed AND still counted.
    const tfile = join(pdir, sid + '.jsonl');
    const attach = (uuid, status) => JSON.stringify({
      type: 'attachment', uuid, parentUuid: 'p1', sessionId: sid,
      timestamp: '2026-09-12T00:0' + (status === 'running' ? '1' : '2') + ':00Z',
      attachment: { type: 'task_status', taskId: 'ada60911f63528f6a', taskType: 'local_agent',
                    status, description: 'verify the branch', deltaSummary: 'd',
                    outputFilePath: 'C:/t/out.txt' } });
    writeFileSync(tfile, attach('t1', 'running') + String.fromCharCode(10)
      + attach('t2', 'completed') + String.fromCharCode(10));
    await hw.file(tfile, true);
    checks.push(['work: a task notification is stored, not only counted (gate can fail)',
      wdb.prepare('SELECT COUNT(*) n FROM task_events').get().n === 2,
      String(wdb.prepare('SELECT COUNT(*) n FROM task_events').get().n)]);
    checks.push(['work: and the attachment census still counts it, which is how a new type shows up',
      wdb.prepare("SELECT n FROM attachments WHERE type = 'task_status'").get()?.n === 2,
      String(wdb.prepare("SELECT n FROM attachments WHERE type = 'task_status'").get()?.n)]);
    const ev = wdb.prepare('SELECT * FROM task_events WHERE uuid = ?').get('t2');
    checks.push(['work: it names the task, its state and the record it sits on',
      ev?.task_id === 'ada60911f63528f6a' && ev?.status === 'completed' && ev?.parent_uuid === 'p1'
        && ev?.task_type === 'local_agent']);
    checks.push(['work: reading that transcript remembered the session tree for the sidecar pass',
      hw.sessionDirs.has(sdir)]);
    wdb.close();

    // THE BACKFILL, ON A FILE, BOTH WAYS. A `--dry-run` that writes is worse than no dry run, and
    // the first one written here did exactly that: `BEGIN` was skipped when write was false, so
    // node:sqlite autocommitted every statement and the pass modified 120 session directories of
    // a copied store before dying on a ROLLBACK with no transaction to undo. The check counts rows
    // rather than reading the report's own `wrote` flag, which a broken run would still set.
    const sdbPath = join(cdir, 'sidecars.db');
    { const s = new DatabaseSync(sdbPath); s.exec(SCHEMA); s.close(); }
    const sopts = { quiet: true, projects: join(cdir, 'projects') };
    const rowsIn = () => {
      const s = new DatabaseSync(sdbPath, { readOnly: true });
      const n = s.prepare('SELECT COUNT(*) n FROM agent_runs').get().n
        + s.prepare('SELECT COUNT(*) n FROM workflow_runs').get().n;
      s.close();
      return n;
    };
    const dry = await backfillSidecars(sdbPath, { ...sopts, write: false });
    checks.push(['work: --dry-run counts what it would write (gate can fail)',
      !!dry && dry.wrote === false && dry.agent_runs === 3 && dry.workflow_runs === 1
        && dry.rows_unchanged,
      dry && JSON.stringify([dry.agent_runs, dry.workflow_runs])]);
    checks.push(['work: and leaves the store exactly as it found it (gate can fail)',
      rowsIn() === 0, String(rowsIn())]);
    const wet = await backfillSidecars(sdbPath, sopts);
    checks.push(['work: the same pass with write on lands the rows',
      !!wet && wet.wrote === true && rowsIn() === 4, String(rowsIn())]);

    // THE TRANSCRIPT BACKFILL, ON A FILE ALREADY MARKED FULLY READ. Both transcript writers ride
    // the byte offsets, so on every store that existed before them the plans and task
    // notifications already on disk are invisible and the panel reports the chat as having
    // planned nothing. This is the pass that answers that, and the offset row is what makes the
    // check about the real failure rather than about an empty database.
    writeFileSync(tfile, readFileSync(tfile, 'utf8') + JSON.stringify({
      type: 'assistant', uuid: 'uplan', sessionId: sid, timestamp: '2026-09-12T00:03:00Z',
      message: { content: [{ type: 'tool_use', id: 'tuplan', name: 'ExitPlanMode',
                             input: { plan: 'x'.repeat(800), planFilePath: 'C:/p/one.md' } }] },
    }) + String.fromCharCode(10));
    {
      const s = new DatabaseSync(sdbPath);
      s.prepare('INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts)'
        + ' VALUES (?,?,?,?,?,?,?)')
        .run(tfile, 99999, 0, 99999, 99, 0, 't');
      s.close();
    }
    const workOf = () => {
      const s = new DatabaseSync(sdbPath, { readOnly: true });
      const n = [s.prepare('SELECT COUNT(*) n FROM plans').get().n,
                 s.prepare('SELECT COUNT(*) n FROM task_events').get().n,
                 s.prepare('SELECT plan_chars c FROM plans WHERE tool_use_id = ?').get('tuplan')?.c];
      s.close();
      return n;
    };
    // BATCH SIZE 1, so the dry run crosses a commit boundary on the very first file. At the
    // default 200 this fixture's single transcript never reaches one, and the check that follows
    // passed over a pass that committed 253 rows of a real store it was told not to touch.
    const wdry = await backfillWork(sdbPath, { ...sopts, write: false, batch: 1 });
    checks.push(['work: --backfill-work finds a plan in a transcript already read to the end',
      !!wdry && wdry.plans_added === 1 && wdry.task_events_added === 2 && wdry.rows_unchanged,
      wdry && JSON.stringify([wdry.plans_added, wdry.task_events_added])]);
    checks.push(['work: and its dry run leaves the store alone (gate can fail)',
      JSON.stringify(workOf()) === JSON.stringify([0, 0, undefined]), JSON.stringify(workOf())]);
    await backfillWork(sdbPath, { ...sopts, batch: 1 });
    checks.push(['work: with write on the whole plan lands, not the 500 byte preview',
      JSON.stringify(workOf()) === JSON.stringify([1, 2, 800]), JSON.stringify(workOf())]);
    rmSync(sdbPath, { force: true });
    rmSync(sdbPath + '-wal', { force: true });
    rmSync(sdbPath + '-shm', { force: true });
  }

  // A chat's file changes: the call's text and the result's patch, landing alone or together in
  // either order, and the three shapes a reader must tell apart: an edit, a created file, and a
  // subagent edit whose result was never recorded.
  {
    const cdir = join(tmp, 'changes');
    const pdir = join(cdir, 'projects', 'P--changes');
    const sid = 'bbbb1111-2222-4333-8444-666666666666';
    mkdirSync(pdir, { recursive: true });
    const NL = String.fromCharCode(10);
    const call = (uuid, id, name, input, side = false) => JSON.stringify({
      type: 'assistant', uuid, sessionId: sid, timestamp: '2026-09-13T01:00:00Z', isSidechain: side,
      message: { content: [{ type: 'tool_use', id, name, input }] } });
    const result = (uuid, id, toolUseResult) => JSON.stringify({
      type: 'user', uuid, sessionId: sid, timestamp: '2026-09-13T01:00:01Z', toolUseResult,
      message: { content: [{ type: 'tool_result', tool_use_id: id, content: 'ok' }] } });
    // THE PREFIXES AND THE TEXTS DISAGREE ON PURPOSE: one old line, but the hunk removes two and
    // adds three. A counter that read the texts would say 1 and 3; the rule says 2 and 3.
    const hunk = { oldStart: 4, oldLines: 3, newStart: 4, newLines: 4,
                   lines: [' context', '-gone', '-also gone', '+one', '+two', '+three'] };
    const three = 'one' + NL + 'two' + NL + 'three';
    const editIn = { file_path: 'C:/p/a.py', old_string: 'gone', new_string: three };
    const editOut = { filePath: 'C:/p/a.py', oldString: 'gone', newString: three,
                      originalFile: 'x'.repeat(50), structuredPatch: [hunk], userModified: false,
                      replaceAll: false };
    const createIn = { file_path: 'C:/p/new.txt', content: 'a' + NL + 'b' };
    const createOut = { type: 'create', filePath: 'C:/p/new.txt', content: 'a' + NL + 'b',
                        originalFile: null, structuredPatch: [] };

    const cdb = new DatabaseSync(':memory:');
    cdb.exec(SCHEMA);
    const hc = new Harvest(cdb);
    const row = (id) => cdb.prepare('SELECT * FROM changes WHERE tool_use_id = ?').get(id);
    const brief = (r) => JSON.stringify(r && [r.file, r.old_text, r.old_lines, r.new_lines,
                                                r.additions, r.deletions, r.kind]);

    hc.scanBlocks(JSON.parse(call('u1', 'ch1', 'Edit', editIn)), 'f', 1);
    checks.push(['changes: the call alone lands the file and the text, with no patch',
      row('ch1')?.file === 'C:/p/a.py' && row('ch1')?.old_text === 'gone'
        && row('ch1')?.old_lines === 1 && row('ch1')?.new_lines === 3
        && row('ch1')?.patch_json === null && row('ch1')?.additions === null, brief(row('ch1'))]);
    hc.scanBlocks(JSON.parse(result('u2', 'ch1', editOut)), 'f', 2);
    checks.push(['changes: the result fills the patch without wiping the text (gate can fail)',
      row('ch1')?.old_text === 'gone' && row('ch1')?.patch_json !== null
        && row('ch1')?.kind === 'edit' && row('ch1')?.original_chars === 50, brief(row('ch1'))]);
    checks.push(['changes: additions and deletions come from the hunk prefixes, not the texts (gate can fail)',
      row('ch1')?.additions === 3 && row('ch1')?.deletions === 2, brief(row('ch1'))]);

    hc.scanBlocks(JSON.parse(result('u3', 'ch2', editOut)), 'f', 3);
    checks.push(['changes: the result alone lands the patch with no file and no text',
      row('ch2')?.additions === 3 && row('ch2')?.file === null && row('ch2')?.old_text === null,
      brief(row('ch2'))]);
    hc.scanBlocks(JSON.parse(call('u4', 'ch2', 'Edit', editIn)), 'f', 4);
    checks.push(['changes: and the call then fills the text without wiping the patch (gate can fail)',
      row('ch2')?.file === 'C:/p/a.py' && row('ch2')?.additions === 3
        && row('ch2')?.patch_json !== null, brief(row('ch2'))]);

    hc.scanBlocks(JSON.parse(call('u5', 'ch3', 'Write', createIn)), 'f', 5);
    hc.scanBlocks(JSON.parse(result('u6', 'ch3', createOut)), 'f', 6);
    checks.push(['changes: a Write that creates a file has no old text, two new lines, zero hunks, zero deletions',
      row('ch3')?.old_text === null && row('ch3')?.new_lines === 2 && row('ch3')?.kind === 'create'
        && row('ch3')?.additions === 0 && row('ch3')?.deletions === 0
        && row('ch3')?.original_chars === null, brief(row('ch3'))]);

    hc.scanBlocks(JSON.parse(call('u7', 'ch4', 'Edit', editIn, true)), 'f', 7);
    hc.scanBlocks(JSON.parse(result('u8', 'ch4', 'The file has been updated.')), 'f', 8);
    checks.push(['changes: a subagent edit keeps its text and stays patch-less, which is a state (gate can fail)',
      row('ch4')?.is_sidechain === 1 && row('ch4')?.old_text === 'gone'
        && row('ch4')?.patch_json === null && row('ch4')?.additions === null, brief(row('ch4'))]);

    const copy = JSON.parse(call('u9', 'ch1', 'Edit', { ...editIn, new_string: 'stolen' }));
    copy.sessionId = 'other-session';
    hc.scanBlocks(copy, 'g', 1);
    checks.push(['changes: a verbatim copy in a resumed session does not take the row (gate can fail)',
      row('ch1')?.session_id === sid && row('ch1')?.new_text === three, brief(row('ch1'))]);

    hc.scanBlocks(JSON.parse(call('u10', 'ch5', 'Read', { file_path: 'C:/p/a.py' })), 'f', 9);
    checks.push(['changes: a Read is not a change',
      !row('ch5') && cdb.prepare('SELECT COUNT(*) n FROM changes').get().n === 4]);
    checks.push(['changes: the counters count what was seen, including the refused copy',
      hc.stats.changes === 5 && hc.stats.changePatches === 3,
      JSON.stringify([hc.stats.changes, hc.stats.changePatches])]);
    cdb.close();

    // THE BACKFILL, on a transcript already marked read to the end, dry then wet, batch 1 so the
    // dry run crosses a commit boundary on its first file.
    const tfile = join(pdir, sid + '.jsonl');
    writeFileSync(tfile, [call('u1', 'ch1', 'Edit', editIn), result('u2', 'ch1', editOut),
                          call('u5', 'ch3', 'Write', createIn), result('u6', 'ch3', createOut)]
      .join(NL) + NL);
    const fdb = join(cdir, 'changes.db');
    {
      const s = new DatabaseSync(fdb);
      s.exec(SCHEMA);
      s.prepare('INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts)'
        + ' VALUES (?,?,?,?,?,?,?)').run(tfile, 99999, 0, 99999, 99, 0, 't');
      s.close();
    }
    const landed = () => {
      const s = new DatabaseSync(fdb, { readOnly: true });
      const out = [s.prepare('SELECT COUNT(*) n FROM changes').get().n,
                   s.prepare('SELECT additions, old_text FROM changes WHERE tool_use_id = ?').get('ch1')];
      s.close();
      return out;
    };
    const copts = { quiet: true, projects: join(cdir, 'projects'), batch: 1 };
    const dry = await backfillChanges(fdb, { ...copts, write: false });
    checks.push(['changes: --backfill-changes finds edits in a transcript already read to the end',
      !!dry && dry.changes_added === 2 && dry.changes_seen === 2 && dry.patches_seen === 2
        && dry.rows_unchanged, dry && JSON.stringify([dry.changes_added, dry.changes_seen, dry.patches_seen])]);
    checks.push(['changes: and its dry run leaves the store alone (gate can fail)',
      landed()[0] === 0, String(landed()[0])]);
    await backfillChanges(fdb, copts);
    checks.push(['changes: with write on both rows land complete, text and patch together',
      landed()[0] === 2 && landed()[1]?.additions === 3 && landed()[1]?.old_text === 'gone',
      JSON.stringify(landed())]);
    rmSync(fdb, { force: true });
    rmSync(fdb + '-wal', { force: true });
    rmSync(fdb + '-shm', { force: true });
  }

  // The two flags that stand between a bare invocation and a 10 GB re-read or a silent write.
  {
    const dir = join(ROOT, 'tmp', `plan-selftest-${process.pid}-${Date.now()}`);
    mkdirSync(dir, { recursive: true });
    const fake = (name, bytes) => { const p = join(dir, name); writeFileSync(p, 'x'.repeat(bytes)); return p; };
    const unchanged = fake('unchanged.jsonl', 10);
    const grown = fake('grown.jsonl', 20);
    const fresh = fake('fresh.jsonl', 30);
    const shrunk = fake('shrunk.jsonl', 20);
    const scratch = join(dir, 'scratch.db');
    {
      const s = new DatabaseSync(scratch);
      s.exec(SCHEMA);
      const put = s.prepare('INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,last_harvest_ts) VALUES (?,?,?,?,?,?,?)');
      put.run(unchanged, 10, 0, 10, 1, 0, 't');
      put.run(grown, 5, 0, 5, 1, 0, 't');
      put.run(shrunk, 50, 0, 50, 1, 0, 't');
      s.close();
    }
    const files = [unchanged, grown, fresh, shrunk];
    const ro = new DatabaseSync(scratch, { readOnly: true });
    const get = ro.prepare('SELECT bytes_read FROM files WHERE path = ?');
    const p = plan(files, (path) => get.get(path) || null);
    ro.close();
    checks.push(['plan: an unchanged file is not read', p.unchanged === 1, JSON.stringify(p)]);
    checks.push(['plan: a grown file is read from its offset, a new one whole, a shrunk one whole as a rewrite',
      p.would_read === 3 && p.rewritten === 1 && p.bytes_to_read === 15 + 30 + 20, JSON.stringify(p)]);
    checks.push(['plan: a plan over no store reads everything (gate can fail)',
      plan(files, () => null).bytes_to_read === 80]);
    checks.push(['--full without --yes is refused with the file count and the byte total',
      (() => { const g = fullGate(['--full'], files); return typeof g === 'string' && g.includes('4 files') && g.includes('--yes'); })()]);
    checks.push(['--full --yes is allowed', fullGate(['--full', '--yes'], files) === null]);
    checks.push(['a bare incremental run is not gated', fullGate([], files) === null]);
    // Through the command line, against the scratch store: the refusal exits 2 before any open,
    // the dry run exits 0, and neither touches the store's bytes.
    const before = statSync(scratch).mtimeMs;
    const refused = spawnSync(process.execPath, [process.argv[1], '--full', '--db', scratch], { encoding: 'utf8', cwd: ROOT, windowsHide: true });
    checks.push(['CLI: --full without --yes exits 2 and names the flag', refused.status === 2 && /--yes/.test(refused.stderr), `${refused.status} ${refused.stderr.slice(0, 120)}`]);
    const dry = spawnSync(process.execPath, [process.argv[1], '--dry-run', '--db', scratch], { encoding: 'utf8', cwd: ROOT, windowsHide: true });
    checks.push(['CLI: --dry-run exits 0 and says nothing was written', dry.status === 0 && /Nothing was written/.test(dry.stdout), `${dry.status} ${(dry.stderr || dry.stdout).slice(0, 160)}`]);
    checks.push(['CLI: neither touched the store', statSync(scratch).mtimeMs === before]);
    rmSync(dir, { recursive: true, force: true });
  }

  // Negative control: the same assertions against an EMPTY store must fail.
  const empty = new DatabaseSync(':memory:');
  empty.exec(SCHEMA);
  const mustFail = !empty.prepare('SELECT * FROM turns WHERE uuid = ?').get('u1');
  checks.push(['negative control: empty store has no turn (gate can fail)', mustFail]);

  // THE SELF-TEST'S OWN FOOTPRINT, asserted rather than assumed. The synthetic unknown record
  // must have landed in the scratch log and NOT in the live one, and the live file's size must be
  // exactly what it was when this function started.
  const scratchLog = join(tmp, 'unknown-records.ndjson');
  checks.push(['the synthetic unknown type is recorded in the SCRATCH log',
    existsSync(scratchLog) && readFileSync(scratchLog, 'utf8').includes('zzz-brand-new-type')]);
  checks.push(['and the live capture log is untouched (gate can fail)',
    liveUnknownBefore === (existsSync(UNKNOWN_LOG) ? statSync(UNKNOWN_LOG).size : -1)]);

  // first_seen means what it says: a type already on disk is not appended a second time.
  {
    const scratchDb = new DatabaseSync(':memory:');
    scratchDb.exec(SCHEMA);
    const again = new Harvest(scratchDb, { unknownLog: scratchLog });
    const before = readFileSync(scratchLog, 'utf8').split(String.fromCharCode(10)).filter(Boolean).length;
    again.noteUnknown('zzz-brand-new-type', '{}');
    const after = readFileSync(scratchLog, 'utf8').split(String.fromCharCode(10)).filter(Boolean).length;
    checks.push(['a type already in the log is NOT re-recorded (gate can fail)', after === before]);
    again.noteUnknown('zzz-second-new-type', '{}');
    const third = readFileSync(scratchLog, 'utf8').split(String.fromCharCode(10)).filter(Boolean).length;
    checks.push(['but a genuinely new type still is', third === before + 1]);
  }

  // WHAT THIS RUN COULD NOT PARSE, not what the log has ever held. A type recognised later kept
  // being announced as unrecognised for the life of the store, because the report read the
  // append-dedup memory, which is loaded from disk and never forgets.
  {
    const scratchDb = new DatabaseSync(':memory:');
    scratchDb.exec(SCHEMA);
    const seeded = join(tmp, 'seeded-unknowns.ndjson');
    writeFileSync(seeded, JSON.stringify({ type: 'cost-state', first_seen: 'x' }) + String.fromCharCode(10));
    const h4 = new Harvest(scratchDb, { unknownLog: seeded });
    // PRE-LOADED ON PURPOSE. seenOnDisk() reads the log lazily, so on a first call the dedup return
    // does not fire and this check passed whether the per-run add sat before or after it: a check
    // that cannot fail. Seeding the set reproduces the state the guard actually guards.
    h4.unknownSeen.add('cost-state');
    h4.noteUnknown('cost-state', '{}');
    h4.noteUnknown('zzz-really-new', '{}');
    checks.push(['a type already in the log is still reported by the run that met it (gate can fail)',
      h4.unknownThisRun.has('cost-state') && h4.unknownThisRun.has('zzz-really-new'),
      [...h4.unknownThisRun].join(',')]);
    // WHICH SET THE REPORT READS is the actual defect, and no assertion over the sets can see it.
    checks.push(['the run report reads the PER-RUN set, not the on-disk memory (gate can fail)',
      /unknown_record_types:\s*\[\.\.\.h\.unknownThisRun\]/.test(src),
      (src.match(/unknown_record_types:.*/) || ['not found'])[0].slice(0, 80)]);
    const fresh = new Harvest(scratchDb, { unknownLog: seeded });
    fresh.noteUnknown('zzz-really-new', '{}');
    checks.push(['but a run that did NOT meet it does not report it (gate can fail)',
      !fresh.unknownThisRun.has('cost-state'), [...fresh.unknownThisRun].join(',')]);
    scratchDb.close();
  }

  // A FULL PASS REPLACES THE CENSUS. The upsert is cumulative, so re-reading every transcript and
  // adding the tally to the previous one doubles every count.
  {
    const scratchDb = new DatabaseSync(':memory:');
    scratchDb.exec(SCHEMA);
    const h5 = new Harvest(scratchDb, { unknownLog: scratchLog });
    h5.countType('assistant', true);
    flushTypesFast(scratchDb, h5.typeCounts, h5.typeKnown);
    const n1 = scratchDb.prepare("SELECT n FROM record_types WHERE type='assistant'").get().n;
    // What an incremental run does: add to what is there.
    flushTypesFast(scratchDb, h5.typeCounts, h5.typeKnown);
    const n2 = scratchDb.prepare("SELECT n FROM record_types WHERE type='assistant'").get().n;
    checks.push(['an incremental flush ADDS, which is what it is for', n1 === 1 && n2 === 2]);
    // What a full run must do: replace.
    scratchDb.exec('DELETE FROM record_types');
    flushTypesFast(scratchDb, h5.typeCounts, h5.typeKnown);
    const n3 = scratchDb.prepare("SELECT n FROM record_types WHERE type='assistant'").get().n;
    checks.push(['clearing first makes a full pass replace rather than double (gate can fail)',
      n3 === 1, String(n3)]);
    // AND THAT run() ACTUALLY CLEARS. The two checks above pass with the clear deleted from run()
    // entirely, which was verified by mutation: they prove the primitive, never the caller, and
    // run() cannot be called from here without a real store and real transcripts. The clear must
    // also sit immediately before the reflush, inside the open transaction, so the two are matched
    // as an ordered pair rather than each being found somewhere in the file.
    const clearAt = src.indexOf("if (full) db.exec('DELETE FROM record_types')");
    const flushAt = src.indexOf('flushTypesFast(db, h.typeCounts');
    checks.push(['the FULL path clears the census before reflushing it (gate can fail)',
      clearAt > 0 && flushAt > clearAt && flushAt - clearAt < 120,
      `clear at ${clearAt}, reflush at ${flushAt}`]);
    scratchDb.close();
  }

  // WHAT WAS PROPOSED, not just how big it was. A rejected call could report 2,655 bytes and an
  // error flag and not one word of its content, so the timeline read "plan written" then "the user
  // does not want to proceed" with nothing in between.
  {
    const scratchDb = new DatabaseSync(":memory:");
    scratchDb.exec(SCHEMA);
    const h6 = new Harvest(scratchDb, { unknownLog: scratchLog });
    const proposal = "# Delete results/t16-keep.txt" + "x".repeat(TOOL_INPUT_PREVIEW * 3);
    h6.scanBlocks({
      sessionId: "s1", uuid: "u1", timestamp: "2026-09-07T05:01:44Z",
      message: { content: [{ type: "tool_use", id: "toolu_1", name: "ExitPlanMode",
                             input: { plan: proposal } }] },
    }, "f", 1);
    const row = scratchDb.prepare("SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_1");
    checks.push(["the head of a tool input is stored, not only its hash (gate can fail)",
      typeof row?.input_preview === "string" && row.input_preview.includes("t16-keep.txt"),
      String(row?.input_preview).slice(0, 40)]);
    checks.push(["and it is CUT, so one call cannot carry a megabyte of input",
      row?.input_preview?.length === TOOL_INPUT_PREVIEW, String(row?.input_preview?.length)]);
    checks.push(["the byte count still measures the WHOLE input, not the preview (gate can fail)",
      row?.input_bytes > TOOL_INPUT_PREVIEW * 3, String(row?.input_bytes)]);
    // THE PLAN, WHOLE AND BESIDE THE CALL. The preview above is the same record cut to 500
    // characters, and JSON.stringify puts "plan" first, so the file path that names the proposal
    // is never inside it.
    const planned = scratchDb.prepare("SELECT * FROM plans WHERE tool_use_id = ?").get("toolu_1");
    checks.push(["the plan is stored whole, not cut to the preview (gate can fail)",
      planned?.plan_chars === proposal.length && planned?.plan_text?.length === proposal.length,
      String(planned?.plan_chars)]);
    checks.push(["the tool call row is still there beside it, not replaced by the plan",
      row?.tool_name === "ExitPlanMode"]);
    h6.scanBlocks({
      sessionId: "s1", uuid: "u1b", timestamp: "2026-09-07T05:03:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_1b", name: "ExitPlanMode",
        input: { plan: "a second proposal", planFilePath: "C:/p/one.md",
                 allowedPrompts: ["run the tests"] } }] },
    }, "f", 3);
    const second = scratchDb.prepare("SELECT * FROM plans WHERE tool_use_id = ?").get("toolu_1b");
    checks.push(["the path the plan was written to is kept (gate can fail)",
      second?.plan_file_path === "C:/p/one.md", String(second?.plan_file_path)]);
    checks.push(["and what the plan pre-approved with it",
      second?.allowed_prompts_json === JSON.stringify(["run the tests"])]);
    // TWO CALLS, ONE PATH, TWO ROWS. Measured on this store: one plan file already serves two
    // ExitPlanMode calls in one session, and they are different documents.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u1c", timestamp: "2026-09-07T05:04:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_1c", name: "ExitPlanMode",
        input: { plan: "a third proposal, same file", planFilePath: "C:/p/one.md" } }] },
    }, "f", 4);
    checks.push(["two plans written to one path stay two rows (gate can fail)",
      scratchDb.prepare("SELECT COUNT(*) n FROM plans WHERE plan_file_path = ?")
        .get("C:/p/one.md").n === 2]);
    // THE WORKFLOW LAUNCH, read off the result record rather than the block, which is where the
    // denial kind already comes from.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u1d", timestamp: "2026-09-07T05:05:00Z",
      toolUseResult: { status: "async_launched", taskId: "wtest0001", runId: "wf_test-001",
                       transcriptDir: "C:/t/dir", scriptPath: "C:/t/s.js" },
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_1", content: "ok" }] },
    }, "f", 5);
    const linked = scratchDb.prepare("SELECT * FROM workflow_runs WHERE run_id = ?").get("wf_test-001");
    checks.push(["a workflow launch names the call it came from, before any json exists (gate can fail)",
      linked?.tool_use_id === "toolu_1" && linked?.turn_uuid === "u1d"
        && linked?.session_id === "s1" && linked?.task_id === "wtest0001",
      JSON.stringify(linked && [linked.tool_use_id, linked.task_id])]);
    checks.push(["and the tool result it rode in on was still recorded, not shadowed by the link",
      scratchDb.prepare("SELECT result_bytes FROM tool_calls WHERE tool_use_id = ?")
        .get("toolu_1")?.result_bytes === 2,
      String(scratchDb.prepare("SELECT result_bytes FROM tool_calls WHERE tool_use_id = ?")
        .get("toolu_1")?.result_bytes)]);
    // A taskId is FOUR namespaces: a local agent, a workflow, a Monitor task and a TodoWrite item.
    // Gating on it rather than on runId would write a workflow row for a todo list.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u1e", timestamp: "2026-09-07T05:06:00Z",
      toolUseResult: { success: true, taskId: "6", updatedFields: ["status"] },
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_1", content: "ok" }] },
    }, "f", 6);
    checks.push(["a todo item's taskId is not a workflow run (gate can fail)",
      scratchDb.prepare("SELECT COUNT(*) n FROM workflow_runs").get().n === 1,
      String(scratchDb.prepare("SELECT COUNT(*) n FROM workflow_runs").get().n)]);
    // A short input is stored whole, which is 73% of the calls on this store.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u2", timestamp: "2026-09-07T05:01:45Z",
      message: { content: [{ type: "tool_use", id: "toolu_2", name: "Read",
                             input: { file_path: "C:/x/a.md" } }] },
    }, "f", 2);
    // THE AGENT'S OWN NOTE, kept whole even when the command is long. This is the line the UI
    // shows above a tool call, and it is not assistant text: it lives in the tool input, in
    // records the messages table drops. Leaving it to input_preview loses it 40% of the time,
    // because JSON.stringify puts the command first.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u9", timestamp: "2026-09-07T05:02:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_9", name: "Bash",
        input: { command: "grep -rn " + "x".repeat(TOOL_INPUT_PREVIEW * 2) + " .",
                 description: "Located chunk files" } }] },
    }, "f", 9);
    const noted = scratchDb.prepare("SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_9");
    checks.push(["the agent's note survives a command longer than the preview (gate can fail)",
      noted?.description === "Located chunk files", String(noted?.description)]);
    checks.push(["and the preview really did cut the command off, or the check above proves nothing",
      typeof noted?.input_preview === "string"
        && noted.input_preview.length === TOOL_INPUT_PREVIEW
        && !noted.input_preview.includes("Located chunk files"),
      String(noted?.input_preview?.length)]);
    // A call with no description stores NULL, so "has a note" stays a question the column answers.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u10", timestamp: "2026-09-07T05:02:01Z",
      message: { content: [{ type: "tool_use", id: "toolu_10", name: "Read",
                             input: { file_path: "C:/x/a.md", description: "   " } }] },
    }, "f", 10);
    const blank = scratchDb.prepare("SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_10");
    checks.push(["a blank description is stored as NULL, not as an empty string",
      blank?.description === null, JSON.stringify(blank?.description)]);
    const small = scratchDb.prepare("SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_2");
    checks.push(["a short input is kept whole",
      small?.input_preview === JSON.stringify({ file_path: "C:/x/a.md" }), String(small?.input_preview)]);
    // A RESULT ARRIVES ON A LATER LINE and must fill the flag in WITHOUT clearing what was
    // asked for. This check used to call the record below "a rejected call", and that sentence
    // is now false: the record carries no toolDenialKind, so the store cannot prove it was a
    // refusal and classifies it unclassified. The assertion is still worth making, so it keeps
    // the record and loses the word it had not earned. The refusal path is the check after it.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u3", timestamp: "2026-09-07T05:01:51Z", version: "2.1.121",
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_1", is_error: true,
                             content: "The user doesn't want to proceed with this tool use." }] },
    }, "f", 3);
    const after = scratchDb.prepare("SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_1");
    checks.push(["a flagged result still says what the call proposed (gate can fail)",
      after?.is_error === 1 && String(after?.input_preview).includes("t16-keep.txt"),
      `is_error=${after?.is_error} preview=${String(after?.input_preview).slice(0, 30)}`]);
    // A3. That build predates 2.1.202, so it recorded no reason and this store must not
    // invent one.
    checks.push(["a build that could not tell us produces neither error nor refused (gate can fail)",
      after?.outcome === TOOL_OUTCOME.UNCLASSIFIED && after?.denial_kind === null,
      `outcome=${after?.outcome} denial=${after?.denial_kind}`]);

    // A1 and A6. THE REFUSAL PATH, proved by the field Claude Code actually writes. The kind is
    // deliberately one this repo has never seen, so a whitelist of the six known values fails.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u11", timestamp: "2026-09-07T05:03:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_11", name: "Edit",
                             input: { file_path: "C:/x/a.md" } }] },
    }, "f", 11);
    h6.scanBlocks({
      sessionId: "s1", uuid: "u12", timestamp: "2026-09-07T05:03:01Z", version: "2.1.233",
      toolDenialKind: "some-future-kind",
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_11", is_error: true,
                             content: "nope" }] },
    }, "f", 12);
    const refused = scratchDb.prepare(
      "SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_11");
    checks.push(["a denial on the RECORD is read, not looked for on the block (gate can fail)",
      refused?.outcome === TOOL_OUTCOME.REFUSED, String(refused?.outcome)]);
    checks.push(["and Claude Code's own word is stored verbatim, even one we have never seen",
      refused?.denial_kind === "some-future-kind", String(refused?.denial_kind)]);

    // A2. A flagged result on a modern build, with no denial, is a genuine failure.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u13", timestamp: "2026-09-07T05:04:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_13", name: "Bash",
                             input: { command: "false" } }] },
    }, "f", 13);
    h6.scanBlocks({
      sessionId: "s1", uuid: "u14", timestamp: "2026-09-07T05:04:01Z", version: "2.1.233",
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_13", is_error: true,
                             content: "Exit code 1" }] },
    }, "f", 14);
    const failed = scratchDb.prepare(
      "SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_13");
    checks.push(["a flagged call on a modern build is a genuine failure",
      failed?.outcome === TOOL_OUTCOME.ERROR && failed?.denial_kind === null,
      `${failed?.outcome}/${failed?.denial_kind}`]);
    // A4. Success is STATED, not inferred from the absence of a flag. Note the row this uses:
    // toolu_2 has no result at all, so its outcome is NULL, and NULL is the right answer for a
    // call nothing ever came back from. That is a different fact from "it succeeded", which is
    // the whole reason ok is written rather than assumed.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u15", timestamp: "2026-09-07T05:05:01Z", version: "2.1.233",
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_2",
                             content: "the file, read" }] },
    }, "f", 15);
    const okRow = scratchDb.prepare(
      "SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_2");
    checks.push(["a call that was not flagged is positively marked ok",
      okRow?.outcome === TOOL_OUTCOME.OK, String(okRow?.outcome)]);
    checks.push(["and a call nothing ever came back from stays NULL, which is not ok (gate can fail)",
      scratchDb.prepare("SELECT outcome FROM tool_calls WHERE tool_use_id = ?")
        .get("toolu_13x")?.outcome === undefined
        && scratchDb.prepare(
          "SELECT COUNT(*) n FROM tool_calls WHERE result_bytes IS NULL AND outcome IS NOT NULL")
          .get().n === 0,
      "a row with no result must carry no outcome"]);

    // A5. THE COALESCE TRAP, and the only failure here a compiler cannot see. Re-reading the
    // tool_use line REPLACES the row; without the outcome columns in putToolCall's COALESCE
    // list the result already stored is silently nulled. No error, the data is just gone.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u11", timestamp: "2026-09-07T05:03:00Z",
      message: { content: [{ type: "tool_use", id: "toolu_11", name: "Edit",
                             input: { file_path: "C:/x/a.md" } }] },
    }, "f", 11);
    const survived = scratchDb.prepare(
      "SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_11");
    checks.push(["a re-read of the tool_use line does not wipe the outcome (gate can fail)",
      survived?.outcome === TOOL_OUTCOME.REFUSED
        && survived?.denial_kind === "some-future-kind",
      `outcome=${survived?.outcome} denial=${survived?.denial_kind}`]);
    // OUT OF ORDER, which is not hypothetical: 16 rows on the live store have their
    // tool_result on an EARLIER line than their tool_use. The ingest sees the result first,
    // setToolResult finds no row to update, and putToolCall then creates the row with
    // result_bytes and is_error NULL. An independent review found the backfill filling the
    // outcome on exactly those rows and leaving the hole, so a row said what it turned out to
    // be while claiming nothing had ever come back from it.
    h6.scanBlocks({
      sessionId: "s1", uuid: "u16", timestamp: "2026-09-07T05:06:00Z", version: "2.1.233",
      message: { content: [{ type: "tool_result", tool_use_id: "toolu_16", is_error: true,
                             content: "Exit code 7" }] },
    }, "f", 16);
    h6.scanBlocks({
      sessionId: "s1", uuid: "u17", timestamp: "2026-09-07T05:06:01Z",
      message: { content: [{ type: "tool_use", id: "toolu_16", name: "Bash",
                             input: { command: "false" } }] },
    }, "f", 17);
    const ooo = scratchDb.prepare(
      "SELECT * FROM tool_calls WHERE tool_use_id = ?").get("toolu_16");
    checks.push(["a result seen BEFORE its call leaves the row honest, not half-filled (gate can fail)",
      ooo?.outcome === null && ooo?.result_bytes === null,
      `outcome=${ooo?.outcome} bytes=${ooo?.result_bytes}`]);

    // A7. The invariant that makes `denial_kind IS NOT NULL` mean exactly "refused".
    const broken = scratchDb.prepare(
      `SELECT COUNT(*) n FROM tool_calls
        WHERE (outcome = 'refused') <> (denial_kind IS NOT NULL)`).get().n;
    checks.push(["refused and a denial kind are the same fact, never one without the other",
      broken === 0, String(broken)]);
    scratchDb.close();
  }

  // THE MEASURED COST. Every branch that can silently produce a wrong number is exercised, because
  // this is the one figure on the page that is money and the app's own rule is that a wrong price
  // is worse than no price.
  {
    const scratchDb = new DatabaseSync(':memory:');
    scratchDb.exec(SCHEMA);
    const costH = new Harvest(scratchDb, { unknownLog: scratchLog });
    const rec = (over = {}) => ({
      type: 'cost-state', sessionId: 's1', startTime: Date.UTC(2026, 8, 7),
      totalCostUSD: 0.2, totalAPIDuration: 13888, totalDuration: 20000,
      modelUsage: { 'claude-opus-5': { inputTokens: 12, outputTokens: 7213, costUSD: 0.2 } },
      ...over,
    });
    const row = () => scratchDb.prepare('SELECT * FROM cost_state WHERE session_id = ?').get('s1');
    const model = () => scratchDb.prepare(
      "SELECT * FROM cost_state_models WHERE session_id = 's1' AND model = 'claude-opus-5'").get();

    costH.putCostState(rec(), 'f', 1);
    checks.push(['a cost-state record is stored (gate can fail)', row()?.total_cost_usd === 0.2]);
    // startTime is epoch MILLISECONDS in the record, not a string. Reading it as a string stored
    // NULL for every row, indistinguishable from a record that carried no start time at all.
    checks.push(['a numeric startTime is stored, not dropped as NULL (gate can fail)',
      row()?.started_at === '2026-09-07T00:00:00.000Z', String(row()?.started_at)]);
    checks.push(['and its per-model breakdown with it', model()?.output_tokens === 7213]);
    checks.push(['hasUnknownModelCost absent reads as 0, not null',
      row()?.has_unknown_model_cost === 0]);

    // Cumulative: a later record supersedes, an earlier one must NOT walk the total backwards.
    costH.putCostState(rec({ totalCostUSD: 0.5, modelUsage: {
      'claude-opus-5': { inputTokens: 20, outputTokens: 9000, costUSD: 0.5 } } }), 'f', 2);
    checks.push(['a larger later total supersedes', row()?.total_cost_usd === 0.5]);
    costH.putCostState(rec({ totalCostUSD: 0.1 }), 'f', 3);
    checks.push(['a re-read of an OLDER record cannot lower it (gate can fail)',
      row()?.total_cost_usd === 0.5, String(row()?.total_cost_usd)]);
    checks.push(['and cannot lower the per-model figure either', model()?.cost_usd === 0.5]);

    // A cost of zero is a CLAIM. A record that carries no usable total is not stored at all.
    costH.putCostState({ type: 'cost-state', sessionId: 's2' }, 'f', 4);
    const s2 = scratchDb.prepare("SELECT COUNT(*) n FROM cost_state WHERE session_id = 's2'").get();
    checks.push(['a record with no total is skipped, never stored as zero (gate can fail)',
      s2.n === 0, String(s2.n)]);
    costH.putCostState({ type: 'cost-state', sessionId: 's3', totalCostUSD: 'free' }, 'f', 5);
    const s3 = scratchDb.prepare("SELECT COUNT(*) n FROM cost_state WHERE session_id = 's3'").get();
    checks.push(['and so is one whose total is not a number', s3.n === 0]);
    costH.putCostState(rec({ sessionId: 's4', hasUnknownModelCost: true }), 'f', 6);
    const s4 = scratchDb.prepare("SELECT has_unknown_model_cost h FROM cost_state WHERE session_id = 's4'").get();
    checks.push(['an incomplete total is flagged as such', s4.h === 1]);
    scratchDb.close();
  }

  // THE CENSUS SAYS WHICH TYPES IT UNDERSTOOD. Both directions, because a flag that is always 1
  // and a flag that is always 0 are equally useless, and the second is what a wrong default
  // produces.
  {
    const scratchDb = new DatabaseSync(':memory:');
    scratchDb.exec(SCHEMA);
    const h2 = new Harvest(scratchDb, { unknownLog: scratchLog });
    h2.countType('assistant', true);
    h2.countType('system/init', true);
    h2.countType('cost-state', false);
    h2.countType('UNPARSEABLE', 0);
    flushTypesFast(scratchDb, h2.typeCounts, h2.typeKnown);
    const row = (t) => scratchDb.prepare('SELECT known FROM record_types WHERE type = ?').get(t)?.known;
    checks.push(['a recognised type is marked known (gate can fail)', row('assistant') === 1]);
    checks.push(['a system subtype is known by its BASE type, not its composite key',
      row('system/init') === 1, String(row('system/init'))]);
    checks.push(['an unrecognised type is marked unknown (gate can fail)', row('cost-state') === 0]);
    checks.push(['an unparseable line is unknown too', row('UNPARSEABLE') === 0]);

    // Recognising a type later must reclassify what was already counted under it, or the store
    // keeps calling it unknown for ever after the fix that recognised it.
    const h3 = new Harvest(scratchDb, { unknownLog: scratchLog });
    h3.countType('cost-state', true);
    flushTypesFast(scratchDb, h3.typeCounts, h3.typeKnown);
    const after = scratchDb.prepare("SELECT n, known FROM record_types WHERE type = 'cost-state'").get();
    checks.push(['recognising a type later reclassifies it (gate can fail)', after.known === 1]);
    checks.push(['and its existing count is kept, not reset', after.n === 2, String(after.n)]);
    scratchDb.close();
  }

  let bad = 0;
  // A failing check prints its detail, when it carries one: the third element many checks
  // already build was never shown, so a FAIL named the rule and hid the value that broke it.
  for (const [name, ok, detail] of checks) {
    if (!ok) bad++;
    const shown = !ok && detail !== undefined ? `  [${String(detail).slice(0, 200)}]` : '';
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${shown}`);
  }
  rmSync(tmp, { recursive: true, force: true });
  delete process.env.C4X_UNKNOWN_LOG;
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

// EVERY FLAG THIS TOOL KNOWS. Anything else that looks like a flag is refused with the usage,
// because the fallthrough below is a harvest of the live store: `--help`, typed by a careful
// reader, ran one.
const USAGE = `harvest.mjs
  node harvest.mjs                 harvest incrementally
  node harvest.mjs --dry-run       say what an incremental run would read; write nothing
  node harvest.mjs --full --yes    ignore stored offsets, re-read everything
  node harvest.mjs --self-test     prove the parser detects what it claims to detect
  node harvest.mjs --stats         print store contents, harvest nothing
  node harvest.mjs --backfill-chains [--dry-run] [--records <dir>]
  node harvest.mjs --backfill-reviews [--dry-run]
                                          tie each one-shot session that quotes another (a hook's
                                          headless reviewer) to the session it read
  node harvest.mjs --backfill-sidecars    read the agent and workflow files beside transcripts
  node harvest.mjs --backfill-work        re-read transcripts for plans and task notifications
  node harvest.mjs --backfill-changes     re-read transcripts for the file changes Claude made
  node harvest.mjs --backfill-survivors | --backfill-titles | --backfill-agents
                   | --backfill-tool-outcomes | --backfill-message-source
  any of the above with --db <path> to name the store`;
const KNOWN_FLAGS = new Set(['--full', '--yes', '--dry-run', '--self-test', '--stats', '--db', '--records',
  '--backfill-chains', '--backfill-reviews', '--backfill-sidecars', '--backfill-work', '--backfill-changes', '--backfill-survivors', '--backfill-titles', '--backfill-agents',
  '--backfill-tool-outcomes', '--backfill-message-source', '--help', '-h']);

const argv = process.argv.slice(2);
let code = 0;
const unknownFlags = argv.filter((a) => a.startsWith('-') && !KNOWN_FLAGS.has(a));
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--help') || argv.includes('-h')) console.log(USAGE);
else if (unknownFlags.length) { console.error(`unknown flag ${unknownFlags.join(' ')}\n${USAGE}`); code = 2; }
else if (argv.includes('--self-test')) code = await selfTest();
else if (argv.includes('--stats')) code = stats();
else if (argv.includes('--backfill-survivors')) code = backfillSurvivors(DB_PATH) ? 0 : 1;
else if (argv.includes('--backfill-titles')) code = await backfillTitles();
else if (argv.includes('--backfill-changes')) {
  const r = await backfillChanges(resolveDbPath(argv), { write: !argv.includes('--dry-run') });
  code = r && r.rows_unchanged ? 0 : 1;
}
else if (argv.includes('--backfill-work')) {
  const r = await backfillWork(resolveDbPath(argv), { write: !argv.includes('--dry-run') });
  code = r && r.rows_unchanged ? 0 : 1;
}
else if (argv.includes('--backfill-sidecars')) {
  const r = await backfillSidecars(resolveDbPath(argv), { write: !argv.includes('--dry-run') });
  code = r && r.rows_unchanged ? 0 : 1;
}
else if (argv.includes('--backfill-agents')) code = await backfillAgents(resolveDbPath(argv));
else if (argv.includes('--backfill-tool-outcomes'))
  code = await backfillToolOutcomes(resolveDbPath(argv));
else if (argv.includes('--backfill-message-source'))
  code = await backfillMessageSource(resolveDbPath(argv));
// BEFORE --dry-run, so that --backfill-reviews --dry-run reaches it and reports without writing.
else if (argv.includes('--backfill-reviews')) {
  const r = await backfillReviews(resolveDbPath(argv), { write: !argv.includes('--dry-run') });
  code = r ? 0 : 1;
}
// BEFORE --dry-run, so that --backfill-chains --dry-run reaches it and reports without writing.
else if (argv.includes('--backfill-chains')) {
  const r = await backfillChains(resolveDbPath(argv), {
    write: !argv.includes('--dry-run'), recordsRoots: resolveRecordsRoots(argv),
  });
  code = r && r.rows_unchanged ? 0 : 1;
}
else if (argv.includes('--dry-run')) code = dryRun(resolveDbPath(argv));
else {
  const refusal = fullGate(argv);
  if (refusal) { console.error(refusal); code = 2; }
  else code = await run({ full: argv.includes('--full') });
}
if (IS_ENTRY) process.exit(code);

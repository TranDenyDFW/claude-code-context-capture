#!/usr/bin/env node
// Build a small synthetic store, so the store-dependent checks can actually RUN in CI.
//
// data/ is gitignored, and rightly so: it holds verbatim conversations. But three of this repo's
// checks need a store, and two node self-tests each carry one store-dependent check that correctly
// FAILS rather than skips when there is none. Without a fixture, CI either runs a subset and calls
// it a suite, or is permanently red for a reason that is not a defect.
//
// It writes through harvest.mjs's own openDb, so the fixture has the SAME schema, pragmas and
// migrations as a real store. A fixture carrying its own copy of the schema drifts from the thing
// under test, and then CI is green against a database the app would never open.
//
// The content is deliberately synthetic and says so: session ids are literal, message text names
// itself as fixture text. Nothing here came from a real conversation.
//
// Usage: node tools/make_fixture.mjs [--out <path>] [--force]

import { existsSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { ensureBaselineSchema } from './breakdown.mjs';
import { openDb } from './harvest.mjs';
import { ensureProbeSchema } from './probe.mjs';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

function arg(flag, fallback) {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const SELF_TEST = process.argv.includes('--self-test');
// The self-test writes somewhere disposable and never near the real store, so running the suite can
// never cost anybody their capture.
const OUT = SELF_TEST
  ? join(ROOT, 'tmp', `fixture-self-test-${process.pid}`, 'context.db')
  : arg('--out', join(ROOT, 'data', 'context.db'));
const FORCE = process.argv.includes('--force') || SELF_TEST;

// A real store is 900 MB of somebody's conversations. Refusing to overwrite one is not politeness,
// it is the difference between a fixture run and a data loss.
if (existsSync(OUT) && !FORCE) {
  console.error(`refusing to overwrite an existing store at ${OUT}`);
  console.error('pass --force if you are certain, or --out <path> to write elsewhere');
  process.exit(2);
}
if (existsSync(OUT) && FORCE) {
  for (const suffix of ['', '-wal', '-shm']) {
    try { rmSync(OUT + suffix); } catch { /* absent is fine */ }
  }
}
mkdirSync(dirname(OUT), { recursive: true });

const db = openDb(OUT);
ensureBaselineSchema(db);   // context_baselines is breakdown.mjs's table, not harvest's
ensureProbeSchema(db);      // probes and its two child tables belong to probe.mjs

// One window, so the mirror's fit has something consistent to validate against. 1M is the window
// this build produces for the current models, and 967000 its compaction threshold.
const MODEL = 'claude-opus-5';
const SMALL_MODEL = 'claude-haiku-4-5-20251001';
const SECOND_MODEL = 'claude-sonnet-5';
const SMALL_COMPACT_AT = 167_000;   // 200000 - 20000 - 13000, the same formula the mirror fits
const VERSION = '2.1.233';
const WINDOW = 1_000_000;
const COMPACT_AT = 967_000;

// `overshoots` are tokens past the compaction threshold, chosen to SPAN the mutation the self-tests
// apply (12000). Some sit inside it, so shifting the threshold pushes them negative and the mutant
// fit is measurably worse; some sit outside, so the real fit stays clean. A fixture where every
// compaction has the same overshoot cannot tell a good formula from a bad one.
const SESSIONS = [
  { id: 'fixture-session-0001', turns: 60, overshoots: [120, 3_000] },
  { id: 'fixture-session-0002', turns: 24, overshoots: [] },
  { id: 'fixture-session-0003', turns: 48, overshoots: [900, 14_500] },
  // Switches model after its compaction, so the trailing segment carries no compaction and its
  // window comes from the model default, which is the only kind the ceiling mutation can move.
  { id: 'fixture-session-0004', turns: 36, overshoots: [6_400], switchAfter: 1 },
  // A 200k-window session, compacting just past the SMALLEST threshold. Nothing smaller exists for
  // the fit to reassign it to, so a wrong buffer turns its overshoot negative and the mutation
  // check can finally tell a good formula from a bad one.
  { id: 'fixture-session-0005', turns: 20, overshoots: [500], small: true },
];

const iso = (minute) => new Date(Date.UTC(2026, 0, 1, 0, 0, 0) + minute * 60_000).toISOString();

let clock = 0;
let requestSeq = 0;

const insertSession = db.prepare(`
  INSERT OR REPLACE INTO sessions (session_id, cwd, project_slug, git_branch, version, entrypoint,
                                   first_ts, last_ts, transcript_path)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`);
const insertTurn = db.prepare(`
  INSERT INTO turns (uuid, session_id, ts, model, request_id, input_tokens,
                     cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
                     thinking_tokens, total_resident, is_sidechain, file_path, line_no)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
const insertMessage = db.prepare(`
  INSERT INTO messages (uuid, session_id, ts, role, type, text, chars, model, request_id,
                        is_sidechain, file_path, line_no)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)`);
const insertCompaction = db.prepare(`
  INSERT INTO compactions (uuid, session_id, ts, trigger, version, entrypoint, pre_tokens,
                           post_tokens, duration_ms, cumulative_dropped_tokens, messages_summarized,
                           summary_uuid, summary_chars, file_path, line_no)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`);
const insertSurvivor = db.prepare(
  "INSERT INTO compaction_survivors (compaction_uuid, kind, uuid) VALUES (?, 'message', ?)");
const insertTool = db.prepare(`
  INSERT INTO tool_calls (tool_use_id, session_id, turn_uuid, ts, tool_name, server_name, target,
                          input_sha1, input_bytes, result_bytes, is_error, is_sidechain,
                          file_path, line_no)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)`);

for (const session of SESSIONS) {
  insertSession.run(session.id, 'C--fixture-project', 'C:\\fixture\\project', 'main', VERSION,
                    'claude-desktop', iso(clock + 1), iso(clock + session.turns),
                    'fixture://transcript.jsonl');

  // Resident climbs toward the threshold, a compaction drops it, and it climbs again. That shape is
  // what the mirror validates its fit against and what the Session tab's diff reads as a fall, so a
  // flat ramp would leave both untested.
  const overshoots = session.overshoots;
  const baseModel = session.small ? SMALL_MODEL : MODEL;
  const ceiling = session.small ? SMALL_COMPACT_AT : COMPACT_AT;
  const startAt = session.small ? 20_000 : 40_000;
  const perCompaction = Math.floor(session.turns / (overshoots.length + 1));
  let resident = startAt;
  let compactionsDone = 0;
  const uuidsSinceCompaction = [];

  for (let turn = 0; turn < session.turns; turn++) {
    clock += 1;
    const ts = iso(clock);
    const uuid = `${session.id}-turn-${String(turn).padStart(4, '0')}`;
    const requestId = `req_fixture_${String(++requestSeq).padStart(5, '0')}`;

    const target = ceiling + (overshoots[compactionsDone] ?? 0);   // keeps climbing past the
    // last compaction, so a trailing model-resolved segment still reaches a peak worth auditing
    const headroom = target - resident;
    const step = Math.max(4_000, Math.floor(headroom / Math.max(2, perCompaction - (turn % perCompaction))));
    resident = Math.min(resident + step, target);

    const cacheRead = Math.max(0, resident - 20_000);
    const output = 900 + (turn % 7) * 130;

    // After the switch point the session runs on a different model, which starts a new segment.
    const model = (session.switchAfter !== undefined && compactionsDone >= session.switchAfter)
      ? SECOND_MODEL : baseModel;
    // A STREAMED message writes several turn rows under one request id. The input and cache
    // columns repeat on each of them while output_tokens accumulates, which is exactly why
    // api_calls takes the max per request id rather than summing. Every fourth turn streams, so the
    // store contains both shapes and a check that confuses them has something to trip on.
    const chunks = turn % 4 === 0 ? 3 : 1;
    for (let chunk = 0; chunk < chunks; chunk++) {
      insertTurn.run(chunk ? `${uuid}-c${chunk}` : uuid, session.id, ts, model, requestId,
                     1_200, 18_000, cacheRead,
                     Math.round(output * (chunk + 1) / chunks),      // accumulates as it streams
                     turn % 3 === 0 ? 400 : 0, resident, 0,
                     'fixture://transcript.jsonl', turn + 1);
    }

    // SUBAGENT work, on the same session. Roughly 70% of the API calls in a real store are
    // sidechain, the Waste tab overrides the scope radio because of it, and the All sessions
    // table counts it without saying so. A fixture with none of it cannot exercise any of that,
    // and two tests refused to pass rather than report a check that could not run.
    if (turn % 2 === 0) {
      for (let agent = 0; agent < 2; agent++) {
        insertTurn.run(`${uuid}-sub${agent}`, session.id, ts, model, `${requestId}-sub${agent}`,
                       900, 4_000, Math.round(cacheRead / 4), 300, 0,
                       Math.round(resident / 3), 1,
                       'fixture://subagent.jsonl', turn + 1);
      }
    }

    const text = `Fixture message ${turn} for ${session.id}. Synthetic text, not a real conversation.`;
    insertMessage.run(`${uuid}-msg`, session.id, ts, turn % 2 === 0 ? 'user' : 'assistant',
                      'message', text, text.length, model, requestId,
                      'fixture://transcript.jsonl', turn + 1);
    uuidsSinceCompaction.push(`${uuid}-msg`);

    if (turn % 5 === 2) {
      insertTool.run(`${uuid}-tool`, session.id, uuid, ts, turn % 10 === 2 ? 'Read' : 'Bash',
                     null, `C:\\fixture\\file_${turn % 4}.txt`, `sha1-${turn}`,
                     220, 4_000 + turn * 37, 'fixture://transcript.jsonl', turn + 1);
    }

    // An MCP call, so the Sources tab's server table has rows. Its branch was unreached with every
    // server NULL, and the audit reported the four evidence_block calls behind it as untaken.
    if (turn % 7 === 3) {
      insertTool.run(`${uuid}-mcp`, session.id, uuid, ts, 'mcp__fixture__lookup', 'fixture-server',
                     `fixture://resource/${turn % 3}`, `sha1-mcp-${turn}`,
                     310, 2_500 + turn * 11, 'fixture://transcript.jsonl', turn + 1);
    }

    // The SAME file read repeatedly in one session, which is the waste the re-read table exists to
    // show. Three or more reads is the threshold that tab uses, so two would leave it empty.
    if (turn % 4 === 1) {
      insertTool.run(`${uuid}-reread`, session.id, uuid, ts, 'Read', null,
                     'C:\\fixture\\read_over_and_over.txt', 'sha1-constant',
                     180, 9_600, 'fixture://transcript.jsonl', turn + 1);
    }

    // Compact when the climb reaches the threshold, which is what a real one does.
    if (compactionsDone < overshoots.length && resident >= target) {
      compactionsDone++;
      const cUuid = `${session.id}-compaction-${compactionsDone}`;
      const summaryUuid = `${cUuid}-summary`;
      const summary = 'Fixture compaction summary. Synthetic text standing in for the prose a real '
                    + 'compaction writes, so the Compactions tab has something to render.';
      insertMessage.run(summaryUuid, session.id, ts, 'assistant', 'compact_summary', summary,
                        summary.length, model, requestId, 'fixture://transcript.jsonl', turn + 1);
      insertCompaction.run(cUuid, session.id, ts, 'auto', VERSION, 'claude-desktop',
                           resident, 58_000, 4_200, 120_000 * compactionsDone,
                           uuidsSinceCompaction.length, summaryUuid, summary.length,
                           'fixture://transcript.jsonl', turn + 1);
      // A few records survive, which is what makes the survivor join non-empty.
      for (const survivor of uuidsSinceCompaction.slice(-4)) {
        insertSurvivor.run(cUuid, survivor);
      }
      uuidsSinceCompaction.length = 0;
      resident = session.small ? 22_000 : 58_000;
    }
  }
}

// One baseline, so the derived category breakdown has a calibration to subtract. Values are the
// shape of a real observation, not a copy of one.
db.prepare(`
  INSERT INTO context_baselines (ts, source, entrypoint, system_prompt, system_tools, mcp_tools,
                                 skills, memory_files, custom_agents, static_total, window_size, note)
  VALUES (?, 'fixture', 'claude-desktop', 3100, 12800, 9400, 9900, 11700, 2100, 49000, ?, ?)`)
  .run(iso(1), WINDOW, 'synthetic baseline for CI; not measured from a real configuration');

// The Sources tab returns EARLY when attachments, hook_events and record_types are all empty, so
// without these three its four evidence_block calls are unreachable and the audit reports them as
// untaken paths. Harness-injected context, lifecycle events and the record census are ordinary
// contents of a real store.
const insertAttachment = db.prepare(
  'INSERT INTO attachments (session_id, type, n) VALUES (?, ?, ?)');
const insertHookEvent = db.prepare(`
  INSERT INTO hook_events (captured_at, probe, event, known, session_id, cwd, permission_mode,
                           tool_name, tool_input_bytes, tool_response_bytes, prompt_chars,
                           source, reason, agent_id, agent_type, truncated, transcript_path, extra)
  VALUES (?, 0, ?, 1, ?, ?, 'default', ?, ?, ?, ?, 'fixture', NULL, NULL, NULL, 0, ?, NULL)`);
const insertRecordType = db.prepare('INSERT INTO record_types (type, n) VALUES (?, ?)');

for (const [i, session] of SESSIONS.entries()) {
  for (const [kind, n] of [['system_reminder', 12 + i], ['skill_listing', 3], ['hook_output', 5 + i]]) {
    insertAttachment.run(session.id, kind, n);
  }
  for (const [event, tool, resp] of [['PreToolUse', 'Read', 0], ['PostToolUse', 'Read', 8_400],
                                     ['PostToolUse', 'Bash', 2_100], ['UserPromptSubmit', null, 0],
                                     ['SessionEnd', null, 0]]) {
    insertHookEvent.run(iso(i * 10 + 1), event, session.id, 'C:\\fixture\\project',
                        tool, tool ? 220 : 0, resp, tool ? 0 : 640,
                        'fixture://transcript.jsonl');
  }
}
for (const [type, n] of [['assistant', 168], ['user', 84], ['system', 21], ['summary', 6]]) {
  insertRecordType.run(type, n);
}

// Probes. probes_layout reads all three of these, and with them empty the tab raises and renders
// an exception panel instead of content, which the table audit reports as a failed tab.
const insertProbe = db.prepare(`
  INSERT INTO probes (id, ts, ok, error, model, max_tokens, total_tokens, percentage,
                      autocompact_source, auto_compact_threshold, is_auto_compact_enabled, raw_json)
  VALUES (?, ?, 1, NULL, ?, ?, ?, ?, 'fixture', ?, 1, '{"note":"synthetic fixture probe"}')`);
const insertCategory = db.prepare(
  'INSERT INTO probe_categories (probe_id, name, tokens, color, is_deferred) VALUES (?, ?, ?, ?, ?)');
const insertDetail = db.prepare(
  `INSERT INTO probe_details (probe_id, kind, name, extra, tokens, loaded)
   VALUES (?, ?, ?, ?, ?, ?)`);
const insertMessageRow = db.prepare(
  'INSERT INTO probe_message_breakdown (probe_id, name, tokens) VALUES (?, ?, ?)');

// The kinds a real probe actually emits. The fixture used to write 'tool', which the app queries
// for nowhere, so every detail table except skills was empty in CI and the audit had nothing to
// walk. Each row here is shaped like its real counterpart, including an MCP tool that is present
// but NOT loaded, which is the case whose 0 tokens means "deferred" rather than "free".
const DETAILS = [
  ['skill', 'fixture-skill-alpha', 'userSettings', 523, null],
  ['skill', 'fixture-skill-beta', 'built-in', 382, null],
  ['skill', 'fixture-skill-gamma', 'plugin', 194, null],
  ['mcpTool', 'mcp__fixture__search', 'fixture-server', 640, 1],
  ['mcpTool', 'mcp__fixture__fetch', 'fixture-server', 0, 0],
  ['mcpTool', 'mcp__other__list', 'other-server', 0, 0],
  ['agent', 'fixture-agent', 'plugin', 120, null],
  ['memoryFile', 'C:\\fixture\\CLAUDE.md', 'User', 11_700, null],
  ['attachment', 'hook_success', null, 4_000, null],
  ['attachment', 'hook_additional_context', null, 1_018, null],
  ['toolCallType', 'Read', null, 900, null],
];

// The message half of the window. Real probes carry these seven; a fixture that omitted them left
// the "what the messages are made of" table unbuildable.
const MESSAGE_BREAKDOWN = [
  ['attachmentTokens', 5_018],
  ['toolCallTokens', 900],
  ['toolResultTokens', 0],
  ['assistantMessageTokens', 0],
  ['userMessageTokens', 0],
  ['redirectedContextTokens', 0],
  ['unattributedTokens', 7],
];

// Free space is computed rather than written, so the categories always sum to the window even if
// a resident category is added or changed here. A fixture whose split does not add up cannot catch
// a missing category, which is the one thing that test is for.
const RESIDENT_CATEGORIES = [
  ['System prompt', 3_100, '#4493f8', 0],
  ['System tools', 12_800, '#3fb950', 0],
  ['MCP tools', 9_400, '#a371f7', 0],
  ['Skills', 9_900, '#d29922', 0],
  ['Memory files', 11_700, '#f85149', 0],
  ['Deferred tools', 104_900, '#8b949e', 1],
];
const RESIDENT_TOTAL = RESIDENT_CATEGORIES
  .filter((c) => !c[3]).reduce((sum, c) => sum + c[1], 0);
const CATEGORIES = [...RESIDENT_CATEGORIES,
  ['Free space', WINDOW - RESIDENT_TOTAL, '#8b949e', 0]];
for (const probeId of [1, 2]) {
  const resident = RESIDENT_TOTAL;
  insertProbe.run(probeId, iso(probeId), MODEL, WINDOW, resident + probeId * 500,
                  Number(((resident / WINDOW) * 100).toFixed(2)), COMPACT_AT);
  for (const [name, tokens, color, deferred] of CATEGORIES) {
    insertCategory.run(probeId, name, tokens, color, deferred);
  }
  for (const [kind, name, extra, tokens, loaded] of DETAILS) {
    insertDetail.run(probeId, kind, name, extra, tokens, loaded);
  }
  for (const [name, tokens] of MESSAGE_BREAKDOWN) insertMessageRow.run(probeId, name, tokens);
}

const counts = {};
for (const table of ['sessions', 'turns', 'messages', 'compactions', 'compaction_survivors',
                     'tool_calls', 'context_baselines', 'probes', 'probe_categories',
                     'probe_details', 'attachments', 'hook_events', 'record_types']) {
  counts[table] = db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n;
}
const apiCalls = db.prepare('SELECT COUNT(*) AS n FROM api_calls').get().n;
const smallWindowCompactions = db.prepare(
  'SELECT COUNT(*) AS n FROM compactions WHERE pre_tokens < 500000').get().n;
const distinctModels = db.prepare(
  'SELECT COUNT(DISTINCT model) AS n FROM turns WHERE model IS NOT NULL').get().n;
const mcpCalls = db.prepare(
  'SELECT COUNT(*) AS n FROM tool_calls WHERE server_name IS NOT NULL').get().n;
const distinctRequestIds = db.prepare(
  'SELECT COUNT(DISTINCT request_id) AS n FROM turns WHERE request_id IS NOT NULL').get().n;
const maxReReads = db.prepare(`
  SELECT COALESCE(MAX(n), 0) AS n FROM (
    SELECT COUNT(*) AS n FROM tool_calls WHERE target IS NOT NULL
     GROUP BY session_id, target)`).get().n;
// Read before the handle closes: the checks below run after it, and a query there throws
// "database is not open" rather than failing the check it was meant to make.
const sidechainTurns = db.prepare(
  'SELECT COUNT(*) n FROM turns WHERE is_sidechain = 1').get().n;
const mainTurns = db.prepare(
  'SELECT COUNT(*) n FROM turns WHERE COALESCE(is_sidechain,0) = 0').get().n;
const categorySum = db.prepare(`
  SELECT COALESCE(SUM(tokens),0) n FROM probe_categories
   WHERE probe_id = 1 AND COALESCE(is_deferred,0) = 0`).get().n;
const probeKinds = new Set(
  db.prepare('SELECT DISTINCT kind FROM probe_details').all().map((r) => r.kind)).size;
const deferredMcp = db.prepare(
  "SELECT COUNT(*) n FROM probe_details WHERE kind='mcpTool' AND loaded=0 AND tokens=0").get().n;
const loadedMcp = db.prepare(
  "SELECT COUNT(*) n FROM probe_details WHERE kind='mcpTool' AND loaded=1 AND tokens>0").get().n;
const messageRowCount = db.prepare(
  'SELECT COUNT(*) n FROM probe_message_breakdown').get().n;

db.close();

if (!SELF_TEST) {
  console.log(`fixture written to ${OUT}`);
  for (const [table, n] of Object.entries(counts)) console.log(`  ${table.padEnd(22)} ${n}`);
  console.log(`  ${'api_calls (view)'.padEnd(22)} ${apiCalls}`);
} else {
  // Assert the SHAPE the checks depend on, not just that rows exist. Each of these is here because
  // a check failed without it, and the comment says which, so a future edit that guts one of them
  // fails here rather than three files away.
  const checks = [
    ['sessions present', counts.sessions >= 4],
    ['compactions spread over more than one window', counts.compactions >= 5],
    ['a compaction near the SMALLEST threshold, so a wrong buffer cannot be reassigned away',
     smallWindowCompactions >= 1],
    ['more than one model, so a segment resolves from the model rather than a compaction',
     distinctModels >= 2],
    ['survivors recorded, so the survivor join is not empty', counts.compaction_survivors > 0],
    ['messages carry text, so the compaction summary renders as prose', counts.messages > 0],
    ['an MCP call, for the Sources server table', mcpCalls > 0],
    ['a file read 3+ times in one session, for the re-read table', maxReReads >= 3],
    ['a baseline, so the derived breakdown has something to subtract', counts.context_baselines > 0],
    ['probe rows, or the Probes tab raises', counts.probes > 0 && counts.probe_details > 0],
    // The Breakdown tab builds one table per detail kind. A fixture missing a kind does not fail
    // loudly; it just renders one table fewer, and the audit walks one table fewer, silently.
    ['every probe detail kind a real probe emits is present',
     probeKinds === new Set(DETAILS.map((d) => d[0])).size],
    ['an MCP tool that is present but not loaded, whose 0 tokens means deferred',
     deferredMcp > 0],
    ['and one that is loaded and costs something, so the two cannot be confused',
     loadedMcp > 0],
    ['a message breakdown, which is the only record of what the conversation half holds',
     messageRowCount > 0],
    // Subagent rows. Without them the fixture cannot exercise the scope radio, the Waste tab's
    // deliberate override of it, or the All sessions table counting subagent work silently.
    ['subagent turns exist, so scope-dependent behaviour can be exercised', sidechainTurns > 0],
    ['and main-thread turns exist too, so the two can be told apart', mainTurns > 0],
    ['a probe accounts for the whole window, free space included',
     categorySum === WINDOW],
    ['attachments, hook events and a record census, or the Sources tab returns before its tables',
     counts.attachments > 0 && counts.hook_events > 0 && counts.record_types > 0],
    // The invariant the whole api_calls view exists for. This used to assert the OPPOSITE, that
    // the fixture held one row per request, which guaranteed the dedup was never exercised.
    ['streamed messages present, so api_calls dedupes rows away', apiCalls < counts.turns],
    ['and the dedup is per request id, not a blanket drop',
     apiCalls === distinctRequestIds],
  ];
  let failed = 0;
  for (const [what, ok] of checks) {
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`);
    if (!ok) failed++;
  }
  rmSync(dirname(OUT), { recursive: true, force: true });
  console.log(`SELF-TEST ${failed ? 'FAIL' : 'PASS'} (${checks.length} checks)`);
  process.exit(failed ? 1 : 0);
}

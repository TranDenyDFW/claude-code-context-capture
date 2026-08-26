#!/usr/bin/env node
// event-hook.mjs - process-level lifecycle capture.
//
// Why this exists: c4x's live channel was the statusLine, and the status line does not run on
// the desktop entrypoint. That was proven with a paired control, arm A on a ConPTY wrote 1
// genuine sample and arm B on the desktop host wrote 0. Hooks are process level rather than part
// of the Ink render tree, so they fire on EVERY entrypoint, which makes them the channel that
// works where the status line is dead.
//
// Handles SessionStart, SessionEnd, UserPromptSubmit, PostToolUse and SubagentStop. The event
// name arrives in the payload as `hook_event_name`, so one handler serves all five and an
// unrecognised event is RECORDED rather than dropped, because a new event type is information.
//
// Contract, copied deliberately from compact-hook.mjs:
//   - NEVER blocks. Always exits 0 with no decision, even on internal failure. A capture tool
//     that can interrupt the sessions it measures corrupts its own dataset.
//   - Declared budget. A payload larger than MAX_PAYLOAD_BYTES is truncated and the truncation is
//     RECORDED, so a short row always has a reason attached rather than looking like a small turn.
//   - Probe separation. Rows written through C4X_EVENTS_OUT are stamped probe:true, the same
//     convention statusline.mjs carries because 164 test rows were once read as live capture.
//
//   node event-hook.mjs              read hook JSON on stdin
//   node event-hook.mjs --self-test

import { appendFileSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

const ROOT = join(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const DEFAULT_OUT = join(ROOT, 'data', 'raw', 'events.ndjson');
const OUT_OVERRIDE = process.env.C4X_EVENTS_OUT || null;
const OUT = OUT_OVERRIDE || DEFAULT_OUT;
const IS_PROBE = Boolean(OUT_OVERRIDE);

// One event is a lifecycle marker, not a transcript. Anything past this is the payload carrying
// content we already have in the transcript, so it is cut rather than duplicated into this file.
const MAX_PAYLOAD_BYTES = 16 * 1024;

const KNOWN_EVENTS = new Set([
  'SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PostToolUse', 'SubagentStop',
]);

/**
 * Reduce a raw hook payload to the fields worth keeping, plus a size-bounded remainder.
 *
 * Deliberately does NOT keep prompt text or tool output. Both are already in the transcript that
 * harvest.mjs reads, and copying them here would double the on-disk footprint of every session
 * while adding a second place for the same content to leak from.
 */
export function summarise(payload, { maxBytes = MAX_PAYLOAD_BYTES } = {}) {
  const p = payload && typeof payload === 'object' ? payload : {};
  const event = typeof p.hook_event_name === 'string' ? p.hook_event_name : 'UNKNOWN';
  const out = {
    event,
    known: KNOWN_EVENTS.has(event),
    session_id: p.session_id ?? null,
    transcript_path: p.transcript_path ?? null,
    cwd: p.cwd ?? null,
    permission_mode: p.permission_mode ?? null,
    tool_name: p.tool_name ?? null,
    // Sizes, not contents. A number answers "did this turn get expensive" without storing the text.
    tool_input_bytes: p.tool_input === undefined ? null : Buffer.byteLength(JSON.stringify(p.tool_input ?? null), 'utf8'),
    tool_response_bytes: p.tool_response === undefined ? null : Buffer.byteLength(JSON.stringify(p.tool_response ?? null), 'utf8'),
    prompt_chars: typeof p.prompt === 'string' ? p.prompt.length : null,
    source: p.source ?? null,
    reason: p.reason ?? null,
    agent_id: p.agent_id ?? null,
    agent_type: p.agent_type ?? null,
    truncated: false,
  };
  // Anything not named above is kept once, bounded, so a field added by a future build is not
  // silently lost. If it does not fit, the row says so.
  const named = new Set(Object.keys(out).concat(['prompt', 'tool_input', 'tool_response', 'hook_event_name']));
  const rest = {};
  for (const [k, v] of Object.entries(p)) if (!named.has(k)) rest[k] = v;
  let extra = JSON.stringify(rest);
  if (Buffer.byteLength(extra, 'utf8') > maxBytes) {
    extra = JSON.stringify({ note: 'extra fields exceeded the budget', keys: Object.keys(rest) });
    out.truncated = true;
  }
  out.extra = extra === '{}' ? null : extra;
  return out;
}

export function record(payload, outPath = OUT, isProbe = (outPath === DEFAULT_OUT ? IS_PROBE : true)) {
  mkdirSync(dirname(outPath), { recursive: true });
  appendFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(), probe: isProbe, ...summarise(payload),
  }) + '\n');
}

function readStdin() {
  try { return readFileSync(0, 'utf8'); } catch { return ''; }
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  const s = summarise({
    hook_event_name: 'PostToolUse', session_id: 'sess-1', cwd: 'P:/x',
    tool_name: 'Read', tool_input: { file_path: 'a.md' }, tool_response: 'hello',
    brand_new_field: 42,
  });
  add('event name captured', s.event === 'PostToolUse');
  add('a known event is marked known', s.known === true);
  add('tool name captured', s.tool_name === 'Read');
  // {"file_path":"a.md"} is 20 bytes.
  add('tool input measured in bytes, not stored', s.tool_input_bytes === 20 && !('tool_input' in s),
    String(s.tool_input_bytes));
  add('tool response measured in bytes', s.tool_response_bytes === 7, String(s.tool_response_bytes));
  add('an unnamed future field is kept in extra', String(s.extra).includes('brand_new_field'));

  const u = summarise({ hook_event_name: 'SomethingNew2027', session_id: 's' });
  add('an unrecognised event is RECORDED, not dropped', u.event === 'SomethingNew2027');
  add('and is flagged as not known (gate can fail)', u.known === false);

  const prompt = summarise({ hook_event_name: 'UserPromptSubmit', prompt: 'abcde' });
  add('prompt is measured, never stored', prompt.prompt_chars === 5 && !('prompt' in prompt));

  const big = summarise({ hook_event_name: 'PostToolUse', junk: 'x'.repeat(50_000) }, { maxBytes: 1024 });
  add('an oversized payload is truncated', big.truncated === true);
  add('and the truncation names what was dropped', String(big.extra).includes('junk'));
  const small = summarise({ hook_event_name: 'PostToolUse', junk: 'x' }, { maxBytes: 1024 });
  add('a small payload is NOT truncated (gate can fail)', small.truncated === false);

  add('a null payload yields an UNKNOWN row rather than throwing', summarise(null).event === 'UNKNOWN');
  add('a string payload does not crash the reducer', summarise('nonsense').event === 'UNKNOWN');

  // Probe separation, the statusline lesson.
  const tmp = join(ROOT, 'tmp', `event-selftest-${process.pid}-${Date.now()}.ndjson`);
  record({ hook_event_name: 'SessionStart', session_id: 'p1' }, tmp);
  const line = JSON.parse(readFileSync(tmp, 'utf8').trim().split(String.fromCharCode(10))[0]);
  add('a redirected write is stamped probe:true', line.probe === true);
  add('the row carries its own capture timestamp', typeof line.captured_at === 'string');
  add('exactly one line per call', readFileSync(tmp, 'utf8').trim().split(String.fromCharCode(10)).length === 1);
  rmSync(tmp, { force: true });

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch when this file IS the entry point. Importing it for its exports used to run the
// handler: event-hook blocked forever on readFileSync(0) waiting for stdin that never came, and
// compact-hook and statusline ran their main path and exited the importing process. probe.mjs
// records two earlier victims of exactly this.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (process.argv.includes('--self-test')) {
  process.exit(selfTest());
} else {
  // Never blocks and never fails loudly. A hook that throws here would surface as an error in the
  // user's session, which is a worse outcome than a missing row.
  try {
    const text = readStdin();
    if (text.trim()) record(JSON.parse(text));
  } catch (e) {
    try { process.stderr.write(`event-hook: ${e.message}\n`); } catch { /* nothing left to do */ }
  }
  process.exit(0);
}

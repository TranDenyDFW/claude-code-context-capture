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

import { appendFileSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync, existsSync, renameSync } from 'node:fs';
import { execFileSync, spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { ensureStoreDir } from '../tools/paths.mjs';
import { pathToFileURL } from 'node:url';
import { audit, applyWiring, backupSettings } from '../tools/install.mjs';

const ROOT = join(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const DEFAULT_OUT = join(ROOT, 'data', 'raw', 'events.ndjson');
const OUT_OVERRIDE = process.env.C4X_EVENTS_OUT || null;
const OUT = OUT_OVERRIDE || DEFAULT_OUT;
const IS_PROBE = Boolean(OUT_OVERRIDE);

// AN OPT-OUT, because this is the one place the tool edits a file it does not own. applyHeal
// rewrites ~/.claude/settings.json unattended on SessionStart, and its own docstring calls it "the
// most dangerous writer in the repo". Anyone who manages that file another way - a dotfiles repo, a
// config manager, a deliberate divergence - needs a way to say no that is not uninstalling capture
// altogether. Set C4X_NO_SELF_HEAL=1 and the wiring is left exactly as found.
const NO_SELF_HEAL = process.env.C4X_NO_SELF_HEAL === '1';

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
  ensureStoreDir(dirname(outPath));
  appendFileSync(outPath, JSON.stringify({
    captured_at: new Date().toISOString(), probe: isProbe, ...summarise(payload),
  }) + '\n');
}

// ---------------------------------------------------------------------------
// Keeping the store fresh without the dashboard.
//
// Until now the only routine harvest was a timer inside app.py, so closing the dashboard froze the
// store while hooks, the status line and Claude Code's own transcripts all kept writing. The data
// was never lost, but every query answered from a snapshot of whenever Python last ran, which is
// the failure this tool exists to prevent.
//
// These two events are already registered by install.mjs, so this needs no new wiring:
//   SessionEnd        eventual consistency for every session, once, at the end.
//   UserPromptSubmit  keeps it live during a long session, at the moment someone is most likely
//                     about to go and look at the data.
// ---------------------------------------------------------------------------
const HARVEST_AFTER = new Set(['SessionEnd', 'UserPromptSubmit']);

/**
 * Does this event trigger a harvest? Exported and used by the dispatch, so the routing is testable
 * on its own rather than only observable end to end. Without this the suite could pass while the
 * dispatch harvested on one event, on every event, or on none.
 */
export const shouldHarvestAfter = (event) => HARVEST_AFTER.has(event);
const HARVEST_DEBOUNCE_MS = 15000;
// A stamp file rather than the store's own mtime: the store is WAL, so its mtime lags behind the
// -wal file and would re-trigger a harvest that had just run.
const STAMP = join(ROOT, 'data', 'raw', '.last-harvest');

/** True when a harvest is worth running. A missing stamp means "never harvested", which is due. */
export function harvestDue(now = Date.now(), stampPath = STAMP, windowMs = HARVEST_DEBOUNCE_MS) {
  try {
    return (now - statSync(stampPath).mtimeMs) >= windowMs;
  } catch {
    return true;
  }
}

/**
 * How an event's harvest runs, or null for an event that runs none.
 *
 * MEASURED, replacing a comment that said "0.1s or less": over the 28,937 incremental harvests the
 * store's own harvest_runs table holds for 2026-08-26 to 2026-09-03, p50 is 792 ms, p99 is 2.8 s,
 * and 72 runs exceeded the 8 s this hook allows. An inline run pays that before the event returns.
 * On UserPromptSubmit the thing waiting is the person's own prompt, so the harvest is DETACHED:
 * spawned with nothing holding on to it, the hook returns at once, and a slow harvest can neither
 * delay the prompt nor be killed half way by the hook's timeout. On SessionEnd nothing is waiting
 * and eventual consistency is the whole point, so it stays inline and bounded.
 */
export function harvestModeFor(event) {
  if (event === 'UserPromptSubmit') return 'detached';
  if (event === 'SessionEnd') return 'inline';
  return null;
}

/**
 * Run an incremental harvest, silent, in the mode harvestModeFor() chose.
 *
 * A child process rather than an import: harvest.mjs dispatches when it is the entry point, and
 * importing it would run a harvest inside THIS process. hooks/compact-hook.mjs already shells out
 * the same way for the same reason.
 *
 * THE STAMP IS WRITTEN FIRST, which is what the sentence above it always claimed and what the
 * debounce actually needs. It used to be written after the run, and on the inline path
 * `execFileSync` BLOCKS, so it recorded a COMPLETION rather than a start. Worse, an inline harvest
 * that hit its eight second timeout threw, the write never happened, and the caller's bare catch
 * swallowed it - so a slow harvest removed the debounce entirely and the very next event started
 * another eight second harvest, and the one after that, each one making the store busier and the
 * next harvest slower.
 *
 * The trade is stated rather than assumed: a run that fails now waits out the debounce instead of
 * being retried immediately. That is the right way round. The store is unharmed either way, the
 * next event picks it up, and the failure mode this replaces was unbounded.
 */
function runHarvest(mode = 'inline') {
  const args = [join(ROOT, 'tools', 'harvest.mjs')];
  ensureStoreDir(dirname(STAMP));
  writeFileSync(STAMP, new Date().toISOString());
  if (mode === 'detached') {
    spawn(process.execPath, args, { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  } else {
    execFileSync(process.execPath, args, { stdio: 'ignore', timeout: 8000 });
  }
}

// ---------------------------------------------------------------------------
// Self-healing the wiring.
//
// "Installed" has to mean "capturing". Anything that edits ~/.claude/settings.json - another tool,
// a hand edit, a partial restore - can leave the receipt in place while no hook of ours fires, and
// the store would look complete while recording nothing.
// ---------------------------------------------------------------------------
const RECEIPT = join(ROOT, 'data', 'install-receipt.json');
const SETTINGS = join(homedir(), '.claude', 'settings.json');

/**
 * Decide whether to repair, without touching anything. Exported so the self-test drives the
 * decision directly rather than through the filesystem.
 *
 * The receipt is the consent record: install.mjs uninstall deletes it, so an uninstall must never
 * be undone by a hook that outlived it. Only 'error' findings count - a warn or info is a note
 * about the wiring, not wiring that fails to fire.
 */
export function healNeeded(settings, root, receiptExists, auditFn = audit) {
  if (!receiptExists) return { heal: false, why: 'no receipt: this install was not made by install.mjs, or was uninstalled' };
  const errors = auditFn(settings, root).filter((f) => f.level === 'error');
  if (!errors.length) return { heal: false, why: 'wiring healthy' };
  return { heal: true, why: errors.map((e) => `${e.event}: ${e.why}`).join('; '), errors };
}

/**
 * Repair the wiring if it drifted. Returns a description of what was repaired, or null.
 *
 * Written atomically via a temp file and rename, because this is the user's live settings.json and
 * a truncated write there disables every hook they have, not just ours.
 */
/**
 * Repair the wiring, backing the file up first.
 *
 * The io seam exists so the ORDER is testable. This is the most dangerous writer in the repo: it
 * fires unattended on SessionStart and rewrites the user's entire settings file, and until now it
 * did so with no backup at all, while install.mjs called backupSettings() before all three of its
 * write paths. A comment saying "back up first" is not a guarantee; a check on the call order is.
 */
export function applyHeal(io = {}) {
  const {
    loadSettings = () => JSON.parse(readFileSync(SETTINGS, 'utf8')),
    hasReceipt = () => existsSync(RECEIPT),
    backup = backupSettings,
    writeTmp = (p, s) => writeFileSync(p, s),
    promote = renameSync,
    settingsPath = SETTINGS,
  } = io;
  const settings = loadSettings();
  const decision = healNeeded(settings, ROOT, hasReceipt());
  if (!decision.heal) return null;
  const { next } = applyWiring(settings, ROOT);
  // Same function the installer uses, so the two cannot drift apart.
  const saved = backup();
  const tmp = `${settingsPath}.c4x-heal-${process.pid}`;
  writeTmp(tmp, JSON.stringify(next, null, 2) + '\n');
  promote(tmp, settingsPath);
  return saved ? `${decision.why} (backup: ${saved})` : decision.why;
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

  // ---- harvest debounce -----------------------------------------------------------------
  // The point is that a burst of prompts cannot spawn a node process per prompt.
  {
    const stamp = join(ROOT, 'tmp', `stamp-${process.pid}-${Date.now()}`);
    rmSync(stamp, { force: true });
    // Routing, checked per event. The debounce checks below call harvestDue() directly and would
    // pass even if the dispatch harvested on the wrong event, or on every event, or on none.
    add('SessionEnd triggers a harvest', shouldHarvestAfter('SessionEnd') === true);
    add('UserPromptSubmit triggers a harvest', shouldHarvestAfter('UserPromptSubmit') === true);
    add('PostToolUse does NOT, or every tool call would spawn one (gate can fail)',
      shouldHarvestAfter('PostToolUse') === false);
    add('SessionStart does NOT: it is the self-heal event, and nothing has happened yet to harvest',
      shouldHarvestAfter('SessionStart') === false);
    add('an unknown event does not trigger one', shouldHarvestAfter('SomethingNew2027') === false);
    // The mode, per event: the prompt must not wait, the session end may.
    add('UserPromptSubmit harvests DETACHED, so the prompt does not wait',
      harvestModeFor('UserPromptSubmit') === 'detached');
    add('SessionEnd harvests inline and bounded, with nothing waiting on it',
      harvestModeFor('SessionEnd') === 'inline');
    add('an event that does not harvest has no mode (gate can fail)',
      harvestModeFor('PostToolUse') === null);

    add('with no stamp at all, a harvest is due', harvestDue(Date.now(), stamp, 15000) === true);
    mkdirSync(dirname(stamp), { recursive: true });
    writeFileSync(stamp, 'x');
    add('immediately after a harvest, another is NOT due (gate can fail)',
      harvestDue(Date.now(), stamp, 15000) === false);
    // Same stamp, same code path, only the clock moves.
    add('once the window has passed, a harvest is due again',
      harvestDue(Date.now() + 20000, stamp, 15000) === true);
    rmSync(stamp, { force: true });
  }

  // ---- self-heal decision ---------------------------------------------------------------
  // Driven through healNeeded() with a stub audit, so the decision is tested without touching
  // the real settings file or depending on this machine's install being healthy.
  {
    const healthy = () => [];
    const broken = () => [{ level: 'error', event: 'PostToolUse', why: 'not wired' }];
    const warnOnly = () => [{ level: 'warn', event: 'statusLine', why: 'not set' }];

    add('no receipt means never heal, even when the wiring is broken (gate can fail)',
      healNeeded({}, ROOT, false, broken).heal === false);
    add('receipt plus broken wiring heals',
      healNeeded({}, ROOT, true, broken).heal === true);
    add('receipt plus healthy wiring does nothing',
      healNeeded({}, ROOT, true, healthy).heal === false);
    // A warn is a note about the wiring, not wiring that fails to fire. Healing on one would
    // rewrite settings.json every session forever over a statusLine the user chose not to set.
    add('a warn-level finding alone does NOT trigger a rewrite (gate can fail)',
      healNeeded({}, ROOT, true, warnOnly).heal === false);
    add('the reason names the offending event, so the log says what was repaired',
      healNeeded({}, ROOT, true, broken).why.includes('PostToolUse'));
  }

  // ---- self-heal against the REAL converge logic ------------------------------------------
  // Not a stub: drift a settings object the way an editor would, and confirm applyWiring both
  // repairs our events and leaves a foreign hook alone.
  {
    const foreign = { type: 'command', command: 'node "C:/somebody/else/hook.mjs"' };
    const drifted = { hooks: { Stop: [{ hooks: [foreign] }] } };   // ours removed entirely
    const before = healNeeded(drifted, ROOT, true);
    add('a settings file with our hooks stripped is detected as broken', before.heal === true);
    const { next } = applyWiring(drifted, ROOT);
    // The write ORDER. A backup that happens after the write, or not at all, is what this repo
    // shipped: the hook rewrote the whole settings file unattended with nothing to go back to.
    {
      const calls = [];
      const stripped = structuredClone(drifted);
      const healed = applyHeal({
        loadSettings: () => structuredClone(stripped),
        hasReceipt: () => true,
        backup: () => { calls.push('backup'); return '/tmp/settings.bak'; },
        writeTmp: () => calls.push('write'),
        promote: () => calls.push('promote'),
        settingsPath: '/tmp/settings.json',
      });
      add('self-heal backs up BEFORE it writes', calls.join(',') === 'backup,write,promote', calls.join(','));
      add('self-heal reports the backup it took', String(healed).includes('/tmp/settings.bak'), String(healed));

      const noop = [];
      applyHeal({
        loadSettings: () => ({}), hasReceipt: () => false,
        backup: () => { noop.push('backup'); return null; },
        writeTmp: () => noop.push('write'), promote: () => noop.push('promote'),
      });
      add('no receipt means no backup and no write (gate can fail)', noop.length === 0, noop.join(','));
    }

    const after = healNeeded(next, ROOT, true);
    add('after applyWiring the same settings audit clean', after.heal === false);
    add('the foreign Stop hook survives the repair',
      JSON.stringify(next.hooks.Stop).includes('somebody/else/hook.mjs'));
  }

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
    if (text.trim()) {
      const payload = JSON.parse(text);
      record(payload);

      // Side effects are skipped entirely under C4X_EVENTS_OUT: that override marks a probe or a
      // test run, and neither should rewrite the user's settings or spawn a harvest.
      if (!IS_PROBE) {
        const event = payload?.hook_event_name;

        if (event === 'SessionStart' && !NO_SELF_HEAL) {
          // Each in its own try: a failed repair must not stop the harvest, and vice versa.
          try {
            const repaired = applyHeal();
            // Recorded through the normal path so the rewrite lands in hook_events.reason. A tool
            // that edits your settings without being asked should at minimum say that it did.
            if (repaired) record({ hook_event_name: 'SessionStart', reason: `c4x self-heal: ${repaired}` });
          } catch (e) {
            try { process.stderr.write(`event-hook: self-heal skipped: ${e.message}\n`); } catch { /* nothing left */ }
          }
        }

        if (shouldHarvestAfter(event) && harvestDue()) {
          try { runHarvest(harvestModeFor(event)); } catch { /* a stale store beats a hook that errors in the session */ }
        }
      }
    }
  } catch (e) {
    try { process.stderr.write(`event-hook: ${e.message}\n`); } catch { /* nothing left to do */ }
  }
  process.exit(0);
}

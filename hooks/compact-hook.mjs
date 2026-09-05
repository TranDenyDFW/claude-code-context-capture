#!/usr/bin/env node
// compact-hook.mjs - handles both PreCompact and PostCompact.
//
// Why this exists: the transcript is append-only TODAY, so pre-compaction history can be
// recovered after the fact. But `performCompactTranscript` (binary offset 287682203) physically
// rewrites the transcript with an atomic rename once ~20 MiB accumulates past a boundary, gated
// on `localGcEnabled`, which defaults false but is switchable by a remote flag we do not control.
// So the safe design snapshots BEFORE the drop rather than trusting recovery.
//
// PreCompact  fires before anything is dropped. Payload adds `trigger` and `custom_instructions`.
//             This handler ingests the transcript into the store and optionally snapshots it.
// PostCompact fires after, and adds `compact_summary`. Cannot block.
//
// This handler NEVER blocks. It always exits 0 with no decision, even on internal failure.
// Blocking compaction to win a measurement would corrupt the very sessions being measured.
//
//   node compact-hook.mjs              read hook JSON on stdin
//   node compact-hook.mjs --self-test

import { appendFileSync, copyFileSync, mkdirSync, readFileSync, statSync, existsSync, rmSync, writeFileSync } from 'node:fs';
import { join, dirname, basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
import { ensureStoreDir } from '../tools/paths.mjs';

const ROOT = join(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const EVENTS = join(ROOT, 'data', 'raw', 'compaction-events.ndjson');
const SNAP_DIR = join(ROOT, 'data', 'snapshots');

// Declared budget. A snapshot larger than this is skipped and the skip is RECORDED, never
// silent, so a gap in the snapshot set always has a reason attached to it.
const SNAPSHOT_MAX_BYTES = 250 * 1024 * 1024;
const SNAPSHOT_ENABLED = process.env.C4X_SNAPSHOT !== '0';

function log(obj, path = EVENTS) {
  ensureStoreDir(dirname(path));
  appendFileSync(path, JSON.stringify(obj) + '\n');
}

export function handle(d, opts = {}) {
  const eventsPath = opts.eventsPath ?? EVENTS;
  const snapDir = opts.snapDir ?? SNAP_DIR;
  const snapshotEnabled = opts.snapshotEnabled ?? SNAPSHOT_ENABLED;
  const runHarvest = opts.runHarvest ?? true;

  const ev = {
    captured_at: new Date().toISOString(),
    hook_event_name: d?.hook_event_name ?? null,
    session_id: d?.session_id ?? null,
    transcript_path: d?.transcript_path ?? null,
    cwd: d?.cwd ?? null,
    trigger: d?.trigger ?? null,
    custom_instructions: d?.custom_instructions ?? null,
    compact_summary_chars: typeof d?.compact_summary === 'string' ? d.compact_summary.length : null,
    compact_summary: typeof d?.compact_summary === 'string' ? d.compact_summary : null,
    transcript: null,
    snapshot: null,
    harvest: null,
  };

  const tp = d?.transcript_path;
  if (tp && existsSync(tp)) {
    const st = statSync(tp);
    ev.transcript = { size: st.size, mtime_ms: Math.round(st.mtimeMs) };

    if (d?.hook_event_name === 'PreCompact' && snapshotEnabled) {
      if (st.size > SNAPSHOT_MAX_BYTES) {
        ev.snapshot = { skipped: true, reason: 'over budget', size: st.size, budget: SNAPSHOT_MAX_BYTES };
      } else {
        try {
          ensureStoreDir(snapDir);
          const stamp = new Date().toISOString().replace(/[:.]/g, '-');
          const dest = join(snapDir, `${basename(tp, '.jsonl')}.${stamp}.pre-compact.jsonl`);
          copyFileSync(tp, dest);
          ev.snapshot = { path: dest, bytes: st.size };
        } catch (e) {
          ev.snapshot = { skipped: true, reason: 'copy failed: ' + e.message };
        }
      }
    }
  } else if (tp) {
    ev.transcript = { missing: true };
  }

  // Ingest now, so the usage rows up to this compaction are durable in the store regardless of
  // what happens to the transcript file afterwards.
  if (runHarvest && d?.hook_event_name === 'PreCompact') {
    try {
      const t0 = Date.now();
      execFileSync(process.execPath, [join(ROOT, 'tools', 'harvest.mjs')], { stdio: 'pipe', timeout: 120000 });
      ev.harvest = { ok: true, ms: Date.now() - t0 };
    } catch (e) {
      ev.harvest = { ok: false, error: String(e.message).slice(0, 300) };
    }
  }

  log(ev, eventsPath);
  return ev;
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);
  const tmp = join(ROOT, 'tmp', 'hook-selftest');
  rmSync(tmp, { recursive: true, force: true });
  mkdirSync(tmp, { recursive: true });

  const fakeTranscript = join(tmp, 'sess-abc.jsonl');
  const transcriptBody = '{"type":"user"}\n{"type":"assistant"}\n';
  writeFileSync(fakeTranscript, transcriptBody);
  const summaryText = 'This session is being continued from a previous conversation.';
  const eventsPath = join(tmp, 'events.ndjson');
  const snapDir = join(tmp, 'snap');

  const pre = handle({
    hook_event_name: 'PreCompact', session_id: 'abc', transcript_path: fakeTranscript,
    cwd: 'C:/x', trigger: 'auto', custom_instructions: null,
  }, { eventsPath, snapDir, runHarvest: false });

  add('PreCompact records the trigger', pre.trigger === 'auto');
  add('PreCompact records transcript size', pre.transcript?.size === Buffer.byteLength(transcriptBody), JSON.stringify(pre.transcript));
  add('PreCompact snapshots the file', !!pre.snapshot?.path && existsSync(pre.snapshot.path));
  add('snapshot is byte-identical to the source',
    !!pre.snapshot?.path && readFileSync(pre.snapshot.path, 'utf8') === readFileSync(fakeTranscript, 'utf8'));

  const post = handle({
    hook_event_name: 'PostCompact', session_id: 'abc', transcript_path: fakeTranscript,
    trigger: 'auto', compact_summary: summaryText,
  }, { eventsPath, snapDir, runHarvest: false });
  add('PostCompact captures the summary text', post.compact_summary?.startsWith('This session is being continued'));
  add('PostCompact records summary length', post.compact_summary_chars === summaryText.length, String(post.compact_summary_chars));
  add('PostCompact does not snapshot', post.snapshot === null);

  const lines = readFileSync(eventsPath, 'utf8').trim().split('\n');
  add('both events were appended', lines.length === 2);

  // A missing transcript must be recorded, not crash and not silently pass.
  const missing = handle({ hook_event_name: 'PreCompact', transcript_path: join(tmp, 'nope.jsonl') },
    { eventsPath, snapDir, runHarvest: false });
  add('missing transcript is recorded as missing', missing.transcript?.missing === true);

  // Negative control: the handler must actually read its input.
  const other = handle({ hook_event_name: 'PreCompact', session_id: 'zzz', trigger: 'manual' },
    { eventsPath, snapDir, runHarvest: false });
  add('different input produces different output (gate can fail)', other.session_id === 'zzz' && other.trigger === 'manual');

  rmSync(tmp, { recursive: true, force: true });
  let bad = 0;
  for (const [n, ok, d] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`); }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch when this file IS the entry point. Importing it for its exports used to run the
// handler and exit the importing process. probe.mjs records two earlier victims of this.
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
  let d = null, raw = '', parseError = null;
  try { raw = readFileSync(0, 'utf8'); } catch (e) { parseError = 'stdin read failed: ' + e.message; }
  if (raw.trim()) {
    try { d = JSON.parse(raw); } catch (e) { parseError = 'stdin was not valid JSON: ' + e.message; }
  } else if (!parseError) {
    parseError = 'stdin was empty';
  }

  if (d) {
    try { handle(d); } catch (e) {
      try { log({ captured_at: new Date().toISOString(), handler_error: String(e.message).slice(0, 500) }); } catch {}
    }
  } else {
    // A compaction we could not record is a GAP in the data, and a gap with no explanation is
    // indistinguishable from a compaction that never happened. Record the failure and the input
    // that caused it. Exiting 0 silently here would make the capture quietly lossy.
    try {
      log({ captured_at: new Date().toISOString(), hook_event_name: null, parse_error: parseError, raw_stdin: raw.slice(0, 2000) });
    } catch {}
    process.stderr.write('compact-hook: ' + parseError + '\n');
  }
  // Always exit 0. This handler must never block a compaction.
  process.exit(0);
}

#!/usr/bin/env node
// segments.mjs - split a session into contiguous model runs and resolve the context window for
// each one.
//
// Why: the window is not a property of a session, it is a property of the model in use. The set
// {claude-sonnet-4-6, claude-opus-4-6, claude-opus-4-8, claude-opus-5} is forced to 200000 when
// the raw model ceiling is under 1M, so switching into or out of it moves the compaction trigger
// between 167000 and 967000 mid-session. 26 sessions in this store have a genuine main-thread
// model switch. Treating the window as constant per session is wrong for all of them.
//
// The transcript never records the window that was actually in effect, so this INFERS it, and
// says which evidence it used. Ranked strongest to weakest:
//
//   observed-compaction  a compaction fired in this segment, and preTokens sits just above
//                        exactly one candidate threshold. Strongest: the client told us.
//   observed-peak        the segment reached more than 200000 resident tokens, so the raw
//                        ceiling was at least 1M and the small-window cap did not apply.
//   model-default        no size evidence, but the model is in the small-window set, so 200000
//                        is the likely window. Marked weak: it is a guess with a reason.
//   unknown              nothing to go on. Returns null and the candidate list, never a number
//                        dressed up as a fact.
//
// Usage:
//   node segments.mjs --session <id>
//   node segments.mjs --audit          falsification sweep over every compaction on record
//   node segments.mjs --self-test

import { DatabaseSync } from 'node:sqlite';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { K, SMALL_WINDOW_MODELS, resolveWindow, reportedAutoCompactThreshold } from './mirror-core.mjs';
import { rootFrom, resolveDb } from './paths.mjs';

// Three writers share this store by design: a manual harvest, the SessionEnd and UserPromptSubmit
// hooks, and the dashboard's refresh loop. SQLite's default busy timeout is ZERO, so a reader that
// arrives mid-write fails outright with SQLITE_BUSY instead of waiting. That killed a --full
// harvest at 7.5 GB and, separately, made segments.mjs throw "database is locked" while the app
// was importing. A reader losing a race should be slow, not absent.
const BUSY_TIMEOUT = 'PRAGMA busy_timeout = 15000';

const ROOT = rootFrom(import.meta.url);
const DB_PATH = resolveDb(ROOT);

export const CANDIDATE_WINDOWS = [200000, 500000, 967000, 1000000];

// `<synthetic>` is not a model the user selected; it tags injected records. Letting it start a
// segment would manufacture switches that never happened.
const NOT_A_MODEL = new Set(['<synthetic>', null, undefined, '']);

/**
 * Group turns into contiguous runs of the same model.
 * @param rows [{ts, model, total_resident, is_sidechain}] in timestamp order
 */
export function segmentSession(rows) {
  const segs = [];
  let cumulativePeak = 0;
  for (const r of rows) {
    if (r.is_sidechain) continue;                     // subagents run their own model, not yours
    const resident = r.total_resident || 0;
    cumulativePeak = Math.max(cumulativePeak, resident);
    if (NOT_A_MODEL.has(r.model)) {
      // Attach to the current segment rather than starting one.
      if (segs.length) {
        const cur = segs[segs.length - 1];
        cur.endTs = r.ts; cur.turns += 1;
        cur.peakResident = Math.max(cur.peakResident, resident);
        cur.cumulativePeak = cumulativePeak;
      }
      continue;
    }
    const cur = segs[segs.length - 1];
    if (cur && cur.model === r.model) {
      cur.endTs = r.ts; cur.turns += 1;
      cur.peakResident = Math.max(cur.peakResident, resident);
      cur.cumulativePeak = cumulativePeak;
    } else {
      segs.push({
        model: r.model, startTs: r.ts, endTs: r.ts, turns: 1,
        peakResident: resident, cumulativePeak,
      });
    }
  }
  return segs;
}

/** The candidate windows whose compact threshold sits at or below `preTokens`, nearest first. */
export function windowsConsistentWith(preTokens) {
  return CANDIDATE_WINDOWS
    .map((w) => ({ window: w, threshold: reportedAutoCompactThreshold(w) }))
    .filter((c) => preTokens >= c.threshold)
    .sort((a, b) => b.threshold - a.threshold);
}

/**
 * Resolve the window for one segment.
 * @param segment from segmentSession
 * @param compactions [{pre_tokens, ts}] that fired inside this segment
 * @param opts.forceRawMax test hook: pin the inferred raw ceiling to prove the audit can fail
 */
export function windowForSegment(segment, compactions = [], opts = {}) {
  const inSeg = compactions.filter(
    (c) => c.pre_tokens != null
      && (!segment.startTs || c.ts >= segment.startTs)
      && (!segment.endTs || c.ts <= segment.endTs));

  if (inSeg.length) {
    // Take the largest, since a bigger window is never contradicted by a smaller compaction.
    const best = inSeg.reduce((a, b) => (b.pre_tokens > a.pre_tokens ? b : a));
    const cand = windowsConsistentWith(best.pre_tokens)[0];
    if (cand) {
      return { window: cand.window, confidence: 'observed-compaction', evidence:
        `compaction at ${best.pre_tokens} tokens, just past the ${cand.threshold} threshold` };
    }
  }

  const peak = opts.forceRawMax !== undefined ? 0 : segment.cumulativePeak ?? segment.peakResident ?? 0;
  const rawMax = opts.forceRawMax !== undefined
    ? opts.forceRawMax
    : (peak > K.SMALL_WINDOW ? K.MAX_WINDOW : undefined);

  if (rawMax !== undefined) {
    const r = resolveWindow({ model: segment.model, rawMax });
    return { window: r.window, confidence: 'observed-peak', evidence:
      `reached ${peak} resident tokens, so the raw ceiling was at least ${rawMax}` };
  }

  if (SMALL_WINDOW_MODELS.has(segment.model)) {
    return { window: K.SMALL_WINDOW, confidence: 'model-default', evidence:
      `${segment.model} is capped to ${K.SMALL_WINDOW} unless the raw ceiling is 1M, and nothing here shows it was` };
  }

  return { window: null, confidence: 'unknown', candidates: CANDIDATE_WINDOWS, evidence:
    'no compaction and never exceeded the small-window cap, so the window is not determinable' };
}

// ---------------------------------------------------------------------------
function db() {
  const d = new DatabaseSync(`file:${DB_PATH}?mode=ro`, { readOnly: true });
  d.exec(BUSY_TIMEOUT);
  return d;
}

export function loadSession(sessionId) {
  const d = db();
  const rows = d.prepare(
    'SELECT ts, model, total_resident, is_sidechain FROM turns WHERE session_id = ? ORDER BY ts'
  ).all(sessionId);
  const comps = d.prepare(
    'SELECT ts, pre_tokens FROM compactions WHERE session_id = ? ORDER BY ts'
  ).all(sessionId);
  d.close();
  return { rows, comps };
}

function showSession(sessionId) {
  const { rows, comps } = loadSession(sessionId);
  const segs = segmentSession(rows);
  const out = segs.map((s) => ({ ...s, ...windowForSegment(s, comps) }));
  console.log(JSON.stringify({ session: sessionId, turns: rows.length, compactions: comps.length,
    segments: out }, null, 2));
  return 0;
}

/**
 * Rank violations by how far the peak exceeded the window, worst first.
 *
 * Exported so it can be tested independently of the store. It could not be before, and a
 * comparator that read a property the rows do not carry (`peak` rather than `peak_after_binding`)
 * returned NaN for every pair, leaving the list in discovery order under a label that claimed it
 * was sorted. With zero violations the list is empty, so nothing ever showed it.
 */
export function worstFirst(violations) {
  return [...violations].sort(
    (a, b) => (b.peak_after_binding - b.window) - (a.peak_after_binding - a.window));
}

/**
 * Falsification sweep. A window smaller than a token count the session actually reached is
 * impossible, so any such row refutes the inference.
 */
export function audit(opts = {}) {
  const d = db();
  const sessions = d.prepare(
    'SELECT DISTINCT session_id FROM compactions WHERE pre_tokens IS NOT NULL'
  ).all().map((r) => r.session_id);

  let checked = 0, violations = [], byConfidence = {}, carryoverTurns = 0;
  for (const sid of sessions) {
    const rows = d.prepare(
      'SELECT ts, model, total_resident, is_sidechain FROM turns WHERE session_id = ? ORDER BY ts'
    ).all(sid);
    const comps = d.prepare(
      'SELECT ts, pre_tokens FROM compactions WHERE session_id = ? AND pre_tokens IS NOT NULL ORDER BY ts'
    ).all(sid);
    for (const seg of segmentSession(rows)) {
      const w = windowForSegment(seg, comps, opts);
      byConfidence[w.confidence] = (byConfidence[w.confidence] || 0) + 1;
      if (w.window == null) continue;
      checked++;

      // When the window SHRINKS (a switch into the small-window set), the turns immediately
      // after the switch still carry context accumulated under the old, larger window, and they
      // legitimately exceed the new one until the next compaction brings them down. Measured
      // case: session 86b46334 switched to claude-sonnet-4-6 holding 219392 tokens, and a
      // compaction at 221442 three minutes later dropped it to 64304. So the window is only
      // binding from the first compaction inside the segment onward. Turns before that are
      // counted as carry-over and reported, never silently discarded.
      const firstComp = comps.find((c) => c.ts >= seg.startTs && c.ts <= seg.endTs);
      const bindingFrom = firstComp ? firstComp.ts : seg.startTs;
      let effectivePeak = 0, carryover = 0;
      for (const r of rows) {
        if (r.is_sidechain) continue;
        if (r.ts < seg.startTs || r.ts > seg.endTs) continue;
        const resident = r.total_resident || 0;
        if (r.ts <= bindingFrom) { if (resident > w.window) carryover++; continue; }
        effectivePeak = Math.max(effectivePeak, resident);
      }
      if (carryover) carryoverTurns += carryover;

      if (w.window < effectivePeak) {
        violations.push({ session: sid.slice(0, 8), model: seg.model, window: w.window,
          peak_after_binding: effectivePeak, raw_peak: seg.peakResident, confidence: w.confidence });
      }
    }
  }
  d.close();
  const out = {
    sessions: sessions.length, segments_checked: checked,
    by_confidence: byConfidence,
    carryover_turns_allowed: carryoverTurns,
    violations: violations.length,
    worst: worstFirst(violations).slice(0, 5),
  };
  if (!opts.quiet) console.log(JSON.stringify(out, null, 2));
  return out;
}

// ---------------------------------------------------------------------------
function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  const T = (ts, model, resident, side = 0) =>
    ({ ts, model, total_resident: resident, is_sidechain: side });

  // A single-model session is exactly one segment.
  const one = segmentSession([T('1', 'claude-opus-5', 100), T('2', 'claude-opus-5', 200)]);
  add('single model yields one segment', one.length === 1 && one[0].turns === 2);

  // A switch splits.
  const two = segmentSession([
    T('1', 'claude-opus-4-8', 100), T('2', 'claude-sonnet-4-6', 219392), T('3', 'claude-opus-4-8', 156155)]);
  add('a model switch splits the session', two.length === 3, String(two.length));
  add('segment boundary carries the switch timestamp', two[1].startTs === '2');

  // Sidechains are subagents, not your model.
  const side = segmentSession([T('1', 'claude-opus-5', 100), T('2', 'claude-haiku-4-5', 50, 1), T('3', 'claude-opus-5', 300)]);
  add('sidechain turns do not split a segment', side.length === 1 && side[0].turns === 2, String(side.length));

  // <synthetic> attaches rather than splitting.
  const syn = segmentSession([T('1', 'claude-opus-5', 100), T('2', '<synthetic>', 120), T('3', 'claude-opus-5', 300)]);
  add('<synthetic> does not manufacture a switch', syn.length === 1 && syn[0].turns === 3, String(syn.length));

  // Window resolution by evidence rank.
  const seg1M = { model: 'claude-opus-4-8', peakResident: 999000, cumulativePeak: 999000, startTs: '1', endTs: '9' };
  const w1 = windowForSegment(seg1M, [{ ts: '5', pre_tokens: 1009556 }]);
  add('a compaction pins the window', w1.window === 1000000 && w1.confidence === 'observed-compaction',
    JSON.stringify(w1));

  const w2 = windowForSegment(seg1M, []);
  add('peak above the cap implies a 1M ceiling', w2.window === 1000000 && w2.confidence === 'observed-peak',
    JSON.stringify(w2));

  const small = { model: 'claude-opus-4-8', peakResident: 150000, cumulativePeak: 150000, startTs: '1', endTs: '9' };
  const w3 = windowForSegment(small, []);
  add('a small-window model with no size evidence guesses 200000, marked weak',
    w3.window === 200000 && w3.confidence === 'model-default', JSON.stringify(w3));

  const unknown = { model: 'claude-made-up', peakResident: 1000, cumulativePeak: 1000, startTs: '1', endTs: '9' };
  const w4 = windowForSegment(unknown, []);
  add('an unknown model with no evidence returns null, not a number',
    w4.window === null && Array.isArray(w4.candidates), JSON.stringify(w4));

  // A compaction at 167000 must pin 200000, not 1M.
  const w5 = windowForSegment(small, [{ ts: '5', pre_tokens: 167063 }]);
  add('a 167k compaction pins the 200k window', w5.window === 200000, JSON.stringify(w5));

  // The ranking that had no coverage, which is how a NaN comparator survived.
  const v = [
    { session: 'a', window: 200000, peak_after_binding: 210000 },   // over by 10k
    { session: 'b', window: 200000, peak_after_binding: 260000 },   // over by 60k
    { session: 'c', window: 1000000, peak_after_binding: 1030000 }, // over by 30k
  ];
  const ranked = worstFirst(v).map((r) => r.session);
  add('violations rank worst-first by how far the peak exceeded the window',
    ranked.join(',') === 'b,c,a', ranked.join(','));
  add('ranking does not mutate the input array (gate can fail)',
    v.map((r) => r.session).join(',') === 'a,b,c');
  add('an empty violation list ranks to empty rather than throwing', worstFirst([]).length === 0);

  // Falsification: the audit must be capable of failing. Pinning the raw ceiling to 200000
  // ignores the peak evidence, so every segment that really reached ~1M becomes impossible.
  let real = null, mutant = null;
  try {
    real = audit({ quiet: true });
    mutant = audit({ quiet: true, forceRawMax: 200000 });
  } catch (e) { /* store may be absent */ }
  if (real && mutant) {
    add('the real audit finds no impossible window', real.violations === 0, JSON.stringify(real.worst?.[0] || {}));
    add('pinning the ceiling to 200k makes the audit FAIL (gate can fail)',
      mutant.violations > 0, `real=${real.violations} mutant=${mutant.violations}`);
  } else {
    add('audit ran against the store', false, 'store unavailable');
  }

  let bad = 0;
  for (const [n, ok, d] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`); }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

/**
 * Window per compaction, for every compaction on record, in ONE pass. The UI needs this for 134
 * rows and must not shell out once per row.
 */
export function windowsForCompactions() {
  const d = db();
  const sessions = d.prepare(
    'SELECT DISTINCT session_id FROM compactions WHERE pre_tokens IS NOT NULL'
  ).all().map((r) => r.session_id);
  const out = {};
  for (const sid of sessions) {
    const rows = d.prepare(
      'SELECT ts, model, total_resident, is_sidechain FROM turns WHERE session_id = ? ORDER BY ts'
    ).all(sid);
    const comps = d.prepare(
      'SELECT uuid, ts, pre_tokens FROM compactions WHERE session_id = ? AND pre_tokens IS NOT NULL ORDER BY ts'
    ).all(sid);
    const segs = segmentSession(rows);
    for (const c of comps) {
      const seg = segs.find((s) => c.ts >= s.startTs && c.ts <= s.endTs);
      // A compaction with no covering segment (no non-sidechain turns recorded around it) is
      // reported as unresolved rather than silently assigned a window.
      const w = seg ? windowForSegment(seg, comps) : { window: null, confidence: 'no-segment' };
      out[c.uuid] = { window: w.window, confidence: w.confidence, model: seg ? seg.model : null };
    }
  }
  d.close();
  return out;
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
else if (argv.includes('--windows-for-compactions')) {
  console.log(JSON.stringify(windowsForCompactions()));
  process.exit(0);
}
else if (argv.includes('--audit')) { audit(); process.exit(0); }
else if (argv.includes('--session')) process.exit(showSession(argv[argv.indexOf('--session') + 1]));
else { console.log('usage: segments.mjs --session <id> | --audit | --self-test'); process.exit(2); }

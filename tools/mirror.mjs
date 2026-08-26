#!/usr/bin/env node
// mirror.mjs - a local reimplementation of Claude Code's context window and compaction math,
// plus the validation that proves (or refutes) that the reimplementation is right.
//
// Every constant and formula below is transcribed from the shipped binary
// ~/.local/share/claude/versions/2.1.229. Byte offsets are cited so any claim here
// can be re-checked against the binary rather than trusted.
//
//   sZs  offset 280112431   the level function (ok / warn / compact / blocked)
//   M5o  offset 280112204   the compact threshold, window - 13000
//   hG   offset 280114317   window resolution with its precedence chain
//   Ihe  offset 280115401   window - min(maxOutputTokens, 20000)
//   a6d  offset 280117184   the per-model default table
//   consts offset 280112755 and 280117024
//
// Usage:
//   node mirror.mjs --self-test          unit checks plus a mutant that must fail
//   node mirror.mjs --validate           fit the model against every real compaction on record
//   node mirror.mjs --predict <tokens> --window <n> [--max-output <n>] [--no-autocompact]

import { DatabaseSync } from 'node:sqlite';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { rootFrom, resolveDb } from './paths.mjs';

const ROOT = rootFrom(import.meta.url);
const DB_PATH = resolveDb(ROOT);

export { K, SMALL_WINDOW_MODELS, MODEL_DEFAULT_WINDOW, compactThreshold, level, usableWindow, reportedAutoCompactThreshold, resolveWindow, assess } from './mirror-core.mjs';
import { K, compactThreshold, level, usableWindow, reportedAutoCompactThreshold, resolveWindow, assess } from './mirror-core.mjs';

// ---------------------------------------------------------------------------
// Validation against every compaction on record.
// ---------------------------------------------------------------------------
//
// What this can and cannot prove, stated plainly:
//
// The transcript records preTokens at the moment of compaction but NOT the window that was in
// effect, and the same model appears at both 200k and 1M windows in this data. So this is a FIT,
// not a blind prediction: for each event we pick the candidate window whose threshold is the
// largest one at or below preTokens, and measure the overshoot.
//
// Overshoot must be >= 0 (compaction cannot fire below its own threshold) and should be roughly
// the size of one turn, since the token count jumps by whole turns. A negative overshoot means
// the formula is wrong for that event. That is the falsifying observation.
//
// Two candidate trigger formulas are tested, because the display path and the trigger path in the
// binary do not obviously use the same base:
//   A  window - 20000 - 13000   (matches the reported autoCompactThreshold)
//   B  window - 13000           (M5o applied directly to the window)

const CANDIDATE_WINDOWS = [200000, 500000, 967000, 1000000];

function thresholdsFor(formula) {
  return CANDIDATE_WINDOWS.map((w) => ({
    window: w,
    threshold: formula === 'A' ? reportedAutoCompactThreshold(w) : compactThreshold(w),
  })).sort((a, b) => a.threshold - b.threshold);
}

function fitOne(preTokens, formula, buffer = K.AUTOCOMPACT_BUFFER) {
  const cands = CANDIDATE_WINDOWS.map((w) => ({
    window: w,
    threshold: formula === 'A' ? w - K.MAX_OUTPUT_RESERVE - buffer : w - buffer,
  })).sort((a, b) => b.threshold - a.threshold);
  for (const c of cands) if (preTokens >= c.threshold) return { ...c, overshoot: preTokens - c.threshold };
  const lowest = cands[cands.length - 1];
  return { ...lowest, overshoot: preTokens - lowest.threshold };
}

function pct(arr, p) {
  if (!arr.length) return null;
  const s = [...arr].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
}

function validate({ buffer = K.AUTOCOMPACT_BUFFER, quiet = false } = {}) {
  const db = new DatabaseSync(DB_PATH);
  const rows = db.prepare(`
    SELECT c.uuid, c.ts, c.version, c.session_id, c.pre_tokens, c.post_tokens,
      (SELECT t.model FROM turns t WHERE t.session_id = c.session_id AND t.ts <= c.ts ORDER BY t.ts DESC LIMIT 1) AS model
    FROM compactions c WHERE c.pre_tokens IS NOT NULL ORDER BY c.ts`).all();
  db.close();

  const result = {};
  for (const formula of ['A', 'B']) {
    const fits = rows.map((r) => ({ ...r, fit: fitOne(r.pre_tokens, formula, buffer) }));
    const overs = fits.map((f) => f.fit.overshoot);
    const negatives = fits.filter((f) => f.fit.overshoot < 0);
    result[formula] = {
      formula: formula === 'A' ? 'window - 20000 - ' + buffer : 'window - ' + buffer,
      n: fits.length,
      negative_overshoots: negatives.length,
      negative_pct: +(100 * negatives.length / fits.length).toFixed(1),
      overshoot_min: Math.min(...overs),
      overshoot_p50: pct(overs, 50),
      overshoot_p90: pct(overs, 90),
      overshoot_max: Math.max(...overs),
      worst: negatives.sort((a, b) => a.fit.overshoot - b.fit.overshoot).slice(0, 3)
        .map((f) => ({ ts: f.ts, version: f.version, model: f.model, pre: f.pre_tokens, window: f.fit.window, threshold: f.fit.threshold, overshoot: f.fit.overshoot })),
      by_window: CANDIDATE_WINDOWS.map((w) => {
        const sub = fits.filter((f) => f.fit.window === w);
        if (!sub.length) return null;
        const o = sub.map((f) => f.fit.overshoot);
        return { window: w, n: sub.length, p50: pct(o, 50), min: Math.min(...o), negatives: o.filter((x) => x < 0).length };
      }).filter(Boolean),
    };
  }

  const best = result.A.negative_overshoots <= result.B.negative_overshoots ? 'A' : 'B';

  // Per-build breakdown for the winning formula. Constants have changed across builds before,
  // so a residual that clusters in one version is a dated fact about that build rather than a
  // refutation of the formula. Reporting it per version is what makes that distinguishable.
  const bestFits = rows.map((r) => ({ ...r, fit: fitOne(r.pre_tokens, best, buffer) }));
  const versions = [...new Set(bestFits.map((f) => f.version))].sort();
  const byVersion = versions.map((v) => {
    const sub = bestFits.filter((f) => f.version === v);
    const o = sub.map((f) => f.fit.overshoot);
    return { version: v, n: sub.length, negatives: o.filter((x) => x < 0).length, min: Math.min(...o), p50: pct(o, 50) };
  });

  const out = {
    db: DB_PATH, compactions: rows.length, buffer_used: buffer, candidates: result, better_fit: best,
    by_version: byVersion,
    anomalies: bestFits.filter((f) => f.fit.overshoot < 0).map((f) => ({
      ts: f.ts, version: f.version, model: f.model, session: f.session_id,
      pre_tokens: f.pre_tokens, fitted_window: f.fit.window, threshold: f.fit.threshold, below_by: -f.fit.overshoot,
    })),
  };
  if (!quiet) console.log(JSON.stringify(out, null, 2));
  return out;
}

// ---------------------------------------------------------------------------
function selfTest() {
  const checks = [];
  const eq = (name, got, want) => checks.push([name, got === want, `got ${got} want ${want}`]);

  // Hand-computed from the transcribed source.
  eq('compactThreshold(200000) = 187000', compactThreshold(200000), 187000);
  eq('compactThreshold(1000000) = 987000', compactThreshold(1000000), 987000);
  eq('usableWindow(200000, 32000) = 180000', usableWindow(200000, 32000), 180000);
  eq('usableWindow(200000, 8000) = 192000', usableWindow(200000, 8000), 192000);
  eq('reportedAutoCompactThreshold(1000000) = 967000', reportedAutoCompactThreshold(1000000), 967000);
  eq('reportedAutoCompactThreshold(200000) = 167000', reportedAutoCompactThreshold(200000), 167000);

  eq('below everything is ok', level(1000, 200000).level, 'ok');
  eq('warn at threshold-20000', level(187000 - 20000, 200000).level, 'warn');
  eq('compact at threshold', level(187000, 200000).level, 'compact');
  eq('blocked at window-3000', level(197000, 200000).level, 'blocked');
  // With autocompact off, s becomes the raw window, so the warn line moves UP to window-20000
  // and the compact level is unreachable. 187000 is past 180000, so it warns rather than
  // returning ok. This surprised the first draft of this test; the binary is right.
  eq('autocompact off never reaches compact', level(187000, 200000, { enabled: false }).level, 'warn');
  eq('autocompact off is ok below the moved warn line', level(179000, 200000, { enabled: false }).level, 'ok');
  eq('autocompact on warns earlier than off', level(179000, 200000, { enabled: true }).level, 'warn');
  eq('autocompact off still blocks', level(197000, 200000, { enabled: false }).level, 'blocked');
  eq('pctLeft at half of threshold', level(93500, 200000).pctLeft, 50);
  eq('pctLeft floors at 0', level(999999, 200000).pctLeft, 0);

  // These pin the caller contract that two call sites got wrong.
  eq('assess triggers at window-33000, not window-13000', assess(967000, 1000000).level, 'compact');
  eq('assess is still only warning one token below', assess(966999, 1000000).level, 'warn');
  eq('assess distance matches the reported threshold', assess(850000, 1000000).tokensUntilCompact, 117000);
  eq('assess agrees with reportedAutoCompactThreshold', assess(0, 200000).triggerThreshold, reportedAutoCompactThreshold(200000));
  eq('raw-window level() is the REFUTED formula (kept faithful on purpose)', level(970000, 1000000).level, 'warn');

  eq('opus-5 with 200k raw max is capped small', resolveWindow({ model: 'claude-opus-5', rawMax: 200000 }).window, 200000);
  eq('sonnet-5 default is 967000', resolveWindow({ model: 'claude-sonnet-5', rawMax: 1000000 }).window, 967000);
  eq('sonnet-5 on local-agent surface is 500000', resolveWindow({ model: 'claude-sonnet-5', rawMax: 1000000, surface: 'local-agent' }).window, 500000);
  eq('settings beats model default', resolveWindow({ model: 'claude-sonnet-5', rawMax: 1000000, settingsWindow: 300000 }).window, 300000);
  eq('env beats settings', resolveWindow({ model: 'claude-sonnet-5', rawMax: 1000000, envWindow: 250000, settingsWindow: 300000 }).source, 'env');
  eq('every branch clamps to raw max', resolveWindow({ model: 'claude-sonnet-5', rawMax: 200000, settingsWindow: 900000 }).window, 200000);
  eq('unknown model falls through to auto', resolveWindow({ model: 'claude-made-up', rawMax: 1000000 }).source, 'auto');

  // Mutant: a wrong buffer must make the empirical fit WORSE. If it does not, the validation
  // is not measuring anything and a green run means nothing.
  let mutantOk = false, mutantDetail = 'validation skipped (no store)';
  try {
    const real = validate({ quiet: true });
    const mutant = validate({ buffer: 12000, quiet: true });
    const f = real.better_fit;
    const realNeg = real.candidates[f].negative_overshoots;
    const mutNeg = mutant.candidates[f].negative_overshoots;
    mutantOk = mutNeg > realNeg;
    mutantDetail = `real negatives=${realNeg}, mutant(buffer=12000) negatives=${mutNeg}`;
  } catch (e) { mutantDetail = 'validation unavailable: ' + e.message; }
  checks.push(['mutant buffer degrades the fit (gate can fail)', mutantOk, mutantDetail]);

  let bad = 0;
  for (const [name, ok, detail] of checks) { if (!ok) bad++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '   [' + detail + ']'}`); }
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
const arg = (n, d) => { const i = argv.indexOf(n); return i === -1 ? d : Number(argv[i + 1]); };
let code = 0;
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) code = selfTest();
else if (argv.includes('--validate')) { validate({ buffer: arg('--buffer', K.AUTOCOMPACT_BUFFER) }); }
else if (argv.includes('--predict')) {
  const tokens = arg('--predict'), window = arg('--window', 200000), maxOut = arg('--max-output', K.MAX_OUTPUT_RESERVE);
  const enabled = !argv.includes('--no-autocompact');
  // assess() hands sZs the USABLE window. Passing the raw window here produced
  // window - 13000, the formula the 134-compaction validation refuted.
  const a = assess(tokens, window, { maxOutputTokens: maxOut, enabled });
  console.log(JSON.stringify({
    tokens, window, maxOutputTokens: maxOut, autocompact: enabled,
    reported_threshold: a.triggerThreshold,
    trigger_threshold: a.triggerThreshold,
    usable_window: a.usableWindow,
    warn_at: a.warnAt,
    blocked_at: a.blockedAt,
    level: a.level, pctLeft: a.pctLeft,
    tokens_until_compact: a.tokensUntilCompact,
  }, null, 2));
} else {
  console.log('usage: mirror.mjs --self-test | --validate [--buffer N] | --predict <tokens> --window <n>');
  code = 2;
}
if (IS_ENTRY) process.exit(code);

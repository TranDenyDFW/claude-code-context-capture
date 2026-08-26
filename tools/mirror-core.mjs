#!/usr/bin/env node
// mirror-core.mjs - the pure context-window math, transcribed from Claude Code 2.1.229.
// No dependencies at all, so latency-sensitive callers (the status line, hooks) can import it
// without paying for a SQLite load. The CLI, validation and store access live in mirror.mjs.
//
//   sZs  offset 280112431   the level function (ok / warn / compact / blocked)
//   M5o  offset 280112204   the compact threshold, window - 13000
//   hG   offset 280114317   window resolution with its precedence chain
//   Ihe  offset 280115401   window - min(maxOutputTokens, 20000)
//   a6d  offset 280117184   the per-model default table
//   consts offset 280112755 and 280117024

// ---------------------------------------------------------------------------
// Constants, verbatim from the binary.
// ---------------------------------------------------------------------------
export const K = {
  AUTOCOMPACT_BUFFER: 13000,   // n6d, labeled "Autocompact buffer" in the UI
  COMPACT_BUFFER: 3000,        // o6d, labeled "Compact buffer"
  WARN_OFFSET: 20000,          // the literal in sZs: a = s - 20000
  MAX_OUTPUT_RESERVE: 20000,   // d6d, the cap in Ihe's min(maxOutputTokens, 20000)
  SMALL_WINDOW: 200000,        // yfe
  RAW_FALLBACK: 200000,        // Ybr, the raw max when nothing else applies
  MIN_WINDOW: 100000,          // L5o
  MAX_WINDOW: 1000000,         // cZs
  PRECOMPUTE_FRACTION: 0.2,    // D5o
};

// Rlb: forced to SMALL_WINDOW whenever the raw max is under 1M.
export const SMALL_WINDOW_MODELS = new Set([
  'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-opus-4-8', 'claude-opus-5',
]);

// a6d: the entire per-model default table in this build. One key.
export const MODEL_DEFAULT_WINDOW = {
  'claude-sonnet-5': { default: 967000, surfaces: { remote_cowork: 500000, 'local-agent': 500000 } },
};

// ---------------------------------------------------------------------------
// The math.
// ---------------------------------------------------------------------------

// M5o: the compact threshold. The test-percentage override is included for fidelity even
// though it is a test-only hook, because omitting it would make this a paraphrase.
export function compactThreshold(rawWindow, { testPctOverride } = {}) {
  const r = rawWindow - K.AUTOCOMPACT_BUFFER;
  if (testPctOverride !== undefined && !isNaN(testPctOverride) && testPctOverride > 0 && testPctOverride <= 100) {
    return Math.min(Math.floor(rawWindow * (testPctOverride / 100)), r);
  }
  return r;
}

// sZs: which level the session is at. `tokens` is the resident token count.
export function level(tokens, rawWindow, { enabled = true, testPctOverride, testBlockingOverride } = {}, blockingBase = rawWindow, precomputed) {
  const i = precomputed ?? compactThreshold(rawWindow, { testPctOverride });
  const s = enabled ? i : rawWindow;
  const a = s - K.WARN_OFFSET;
  const c = (testBlockingOverride !== undefined && !isNaN(testBlockingOverride) && testBlockingOverride > 0)
    ? testBlockingOverride : blockingBase - K.COMPACT_BUFFER;
  const pctLeft = Math.max(0, Math.round((s - tokens) / s * 100));
  if (tokens >= c) return { level: 'blocked', pctLeft };
  if (enabled && tokens >= i) return { level: 'compact', pctLeft };
  if (tokens >= a) return { level: 'warn', pctLeft };
  return { level: 'ok', pctLeft };
}

// Ihe: the window minus room for the model to finish a full response.
export function usableWindow(window, maxOutputTokens) {
  return window - Math.min(maxOutputTokens, K.MAX_OUTPUT_RESERVE);
}

// The figure the analyzer reports as autoCompactThreshold: Ihe(...) - 13000.
// Note the 13000 is subtracted here IN ADDITION to the one inside M5o on the trigger path.
// That asymmetry is in the shipped code and is reproduced, not corrected.
export function reportedAutoCompactThreshold(window, maxOutputTokens = K.MAX_OUTPUT_RESERVE) {
  return usableWindow(window, maxOutputTokens) - K.AUTOCOMPACT_BUFFER;
}

// hG: window resolution. Every branch is clamped by the raw model ceiling.
export function resolveWindow({ model, rawMax, envWindow, settingsWindow, clientDataWindow, experimentWindow, surface }) {
  const clamp = (v) => Math.min(rawMax, v);
  if (envWindow !== undefined) return { window: clamp(Math.max(K.MIN_WINDOW, envWindow)), configured: envWindow, source: 'env' };
  if (settingsWindow !== undefined) return { window: clamp(settingsWindow), configured: settingsWindow, source: 'settings' };
  if (clientDataWindow !== undefined) return { window: clamp(clientDataWindow), configured: clientDataWindow, source: 'clientdata' };
  if (experimentWindow !== undefined) return { window: clamp(experimentWindow), configured: experimentWindow, source: 'experiment' };
  if (rawMax < K.MAX_WINDOW && SMALL_WINDOW_MODELS.has(model)) {
    return { window: clamp(K.SMALL_WINDOW), configured: K.SMALL_WINDOW, source: 'model-default' };
  }
  const entry = MODEL_DEFAULT_WINDOW[model];
  if (entry) {
    const v = (surface && entry.surfaces[surface] !== undefined) ? entry.surfaces[surface] : entry.default;
    return { window: clamp(v), configured: v, source: 'model-default' };
  }
  return { window: rawMax, configured: rawMax, source: 'auto' };
}


// ---------------------------------------------------------------------------
// The CORRECT caller for level().
//
// `level` is a faithful transcription of sZs, and sZs derives its compact threshold as
// `t - 13000` from whatever `t` it is handed. Handing it the RAW window yields window - 13000,
// which is the formula the 134-compaction validation REFUTED. The shipped code hands it the
// usable window (Ihe = window - min(maxOutputTokens, 20000)), which yields window - 33000, the
// formula the data supports and the same figure the analyzer reports as autoCompactThreshold.
//
// Two call sites got this wrong by passing the raw window (mirror.mjs --predict and the status
// line), so the decision now lives in one function instead of at each call site.
export function assess(tokens, window, { maxOutputTokens = K.MAX_OUTPUT_RESERVE, enabled = true } = {}) {
  const usable = usableWindow(window, maxOutputTokens);
  const trigger = usable - K.AUTOCOMPACT_BUFFER;      // === reportedAutoCompactThreshold(window)
  const r = level(tokens, usable, { enabled }, window, trigger);
  return {
    ...r,
    window,
    usableWindow: usable,
    triggerThreshold: trigger,
    blockedAt: window - K.COMPACT_BUFFER,
    warnAt: (enabled ? trigger : usable) - K.WARN_OFFSET,
    tokensUntilCompact: Math.max(0, trigger - tokens),
  };
}

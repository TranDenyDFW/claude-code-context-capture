#!/usr/bin/env node
// outcomes.mjs - what happened to a tool call, and how to say it.
//
// ONE FLAG MEANT TWO OPPOSITE THINGS. Claude Code sets `is_error` on a tool that RAN AND FAILED and
// on a tool that NEVER RAN because something refused it. Measured across every transcript on this
// machine, 7,941 files and 11.1 GB, joined to the store: of 6,688 flagged calls, 1,828 (27.3%) were
// refusals. Per tool it is worse, because refusal is not evenly spread: the ExitPlanMode row read
// 39 errors of which 36 were a person rejecting a plan and 3 were real.
//
// THE SIGNAL IS EXACT, NOT A GUESS. Claude Code writes `toolDenialKind` at the TOP LEVEL of the
// record, a sibling of `message`, never inside the content block. Across all transcripts 2,046
// records carry it and 100% of those are genuine refusals, with zero false positives. Text matching
// was the obvious alternative and it is brittle in both directions: roughly 250 refusals are
// arbitrary strings a hook author chose ("infra is protected", "filtered", "slow deny"), which no
// pattern can enumerate, and a loose matcher flags genuine Bash failures whose stdout quotes the
// word BLOCKED.
//
// This module is the one definition of that vocabulary, shared by the writer (harvest.mjs), the
// readers (waste.mjs, and c4x/store.py which mirrors it) and the fixture builder. A second copy
// would be a second answer.
//
//   node tools/outcomes.mjs --self-test

import { pathToFileURL } from 'node:url';

/**
 * What a tool call turned out to be. A closed set: anything outside it is a bug, not a new case.
 *
 * `unclassified` is the honest fifth answer and the reason this is not a boolean. A build older
 * than 2.1.202 recorded no reason at all, and its refusal records are identical to its failure
 * records down to the key set, so a flagged call from that era is genuinely unknowable. Calling it
 * an error would be the same defect this module exists to remove, twenty times smaller.
 */
export const TOOL_OUTCOME = {
  OK: 'ok',
  ERROR: 'error',
  REFUSED: 'refused',
  UNCLASSIFIED: 'unclassified',
};

/** The Claude Code build that began writing `toolDenialKind`. Before this, silence means nothing. */
export const DENIAL_KIND_SINCE = '2.1.202';

/**
 * Is `version` at least `floor`, compared NUMERICALLY, component by component?
 *
 * String comparison is the trap and it fails in the direction that matters: '2.1.99' >= '2.1.202'
 * is TRUE as strings, so every pre-era row would silently be classified as a genuine error, which
 * is exactly the wrong answer arrived at confidently. An unparseable or absent version returns
 * false, so a record that cannot prove it is modern is treated as old.
 */
export function atLeastVersion(version, floor = DENIAL_KIND_SINCE) {
  const parts = (value) => String(value ?? '').trim().split('.').map((p) => parseInt(p, 10));
  const have = parts(version);
  const want = parts(floor);
  if (!have.length || have.some((n) => !Number.isFinite(n))) return false;
  for (let i = 0; i < Math.max(have.length, want.length); i++) {
    const a = Number.isFinite(have[i]) ? have[i] : 0;
    const b = Number.isFinite(want[i]) ? want[i] : 0;
    if (a !== b) return a > b;
  }
  return true;
}

/**
 * The outcome of one tool_result, from the record that carried it.
 *
 * Order matters. A record that SAYS it was denied was denied, whatever else is true of it, so the
 * denial kind is read first. Only then does the flag decide, and only for a build that would have
 * told us had it been a refusal.
 */
export function classifyResult({ isError, denialKind, version } = {}) {
  if (typeof denialKind === 'string' && denialKind) return TOOL_OUTCOME.REFUSED;
  if (!isError) return TOOL_OUTCOME.OK;
  return atLeastVersion(version) ? TOOL_OUTCOME.ERROR : TOOL_OUTCOME.UNCLASSIFIED;
}

/**
 * What the merged column says, and NOTHING when there is nothing to say.
 *
 * A zero is noise: on a table of forty tools most rows have no failures at all, and forty cells
 * reading "0 errors" is forty cells of nothing dressed as a measurement.
 *
 * THE WORDS ARE LONG ON PURPOSE. tools/table_audit.py flags a cell matching its stringified-number
 * pattern, a number followed by up to four letters, so "3 err" would be reported as a number stored
 * as text while "3 errors" is not. And the bare word "unknown" is one of that audit's placeholder
 * strings, so the count always leads: "2 unknown", never "unknown".
 */
export function outcomeText({ errors = 0, refused = 0, unknown = 0 } = {}) {
  const parts = [];
  const say = (n, one, many) => { if (n > 0) parts.push(`${n.toLocaleString()} ${n === 1 ? one : many}`); };
  say(Number(errors) || 0, 'error', 'errors');
  say(Number(refused) || 0, 'refused', 'refused');
  say(Number(unknown) || 0, 'unknown', 'unknown');
  return parts.join(', ');
}

// ------------------------------------------------------------------ self-test

export function selfTest() {
  const checks = [];
  const add = (what, ok, detail = '') => checks.push([what, ok, detail]);

  // B1 to B3, the rendering.
  add('the merged column names both kinds',
    outcomeText({ errors: 3, refused: 36 }) === '3 errors, 36 refused',
    outcomeText({ errors: 3, refused: 36 }));
  // THE ONE THE WHOLE COLUMN EXISTS FOR. A table of forty tools mostly has nothing to report.
  add('nothing to report renders NOTHING, not a zero (gate can fail)',
    outcomeText({ errors: 0, refused: 0, unknown: 0 }) === '' && outcomeText() === '',
    JSON.stringify(outcomeText({})));
  add('one is singular, and only the one that is one',
    outcomeText({ errors: 1 }) === '1 error'
      && outcomeText({ errors: 1, refused: 1 }) === '1 error, 1 refused',
    outcomeText({ errors: 1, refused: 1 }));
  add('a count always leads the word unknown',
    outcomeText({ unknown: 2 }) === '2 unknown', outcomeText({ unknown: 2 }));

  // B4. The audit's own pattern, copied from tools/table_audit.py, so a reworded cell that would
  // fail that audit fails HERE first, where the fix is one word.
  const NUMERIC_LOOKING = /^[-+$]?[\d,]+(\.\d+)?(e[-+]?\d+)?\s*[A-Za-z%$/]{0,4}$/i;
  const PLACEHOLDER = new Set(['unknown', 'n/a', 'none', '-']);
  const rendered = [
    outcomeText({ errors: 3, refused: 36 }), outcomeText({ errors: 1 }),
    outcomeText({ refused: 9, unknown: 3 }), outcomeText({ unknown: 2 }),
  ];
  add('no rendered value reads as a number stored as text (gate can fail)',
    rendered.every((t) => !NUMERIC_LOOKING.test(t)),
    rendered.filter((t) => NUMERIC_LOOKING.test(t)).join(' | '));
  add('and none is a bare placeholder word',
    rendered.every((t) => !PLACEHOLDER.has(t.trim().toLowerCase())), rendered.join(' | '));

  // A9's twin, checked here because this is where the comparison lives. String comparison says
  // '2.1.99' >= '2.1.202', which would silently make every pre-era row a genuine error.
  add('version compare is numeric, not lexical (gate can fail)',
    atLeastVersion('2.1.99') === false && ('2.1.99' >= '2.1.202') === true,
    `atLeast=${atLeastVersion('2.1.99')}`);
  add('the era boundary itself counts as modern',
    atLeastVersion('2.1.202') === true && atLeastVersion('2.1.201') === false);
  add('a longer or shorter version string still compares',
    atLeastVersion('2.2') === true && atLeastVersion('2.1.202.1') === true
      && atLeastVersion('3.0.0') === true);
  add('a version that cannot be read is treated as OLD, never as modern',
    atLeastVersion(undefined) === false && atLeastVersion('') === false
      && atLeastVersion('dev') === false);

  // The classifier.
  add('a record that says it was denied is refused, whatever else is true',
    classifyResult({ isError: true, denialKind: 'permission-rule', version: '2.1.219' })
      === TOOL_OUTCOME.REFUSED);
  add('a flagged call on a modern build is a genuine failure',
    classifyResult({ isError: true, denialKind: null, version: '2.1.233' })
      === TOOL_OUTCOME.ERROR);
  // The honest fifth answer. That build recorded no reason, so this store cannot say.
  add('a flagged call on a build that could not tell us is unclassified (gate can fail)',
    classifyResult({ isError: true, denialKind: null, version: '2.1.121' })
      === TOOL_OUTCOME.UNCLASSIFIED,
    classifyResult({ isError: true, denialKind: null, version: '2.1.121' }));
  add('a call that was not flagged is positively ok, on any build',
    classifyResult({ isError: false, version: '2.1.121' }) === TOOL_OUTCOME.OK
      && classifyResult({ isError: false, version: '2.1.233' }) === TOOL_OUTCOME.OK);
  // The vocabulary belongs to Claude Code, not to this file.
  add('an unrecognised denial kind still means refused (gate can fail)',
    classifyResult({ isError: true, denialKind: 'some-future-kind', version: '2.1.233' })
      === TOOL_OUTCOME.REFUSED);
  add('an empty denial kind is not a denial',
    classifyResult({ isError: true, denialKind: '', version: '2.1.233' }) === TOOL_OUTCOME.ERROR);

  let bad = 0;
  for (const [what, ok, detail] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}${ok ? '' : `   [${detail}]`}`);
  }
  console.log(bad ? `SELF-TEST FAIL (${bad}/${checks.length} failed)`
    : `SELF-TEST PASS (${checks.length} checks)`);
  return bad ? 1 : 0;
}

const IS_ENTRY = (() => {
  try {
    return process.argv[1] ? pathToFileURL(process.argv[1]).href === import.meta.url : false;
  } catch { return false; }
})();
if (IS_ENTRY && process.argv.includes('--self-test')) process.exit(selfTest());

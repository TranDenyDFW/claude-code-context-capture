#!/usr/bin/env node
// install.mjs - owns this extension's wiring in ~/.claude/settings.json.
//
// Why this exists: the wiring used to be written by hand, and hand-written wiring was wrong for
// two days without anything saying so. Each hook event was a single object where Claude Code
// requires an ARRAY of matcher groups, so the file was valid JSON, pointed at real scripts, and
// fired nothing. Nothing in the repo could have told you that. This tool can.
//
// It converges rather than scripts: currentState -> desiredState -> apply the difference. That is
// what makes `install` safe to run twice, safe to run over a broken config, and identical in
// effect to `install --rewire` when only the settings drifted.
//
//   node install.mjs status                 read-only audit; exit 0 healthy, 1 drifted, 2 misuse
//   node install.mjs install [--rewire]     converge. --rewire touches settings only
//   node install.mjs install --dry-run      print the diff, write nothing
//   node install.mjs install --adopt <dir>  carry an older install's store forward first
//   node install.mjs uninstall [--purge]    remove only our entries; --purge also drops the store
//   node install.mjs reset --data|--settings|--all
//   node install.mjs --self-test
//
// It never prompts. An installer that blocks on stdin cannot run where this one has to: the
// desktop app spawns the CLI with piped stdio and no TTY, and SessionStart fires before any
// prompt exists. --dry-run is the review path instead.

import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync, copyFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { rootFrom, defaultDb } from './paths.mjs';

const ROOT = rootFrom(import.meta.url);
const SETTINGS = join(homedir(), '.claude', 'settings.json');
const RECEIPT = join(ROOT, 'data', 'install-receipt.json');
const RECEIPT_VERSION = 1;

// Settings hold forward-slash paths even on Windows: backslashes would need escaping inside a
// JSON string and inside the shell word, and one of the two always gets forgotten.
const posix = (p) => String(p).replace(/\\/g, '/');

// UserPromptSubmit is deliberately matcher-less: the event does not support a matcher at all, and
// supplying one is the kind of silently-ignored key this tool exists to catch.
const WIRING = [
  { event: 'SessionStart', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'SessionEnd', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'UserPromptSubmit', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'PostToolUse', script: 'hooks/event-hook.mjs', timeout: 10, matcher: '*' },
  { event: 'SubagentStop', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'PreCompact', script: 'hooks/compact-hook.mjs', timeout: 120, matcher: null },
  { event: 'PostCompact', script: 'hooks/compact-hook.mjs', timeout: 120, matcher: null },
];
const STATUSLINE_SCRIPT = 'tools/statusline.mjs';

export const cmdFor = (root, script) => `node "${posix(join(root, script))}"`;
export const ownsCommand = (cmd, root) => typeof cmd === 'string' && cmd.includes(posix(root));

// ---------------------------------------------------------------- pure state transforms

// A hook event maps to an array of matcher groups. Anything else is the defect this tool was
// written for, so it is normalised rather than trusted, and the normalisation is reported.
function asGroups(v) {
  if (Array.isArray(v)) return { groups: v, wasMalformed: false };
  if (v && typeof v === 'object') return { groups: [v], wasMalformed: true };
  return { groups: [], wasMalformed: false };
}

function desiredGroup({ script, timeout, matcher }, root) {
  const g = { hooks: [{ type: 'command', command: cmdFor(root, script), timeout }] };
  return matcher === null ? g : { matcher, ...g };
}

export function applyWiring(settings, root) {
  const next = structuredClone(settings ?? {});
  const changes = [];
  if (!next.hooks || typeof next.hooks !== 'object' || Array.isArray(next.hooks)) next.hooks = {};

  for (const w of WIRING) {
    const { groups, wasMalformed } = asGroups(next.hooks[w.event]);
    if (wasMalformed) changes.push(`repair ${w.event}: object -> array of matcher groups`);
    const want = desiredGroup(w, root);
    const wantJson = JSON.stringify(want);
    const isOurs = (h) => ownsCommand(h?.command, root) && String(h.command).endsWith(`${w.script}"`);

    // Never rewrite a group wholesale. A matcher group can carry another tool's hook alongside
    // ours, and replacing the group would delete it - the same "only touch what is yours" rule
    // that uninstall obeys. So our hook is extracted from wherever it sits and re-added in a
    // group of our own, and every foreign hook is left exactly where it was.
    let found = false, displaced = false, canonical = false;
    const kept = [];
    for (const g of groups) {
      if (!Array.isArray(g?.hooks)) { kept.push(g); continue; }
      const others = g.hooks.filter((h) => !isOurs(h));
      if (others.length === g.hooks.length) { kept.push(g); continue; }
      found = true;
      if (others.length === 0 && JSON.stringify(g) === wantJson) {
        if (canonical) changes.push(`remove a duplicate ${w.event} group`);
        else { kept.push(g); canonical = true; }
        continue;
      }
      if (others.length) { kept.push({ ...g, hooks: others }); displaced = true; }
    }

    if (!canonical) {
      kept.push(want);
      changes.push(!found ? `add ${w.event} -> ${w.script}`
        : displaced ? `correct ${w.event} (move our hook into its own group, leaving the other tool's alone)`
          : `correct ${w.event} (matcher/timeout/command drift)`);
    }
    next.hooks[w.event] = kept;
  }

  const wantLine = { type: 'command', command: cmdFor(root, STATUSLINE_SCRIPT) };
  const cur = next.statusLine;
  if (JSON.stringify(cur) !== JSON.stringify(wantLine)) {
    next.statusLine = wantLine;
    changes.push(ownsCommand(cur?.command, root) ? 'correct statusLine' : 'set statusLine');
  }
  return { next, changes };
}

// Removes only what belongs to this install. A group that also carries someone else's hook keeps
// that hook and survives; a key that empties out is deleted rather than left as {} or [].
export function removeWiring(settings, root, receipt = null) {
  const next = structuredClone(settings ?? {});
  const changes = [];

  if (next.hooks && typeof next.hooks === 'object' && !Array.isArray(next.hooks)) {
    for (const event of Object.keys(next.hooks)) {
      const { groups } = asGroups(next.hooks[event]);
      const kept = [];
      for (const g of groups) {
        if (!Array.isArray(g?.hooks)) { kept.push(g); continue; }
        const survivors = g.hooks.filter((h) => !ownsCommand(h?.command, root));
        if (survivors.length === g.hooks.length) { kept.push(g); continue; }
        const n = g.hooks.length - survivors.length;
        changes.push(`remove ${n} hook${n === 1 ? '' : 's'} from ${event}`);
        if (survivors.length) kept.push({ ...g, hooks: survivors });
      }
      if (kept.length) next.hooks[event] = kept;
      else delete next.hooks[event];
    }
    if (!Object.keys(next.hooks).length) delete next.hooks;
  }

  if (ownsCommand(next.statusLine?.command, root)) {
    const prior = receipt && 'priorStatusLine' in receipt ? receipt.priorStatusLine : null;
    if (prior) { next.statusLine = prior; changes.push('restore the statusLine that was here before'); }
    else { delete next.statusLine; changes.push('remove statusLine'); }
  }
  return { next, changes };
}

// Findings are what `status` reports. Each one names a way the config can be present and dead.
export function audit(settings, root, { rootExists = true } = {}) {
  const findings = [];
  const hooks = settings?.hooks;

  for (const w of WIRING) {
    const raw = hooks?.[w.event];
    if (raw === undefined) { findings.push({ level: 'error', event: w.event, why: 'not wired' }); continue; }
    if (!Array.isArray(raw)) {
      findings.push({ level: 'error', event: w.event, why: 'mapped to an object; Claude Code requires an array of matcher groups, so this never fires' });
      continue;
    }
    const { groups } = asGroups(raw);
    const mine = groups.filter((g) => Array.isArray(g?.hooks) && g.hooks.some((h) => ownsCommand(h?.command, root)));
    if (!mine.length) { findings.push({ level: 'error', event: w.event, why: 'no hook of ours is registered' }); continue; }
    if (w.matcher === null && mine.some((g) => 'matcher' in g) && w.event === 'UserPromptSubmit') {
      findings.push({ level: 'warn', event: w.event, why: 'carries a matcher, which this event does not support' });
    }
  }

  if (!rootExists) {
    findings.push({ level: 'error', event: '*', why: `settings point at ${posix(root)}, which does not exist; every session fires a hook that errors` });
  }
  const sl = settings?.statusLine;
  if (!sl) findings.push({ level: 'warn', event: 'statusLine', why: 'not set' });
  else if (!ownsCommand(sl.command, root)) findings.push({ level: 'info', event: 'statusLine', why: 'set to something else; install would replace it and record the old value' });

  return findings;
}

// ---------------------------------------------------------------- io

const readJson = (p, dflt = null) => { try { return JSON.parse(readFileSync(p, 'utf8').replace(/^\uFEFF/, '')); } catch { return dflt; } };

// Claude Code writes this file too. Re-read immediately before serialising, then temp+rename so a
// concurrent reader never observes a half-written file.
function writeJsonAtomic(p, obj) {
  mkdirSync(join(p, '..'), { recursive: true });
  const tmp = `${p}.tmp-${process.pid}`;
  writeFileSync(tmp, `${JSON.stringify(obj, null, 2)}\n`, 'utf8');
  renameSync(tmp, p);
}

function backupSettings() {
  if (!existsSync(SETTINGS)) return null;
  const dest = `${SETTINGS}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`;
  copyFileSync(SETTINGS, dest);
  return dest;
}

// A SQLite store is three files, not one. Moving context.db while leaving context.db-wal behind
// strands a write-ahead log next to whatever database appears at that path next, and SQLite will
// try to replay it. The sidecars travel with the file or they go with it.
export const sidecars = (db) => [db, `${db}-wal`, `${db}-shm`];

function loadReceipt() { return readJson(RECEIPT, null); }

function saveReceipt(extra) {
  mkdirSync(join(ROOT, 'data'), { recursive: true });
  const prior = loadReceipt();
  writeJsonAtomic(RECEIPT, {
    tool: 'install.mjs', version: RECEIPT_VERSION, root: posix(ROOT), settings: posix(SETTINGS),
    installedAt: new Date().toISOString(),
    priorStatusLine: prior?.priorStatusLine ?? null,
    wiring: WIRING.map((w) => ({ ...w, command: cmdFor(ROOT, w.script) })),
    ...extra,
  });
}

// ---------------------------------------------------------------- commands

function cmdStatus() {
  const settings = readJson(SETTINGS, {});
  const findings = audit(settings, ROOT, { rootExists: existsSync(ROOT) });
  const db = defaultDb(ROOT);
  const receipt = loadReceipt();

  console.log(`install root : ${posix(ROOT)}`);
  console.log(`settings     : ${posix(SETTINGS)}${existsSync(SETTINGS) ? '' : '  (missing)'}`);
  console.log(`store        : ${posix(db)}${existsSync(db) ? '' : '  (not created yet)'}`);
  console.log(`receipt      : ${receipt ? `written ${receipt.installedAt}` : 'none - this install was not made by install.mjs'}`);
  for (const f of findings) console.log(`${f.level.toUpperCase().padEnd(5)} ${String(f.event).padEnd(16)} ${f.why}`);
  const errors = findings.filter((f) => f.level === 'error').length;
  console.log(errors ? `DRIFTED (${errors} error${errors === 1 ? '' : 's'})` : 'HEALTHY');
  return errors ? 1 : 0;
}

function cmdInstall(argv) {
  const dry = argv.includes('--dry-run');
  const rewire = argv.includes('--rewire');

  const adoptAt = argv.indexOf('--adopt');
  let adopted = null;
  if (adoptAt !== -1) {
    const from = argv[adoptAt + 1];
    if (!from || from.startsWith('--')) { console.error('--adopt needs a directory'); return 2; }
    const src = join(from, 'data', 'context.db');
    const dest = defaultDb(ROOT);
    if (!existsSync(src)) { console.error(`--adopt: no store at ${posix(src)}`); return 2; }
    if (existsSync(dest)) console.log(`adopt skipped: ${posix(dest)} already exists`);
    else if (dry) adopted = `would copy ${posix(src)} -> ${posix(dest)}`;
    else { mkdirSync(join(ROOT, 'data'), { recursive: true }); copyFileSync(src, dest); adopted = `copied ${posix(src)} -> ${posix(dest)}`; }
  }

  const settings = readJson(SETTINGS, {});
  const { next, changes } = applyWiring(settings, ROOT);

  if (!changes.length && !adopted) { console.log('no changes: already converged'); return 0; }
  for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);
  if (adopted) console.log(adopted);
  if (dry) { console.log('--dry-run: nothing written'); return 0; }

  const priorStatusLine = ownsCommand(settings?.statusLine?.command, ROOT) ? undefined : (settings?.statusLine ?? null);
  const backup = backupSettings();
  writeJsonAtomic(SETTINGS, next);
  if (!rewire) mkdirSync(join(ROOT, 'data', 'raw'), { recursive: true });
  saveReceipt({ settingsBackup: backup ? posix(backup) : null, ...(priorStatusLine === undefined ? {} : { priorStatusLine }) });
  console.log(`wrote ${posix(SETTINGS)}${backup ? ` (backup: ${posix(backup)})` : ''}`);
  console.log('hooks apply to sessions started from now on.');
  return 0;
}

function cmdUninstall(argv) {
  const dry = argv.includes('--dry-run');
  const purge = argv.includes('--purge');
  const settings = readJson(SETTINGS, {});
  const { next, changes } = removeWiring(settings, ROOT, loadReceipt());

  if (!changes.length) console.log('nothing of ours found in settings');
  for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);

  const db = defaultDb(ROOT);
  if (purge && existsSync(db)) console.log(`${dry ? 'would ' : ''}delete ${posix(db)}`);
  else if (existsSync(db)) console.log(`store kept at ${posix(db)} - pass --purge to delete it`);

  if (dry) { console.log('--dry-run: nothing written'); return 0; }
  if (changes.length) { backupSettings(); writeJsonAtomic(SETTINGS, next); }
  if (purge) { for (const f of sidecars(db)) rmSync(f, { force: true }); rmSync(join(ROOT, 'data', 'raw'), { recursive: true, force: true }); }
  rmSync(RECEIPT, { force: true });
  return 0;
}

function cmdReset(argv) {
  const dry = argv.includes('--dry-run');
  const all = argv.includes('--all');
  const wantData = all || argv.includes('--data');
  const wantSettings = all || argv.includes('--settings');
  if (!wantData && !wantSettings) { console.error('reset needs --data, --settings, or --all'); return 2; }

  if (wantData) {
    const db = defaultDb(ROOT);
    if (existsSync(db)) {
      const bak = `${db}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`;
      console.log(`${dry ? 'would ' : ''}move ${posix(db)} -> ${posix(bak)}`);
      // Suffix order matters: context.db -> X means context.db-wal must become X-wal, which is
      // where SQLite looks for it, so the backup stays a usable database.
      for (const [i, f] of sidecars(db).entries()) {
        if (i === 0 || !existsSync(f)) continue;
        console.log(`${dry ? 'would ' : ''}move ${posix(f)} alongside it`);
        if (!dry) renameSync(f, `${bak}${f.slice(db.length)}`);
      }
      if (!dry) renameSync(db, bak);
    } else console.log('store already absent');
    const raw = join(ROOT, 'data', 'raw');
    if (existsSync(raw)) { console.log(`${dry ? 'would ' : ''}clear ${posix(raw)}`); if (!dry) { rmSync(raw, { recursive: true, force: true }); mkdirSync(raw, { recursive: true }); } }
  }

  if (wantSettings) {
    const settings = readJson(SETTINGS, {});
    const cleared = removeWiring(settings, ROOT, loadReceipt()).next;
    const { next, changes } = applyWiring(cleared, ROOT);
    for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);
    if (!dry) { backupSettings(); writeJsonAtomic(SETTINGS, next); saveReceipt({}); }
  }
  if (dry) console.log('--dry-run: nothing written');
  return 0;
}

// ---------------------------------------------------------------- self-test

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);
  const R = 'X:/fake/root';

  // The exact shape that was live for two days and fired nothing.
  const malformed = { hooks: Object.fromEntries(WIRING.map((w) => [w.event, { matcher: '*', hooks: [{ type: 'command', command: cmdFor(R, w.script), timeout: w.timeout }] }])) };
  const good = applyWiring({}, R).next;

  add('a clean settings file gets every event wired', WIRING.every((w) => Array.isArray(good.hooks[w.event]) && good.hooks[w.event].length === 1));
  add('every event maps to an ARRAY, never a bare object', Object.values(good.hooks).every(Array.isArray));
  add('UserPromptSubmit carries no matcher', !('matcher' in good.hooks.UserPromptSubmit[0]));
  add('PostToolUse carries the wildcard matcher', good.hooks.PostToolUse[0].matcher === '*');
  add('statusLine is set', good.statusLine.command.includes(STATUSLINE_SCRIPT));
  add('commands use forward slashes only', !JSON.stringify(good).includes('\\\\'));

  const twice = applyWiring(good, R);
  add('applying twice changes nothing (idempotent)', twice.changes.length === 0, JSON.stringify(twice.changes));
  add('applying twice adds no duplicate group', twice.next.hooks.PostToolUse.length === 1);

  const repaired = applyWiring(malformed, R);
  add('the malformed object shape is detected and repaired', repaired.changes.some((c) => c.includes('object -> array')));
  add('repair yields arrays', Object.values(repaired.next.hooks).every(Array.isArray));

  // A foreign hook sharing OUR matcher group. This is the case that a wholesale group rewrite
  // silently destroyed: permissions survived, the other tool's hook did not.
  const FOREIGN = { type: 'command', command: 'node "D:/other-tool/watch.mjs"' };
  const shared = structuredClone(good);
  shared.hooks.PostToolUse[0].hooks.push(structuredClone(FOREIGN));
  const reapplied = applyWiring(shared, R);
  const flatCmds = JSON.stringify(reapplied.next.hooks.PostToolUse);
  add('install keeps a foreign hook that shares our matcher group', flatCmds.includes('other-tool/watch.mjs'), flatCmds);
  add('install still registers our own hook after that split', flatCmds.includes('event-hook.mjs'));
  add('the split leaves our hook in a group of its own',
    reapplied.next.hooks.PostToolUse.some((g) => g.hooks.length === 1 && ownsCommand(g.hooks[0].command, R)));
  add('re-applying the split result is idempotent', applyWiring(reapplied.next, R).changes.length === 0,
    JSON.stringify(applyWiring(reapplied.next, R).changes));

  // A duplicate of our own group must collapse rather than accumulate.
  const dupe = structuredClone(good);
  dupe.hooks.PostToolUse.push(structuredClone(good.hooks.PostToolUse[0]));
  add('a duplicate of our group is collapsed', applyWiring(dupe, R).next.hooks.PostToolUse.length === 1);

  // Foreign entries must survive both directions.
  const foreignHook = { hooks: [{ type: 'command', command: 'node "D:/someone-else/thing.mjs"' }] };
  const mixed = structuredClone(good);
  mixed.hooks.PostToolUse.push(foreignHook);
  mixed.permissions = { allow: ['Bash'] };
  const removed = removeWiring(mixed, R, null);
  add("uninstall keeps another tool's hook", removed.next.hooks.PostToolUse.length === 1 && removed.next.hooks.PostToolUse[0].hooks[0].command.includes('someone-else'));
  add('uninstall leaves unrelated settings keys alone', JSON.stringify(removed.next.permissions) === '{"allow":["Bash"]}');
  add('uninstall drops events that held only our hooks', removed.next.hooks.SessionStart === undefined);

  const priorLine = { type: 'command', command: 'node "D:/other/line.mjs"' };
  const restored = removeWiring(good, R, { priorStatusLine: priorLine });
  add('uninstall restores the statusLine that was there before', JSON.stringify(restored.next.statusLine) === JSON.stringify(priorLine));
  add('uninstall with no prior statusLine deletes the key', removeWiring(good, R, null).next.statusLine === undefined);

  const roundTrip = applyWiring(removeWiring(good, R, null).next, R).next;
  add('uninstall then install returns the identical file', JSON.stringify(roundTrip) === JSON.stringify(good));

  add('a healthy config audits clean', audit(good, R).filter((f) => f.level === 'error').length === 0);
  const badFindings = audit(malformed, R);
  add('the malformed config audits as an error', badFindings.some((f) => f.level === 'error' && f.why.includes('never fires')));
  add('a missing install root is reported', audit(good, R, { rootExists: false }).some((f) => f.why.includes('does not exist')));
  add('an empty settings file audits as errors, not silence', audit({}, R).filter((f) => f.level === 'error').length === WIRING.length);

  // A store is db + -wal + -shm. Losing the second two behind a move is how a stale write-ahead
  // log ends up next to a fresh database.
  const s = sidecars('/x/data/context.db');
  add('a store is treated as three files, not one', s.length === 3 && s[1].endsWith('context.db-wal') && s[2].endsWith('context.db-shm'), s.join(','));
  add('the sidecar suffixes append to the backup name so the pair stays valid',
    `${'/x/data/context.db.bak-T'}${s[1].slice('/x/data/context.db'.length)}` === '/x/data/context.db.bak-T-wal');

  // Falsification gate: the audit must be capable of failing, or "audits clean" proves nothing.
  const realErrors = audit(good, R).filter((f) => f.level === 'error').length;
  const mutantErrors = audit(good, 'Y:/different/root').filter((f) => f.level === 'error').length;
  add('pointing the audit at a different root makes it FAIL (gate can fail)', mutantErrors > realErrors, `real=${realErrors} mutant=${mutantErrors}`);

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : `  [${d}]`}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// ---------------------------------------------------------------- entry

// Only dispatch the CLI when this file IS the entry point, so importing the exports never runs
// the command. probe.mjs records two earlier victims of the missing guard.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

const argv = process.argv.slice(2);
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) process.exit(selfTest());
else {
const verb = argv.find((a) => !a.startsWith('--'));
if (argv.includes('--help') || !verb) {
  console.log(readFileSync(new URL(import.meta.url), 'utf8').split('\n').filter((l) => l.startsWith('//')).slice(0, 24).map((l) => l.replace(/^\/\/ ?/, '')).join('\n'));
  // --help was asked for and answered, so it succeeded. Only a bare invocation is a misuse.
  process.exit(argv.includes('--help') ? 0 : 2);
}
if (verb === 'status' || verb === 'doctor') process.exit(cmdStatus());
if (verb === 'install') process.exit(cmdInstall(argv));
if (verb === 'uninstall') process.exit(cmdUninstall(argv));
if (verb === 'reset') process.exit(cmdReset(argv));
console.error(`unknown command: ${verb}`);
process.exit(2);
}

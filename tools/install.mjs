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
//   node install.mjs install --evict-missing  also remove c4x entries whose script file is gone
//   node install.mjs install --no-dashboard  do not start the dashboard with Claude (--dashboard undoes it)
//   node install.mjs install --no-review-sweep  the server neither takes back review-run records at
//                                            startup nor restarts Claude (--review-sweep undoes it)
//   node install.mjs reset --data|--settings|--all
//   node install.mjs --self-test
//
// It never prompts. An installer that blocks on stdin cannot run where this one has to: the
// desktop app spawns the CLI with piped stdio and no TTY, and SessionStart fires before any
// prompt exists. --dry-run is the review path instead.

import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync, copyFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { execFileSync, spawnSync } from 'node:child_process';
import { rootFrom, defaultDb, ensureStoreDir, posix } from './paths.mjs';
import { report, STALE_AFTER_DAYS } from './statusline.mjs';
import { DatabaseSync } from 'node:sqlite';

const ROOT = rootFrom(import.meta.url);
const SETTINGS = join(homedir(), '.claude', 'settings.json');
// The receipt records an install into ONE settings file, but its path is derived from the install
// ROOT, and ROOT does not move when HOME does. So pointing HOME at a scratch directory to exercise
// the installer safely still reads and REWRITES the receipt belonging to the real install, and an
// uninstall run that way deletes it outright. Measured, not theorised: a round trip against a
// scratch HOME destroyed the live receipt on this machine twice, taking priorStatusLine with it,
// which is the value a genuine uninstall needs to put the previous status line back.
//
// C4X_RECEIPT scopes the receipt to match a scoped HOME. Set both together or neither.
const RECEIPT = process.env.C4X_RECEIPT || join(ROOT, 'data', 'install-receipt.json');
const RECEIPT_VERSION = 1;

// Settings hold forward-slash paths even on Windows: backslashes would need escaping inside a
// JSON string and inside the shell word, and one of the two always gets forgotten.
const mib = (n) => (n >= 1 << 30 ? `${(n / (1 << 30)).toFixed(1)} GB` : `${Math.round(n / (1 << 20))} MB`);

// UserPromptSubmit is deliberately matcher-less: the event does not support a matcher at all, and
// supplying one is the kind of silently-ignored key this tool exists to catch.
export const WIRING = [
  { event: 'SessionStart', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'SessionEnd', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'UserPromptSubmit', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'PostToolUse', script: 'hooks/event-hook.mjs', timeout: 10, matcher: '*' },
  { event: 'SubagentStop', script: 'hooks/event-hook.mjs', timeout: 10, matcher: null },
  { event: 'PreCompact', script: 'hooks/compact-hook.mjs', timeout: 120, matcher: null },
  { event: 'PostCompact', script: 'hooks/compact-hook.mjs', timeout: 120, matcher: null },
];
const STATUSLINE_SCRIPT = 'tools/statusline.mjs';

// WHICH NODE THE HOOKS RUN. A checkout says `node` and means whatever is on PATH, which is what
// every install written so far says and what a developer has. The download carries its own
// (`node/node.exe` beside `c4x.exe`), because a machine that wanted a program with no
// prerequisites has no node either, and `node` there resolves to nothing at all.
//
// Quoted whichever it is: `C:/Users/Someone Else/c4x` has a space in it.
export const runtimeFor = (root, exists = existsSync) => {
  const bundled = join(root, 'node', process.platform === 'win32' ? 'node.exe' : 'node');
  return exists(bundled) ? `"${posix(bundled)}"` : 'node';
};

export const cmdFor = (root, script, exists = existsSync) =>
  `${runtimeFor(root, exists)} "${posix(join(root, script))}"`;

// Ownership used to be `cmd.includes(root)`, and a bare substring is wrong in both directions.
// Downwards: with root .../c4x, a hook at .../c4x-old/hooks/event-hook.mjs read as ours, and
// applyWiring dropped that other install's group on the floor. Upwards: c4x sits INSIDE
// P:/ClaudeExt/ccxe, so an installer rooted at the parent claimed every c4x hook as its own and
// removeWiring stripped all of them plus the statusLine. A segment boundary alone does not fix
// the second case, because the child genuinely is under the parent.
//
// So ownership is not "somewhere under root", it is "points at one of OUR script files in THIS
// root". Both siblings and parents fail that test, and an exact install still passes it.
// Case-insensitive on Windows, where the same path in different case is the same path and would
// otherwise be wired a second time rather than recognised as already present.
const OUR_SCRIPTS = [...new Set([...WIRING.map((w) => w.script), STATUSLINE_SCRIPT])];
const BOUNDARY_AFTER = new Set(['', '"', "'", ' ']);
const referencesPath = (cmd, abs) => {
  const fold = (s) => (process.platform === 'win32' ? s.toLowerCase() : s);
  const subject = fold(posix(cmd));
  const needle = fold(posix(abs));
  for (let i = subject.indexOf(needle); i !== -1; i = subject.indexOf(needle, i + 1)) {
    if (BOUNDARY_AFTER.has(subject[i + needle.length] ?? '')) return true;
  }
  return false;
};
export const ownsCommand = (cmd, root) => {
  if (typeof cmd !== 'string' || !posix(root).replace(/\/+$/, '')) return false;
  return OUR_SCRIPTS.some((s) => referencesPath(cmd, join(root, s)));
};

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
/**
 * Hook entries and a statusLine that name one of OUR scripts under a root that is GONE.
 *
 * `ownsCommand` is root-scoped on purpose, and the self-tests assert that a sibling install is NOT
 * owned - that is what stops this tool from ripping out another checkout's wiring. But it leaves a
 * gap it cannot see: a command ending in hooks/event-hook.mjs, hooks/compact-hook.mjs or
 * tools/statusline.mjs, under a directory that does not exist, is unambiguously OUR dead wiring and
 * nobody else's. Nothing looked for it.
 *
 * What that cost, observed on this machine: a previous install was deleted, its seven hooks and its
 * status line stayed in settings.json erroring on every event, `status` said HEALTHY, and a plain
 * reinstall would have produced FOURTEEN hook entries plus a receipt whose priorStatusLine pointed
 * at the deleted path - so a later uninstall would have "restored" a dead status line.
 *
 * THE FILE MUST BE ABSENT, not merely a different root. A live sibling checkout is somebody's
 * working install and is left alone, which is the whole reason ownsCommand is scoped the way it is.
 */
export function deadWiring(settings, exists = existsSync) {
  const out = [];
  const dead = (command, where) => {
    if (typeof command !== 'string') return;
    for (const script of OUR_SCRIPTS) {
      const tail = script.replace(/^tools[/\\]|^hooks[/\\]/, '');
      const m = command.match(new RegExp(`["'\\s]([^"'\\s]*[/\\\\](?:tools|hooks)[/\\\\]${tail.replace('.', '\\.')})["'\\s]?`));
      if (!m) continue;
      const file = m[1];
      if (exists(file)) return;                   // a live install, ours or a sibling: leave it
      out.push({ where, script, file });
      return;
    }
  };
  for (const [event, raw] of Object.entries(settings?.hooks ?? {})) {
    const { groups } = asGroups(raw);
    for (const g of groups) for (const h of (g?.hooks ?? [])) dead(h?.command, event);
  }
  dead(settings?.statusLine?.command, 'statusLine');
  return out;
}


/**
 * Settings with every dead-root c4x entry removed, and the list of what went.
 *
 * A pure transform for the same reason applyWiring is one: the decision can be asserted without
 * writing a settings file, and --dry-run reports exactly what the real run will do because both
 * call this.
 *
 * An emptied matcher group is dropped too. Leaving `{matcher:'*',hooks:[]}` behind would keep
 * `status` reporting a group for the event while nothing in it fires, which is the shape this file
 * was written to catch in the first place.
 */
export function evictDead(settings, exists = existsSync) {
  const gone = deadWiring(settings, exists);
  if (!gone.length) return { next: settings, gone };
  const files = new Set(gone.map((d) => d.file));
  const hit = (cmd) => typeof cmd === 'string' && [...files].some((f) => cmd.includes(f));
  const next = JSON.parse(JSON.stringify(settings));
  for (const [event, raw] of Object.entries(next.hooks ?? {})) {
    const { groups } = asGroups(raw);
    const kept = [];
    for (const g of groups) {
      const hooks = (g?.hooks ?? []).filter((h) => !hit(h?.command));
      if (hooks.length) kept.push({ ...g, hooks });
    }
    if (kept.length) next.hooks[event] = kept;
    else delete next.hooks[event];
  }
  if (hit(next.statusLine?.command)) delete next.statusLine;
  return { next, gone };
}


export function audit(settings, root, { rootExists = true, exists = existsSync } = {}) {
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
    // DRIFT IN THE COMMAND ITSELF, which nothing compared. `ownsCommand` only asks whether the
    // string mentions one of our scripts, so a hook wired with a different interpreter, a stale
    // absolute path to a node that has since been upgraded away, or an edited timeout, all read as
    // correctly installed. `applyWiring` would rewrite it on the next install; until then `status`
    // said HEALTHY over a command that may not run.
    //
    // DETECTION, NOT A PIN. Writing `process.execPath` into settings.json instead of `node` was the
    // obvious fix and is the wrong one: it hard-codes one interpreter into a file that outlives it,
    // so an nvm or fnm switch, or any node upgrade, leaves a command pointing at a binary that is
    // gone - and that failure looks exactly like the one this is meant to catch. Relying on PATH is
    // the more durable choice; noticing when the string is no longer what we would write is the
    // check that was missing.
    const want = cmdFor(root, w.script);
    const drifted = mine.flatMap((g) => g.hooks ?? [])
      .filter((h) => ownsCommand(h?.command, root) && h.command !== want);
    if (drifted.length) {
      findings.push({ level: 'warn', event: w.event,
                      why: `wired as ${JSON.stringify(drifted[0].command)} but this install writes `
                         + `${JSON.stringify(want)}. Re-run install to bring it back in step.` });
    }
  }

  // A DEAD ROOT, NAMED. `rootExists` used to carry this, and it could never fire: cmdStatus passed
  // `existsSync(ROOT)` where ROOT comes from import.meta.url, so it is true by construction and the
  // branch was reachable only from this file's own self-test. The parameter stays for that test and
  // for any caller that genuinely knows the root is gone; the finding that matters is this one,
  // which looks at what settings.json actually points at.
  if (!rootExists) {
    findings.push({ level: 'error', event: '*', why: `settings point at ${posix(root)}, which does not exist; every session fires a hook that errors` });
  }
  for (const d of deadWiring(settings, exists)) {
    findings.push({ level: 'error', event: d.where,
                    why: `points at ${posix(d.file)}, which does not exist. That is c4x wiring from `
                       + 'an install that was moved or deleted; it errors on every event. '
                       + 'Remove it with: node tools/install.mjs install --evict-missing' });
  }
  const sl = settings?.statusLine;
  if (!sl) findings.push({ level: 'warn', event: 'statusLine', why: 'not set' });
  else if (!ownsCommand(sl.command, root)) findings.push({ level: 'info', event: 'statusLine', why: 'set to something else; install would replace it and record the old value' });

  return findings;
}

// ---------------------------------------------------------------- io

const readJson = (p, dflt = null) => { try { return JSON.parse(readFileSync(p, 'utf8').replace(/^\uFEFF/, '')); } catch { return dflt; } };

// The settings file is NOT allowed to fall back to a default. The old `readJson(SETTINGS, {})` treated an
// unparseable file as an empty one, and since install converges {} to "our wiring", the write that
// followed replaced the user's entire configuration with our hooks plus statusLine. backupSettings()
// made that recoverable, which is not the same as harmless.
//
// Absent and unparseable are different states and only the first one is ordinary: a missing file is
// a first install, a corrupt file is a question for its owner.
export class SettingsUnreadable extends Error {}
export function parseSettings(raw) {
  if (raw === null || raw === undefined) return {};
  try { return JSON.parse(String(raw).replace(/^\uFEFF/, '')); }
  catch (e) { throw new SettingsUnreadable(`${posix(SETTINGS)} exists but is not valid JSON: ${e.message}`); }
}
const readSettings = () => parseSettings(existsSync(SETTINGS) ? readFileSync(SETTINGS, 'utf8') : null);

// temp+rename, so a concurrent reader never observes a half-written file. This one serialises the
// object it is handed; the re-read belongs to writeSettingsAtomic below, which is the only writer
// that has a file someone else also owns.
function writeJsonAtomic(p, obj) {
  mkdirSync(join(p, '..'), { recursive: true });
  const tmp = `${p}.tmp-${process.pid}`;
  writeFileSync(tmp, `${JSON.stringify(obj, null, 2)}\n`, 'utf8');
  renameSync(tmp, p);
}

// Claude Code writes settings.json too, and so does our own SessionStart hook. Everything above
// computes `next` from a snapshot taken earlier in the command, prints a diff from it, and then
// wrote that stale object back, silently discarding anything that landed in between. The comment
// on writeJsonAtomic claimed a re-read that never existed.
//
// So: re-read immediately before serialising and re-apply the transform to what is actually on
// disk. Both transforms converge, which is what makes replaying them on fresher input safe rather
// than merely different. The printed diff still comes from the earlier snapshot, so it can differ
// from what lands; that is reported instead of hidden.
function writeSettingsAtomic(transform) {
  const before = readSettings();
  const next = transform(before);
  writeJsonAtomic(SETTINGS, next);
  return { before, next };
}

// Exported because event-hook.mjs rewrites this same file when it self-heals, and a second copy
// of "where the backup goes" is exactly the kind of parallel literal that drifts apart.
export function backupSettings() {
  if (!existsSync(SETTINGS)) return null;
  const dest = `${SETTINGS}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`;
  copyFileSync(SETTINGS, dest);
  return dest;
}

// A SQLite store is three files, not one. Moving context.db while leaving context.db-wal behind
// strands a write-ahead log next to whatever database appears at that path next, and SQLite will
// try to replay it. The sidecars travel with the file or they go with it.
export const sidecars = (db) => [db, `${db}-wal`, `${db}-shm`];

/**
 * EVERYTHING `--purge` DELETES, as data rather than as four rmSync calls buried in a command.
 *
 * `data/snapshots` was the one this list was missing, and missing it is not a small thing: the
 * compact hook copies the WHOLE transcript there before every compaction, so it holds verbatim
 * conversation text - 603 MB in 13 files on the machine this was written on - while the README
 * said "--purge deletes what it collected". A user who ran the documented deletion path kept the
 * most sensitive artefact the tool produces.
 *
 * Exported and pure so the self-test can assert WHAT gets deleted without deleting anything, and
 * so `--dry-run` and the real run cannot describe different sets. They read the same list.
 */
export const purgeTargets = (root, db) => [
  ...sidecars(db),
  join(root, 'data', 'raw'),
  join(root, 'data', 'snapshots'),
];

function loadReceipt() { return readJson(RECEIPT, null); }

// The receipt lives beside the ROOT, but it describes a particular SETTINGS file. Those are not
// the same scope, and pointing HOME somewhere else to exercise the installer safely does not move
// the receipt with it: an uninstall run that way deletes the receipt belonging to the real install,
// taking priorStatusLine with it, so the genuine uninstall can no longer restore the status line it
// replaced and the SessionStart self-heal stops recognising itself as installed.
//
// Observed, not theorised: an install/uninstall round trip against a scratch HOME destroyed the
// live receipt on this machine and it had to be restored from a backup.
//
// A receipt with no settings field predates this and is treated as describing whatever it is asked
// about, so an older install still uninstalls cleanly.
export const receiptDescribes = (receipt, settingsPath) => {
  if (!receipt || typeof receipt.settings !== 'string') return true;
  return posix(receipt.settings).toLowerCase() === posix(settingsPath).toLowerCase();
};

/**
 * The receipt fields that are CHOICES rather than facts about this install, carried forward
 * across re-runs. `saveReceipt` rebuilds the file from scratch, so a field it does not carry is a
 * field a plain `install` or `reset` silently drops: the README promises a re-run changes nothing,
 * and a dashboard opt-out that came back on that promise would be exactly the surprise it forbids.
 */
/** The executable named by `--launcher <path>`, or null. */
export function launcherFrom(argv = []) {
  const at = argv.indexOf('--launcher');
  if (at === -1) return null;
  const said = argv[at + 1];
  return said && !said.startsWith('--') ? said : null;
}

export function receiptFields(argv = [], prior = null) {
  let dashboard = prior?.dashboard ?? true;
  if (argv.includes('--no-dashboard')) dashboard = false;
  if (argv.includes('--dashboard')) dashboard = true;
  // The startup sweep: the same shape, read by tools/dashboard.mjs into the server's argv.
  let reviewSweep = prior?.reviewSweep ?? true;
  if (argv.includes('--no-review-sweep')) reviewSweep = false;
  if (argv.includes('--review-sweep')) reviewSweep = true;
  return { dashboard, reviewSweep, dashboardLauncher: prior?.dashboardLauncher ?? null };
}

const CHOICE_FLAGS = ['--no-dashboard', '--dashboard', '--no-review-sweep', '--review-sweep'];

function choicesLine(receipt) {
  return `dashboard autostart: ${receipt?.dashboard === false ? 'off' : 'on'}; `
    + `review sweep at server start: ${receipt?.reviewSweep === false ? 'off' : 'on'}`;
}

function saveReceipt(extra, argv = []) {
  ensureStoreDir(join(ROOT, 'data'), { force: true });
  const prior = loadReceipt();
  writeJsonAtomic(RECEIPT, {
    tool: 'install.mjs', version: RECEIPT_VERSION, root: posix(ROOT), settings: posix(SETTINGS),
    installedAt: new Date().toISOString(),
    priorStatusLine: prior?.priorStatusLine ?? null,
    wiring: WIRING.map((w) => ({ ...w, command: cmdFor(ROOT, w.script) })),
    ...receiptFields(argv, prior),
    ...extra,
  });
}

// ---------------------------------------------------------------- the dashboard

// Through a child process, never an import: tools/dashboard.mjs imports the hook for `record`,
// and the hook imports this file, so importing the helper here would close a cycle whose bindings
// are unset at the moment they are read. Each call is bounded; `status` is a script gate.
function dashboardJson(verb, args = [], timeout = 4000) {
  const r = spawnSync(process.execPath, [join(ROOT, 'tools', 'dashboard.mjs'), verb, '--json', ...args],
                      { encoding: 'utf8', cwd: ROOT, timeout, windowsHide: true });
  try { return JSON.parse(String(r.stdout).trim().split(String.fromCharCode(10)).pop()); } catch { return null; }
}

/**
 * The "dashboard" line of `status`. Pure, like captureLine, so its states are checked with fixed
 * inputs rather than a live port.
 */
export function dashboardLine({ probe = null, receipt = null, env = {} } = {}) {
  if (env.C4X_NO_DASHBOARD === '1') return 'disabled (C4X_NO_DASHBOARD=1)';
  if (receipt && receipt.dashboard === false) return 'disabled (install --no-dashboard; `install --dashboard` turns it back on)';
  if (probe?.answered && probe.ours) {
    const missing = probe.storeExists === false ? ', but the store it names is missing' : '';
    const stop = probe.token
      ? `; stop it with: curl -X POST http://127.0.0.1:${probe.port}/__shutdown__ -H "X-C4X-Shutdown: ${probe.token}"`
      : '';
    return `answering on ${probe.port} for ${probe.db}${missing}${stop}`;
  }
  if (probe?.answered) return `port ${probe.port} held by ${probe.db || 'something that is not a c4x dashboard'}`;
  const l = receipt?.dashboardLauncher;
  const launcher = l?.launcher ? `${l.launcher.cmd.join(' ')} (${l.launcher.kind})`
    : l ? `none: ${l.why}` : 'resolved at the next Claude session';
  return `not running (launcher: ${launcher}); starts with the next Claude session`;
}

// ---------------------------------------------------------------- commands

// The groups that should not be able to read a store of conversation text. Windows-only: on POSIX
// `ensureStoreDir` chmods to 0700 and there is no inheritance to fight. Returns [] on any failure,
// because a status command that dies because icacls is missing helps nobody.
function looseGrants(dir) {
  if (process.platform !== 'win32' || !existsSync(dir)) return [];
  try {
    const out = execFileSync('icacls', [dir],
                             { encoding: 'utf8', timeout: 15_000, windowsHide: true });
    // NO BACKSLASH IN ANY LITERAL HERE, deliberately, because that character is what broke this
    // check the first time. Written as 'BUILTIN\Users' in the source it silently held
    // "BUILTINUsers": in a JavaScript string `\U` is not an escape, so the backslash is DROPPED
    // rather than kept, and the test for the group icacls prints could never match. An
    // independent reviewer found it by widening a directory to exactly that group and watching
    // `status` say nothing at all.
    //
    // icacls prints one ACE per line as `<trailing spaces><PRINCIPAL>:(FLAGS)`, and a principal
    // is `DOMAIN\NAME` or a bare name. Splitting on the `:(` and comparing the TAIL needs no
    // escape and cannot silently degrade the way a literal did.
    const principals = out.split(String.fromCharCode(10))
      .map((line) => line.split(':(')[0].trim())
      .filter(Boolean)
      .map((p) => p.slice(p.lastIndexOf(String.fromCharCode(92)) + 1));
    return ['Authenticated Users', 'Users', 'Everyone'].filter((g) => principals.includes(g));
  } catch { return []; }
}

/**
 * The two capture channels, side by side, because only the RATIO exposes the failure.
 *
 * Neither number is alarming alone. "16 status-line samples" looks like capture working; "43,518
 * hook events" looks like capture working. Sixteen against forty-three thousand, with the newest
 * sample nine days old, is the shape of a channel that stopped, and nothing in this tool put the
 * two side by side, so nobody could see it. The README's whole status-line narrative silently does
 * not apply to Claude Desktop users, who are the people most likely to install from the app.
 *
 * READ FROM THE STORE, not from data/raw/events.ndjson. That file is 44 MB here and grows about
 * 4 MB a day, and `status` is advertised as a script gate, so line-counting it would put an
 * unbounded read inside the fast command. `hook_events` holds the same rows, indexed.
 */
// Printed beside the self-heal line so the opt-out is discoverable at the moment it is relevant,
// rather than only in the source that reads it.
const NO_SELF_HEAL_HINT = process.env.C4X_NO_SELF_HEAL === '1'
  ? '  (disabled by C4X_NO_SELF_HEAL=1)'
  : '  (set C4X_NO_SELF_HEAL=1 to stop it)';

function captureLiveness(root) {
  const out = { events: null, lastEvent: null, samples: null, lastSample: null, ageDays: null,
                lastHeal: null, rawPending: false, lastRawEvent: null };
  try {
    const r = report(join(root, 'data', 'raw', 'statusline.ndjson'));
    if (r.exists) { out.samples = r.genuine; out.lastSample = r.last_genuine; out.ageDays = r.genuine_age_days; }
  } catch { /* absent is a real answer, reported as null */ }
  // THE THIRD STATE: captured but not yet harvested.
  //
  // The store is the right source and the comment above says why, but reading only the store gave
  // this line two answers where the user has three situations. Right after install, five events
  // were already in data/raw/events.ndjson and `hook_events` was empty because no harvest had run,
  // so `status` printed "0 events, last never" - which is what a DEAD capture looks like, on a
  // capture that was working perfectly. The surface built to answer "is it still running" reported
  // the one thing it exists to rule out.
  //
  // statSync, not a line count: size and mtime are one inode read whatever the file has grown to,
  // so the objection above still holds. mtime is when a hook last appended, which is the fact this
  // line needs. The COUNT still comes from the store, because that is the number that is indexed.
  try {
    const raw = join(root, 'data', 'raw', 'events.ndjson');
    if (existsSync(raw)) {
      const st = statSync(raw);
      out.rawPending = st.size > 0;
      out.lastRawEvent = new Date(st.mtimeMs).toISOString();
    }
  } catch { /* an unreadable raw log is not a status failure either */ }
  const db = defaultDb(root);
  if (!existsSync(db)) return out;
  try {
    const con = new DatabaseSync(`file:${posix(db)}?mode=ro`, { readOnly: true });
    try {
      const row = con.prepare('SELECT COUNT(*) n, MAX(captured_at) last FROM hook_events').get();
      out.events = row?.n ?? 0;
      out.lastEvent = row?.last ?? null;
      // WHEN THIS TOOL LAST EDITED YOUR SETTINGS. The SessionStart self-heal rewrites
      // ~/.claude/settings.json unattended and records itself in hook_events.reason, which is a
      // good instinct spoiled by nothing ever reading it back. A tool that edits a file it does
      // not own should say when it last did.
      const heal = con.prepare(
        "SELECT MAX(captured_at) last FROM hook_events WHERE reason LIKE 'c4x self-heal%'").get();
      out.lastHeal = heal?.last ?? null;
    } finally { con.close(); }
  } catch { /* a store mid-migration is not a status failure */ }
  return out;
}

/**
 * The "hook capture" line, from the store's count and the raw log's timestamp.
 *
 * Pure, and separate from cmdStatus, because the sentence is the part that was wrong: the numbers
 * were always available, and the line drew the wrong conclusion from them. `ago` is passed in so a
 * check can supply a fixed clock.
 */
export function captureLine(live, ago) {
  const waiting = live.rawPending
    ? `captured and waiting for a harvest, last ${ago(live.lastRawEvent)}`
    : null;
  if (live.events === null) {
    // No store. Absent a raw log this really is "nothing yet"; with one, capture is already running
    // and saying "no store" alone would read as a fault.
    return waiting ? `no store yet, but events are ${waiting}` : 'no store yet';
  }
  const counted = `${live.events.toLocaleString()} events, last ${ago(live.lastEvent)}`;
  const newer = live.rawPending && live.lastRawEvent
    && (!live.lastEvent || Date.parse(live.lastRawEvent) > Date.parse(live.lastEvent));
  if (live.events === 0 && live.rawPending) return `none harvested yet; events are ${waiting}`;
  return newer ? `${counted}; more ${waiting}` : counted;
}

function cmdStatus() {
  const settings = readSettings();
  const findings = audit(settings, ROOT, { rootExists: existsSync(ROOT) });
  const db = defaultDb(ROOT);
  const receipt = loadReceipt();

  console.log(`install root : ${posix(ROOT)}`);
  console.log(`settings     : ${posix(SETTINGS)}${existsSync(SETTINGS) ? '' : '  (missing)'}`);
  console.log(`store        : ${posix(db)}${existsSync(db) ? '' : '  (not created yet)'}`);
  console.log(`receipt      : ${receipt ? `written ${receipt.installedAt}` : 'none - this install was not made by install.mjs'}`);
  // WHO ELSE CAN READ THE CONVERSATION TEXT. Reported rather than fixed here: `ensureStoreDir`
  // hardens the directory when it creates it, but a store created before that existed, or one
  // whose ACL was widened afterwards, keeps whatever it had, and nothing would ever say so. A
  // warning, not an error, because a single-account machine is not at risk and refusing to run
  // over it would be theatre.
  // LIVENESS, not wiring. Everything above answers "is it configured"; this answers "is it
  // capturing", which is the question the README actually promises `status` will settle.
  const live = captureLiveness(ROOT);
  const ago = (ts) => {
    if (!ts) return 'never';
    const mins = Math.round((Date.now() - Date.parse(ts)) / 60000);
    if (mins < 60) return `${mins} min ago`;
    if (mins < 1440) return `${Math.round(mins / 60)} h ago`;
    return `${Math.round(mins / 1440)} days ago`;
  };
  console.log(`hook capture : ${captureLine(live, ago)}`);
  console.log(`status line  : ${live.samples === null ? 'no samples file' : `${live.samples.toLocaleString()} genuine samples, last ${ago(live.lastSample)}`}`);
  console.log(`self-heal    : ${live.lastHeal ? `last rewrote ${posix(SETTINGS)} ${ago(live.lastHeal)}`
    : 'never rewrote your settings'}${NO_SELF_HEAL_HINT}`);
  // IS THE PAGE UP, and is it ours. The hook starts it with Claude and the server stops itself
  // about a minute after the last Claude process; this line is how anyone finds out which it is.
  console.log(`dashboard    : ${dashboardLine({ probe: dashboardJson('probe'), receipt, env: process.env })}`);
  if (live.events > 0 && live.samples === 0) {
    findings.push({ level: 'warn', event: 'statusLine',
                    why: `never fired, while the hooks captured ${live.events.toLocaleString()} `
                       + 'events. The status line renders only in a terminal session; the Claude '
                       + 'Desktop chat view does not invoke it. Capture continues through the hooks.' });
  } else if (live.samples > 0 && live.ageDays !== null && live.ageDays >= STALE_AFTER_DAYS
             && live.events > 0 && ago(live.lastEvent) !== 'never') {
    findings.push({ level: 'warn', event: 'statusLine',
                    why: `stopped ${live.ageDays} days ago after ${live.samples} sample(s), while `
                       + `the hooks have captured ${live.events.toLocaleString()} events. That is `
                       + 'the Claude Desktop chat view, which never invokes a status line. Capture '
                       + 'continues through the hooks.' });
  }

  const loose = looseGrants(join(ROOT, 'data'));
  if (loose.length) {
    findings.push({ level: 'warn', event: 'data/',
                    why: `readable by ${loose.join(', ')}. It holds the text of your `
                       + 'conversations. Fix: icacls "' + posix(join(ROOT, 'data'))
                       // NO /T. It re-runs the whole command against every child, and (OI)(CI) on
                       // a FILE grants nothing, so each existing file is left with an empty DACL
                       // that not even its owner can read. Watched happen to data/raw here.
                       // Without /T the children inherit the new narrow ACL, which is the point.
                       + '" /inheritance:r /grant:r "%USERNAME%":(OI)(CI)F SYSTEM:(OI)(CI)F' });
  }
  for (const f of findings) console.log(`${f.level.toUpperCase().padEnd(5)} ${String(f.event).padEnd(16)} ${f.why}`);
  const errors = findings.filter((f) => f.level === 'error').length;
  console.log(errors ? `DRIFTED (${errors} error${errors === 1 ? '' : 's'})` : 'HEALTHY');
  return errors ? 1 : 0;
}

// ONE HARVEST AT THE END OF AN INSTALL THAT FOUND NO STORE.
//
// README's very next step after installing is `node tools/harvest.mjs --stats`, under the heading
// "Confirm it captured something". On day one that ran against a store which did not exist, because
// install writes wiring and nothing else: the store appears when the first hook fires or when
// somebody runs a harvest by hand. So the documented way to check a correct install reported a
// failure, on every correct install.
//
// ONLY WHEN THE STORE IS ABSENT. Installing over an existing store is a rewire, the hooks are
// already harvesting into it, and re-reading a large transcript set there would make `install` cost
// minutes to tell the user nothing. `--no-harvest` skips it either way.
//
// A failure here never fails the install. By this point the wiring is written and correct, which is
// what `install` promises; the harvest is a convenience on top, and saying so beats a non-zero exit
// that makes a working install look broken. That is the same mistake in the opposite direction.
// The decision, separated from the spawn so it can be checked. The spawn needs a scoped ROOT and a
// real transcript set to exercise, and ROOT comes from import.meta.url, so a test of the whole
// function would either run a real harvest or not run at all. The part that can be got wrong is
// which of the four inputs suppresses it, and that part is pure.
export function shouldFirstHarvest(argv, dry, storeExists) {
  if (dry) return false;
  if (argv.includes('--no-harvest')) return false;
  // --rewire is "touch settings only" by definition; harvesting there would exceed what was asked.
  if (argv.includes('--rewire')) return false;
  return !storeExists;
}

function firstHarvest(argv, dry) {
  if (!shouldFirstHarvest(argv, dry, existsSync(defaultDb(ROOT)))) return;
  console.log('no store yet: running one harvest, so there is something to confirm.');
  const r = spawnSync(process.execPath, [join(ROOT, 'tools', 'harvest.mjs')],
    { encoding: 'utf8', cwd: ROOT, timeout: 900_000, windowsHide: true });
  if (r.status !== 0 || r.error) {
    const why = r.error ? r.error.message : `exit ${r.status}${r.signal ? ` (${r.signal})` : ''}`;
    console.log(`  harvest did not finish (${why}). The wiring is written; run `
      + '`node tools/harvest.mjs` when convenient.');
    return;
  }
  let summary = null;
  try { summary = JSON.parse(r.stdout); } catch { /* fall through to the generic line */ }
  if (summary) {
    console.log(`  ${summary.files_read} of ${summary.files_seen} transcripts read, `
      + `${summary.turn_rows_stored} turns and ${summary.message_rows_stored} messages stored, `
      + `${summary.seconds}s.`);
    if (summary.unknown_record_types?.length) {
      console.log(`  unrecognised record types: ${summary.unknown_record_types.join(', ')}`);
    }
  } else {
    console.log(`  store created at ${posix(defaultDb(ROOT))}`);
  }
}

function cmdInstall(argv) {
  const dry = argv.includes('--dry-run');
  const rewire = argv.includes('--rewire');
  // Opt-in, never automatic. Removing entries from the user's settings.json without being asked is
  // exactly the behaviour ownsCommand's root scoping exists to prevent, so this is a flag and
  // `status` tells you when to reach for it.
  const evict = argv.includes('--evict-missing');

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
    else { ensureStoreDir(join(ROOT, 'data'), { force: true }); copyFileSync(src, dest); adopted = `copied ${posix(src)} -> ${posix(dest)}`; }
  }

  const settings = readSettings();
  const { gone } = evictDead(settings);
  if (gone.length && !evict) {
    console.log(`${gone.length} dead c4x entr${gone.length === 1 ? 'y' : 'ies'} found, pointing at a `
      + 'root that no longer exists. Re-run with --evict-missing to remove them:');
    for (const d of gone) console.log(`  ${d.where.padEnd(18)} ${posix(d.file)}`);
  }
  const base = evict ? evictDead(settings).next : settings;
  const { changes } = applyWiring(base, ROOT);
  if (evict) for (const d of gone) console.log(`${dry ? 'would ' : ''}evict ${d.where} -> ${posix(d.file)}`);

  if (!changes.length && !adopted && !(evict && gone.length)) {
    console.log('no changes: already converged');
    // A choice is not a change to the wiring, and it still has to land.
    if (!dry && CHOICE_FLAGS.some((f) => argv.includes(f))) {
      saveReceipt({}, argv);
      console.log(choicesLine(loadReceipt()));
    }
    firstHarvest(argv, dry);
    return 0;
  }
  for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);
  if (adopted) console.log(adopted);
  if (dry) { console.log('--dry-run: nothing written'); return 0; }

  const backup = backupSettings();
  const { before } = writeSettingsAtomic((s) => applyWiring(evict ? evictDead(s).next : s, ROOT).next);
  // Taken from what was actually on disk at write time, not from the earlier snapshot, so a
  // statusLine that arrived in between is still the one we record as the prior value.
  const priorStatusLine = ownsCommand(before?.statusLine?.command, ROOT) ? undefined : (before?.statusLine ?? null);
  if (!rewire) ensureStoreDir(join(ROOT, 'data', 'raw'), { force: true });
  // WHICH LAUNCHER, decided now rather than in the hook: this is the one place that may take its
  // time, and a python probe costs seconds. Written into the receipt so the hook reads it.
  //
  // `--launcher <exe>` SKIPS THE PROBE ENTIRELY, and is how the download installs itself: the
  // program doing the installing is the program that should start the dashboard, there is no
  // Python on that machine to find, and probing for one would spend seconds to learn nothing.
  const told = launcherFrom(argv);
  const resolved = told ? null : dashboardJson('resolve', ['--fresh'], 60_000);
  const dashboardLauncher = told
    ? { launcher: { cmd: [posix(told)], module: false, kind: 'exe' },
        why: 'the program that installed it', resolvedAt: new Date().toISOString() }
    : resolved
      ? { launcher: resolved.launcher ?? null, why: resolved.why ?? '', resolvedAt: new Date().toISOString() }
      : null;
  saveReceipt({ settingsBackup: backup ? posix(backup) : null,
                ...(priorStatusLine === undefined ? {} : { priorStatusLine }),
                ...(dashboardLauncher ? { dashboardLauncher } : {}) }, argv);
  {
    const r = loadReceipt();
    const l = r?.dashboardLauncher;
    console.log(`dashboard    : ${r?.dashboard === false ? 'off (--no-dashboard)'
      : l?.launcher ? `starts with Claude via ${l.launcher.cmd.join(' ')} (${l.launcher.kind})`
      : `cannot start: ${l?.why ?? 'launcher not resolved'}`}`);
    console.log(`review sweep : ${r?.reviewSweep === false ? 'off (--no-review-sweep)'
      : 'at server start, records c4x wrote for review runs are taken back and Claude is restarted'}`);
  }
  console.log(`wrote ${posix(SETTINGS)}${backup ? ` (backup: ${posix(backup)})` : ''}`);
  // Measured, not assumed: the first hook fired nine seconds after this write, in a session that
  // had already been running for twenty hours. The only thing a running session cannot get is an
  // event that has already gone past.
  console.log('hooks take effect immediately, in sessions already running too.');
  console.log('only events that already fired are missed: SessionStart, and any past compaction.');
  // AFTER the settings write, the receipt and ensureStoreDir, deliberately. A harvest can run for
  // a while on a long transcript history, and if it is interrupted the wiring is already correct
  // and the receipt already describes it, so an uninstall still undoes exactly what was done.
  //
  // This is the path a REAL first install takes, and until now it was the one path that never
  // harvested: the only call sat inside the `already converged` branch above, which by definition
  // runs when there was nothing to install. The feature was inverted - it fired on a no-op
  // re-install, where a store almost always exists and shouldFirstHarvest returns false anyway.
  firstHarvest(argv, dry);
  return 0;
}

function cmdUninstall(argv) {
  const dry = argv.includes('--dry-run');
  const purge = argv.includes('--purge');
  const settings = readSettings();
  const receipt = loadReceipt();
  const { changes } = removeWiring(settings, ROOT, receipt);

  if (!changes.length) console.log('nothing of ours found in settings');
  for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);

  const db = defaultDb(ROOT);
  if (purge && existsSync(db)) console.log(`${dry ? 'would ' : ''}delete ${posix(db)}`);
  else if (existsSync(db)) console.log(`store kept at ${posix(db)} - pass --purge to delete it`);

  // THE SNAPSHOTS ARE THE MOST SENSITIVE THING THIS TOOL WRITES AND --purge LEFT THEM BEHIND.
  //
  // On every PreCompact, hooks/compact-hook.mjs copies the WHOLE transcript into data/snapshots -
  // verbatim conversation text, up to 250 MB a file, and the same session re-snapshotted at every
  // boundary so the copies are cumulative. On this machine that is 604 MB in 13 files. Purge
  // removed the store, its sidecars and data/raw, and nothing in this repo has ever deleted a
  // snapshot, while README says "--purge deletes what it collected".
  //
  // REPORTED OUTSIDE THE existsSync(db) BRANCHES ABOVE, deliberately. Those two lines are one
  // if/else on the store's presence, and after `reset --data` renames context.db to a .bak the
  // store is absent - so a snapshot line placed there would print nothing in exactly the state
  // where a user still has 604 MB of transcripts and is being told the tool has been purged.
  const snaps = join(ROOT, 'data', 'snapshots');
  const snapFiles = existsSync(snaps) ? readdirSync(snaps) : [];
  if (snapFiles.length) {
    const bytes = snapFiles.reduce((n, f) => n + (statSync(join(snaps, f)).size || 0), 0);
    const size = `${snapFiles.length} pre-compaction transcript snapshot(s), ${mib(bytes)}`;
    if (purge) console.log(`${dry ? 'would ' : ''}delete ${posix(snaps)} - ${size}`);
    else console.log(`snapshots kept at ${posix(snaps)} - ${size}. They are verbatim copies of `
                     + 'your transcripts; pass --purge to delete them, or set C4X_SNAPSHOT=0 to '
                     + 'stop taking them.');
  }

  if (dry) { console.log('--dry-run: nothing written'); return 0; }
  // A server the hook started would otherwise outlive the install that started it, holding the
  // store open for up to a minute after the last Claude process, or for as long as one stays open.
  {
    const up = dashboardJson('probe');
    if (up?.answered && up.ours) {
      const r = dashboardJson('stop');
      console.log(`dashboard    : ${r?.stopped ? 'stopped' : `still running: ${r?.why ?? 'could not ask it to stop'}`}`);
    }
  }
  if (changes.length) { backupSettings(); writeSettingsAtomic((s) => removeWiring(s, ROOT, loadReceipt()).next); }
  if (purge) {
    for (const f of purgeTargets(ROOT, db)) rmSync(f, { recursive: true, force: true });
  }
  if (receiptDescribes(receipt, SETTINGS)) rmSync(RECEIPT, { force: true });
  else console.log(`kept the receipt: it records an install into ${posix(receipt.settings)}, not ${posix(SETTINGS)}`);
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
    if (existsSync(raw)) { console.log(`${dry ? 'would ' : ''}clear ${posix(raw)}`); if (!dry) { rmSync(raw, { recursive: true, force: true }); ensureStoreDir(raw); } }
    // Same reasoning as --purge: "reset the data" that leaves every transcript copy in place is
    // not a reset. The store is renamed to a .bak rather than deleted, so the snapshots are
    // renamed alongside it rather than dropped, and nothing the user had is lost by surprise.
    const snaps = join(ROOT, 'data', 'snapshots');
    if (existsSync(snaps) && readdirSync(snaps).length) {
      const bak = `${snaps}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`;
      console.log(`${dry ? 'would ' : ''}move ${posix(snaps)} -> ${posix(bak)}`);
      if (!dry) renameSync(snaps, bak);
    }
  }

  if (wantSettings) {
    const settings = readSettings();
    const cleared = removeWiring(settings, ROOT, loadReceipt()).next;
    const { changes } = applyWiring(cleared, ROOT);
    for (const c of changes) console.log(`${dry ? 'would ' : ''}${c}`);
    if (!dry) { backupSettings(); writeSettingsAtomic((s) => applyWiring(removeWiring(s, ROOT, loadReceipt()).next, ROOT).next); saveReceipt({}, argv); }
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

  // Ownership. Every one of these passed as "ours" under the old substring test, and the two
  // consequence checks below are the damage that followed from it.
  const SIBLING = 'X:/fake/root-old';
  const NESTED = 'X:/fake/root/nested';
  add('our own hook is owned', ownsCommand(cmdFor(R, 'hooks/event-hook.mjs'), R));
  add('our own statusLine is owned', ownsCommand(cmdFor(R, STATUSLINE_SCRIPT), R));
  add("another tool's hook is not owned", !ownsCommand('node "D:/other-tool/watch.mjs"', R));
  add('a sibling root whose name merely starts with ours is NOT owned',
    !ownsCommand(cmdFor(SIBLING, 'hooks/event-hook.mjs'), R), cmdFor(SIBLING, 'hooks/event-hook.mjs'));
  add('a nested install one directory down is NOT owned by the parent root',
    !ownsCommand(cmdFor(NESTED, 'hooks/event-hook.mjs'), R), cmdFor(NESTED, 'hooks/event-hook.mjs'));
  add('a path under our root that is not one of our scripts is not owned',
    !ownsCommand(`node "${R}/hooks/somebody-elses.mjs"`, R));

  // Consequence 1: installing must not delete a sibling install's group.
  const withSibling = structuredClone(good);
  withSibling.hooks.SessionStart.push({ hooks: [{ type: 'command', command: cmdFor(SIBLING, 'hooks/event-hook.mjs') }] });
  add('install leaves a sibling install\u0027s hook alone',
    JSON.stringify(applyWiring(withSibling, R).next.hooks.SessionStart).includes('root-old'));

  // The receipt describes ONE settings file, and it does not travel when HOME is pointed elsewhere.
  // Uninstalling against a scratch HOME used to delete the real install's receipt, which is how the
  // live one on this machine was lost and had to be restored from a backup.
  const liveReceipt = { settings: 'C:/Users/Shake/.claude/settings.json', priorStatusLine: null };
  add('a receipt is deleted when it describes the settings file being uninstalled',
    receiptDescribes(liveReceipt, 'C:/Users/Shake/.claude/settings.json'));
  add('a receipt describing a DIFFERENT settings file is kept (gate can fail)',
    !receiptDescribes(liveReceipt, `${R}/tmp/scratch/.claude/settings.json`));
  add('the comparison ignores separator and case, which are the same path on Windows',
    receiptDescribes({ settings: 'C:\\Users\\Shake\\.claude\\settings.json' }, 'C:/Users/shake/.claude/settings.json'));
  add('a receipt with no settings field predates this and still uninstalls',
    receiptDescribes({ priorStatusLine: null }, 'anything'));
  add('no receipt at all is not an obstacle', receiptDescribes(null, 'anything'));

  // Reading settings. Absent and unparseable are different states, and only the first is ordinary.
  const threw = (fn) => { try { fn(); return null; } catch (e) { return e; } };
  add('a missing settings file reads as empty, which is a normal first install', JSON.stringify(parseSettings(null)) === '{}');
  add('a valid settings file parses', parseSettings('{"permissions":{"allow":["Bash"]}}').permissions.allow[0] === 'Bash');
  add('a leading BOM does not break the parse', parseSettings('﻿{"a":1}').a === 1);
  const corrupt = threw(() => parseSettings('{"hooks": {oops'));
  add('an unparseable settings file throws instead of becoming {}', corrupt instanceof SettingsUnreadable, String(corrupt));
  // The stakes, stated as a check rather than a comment: this is what a {} fallback would write.
  add('converging {} would have written only our keys, which is what the throw prevents',
    Object.keys(applyWiring({}, R).next).join(',') === 'hooks,statusLine');

  // The race. writeSettingsAtomic re-reads and re-applies rather than writing a stale snapshot,
  // so what has to hold is that replaying a transform on fresher input keeps what it did not put
  // there, and settles. Both transforms are checked, in both directions.
  const raced = structuredClone(good);
  raced.env = { ARRIVED_LATE: '1' };
  raced.hooks.Stop = [{ hooks: [{ type: 'command', command: 'node "D:/guard/stop.mjs"' }] }];
  const replayedIn = applyWiring(raced, R).next;
  add('a key that lands between read and write survives the replay', replayedIn.env?.ARRIVED_LATE === '1');
  add('a hook event that lands between read and write survives the replay',
    JSON.stringify(replayedIn.hooks.Stop).includes('guard/stop.mjs'));
  add('replaying install on its own output settles', applyWiring(replayedIn, R).changes.length === 0);
  const replayedOut = removeWiring(raced, R, null).next;
  add('uninstall replayed on fresher input keeps the late arrival', replayedOut.env?.ARRIVED_LATE === '1');
  add('uninstall replayed on its own output settles', removeWiring(replayedOut, R, null).changes.length === 0);

  // Consequence 2: uninstalling at the PARENT root must not strip the nested install.
  const nestedInstalled = applyWiring({}, NESTED).next;
  const afterParentRemove = removeWiring(nestedInstalled, R, null);
  add('uninstall at a parent root leaves a nested install wired',
    JSON.stringify(afterParentRemove.next.hooks) === JSON.stringify(nestedInstalled.hooks), afterParentRemove.changes.join(','));
  add('uninstall at a parent root leaves the nested statusLine alone',
    JSON.stringify(afterParentRemove.next.statusLine) === JSON.stringify(nestedInstalled.statusLine));

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

  // `live` stubs existsSync, because every fixture here points at X:/fake/root and the new
  // dead-wiring finding would otherwise fire on all of them. The dead-root behaviour itself is
  // asserted separately below, with the stub saying the file is gone.
  const live = () => true;
  add('a healthy config audits clean', audit(good, R, { exists: live }).filter((f) => f.level === 'error').length === 0);
  const badFindings = audit(malformed, R, { exists: live });
  add('the malformed config audits as an error', badFindings.some((f) => f.level === 'error' && f.why.includes('never fires')));
  add('a missing install root is reported', audit(good, R, { rootExists: false, exists: live }).some((f) => f.why.includes('does not exist')));
  add('an empty settings file audits as errors, not silence', audit({}, R).filter((f) => f.level === 'error').length === WIRING.length);

  // A store is db + -wal + -shm. Losing the second two behind a move is how a stale write-ahead
  // log ends up next to a fresh database.
  const s = sidecars('/x/data/context.db');
  add('a store is treated as three files, not one', s.length === 3 && s[1].endsWith('context.db-wal') && s[2].endsWith('context.db-shm'), s.join(','));
  add('the sidecar suffixes append to the backup name so the pair stays valid',
    `${'/x/data/context.db.bak-T'}${s[1].slice('/x/data/context.db'.length)}` === '/x/data/context.db.bak-T-wal');

  // Falsification gate: the audit must be capable of failing, or "audits clean" proves nothing.
  const realErrors = audit(good, R, { exists: live }).filter((f) => f.level === 'error').length;
  const mutantErrors = audit(good, 'Y:/different/root', { exists: live }).filter((f) => f.level === 'error').length;
  add('pointing the audit at a different root makes it FAIL (gate can fail)', mutantErrors > realErrors, `real=${realErrors} mutant=${mutantErrors}`);

  // --purge REACHES data/snapshots. Driven end to end against a scratch root holding a real file,
  // not asserted off the list, because "the list is right" and "the deletion uses the list" are two
  // different claims and only one of them was true before.
  {
    const root = join(ROOT, 'tmp', `purge-selftest-${process.pid}`);
    const db = join(root, 'data', 'context.db');
    const snap = join(root, 'data', 'snapshots', 'sess.2026-01-01.pre-compact.jsonl');
    const raw = join(root, 'data', 'raw', 'events.ndjson');
    try {
      mkdirSync(join(root, 'data', 'snapshots'), { recursive: true });
      mkdirSync(join(root, 'data', 'raw'), { recursive: true });
      writeFileSync(db, 'x'); writeFileSync(snap, 'transcript'); writeFileSync(raw, '{}');

      const targets = purgeTargets(root, db);
      add('purge names the snapshot directory (gate can fail)',
        targets.some((t) => t.endsWith('snapshots')), targets.join(' | '));
      add('purge names the store, its sidecars and data/raw',
        targets.length === 5 && targets.includes(db) && targets.some((t) => t.endsWith('raw')));

      for (const f of targets) rmSync(f, { recursive: true, force: true });
      add('deleting them REMOVES the transcript snapshot (gate can fail)', !existsSync(snap));
      add('and removes the store and the raw log', !existsSync(db) && !existsSync(raw));
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  }

  // COMMAND DRIFT: a hook that mentions our script but is not the string we would write.
  {
    const drifted = JSON.parse(JSON.stringify(good));
    const ev = WIRING[0].event;
    drifted.hooks[ev][0].hooks[0].command = `"C:/old/node.exe" "${R}/${WIRING[0].script}"`;
    const f = audit(drifted, R, { exists: live });
    add('a hook wired with a different interpreter is REPORTED (gate can fail)',
      f.some((x) => x.event === ev && x.why.includes('but this install writes')),
      JSON.stringify(f.map((x) => x.why.slice(0, 40))));
    add('and the unmodified config still reports no drift',
      !audit(good, R, { exists: live }).some((x) => x.why.includes('but this install writes')));
  }

  // DEAD WIRING: found, evicted, and NOT confused with a live sibling.
  //
  // The third case is the one that matters most and the reason ownsCommand is root-scoped: another
  // checkout's hooks are somebody's working install. This must remove entries whose FILE is gone
  // and nothing else.
  {
    const deadRoot = 'V:/DELETED/old-root';
    const sibling = 'D:/other-checkout';
    const cfg = {
      hooks: {
        PostToolUse: [{ matcher: '*', hooks: [
          { type: 'command', command: `node "${deadRoot}/hooks/event-hook.mjs"` },
          { type: 'command', command: `node "${sibling}/hooks/event-hook.mjs"` },
          { type: 'command', command: 'node "D:/someone-else/watch.mjs"' },
        ] }],
        Stop: [{ hooks: [{ type: 'command', command: `node "${deadRoot}/hooks/compact-hook.mjs"` }] }],
      },
      statusLine: { type: 'command', command: `node "${deadRoot}/tools/statusline.mjs"` },
    };
    // Everything exists EXCEPT the deleted root, which is the real-world shape.
    const exists = (f) => !String(f).includes('DELETED');

    const found = deadWiring(cfg, exists);
    add('dead c4x wiring is found', found.length === 3, JSON.stringify(found.map((d) => d.where)));
    add('a LIVE sibling install is NOT touched (gate can fail)',
      !found.some((d) => d.file.includes(sibling)));
    add("and another tool's hook is not ours to judge",
      !found.some((d) => d.file.includes('someone-else')));

    const { next } = evictDead(cfg, exists);
    const left = (next.hooks.PostToolUse ?? []).flatMap((g) => g.hooks).map((h) => h.command);
    add('eviction keeps the sibling and the foreign hook', left.length === 2
      && left.some((c) => c.includes(sibling)) && left.some((c) => c.includes('someone-else')));
    add('eviction drops the dead one', !left.some((c) => c.includes('DELETED')));
    add('an emptied event is removed, not left as a hollow group', next.hooks.Stop === undefined);
    add('a dead statusLine is dropped', next.statusLine === undefined);
    add('audit REPORTS it rather than staying silent (gate can fail)',
      audit(cfg, 'P:/live/root', { exists }).some((f) => f.why.includes('--evict-missing')));
  }

  // The day-one harvest. README's next step after installing is `harvest --stats`, and on a fresh
  // machine that ran against a store nothing had created yet, so the documented way to confirm a
  // correct install reported a failure. Each suppressing input is checked on its own, because a
  // condition that is never exercised is how this stops firing without anyone noticing.
  add('a fresh install harvests once, so there is a store to confirm (gate can fail)',
    shouldFirstHarvest([], false, false) === true);
  add('an install over an existing store does not re-harvest',
    shouldFirstHarvest([], false, true) === false);
  // THE DOWNLOAD RUNS ITS OWN NODE. A machine that unzipped one folder has no node on PATH, so a
  // hook command reading `node "...\hooks\event-hook.mjs"` there would fail on every event.
  {
    const bundled = (p) => String(p).replace(/\\/g, '/').endsWith('/node/node.exe')
      || String(p).replace(/\\/g, '/').endsWith('/node/node');
    add('a folder carrying node has its hooks run that node',
      cmdFor('C:/Users/me/c4x', 'hooks/event-hook.mjs', bundled)
        === '"C:/Users/me/c4x/node/node.exe" "C:/Users/me/c4x/hooks/event-hook.mjs"'
      || cmdFor('C:/Users/me/c4x', 'hooks/event-hook.mjs', bundled)
        === '"C:/Users/me/c4x/node/node" "C:/Users/me/c4x/hooks/event-hook.mjs"');
    add('a checkout keeps the bare name, which is every existing install',
      cmdFor('C:/work/c4x', 'hooks/event-hook.mjs', () => false)
        === 'node "C:/work/c4x/hooks/event-hook.mjs"');
    add('a path with a space in it is quoted either way',
      cmdFor('C:/Users/Someone Else/c4x', 'tools/statusline.mjs', () => false)
        .includes('"C:/Users/Someone Else/c4x/tools/statusline.mjs"'));
    // OWNERSHIP IS ABOUT THE SCRIPT, NOT THE INTERPRETER: an install that switched from PATH's
    // node to its own must still recognise, and be able to remove, what it wrote before.
    add('a command is ours whichever node it names',
      ownsCommand(cmdFor('C:/Users/me/c4x', 'hooks/event-hook.mjs', bundled), 'C:/Users/me/c4x')
      && ownsCommand(cmdFor('C:/Users/me/c4x', 'hooks/event-hook.mjs', () => false), 'C:/Users/me/c4x'));
  }

  add('--launcher names the program that installed it',
    launcherFrom(['install', '--launcher', 'C:/Users/me/c4x/c4x.exe']) === 'C:/Users/me/c4x/c4x.exe');
  add('--launcher with nothing after it is not a launcher',
    launcherFrom(['install', '--launcher']) === null && launcherFrom(['install', '--launcher', '--rewire']) === null);
  add('no --launcher means the probe still decides', launcherFrom(['install']) === null);

  add('--dry-run writes nothing and harvests nothing',
    shouldFirstHarvest([], true, false) === false);
  add('--no-harvest opts out even with no store',
    shouldFirstHarvest(['--no-harvest'], false, false) === false);
  add('--rewire touches settings only, as it says',
    shouldFirstHarvest(['--rewire'], false, false) === false);

  // THE DECISION WAS CHECKED AND THE WIRING WAS NOT, so all five checks above passed while the
  // feature was dead. firstHarvest had exactly one call site, inside the `already converged`
  // branch, which by definition runs when there was nothing to install: it fired on a no-op
  // re-install and never on a real first install, the only case it exists for.
  //
  // A self-test cannot spawn an install to prove this - that is what the comment above
  // shouldFirstHarvest explains - so the call GRAPH is asserted from this module's own source
  // instead. tests/test_floor.py reads source for the same reason, and
  // tests/test_render_tables.py states the principle: a dispatch nobody installed is a comment.
  {
    const src = readFileSync(new URL(import.meta.url), 'utf8');
    const from = src.indexOf('function cmdInstall(');
    const rest = src.slice(from);
    const stop = rest.indexOf(String.fromCharCode(10) + 'function ');
    const fn = stop === -1 ? rest : rest.slice(0, stop);
    const calls = [...fn.matchAll(/firstHarvest\(/g)].map((m) => m.index);
    const writesAt = fn.indexOf('writeSettingsAtomic(');
    add('the day-one harvest is called on more than one path (gate can fail)',
      calls.length >= 2, `${calls.length} call site(s) in cmdInstall`);
    add('and one call is on the path that actually WRITES the wiring (gate can fail)',
      writesAt !== -1 && calls.some((i) => i > writesAt),
      `writeSettingsAtomic at ${writesAt}, calls at ${calls.join(',')}`);
  }

  // The capture line. The defect was a sentence, not a number: a working capture whose events were
  // not yet harvested was described exactly like a dead one. A fixed clock, so these do not drift.
  {
    const at = (ts) => (ts === 'T0' ? '0 min ago' : ts === null ? 'never' : 'a while ago');
    const base = { events: null, lastEvent: null, rawPending: false, lastRawEvent: null };
    add('a fresh install with events on disk does not read as dead (gate can fail)',
      captureLine({ ...base, rawPending: true, lastRawEvent: 'T0' }, at)
        === 'no store yet, but events are captured and waiting for a harvest, last 0 min ago');
    add('and with nothing captured at all it still says so plainly',
      captureLine(base, at) === 'no store yet');
    add('a harvested store reports the indexed count',
      captureLine({ ...base, events: 37, lastEvent: 'T0' }, at) === '37 events, last 0 min ago');
    add('an empty table with a non-empty log is not reported as zero events',
      captureLine({ ...base, events: 0, lastEvent: null, rawPending: true, lastRawEvent: 'T0' }, at)
        === 'none harvested yet; events are captured and waiting for a harvest, last 0 min ago');
    add('events arriving since the last harvest are reported as pending',
      captureLine({ events: 5, lastEvent: '2026-01-01T00:00:00Z', rawPending: true,
                    lastRawEvent: '2026-01-02T00:00:00Z' }, at).includes('more captured and waiting'));
    add('and a log older than the last harvest adds nothing (gate can fail)',
      captureLine({ events: 5, lastEvent: '2026-01-02T00:00:00Z', rawPending: true,
                    lastRawEvent: '2026-01-01T00:00:00Z' }, at) === '5 events, last a while ago');
  }

  // The dashboard choice survives a re-run, which is what a choice recorded in a file rebuilt
  // from scratch was not going to do on its own.
  add('a fresh install starts the dashboard by default', receiptFields([], null).dashboard === true);
  add('--no-dashboard records the opt-out', receiptFields(['--no-dashboard'], null).dashboard === false);
  add('a re-install without the flag KEEPS the opt-out (gate can fail)',
    receiptFields([], { dashboard: false }).dashboard === false);
  add('--dashboard lifts it', receiptFields(['--dashboard'], { dashboard: false }).dashboard === true);
  add('the resolved launcher is carried forward too',
    receiptFields([], { dashboardLauncher: { why: 'x' } }).dashboardLauncher?.why === 'x');
  add('the review sweep is on by default', receiptFields([], null).reviewSweep === true);
  add('--no-review-sweep records the opt-out', receiptFields(['--no-review-sweep'], null).reviewSweep === false);
  add('a re-install without the flag KEEPS the sweep opt-out (gate can fail)',
    receiptFields([], { reviewSweep: false }).reviewSweep === false);
  add('--review-sweep lifts it', receiptFields(['--review-sweep'], { reviewSweep: false }).reviewSweep === true);
  add('the sweep choice does not touch the dashboard choice',
    receiptFields(['--no-review-sweep'], { dashboard: false }).dashboard === false
    && receiptFields(['--no-dashboard'], null).reviewSweep === true);

  // The status line's states, with fixed inputs.
  {
    const up = { answered: true, ours: true, port: 8059, db: 'P:/c4x/data/context.db', storeExists: true, token: 'tok' };
    add('a live server of ours is reported with how to stop it',
      dashboardLine({ probe: up }) === 'answering on 8059 for P:/c4x/data/context.db; stop it with: '
        + 'curl -X POST http://127.0.0.1:8059/__shutdown__ -H "X-C4X-Shutdown: tok"');
    add('a live server whose store is missing says so',
      dashboardLine({ probe: { ...up, storeExists: false, token: null } }).includes('the store it names is missing'));
    add('another holder is named',
      dashboardLine({ probe: { answered: true, ours: false, port: 8059, db: 'E:/x.db' } }) === 'port 8059 held by E:/x.db');
    add('not running names the launcher the receipt resolved',
      dashboardLine({ probe: { answered: false }, receipt: { dashboardLauncher: { launcher: { cmd: ['py', '-3'], kind: 'python' } } } })
        === 'not running (launcher: py -3 (python)); starts with the next Claude session');
    add('not running with no launcher says why',
      dashboardLine({ probe: { answered: false }, receipt: { dashboardLauncher: { launcher: null, why: 'no python' } } }).includes('none: no python'));
    add('the env opt-out wins over a live server (gate can fail)',
      dashboardLine({ probe: up, env: { C4X_NO_DASHBOARD: '1' } }).startsWith('disabled'));
    add('the receipt opt-out is reported with the way back',
      dashboardLine({ probe: { answered: false }, receipt: { dashboard: false } }).includes('install --dashboard'));
  }

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
try {
  if (verb === 'status' || verb === 'doctor') process.exit(cmdStatus());
  if (verb === 'install') process.exit(cmdInstall(argv));
  if (verb === 'uninstall') process.exit(cmdUninstall(argv));
  if (verb === 'reset') process.exit(cmdReset(argv));
} catch (e) {
  if (!(e instanceof SettingsUnreadable)) throw e;
  // Stop rather than converge. Converging an unreadable file means writing our wiring over
  // whatever could not be parsed, and a backup makes that undoable, not correct.
  console.error(`ERROR ${e.message}`);
  console.error('Refusing to write, because converging this file would replace your configuration with ours.');
  console.error('Fix the JSON, or move the file aside to start fresh, then run this again.');
  process.exit(2);
}
console.error(`unknown command: ${verb}`);
process.exit(2);
}

// paths.mjs - where the install lives and where its store lives, resolved one way for every tool.
//
// Why this exists: harvest.mjs and waste.mjs honoured C4X_DB, while mirror.mjs, probe.mjs and
// segments.mjs hardcoded join(ROOT,'data','context.db'). So `C4X_DB=copy.db node mirror.mjs` read
// production while `... node waste.mjs` read the copy, and no error said so. A tool that silently
// ignores the flag you pointed at it is worse than one that refuses it.
//
// Precedence, identical everywhere: --db flag, then C4X_DB, then <root>/data/context.db.

import { join, dirname } from 'node:path';
import { existsSync, mkdirSync, chmodSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { rmSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// import.meta.url is a file:// URL; on Windows that is /C:/... and the drive letter needs the
// leading slash stripped before it is a usable path. Every tool derived this line for itself.
export function rootFrom(importMetaUrl) {
  return join(dirname(new URL(importMetaUrl).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
}

/**
 * A path as the docs and the receipt spell it: forward slashes, everywhere it is PRINTED.
 *
 * This existed as a module-private one-liner in install.mjs, so every other tool printed whatever
 * the platform handed it. The result is one store described two ways in the same session, and in
 * otel-ingest's case two ways five lines apart, which reads as two different files.
 *
 * For DISPLAY and for the settings file, never for opening anything: Windows accepts both, and
 * rewriting a path before passing it to the filesystem is how a normaliser becomes a bug.
 */
export function posix(p) {
  return String(p).split(String.fromCharCode(92)).join('/');
}

export function defaultDb(root) {
  return join(root, 'data', 'context.db');
}

// Directories this tool creates for its own data, made PRIVATE TO THE USER who created them.
//
// The store holds the verbatim text of conversations. Under C:/Users/<you> or ~ it would inherit
// a user-only ACL and this would be redundant, but the checkout does not have to live there: on a
// data volume it inherits whatever the volume root grants, which on a stock Windows data drive is
// `Authenticated Users:(M)` and `BUILTIN\Users:(RX)`. Measured on two different machines and two
// different drives. So the tool copies transcripts out of a user-only directory and lands them
// somewhere every local account can read, and nothing said so.
//
// EVERY site that can create a directory under data/ goes through here. That list is longer than
// it looks and getting it wrong is invisible: an independent reviewer found `harvest.mjs`'s
// unknown-records writer still bare-mkdir'ing data/raw after the first pass "fixed" this, and the
// comment here claimed the job was done. The full set, all wired:
//
//   tools/install.mjs   data/, the --adopt copy, data/raw
//   tools/harvest.mjs   openDb (store dir + raw), the unknown-records writer
//   tools/probe.mjs     the store dir and its raw log
//   tools/statusline.mjs the sample log
//   tools/otel-ingest.mjs the store dir and its unparsed log
//   hooks/event-hook.mjs  the events log and the harvest stamp - THE FIRST WRITER on a fresh
//                         machine, since a hook fires before anyone runs a harvest by hand
//   hooks/compact-hook.mjs the compaction log and data/snapshots, which holds verbatim
//                         transcripts and is the most sensitive directory this tool creates
//
// chmodSync, NOT mkdir's `mode`: mode is masked by the process umask, and it is ignored outright
// when the directory already exists, which is every run after the first.
/**
 * One argument, quoted for cmd.exe, for the calls that genuinely need a shell.
 *
 * Node emits DEP0190 when `shell: true` is paired with an args ARRAY, and the warning is not
 * pedantry: with a shell, node concatenates the arguments into one command line without escaping
 * them, so a path containing a space or an operator is re-split by cmd. The fix is to do the
 * quoting deliberately and pass a single string, which leaves args.length at 0 so the deprecation
 * cannot fire either.
 *
 * `%` IS IN THE TEST, which is easy to miss. cmd.exe expands `%VAR%` INSIDE double quotes, so a
 * checkout under a directory whose name contains a percent sign - legal on NTFS - would still be
 * mangled by a quoting rule that only looked for whitespace.
 */
export function winArg(s) {
  const str = String(s);
  return /[\s&|<>^"%()]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
}

export function ensureStoreDir(dir, { force = false } = {}) {
  // HARDEN ON CREATION, NOT ON EVERY CALL. mkdirSync(recursive) returns the first path it made,
  // or undefined when the directory was already there. The hooks call this on every event, and
  // spawning icacls per tool call would put a process spawn on the PostToolUse path that Claude
  // Code waits for - the cost the README already has to apologise for. `install` passes
  // force:true, because it runs rarely and is the command that should repair an existing tree.
  const created = mkdirSync(dir, { recursive: true });
  if (!created && !force) return dir;
  try {
    if (process.platform === 'win32') {
      // Break inheritance on THE DIRECTORY, and grant inheritable full control to this user and
      // SYSTEM. Existing files inherit the new, narrow ACL automatically, because their own
      // inheritance is still enabled.
      //
      // NO /T, AND THE REASON IS NOT STYLE. /T re-runs this ENTIRE command against every child,
      // `/inheritance:r` included - and on a FILE the (OI)(CI) flags on the grants are inheritance
      // flags with nothing to inherit them, so they contribute no access. The child ends with its
      // inherited ACEs stripped and nothing put back: an EMPTY DACL that even its owner cannot
      // read. Written that way here first, and it locked this account out of its own
      // data/raw/*.ndjson on the very next command; `icacls <dir> /reset /T` is what puts a tree
      // damaged that way back. A store nobody can read is a worse outcome than a store someone
      // else can.
      //
      // Failure is not fatal: `install.mjs status` reports a weak ACL separately, so the condition
      // is surfaced rather than swallowed, and a harvest that dies over permissions helps nobody.
      execFileSync('icacls', [dir, '/inheritance:r',
                              '/grant:r', `${process.env.USERNAME}:(OI)(CI)F`,
                              '/grant:r', 'SYSTEM:(OI)(CI)F', '/C', '/Q'],
                   { stdio: 'ignore', timeout: 30_000, windowsHide: true });
    } else {
      chmodSync(dir, 0o700);
    }
  } catch { /* reported by status, never fatal here */ }
  return dir;
}

// An EXPLICIT path must already exist. SQLite creates on open, so a typo in --db would otherwise
// produce an empty database and a report full of zeros, which reads exactly like "there was
// nothing to do" - the most expensive kind of wrong answer this repo can give. The DEFAULT path is
// exempt: that is how a fresh install bootstraps its store on the first harvest.
//
// onError lets a caller decide between exiting (CLI) and throwing (self-test), instead of this
// module deciding that process.exit is always acceptable.
export function resolveDb(root, argv = process.argv.slice(2), onError = null) {
  const fail = (msg) => {
    if (onError) return onError(msg);
    console.error(msg);
    process.exit(2);
  };
  const i = argv.indexOf('--db');
  if (i !== -1) {
    const v = argv[i + 1];
    if (!v || v.startsWith('--')) return fail('--db needs a path, for example --db tmp/copy.db');
    if (!existsSync(v)) return fail(`--db ${v} does not exist. Refusing to create it: an empty store reports zeros, which is indistinguishable from a store with nothing in it.`);
    return v;
  }
  const env = process.env.C4X_DB;
  if (env) {
    if (!existsSync(env)) return fail(`C4X_DB points at ${env}, which does not exist. Refusing to create it.`);
    return env;
  }
  return defaultDb(root);
}

// ---------------------------------------------------------------------------
// Checks. This file was EXEMPT from the suite's self-test requirement, on the grounds that it held
// "path constants only, nothing to exercise". That stopped being true when it gained the store
// directory hardening and the Windows argument quoting, and an exemption that no longer describes
// its subject is how a gate goes quiet. The exemption is gone and these run with the rest.
// ---------------------------------------------------------------------------
function selfTest() {
  const checks = [];
  const add = (what, ok, detail = '') => checks.push([what, ok, detail]);

  add('a plain argument is left alone', winArg('lint') === 'lint');
  add('a path with a space is quoted', winArg('C:/Program Files/x') === '"C:/Program Files/x"');
  // cmd.exe expands %VAR% INSIDE double quotes, so a percent must force quoting too, and a
  // whitespace-only rule would have missed it. NTFS allows % in a directory name.
  add('a percent forces quoting (gate can fail)', winArg('C:/pct%20dir') === '"C:/pct%20dir"');
  add('a shell operator forces quoting', winArg('a&b') === '"a&b"' && winArg('a|b') === '"a|b"');
  add('an embedded quote is doubled, not dropped', winArg('a"b') === '"a""b"');
  add('the result never carries an args array with it', ['npm', 'run', 'lint'].map(winArg).join(' ')
    === 'npm run lint');

  const slash = (s) => s.split(String.fromCharCode(92)).join('/');
  add('defaultDb sits under data/', slash(defaultDb('R')) === 'R/data/context.db');
  add('rootFrom strips the leading slash from a Windows file URL',
    /^[A-Za-z]:/.test(slash(rootFrom('file:///C:/a/b/c.mjs'))));

  // ensureStoreDir returns its directory and is idempotent; the ACL half is platform behaviour and
  // is exercised where it can be observed, not asserted here.
  const scratch = join(rootFrom(import.meta.url), 'tmp', `paths-selftest-${process.pid}`);
  try {
    add('ensureStoreDir creates and returns the directory',
      ensureStoreDir(scratch) === scratch && existsSync(scratch));
    add('and calling it again is harmless', ensureStoreDir(scratch) === scratch);
  } finally { rmSync(scratch, { recursive: true, force: true }); }

  let bad = 0;
  for (const [what, ok, detail] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}${ok ? '' : `   [${detail}]`}`);
  }
  console.log(bad ? `SELF-TEST FAIL (${bad}/${checks.length} failed)` : `SELF-TEST PASS (${checks.length} checks)`);
  return bad ? 1 : 0;
}

const IS_ENTRY = (() => {
  try {
    return process.argv[1] ? pathToFileURL(process.argv[1]).href === import.meta.url : false;
  } catch { return false; }
})();
if (IS_ENTRY && process.argv.includes('--self-test')) process.exit(selfTest());

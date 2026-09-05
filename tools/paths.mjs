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

// import.meta.url is a file:// URL; on Windows that is /C:/... and the drive letter needs the
// leading slash stripped before it is a usable path. Every tool derived this line for itself.
export function rootFrom(importMetaUrl) {
  return join(dirname(new URL(importMetaUrl).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
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

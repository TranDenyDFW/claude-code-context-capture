// paths.mjs - where the install lives and where its store lives, resolved one way for every tool.
//
// Why this exists: harvest.mjs and waste.mjs honoured C4X_DB, while mirror.mjs, probe.mjs and
// segments.mjs hardcoded join(ROOT,'data','context.db'). So `C4X_DB=copy.db node mirror.mjs` read
// production while `... node waste.mjs` read the copy, and no error said so. A tool that silently
// ignores the flag you pointed at it is worse than one that refuses it.
//
// Precedence, identical everywhere: --db flag, then C4X_DB, then <root>/data/context.db.

import { join, dirname } from 'node:path';
import { existsSync } from 'node:fs';

// import.meta.url is a file:// URL; on Windows that is /C:/... and the drive letter needs the
// leading slash stripped before it is a usable path. Every tool derived this line for itself.
export function rootFrom(importMetaUrl) {
  return join(dirname(new URL(importMetaUrl).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
}

export function defaultDb(root) {
  return join(root, 'data', 'context.db');
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

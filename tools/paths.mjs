// paths.mjs - where the install lives and where its store lives, resolved one way for every tool.
//
// Why this exists: harvest.mjs and waste.mjs honoured C4X_DB, while mirror.mjs, probe.mjs and
// segments.mjs hardcoded join(ROOT,'data','context.db'). So `C4X_DB=copy.db node mirror.mjs` read
// production while `... node waste.mjs` read the copy, and no error said so. A tool that silently
// ignores the flag you pointed at it is worse than one that refuses it.
//
// Precedence, identical everywhere: --db flag, then C4X_DB, then <root>/data/context.db.

import { join, dirname, resolve } from 'node:path';
import http from 'node:http';
import { existsSync, mkdirSync, chmodSync, readdirSync, readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { rmSync } from 'node:fs';
import { domainToUnicode, fileURLToPath, pathToFileURL } from 'node:url';

// import.meta.url is a file:// URL; on Windows that is /C:/... and the drive letter needs the
// leading slash stripped before it is a usable path. Every tool derived this line for itself.
//
// AND A URL IS NOT A PATH: its path part stays percent-encoded. Until it was decoded here, an
// install under a folder whose name held a space, `#`, `%`, a square or curly bracket, a tilde,
// a caret, a backtick or any non-ASCII letter (`C:\Users\John Smith`, a profile with an
// accent in it, an 8.3 short name such as `ADMINI~1`) resolved to `...\John%20Smith\...`,
// which is not the folder it lives in. That was so for the ten tools that call this, for two
// more that carried a copy of the line, and for both hooks; and it is the root `install.mjs`
// hands to `cmdFor` when it writes the hook commands. Decoded ONCE: a folder really called
// `p%41q` arrives as `p%2541q` and must come out as `p%41q`, not `pAq`.
//
// A SHARE is spelled `file://server/share/...` (that is what `pathToFileURL` makes of one), and
// the path part alone drops the host, so it goes back in front, in the file system's spelling
// rather than the URL's (a server name with an accent travels as punycode). A drive letter
// loses its slash only when there is NO host. No module has been seen running from a share:
// started over this machine's own admin share, node 24.19 failed before the module ran.
//
// Not a plain `fileURLToPath`: by default it follows the platform it runs on, and the self-test
// feeds this a Windows URL on the ubuntu legs too. It is the oracle there instead, for the URLs
// the platform itself makes. ONE KNOWN DIFFERENCE, older than the decoding and left alone: on
// POSIX a folder at the very top of the file system whose name is a letter and a colon (`/c:/x`)
// reads as a drive and comes out relative.
export function rootFrom(importMetaUrl) {
  const url = new URL(importMetaUrl);
  let path = url.pathname;
  try { path = decodeURIComponent(path); } catch { /* a URL no loader makes; leave it as it came */ }
  const whole = url.host
    ? `//${domainToUnicode(url.host) || url.host}${path}`
    : path.replace(/^\/([A-Za-z]:)/, '$1');
  return join(dirname(whole), '..');
}

// A LINE OF A TOOL OR A HOOK THAT TAKES A PATH OUT OF A MODULE'S URL BY HAND. Two triggers. The
// word itself, however it is reached (a property, a destructuring, a bracket). And ANY use of
// the module's own URL that is not one of the four this tree has: handed to `rootFrom`, handed
// to `fileURLToPath`, compared with another URL (how a file asks whether it is the entry), or
// read back as source. An allow-list, because a deny-list on one spelling let eight others by
// (slicing `href`, replacing the scheme, `decodeURI` and a substring, `url.parse`). It is still
// a LINE sweep: a URL put in a variable on one line and cut up on the next is not seen.
// The detector is spelled in two halves so that this file's own lines are not what it finds.
const PATH_WORD = new RegExp('\\bpath' + 'name\\b');
const OWN_URL = new RegExp('import[.]meta[.]' + 'url');
export function rootByHand(line) {
  if (/^\s*(\/\/|\/?\*)/.test(line)) return false;
  if (PATH_WORD.test(line)) return true;
  const rest = line
    .replace(/\b(rootFrom|fileURLToPath)\(import[.]meta[.]url\)/g, '')
    .replace(/[!=]==?\s*import[.]meta[.]url|import[.]meta[.]url\s*[!=]==?/g, '')
    .replace(/readFileSync\(new URL\(import[.]meta[.]url\)/g, '');
  return OWN_URL.test(rest);
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

// ---------------------------------------------------------------------------
// Is the dashboard up, and is it OURS. Three callers ask: the SessionStart hook before it starts
// one, tools/dashboard.mjs before it spawns one, and `install status` when it reports. They live
// here because every one of them already imports this module and the hook must not import the
// helper it spawns (dashboard.mjs imports the hook for `record`, and a cycle between them would
// leave one side's bindings unset at the moment they are read).
// ---------------------------------------------------------------------------

/** The port the API serves: C4X_API_PORT, else 8059, which is what c4x/api/__main__.py does. */
export function portFrom(env = process.env) {
  const n = Number(env.C4X_API_PORT);
  return Number.isInteger(n) && n > 0 ? n : 8059;
}

/**
 * Whether two spellings name one store file.
 *
 * The server answers `/__health__` with `DB_PATH.as_posix()`, resolved by Python; the node side
 * holds `join(ROOT, 'data', 'context.db')`, with backslashes on Windows and whatever drive-letter
 * case node was started with. A plain string comparison of those two never matched, so a hook
 * that asked "is this ours" would have read its own server as a stranger's on every session.
 * Resolved, forward slashes, and case-folded where the filesystem is.
 */
export function sameStorePath(a, b) {
  if (!a || !b) return false;
  const norm = (p) => {
    const s = posix(resolve(String(p)));
    return process.platform === 'win32' ? s.toLowerCase() : s;
  };
  return norm(a) === norm(b);
}

/**
 * Ask `/__health__` on a port who is there. Resolves, never rejects, to one of:
 *   { answered: false, why }                        refused, or nothing within timeoutMs
 *   { answered: true, ours: false, db: null, why }   something that is not a c4x dashboard
 *   { answered: true, ours, db, port, storeExists }  a c4x dashboard, ours when db is `store`
 * Bounded by `timeoutMs` in every path: a hook calls this inside a 10 s budget.
 */
export function probeHealth(port, store, { timeoutMs = 1000 } = {}) {
  return new Promise((done) => {
    let settled = false;
    const finish = (answer) => { if (!settled) { settled = true; done(answer); } };
    const timer = setTimeout(() => finish({ answered: false, why: `no answer within ${timeoutMs} ms` }), timeoutMs);
    let req;
    try {
      req = http.get({ host: '127.0.0.1', port, path: '/__health__', timeout: timeoutMs }, (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => { if (body.length < 4096) body += chunk; });
        res.on('end', () => {
          clearTimeout(timer);
          if (res.statusCode !== 200) return finish({ answered: true, ours: false, db: null, why: `status ${res.statusCode}` });
          let parsed = null;
          try { parsed = JSON.parse(body); } catch { /* not JSON: not a dashboard */ }
          if (!parsed || parsed.ok !== true || typeof parsed.db !== 'string') {
            return finish({ answered: true, ours: false, db: null, why: 'answered, but not as a c4x dashboard' });
          }
          finish({ answered: true, ours: sameStorePath(parsed.db, store), db: parsed.db,
                   port: parsed.port ?? null, storeExists: parsed.store_exists ?? null });
        });
        res.on('error', (e) => { clearTimeout(timer); finish({ answered: false, why: e.message }); });
      });
      req.on('timeout', () => { req.destroy(); });
      req.on('error', (e) => { clearTimeout(timer); finish({ answered: false, why: e.code || e.message }); });
    } catch (e) {
      clearTimeout(timer);
      finish({ answered: false, why: e.message });
    }
  });
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
// THE GATE FOR THE CLASS, NOT THE INSTANCE.
//
// `winArg` above was written for one defect and then applied to two of the three call sites that
// had it. The third, tools/probe.mjs, went on emitting DEP0190 on every run, and an external sweep
// found it still there after the fix and its written rationale had both shipped. That is the shape
// of the mistake: the helper lands, one caller is converted, and nothing ever asks who else needed
// it. A helper whose adoption nothing checks is a helper that gets adopted once.
//
// So the rule is machine-checked over the source, not remembered. A call is reported when BOTH
// hold: it passes three or more arguments, so there is an argv distinct from the command, AND its
// options object names `shell` as anything but false. The single pre-quoted string form that
// `winArg` exists to build has two arguments and is what this asks for.
// ---------------------------------------------------------------------------

// A copy of the source with the INSIDE of every string, template, comment and regex replaced by
// spaces, keeping length and line breaks so offsets still point at the original.
//
// Without this the gate reported its own control strings: the checks below pass a bad call as a
// STRING to prove the detector fires, and scanning this file found those three literals and called
// them offenders. A detector that cannot tell code from a quoted example of code is a detector
// that will also miss the real thing behind a quote.
function blankLiterals(src) {
  const BACKSLASH = String.fromCharCode(92);
  const out = src.split('');
  const blank = (from, to) => {
    for (let k = from; k < to && k < out.length; k++) if (out[k] !== '\n') out[k] = ' ';
  };
  // A slash starts a regex when what precedes it cannot end an expression.
  const PRE = /[([{,;:=!&|?+\-*%<>~^]/;
  const WORD = /\b(return|typeof|case|in|of|do|else)$/;
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < src.length) {
        if (src[j] === BACKSLASH) { j += 2; continue; }
        if (src[j] === c) break;
        j++;
      }
      blank(i + 1, j); i = j + 1; continue;
    }
    if (c === '/' && src[i + 1] === '/') {
      let j = i; while (j < src.length && src[j] !== '\n') j++;
      blank(i, j); i = j; continue;
    }
    if (c === '/' && src[i + 1] === '*') {
      const e = src.indexOf('*/', i + 2); const j = e < 0 ? src.length : e + 2;
      blank(i, j); i = j; continue;
    }
    if (c === '/') {
      let k = i - 1;
      while (k >= 0 && (src[k] === ' ' || src[k] === '\n' || src[k] === '\t')) k--;
      if (k < 0 || PRE.test(src[k]) || WORD.test(src.slice(0, k + 1))) {
        let j = i + 1, cls = false;
        while (j < src.length && src[j] !== '\n') {
          if (src[j] === BACKSLASH) { j += 2; continue; }
          if (src[j] === '[') cls = true;
          else if (src[j] === ']') cls = false;
          else if (src[j] === '/' && !cls) break;
          j++;
        }
        blank(i + 1, j); i = j + 1; continue;
      }
    }
    i++;
  }
  return out.join('');
}

// Top-level arguments of the call whose opening parenthesis is at `open`, or null if the source
// runs out first. Tracks strings, template literals and comments so a comma or bracket inside one
// does not split the list.
function callArgs(src, open) {
  const parts = [];
  const BACKSLASH = String.fromCharCode(92);
  let depth = 0, start = open + 1, quote = null;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === BACKSLASH) { i++; continue; }
      if (c === quote) quote = null;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') { quote = c; continue; }
    if (c === '/' && src[i + 1] === '/') { while (i < src.length && src[i] !== '\n') i++; continue; }
    if (c === '/' && src[i + 1] === '*') { const e = src.indexOf('*/', i + 2); if (e < 0) return null; i = e + 1; continue; }
    if (c === '(' || c === '[' || c === '{') { depth++; if (depth === 1) start = i + 1; continue; }
    if (c === ')' || c === ']' || c === '}') {
      depth--;
      if (depth === 0) { parts.push(src.slice(start, i)); return parts; }
      continue;
    }
    if (c === ',' && depth === 1) { parts.push(src.slice(start, i)); start = i + 1; }
  }
  return null;
}

/** Every child-process call in `source` that pairs a shell with a separate argument vector. */
export function shellArgvCalls(source) {
  const found = [];
  const src = blankLiterals(source);
  const CALL = /\b(spawnSync|spawn|execFileSync|execFile)\s*\(/g;
  let m;
  while ((m = CALL.exec(src)) !== null) {
    const args = callArgs(src, m.index + m[0].length - 1);
    if (!args || args.length < 3) continue;
    const opts = args[args.length - 1];
    if (!/\bshell\s*:/.test(opts) || /\bshell\s*:\s*false\b/.test(opts)) continue;
    found.push({ fn: m[1], line: source.slice(0, m.index).split('\n').length });
  }
  return found;
}

/** Every .mjs file under the directories a hook or tool can ship from. */
export function sourceFiles(root, dirs = ['tools', 'hooks']) {
  const out = [];
  const walk = (dir) => {
    if (!existsSync(dir)) return;
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      if (e.name === 'node_modules' || e.name.startsWith('.')) continue;
      const full = join(dir, e.name);
      if (e.isDirectory()) walk(full);
      else if (/\.(mjs|cjs|js)$/.test(e.name)) out.push(full);
    }
  };
  for (const d of dirs) walk(join(root, d));
  return out;
}

// ---------------------------------------------------------------------------
// Checks. This file was EXEMPT from the suite's self-test requirement, on the grounds that it held
// "path constants only, nothing to exercise". That stopped being true when it gained the store
// directory hardening and the Windows argument quoting, and an exemption that no longer describes
// its subject is how a gate goes quiet. The exemption is gone and these run with the rest.
// ---------------------------------------------------------------------------
async function selfTest() {
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

  // The gate for the class. Controls first: a detector that reports nothing passes a clean tree
  // for the wrong reason, and one that reports everything is equally useless.
  add('the shell-plus-argv gate catches the bad form (gate can fail)',
    shellArgvCalls('spawn(CLAUDE, args, { shell: true, windowsHide: true });').length === 1);
  add('and catches it when the shell is decided per platform',
    shellArgvCalls("spawn(c, a, { shell: process.platform === 'win32' });").length === 1);
  add('and ignores the pre-quoted single string this asks for',
    shellArgvCalls("spawnSync(['npm','run','lint'].map(winArg).join(' '), { shell: true });").length === 0);
  add('and ignores an argv with no shell at all',
    shellArgvCalls("spawnSync('npm', ['run', 'lint'], { encoding: 'utf8' });").length === 0);
  add('and ignores an argv whose shell is explicitly false',
    shellArgvCalls("spawn(c, a, { shell: false });").length === 0);
  add('and is not fooled by a comma inside a string argument',
    shellArgvCalls('spawn("a,b", { shell: true });').length === 0);

  const offenders = [];
  for (const f of sourceFiles(rootFrom(import.meta.url))) {
    for (const c of shellArgvCalls(readFileSync(f, 'utf8'))) {
      offenders.push(`${f.split(String.fromCharCode(92)).join('/').split('/').slice(-2).join('/')}:${c.line} ${c.fn}`);
    }
  }
  add('no tool pairs a shell with an args array (gate can fail)', offenders.length === 0,
    offenders.join('; '));

  const slash = (s) => s.split(String.fromCharCode(92)).join('/');
  add('defaultDb sits under data/', slash(defaultDb('R')) === 'R/data/context.db');
  add('rootFrom strips the leading slash from a Windows file URL',
    /^[A-Za-z]:/.test(slash(rootFrom('file:///C:/a/b/c.mjs'))));
  // A URL IS NOT A PATH: its path part stays percent-encoded, so an install under a folder with
  // a space in its name resolved to `...%20...`, which is not the folder it lives in. Measured
  // on node 24 for a space, `#`, `%`, square and curly brackets, a tilde (every 8.3 short name
  // has one: `ADMINI~1`), a caret, a backtick and several non-ASCII letters (a Windows profile
  // called Jose with an accent is enough); parentheses, `&`, `'`, `;`, `+` were always read right. The URLs below are the ones node itself makes
  // for a module under such a folder, and `fileURLToPath` is the oracle FOR THESE NAMES: by
  // default it follows the platform it runs on, which is why a plain swap cannot replace
  // `rootFrom` (the check above feeds a Windows URL to an ubuntu leg).
  const awkward = ['plain', 'sp ace', 'a#b', 'p%41q', '100%', 'uni\u00e9', '[br]{ace}', "quo'te", 'amp&eq=x;+',
    'ADMINI~1', 'car^et', 'back`tick'];
  const under = (name) => join(rootFrom(import.meta.url), 'tmp', 'no-such', name);
  const wrong = awkward.filter((name) =>
    rootFrom(pathToFileURL(join(under(name), 'tools', 'x.mjs')).href) !== under(name));
  add('rootFrom finds the install under a folder a URL escapes: a space, #, %41, an accent (gate can fail)',
    wrong.length === 0, wrong.map((name) => `${name} -> ${rootFrom(pathToFileURL(join(under(name), 'tools', 'x.mjs')).href)}`).join('; '));
  const disagrees = awkward.filter((name) => {
    const url = pathToFileURL(join(under(name), 'tools', 'x.mjs')).href;
    return rootFrom(url) !== join(dirname(fileURLToPath(url)), '..');
  });
  add('and agrees with fileURLToPath for each of those names (gate can fail)',
    disagrees.length === 0, disagrees.join('; '));
  add('an escape in the FOLDER NAME is not decoded twice: p%41q stays p%41q, never pAq (gate can fail)',
    slash(rootFrom('file:///C:/x/p%2541q/tools/c.mjs')) === 'C:/x/p%41q');
  // A SHARE. node spells `\\server\share\x` as `file://server/share/x`, host and all, and the
  // path part alone drops the host. A share is a Windows thing, but the checks are exact on
  // both platforms: POSIX `join` folds the doubled slash, so the host is still there to be seen,
  // and dropping it fails on an ubuntu leg too. The URLs are put together from pieces so that
  // no line of this file looks like a share to a tool that reads command lines.
  const shareUrl = (host, rest) => `file:${'/'.repeat(2)}${host}/${rest}`;
  const onShare = (host, rest) => (process.platform === 'win32' ? '//' : '/') + `${host}/${rest}`;
  add('a share keeps its host (gate can fail)',
    slash(rootFrom(shareUrl('server', 'share/my%20c4x/tools/c.mjs'))) === onShare('server', 'share/my c4x'));
  add('a drive letter after a host keeps its slash: the host and the drive are not glued together (gate can fail)',
    slash(rootFrom(shareUrl('server', 'C:/a/tools/c.mjs'))) === onShare('server', 'C:/a'));
  add('a server name with an accent comes back as the file system spells it, not as punycode (gate can fail)',
    slash(rootFrom(shareUrl('s\u00e9rver', 'share/c4x/tools/c.mjs'))) === onShare('s\u00e9rver', 'share/c4x'),
    slash(rootFrom(shareUrl('s\u00e9rver', 'share/c4x/tools/c.mjs'))));
  add('a URL no loader makes (a lone %) is left as it came rather than thrown on',
    (() => { try { return slash(rootFrom('file:///C:/a%zz/tools/c.mjs')) === 'C:/a%zz'; } catch { return false; } })());

  // THE SWEEP, fed what it claims to see before it is trusted with the tree. Every bad line
  // below resolves the wrong root under a folder with a space in it; the first is the line four
  // files carried, the rest are the spellings a review showed slipping past a sweep that knew
  // only that one.
  const M = ['import', 'meta', 'url'].join('.');
  const P = 'path' + 'name';
  const badLines = [
    `const ROOT = join(dirname(new URL(${M}).${P}.replace(/^\\/([A-Za-z]:)/, '$1')), '..');`,
    `const { ${P} } = new URL(${M});`,
    `const here = new URL(${M})['${P}'];`,
    `const here = ${M}.slice(8);`,
    `const here = ${M}.replace('file:///', '');`,
    `const here = decodeURI(${M}).substring(8);`,
    `const here = new URL(${M}).href.slice(8);`,
    `const here = url.parse(${M}).path;`,
    `const here = ${M}.split('/').slice(3).join('/');`,
    `const here = ${M}.match(/^file:...(.*)$/)[1];`,
    // The URL was taken on an earlier line; only the word gives these two away.
    `const here = decodeURIComponent(u.${P});`,
    `const { ${P}: here } = u;`,
  ];
  const fineLines = [
    `const ROOT = rootFrom(${M});`,
    `const SELF = fileURLToPath(${M});`,
    `const ROOT = join(dirname(fileURLToPath(${M})), '..');`,
    `    return process.argv[1] ? pathToFileURL(process.argv[1]).href === ${M} : false;`,
    `  const src = readFileSync(new URL(${M}), 'utf8');`,
    `// a comment may say ${M} and ${P} as often as it likes`,
    ` * and so may a block comment: ${M}.${P}`,
    `const d = new DatabaseSync(DB_PATH, { readOnly: true });`,
  ];
  const missed = badLines.filter((line) => !rootByHand(line));
  const falseAlarms = fineLines.filter((line) => rootByHand(line));
  add('the sweep sees twelve ways of cutting a path out of a module URL by hand (gate can fail)',
    missed.length === 0, missed.join(' | '));
  add('and lets the four honest uses of a module URL, and comments, through (gate can fail)',
    falseAlarms.length === 0, falseAlarms.join(' | '));
  // THE TREE. Every source file under tools/ and hooks/, this one included up to its self-test
  // (whose lines spell the bad forms out in order to look for them). This file is allowed ONE
  // hit, the line inside `rootFrom`; a new helper here that cut a URL up by hand would be a
  // second. If this ever flags an HTTP URL's path in a server or a fetch helper, that is a
  // different thing from a module's own URL: name the line in LEGIT with the reason.
  const LEGIT = [];   // e.g. 'tools/dashboard.mjs: const route = new URL(req.url, base)...'
  const byHand = [];
  for (const f of sourceFiles(rootFrom(import.meta.url))) {
    const at = slash(f).split('/').slice(-2).join('/');
    const lines = readFileSync(f, 'utf8').split('\n');
    const stop = at === 'tools/paths.mjs' ? lines.findIndex((line) => line.startsWith('async function selfTest')) : lines.length;
    lines.slice(0, stop).forEach((line, i) => {
      if (rootByHand(line) && !LEGIT.some((known) => known.startsWith(`${at}:`) && line.includes(known.slice(at.length + 1).trim()))) {
        byHand.push(`${at}:${i + 1}`);
      }
    });
  }
  const own = byHand.filter((at) => at.startsWith('tools/paths.mjs:'));
  const others = byHand.filter((at) => !at.startsWith('tools/paths.mjs:'));
  add('no tool or hook cuts a path out of its own URL by hand: rootFrom or fileURLToPath (gate can fail)',
    others.length === 0, others.join('; '));
  add('and this file does it once, inside rootFrom (gate can fail)', own.length === 1, own.join('; '));

  // The port, and the store comparison the hook's "is this ours" rests on.
  add('the port is 8059 unless C4X_API_PORT says otherwise',
    portFrom({}) === 8059 && portFrom({ C4X_API_PORT: '8061' }) === 8061);
  add('a malformed C4X_API_PORT falls back rather than probing port NaN',
    portFrom({ C4X_API_PORT: 'abc' }) === 8059 && portFrom({ C4X_API_PORT: '-1' }) === 8059);
  add('two spellings of one relative path are the same store',
    sameStorePath('tmp/x.db', './tmp/../tmp/x.db'));
  add('two different files are not (gate can fail)', !sameStorePath('tmp/x.db', 'tmp/y.db'));
  add('an empty side is never a match', !sameStorePath('', 'tmp/x.db') && !sameStorePath(null, null));
  if (process.platform === 'win32') {
    // The exact pair that never matched: Python's as_posix() against node's join().
    add('on Windows, backslashes and drive-letter case do not make two stores',
      sameStorePath(['P:', 'x', 'data', 'context.db'].join(String.fromCharCode(92)), 'p:/x/data/context.db'));
  }

  // The probe, against real sockets: the four answers the hook and the helper act on.
  {
    const serve = (body, status = 200) => new Promise((ready) => {
      const server = http.createServer((req, res) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(body); });
      server.listen(0, '127.0.0.1', () => ready(server));
    });
    const store = join(rootFrom(import.meta.url), 'tmp', 'probe-store.db');
    const ours = await serve(JSON.stringify({ ok: true, db: posix(store), port: 1, store_exists: false }));
    const theirs = await serve(JSON.stringify({ ok: true, db: 'Q:/elsewhere/context.db' }));
    const junk = await serve('<html>not a dashboard</html>');
    const a = await probeHealth(ours.address().port, store);
    add('our own server answers ours, with the store spelled the other way',
      a.answered === true && a.ours === true && a.storeExists === false, JSON.stringify(a));
    const b = await probeHealth(theirs.address().port, store);
    add('another store answers as not ours and names itself', b.answered === true && b.ours === false && b.db === 'Q:/elsewhere/context.db');
    const c = await probeHealth(junk.address().port, store);
    add('a listener that is not a dashboard is answered, not ours, no db', c.answered === true && c.ours === false && c.db === null);
    for (const s of [ours, theirs, junk]) s.close();
    const closed = await new Promise((ready) => { const s = http.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => ready(p)); }); });
    const d = await probeHealth(closed, store);
    add('a closed port is unanswered (gate can fail)', d.answered === false);
    const { createServer } = await import('node:net');
    const silent = createServer(() => { /* accept and say nothing */ });
    await new Promise((ready) => silent.listen(0, '127.0.0.1', ready));
    const t0 = Date.now();
    const e = await probeHealth(silent.address().port, store, { timeoutMs: 300 });
    add('a listener that never answers is unanswered within the timeout', e.answered === false && Date.now() - t0 < 2000, `${Date.now() - t0} ms`);
    silent.close();
  }

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
if (IS_ENTRY && process.argv.includes('--self-test')) process.exit(await selfTest());

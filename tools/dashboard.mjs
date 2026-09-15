#!/usr/bin/env node
// dashboard.mjs - start the API for the SessionStart hook, off the hook's clock.
//
// Why this exists: the hook that wants the page up has ten seconds, and one python import of the
// dashboard's modules costs three of them on a warm machine (measured: 3.2 s for `py -3`, 3.6 s
// for `python`). Three candidates and a cold disk do not fit, and a hook that Claude Code kills
// at its timeout writes no cache and repeats the same stall on every session. So the hook does
// one bounded thing, a 1 s probe of the port, and spawns THIS, detached, to do the rest.
//
//   node dashboard.mjs launch [--port N]    debounce, probe again, resolve a launcher, spawn
//   node dashboard.mjs resolve [--json]     which launcher this install would use, and why
//   node dashboard.mjs probe [--json]       what answers /__health__ on the port
//   node dashboard.mjs stop [--json]        POST /__shutdown__ with the token from the log
//   node dashboard.mjs --self-test
//
// The launcher, in order: C4X_DASHBOARD_CMD (a path to an executable); the install receipt's;
// the cache in data/raw (a cached "none" too, so a machine with nothing pays the probe once a
// week rather than once a session); the first of `py -3`, `python`, `python3` that imports the
// dashboard's modules, recorded as the INTERPRETER'S PATH; the built exe under dist/c4x. Python
// beats the exe when both work, so a stale exe beside a checkout never shadows the source.
//
// THE PATH, NOT THE LAUNCHER, and on Windows pythonw. The first build spawned `py -3` with
// windowsHide, and the hide landed on py.exe; py.exe then started python.exe, a console program,
// from a parent with no console, and Windows gave it a new one: a python window on the desktop for
// as long as the server ran (on the test laptop, py.exe pid 16076 and its python.exe 16264, both
// alive). Spawning the interpreter itself removes the indirection; pythonw.exe, which has no
// console at all, removes the class. Its stdout and stderr still reach data/raw/dashboard.log
// through the spawn's fds, since a redirected handle is a valid sys.stdout.
//
// The server's stdout and stderr go to data/raw/dashboard.log. The shutdown token is printed
// there and nowhere else, so `install status` can show how to stop a server the hook started,
// `install uninstall` can stop it, and a child that died at startup leaves its reason behind.

import { closeSync, existsSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import http from 'node:http';
import { pathToFileURL } from 'node:url';
import { rootFrom, defaultDb, ensureStoreDir, posix, portFrom, probeHealth } from './paths.mjs';
import { record } from '../hooks/event-hook.mjs';

const ROOT = rootFrom(import.meta.url);
const RAW = join(ROOT, 'data', 'raw');
export const STAMP = join(RAW, '.dashboard-launch');
export const CACHE = join(RAW, '.dashboard-launcher.json');
export const LOG = join(RAW, 'dashboard.log');
const RECEIPT = process.env.C4X_RECEIPT || join(ROOT, 'data', 'install-receipt.json');

export const DEBOUNCE_MS = 20_000;
export const CACHE_MS = 7 * 24 * 60 * 60 * 1000;
export const PROBE_TIMEOUT_MS = 8000;
// c4x.api.main, not c4x.api: the package's __init__ is a docstring, so importing it proves
// nothing. main builds every route, which pulls in the store (pandas) and the upload route
// (python-multipart), the two imports a python with only fastapi installed fails on.
export const PROBE_IMPORT = 'import c4x.api.main, uvicorn, dash, psutil, sys; print(sys.executable)';

export function candidatesFor(platform = process.platform) {
  return platform === 'win32' ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
}

export function exePath(root = ROOT, platform = process.platform) {
  return join(root, 'dist', 'c4x', platform === 'win32' ? 'c4x.exe' : 'c4x');
}

// Every launcher is a path now, python included, and a path that is gone is not a launcher. A
// receipt or cache from the first build carries `['py', '-3']`, no such file, so it is re-resolved
// once and rewritten in the new shape.
const usable = (l, exists) => Boolean(l && Array.isArray(l.cmd) && l.cmd.length && exists(l.cmd[0]));

/**
 * Which launcher, and why. Pure: every input is a parameter, so the self-test drives all six
 * outcomes without a python, a receipt or a filesystem.
 *
 * Returns { launcher: { cmd, module, kind } | null, why, fromCache }. `module` says whether
 * `-m c4x.api` follows the command; an exe takes the flags directly.
 */
export function dashboardLauncher({ env = {}, receipt = null, cache = null, now = Date.now(),
                                    platform = process.platform, root = ROOT, exists = existsSync,
                                    tryPython = () => false } = {}) {
  const override = env.C4X_DASHBOARD_CMD;
  if (override) {
    if (exists(override)) {
      return { launcher: { cmd: [override], module: false, kind: 'env' }, why: 'C4X_DASHBOARD_CMD', fromCache: false };
    }
    return { launcher: null, why: `C4X_DASHBOARD_CMD points at ${posix(override)}, which does not exist`, fromCache: false };
  }
  const recorded = receipt?.dashboardLauncher?.launcher;
  if (usable(recorded, exists)) return { launcher: recorded, why: 'install receipt', fromCache: false };
  if (cache && typeof cache.checkedAt === 'number' && now - cache.checkedAt < CACHE_MS
      && (cache.launcher === null || usable(cache.launcher, exists))) {
    return { launcher: cache.launcher, why: cache.why || 'cached', fromCache: true };
  }
  for (const cmd of candidatesFor(platform)) {
    // tryPython answers with the interpreter's path (sys.executable), or null. The path is what
    // gets spawned; the candidate that found it is kept as `via` for the status line.
    const interpreter = tryPython(cmd);
    if (typeof interpreter === 'string' && interpreter) {
      return { launcher: { cmd: [interpreter], module: true, kind: 'python', via: cmd.join(' ') },
               why: `${cmd.join(' ')} imports the dashboard (${posix(interpreter)})`, fromCache: false };
    }
  }
  const exe = exePath(root, platform);
  if (exists(exe)) return { launcher: { cmd: [exe], module: false, kind: 'exe' }, why: 'the built executable', fromCache: false };
  return { launcher: null, fromCache: false,
           why: 'no python imports the dashboard (pip install -r requirements.txt) and there is no '
              + `executable at ${posix(exe)}` };
}

/** The interpreter with no console, when it is beside the one that was found. Windows only. */
export function windowless(interpreter, { exists = existsSync, platform = process.platform } = {}) {
  if (platform !== 'win32' || !/python\.exe$/i.test(interpreter)) return interpreter;
  const quiet = interpreter.replace(/python\.exe$/i, 'pythonw.exe');
  return exists(quiet) ? quiet : interpreter;
}

/**
 * The server's command line. `sweep: false` (the receipt's `reviewSweep: false`, recorded by
 * `install --no-review-sweep`) adds `--no-review-sweep`, so the server neither takes back
 * review-run records at startup nor restarts Claude; the fold itself is untouched by the flag.
 */
export function launchArgv(launcher, { db, port, sweep = true, exists = existsSync, platform = process.platform }) {
  const head = launcher.kind === 'python' ? windowless(launcher.cmd[0], { exists, platform }) : launcher.cmd[0];
  return [head, ...launcher.cmd.slice(1), ...(launcher.module ? ['-m', 'c4x.api'] : []),
          '--db', db, '--port', String(port), '--watchdog', ...(sweep === false ? ['--no-review-sweep'] : [])];
}

/** The path of the interpreter this command runs, when it imports the dashboard's modules; else null. */
export function pythonImports(cmd, { root = ROOT, timeoutMs = PROBE_TIMEOUT_MS } = {}) {
  const r = spawnSync(cmd[0], [...cmd.slice(1), '-c', PROBE_IMPORT],
                      { cwd: root, timeout: timeoutMs, encoding: 'utf8', windowsHide: true });
  if (r.status !== 0 || r.error) return null;
  const path = String(r.stdout || '').trim().split(/\r?\n/).pop();
  return path ? path : null;
}

function readJson(path) {
  try { return JSON.parse(readFileSync(path, 'utf8')); } catch { return null; }
}

/** The launcher for THIS install, cached whatever the outcome unless it came from the receipt or the environment. */
export function resolveLauncher({ env = process.env, now = Date.now(), useCache = true } = {}) {
  const answer = dashboardLauncher({
    env, receipt: readJson(RECEIPT), cache: useCache ? readJson(CACHE) : null, now,
    tryPython: (cmd) => pythonImports(cmd),
  });
  if (!answer.fromCache && answer.why !== 'install receipt' && !env.C4X_DASHBOARD_CMD) {
    try {
      ensureStoreDir(RAW);
      writeFileSync(CACHE, JSON.stringify({ launcher: answer.launcher, why: answer.why, checkedAt: now }) + '\n');
    } catch { /* a cache that cannot be written costs one probe per session, nothing else */ }
  }
  return answer;
}

/** True while a stamp younger than the window exists: another session is starting the server. */
export function debounced(stampPath = STAMP, now = Date.now(), windowMs = DEBOUNCE_MS) {
  try { return now - statSync(stampPath).mtimeMs < windowMs; } catch { return false; }
}

/**
 * Take the launch for this process, atomically. Returns true for the one launch that may spawn.
 *
 * NOT check-then-write. Two sessions started in the same second on 2026-09-14, both hooks found no
 * stamp, both spawned a server: one bound the port, the other died on it (Errno 10048) after
 * truncating the shared log, and the log then held the dead one's token, so `stop` got a 403 from
 * the live one. `wx` creates the file or fails because it exists, in one step the filesystem
 * decides; a stamp older than the window is a crashed launch and is taken over.
 */
export function claim(stampPath = STAMP, now = Date.now(), windowMs = DEBOUNCE_MS) {
  ensureStoreDir(RAW);
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = openSync(stampPath, 'wx');
      writeFileSync(fd, new Date(now).toISOString());
      closeSync(fd);
      return true;
    } catch (e) {
      if (e.code !== 'EEXIST') return false;
      if (!debounced(stampPath, now, windowMs)) {
        try { unlinkSync(stampPath); } catch { return false; }
        continue;
      }
      return false;
    }
  }
  return false;
}

/** The shutdown token out of the server's own announce line, or null. */
/**
 * The NEWEST token in the log. A restart from the page (`c4x/server.py restart_server`) appends
 * its replacement's output to the same log, so the log then holds two tokens and only the last
 * one belongs to a server that is still there; the first match sent `stop` a dead server's token.
 */
export function tokenFrom(logText) {
  const all = [...String(logText ?? '').matchAll(/X-C4X-Shutdown:\s*([^"\s]+)/g)];
  return all.length ? all[all.length - 1][1] : null;
}

export function readLog(path = LOG) {
  try { return readFileSync(path, 'utf8'); } catch { return ''; }
}

/**
 * Spawn the server with its output in a log OF ITS OWN, then keep that log as `dashboard.log`
 * only once the port answers: a server that lost the port to another dies after writing its token,
 * and a shared log written by the loser is what sent `stop` to the winner with the wrong token.
 */
function spawnServer(argv) {
  ensureStoreDir(RAW);
  const own = `${LOG}.${process.pid}`;
  const fd = openSync(own, 'w');
  try {
    const child = spawn(argv[0], argv.slice(1),
                        { cwd: ROOT, detached: true, stdio: ['ignore', fd, fd], windowsHide: true });
    child.unref();
  } finally {
    closeSync(fd);
  }
  return own;
}

/** Wait for the server to answer, then promote its log; a server that never answers keeps its own log for reading. */
export async function settle(ownLog, port, db, { probe = probeHealth, tries = 60, everyMs = 500, promote = renameSync } = {}) {
  for (let i = 0; i < tries; i++) {
    const answer = await probe(port, db, { timeoutMs: 1000 });
    if (answer.answered && answer.ours) {
      try { promote(ownLog, LOG); } catch { /* the own log stays readable under its own name */ }
      return { up: true, tries: i + 1 };
    }
    await new Promise((r) => setTimeout(r, everyMs));
  }
  return { up: false, tries };
}

/**
 * Start the server unless one is already there. Every side effect is an `io` seam so the
 * self-test drives all five outcomes; the real run passes nothing.
 */
export async function launch({ port = portFrom(), env = process.env, now = Date.now(), io = {} } = {}) {
  const { probe = probeHealth, spawnIt = spawnServer, log = record, stampPath = STAMP,
          take = claim, resolve = resolveLauncher, wait = settle } = io;
  const db = env.C4X_DB || defaultDb(ROOT);
  if (!take(stampPath, now)) return { did: 'skipped', why: 'another session is starting it' };
  // The hook probed seconds ago; a server it did not see may have answered since.
  const answer = await probe(port, db, { timeoutMs: 1000 });
  if (answer.answered && answer.ours) return { did: 'skipped', why: `already answering on ${port}` };
  if (answer.answered) {
    const holder = answer.db || 'something that is not a c4x dashboard';
    log({ hook_event_name: 'SessionStart', reason: `c4x dashboard: ${port} held by ${holder}` });
    return { did: 'held', why: holder };
  }
  const { launcher, why } = resolve({ env, now });
  if (!launcher) {
    log({ hook_event_name: 'SessionStart', reason: `c4x dashboard: not started: ${why}` });
    return { did: 'none', why };
  }
  const argv = launchArgv(launcher, { db, port, sweep: readJson(RECEIPT)?.reviewSweep !== false });
  const ownLog = spawnIt(argv);
  log({ hook_event_name: 'SessionStart', reason: `c4x dashboard: started ${posix(argv[0])} (${launcher.kind}) on ${port}` });
  const settled = ownLog ? await wait(ownLog, port, db, { probe }) : { up: null };
  if (settled.up === false) {
    log({ hook_event_name: 'SessionStart', reason: `c4x dashboard: ${posix(argv[0])} did not answer on ${port}; its output is in ${posix(ownLog)}` });
  }
  return { did: 'started', argv, launcher, up: settled.up };
}

/**
 * POST /__shutdown__ with the token the server printed into the log. Resolves, never rejects.
 *
 * A LOST REPLY IS CHECKED, NOT REPORTED. The server answers the shutdown and exits a quarter of a
 * second later, and that reply can be cut on its way out: seen here as "no answer" from a server
 * that was already gone. So when the request ends without a 200, the port is probed once more,
 * and nothing answering is the stop that was asked for.
 */
export async function stop({ port = portFrom(), token = tokenFrom(readLog()), timeoutMs = 3000,
                             probe = probeHealth } = {}) {
  const first = await request({ port, token, timeoutMs });
  if (first.stopped || !token) return first;
  const after = await probe(port, '', { timeoutMs: 1000 });
  if (!after.answered) return { stopped: true, why: `stopped (${first.why}, and nothing answers now)` };
  return first;
}

function request({ port, token, timeoutMs }) {
  return new Promise((done) => {
    if (!token) {
      return done({ stopped: false, why: `no shutdown token in ${posix(LOG)}: stop it with taskkill, or wait for the watchdog` });
    }
    let settled = false;
    const finish = (a) => { if (!settled) { settled = true; done(a); } };
    const req = http.request({ host: '127.0.0.1', port, path: '/__shutdown__?reason=install+uninstall',
                               method: 'POST', timeout: timeoutMs, headers: { 'X-C4X-Shutdown': token } },
    (res) => {
      res.resume();
      res.on('end', () => finish(res.statusCode === 200
        ? { stopped: true, why: 'stopped' }
        : { stopped: false, why: `the server answered ${res.statusCode}` }));
    });
    req.on('timeout', () => { req.destroy(); finish({ stopped: false, why: 'no answer' }); });
    req.on('error', (e) => finish({ stopped: false, why: e.code || e.message }));
    req.end();
  });
}

// ---------------------------------------------------------------------------
async function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);
  const R = 'X:/fake/root';
  const yes = () => true;
  const no = () => false;
  const win = { platform: 'win32', root: R };
  // tryPython stubs answer as the real one does: the interpreter's path, or null.
  const PY = 'X:/py314/python.exe';
  const found = () => PY;
  const notFound = () => null;

  // The launcher decision, every branch.
  const env = dashboardLauncher({ ...win, env: { C4X_DASHBOARD_CMD: 'X:/tool/c4x-api.exe' }, exists: yes });
  add('C4X_DASHBOARD_CMD wins and takes the flags directly',
    env.launcher?.kind === 'env' && env.launcher.module === false && env.launcher.cmd[0] === 'X:/tool/c4x-api.exe');
  add('a C4X_DASHBOARD_CMD that does not exist is a reason, not a launcher',
    dashboardLauncher({ ...win, env: { C4X_DASHBOARD_CMD: 'X:/gone.exe' }, exists: no }).launcher === null);
  const receipt = { dashboardLauncher: { launcher: { cmd: [PY], module: true, kind: 'python', via: 'py -3' } } };
  const fromReceipt = dashboardLauncher({ ...win, receipt, exists: (p) => p === PY, tryPython: notFound });
  add('the receipt\u0027s python launcher is used without probing again',
    fromReceipt.why === 'install receipt' && fromReceipt.launcher.cmd.join(' ') === PY);
  const oldShape = { dashboardLauncher: { launcher: { cmd: ['py', '-3'], module: true, kind: 'python' } } };
  const reResolved = dashboardLauncher({ ...win, receipt: oldShape, exists: (p) => p === PY, tryPython: found });
  add('a first-build receipt naming the py launcher is re-resolved to a path (gate can fail)',
    reResolved.why !== 'install receipt' && reResolved.launcher?.cmd[0] === PY);
  const staleExe = { dashboardLauncher: { launcher: { cmd: ['X:/old/c4x.exe'], module: false, kind: 'exe' } } };
  add('a receipt naming an exe that is gone is skipped (gate can fail)',
    dashboardLauncher({ ...win, receipt: staleExe, exists: no, tryPython: notFound }).launcher === null);
  const now = 1_000_000_000_000;
  const cachedNone = { launcher: null, why: 'nothing last week', checkedAt: now - 1000 };
  const c1 = dashboardLauncher({ ...win, cache: cachedNone, now, exists: yes, tryPython: found });
  add('a cached "none" is honoured, so a miss is paid once a week not once a session',
    c1.launcher === null && c1.fromCache === true && c1.why === 'nothing last week');
  const expired = { ...cachedNone, checkedAt: now - CACHE_MS - 1 };
  add('an expired cache is probed again', dashboardLauncher({ ...win, cache: expired, now, exists: no, tryPython: found }).launcher?.kind === 'python');
  const py = dashboardLauncher({ ...win, exists: yes, tryPython: (cmd) => (cmd[0] === 'python' ? PY : null) });
  add('the first python that imports the dashboard wins, in order, recorded as its path',
    py.launcher?.kind === 'python' && py.launcher.cmd.join(' ') === PY && py.launcher.module === true
      && py.launcher.via === 'python');
  add('the launcher is never the py launcher itself (gate can fail)',
    !dashboardLauncher({ ...win, exists: yes, tryPython: found }).launcher.cmd[0].startsWith('py'));
  add('python beats an exe that is also there (gate can fail)',
    dashboardLauncher({ ...win, exists: yes, tryPython: found }).launcher?.kind === 'python');
  const exe = dashboardLauncher({ ...win, exists: (p) => p === exePath(R, 'win32'), tryPython: notFound });
  add('with no python the built exe is used', exe.launcher?.kind === 'exe' && exe.launcher.module === false);
  add('the exe is dist/c4x/c4x.exe on Windows and dist/c4x/c4x elsewhere',
    posix(exePath(R, 'win32')).endsWith('/dist/c4x/c4x.exe') && posix(exePath('/r', 'linux')).endsWith('/dist/c4x/c4x'));
  const none = dashboardLauncher({ ...win, exists: no, tryPython: notFound });
  add('with neither the answer is null and the reason names both', none.launcher === null
    && none.why.includes('requirements.txt') && none.why.includes('dist/c4x'));
  add('posix candidates never try the py launcher', !candidatesFor('linux').some((c) => c[0] === 'py'));

  const pyL = { cmd: [PY], module: true, kind: 'python' };
  add('the server argv carries the store, the port and the watchdog',
    launchArgv(pyL, { db: 'D:/s.db', port: 8061, exists: no, platform: 'win32' }).join(' ')
      === `${PY} -m c4x.api --db D:/s.db --port 8061 --watchdog`);
  add('on Windows pythonw beside the interpreter is spawned instead (gate can fail)',
    launchArgv(pyL, { db: 'D:/s.db', port: 8059, exists: (p) => p === 'X:/py314/pythonw.exe', platform: 'win32' })[0]
      === 'X:/py314/pythonw.exe');
  add('but only when it is there', windowless(PY, { exists: no, platform: 'win32' }) === PY);
  add('and never off Windows', windowless('/usr/bin/python3', { exists: yes, platform: 'linux' }) === '/usr/bin/python3');
  add('an exe takes the flags without -m and is never swapped',
    launchArgv({ cmd: ['X:/c4x.exe'], module: false, kind: 'exe' }, { db: 'D:/s.db', port: 8059, exists: yes, platform: 'win32' }).join(' ')
      === 'X:/c4x.exe --db D:/s.db --port 8059 --watchdog');
  add('a receipt that turned the review sweep off puts --no-review-sweep on the command line',
    launchArgv(pyL, { db: 'D:/s.db', port: 8059, sweep: false, exists: no, platform: 'win32' }).join(' ')
      === `${PY} -m c4x.api --db D:/s.db --port 8059 --watchdog --no-review-sweep`);
  add('and by default the flag is absent (gate can fail)',
    !launchArgv(pyL, { db: 'D:/s.db', port: 8059, exists: no, platform: 'win32' }).includes('--no-review-sweep'));
  add('the newest shutdown token in a log wins, a restart having appended a second server\'s',
    tokenFrom('X-C4X-Shutdown: first"\n[shutdown] restart\nX-C4X-Shutdown: second"\n') === 'second'
    && tokenFrom('nothing here') === null);
  add('an interpreter that does not exist is null, quickly',
    pythonImports(['c4x-no-such-interpreter-xyz'], { timeoutMs: 2000 }) === null);

  // The token, from the exact line c4x/server.py prints.
  const announce = '  stop it with: curl -X POST http://127.0.0.1:8059/__shutdown__ -H "X-C4X-Shutdown: abc-DEF_123"';
  add('the token is read out of the announce line', tokenFrom(announce) === 'abc-DEF_123');
  add('a log without one yields null', tokenFrom('c4x api on http://127.0.0.1:8059') === null);

  // The launch lock, on a real stamp: atomic, and taken over when stale.
  const stampPath = join(ROOT, 'tmp', `dashboard-stamp-${process.pid}`);
  ensureStoreDir(join(ROOT, 'tmp'));
  try { unlinkSync(stampPath); } catch { /* none yet */ }
  add('no stamp: not debounced', debounced(stampPath, Date.now()) === false);
  add('the first claim wins', claim(stampPath, Date.now()) === true);
  add('the second claim, at once, loses (gate can fail)', claim(stampPath, Date.now()) === false);
  add('a fresh stamp debounces (gate can fail)', debounced(stampPath, Date.now()) === true);
  add('a stale stamp is taken over', claim(stampPath, Date.now() + DEBOUNCE_MS + 5000) === true);
  // A margin of seconds, not a millisecond: the file's mtime comes from the filesystem clock and
  // Date.now() from the process, and on the Windows CI runner the former sat ahead of the latter
  // by more than 1 ms, which failed the suite once for a property that plainly held.
  add('an old stamp does not', debounced(stampPath, Date.now() + DEBOUNCE_MS + 5000) === false);

  // launch(), every outcome, with every side effect stubbed.
  const drive = async (answer, launcher, opts = {}) => {
    const spawned = []; const logged = []; let probed = 0;
    const out = await launch({ port: 8059, env: { C4X_DB: 'D:/s.db' }, now: opts.now ?? Date.now() + 60_000, io: {
      probe: async () => { probed++; return answer; },
      spawnIt: (argv) => { spawned.push(argv); return null; },
      log: (p) => logged.push(p.reason),
      stampPath, take: opts.take ?? (() => true),
      resolve: () => ({ launcher, why: launcher ? 'stub' : 'no launcher here' }),
    } });
    return { out, spawned, logged, probed };
  };
  const ours = await drive({ answered: true, ours: true, db: 'D:/s.db' }, pyL);
  add('a server of ours already up: nothing spawned', ours.out.did === 'skipped' && ours.spawned.length === 0);
  const held = await drive({ answered: true, ours: false, db: 'E:/other.db' }, pyL);
  add('another store on the port: nothing spawned and the holder is recorded',
    held.out.did === 'held' && held.spawned.length === 0 && held.logged[0]?.includes('held by E:/other.db'));
  const started = await drive({ answered: false }, pyL);
  add('refused plus a launcher: the server is spawned with --db, --port and --watchdog',
    started.out.did === 'started' && started.spawned.length === 1
      && started.spawned[0].slice(1).join(' ') === '-m c4x.api --db D:/s.db --port 8059 --watchdog'
      && /python(w)?\.exe$/i.test(started.spawned[0][0]));
  add('and the start is recorded', started.logged[0]?.startsWith('c4x dashboard: started X:/py314/python'));
  const nothing = await drive({ answered: false }, null);
  add('refused and no launcher: nothing spawned, the reason recorded',
    nothing.out.did === 'none' && nothing.spawned.length === 0 && nothing.logged[0]?.includes('not started: no launcher here'));
  const bounced = await drive({ answered: false }, pyL, { take: () => false });
  add('a lost claim skips everything, the probe included (gate can fail)',
    bounced.out.did === 'skipped' && bounced.probed === 0 && bounced.spawned.length === 0);

  // settle(): the log is promoted only once the port answers as ours.
  {
    const moves = [];
    const up = await settle('X:/raw/dashboard.log.1', 8059, 'D:/s.db',
      { probe: async () => ({ answered: true, ours: true }), promote: (a, b) => moves.push([a, b]), everyMs: 1 });
    add('a server that answers has its log promoted (gate can fail)', up.up === true && moves.length === 1 && moves[0][1] === LOG);
    const down = await settle('X:/raw/dashboard.log.2', 8059, 'D:/s.db',
      { probe: async () => ({ answered: false }), promote: (a, b) => moves.push([a, b]), tries: 3, everyMs: 1 });
    add('a server that never answers keeps its own log and is reported down', down.up === false && moves.length === 1);
    const other = await settle('X:/raw/dashboard.log.3', 8059, 'D:/s.db',
      { probe: async () => ({ answered: true, ours: false, db: 'E:/x.db' }), promote: (a, b) => moves.push([a, b]), tries: 2, everyMs: 1 });
    add('another store answering does not promote our log (gate can fail)', other.up === false && moves.length === 1);
  }
  try { const { rmSync } = await import('node:fs'); rmSync(stampPath, { force: true }); } catch { /* scratch */ }

  // stop() against a real socket: a refused port is a reason, a 200 is a stop.
  const server = http.createServer((req, res) => {
    const ok = req.method === 'POST' && req.headers['x-c4x-shutdown'] === 'tok';
    res.writeHead(ok ? 200 : 403); res.end(ok ? 'stopped' : 'refused');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const stillUp = async () => ({ answered: true, ours: true });
  add('stop with the right token is a stop', (await stop({ port, token: 'tok', probe: stillUp })).stopped === true);
  add('stop with the wrong token reports the refusal', (await stop({ port, token: 'nope', probe: stillUp })).why.includes('403'));
  add('stop with no token says where the token would be', (await stop({ port, token: null, probe: stillUp })).why.includes('dashboard.log'));
  // The reply is lost but the server is gone: a stop, said as one.
  const gone = async () => ({ answered: false, why: 'ECONNREFUSED' });
  const lost = await stop({ port, token: 'nope', probe: gone });
  add('a lost reply with nothing answering afterwards is reported as stopped (gate can fail)',
    lost.stopped === true && lost.why.includes('nothing answers now'));
  server.close();

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// ---------------------------------------------------------------------------
const IS_ENTRY = (() => {
  try {
    return process.argv[1] ? pathToFileURL(process.argv[1]).href === import.meta.url : false;
  } catch { return false; }
})();

if (IS_ENTRY) {
  const argv = process.argv.slice(2);
  const json = argv.includes('--json');
  const verb = argv.find((a) => !a.startsWith('--'));
  const portAt = argv.indexOf('--port');
  const port = portAt !== -1 && argv[portAt + 1] ? Number(argv[portAt + 1]) || portFrom() : portFrom();
  const out = (obj, line) => console.log(json ? JSON.stringify(obj) : line);
  if (argv.includes('--self-test')) {
    process.exit(await selfTest());
  } else if (verb === 'launch') {
    // Never fails loudly: a hook spawned this, and nothing is waiting on it.
    try { const r = await launch({ port }); out(r, `${r.did}: ${r.why ?? r.argv?.join(' ')}`); } catch (e) { console.error(`dashboard: ${e.message}`); }
    process.exit(0);
  } else if (verb === 'resolve') {
    const r = resolveLauncher({ useCache: !argv.includes('--fresh') });
    out(r, r.launcher ? `${r.launcher.cmd.join(' ')} (${r.launcher.kind}): ${r.why}` : `none: ${r.why}`);
    process.exit(0);
  } else if (verb === 'probe') {
    const db = process.env.C4X_DB || defaultDb(ROOT);
    const r = await probeHealth(port, db, { timeoutMs: 1500 });
    // The token rides along so `install status` can print how to stop a server the hook started
    // without importing this file (see install.mjs: that import would close a cycle).
    out({ port, db: posix(db), token: r.answered && r.ours ? tokenFrom(readLog()) : null, ...r },
        r.answered ? (r.ours ? `ours on ${port}` : `held by ${r.db ?? 'something else'}`) : `nothing on ${port}: ${r.why}`);
    process.exit(0);
  } else if (verb === 'stop') {
    const r = await stop({ port });
    out(r, r.why);
    process.exit(r.stopped ? 0 : 1);
  } else {
    console.log(readFileSync(new URL(import.meta.url), 'utf8').split('\n').filter((l) => l.startsWith('//')).slice(0, 22).map((l) => l.replace(/^\/\/ ?/, '')).join('\n'));
    process.exit(verb ? 2 : 0);
  }
}

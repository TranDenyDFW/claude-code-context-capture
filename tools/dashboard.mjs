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
// dashboard's modules; the built exe under dist/c4x-api. Python beats the exe when both work, so
// a stale exe beside a checkout never shadows the source.
//
// The server's stdout and stderr go to data/raw/dashboard.log. The shutdown token is printed
// there and nowhere else, so `install status` can show how to stop a server the hook started,
// `install uninstall` can stop it, and a child that died at startup leaves its reason behind.

import { closeSync, existsSync, openSync, readFileSync, statSync, writeFileSync } from 'node:fs';
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
export const PROBE_IMPORT = 'import c4x.api.main, uvicorn, dash, psutil';

export function candidatesFor(platform = process.platform) {
  return platform === 'win32' ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
}

export function exePath(root = ROOT, platform = process.platform) {
  return join(root, 'dist', 'c4x-api', platform === 'win32' ? 'c4x-api.exe' : 'c4x-api');
}

// A python launcher is trusted by name: it was probed when it was recorded, and `py` is on PATH
// rather than at a path. An exe or an override must still be where it was.
const usable = (l, exists) => Boolean(l && Array.isArray(l.cmd) && l.cmd.length
  && (l.kind === 'python' || exists(l.cmd[0])));

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
    if (tryPython(cmd)) {
      return { launcher: { cmd, module: true, kind: 'python' }, why: `${cmd.join(' ')} imports the dashboard`, fromCache: false };
    }
  }
  const exe = exePath(root, platform);
  if (exists(exe)) return { launcher: { cmd: [exe], module: false, kind: 'exe' }, why: 'the built executable', fromCache: false };
  return { launcher: null, fromCache: false,
           why: 'no python imports the dashboard (pip install -r requirements.txt) and there is no '
              + `executable at ${posix(exe)}` };
}

export function launchArgv(launcher, { db, port }) {
  return [...launcher.cmd, ...(launcher.module ? ['-m', 'c4x.api'] : []),
          '--db', db, '--port', String(port), '--watchdog'];
}

/** Does this interpreter import the dashboard's modules? Bounded; a hang is a no. */
export function pythonImports(cmd, { root = ROOT, timeoutMs = PROBE_TIMEOUT_MS } = {}) {
  const r = spawnSync(cmd[0], [...cmd.slice(1), '-c', PROBE_IMPORT],
                      { cwd: root, timeout: timeoutMs, stdio: 'ignore', windowsHide: true });
  return r.status === 0 && !r.error;
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

function writeStamp(stampPath = STAMP) {
  ensureStoreDir(RAW);
  writeFileSync(stampPath, new Date().toISOString());
}

/** The shutdown token out of the server's own announce line, or null. */
export function tokenFrom(logText) {
  const m = String(logText ?? '').match(/X-C4X-Shutdown:\s*([^"\s]+)/);
  return m ? m[1] : null;
}

export function readLog(path = LOG) {
  try { return readFileSync(path, 'utf8'); } catch { return ''; }
}

function spawnServer(argv) {
  ensureStoreDir(RAW);
  const fd = openSync(LOG, 'w');
  try {
    const child = spawn(argv[0], argv.slice(1),
                        { cwd: ROOT, detached: true, stdio: ['ignore', fd, fd], windowsHide: true });
    child.unref();
  } finally {
    closeSync(fd);
  }
}

/**
 * Start the server unless one is already there. Every side effect is an `io` seam so the
 * self-test drives all five outcomes; the real run passes nothing.
 */
export async function launch({ port = portFrom(), env = process.env, now = Date.now(), io = {} } = {}) {
  const { probe = probeHealth, spawnIt = spawnServer, log = record, stampPath = STAMP,
          stamp = writeStamp, resolve = resolveLauncher } = io;
  const db = env.C4X_DB || defaultDb(ROOT);
  if (debounced(stampPath, now)) return { did: 'skipped', why: 'another session is starting it' };
  stamp(stampPath);
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
  const argv = launchArgv(launcher, { db, port });
  spawnIt(argv);
  log({ hook_event_name: 'SessionStart', reason: `c4x dashboard: started ${posix(argv[0])} (${launcher.kind}) on ${port}` });
  return { did: 'started', argv, launcher };
}

/** POST /__shutdown__ with the token the server printed into the log. Resolves, never rejects. */
export function stop({ port = portFrom(), token = tokenFrom(readLog()), timeoutMs = 3000 } = {}) {
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

  // The launcher decision, every branch.
  const env = dashboardLauncher({ ...win, env: { C4X_DASHBOARD_CMD: 'X:/tool/c4x-api.exe' }, exists: yes });
  add('C4X_DASHBOARD_CMD wins and takes the flags directly',
    env.launcher?.kind === 'env' && env.launcher.module === false && env.launcher.cmd[0] === 'X:/tool/c4x-api.exe');
  add('a C4X_DASHBOARD_CMD that does not exist is a reason, not a launcher',
    dashboardLauncher({ ...win, env: { C4X_DASHBOARD_CMD: 'X:/gone.exe' }, exists: no }).launcher === null);
  const receipt = { dashboardLauncher: { launcher: { cmd: ['py', '-3'], module: true, kind: 'python' } } };
  const fromReceipt = dashboardLauncher({ ...win, receipt, exists: no, tryPython: no });
  add('the receipt\u0027s python launcher is used without probing again',
    fromReceipt.why === 'install receipt' && fromReceipt.launcher.cmd.join(' ') === 'py -3');
  const staleExe = { dashboardLauncher: { launcher: { cmd: ['X:/old/c4x-api.exe'], module: false, kind: 'exe' } } };
  add('a receipt naming an exe that is gone is skipped (gate can fail)',
    dashboardLauncher({ ...win, receipt: staleExe, exists: no, tryPython: no }).launcher === null);
  const now = 1_000_000_000_000;
  const cachedNone = { launcher: null, why: 'nothing last week', checkedAt: now - 1000 };
  const c1 = dashboardLauncher({ ...win, cache: cachedNone, now, exists: yes, tryPython: yes });
  add('a cached "none" is honoured, so a miss is paid once a week not once a session',
    c1.launcher === null && c1.fromCache === true && c1.why === 'nothing last week');
  const expired = { ...cachedNone, checkedAt: now - CACHE_MS - 1 };
  add('an expired cache is probed again', dashboardLauncher({ ...win, cache: expired, now, exists: no, tryPython: yes }).launcher?.kind === 'python');
  const py = dashboardLauncher({ ...win, exists: yes, tryPython: (cmd) => cmd[0] === 'python' });
  add('the first python that imports the dashboard wins, in order',
    py.launcher?.kind === 'python' && py.launcher.cmd.join(' ') === 'python' && py.launcher.module === true);
  add('python beats an exe that is also there (gate can fail)',
    dashboardLauncher({ ...win, exists: yes, tryPython: yes }).launcher?.kind === 'python');
  const exe = dashboardLauncher({ ...win, exists: (p) => p === exePath(R, 'win32'), tryPython: no });
  add('with no python the built exe is used', exe.launcher?.kind === 'exe' && exe.launcher.module === false);
  const none = dashboardLauncher({ ...win, exists: no, tryPython: no });
  add('with neither the answer is null and the reason names both', none.launcher === null
    && none.why.includes('requirements.txt') && none.why.includes('dist/c4x-api'));
  add('posix candidates never try the py launcher', !candidatesFor('linux').some((c) => c[0] === 'py'));

  add('the server argv carries the store, the port and the watchdog',
    launchArgv({ cmd: ['py', '-3'], module: true }, { db: 'D:/s.db', port: 8061 }).join(' ')
      === 'py -3 -m c4x.api --db D:/s.db --port 8061 --watchdog');
  add('an exe takes the flags without -m',
    launchArgv({ cmd: ['X:/c4x-api.exe'], module: false }, { db: 'D:/s.db', port: 8059 }).join(' ')
      === 'X:/c4x-api.exe --db D:/s.db --port 8059 --watchdog');
  add('an interpreter that does not exist is a no, quickly',
    pythonImports(['c4x-no-such-interpreter-xyz'], { timeoutMs: 2000 }) === false);

  // The token, from the exact line c4x/server.py prints.
  const announce = '  stop it with: curl -X POST http://127.0.0.1:8059/__shutdown__ -H "X-C4X-Shutdown: abc-DEF_123"';
  add('the token is read out of the announce line', tokenFrom(announce) === 'abc-DEF_123');
  add('a log without one yields null', tokenFrom('c4x api on http://127.0.0.1:8059') === null);

  // The debounce, on a real stamp.
  const stampPath = join(ROOT, 'tmp', `dashboard-stamp-${process.pid}`);
  ensureStoreDir(join(ROOT, 'tmp'));
  add('no stamp: not debounced', debounced(stampPath, Date.now()) === false);
  writeFileSync(stampPath, 'x');
  add('a fresh stamp debounces (gate can fail)', debounced(stampPath, Date.now()) === true);
  add('an old stamp does not', debounced(stampPath, Date.now() + DEBOUNCE_MS + 1) === false);

  // launch(), every outcome, with every side effect stubbed.
  const drive = async (answer, launcher, opts = {}) => {
    const spawned = []; const logged = []; let probed = 0;
    const out = await launch({ port: 8059, env: { C4X_DB: 'D:/s.db' }, now: opts.now ?? Date.now() + 60_000, io: {
      probe: async () => { probed++; return answer; },
      spawnIt: (argv) => spawned.push(argv),
      log: (p) => logged.push(p.reason),
      stampPath, stamp: () => {},
      resolve: () => ({ launcher, why: launcher ? 'stub' : 'no launcher here' }),
    } });
    return { out, spawned, logged, probed };
  };
  const pyL = { cmd: ['python'], module: true, kind: 'python' };
  const ours = await drive({ answered: true, ours: true, db: 'D:/s.db' }, pyL);
  add('a server of ours already up: nothing spawned', ours.out.did === 'skipped' && ours.spawned.length === 0);
  const held = await drive({ answered: true, ours: false, db: 'E:/other.db' }, pyL);
  add('another store on the port: nothing spawned and the holder is recorded',
    held.out.did === 'held' && held.spawned.length === 0 && held.logged[0]?.includes('held by E:/other.db'));
  const started = await drive({ answered: false }, pyL);
  add('refused plus a launcher: the server is spawned with --db, --port and --watchdog',
    started.out.did === 'started' && started.spawned.length === 1
      && started.spawned[0].join(' ') === 'python -m c4x.api --db D:/s.db --port 8059 --watchdog');
  add('and the start is recorded', started.logged[0]?.startsWith('c4x dashboard: started python'));
  const nothing = await drive({ answered: false }, null);
  add('refused and no launcher: nothing spawned, the reason recorded',
    nothing.out.did === 'none' && nothing.spawned.length === 0 && nothing.logged[0]?.includes('not started: no launcher here'));
  const bounced = await drive({ answered: false }, pyL, { now: Date.now() });
  add('a fresh stamp skips everything, the probe included (gate can fail)',
    bounced.out.did === 'skipped' && bounced.probed === 0 && bounced.spawned.length === 0);
  try { const { rmSync } = await import('node:fs'); rmSync(stampPath, { force: true }); } catch { /* scratch */ }

  // stop() against a real socket: a refused port is a reason, a 200 is a stop.
  const server = http.createServer((req, res) => {
    const ok = req.method === 'POST' && req.headers['x-c4x-shutdown'] === 'tok';
    res.writeHead(ok ? 200 : 403); res.end(ok ? 'stopped' : 'refused');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  add('stop with the right token is a stop', (await stop({ port, token: 'tok' })).stopped === true);
  add('stop with the wrong token reports the refusal', (await stop({ port, token: 'nope' })).why.includes('403'));
  add('stop with no token says where the token would be', (await stop({ port, token: null })).why.includes('dashboard.log'));
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

#!/usr/bin/env node
// otel-gate.mjs - does OTEL_LOG_RAW_API_BODIES=file:<dir> actually write tool schemas to disk?
//
// ANSWERED 2026-08-22 on 2.1.237: yes. 18 tools and 91.8 KB of schema in a 205.1 KB request body,
// with NO collector configured. The README previously claimed this detail existed on no route,
// naming deferredBuiltinTools and systemTools because get_context_usage returns them as `void 0`;
// that claim has been corrected and now scopes itself to the get_context_usage route.
//
// The tool remains useful: it is how the answer is REPRODUCED, and how a future build that changes
// or withdraws the route gets caught rather than assumed.
//
// What is already known, read out of 2.1.237 rather than taken from docs:
//   - OTEL_LOG_RAW_API_BODIES, CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH, api_request_body and body_ref
//     are all present in the binary
//   - the mode gate reads process.env.OTEL_LOG_RAW_API_BODIES ALONE, and file mode calls writeFile
//     with an mkdir fallback, consulting no exporter, so it appears not to need a collector
//
// What is NOT known: whether a body is written in practice. Answering it needs one real API
// request, and on a desktop-app box `claude -p` cannot authenticate until `claude setup-token` has
// been run once in a real terminal. THAT IS A USER ACTION. This tool never attempts it.
//
// The distinction this tool exists to preserve: "no bodies were written" and "the check could not
// run" are different answers, and only one of them is evidence. An auth failure is reported as
// BLOCKED, never as a negative result.
//
//   node otel-gate.mjs --check      does the CLI have usable credentials? no API call, no spend
//   node otel-gate.mjs --run        the real gate: one cheap prompt, then inspect the bodies
//   node otel-gate.mjs --self-test

import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { homedir } from 'node:os';

const ROOT = join(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..');
const BODY_DIR = join(ROOT, 'tmp', 'otel-bodies');

/**
 * Classify a `claude -p` result WITHOUT deciding the gate.
 *
 * Returns one of: 'ok', 'blocked-auth', 'blocked-other'. The whole point is that an auth failure
 * must never be reportable as "the route does not work".
 */
export function classifyRun({ status, stdout = '', stderr = '' }) {
  const text = `${stdout}\n${stderr}`;
  if (/Failed to authenticate|OAuth session expired|Please run \/login|Not logged in/i.test(text)) {
    return 'blocked-auth';
  }
  if (status !== 0) return 'blocked-other';
  return 'ok';
}

/** Does a written request body actually carry the thing we are after? */
export function inspectBody(json) {
  let d;
  try { d = JSON.parse(json); } catch { return { parsed: false }; }
  const tools = Array.isArray(d.tools) ? d.tools : null;
  return {
    parsed: true,
    hasTools: Boolean(tools && tools.length),
    toolCount: tools ? tools.length : 0,
    toolNames: tools ? tools.slice(0, 8).map((t) => t?.name).filter(Boolean) : [],
    hasSystem: d.system !== undefined,
    messageCount: Array.isArray(d.messages) ? d.messages.length : null,
    model: d.model ?? null,
    // The schemas are the payload that matters, so measure them rather than the whole body.
    toolSchemaBytes: tools
      ? tools.reduce((a, t) => a + Buffer.byteLength(JSON.stringify(t), 'utf8'), 0) : 0,
  };
}

function credentialState() {
  const p = join(homedir(), '.claude', '.credentials.json');
  if (!existsSync(p)) return { present: false, reason: 'no .credentials.json' };
  let d;
  try { d = JSON.parse(readFileSync(p, 'utf8')); } catch { return { present: false, reason: 'unparseable' }; }
  const o = d.claudeAiOauth;
  if (!o) return { present: false, reason: 'no claudeAiOauth section' };
  const exp = Number(o.expiresAt) || 0;
  return {
    present: true,
    expiresAt: exp,
    expired: exp <= Date.now(),
    // Never print token values. Presence and expiry are the whole diagnostic.
    hasAccess: Boolean(o.accessToken),
    hasRefresh: Boolean(o.refreshToken),
  };
}

/**
 * Is a long-lived token visible in THIS process's environment?
 *
 * `claude setup-token` does NOT write to .credentials.json. It prints a token and leaves storing
 * it to the operator, for use as CLAUDE_CODE_OAUTH_TOKEN. The first version of this tool checked
 * only the credential file, so it would have reported NOT USABLE forever on a machine where auth
 * was working perfectly well through the environment. Checking one of two mechanisms and reporting
 * a verdict on both is the same shape of error this whole tool exists to avoid.
 *
 * Presence and shape only. The value is never read into a message, printed, or logged.
 */
export function envTokenState(env = process.env) {
  const v = env.CLAUDE_CODE_OAUTH_TOKEN;
  if (!v) return { present: false };
  return {
    present: true,
    length: v.length,
    // Shape check only. A truncated paste is a common and otherwise silent failure.
    looksLikeToken: /^sk-ant-oat/.test(v),
  };
}

function check() {
  const c = credentialState();
  const e = envTokenState();
  console.log('Auth state (no values printed, no API call made)');
  console.log('');
  console.log('  route 1, CLAUDE_CODE_OAUTH_TOKEN in this process environment');
  if (e.present) {
    console.log(`    present, ${e.length} chars, expected prefix: ${e.looksLikeToken}`);
  } else {
    console.log('    not set');
  }
  console.log('');
  console.log('  route 2, ~/.claude/.credentials.json claudeAiOauth');
  if (c.present) {
    console.log(`    section present. accessToken: ${c.hasAccess}   refreshToken: ${c.hasRefresh}`);
    console.log(`    expiresAt: ${c.expiresAt || 0}${c.expired ? '  EXPIRED' : '  valid'}`);
  } else {
    console.log(`    ${c.reason}`);
  }

  const viaEnv = e.present && e.looksLikeToken;
  const viaFile = c.present && c.hasAccess && !c.expired;
  console.log('');
  if (viaEnv || viaFile) {
    console.log(`LIKELY USABLE via ${viaEnv ? 'the environment token' : 'the credential file'}.`);
    console.log('Run: node tools/otel-gate.mjs --run');
    console.log('Still a heuristic, not proof. Only --run actually answers it.');
    return 0;
  }
  console.log('NOT USABLE from THIS process.');
  console.log('');
  console.log('`claude setup-token` prints a token and does NOT persist it: storing it is yours.');
  console.log('Exporting it in one shell makes it visible to THAT shell only. To make it visible');
  console.log('to other processes on Windows, set it at User scope, then start a NEW shell:');
  console.log('  [Environment]::SetEnvironmentVariable("CLAUDE_CODE_OAUTH_TOKEN", "<token>", "User")');
  console.log('Or simply run --run yourself from the shell where it is already exported.');
  return 1;
}

function run() {
  rmSync(BODY_DIR, { recursive: true, force: true });
  mkdirSync(BODY_DIR, { recursive: true });

  const env = {
    ...process.env,
    OTEL_LOG_RAW_API_BODIES: `file:${BODY_DIR.replace(/\\/g, '/')}`,
    CLAUDE_CODE_ENABLE_TELEMETRY: '1',
    // Deliberately NO exporter is configured. If bodies appear anyway, that settles whether file
    // mode needs a collector, which is the half of the question the binary read could not answer.
  };
  // The child must not inherit this session's markers or it refuses to start.
  delete env.CLAUDECODE;
  delete env.CLAUDE_CODE_CHILD_SESSION;

  console.log('spawning one `claude -p` with file-mode raw bodies and NO collector...');
  const r = spawnSync('claude', ['-p', 'Reply with the single word OK'], {
    env, encoding: 'utf8', timeout: 120000, windowsHide: true,
  });
  const verdict = classifyRun({ status: r.status, stdout: r.stdout || '', stderr: r.stderr || '' });

  if (verdict !== 'ok') {
    console.log('');
    console.log(`BLOCKED (${verdict}). The gate did NOT run, so this is not a negative result.`);
    console.log(`  exit: ${r.status}`);
    console.log(`  said: ${String(r.stdout || r.stderr || '').trim().slice(0, 200)}`);
    if (verdict === 'blocked-auth') {
      console.log('  fix: run `claude setup-token` once in a real terminal, then re-run this.');
    }
    return 2;
  }

  const files = existsSync(BODY_DIR) ? readdirSync(BODY_DIR) : [];
  const requests = files.filter((f) => f.endsWith('.request.json'));
  console.log('');
  console.log(`files written: ${files.length}   request bodies: ${requests.length}`);
  if (!requests.length) {
    console.log('');
    console.log('NEGATIVE, and this one IS a result: the request completed and no body was written.');
    console.log('File mode therefore needs something more than the env var, most likely a running');
    console.log('OTLP collector. The README claim stands.');
    return 1;
  }

  const biggest = requests
    .map((f) => ({ f, size: statSync(join(BODY_DIR, f)).size }))
    .sort((a, b) => b.size - a.size)[0];
  const info = inspectBody(readFileSync(join(BODY_DIR, biggest.f), 'utf8'));
  console.log(`largest body: ${biggest.f}  ${(biggest.size / 1024).toFixed(1)} KB`);
  console.log(`  parsed: ${info.parsed}   model: ${info.model}   messages: ${info.messageCount}`);
  console.log(`  system prompt present: ${info.hasSystem}`);
  console.log(`  tools array: ${info.hasTools ? `${info.toolCount} tools, ${(info.toolSchemaBytes / 1024).toFixed(1)} KB of schema` : 'ABSENT'}`);
  if (info.toolNames.length) console.log(`  first tools: ${info.toolNames.join(', ')}`);
  console.log('');
  if (info.hasTools) {
    console.log('POSITIVE. Tool schemas ARE recoverable from disk with no collector running.');
    console.log('This reproduces the 2026-08-22 result on 2.1.237 (18 tools, 91.8 KB of schema).');
    console.log('The README already records it. If your numbers differ materially from that, the');
    console.log('route has changed and the README needs re-pinning to the build you just ran.');
    return 0;
  }
  console.log('PARTIAL. A body was written but carries no tools array. Report what you saw.');
  return 1;
}

function selfTest() {
  const checks = [];
  const add = (n, ok, d = '') => checks.push([n, ok, d]);

  add('an auth failure classifies as BLOCKED, never as a negative',
    classifyRun({ status: 1, stdout: 'Failed to authenticate: OAuth session expired and could not be refreshed' }) === 'blocked-auth');
  add('a login prompt also classifies as blocked',
    classifyRun({ status: 1, stdout: 'Not logged in - Run /login' }) === 'blocked-auth');
  add('a clean run classifies as ok', classifyRun({ status: 0, stdout: 'OK' }) === 'ok');
  add('a non-zero exit with no auth text is blocked-other (gate can fail)',
    classifyRun({ status: 3, stderr: 'some other failure' }) === 'blocked-other');

  const body = JSON.stringify({
    model: 'claude-opus-5', system: 'you are...', messages: [{ role: 'user', content: 'hi' }],
    tools: [{ name: 'Read', input_schema: { type: 'object' } }, { name: 'Bash', input_schema: {} }],
  });
  const i = inspectBody(body);
  add('a body with tools reports them', i.hasTools && i.toolCount === 2, JSON.stringify(i.toolCount));
  add('tool names are surfaced', i.toolNames.join(',') === 'Read,Bash', i.toolNames.join(','));
  add('schema bytes are measured', i.toolSchemaBytes > 0);
  add('system prompt presence is reported', i.hasSystem === true);

  const noTools = inspectBody(JSON.stringify({ model: 'm', messages: [] }));
  add('a body WITHOUT tools reports absent (gate can fail)', noTools.hasTools === false);
  add('unparseable input is reported, not thrown', inspectBody('{not json').parsed === false);

  const c = credentialState();
  add('credential check returns a shape without printing secrets',
    typeof c.present === 'boolean' && !('accessToken' in c) && !('refreshToken' in c));

  // The env-token route. The first version of this tool knew only about the credential file and
  // would have reported NOT USABLE on a machine authenticating fine through the environment.
  const e1 = envTokenState({ CLAUDE_CODE_OAUTH_TOKEN: 'sk-ant-oat01-abcdefghij' });
  add('an env token is detected', e1.present === true);
  add('and its shape is checked', e1.looksLikeToken === true);
  add('and its length is reported without the value',
    e1.length === 23 && !('value' in e1) && !('token' in e1), String(e1.length));
  const e2 = envTokenState({});
  add('an absent env token reports absent (gate can fail)', e2.present === false);
  const e3 = envTokenState({ CLAUDE_CODE_OAUTH_TOKEN: 'not-a-real-prefix' });
  add('a wrong-prefix value is flagged rather than accepted', e3.present === true && e3.looksLikeToken === false);

  let bad = 0;
  for (const [n, ok, d] of checks) {
    if (!ok) bad++;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${ok ? '' : '  [' + d + ']'}`);
  }
  console.log(bad === 0 ? `SELF-TEST PASS (${checks.length} checks)` : `SELF-TEST FAIL (${bad}/${checks.length} failed)`);
  return bad === 0 ? 0 : 1;
}

// Only dispatch the CLI when this file IS the entry point. Without this guard, importing the
// module for its exports runs the dispatch as a side effect. probe.mjs records two earlier
// victims; otel-ingest.mjs became a third the moment it imported inspectBody from here.
const IS_ENTRY = (() => {
  try {
    const entry = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
    return entry === import.meta.url;
  } catch { return false; }
})();

const argv = process.argv.slice(2);
if (!IS_ENTRY) { /* imported for its exports: do nothing */ }
else if (argv.includes('--self-test')) process.exit(selfTest());
else if (argv.includes('--check')) process.exit(check());
else if (argv.includes('--run')) process.exit(run());
else { console.log('specify --check, --run, or --self-test'); process.exit(2); }

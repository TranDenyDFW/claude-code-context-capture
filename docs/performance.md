# What capture costs, and how to measure it on your own machine

Moved out of the README, which had grown a shell script in the middle of its install section.

The `PostToolUse` hook is wired with matcher `*`, so Claude Code spawns it once per tool call and
waits for it. Almost all of that is Node process startup rather than anything this repo does, and
startup varies by roughly five times across machines, so measure yours rather than trusting a
number here.

```bash
node -e "
const {spawnSync}=require('node:child_process');
const payload=JSON.stringify({hook_event_name:'PostToolUse',session_id:'m',cwd:process.cwd(),
                              tool_name:'Read',tool_input:{},tool_response:'x'});
const env={...process.env, C4X_EVENTS_OUT: process.cwd()+'/tmp/cost.ndjson'};
const t=(f,n)=>{const a=[];for(let i=0;i<n;i++){const s=Date.now();f();a.push(Date.now()-s);}
                a.sort((x,y)=>x-y);return a[Math.floor(a.length/2)];};
console.log('bare node startup:', t(()=>spawnSync(process.execPath,['-e','']),10), 'ms');
console.log('this hook        :', t(()=>spawnSync(process.execPath,['hooks/event-hook.mjs'],
                                                  {input:payload,env}),20), 'ms');"
```

`C4X_EVENTS_OUT` sends the measurement rows to a scratch file, so nothing real is written.

Two machines, same method, medians:

| | bare node startup | the hook | this repo's own share |
|---|---|---|---|
| a fast desktop | 32 ms | 44 ms | 12 ms |
| a slower one | 170 ms | 248 ms | 78 ms |

If that is too much, remove the `PostToolUse` entry from `~/.claude/settings.json`. You lose
per-tool byte accounting on the Cost tab; everything else keeps working, because the transcript
harvest does not depend on it.

## Reading `install status`

```
install root : /path/to/claude-code-context-capture
settings     : /home/you/.claude/settings.json
store        : /path/to/claude-code-context-capture/data/context.db
receipt      : written 2026-08-28T19:26:22.167Z
hook capture : 1,204 events, last 3 min ago
status line  : 11 genuine samples, last 3 min ago
self-heal    : never rewrote your settings  (set C4X_NO_SELF_HEAL=1 to stop it)
dashboard    : answering on 8059 for /path/to/claude-code-context-capture/data/context.db; stop it with: curl -X POST http://127.0.0.1:8059/__shutdown__ -H "X-C4X-Shutdown: <token>"
HEALTHY
```

Exit code 0 is healthy, 1 is drifted, 2 is misuse, so it can gate a script.

The `dashboard` line has three states: `answering on <port> for <store>` when the page the hook
started (or one started by hand) is up, with the stop command read from `data/raw/dashboard.log`;
`not running (launcher: ...)` naming the interpreter or exe the next Claude session will start it
with, or why there is none; and `disabled` when `install --no-dashboard` or `C4X_NO_DASHBOARD=1`
turned the autostart off. A fourth, `port <port> held by ...`, means something else answers there
and the hook will not fight it.

The last three lines are liveness rather than wiring: whether anything has actually been captured,
and when. `hook capture` counts what a harvest has stored and also reports when a hook last
appended to the raw log, so a fresh install says events are captured and waiting for a harvest
rather than printing a zero that reads exactly like a dead capture. `status line` will say it never
fired if you use the Claude Desktop chat view, which does not invoke a status line at all; capture
continues through the hooks either way.

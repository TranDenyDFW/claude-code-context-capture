# c4x: context window capture

Claude Code shows you one number: a percentage in the context bar. Everything behind it is thrown
away at render time. There is no history, so when a compaction fires you cannot see what it dropped,
and you cannot see it coming.

This captures that state into a local SQLite store on your machine, and mirrors Claude Code's own
window arithmetic closely enough to tell you when the next compaction will fire.

Three questions it answers about your own sessions:

- **Where did the window go?** Context growth per turn, with every compaction marked.
- **When will it compact?** The trigger threshold for the model you are on, and how far from it you are.
- **What am I paying for twice?** Files read repeatedly in one session, and tools loaded but never called.

Everything stays on your machine. `data/` is gitignored and nothing is sent anywhere.

## What you get

A dashboard over the store, five tabs:

| Tab | Answers |
|---|---|
| **Overview** | totals, and which projects consume the most context |
| **Session** | context growth turn by turn, compaction boundaries, the threshold for each model run |
| **Compactions** | every compaction, the predicted trigger, and how far past it you actually went |
| **Mirror** | a live calculator: given N tokens and a window, what happens |
| **Waste** | files re-read inside one session, MCP servers by invocation, tool schema cost |

And a command line, when you want a number rather than a page:

```
$ node tools/mirror.mjs --predict 850000 --window 1000000
{
  "tokens": 850000,
  "window": 1000000,
  "maxOutputTokens": 20000,
  "autocompact": true,
  "reported_threshold": 967000,
  "trigger_threshold": 967000,
  "usable_window": 980000,
  "warn_at": 947000,
  "blocked_at": 997000,
  "level": "ok",
  "pctLeft": 12,
  "tokens_until_compact": 117000
}
```

## Requirements

- **Node 24 or newer.** Every tool opens the store through the built-in `node:sqlite`. Earlier
  majors shipped it behind an experimental flag; treat those as unverified.
- **Python** and `pip install -r requirements.txt`, for the dashboard only. The capture side needs
  nothing from pip.
- One optional tool, `tools/pty-control-arm.py`, is **Windows only** and needs `pywinpty`. Nothing
  else depends on it.

## Install

```bash
git clone https://github.com/TranDenyDFW/claude-code-context-capture.git
cd claude-code-context-capture
node tools/install.mjs install
```

That writes this checkout's hooks and status line into `~/.claude/settings.json`, and records what
it changed in `data/install-receipt.json`. It registers `SessionStart`, `SessionEnd`,
`UserPromptSubmit`, `PostToolUse`, `SubagentStop`, `PreCompact` and `PostCompact`, and sets
`statusLine`.

It converges rather than overwrites. Running it twice changes nothing the second time, it repairs a
config that has drifted, and it leaves other tools' hooks, your `permissions`, and every unrelated
key alone. If another tool's hook shares a matcher group with ours, it moves ours out rather than
overwriting the group.

Hooks apply to sessions started after the write, so your current session will not be captured.

```bash
node tools/install.mjs install --dry-run   # print the exact diff, write nothing
```

## First run

```bash
node tools/harvest.mjs      # read your transcripts into the store
pip install -r requirements.txt
python app.py               # http://127.0.0.1:8056
```

`harvest.mjs` reads every transcript under `~/.claude/projects` and creates the store on first run.
It is incremental: later runs read only what was appended, so re-running is cheap and safe.

Shut the dashboard down with the Quit button in the header, or
`curl -X POST http://127.0.0.1:8056/__shutdown__`.

## Everyday use

Three commands cover almost everything:

```bash
node tools/install.mjs status   # is the capture wiring live? exit 0 healthy, 1 drifted
node tools/harvest.mjs          # pull in new sessions
python app.py                   # look at the result
```

Occasionally useful:

```bash
node tools/waste.mjs --duplicates      # files read repeatedly inside one session
node tools/waste.mjs --tools           # invocations, and measured schema cost where captured
node tools/waste.mjs --servers         # MCP servers, including loaded-but-never-called
node tools/mirror.mjs --predict 850000 --window 1000000
node tools/harvest.mjs --stats         # what the store holds
node tools/statusline.mjs --report     # has the live status-line capture ever run
node tools/segments.mjs --session ID  # which model ran when, and the window for each run
```

To get a session ID for that last command, either pick one from the dropdown on the dashboard's
Session tab, or list them:

```bash
node -e "const {DatabaseSync}=require('node:sqlite');
const db=new DatabaseSync('data/context.db');
for (const r of db.prepare('SELECT session_id, project_slug, COUNT(*) turns, MAX(total_resident) peak FROM turns JOIN sessions USING(session_id) GROUP BY session_id ORDER BY peak DESC LIMIT 10').all())
  console.log(r.session_id, r.project_slug, r.turns + ' turns', 'peak ' + r.peak);"
```

Every tool ships a `--self-test`, which is the fastest way to see what it actually checks. Only
`install.mjs` implements `--help`.

Every tool accepts `--db <path>` (or `C4X_DB`) to run against a copy instead of the live store. An
explicit path must already exist: SQLite creates on open, so a typo would otherwise produce an empty
database and report zeros, which reads exactly like "there was nothing to do".

## Troubleshooting

**Capture seems silent.** Run `node tools/install.mjs status`. It reports whether each hook is
registered in a form Claude Code will actually fire, and exits non-zero if not. A config can be
present, valid JSON, point at real files, and still never run. This is the command that tells you.

**No status line.** Expected under the desktop app: the status line is part of the terminal UI and
does not run on that entrypoint. Hooks are process level and do fire there, so capture still works.
In a real terminal you will see it. `node tools/statusline.mjs --report` separates "never ran" from
"ran and wrote nothing".

**Dashboard is empty, or errors on startup.** Run `node tools/harvest.mjs` first. The app opens the
store read-only and will not create it.

**Token numbers look about twice too high.** You are summing `turns`. Use the `api_calls` view, below.

## Uninstall, or start fresh

```bash
node tools/install.mjs uninstall           # remove our entries; your store is kept
node tools/install.mjs uninstall --purge   # ...and delete the store too
node tools/install.mjs reset --data        # empty store, keeping a timestamped copy of the old one
node tools/install.mjs reset --settings    # rewrite just the wiring
```

`uninstall` removes only what this tool added, restores whatever `statusLine` was set before it, and
leaves every other hook and setting untouched.

## The store

`data/context.db`, SQLite, yours to query directly.

- `turns` - one row per assistant record: input, cache creation, cache read, output and thinking
  tokens, model, request id. `total_resident = input + cache_creation + cache_read`, the same sum
  Claude Code uses for the context bar.
- `api_calls` - **a view, and the one to query for token totals.** A streamed assistant message is
  written as several transcript rows sharing one request id, so summing `turns` counts the same API
  call two to eight times. The view takes the max per request id, because the input and cache
  columns repeat while `output_tokens` accumulates as the message streams.
- `compactions` - trigger, tokens before and after, duration, dropped tokens, paired summary.
- `compaction_survivors` - which records lived through each compaction, by uuid.
- `tool_calls` - one row per tool use: tool and MCP server name, the file or url, the result size.
- `hook_events` - lifecycle events, sizes only and never contents.
- `request_bodies`, `tool_schemas` - per-tool schema cost, populated only if you opt into raw-body
  capture, below.
- `sessions`, `attachments`, `files`, `record_types`, `harvest_runs`.

## Measuring tool schema cost (optional)

Loaded tools cost tokens on every request whether or not you call them, and that cost is not in the
session logs. It is recoverable from the raw API request bodies, which Claude Code can be asked to
write to disk:

```bash
node tools/otel-gate.mjs --check     # can this machine run it? no API call, no token values printed
node tools/otel-ingest.mjs --enable  # prints the env block to export; sets nothing itself
node tools/otel-ingest.mjs --ingest  # read captured bodies into the store
node tools/waste.mjs --tools         # now shows measured schema bytes per tool
```

**A raw body is the entire conversation plus the system prompt**, so this is off by default and
gated behind two environment variables. The ingest stores sizes and hashes only, never message text,
system prompt text or tool descriptions, so the store never becomes a second copy of your
conversations.

## Limits

- **The status line does not run under the desktop app.** It is part of the terminal UI. Hooks cover
  that entrypoint, so capture is not lost, but live per-render sampling only happens in a terminal.
- **Probe rows describe a probe session, not your live work.** `probe.mjs` is the only route to the
  per-category breakdown, and control requests are served by the process it spawns; there is no way
  to attach to a running session. It is useful for the static categories - system prompt, tools,
  skills, memory files - which depend on your configuration rather than the conversation.
- **Schema cost is measured only for bodies you captured.** A tool with no captured schema reads
  UNKNOWN, never 0: an uncaptured schema costs an unknown number of tokens, not none.
- **The transcript is not the context.** It is append-only history; the context window is what was
  actually sent after compaction and eviction. The `usage` numbers are ground truth, the message
  list is not.
- **The window math is pinned to a Claude Code build.** Re-run `node tools/mirror.mjs --validate`
  after a Claude Code update before trusting the predictor. It fits the math against every
  compaction in your own store and reports where it disagrees.

## License

MIT. See `LICENSE`.
# c4x: context window capture

Claude Code shows you one number: a percentage in the context bar. Everything behind it is thrown
away at render time. There is no history, so when a compaction fires you cannot see what it dropped,
and you cannot see it coming.

This captures that state into a local SQLite store on your machine, and mirrors Claude Code's own
window arithmetic closely enough to tell you when the next compaction will fire.

Five questions it answers about your own sessions:

- **Where am I right now?** A live mirror of the context bar - `128.5k / 1M (13%)` - refreshed on a
  timer, so the page tracks the session instead of showing whenever you last ran a command.
- **Where did the window go?** Context growth per turn, with every compaction marked.
- **When will it compact?** The trigger threshold for the model you are on, and how far from it you are.
- **What did a compaction throw away?** The summary it wrote, in full, and the messages that are
  absent from its survivor list.
- **What is actually filling the window?** The category split Claude Code shows in its tooltip,
  with the history the tooltip cannot show.
- **What am I paying for twice?** Files read repeatedly in one session, and tools loaded but never called.

Everything stays on your machine. `data/` is gitignored and nothing is sent anywhere.

**The store keeps the text of your conversations, not just their sizes.** That is what makes a
compaction summary readable instead of a character count, and what makes a dropped message
recoverable at all. It lives in `data/context.db` on your disk and goes nowhere else.

**While it is installed, it captures. There is no off switch.** No setting, no environment variable
and no button stops capture short of uninstalling, and the hooks repair their own wiring if
something edits it out of your settings. That is deliberate: a capture tool you can silently
disable still produces a store that *looks* complete, and nothing in it tells you which sessions
were recorded and which were not. Read this before you install, not after:

```bash
node tools/install.mjs uninstall   # the only way to stop it
```

## What you get

A dashboard over the store, five tabs:

The header carries the live reading on every tab, coloured by how close the next compaction is,
with a mark on the bar where the trigger sits.

| Tab | Answers |
|---|---|
| **Overview** | totals, and which projects consume the most context |
| **Session** | context growth turn by turn, compaction boundaries, the threshold for each model run, and every message in the session - click one to read it |
| **Compactions** | every compaction, the predicted trigger, how far past it you went, and on click: the summary it wrote plus what it dropped |
| **Breakdown** | the context tooltip's category split - Messages, System tools, MCP tools, Skills, System prompt, Free space - for the current turn and across every turn on record |
| **Mirror** | a live calculator: given N tokens and a window, what happens |
| **Waste** | files re-read inside one session, MCP servers by invocation, tool schema cost |

The dashboard runs an incremental harvest on a timer, so it follows a live session on its own.
So do the hooks, on `SessionEnd` and `UserPromptSubmit` - which means the store stays current
whether or not the dashboard is running. Closing this viewer does not pause capture.

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

**Hooks take effect immediately, in a session that is already running.** No restart. Measured at
nine seconds from the write, in a session that had been going for twenty hours. The only thing a
running session cannot pick up is an event that has already gone past: its own `SessionStart`, and
`PreCompact`/`PostCompact` for a compaction that already happened.

That matters less than it sounds, because hooks are not where the substance comes from. Token
counts, compactions, message text and tool calls are all read out of transcripts by `harvest.mjs`,
and Claude Code writes those whether or not a hook is registered. **Your existing sessions are
captured retroactively** the first time you harvest, going back as far as your transcripts do.
Hooks add lifecycle detail transcripts do not carry - permission mode, agent id and type, prompt
size, and exact event timing.

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

Stop the dashboard with Ctrl+C in the terminal running it. Closing the dashboard does not stop
capture - the hooks keep harvesting on their own, which is the point.

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
node tools/breakdown.mjs --show        # the category split for the newest turn
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
- `messages` - **the text of every record**, with its role, kind, character count and source line.
  This is the one table that holds content rather than measurements. It is what the Compactions tab
  reads to show you a summary as prose, and what makes "which messages did this compaction throw
  away" answerable. Roughly the size of your transcripts, and not optional while installed.
- `context_baselines` - one row per observation of your configuration's fixed overhead, which is
  what makes the category split derivable. See [The breakdown](#the-breakdown).
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
gated behind two environment variables. This ingest stores sizes and hashes only - never system
prompt text and never tool descriptions - so opting into it does not add the *system* side of a
request to the store. Your message text is a separate matter and is captured by `harvest.mjs`
regardless of this setting; see `messages` under [The store](#the-store).

## The breakdown

Claude Code's context tooltip splits the window into Messages, System tools, MCP tools, Skills,
System prompt and Free space. **That split is stored nowhere.** It is computed for the UI and
discarded: it appears in no transcript record, no hook payload and no status line sample.

It is still recoverable, because the arithmetic is closed:

```
resident = static + messages          static   = your configuration's fixed overhead
free     = window - resident          messages = everything the conversation added
```

`resident` is exact and already recorded for every turn ever harvested. So one reading of `static`
unlocks the split for all of your history at once, with no further capture. Take that reading from
the tooltip and record it:

```bash
node tools/breakdown.mjs --calibrate --system-prompt 5100 --system-tools 19100 --mcp-tools 11000 --skills 6100
```

Baselines are kept as a history, not a single value, because the overhead moves when you add an MCP
server or a skill - so an old turn is split using the overhead that was in force then. Re-calibrate
after changing your setup.

**What is exact and what is derived.** Resident and free space are measured. Messages and the
category rows are computed from a baseline, and the Breakdown tab says so on the page rather than
presenting them as readings. A turn from before your first calibration is flagged, because it is
being split with an overhead that may not have applied to it.

**Why not just ask Claude Code?** `tools/probe.mjs` can request the split over the control
protocol, but only from a session it spawns, and a spawned CLI session is not configured like your
desktop app - measured here, MCP tools was 11k in the app and 0 in the CLI, which reports no MCP
servers configured at all. The desktop app's messaging socket is an inbox for injecting messages,
not a query channel: `get_context_usage` appears at 14 offsets in the binary and none of them lie
inside that socket's code.

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
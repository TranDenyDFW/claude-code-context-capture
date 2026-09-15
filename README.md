# c4x: context window capture

Claude Code shows you one number: the percentage in the context bar. Everything behind it is
discarded at render time, so you cannot see what a compaction dropped, or see the next one coming.

**c4x** records that state into a local SQLite store as you work, and mirrors Claude Code's own
window arithmetic closely enough to say when the next compaction will fire. Install is three
commands and pulls nothing from npm.

**Status: beta.** Built and used on Windows with the Claude desktop app; the checks also run on
Ubuntu in CI. The desktop-app features (accounts, Adopt) are Windows only. What changed and when
is in [CHANGELOG.md](CHANGELOG.md).

![One session's context growth, with compaction markers, the predicted trigger line, the model's warn and blocked zones, and a rolling band marking calls unlike the rest of the session](docs/images/session.png)

<sub>A real store of 1,409 sessions, with working directories, file names and message text replaced
by <code>tools/redact.py</code>. Every number, chart and finding is untouched.</sub>

> **Everything stays on your disk** under `data/`: the store, the raw capture logs, and a copy of
> each transcript taken just before it is compacted. The store keeps the TEXT of your
> conversations, not just their sizes. While it is installed it captures, and there is no off
> switch short of uninstalling. [What that means.](#privacy)

## Install

Needs **Node 24+** and, for the dashboard and CLI only, **Python 3.12+**. Nothing from npm.

```bash
git clone https://github.com/TranDenyDFW/claude-code-context-capture
cd claude-code-context-capture
node tools/install.mjs install
pip install -r requirements.txt        # dashboard and CLI only
```

That writes this checkout's hooks and status line into `~/.claude/settings.json` and records what
it changed in `data/install-receipt.json`. Running it twice changes nothing; running it over a
broken config repairs it. If there is no store yet it runs one harvest.

**Check it worked.** This hooks into another program's lifecycle, and a silent failure there looks
exactly like a quiet week. `node tools/install.mjs status` exits 0 healthy, 1 drifted, 2 misuse.

`install --dry-run` prints the diff and writes nothing. `uninstall` removes only this tool's entries
and keeps the store; `--purge` deletes it too. [What capture costs, and how to read `status`.](docs/performance.md)

## Use it

Everything below runs against transcripts you already have.

```bash
node tools/harvest.mjs --stats               # confirm it captured something
python -m c4x.cli sessions --limit 5         # your sessions, largest first
python -m c4x.api                            # the dashboard, on 127.0.0.1:8059 (starts with Claude by itself, see below)
```

In the session list, `current` is the last reading and `peak` the high-water mark. When does the
next compaction fire?

```bash
node tools/mirror.mjs --predict 850000 --window 1000000
```

```json
{ "trigger_threshold": 967000, "warn_at": 947000, "blocked_at": 997000,
  "level": "ok", "pctLeft": 12, "tokens_until_compact": 117000 }
```

The arithmetic comes from `tools/mirror-core.mjs`, the same module the dashboard draws its
threshold lines from, so the number here and the line on the chart cannot drift apart.

**Query it yourself.** Every table on the page prints the query that produced it, and so does the
dump. Add `--json` for the same content machine-readably.

```bash
python -m c4x.cli dump --tab tab-cost | grep -A 12 "Query"
sqlite3 data/context.db
```

## The dashboard

`http://127.0.0.1:8059/`. It starts with Claude and stops itself about a minute after the last
Claude process exits; closing the page does not stop capture. **Stop C4X** in the header stops the
server, **Restart C4X** starts a fresh one, and `install --no-dashboard` turns the autostart off.
The CLI renders the same callbacks the browser does, so a dump is what the page shows, and every
table prints the query that produced it.

| Tab | What it answers | Dump it |
|---|---|---|
| Summary | what is worth doing about this store | `python -m c4x.cli dump --tab tab-summary` |
| All sessions | every chat as a point and a row, with a resumed chat's sessions folded into its newest one | `--tab tab-sessions` |
| Session | where one session's window went | `--tab tab-session --session <id>` |
| Compactions | what each compaction discarded | `--tab tab-compactions` |
| Window | what is in the window right now | `--tab tab-window --session <id>` |
| Cost | what was read twice, and what it cost | `--tab tab-cost` |
| Compare | two populations, measured the same way | `--tab tab-compare --compare-with <id>` |
| Diagnostics | is the capture healthy | `--tab tab-diagnostics` |

**Two tabs need a reading the install does not take.** Window and Diagnostics stay empty until you
record one. Nothing else depends on either, so the install does not run something that costs money.

```bash
node tools/probe.mjs                  # one billable session, about 12 seconds
node tools/breakdown.mjs --calibrate  # your configuration's fixed overhead
```

The header, accounts, Adopt, review runs and the exe build: [docs/dashboard.md](docs/dashboard.md).

## Several accounts, and moving a project

The header's **All** / **Current** switch decides whether every account on the machine reads the
same chat list; [docs/desktop-records.md](docs/desktop-records.md) section 6 explains the sharing.

```bash
python -m c4x.projects export "P:\Work\Thing" --out thing.db
python -m c4x.projects import thing.db --into "D:\Elsewhere\Thing"
```

The export carries the transcripts, project memory, tasks, the trust setting and the desktop app's
own record, so the project opens in Claude Code on the other machine; `--dry-run` writes nothing.

## Privacy

**Nothing leaves the machine.** No network call in the capture path; `data/` is gitignored.

**The store keeps the text of your conversations**, not just their sizes. That is what makes a
compaction summary readable and a dropped message recoverable. Know it before you install.

**There is a second copy.** On every compaction, `hooks/compact-hook.mjs` copies the whole
transcript into `data/snapshots/` before Claude Code drops the messages. A long-lived session
accumulates several. A transcript over 250 MB is skipped rather than copied, and the skip is
recorded with its reason. `C4X_SNAPSHOT=0` turns the copies off; capture continues.

**There is no off switch on purpose.** A tool you can quietly disable still produces a store that
looks complete. `node tools/install.mjs uninstall` stops it and prints what it keeps.

**Who else can read it.** The installer restricts `data/` to your account and SYSTEM; a checkout on
a data volume would otherwise inherit that volume's permissions, which on a stock Windows data
drive lets every local account read the text. `install status` warns and prints the fix.

## Docs

- [docs/architecture.md](docs/architecture.md): the three stages and where the data lives.
- [docs/performance.md](docs/performance.md): what the hook costs and how to measure it.
- [docs/desktop-records.md](docs/desktop-records.md): the desktop app's chat records.
- [docs/dashboard.md](docs/dashboard.md): the dashboard in detail.
- [CONTRIBUTING.md](CONTRIBUTING.md): how to run the checks.
- With the server running, `/api/docs` is the generated API reference and `/api/openapi.json` is the schema.

## License

MIT

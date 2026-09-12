# c4x: context window capture

Claude Code shows you one number: a percentage in the context bar. Everything behind it is
discarded at render time, so when a compaction fires you cannot see what it dropped, and you cannot
see it coming.

**c4x** records that state into a local SQLite store as you work, and mirrors Claude Code's own
window arithmetic closely enough to say when the next compaction will fire. Install is three
commands and pulls nothing from npm.

![One session's context growth, with compaction markers, the predicted trigger line, the model's warn and blocked zones, and a rolling band marking calls unlike the rest of the session](docs/images/session.png)

> **Everything stays on your disk** under `data/`: the store, the raw capture logs, and a copy of
> each transcript taken just before it is compacted. The store keeps the TEXT of your
> conversations, not just their sizes. While it is installed it captures, and there is no off
> switch short of uninstalling. [What that means.](#privacy)

<sub>Every screenshot is a real store of 1,409 sessions with working directories, file names and
message text replaced by <code>tools/redact.py</code>. Every number, chart and finding is
untouched.</sub>

## Install

Needs **Node 24+** (the tools open the store through the built-in `node:sqlite`) and, for the
dashboard and CLI only, **Python 3.12+**. Capture itself needs no Python and nothing from npm.

```bash
git clone https://github.com/TranDenyDFW/claude-code-context-capture
cd claude-code-context-capture
node tools/install.mjs install
pip install -r requirements.txt        # dashboard and CLI only
```

That writes this checkout's hooks and status line into `~/.claude/settings.json` and records what
it changed in `data/install-receipt.json`. It converges rather than overwrites, so running it twice
changes nothing and running it over a broken config repairs it. If there is no store yet it runs
one harvest, so your existing transcripts are already in it.

**Check it worked.** Worth not skipping: this hooks into another program's lifecycle, and a silent
failure there looks exactly like a quiet week.

```bash
node tools/install.mjs status          # exit 0 healthy, 1 drifted, 2 misuse
```

`node tools/install.mjs install --dry-run` prints the exact diff and writes nothing.
`uninstall` removes only this tool's entries and keeps the store; `--purge` deletes the store too.
[What capture costs, and how to read `status`.](docs/performance.md)

## Usage

Everything below runs against transcripts you already have.

```bash
node tools/harvest.mjs --stats               # confirm it captured something
python -m c4x.cli sessions --limit 5         # your sessions, largest first
python -m c4x.api                            # the dashboard, on 127.0.0.1:8059
```

```
session_id  title                                         project        turns  current   peak   compactions
928cf7e5    Status line documentation accuracy            /work/c4x       7425   670569   997078           4
ed1902c7    Economic policy impact on prices and markets  /work/secdb     5986   744642   997778           2
```

`current` is the last reading, `peak` is the high-water mark. A session can sit at 670k having
touched 997k earlier, which is the difference the context bar alone cannot show you.

**When does the next compaction fire?**

```bash
node tools/mirror.mjs --predict 850000 --window 1000000
```

```json
{ "trigger_threshold": 967000, "warn_at": 947000, "blocked_at": 997000,
  "level": "ok", "pctLeft": 12, "tokens_until_compact": 117000 }
```

The arithmetic comes from `tools/mirror-core.mjs`, the same module the dashboard draws its
threshold lines from, so the number here and the line on the chart cannot drift apart.

**What did you pay for twice?**

```bash
node tools/waste.mjs --duplicates
```

```
duplicate reads (>= 3 reads of one file in one session)
  groups: 1129   re-reads beyond the first: 7323   bytes in the repeats: 72778.4 KB

    614x     297.1 KB  identical     582a3e1c  /work/categories.json
    340x    8819.3 KB  42 variants   7fe4cdc8  /books/_standards_catalog.md
```

`variants` is how a re-read hides: the same file reached by 42 different spellings of its path.

**Query it yourself.** Every table on the page prints the query that produced it, and so does the
dump. Add `--json` for the same content machine-readably.

```bash
python -m c4x.cli dump --tab tab-cost | grep -A 12 "Query"
sqlite3 data/context.db
```

## The dashboard

`python -m c4x.api`, then `http://127.0.0.1:8059/`. Closing it does not stop capture. The page is
committed built, so npm is needed only to change the frontend. The same server serves every tab as
JSON: `curl 127.0.0.1:8059/api/tab/tab-cost`.

The CLI renders the same callbacks the browser does, so a dump is what the page shows rather than a
parallel implementation of it.

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

![The Summary tab: findings that each name a session and an action, store totals, and tool bytes by working directory](docs/images/summary.png)

Every finding is clickable: it selects the session it names and jumps to the tab that proves it, so
a claim on the front page is one click from its evidence.

![The Cost tab: re-read groups, the concentration curve, and how each tool call turned out](docs/images/cost.png)

Cost is an estimate and says so, from a price table at `c4x/prices.json` refreshed by CI against
two published sources that have to agree. A model with no entry renders blank, never zero.

**`errors` counts what failed, not what was refused.** Claude Code sets one flag on a tool that ran
and failed and on a tool that was stopped before it ran. Measured on this store, 26.9% of what was
called an error never ran. The `outcome` column reports the two apart, and says `unknown` where the
transcript cannot prove which it was.

![The Compactions tab: every compaction with its predicted trigger, its overshoot, and the survivors it kept](docs/images/compactions.png)

`overshoot` is how far past the predicted trigger the session actually got. Clicking a row opens the
summary the compaction wrote, in full, plus the messages absent from its survivor list, recovered
from the store rather than reconstructed.

**Two tabs need a reading the install does not take.** Window and Diagnostics stay empty until you
record one, and say so on the page rather than looking broken. Nothing else depends on either,
which is why the install does not run something that costs money on your behalf.

```bash
node tools/probe.mjs                  # one billable session, about 12 seconds
node tools/breakdown.mjs --calibrate  # your configuration's fixed overhead
```

![The Window tab: what is in the context window right now, as area, grouped into configuration, messages and free space](docs/images/window.png)

## Moving a project between machines

```bash
python -m c4x.projects export "P:\Work\Thing" --out thing.db
python -m c4x.projects import thing.db --into "D:\Elsewhere\Thing"
```

The export carries the conversations, not only the rows: the transcripts, project memory, tasks,
the trust setting, and the desktop app's own record, so the project opens in Claude Code on the
other machine. Everything is rebuilt from the destination you choose, on the importing user's own
paths. `--dry-run` names every destination and writes nothing; `verify-mirror` re-hashes what
landed and exits non-zero on a difference.

It moves CHATS. A chat resumed four times is five CLI sessions and one entry in the app, and the
export says so, carries every session it spans, and names any chat whose record it could not find
under a session it carries. The import checks the store afterwards and reports any chat whose
sessions landed here as more than one. Records are read from, and removed from, every root the app
uses, which on a packaged install can be two.

## Privacy

**Nothing leaves the machine.** There is no network call in the capture path, and `data/` is
gitignored.

**The store keeps the text of your conversations**, not just their sizes. That is what makes a
compaction summary readable instead of a character count, and a dropped message recoverable at all.
It is the thing to know before you install rather than after.

**There is a second copy.** On every compaction, `hooks/compact-hook.mjs` copies the whole
transcript into `data/snapshots/` before Claude Code drops the messages, because a compaction is the
one event after which the original cannot be recovered from anywhere else. A long-lived session
accumulates several. A transcript over 250 MB is skipped rather than copied, and the skip is
recorded with its reason. `C4X_SNAPSHOT=0` turns the copies off; capture continues.

**There is no off switch on purpose.** A capture tool you can quietly disable still produces a store
that looks complete, with nothing in it saying which sessions were recorded and which were not.
`node tools/install.mjs uninstall` is the way to stop it, and it prints what it is keeping.

**Who else can read it.** The installer restricts `data/` to your account and SYSTEM. A checkout on
a data volume would otherwise inherit that volume's permissions, which on a stock Windows data drive
lets every local account read the conversation text. `install status` warns if it finds that state
and prints the command that fixes it.

## Docs

The dashboard explains itself as you use it: every column carries a tooltip saying what it means,
every table carries the SQL behind it, and every derived figure says on the page that it is derived
rather than measured.

- [docs/architecture.md](docs/architecture.md): the three stages, where the data lives, the one
  invariant that catches everyone, and why the category breakdown is derived rather than read.
- [docs/performance.md](docs/performance.md): what the hook costs and how to measure it here.
- Every tool prints its own usage. `node tools/install.mjs --help` explains why the installer
  converges instead of scripting.
- With the server running, `/api/docs` is the generated API reference and `/api/openapi.json` is the
  schema. `GET /api/health` answers without touching the store, for a readiness check. Neither is
  authenticated and neither needs to be: the server binds to `127.0.0.1` only, and the routes that
  can change anything refuse a cross-origin request.

## License

MIT

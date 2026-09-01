# Architecture

Three stages, one store.

```mermaid
flowchart LR
    subgraph capture["capture, while you work"]
        CC[Claude Code<br/>terminal or desktop]
        SL["statusline.mjs<br/>per-render sampling<br/>(terminal only)"]
        EH[event-hook.mjs<br/>lifecycle events]
        CH[compact-hook.mjs<br/>pre-compaction snapshot]
    end

    subgraph ingest["ingest, on demand and on hooks"]
        HV["harvest.mjs<br/>incremental, by file offset"]
        BD[breakdown.mjs<br/>baselines]
        PR[probe.mjs<br/>category split]
    end

    DB[("data/context.db<br/>SQLite, WAL")]

    subgraph read["read, never write"]
        APP["c4x/api + app.py + c4x/<br/>127.0.0.1:8059"]
        MIR[mirror.mjs<br/>window math]
        SEG[segments.mjs<br/>model segments]
        WST[waste.mjs<br/>re-reads]
    end

    CC --> SL & EH & CH
    SL & EH & CH --> HV
    CC -. transcripts under<br/>~/.claude/projects .-> HV
    HV --> DB
    BD --> DB
    PR --> DB
    DB --> APP & MIR & SEG & WST
    MIR -. constants read at startup .-> APP
```

## What each stage is for

**Capture** writes small things fast and never blocks Claude. The hooks always exit 0: a capture
tool that can fail a tool call is worse than one that misses a row.

**Ingest** is where the transcripts become measurements. `harvest.mjs` stores a file offset per
transcript and reads only what was appended, so a re-run is cheap and the first run is retroactive,
going back as far as your transcripts do.

**Read** never writes. The dashboard opens the store read-only:

| file | holds |
|---|---|
| `app.py` | the composition root: the Dash instance, the layout, the `TABS` registry, every callback. Headless since the React frontend landed, so it builds panes and serves nothing |
| `c4x/api/` | the HTTP surface and the response cache. The only thing that listens |
| `frontend/` | the React page. Reads the API, draws it, holds no queries of its own |
| `c4x/theme.py` | colours, shared styles, the two formatters, the small shared builders |
| `c4x/store.py` | every read, the scoping and cohort rules, the window math they depend on |
| `c4x/panels.py` | the panels several tabs share: evidence blocks, the compare table, the turn diff |
| `c4x/breakdown.py` | deriving the category split, and the two helpers other tabs borrow |
| `c4x/tabs/` | one module per tab, ten of them, with no edges between them |

They layer strictly: theme and store know nothing of each other, panels sits above both, tabs above
all three, app.py above those, and the API above app.py. Nothing imports upward, so there are no cycles to unpick.

`app.py` opens the store read-only. The window math lives in
`mirror-core.mjs` as pure functions with no I/O, and `app.py` reads its constants out of that module
at startup rather than keeping a second copy, so the dashboard and the CLI cannot disagree about
what a threshold is.

## The one invariant that catches everyone

**Sum `api_calls`, never `turns`.**

A streamed assistant message is written to the transcript as several rows sharing one request id.
The input and cache columns repeat on each of those rows while `output_tokens` accumulates as the
message streams. Summing `turns` therefore counts the same API call two to eight times.

`api_calls` is a view that takes the max per request id. It exists for exactly this reason, and
getting it wrong is not subtle in its effect: it once put two different figures for one quantity on
one screen, 852M against the true 434M, a 1.96x overcount.

## Why the breakdown is derived rather than read

Claude Code stores the per-category split of the context window nowhere. The tooltip computes it and
discards it.

So `breakdown.mjs` records a **baseline**, one observation of your configuration's fixed overhead,
and the category split is `resident - baseline`, attributed by the baseline in force at that turn.
Resident and free space are exact; the split is derived and labelled as such everywhere it appears.
A breakdown built on an invented static figure would render exactly as authoritatively as a real
one, which is the single failure that design avoids.

## Where the data lives

| table | holds |
|---|---|
| `turns` | one row per assistant record, raw. Do not sum this. |
| `api_calls` | a view, deduped per request id. **Sum this.** |
| `messages` | the TEXT of every record, not just its size |
| `compactions`, `compaction_survivors` | what fired, and what lived through it |
| `context_baselines` | observations of fixed overhead, written by `breakdown.mjs` |
| `probes`, `probe_categories`, `probe_details` | written by `probe.mjs` |
| `tool_calls`, `attachments`, `hook_events`, `record_types` | per-call and per-event detail |

`data/` is gitignored. It holds verbatim conversations, and a private repository is not a good
enough reason to put that in a history it cannot be removed from.

## One page, and how it is kept honest

`app.py` is HEADLESS. It still builds every pane and it serves nothing: `c4x/api/` is the only
thing that listens, on 8059, and it renders through `app._render_tab`, the same callback the
browser used to dispatch. So the API is not a reimplementation, agreement is true by
construction rather than by care, and a React page replaced the old one without a single
query being rewritten.

Two dashboards over one store is a way to read a stale number and not know it, which is why
there is one page now. `frontend/dist` is COMMITTED, which is unusual for build output and
deliberate: install is three commands and pulls nothing from npm, and that would stop being
true if looking at your own context window required 149 MB of build tooling first.

What makes that checkable is a shape the repo already had. `c4x/cli/extract.py::describe()` reduces
a rendered pane to `{tables, figures, text}`, and that IS the API's contract:

| surface | who reads it | shape |
|---|---|---|
| `GET /api/tab/{id}` | the CLI, the parity differ, the tests | exactly `describe()`, byte for byte what `c4x.cli dump --json` prints |
| `GET /api/tab/{id}/render` | the browser | the same, plus full Plotly figures and the collapsible sections |

The verify surface is deliberately frozen. Adding a field to it for a browser's benefit would change
what the CLI prints and what the differ compares, which is why `details` lives only on `/render`.

The API caches a serialised answer per selection, invalidated on the size and mtime of the database
and its write-ahead log, and served for at most five seconds after the store moves. That bound is
what makes the cache hit at all: this store is harvested continuously, so a tab taking 1.5 seconds
to build is usually invalidated before it can be asked for twice. Nothing here is staler than
`store.py`, which has always cached `session_rows()` for forty-five seconds.

Three commands keep the two honest, and each is proven able to fail before it is believed:

- `python tools/parity.py` renders all eight tabs from both sides under five selection states and
  compares them cell by cell. `--must-fail` corrupts an answer on purpose and confirms it is caught.
- `python tools/bench.py --against` gates render latency; `--via api` measures the API's UNCACHED
  build against the Dash baseline, because comparing a 3 ms cache hit to a 1.6 s render would call
  every change an improvement.
- `python tools/contract_audit.py` applies the data-integrity rules of `table_audit.py` to the API
  payload, so they outlive Dash. It cannot prove every table-building path was reached, which is
  what the coverage half of `table_audit.py` instruments construction to do. Both run.

## Testing

See [CONTRIBUTING.md](../CONTRIBUTING.md). One command, `node tools/run_tests.mjs`, and a synthetic
store via `node tools/make_fixture.mjs` when there is no real one.

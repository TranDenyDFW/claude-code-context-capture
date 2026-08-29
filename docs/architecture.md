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
        APP[app.py<br/>Dash, 127.0.0.1]
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

**Read** never writes. `app.py` opens the store read-only. The window math lives in
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

## Testing

See [CONTRIBUTING.md](../CONTRIBUTING.md). One command, `node tools/run_tests.mjs`, and a synthetic
store via `node tools/make_fixture.mjs` when there is no real one.

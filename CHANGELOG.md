# Changelog

Dates are when the work landed. This project is versioned from 0.1.0, the first release with a test
runner, CI and a linted tree.

## Unreleased

Everything since 0.1.0. This section exists because the file sat at one entry for 58 commits, long
enough that it described a build nobody was running: the privacy line it lists under 0.1.0 had been
removed and nothing here said so.

### Added

- **An HTTP API and a React frontend.** `python -m c4x.api` serves `/api/tab/{id}` and a built
  bundle from `frontend/dist`, which is tracked on purpose so the documented install pulls nothing
  from npm. A suite check now compares the bundle's commit time against its source, because the
  tests run against `src` and the users get `dist`.
- **A response cache** (`c4x/api/cache.py`) keyed on the store's file state, with a five second
  upper bound on staleness. Both clauses are load-bearing and the docstring says which does the
  work.
- **`tools/contract_audit.py`**, a payload-shaped gate meant to outlive Dash, and a shared
  `RENDER_FAILED` marker so a crashed tab is visible to the CLI's exit code and to every audit
  rather than only to a reader.
- **Capture liveness in `status`**: hook events and their recency, status-line samples against
  those events, and when the SessionStart self-heal last rewrote your settings. `C4X_NO_SELF_HEAL=1`
  turns that rewrite off.
- **`install --evict-missing`**, which removes c4x wiring pointing at a root that no longer exists.
  Moving a checkout used to strand hooks that `status` never named.
- **Project import and export**, with the manifest verified before a row is written.
- **Pre-compaction transcript snapshots are documented**, including where they live, the size cap,
  `C4X_SNAPSHOT=0`, and that `--purge` deletes them.
- **What a tool call turned out to be**, on `tool_calls` as `outcome` and `denial_kind`, from
  the `toolDenialKind` field Claude Code writes at the top level of the record. Both nullable
  TEXT through the existing migration path, so an older store gains them the next time harvest
  runs. `is_error` stays: it is the raw transcript fact, and it simply stops being read by
  anything that reports a number. `tools/outcomes.mjs` holds the one definition the writer and
  the readers share; `c4x/store.py` mirrors it in SQL.
- **A Refusals table on the Cost tab, and `waste.mjs --refusals`**, carrying Claude Code's own
  vocabulary unchanged. A table rather than a note, because a note cannot be sorted, filtered
  or exported, and this is the one place the values are quoted verbatim rather than summarised.
- **`harvest --backfill-tool-outcomes`**, which classifies every existing row from the
  transcripts still on disk. UPDATE only, idempotent, and guarded on a high-water rowid rather
  than a row count, because three writers share this store.

### Changed

- **The dashboard header no longer carries a privacy paragraph.** 0.1.0 added it; it repeated the
  same three sentences on every tab of every session, which is how a standing notice becomes
  furniture. The store's path is in the header and what is in the store is in the README.
  `c4x/ui/layout.py` records the reasoning where the paragraph used to be.
- **`harvest --stats` on a machine with no store yet** prints the zeroed shape with a note and exits
  0, and `install` runs one harvest when it finds no store. The README's own "confirm it captured
  something" step used to fail on every correct first install.
- **Hook events are ingested from the last offset** rather than by re-reading the whole log, which
  on one install meant 29,049 runs each re-reading a 36 MB file to learn what the previous run
  already knew.
- **`errors` no longer counts calls that never ran.** Claude Code sets `is_error` on a tool
  that RAN AND FAILED and on a tool that was REFUSED before it ran, and every "errors" number
  this app printed was the two added together. After the backfill this store reads 6,871 flagged
  calls: 1,848 refusals, 3,945 genuine failures, and 1,078 that predate the field and cannot be
  told apart. So 26.9% of what was called an error never ran, and of the calls that can be
  classified at all it is 31.9%. Per tool it is far worse, because refusal is not spread
  evenly: the `ExitPlanMode` row read 39 errors and NONE of them can be proven to have run and
  failed. That row now reads "13 refused, 30 unknown", because the shipped classifier will
  not claim a refusal it cannot prove and 28 of those calls predate the field that would have
  proved it. Reading the result text of the 39 that could be matched: 23 say the user did not
  want to proceed, 2 are permission failures, and exactly ONE is a genuine tool error. The plan
  said 3 and two rounds of commit messages repeated it unmeasured. Only the exact signal ships,
  so the app says unknown where an inference would have said refused.
  Six surfaces now read one merged `outcome`
  column that is BLANK when there is nothing to say, with the three counts kept as hidden
  columns so the CSV export still carries the numbers. It does NOT make them sortable: a hidden
  column has no header, so nothing in the browser can order by it, and the merged text cell
  sorts lexicographically. The plan claimed sorting too; that half was not deliverable and is
  stated here rather than left as a promise the page does not keep.
- **A store harvested before those columns existed reports "N unknown", not zero.** A blank
  column there would be indistinguishable from "everything succeeded", which is the defect
  being fixed. The page names the command that resolves it.
- **Calls flagged by a build older than 2.1.202 are Unknown and stay Unknown.** That build
  recorded no reason, and its refusal records are identical to its failure records down to the
  key set, so roughly 818 real failures move out of `errors`. Reporting them as errors would be
  the same defect, twenty times smaller. The era is stored, so this is a one-line policy change
  later.
- **Hook and user are NOT separated**, because Claude Code does not separate them: a settings
  deny rule and a hook block both record `permission-rule`, and the leak runs both ways. The
  vocabulary is reported as recorded and the column help says why.

### Fixed

- **Mutation routes refuse a cross-origin request.** A multipart POST is a CORS simple request, so
  it reached the import handler and staged the upload to disk before anything validated it. The
  guard is middleware rather than a route dependency, because FastAPI reads the body first.
- **The store directory is created with a narrow ACL** on every path that creates it, not just the
  installer's. On a data drive it had been inheriting `Authenticated Users`.
- **Redaction covers values, not just variable names**, and its stand-in identifiers are stable
  across processes; they were derived from `hash()`, which Python salts per process.
- **Optional tables no longer raise.** A store with no probe or calibration rows renders an empty
  state instead of an exception panel, and the suite now runs against a first-run fixture that
  actually has those tables missing.
- **Every table on the turn-diff panel shows the query that actually ran.** Three of them were
  handed a retyping of their own query. One said `SUM(is_error)` where the real one said a
  `CASE`, and all three dropped the scope clause AND its bound arguments, so a reader who copied
  what was shown got the whole store back instead of the session in front of them.
- **`errors` left the Tool Calls heat map.** `heat_cells` filters to numbers, so a text column
  in that list is a silent no-op: a shading feature quietly doing nothing. It had also been
  shading a count that was 27% refusals, so the darkest cells pointed at tools that had not
  failed.
- **`make_fixture.mjs` can exercise any of this.** It hard-coded `is_error` to 0 for every
  synthetic row, so CI could not reach a single one of these paths.

### Known limits

- A hand-run `tools/statusline.mjs` whose payload session id matches the ambient one is still
  counted as a genuine sample. The cross-check is a session-id heuristic and `statusline.mjs`
  says so.
- `data/raw/events.ndjson` is ingested incrementally but never rotated, so it grows.
- A refused call cannot be attributed to a hook rather than to a person, or the reverse. Claude
  Code records `permission-rule` for both a settings deny rule and a hook block, so this store
  holds no evidence that would separate them. An earlier draft of this entry put numbers on
  how often the two recorded kinds cross over; those came from a text heuristic, do not
  reproduce, and have been removed rather than restated. The limit is in the source, and
  nothing here guesses at the split.
- A call flagged by a build older than 2.1.202 can never be classified: that build recorded no
  reason at all, and its refusal records are identical to its failure records.

## 0.1.0 - 2026-08-29

First tagged version. Everything below already worked; what changed is that it can now be checked by
someone who did not write it.

### Added

- **`tools/run_tests.mjs`**, one command for the whole suite: 384 checks across 16 files at this
  release, which is a record of that release and not the total today. Exit 0 is
  not treated as a pass, because two tools exit 0 for any argument and a runner that trusts the exit
  code reports a total that is a lie. A file with no self-test fails the run; each Python check
  declares the string its success must print.
- **`tools/make_fixture.mjs`**, a synthetic store so the store-dependent checks can run without a
  real capture. Built through the owning tools' own schemas, never a pasted copy of them.
- **`.github/workflows/tests.yml`**: builds the fixture, runs the suite, deletes the store after.
- **Linting**: `ruff` for Python (config in `pyproject.toml`) and `eslint` for the node tools
  (`eslint.config.mjs`, no dependencies, run with `npx --yes eslint .`).
- **`CONTRIBUTING.md`** and **`docs/architecture.md`**, including the invariant that catches
  everyone: sum `api_calls`, never `turns`.
- **`package.json`** for `engines.node >= 24` and script aliases. Deliberately no dependencies.
- **A privacy line in the dashboard header**, stating with a live count that the store holds the
  text of your conversations. The README said so already; the page a person actually looks at did
  not.

- **The dashboard is a package.** `app.py` was 3124 lines and is now 995, with `c4x/theme.py`,
  `c4x/store.py`, `c4x/panels.py`, `c4x/breakdown.py` and a `c4x/tabs/` package of ten modules,
  one per tab, beside it. `tools/table_audit.py` was taught to
  read the whole package first, because it named `app.py` in seven places and splitting the file
  under it would have left the static scan reporting fewer construction sites and passing.

### Fixed

- **`/__shutdown__` accepted GET.** Binding to `127.0.0.1` is no defence, because the browser is on
  loopback too, so any page you visited could stop the dashboard with an `img` tag. POST only now,
  and GET returns 405 rather than falling through to Dash's catch-all and answering 200.
- **A shadowed variable made a self-test pass for the wrong reason.** An inner block declared its own
  flag while a later line still read the outer one, which was already true. Found by turning on
  `no-shadow`, which is the entire argument for the rule.
- Four `zip()` calls now state `strict=`, so a length mismatch between columns of one frame is an
  error rather than a silent truncation.

### Known limits

- `tools/table_audit.py` does not cover clientside callbacks, which are JavaScript, or a branch this
  run's inputs never take. Both are named in its docstring rather than left to be discovered.
- The window math is pinned to a Claude Code build. Re-run `node tools/mirror.mjs --validate` after
  an update before trusting the predictor.

# Changelog

Dates are when the work landed. This project is versioned from 0.1.0, the first release with a test
runner, CI and a linted tree.

## Unreleased

Everything since 0.1.0. This section exists because the file sat at one entry for 58 commits, long
enough that it described a build nobody was running: the privacy line it lists under 0.1.0 had been
removed and nothing here said so.

### Added

- **One row per chat, the way the desktop app lists them.** The app resumes a chat by starting a
  NEW CLI session whose transcript is a copy of the old one plus what follows, so a chat resumed
  four times was five rows here under five names. Harvest now derives the chain from transcript
  uuid overlap into `session_links` (per directory, whenever a new transcript appears, and for
  existing stores via `node tools/harvest.mjs --backfill-chains`), and the Sessions list, the
  picker, cohorts, Compare and every scoped tab fold a chat's older sessions into its newest one:
  named by that session's title, with turns, peak and compactions counted over the whole chain,
  and a `cli sessions` column saying how many it spans. Selecting a superseded id, from a URL, a
  table row or Compare's arm B, resolves to the chat server-side; the API says so in an
  `x-c4x-session-requested` header rather than in the cached payload. A session with a desktop
  record of its own is never folded: a fork copies history too, and the app shows it separately.
  For every other session the copied lines decide: a resume rewrites them to its own id, a fork
  copies them verbatim, so a session succeeds another only when it holds the other's records
  under its own id, is the later transcript, holds something the other wrote itself, and did not
  reach those records through a fork. Measured over 17 links on the author's store, every resume
  was native 100 percent and every fork 0 percent. Before that rule a parent chat's history
  folded into its fork whenever the parent's own successor was a post-compaction resume: one
  fork showed 8,076 turns of which 7,598 were the parent's.
- **Copied rows now belong to the session that produced them.** `turns`, `messages`,
  `compactions` and `tool_calls` were written with `INSERT OR REPLACE` on their uuid, so a copied
  row went to whichever transcript was harvested last, in directory order: one session that
  produced 388 turns was left holding 3. Ingest now reads transcripts in first-timestamp order
  and refuses to move a row to another session; `--backfill-chains` returns the rows written
  under the old rule to their producer. The ordinary harvest does the same for any directory that
  gains a transcript, so by the time the backfill ran on a copy of the author's store the
  directories worked in that day were already right and it reported 16,179 turns, 14,268 messages,
  21 compactions and 7,580 tool calls still to move, most of them under one fork.
  A producer whose transcript is gone still owns its rows: two forks of a deleted parent name it
  on every copied line, and the first rule moved all of the parent's rows into whichever fork
  sorted first. `attachments` and `record_types` are per-session counters and still count a copy
  twice; that is stated here rather than fixed.
- **A session row now says where its transcript actually is.** `sessions.cwd` was the last cwd a
  transcript named, and a session that changed directory was filed under a subdirectory its file
  is not in: export walked the wrong slug directory and carried no transcript, delete left the
  file and excluded the wrong directory, and a resumed chat could span two "projects" and refuse
  to delete. It is now the first cwd whose slug is the file's directory (1,050 of 1,050 top-level
  transcripts on the author's store, against 994 for the old rule; it matched the app's own
  record for 47 of 47 record holders against 43). `transcript_path` is the session's own
  top-level file, where 69 rows pointed at a subagent file that directory order had reached
  first, and `project_slug` is the project directory, not `subagents`. The chains pass and
  `--backfill-chains` repair older rows and report `rows_repaired`.
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
- **Export and import now move the CONVERSATIONS, not only the rows**, and the import lands where
  you choose. `c4x/appstate.py` carries the four things Claude Code holds outside the store: the
  transcripts and their per-session directories, project memory, `~/.claude/tasks/<session>`, the
  `~/.claude.json` entry (which is `hasTrustDialogAccepted`), and the desktop app's own record.
  Before this, an import touched exactly one file, `context.db`, so the project appeared in c4x and
  was invisible in the desktop app.
  - `import --into "<working directory>"`, and the page offers the same field with the directory
    name it produces shown beside it. Everything is rebuilt from that path on the CURRENT user's
    machine: the slug directory, the config key, the desktop record, and the `cwd` in the rows.
    Nothing absolute from the exporting machine is used as a destination.
  - **The desktop record is filed under THIS machine's account and organisation.** Those two
    directory levels are the machine's, not the project's, so copying the source path would put
    the record where the destination app never looks. Measured, with the resolution rule and the
    evidence for it, in `.md/20260909-desktop-record-addressing.md`.
  - **Source always wins**, and every file is re-read and re-hashed after writing. A replacement
    that SHRINKS a file is named, because a compacted transcript is newer and shorter.
  - `verify-mirror <export> [--into ...]` answers "does this machine hold what the export
    carries?" standalone, exits non-zero on a difference, and runs automatically at the end of
    every import. Files the export does not carry are reported and never deleted: a slug directory
    is shared by every session with the same working directory.
- **`--dry-run` on import**, which names every destination and writes nothing.
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
  evenly: the `ExitPlanMode` row read 41 errors and NONE of them can be proven to have run and
  failed. That row now reads "13 refused, 30 unknown", because the shipped classifier will
  not claim a refusal it cannot prove and 28 of the flagged calls predate the field that would have
  proved it. (41 is the flagged count, `is_error = 1`; 30 is the unknown count across all 285 calls
  of that tool, which is what the row shows. This entry said 39 for the first figure while three
  code comments said 41, and 41 is what the store contains: an independent sweep found the
  disagreement.) Reading the result text of the calls that could be matched to a transcript:
  23 say the user did not want to proceed, 2 are permission failures, and exactly ONE is a genuine
  tool error. The plan said 3 and two rounds of commit messages repeated it unmeasured. Only the exact signal ships,
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

- **An export from a store harvested before `session_links` existed no longer fails.** The row
  copy skipped the missing table and the manifest's count loop then raised `no such table` on
  the same file, so `delete`, whose backup is an export, refused too. Every loop over the
  per-session tables now reads the store's own table list; a preview, an export, a delete and an
  import of such a store carry what it has.
- **The compaction pages count over the same chat as the list they were reached from.** The SQL
  twin of the member map resolved `head_id` one hop while every other reader followed a chain to
  its end, so on rows that outlived the pass that wrote them (`B -> A` beside `A -> Z`) a
  compaction's dropped-message count covered a different set of sessions. One flattened map is
  now bound into those queries. `cli sessions` also counts a member with no turns row of its own,
  and an arm B that resolves into arm A's own chat falls back to the default arm and is reported
  in an `x-c4x-compare-requested` header rather than rendering the chat against itself.
- **`node tools/harvest.mjs --help` printed nothing and ran a harvest.** Any flag the tool does
  not know is now refused with the usage; `--help` prints it.
- **An import no longer writes the EXPORTER's path into your `~/.claude.json`.** The destination
  was resolved with `mapping.get(row["cwd"], row["cwd"])`, exact string equality on a value that
  arrives from three places: `sessions.cwd` for a transcript, the raw config KEY for a config
  entry, and the string inside the record for a desktop row. Those need not be spelled the same,
  which is why `normalised()` exists at all: 4 of the 91 projects on this machine carry both slash
  spellings. So a project whose config key used forward slashes had the exporter's absolute path
  written into the importing user's config, kept the imported chat pointing at a directory that
  does not exist on that machine, and still passed the mirror check. Found by independent review.
- **The mirror check now sees the two fields an import rewrites.** A desktop record is hashed with
  `cwd` and `originCwd` neutralised, so a rebased record can be compared at all, which left exactly
  those two fields outside every check: a record pointed at a directory that does not exist
  returned ok. The hash still covers everything the import must not change; the rewritten values
  are now checked against the destination.
- **`--into` is validated before anything is derived from it.** A relative path, an existing file,
  and a page label ending in the archived suffix were all accepted. Nothing failed loudly; the
  project was filed under a name nothing would look for, since `..\x` slugs to `---x`.
- **A rows-only export is no longer called a mirror.** `delete` takes its backup that way, so
  pointing `verify-mirror` at one returned ok while every file in that project's directory was
  carried by nothing.
- **"byte for byte" was an overclaim** and the wording now says what holds: transcripts, memory and
  tasks land byte-identical, while a config entry and a desktop record are content-identical with
  the working directory rebased. A real record grew from 181,366 to 190,303 bytes on being
  re-serialised.
- **An export no longer holds itself in memory.** The whole capture was built as a list of blobs
  before a byte reached the disk, 694.5 MB for this repo's own project, all of it already on the
  disk it was read from. Capture now streams into the export row by row.
- **A delete no longer orphans the two cost tables.** `cost_state` and `cost_state_models` were
  appended to `BY_SESSION` after `sessions`, which put deletion back in the window that ordering
  exists to close: `sessions` gone while rows still pointed at it. The self-test had been
  reporting it as a FAIL. Nothing but the deletion order reads that sequence, so exports and
  footprints are unaffected.
- **No test WRITES to the real `~/.claude`, `~/.claude.json` or `%APPDATA%\Claude`,** and none of
  them reaches those paths through `c4x/appstate.py`. An autouse fixture points that module's three
  roots at a temporary directory. The risk was not in the tests that mean to touch those paths; it
  was in the export tests that now capture app state and did not know they would read the
  developer's own machine.
  The claim stops there on purpose, because a wider one would be false: `c4x/store.py` still READS
  the real machine, by design, and the suite depends on it. `archived_sessions()` is what puts the
  `\archived` marker on a session row and `transcript_ids()` is what tells an imported session from
  a local one, so pointing those at an empty directory silently changed what every pane test saw.
  One test asserts the marker against the real records and is right to. Two of these reads are
  named in `tests/test_appstate.py::TestTheSlugAgainstTheRealMachine`, which skips where there is
  nothing to compare.
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

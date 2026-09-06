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

### Known limits

- A hand-run `tools/statusline.mjs` whose payload session id matches the ambient one is still
  counted as a genuine sample. The cross-check is a session-id heuristic and `statusline.mjs`
  says so.
- `data/raw/events.ndjson` is ingested incrementally but never rotated, so it grows.

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

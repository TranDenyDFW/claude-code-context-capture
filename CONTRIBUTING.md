# Contributing

## Run the checks

```bash
node tools/run_tests.mjs
```

It runs every `--self-test` in `tools/` and `hooks/`, plus the Python checks over the dashboard,
and prints one total.

The total is not quoted here on purpose. It moves whenever a check is added, and a machine whose
packages differ from the pins collects a different one; the suite checks for exactly that drift
and names the package. A figure in a guide that nobody re-runs is worse than no figure, which this
line learned the hard way. Run it and read the last line.

Two things it does that a plain loop does not, both because a plain loop got them wrong:

- **Exit 0 is not a pass.** `mirror-core.mjs` and `paths.mjs` exit 0 for any argument at all, so a
  runner that trusts the exit code scores them green and reports a total that is a lie. They are
  listed as exempt with a reason. A new file with no self-test **fails** the run rather than
  slipping through, and each Python entry declares the string a successful run must print.
- **A check that could not run is a failure, never a warning.** If the store is missing, the Python
  checks are reported as SKIPPED with the reason and the summary says the total does not cover the
  dashboard, so a partial run cannot be read as a full one.

No store on this machine? Build a synthetic one:

```bash
node tools/make_fixture.mjs        # writes tmp/fixture-<stamp>/context.db and prints the path
export C4X_DB=tmp/fixture-<stamp>/context.db   # point the checks at it; data/context.db needs --out and --force
node tools/run_tests.mjs
```

That is what CI does. The fixture is not decoration: two node self-tests and all three Python checks
read a store, and without one they correctly fail rather than skip.

The Python side needs its dev dependencies for the suite and the linter, installed the way CI
installs them, with the pins:

    pip install -c constraints-ci.txt -r requirements.txt -r requirements-dev.txt

Not `pip install -r requirements.txt` alone. That gives you whatever is newest, and on 2026-09-02
a machine that had done that passed a change locally, passed an independent review on the same
machine, and failed CI twice, because CI's pandas represents a missing cell differently from the
one it had. The suite now refuses to pass on a machine whose direct requirements differ from the
pins, and tells you which. Python 3.12 or newer: the pins do not resolve on 3.11.

The pins move as a set and never one line at a time: `python tools/make_constraints.py
--regenerate` re-resolves the whole file, and `.github/workflows/pins.yml` runs exactly that on
the first of every month and opens a pull request when something moved. Do not edit
`constraints-ci.txt` by hand, and do not accept a bot's one-line bump to it; the self-test in the
suite fails on a set that no longer covers the requirements, and pip fails on one that no longer
resolves.

`node tools/run_tests.mjs` runs everything, pytest included, and reports one total. Running pytest
directly works too, but note that the runner reads pytest's `N passed` summary line to know the
suite executed at all: an entry that exits 0 without printing its marker is treated as not having
run, which is why `addopts` in pyproject.toml does not set `-q`.

## The self-test convention

Every tool answers `--self-test`, exits non-zero on failure, and prints `SELF-TEST PASS (N checks)`.
The runner parses that count, so a self-test that prints nothing reads as absent.

**Include at least one check that can fail.** Several here are marked `(gate can fail)` and work by
planting a deliberate mutation and requiring the result to get worse: `mirror.mjs` plants a wrong
buffer, `segments.mjs` pins the window ceiling, `table_audit.py` feeds itself a stringified table. A
suite of forward-only assertions tests its fixtures rather than its gates.

That convention is why building the CI fixture took five rounds. Each round was a mutation check
refusing to pass on data too thin to discriminate, which is the checks working. Two examples worth
knowing before you touch the fixture:

- The mirror's fit **reassigns** a compaction to the next candidate window when the threshold moves,
  so shifting the buffer never makes a 1M-window compaction negative. The fixture carries a
  200k-window session, where there is nothing smaller to be reassigned to.
- The segment audit resolves a window from an **observed compaction** where it can, and an
  observation cannot be contradicted by a ceiling. The fixture switches model mid session so one
  segment resolves from the model instead.

## `tools/table_audit.py`

Every table in the dashboard exports CSV. A number formatted into a string sorts lexicographically
and lands in that export as text, so `fmt_tokens(997800)` producing `"997.8k"` is a defect and not a
presentation choice. The audit exists because that fix was applied three times to whichever columns
someone happened to look at.

It measures coverage rather than asserting it, five ways: every entry point Dash registers is
exercised, every construction site is reached, every call that can reach a table is taken, nothing
is built that the static scan did not predict, and everything built is also walked.

**It must keep being able to fail.** `python tools/table_audit.py --self-test` shows each gate firing
on a case built to defeat it. If you change the audit, run that, and add a case for whatever you
changed. Four separate reviews each defeated an earlier version of it; the shapes they used are
recorded in the module docstring, along with what it still does not check.

## Where things live

`app.py` is the composition root: the Dash instance, the layout, the `TABS` registry and every
callback. Everything else is in `c4x/`, layered so nothing imports upward: `theme` (how it looks),
`store` (what it reads), `panels` (shared builders), `breakdown` (the derived category split),
and `tabs/`, one module per tab.

Moving code between them is safe as long as `python tools/table_audit.py` still reports the same
number of construction sites. It reads the whole package, taken from the import graph rather than a
list of filenames, so a new module is covered the moment the app imports it.

`c4x/cli/` is the same app without a browser. It does not query anything itself: it calls
`_render_tab`, the exact callback the browser calls, and reads the components back into plain data.
That distinction is the point. A parallel implementation would drift, and worse, it would agree
with itself while the page was broken: this repo has already shipped a tab that produced perfectly
correct data on the server and blanked itself in the browser.

    python -m c4x.cli tabs                      # what tabs exist
    python -m c4x.cli sessions --limit 10       # ids to feed the next command
    python -m c4x.cli dump --tab tab-breakdown  # one tab's tables, figures and prose
    python -m c4x.cli all --session <id>        # every tab, and what each produced
    python -m c4x.cli dump --tab tab-compare --session <a> --compare-with <b>

Add `--json` to any dump for the whole payload rather than the first page of each table.

`tests/` is one file per tab, so a failure names the tab in its filename. Two rules run through all
of them:

- Render through the CALLBACK, never the builder. Calling `session_layout(...)` proves the builder
  works and says nothing about the path that delivers it.
- Derive the expected value INDEPENDENTLY. A test that calls the same function the page called is a
  tautology, so these write their own SQL and disagree when the app's query is wrong.

They run against a SNAPSHOT of the store, taken once in `conftest.py` through sqlite's backup API.
Against the live store a rendered figure and a later `SELECT` are read at different instants while
the capture hooks are appending, and a growing count differs for no reason but elapsed time. That
mistake has already cost an afternoon here.

## House rules that are not obvious

- **`api_calls` is the view to sum, never `turns`.** A streamed assistant message is written as
  several transcript rows sharing one request id, so summing `turns` counts the same API call two to
  eight times. This is the single easiest mistake to make in this codebase.
- **The store is read-only from the app.** `app.py` never writes. Only `harvest.mjs` and the hooks do.
- **`immutable=1` is wrong for this store.** It tells SQLite the file cannot change, so SQLite skips
  the WAL, and against a store the hooks are writing it returns stale data and can report a healthy
  database as malformed. Use `mode=ro` with `PRAGMA busy_timeout`.
- **Numbers stay numbers.** Build column specs with `numeric_columns()` and let Dash format the
  display. The audit will catch you.

## Before opening a PR

```bash
node tools/run_tests.mjs
```

Green, with `0 skipped`. If anything is skipped, say why in the PR.

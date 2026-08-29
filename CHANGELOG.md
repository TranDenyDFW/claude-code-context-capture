# Changelog

Dates are when the work landed. This project is versioned from 0.1.0, the first release with a test
runner, CI and a linted tree.

## 0.1.0 - 2026-08-29

First tagged version. Everything below already worked; what changed is that it can now be checked by
someone who did not write it.

### Added

- **`tools/run_tests.mjs`**, one command for the whole suite: 384 checks across 16 files. Exit 0 is
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

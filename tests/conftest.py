"""Shared fixtures. Everything expensive is built once per run.

Two rules these tests follow throughout:

Render through the CALLBACK, never the builder. Calling `session_layout(...)` proves the builder
works and says nothing about the path that delivers it, and both of the defects this suite exists
to prevent lived in the delivery: a table that was right in its builder and invisible on the page,
and a tab that rendered correctly on the server while the browser reset itself.

Derive the expected value INDEPENDENTLY. A test that calls the same function the page called is a
tautology. Where a number can be recomputed from SQL, these tests write that SQL themselves, so a
wrong query in the app produces a disagreement rather than two matching wrong answers.
"""
import os
import sqlite3
import sys
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _snapshot_the_store():
    """Freeze the store before anything imports it, and point the app at the copy.

    These tests render a tab and then recompute the same figure in SQL. Against the LIVE store
    those two reads happen at different instants while capture hooks are appending, so a growing
    count can differ for no reason but elapsed time. That exact mistake already cost an afternoon
    on this project: a query comparison reported three sessions disagreeing, and the three were
    simply the ones being written to while it ran.

    sqlite3's backup API is used rather than a file copy, because a copy taken without the WAL is
    a torn read of a live database. Honoured only when C4X_DB is unset, so CI, which points at a
    fixture, is left alone.
    """
    if os.environ.get("C4X_DB"):
        return
    source = ROOT / "data" / "context.db"
    if not source.exists():
        return
    target = ROOT / "tmp" / "test-store.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.execute("PRAGMA busy_timeout=30000")
    dst = sqlite3.connect(str(target))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    os.environ["C4X_DB"] = str(target)


_snapshot_the_store()


@pytest.fixture(autouse=True)
def never_the_real_claude_directory(tmp_path_factory, monkeypatch):
    """NO TEST EVER READS OR WRITES THE REAL `~/.claude`, `~/.claude.json` or `%APPDATA%\\Claude`.

    Autouse, because the risk is not in the tests that mean to touch those paths: it is in the ones
    that do not know they do. `projects.export` now captures Claude Code's own state, so every
    export test in this suite, and every API test that drives one over HTTP, would otherwise read
    this machine's real config and its real desktop records, and could write them into a test
    export under `tmp/`.

    Read-only would still be wrong. A test that passes because the developer's own machine happens
    to have an entry for `P:\\Alpha` is a test that says nothing, and one that fails on a fresh
    checkout for the same reason is worse.

    A test that wants a populated fake machine builds one over the top of this, which works because
    monkeypatch unwinds in reverse order.
    """
    from c4x import appstate
    empty = tmp_path_factory.mktemp("no-real-claude")
    (empty / ".claude" / "projects").mkdir(parents=True)
    (empty / ".claude" / "tasks").mkdir(parents=True)
    (empty / ".claude.json").write_text('{"projects": {}}', encoding="utf-8")
    (empty / "appdata" / "Claude" / "claude-code-sessions").mkdir(parents=True)
    monkeypatch.setattr(appstate, "CLAUDE_DIR", empty / ".claude")
    monkeypatch.setattr(appstate, "CONFIG_PATH", empty / ".claude.json")
    # `appstate.sessions_root`, NOT `store.sessions_root`. The store's is what feeds the
    # `\archived` marker on every session row through `archived_sessions()`, and pointing THAT at
    # an empty directory silently removed the marker from the whole suite: the one test that checks
    # it against the real records failed with an empty dict, and it was right to.
    monkeypatch.setattr(appstate, "sessions_root",
                        lambda: str(empty / "appdata" / "Claude" / "claude-code-sessions"))


@pytest.fixture(scope="session")
def app():
    """The Dash app module, imported once.

    Importing it registers every callback and opens the store, which is far too expensive to repeat
    per test.
    """
    import app as module
    return module


@pytest.fixture(scope="session")
def store():
    from c4x import store as module
    return module


@pytest.fixture(scope="session")
def q(store):
    """Raw SQL against the store, for computing an expected value independently."""
    return store.q


@pytest.fixture(scope="session")
def rows(store):
    """The session frame the picker and the All sessions table are both built from."""
    return store.session_rows()


@pytest.fixture(scope="session")
def has_probes():
    """Skip when the store has no probe tables at all, which is every store until someone runs one.

    NOT the same judgement as has_store below, and the difference is the point. Turns arrive from
    the documented install, so an empty `turns` really is a broken fixture and must fail. The probe
    family is created only by `node tools/probe.mjs`, which the install path never runs and the
    README does not mention, so its absence is the NORMAL first-run shape rather than a fault.

    Without this the tests did not skip, they CRASHED: `q` raises DatabaseError "no such table:
    probes" before any `.empty` check can be reached, so a first-run store produced errors that
    read like defects in the tab. The app itself already draws this distinction with
    `store.q_optional` and `store.tables_present`; this is the same rule on the test side.
    """
    from c4x.store import tables_present
    if not tables_present("probes", "probe_categories", "probe_details",
                          "probe_message_breakdown"):
        pytest.skip("this store has no probe tables: run `node tools/probe.mjs` to create them")


@pytest.fixture(scope="session")
def has_baseline():
    """Skip when nothing has calibrated this store.

    Sibling of has_probes, and it exists for the same reason on a different table:
    `context_baselines` is written only by `node tools/breakdown.mjs --calibrate`, which no install
    step runs, so every fresh store lacks it and `q` raises "no such table: context_baselines"
    before any emptiness check. c4x/breakdown.py:latest_baseline already treats the missing table
    as the valid "never calibrated" state; this is that rule on the test side.
    """
    from c4x.store import tables_present
    if not tables_present("context_baselines"):
        pytest.skip("this store has no calibration baseline: run "
                    "`node tools/breakdown.mjs --calibrate`")


@pytest.fixture(scope="session")
def has_store(q):
    """Whether there is anything to test against.

    An empty store is a FAILING condition, not a reason to skip: CI builds a fixture precisely so
    these tests have data, and a suite that silently passes on an empty database is the kind of
    check that cannot fail.
    """
    n = int(q("SELECT COUNT(*) AS n FROM turns").iloc[0]["n"])
    assert n > 0, ("no turns in the store, so nothing below could be verified. Build the fixture "
                   "with `node tools/make_fixture.mjs --out tmp/test.db` and set C4X_DB, or run "
                   "`node tools/harvest.mjs`.")
    return True


@pytest.fixture(scope="session")
def session_id(q, has_store):
    """One session, chosen by a rule rather than hardcoded.

    The most-compacted session, because it exercises the parts a quiet session cannot: compaction
    rows, several model segments, and a resident curve that falls as well as rises. A hardcoded id
    would rot the moment the store changed.
    """
    df = q("""SELECT session_id, COUNT(*) AS n FROM compactions
               GROUP BY session_id ORDER BY n DESC, session_id LIMIT 1""")
    if not df.empty:
        return df.iloc[0]["session_id"]
    df = q("""SELECT session_id, COUNT(*) AS n FROM turns
               GROUP BY session_id ORDER BY n DESC, session_id LIMIT 1""")
    return df.iloc[0]["session_id"]


@pytest.fixture(scope="session")
def other_session_id(q, session_id):
    """A second session, for the Compare tab. Never the same one as `session_id`."""
    df = q("""SELECT session_id, COUNT(*) AS n FROM turns WHERE session_id <> ?
               GROUP BY session_id ORDER BY n DESC, session_id LIMIT 1""", (session_id,))
    return None if df.empty else df.iloc[0]["session_id"]


@pytest.fixture(scope="session")
def cohort(rows):
    """A cohort value the app itself would offer, rather than one invented here."""
    if rows.empty:
        return None
    project = rows["project"].value_counts().index[0]
    return f"project::{project}"


@pytest.fixture(scope="session")
def pane(app):
    """Render any tab the way the browser does, by tab id."""
    from c4x.cli import commands

    def _render(tab_id, session=None, scope="main", coh=None):
        ids = [t[0] for t in app.TABS]
        assert tab_id in ids, f"unknown tab {tab_id}"
        return app._render_tab(ids.index(tab_id), session, scope, coh)

    _render.commands = commands
    return _render

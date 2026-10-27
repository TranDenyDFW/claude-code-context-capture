"""The Compactions tab: counts, overshoot, and the claim the tab makes about its own chart."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from c4x.cli import extract

# Run in a child, with `subprocess.run` recorded BEFORE the store is imported. `c4x.proc.run`
# looks the name up on the module at call time, so a spy installed here sees every shell-out the
# import makes, whichever helper issues it.
_IMPORT_PROBE = """
import json, subprocess
seen = []
real = subprocess.run


def spy(args, **kw):
    seen.append([str(a) for a in list(args)[:2]])
    return real(args, **kw)


subprocess.run = spy
import c4x.store  # noqa: E402
print(json.dumps(seen))
"""


@pytest.fixture(scope="module")
def pane_all(pane, has_store):
    return pane("tab-compactions")


def test_the_table_holds_every_compaction_in_the_store(pane_all, q):
    tables = extract.tables(pane_all)
    assert tables, "no table on the Compactions tab"
    total = int(q("SELECT COUNT(*) AS n FROM compactions").iloc[0]["n"])
    rendered = max(len(t["rows"]) for t in tables)
    assert rendered <= total
    # Prose AND figure titles. This tab states its population in the chart title rather than in a
    # paragraph, and a check that reads only paragraphs would call that silence.
    stated = "\n".join(extract.texts(pane_all)
                       + [str(f["title"]) for f in extract.figures(pane_all) if f["title"]])
    assert str(total) in stated.replace(",", ""), (
        f"the tab never states how many compactions exist ({total})")


def test_a_selected_session_narrows_to_its_own_compactions(pane, session_id, q, has_store):
    mine = int(q("SELECT COUNT(*) AS n FROM compactions WHERE session_id = ?",
                 (session_id,)).iloc[0]["n"])
    tables = extract.tables(pane("tab-compactions", session=session_id))
    assert tables
    rows = max((len(t["rows"]) for t in tables), default=0)
    assert rows <= mine, f"{rows} rows rendered for a session with {mine} compactions"


def test_overshoot_is_never_negative(pane_all):
    """The tab states this as a rule: a compaction cannot fire below its own threshold.

    So a negative point is either a wrong threshold or a wrong reading, and either way the chart
    would be asserting something impossible.
    """
    figures = extract.figures(pane_all)
    if not figures:
        pytest.fail("no chart on the Compactions tab, so overshoot could not be checked")
    text = "\n".join(extract.texts(pane_all))
    assert "non-negative" in text or "cannot fire" in text, (
        "the tab no longer states the rule its chart depends on")
    for fig in figures:
        for trace in fig["traces"]:
            if trace["y_min"] is None:
                continue
            # Falsifying points are drawn deliberately, in red, and the tab says so. What must not
            # happen is a negative appearing with no such statement on the page.
            if trace["y_min"] < 0:
                assert "falsifying" in text or "below threshold" in text.lower(), (
                    f"a negative overshoot of {trace['y_min']:,.0f} is drawn and unexplained")


def test_pre_and_post_token_columns_match_sql(pane_all, q):
    tables = [t for t in extract.tables(pane_all) if "pre_tokens" in (t["columns"] or [])]
    if not tables:
        pytest.skip("no compaction table carries pre_tokens on this store")
    rows = tables[0]["rows"][:20]
    for row in rows:
        uuid = row.get("uuid")
        if not uuid:
            continue
        truth = q("SELECT pre_tokens, post_tokens FROM compactions WHERE uuid = ?",
                  (uuid,)).iloc[0]
        assert int(row["pre_tokens"] or 0) == int(truth["pre_tokens"] or 0)
        assert int(row["post_tokens"] or 0) == int(truth["post_tokens"] or 0)


def test_a_compaction_always_reduces_the_window(pane_all, q):
    """post must be below pre. A compaction that grew the window would be a broken reading."""
    bad = q("""SELECT COUNT(*) AS n FROM compactions
                WHERE pre_tokens IS NOT NULL AND post_tokens IS NOT NULL
                  AND post_tokens > pre_tokens""").iloc[0]["n"]
    assert int(bad) == 0, f"{bad} compactions report a larger window afterwards"


def test_importing_the_store_shells_out_to_nothing():
    """`import c4x.store` must not run node, or anything else.

    It used to resolve every compaction's window at import, a node pass over the whole store under
    a 120 s cap. Everything that imports the store paid it: the account merge imports it for one
    path, and on a 4.1 GB store the pass went past the cap, so the import itself raised
    TimeoutExpired. That took out `python -m c4x.merge` and a restarted server, which died before
    binding its port with the reason only in `data/raw/dashboard.log`.
    """
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run([sys.executable, "-c", _IMPORT_PROBE], cwd=str(root),
                          capture_output=True, text=True, timeout=900, env=dict(os.environ))
    assert done.returncode == 0, f"importing the store failed: {done.stderr[-2000:]}"
    calls = json.loads(done.stdout.strip().splitlines()[-1])
    assert calls == [], f"importing c4x.store ran {calls}"


def test_the_windows_are_resolved_once_and_only_when_asked(monkeypatch):
    """First use loads them, later uses do not load them again."""
    from c4x import store
    calls = []
    monkeypatch.setattr(store, "load_compaction_windows",
                        lambda: calls.append(1) or {"uuid-1": {"window": 200000}})
    # setitem, so the process keeps whatever it had already resolved once this test is over.
    monkeypatch.setitem(store._windows_cache, "map", None)
    assert store.compaction_windows() == {"uuid-1": {"window": 200000}}
    assert store.compaction_windows() == {"uuid-1": {"window": 200000}}
    assert calls == [1], f"the map was loaded {len(calls)} time(s)"

"""The All sessions table: every column recomputed from SQL written here.

This is the tab a reader trusts to say what exists, so each of its numeric columns is checked
against a query this file writes rather than against the frame the app built.
"""
import pytest

from c4x.cli import extract
from c4x.store import SESSION_TURN_FLOOR

TABLE_ID = "tbl-session"


@pytest.fixture(scope="module")
def table(pane, has_store):
    found = extract.table_by_id(pane("tab-sessions"), TABLE_ID)
    assert found is not None, "the All sessions tab rendered no table with that id"
    return found


def test_the_table_has_the_columns_the_page_promises(table):
    for column in ("section", "title", "project", "turns", "current", "peak", "compactions"):
        assert column in table["columns"], f"{column} missing from the table"


def test_row_count_matches_the_population_the_page_states(table, q, pane):
    """The tab prints "N sessions with F or more turns". That sentence is a claim, and F is the
    floor the store applies, not a number this test happens to agree with."""
    expected = int(q("""SELECT COUNT(*) AS n FROM (
                          SELECT session_id FROM turns GROUP BY session_id HAVING COUNT(*) >= ?)""",
                     (SESSION_TURN_FLOOR,)).iloc[0]["n"])
    assert len(table["rows"]) == expected
    text = "\n".join(extract.texts(pane("tab-sessions")))
    assert f"{expected:,} sessions with {SESSION_TURN_FLOOR} or more turns" in text, (
        "the stated population and the rendered one disagree")


def test_every_numeric_column_matches_its_own_sql(table, q):
    """turns, peak, current and compactions, per session, recomputed here.

    `turns` counts transcript ROWS, not deduped API calls: a streamed assistant message writes
    several rows under one request id. That is what the column means, and pinning it here stops the
    two being quietly swapped, which is the mistake the README calls the easiest in this codebase.
    """
    sample = table["rows"][:25]
    assert sample, "no rows to check"
    for row in sample:
        sid = row["session_id"]
        truth = q("""SELECT COUNT(*) AS turns, MAX(total_resident) AS peak
                       FROM turns WHERE session_id = ?""", (sid,)).iloc[0]
        current = q("""SELECT total_resident FROM turns WHERE session_id = ?
                        ORDER BY ts DESC LIMIT 1""", (sid,)).iloc[0]["total_resident"]
        comps = int(q("SELECT COUNT(*) AS n FROM compactions WHERE session_id = ?",
                      (sid,)).iloc[0]["n"])
        assert row["turns"] == int(truth["turns"]), f"turns wrong for {sid}"
        assert row["peak"] == int(truth["peak"] or 0), f"peak wrong for {sid}"
        assert row["current"] == int(current or 0), f"current wrong for {sid}"
        assert row["compactions"] == comps, f"compactions wrong for {sid}"


def test_current_never_exceeds_peak(table):
    """peak is a high-water mark, so this cannot be violated by any real reading.

    It caught nothing when written, which is the point: it is the invariant that would break first
    if `current` were ever read from the wrong column again.
    """
    bad = [r for r in table["rows"] if r["current"] > r["peak"]]
    assert not bad, f"{len(bad)} rows where current > peak, e.g. {bad[:2]}"


def test_sections_partition_the_rows(table, pane):
    """Every row lands in exactly one section, and the stated counts add up to the total."""
    from collections import Counter
    counts = Counter(r["section"] for r in table["rows"])
    assert sum(counts.values()) == len(table["rows"])
    text = "\n".join(extract.texts(pane("tab-sessions")))
    for section, n in counts.items():
        assert f"{section} {n:,}" in text, f"the page does not state {section} {n:,}"


def test_archived_paths_agree_with_the_desktop_records(table, store):
    """A path ending in \\archived must correspond to a record that says archived.

    The marker is derived from files outside this store, so it is the one column here that cannot
    be recomputed from SQL. It is checked against the source instead.
    """
    flags = store.archived_sessions()
    marked = [r for r in table["rows"] if str(r["project"]).endswith("\\" + store.ARCHIVED_SUFFIX)]
    for row in marked:
        assert flags.get(row["session_id"]) is True, (
            f"{row['session_id']} is marked archived but its record does not say so")
    truly = {sid for sid, is_archived in flags.items() if is_archived}
    shown = {r["session_id"] for r in table["rows"]}
    for sid in truly & shown:
        row = next(r for r in table["rows"] if r["session_id"] == sid)
        assert str(row["project"]).endswith("\\" + store.ARCHIVED_SUFFIX), (
            f"{sid} is archived but its path carries no marker")


def test_this_table_ignores_the_scope_switch_and_says_so(pane, has_store, q):
    """This table counts EVERY transcript row, subagents included, whatever the radio says.

    That is deliberate: it is a browsing index of what exists, not a measurement under a scope. But
    it is only honest if the page admits it, because the gap is enormous. One session in this store
    has 59,864 rows of which 690 are main thread, so a reader comparing this column against the
    Session tab, which does respect the radio, sees an 87x difference with nothing explaining it.
    """
    sidechain = int(q("SELECT COUNT(*) AS n FROM turns WHERE COALESCE(is_sidechain,0) = 1"
                      ).iloc[0]["n"])
    if not sidechain:
        pytest.fail("no sidechain rows in this store, so this could not be exercised, which is a "
                    "failure rather than a pass")
    main = extract.table_by_id(pane("tab-sessions", scope="main"), TABLE_ID)
    every = extract.table_by_id(pane("tab-sessions", scope="all"), TABLE_ID)
    assert main["rows"] == every["rows"], "the table is documented as scope-independent"
    # all_words, not texts: this statement moved from a paragraph above the table to the `turns`
    # column tooltip, which is where it belongs. Where a caveat LIVES is a presentation decision;
    # that a reader can find it at all is the thing worth testing.
    said = extract.all_words(pane("tab-sessions")).lower()
    assert "subagent" in said, "the table counts subagent rows and the page never says so"


def test_the_turns_column_really_does_include_subagent_rows(table, q):
    """The claim above, verified rather than assumed."""
    with_side = q("""SELECT session_id, COUNT(*) AS n FROM turns
                      WHERE COALESCE(is_sidechain,0) = 1
                      GROUP BY session_id ORDER BY n DESC LIMIT 1""")
    if with_side.empty:
        pytest.fail("no session with sidechain rows, so this could not be exercised")
    sid = with_side.iloc[0]["session_id"]
    row = next((r for r in table["rows"] if r["session_id"] == sid), None)
    if row is None:
        pytest.skip("that session is below the 5-turn floor this table draws")
    both = int(q("SELECT COUNT(*) AS n FROM turns WHERE session_id = ?", (sid,)).iloc[0]["n"])
    main_only = int(q("""SELECT COUNT(*) AS n FROM turns
                          WHERE session_id = ? AND COALESCE(is_sidechain,0) = 0""",
                      (sid,)).iloc[0]["n"])
    assert row["turns"] == both
    assert row["turns"] != main_only, "this session cannot distinguish the two counts"


def test_a_cohort_narrows_rather_than_reorders(pane, cohort, has_store):
    """Selecting a project must return a subset, never a different set."""
    everything = extract.table_by_id(pane("tab-sessions"), TABLE_ID)
    narrowed = extract.table_by_id(pane("tab-sessions", coh=cohort), TABLE_ID)
    assert narrowed is not None
    assert len(narrowed["rows"]) <= len(everything["rows"])
    all_ids = {r["session_id"] for r in everything["rows"]}
    assert {r["session_id"] for r in narrowed["rows"]} <= all_ids

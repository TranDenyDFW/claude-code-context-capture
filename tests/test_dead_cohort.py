"""A cohort that was asked for and answers to nothing must mean nothing, never everything.

`cohort_sessions` returns an empty list for two opposite situations: no cohort was chosen, and a
cohort was chosen whose sessions are gone. Every filtering site read empty as "no restriction", so
deleting a project turned its filter into the whole store, silently, and the page showed MORE than
was asked for under a header naming the project that had just been deleted.

The shape was copied to five places and every copy had the same hole. These pin the rule at the one
home they now share, and at each site, because a helper nothing calls is a helper adopted once.

This repo met the same conflation twice before and both times guarded the INPUT SHAPE, which does
nothing for a well-formed cohort whose rows are gone. That is exactly what a delete leaves behind.
"""
import pandas as pd
import pytest

from c4x import store

DEAD = "project::Z:/deleted/for/ever"
BARE = "Z:/no/prefix/at/all"


def _frame():
    return pd.DataFrame({"session_id": ["a", "b", "c"], "project": ["P:/live"] * 3})


def test_a_named_cohort_is_told_apart_from_no_cohort():
    assert store.cohort_named(DEAD) is True
    assert store.cohort_named(None) is False
    assert store.cohort_named(store.COHORT_ALL) is False


def test_a_bare_path_is_still_not_a_named_cohort():
    """The earlier guard, kept. cohort_parts refuses an unprefixed string, and the delete path
    depends on that refusal: a bare path must not become a named-but-empty cohort either."""
    assert store.cohort_named(BARE) is False


def test_scoped_restricts_to_nothing_rather_than_everything():
    """THE DEFECT. A dead cohort emitted byte-identical SQL to no filter at all."""
    where, args = store.scoped(None, "all", cohort=DEAD)
    assert where.strip() == "AND 1 = 0", f"a dead cohort must match no row, got {where!r}"
    assert args == ()


def test_scoped_leaves_an_absent_cohort_alone():
    """The negative control. Without it, the fix above could pass by restricting everything."""
    assert store.scoped(None, "all", cohort=None)[0] == ""
    assert store.scoped(None, "all", cohort=store.COHORT_ALL)[0] == ""
    assert store.scoped(None, "all", cohort=BARE)[0] == ""


def test_restrict_to_cohort_empties_the_frame_for_a_dead_cohort():
    assert len(store.restrict_to_cohort(_frame(), DEAD)) == 0


def test_restrict_to_cohort_returns_everything_when_nothing_was_asked_for():
    assert len(store.restrict_to_cohort(_frame(), None)) == 3
    assert len(store.restrict_to_cohort(_frame(), store.COHORT_ALL)) == 3


def test_restrict_to_cohort_keeps_the_columns_it_was_given():
    """An empty frame with no columns breaks every caller that reads one by name."""
    out = store.restrict_to_cohort(_frame(), DEAD)
    assert list(out.columns) == ["session_id", "project"]


def test_the_session_default_declines_rather_than_picking_from_the_store(monkeypatch):
    """most_recent_session fell through to the newest session in the STORE, which is how a deleted
    project displayed a session from somewhere else under its own name."""
    from c4x.tabs.session import most_recent_session
    assert most_recent_session(DEAD) is None


def test_every_frame_filter_site_agrees(monkeypatch, has_store):
    """THE SWEEP, not the instance. Five sites shared the broken shape and the report named two.

    Each is asked the same question and must give the same answer: a dead cohort is an empty
    population everywhere, or the page contradicts itself between the picker and the table.
    """
    from c4x.cli import extract
    from c4x.tabs.sessions import sessions_table_layout
    from c4x.ui.header import selector_options

    assert len(store.restrict_to_cohort(store.session_rows(), DEAD)) == 0
    assert selector_options(DEAD) == []
    rows = extract.tables(sessions_table_layout(cohort=DEAD))[0]["rows"]
    assert rows == [] or len(rows) == 0


def test_a_live_cohort_is_unaffected(has_store):
    """The control that makes the rest mean something: if this ever returns 0, the fix above is
    restricting everything rather than only the dead case."""
    options = store.cohort_options()
    project = next((o["value"] for o in options if str(o["value"]).startswith("project::")), None)
    if project is None:
        pytest.skip("this store lists no project cohort")
    assert len(store.restrict_to_cohort(store.session_rows(), project)) > 0
    assert store.scoped(None, "all", cohort=project)[0].startswith("AND session_id IN")

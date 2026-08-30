"""A gesture on a chart narrows the table beside it, and the page says what it did.

Cross-filtering is the one feature here where doing nothing looks identical to working: a table
that never narrows and a table whose filter silently failed both show every row. So every check
below asserts the ROWS and the SENTENCE together, and the refusing branches get as much attention
as the filtering ones, because a filter that quietly clears a reader's selection is worse than one
that never fires.
"""
import pytest

from c4x.cli import extract
from c4x.ui.callbacks.crossfilter import (
    _reread_crossfilter,
    _sessions_crossfilter,
    selected_ids,
)


def said(note):
    return " ".join(extract.texts(note))


@pytest.fixture(scope="module")
def session_rows(pane, has_store):
    table = extract.table_by_id(pane("tab-sessions"), "tbl-session")
    assert table and table["rows"], "the All sessions tab rendered no rows to filter"
    return table["rows"]


# --- reading a Plotly selection -------------------------------------------
def test_a_selection_is_read_from_customdata_not_from_point_indices():
    """Plotly numbers a selected point WITHIN its trace, and the scatter draws one trace per
    section. Index 0 is a different session in each of them, so an index-based reading would
    filter to a set of sessions the reader never selected and there would be nothing on screen to
    show it had happened."""
    selection = {"points": [
        {"curveNumber": 0, "pointIndex": 0, "customdata": ["t", "p", 0, "aaa"]},
        {"curveNumber": 2, "pointIndex": 0, "customdata": ["t", "p", 0, "bbb"]},
    ]}
    assert selected_ids(selection) == ["aaa", "bbb"]


def test_no_selection_and_an_empty_selection_are_different_answers():
    """A box drawn over empty space selects nothing and must narrow the table to nothing. No box
    at all must leave the table whole. Collapsing the two makes one of them silently wrong."""
    assert selected_ids(None) is None
    assert selected_ids({}) is None
    assert selected_ids({"points": None}) is None
    assert selected_ids({"points": []}) == []


def test_a_point_without_customdata_is_skipped_rather_than_crashing():
    assert selected_ids({"points": [{"curveNumber": 0}, {"customdata": ["a", "b", 0, "keep"]}]}) \
        == ["keep"]
    assert selected_ids({"points": [{"customdata": ["too", "short"]}]}) == []


# --- the sessions cross-filter --------------------------------------------
def test_selecting_on_the_chart_narrows_the_table_to_that_selection(session_rows):
    picked = [r["session_id"] for r in session_rows[:3]]
    selection = {"points": [{"customdata": ["t", "p", 0, sid]} for sid in picked]}
    rows, note = _sessions_crossfilter(selection, session_rows)
    assert [r["session_id"] for r in rows] == picked
    assert f"Showing {len(picked):,} of {len(session_rows):,}" in said(note)


def test_no_selection_leaves_every_row_and_says_how_to_filter(session_rows):
    """Also the first-paint case: the table is filled by this callback, so a wrong refusal branch
    would render an empty table on arrival."""
    rows, note = _sessions_crossfilter(None, session_rows)
    assert len(rows) == len(session_rows)
    assert "Drag a box" in said(note)


def test_a_box_over_empty_space_empties_the_table_and_says_so(session_rows):
    rows, note = _sessions_crossfilter({"points": []}, session_rows)
    assert rows == []
    assert "no sessions" in said(note)
    assert "clear" in said(note).lower(), "an empty table with no way back out of it"


def test_a_selection_naming_sessions_that_are_not_in_the_table_is_not_a_crash(session_rows):
    rows, note = _sessions_crossfilter(
        {"points": [{"customdata": ["t", "p", 0, "no-such-session"]}]}, session_rows)
    assert rows == []
    assert said(note)


def test_the_filter_reads_the_store_rather_than_the_table_it_writes(session_rows):
    """Filtering twice from the unfiltered rows must WIDEN when the second selection is wider.

    Sourcing the rows from the table's own `data` would make every selection narrow the previous
    one, so a reader who selected a small region and then a large one would see the intersection
    and no sign of why.
    """
    narrow = {"points": [{"customdata": ["t", "p", 0, session_rows[0]["session_id"]]}]}
    wide = {"points": [{"customdata": ["t", "p", 0, r["session_id"]]} for r in session_rows[:5]]}
    _first, _note = _sessions_crossfilter(narrow, session_rows)
    second, _note = _sessions_crossfilter(wide, session_rows)
    assert len(second) == 5, "the second, wider selection did not widen the table"


def test_the_scatter_carries_the_id_the_filter_reads(pane, has_store):
    """The chart puts session_id at customdata[3]. If the scatter stopped doing that, every
    selection would filter to nothing and the note would say the selection was empty, which is a
    sentence about the reader's box rather than about the bug."""
    figures = extract.figures(pane("tab-sessions"))
    assert figures, "no scatter to filter from"
    from c4x.tabs.sessions import sessions_scatter
    table = extract.table_by_id(pane("tab-sessions"), "tbl-session")
    trace = sessions_scatter(table["rows"]).data[0]
    assert trace.customdata is not None
    known = {r["session_id"] for r in table["rows"]}
    assert all(point[3] in known for point in trace.customdata)


# --- the re-read cross-filter ---------------------------------------------
def held(n=200, population=1129):
    return {"rows": [{"reads": n - i, "target": f"f{i}"} for i in range(n)],
            "population": population}


def test_clicking_the_curve_keeps_the_groups_up_to_that_rank():
    rows, note = _reread_crossfilter({"points": [{"curveNumber": 0, "x": 10, "y": 26.6}]}, held())
    assert len(rows) == 10
    text = said(note)
    assert "worst 10 groups of 1,129" in text
    assert "26.6%" in text, "the share the reader clicked on is not repeated back"


def test_clicking_past_what_the_table_holds_says_so_rather_than_pretending():
    """The curve is drawn over every group and the table carries the worst 200 of them.

    A click at rank 900 asks for rows the table does not have. Returning all 200 with a note
    saying "showing the worst 900" would state a filter that did not happen.
    """
    rows, note = _reread_crossfilter({"points": [{"curveNumber": 0, "x": 900, "y": 98.0}]}, held())
    assert len(rows) == 200
    text = said(note)
    assert "holds the worst 200 of 1,129" in text
    assert "showing the worst 900" not in text.lower()


def test_a_click_on_the_reference_diagonal_does_nothing():
    """It is drawn to show what NO concentration would look like, and its x values are the two ends
    of the axis, so a click on it would filter to 1 group or to all of them. Neither means
    anything, and both would look like a working filter."""
    rows, note = _reread_crossfilter({"points": [{"curveNumber": 1, "x": 500, "y": 50}]}, held())
    assert len(rows) == 200
    assert "Click a point on the curve" in said(note)


def test_no_click_leaves_the_table_whole():
    rows, note = _reread_crossfilter(None, held())
    assert len(rows) == 200
    assert "All 200 rows" in said(note)


def test_a_click_with_no_usable_rank_changes_nothing():
    rows, _note = _reread_crossfilter({"points": [{"curveNumber": 0, "x": None}]}, held())
    assert len(rows) == 200
    rows, _note = _reread_crossfilter({"points": [{"curveNumber": 0, "x": "twelve"}]}, held())
    assert len(rows) == 200


def test_an_empty_store_does_not_break_either_filter():
    """Both run on first paint, before anything has been selected, and on a tab whose population
    can be empty under a narrow cohort."""
    assert _sessions_crossfilter(None, None)[0] == []
    assert _sessions_crossfilter(None, [])[0] == []
    assert _reread_crossfilter(None, None)[0] == []
    assert _reread_crossfilter(None, {"rows": [], "population": 0})[0] == []


def test_the_cost_tab_ships_the_rows_the_filter_reads(pane, has_store):
    """The Store carries the unfiltered rows AND the population size. Without the population the
    note above could not distinguish "the worst 200" from "all of them"."""
    body = pane("tab-cost")
    stores = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        if not hasattr(node, "_prop_names"):
            return
        if type(node).__name__ == "Store" and getattr(node, "id", None) == "reread-rows":
            stores.append(node)
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value)

    walk(body)
    assert stores, "the Cost tab ships no row store, so its cross-filter can never fire"
    data = stores[0].data
    assert "rows" in data and "population" in data
    assert data["population"] >= len(data["rows"])

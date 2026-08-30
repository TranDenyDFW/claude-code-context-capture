"""Arriving with nothing selected still produces evidence, and a finding is a door.

Two behaviours that only exist together. A finding that names a session is useless if clicking it
does nothing, and a default selection is useless if the page does not admit it is a default. Both
were empty states before: the Session tab rendered a single sentence and the Compare tab rendered a
prompt, so five of eight tabs answered a first-time reader with nothing at all.

The click itself is a Dash callback, so what is testable here is the callback function and the data
it reads. The wiring between them, that the table really does hand `derived_viewport_data` to it,
is asserted by checking the hidden columns are present in the table the tab renders rather than
assumed from the callback's signature.
"""
import pytest
from dash.exceptions import PreventUpdate

from c4x.cli import extract
from c4x.tabs.compare import default_arm_b
from c4x.tabs.session import most_recent_session
from c4x.tabs.summary import decisions
from c4x.ui.callbacks.selection import _finding_clicked
from c4x.ui.layout import TAB_IDS


@pytest.fixture(scope="module")
def findings(has_store):
    found = decisions()
    assert found, "the Overview has no findings, so none of this can be exercised"
    return found


def test_every_destination_a_finding_names_is_a_tab_that_exists(findings):
    """A stale id here is silent: the callback raises PreventUpdate and the click does nothing.

    That is the failure mode this catches. A finding pointing at "tab-waste" would still render,
    still look clickable, and still carry its evidence; only the click would be dead.
    """
    for finding in findings:
        target = finding.get("goes to")
        if target is None:
            continue
        assert target in TAB_IDS, f"{finding['finding']!r} points at {target}, which is not a tab"


def test_every_finding_that_quotes_a_session_can_be_opened(findings):
    """A finding whose evidence names a session must carry that session as data.

    The evidence string abbreviates it to eight characters, which is enough for a reader to
    recognise and not enough to select. The full id travels in a hidden column.
    """
    for finding in findings:
        if not finding.get("session_id"):
            continue
        assert finding.get("goes to"), (
            f"{finding['finding']!r} carries a session but no destination, so the click "
            "would select a session and leave the reader on the Overview")


def test_a_click_lands_on_the_tab_the_finding_names(findings):
    """The callback, driven with the rows the table would hand it."""
    rows = [dict(f) for f in findings]
    for index, finding in enumerate(rows):
        target = finding.get("goes to")
        cell = {"row": index, "column": 0, "column_id": "finding"}
        if not target:
            with pytest.raises(PreventUpdate):
                _finding_clicked(cell, rows)
            continue
        session, tab = _finding_clicked(cell, rows)
        assert tab == TAB_IDS.index(target)
        if finding.get("session_id"):
            assert session == finding["session_id"]


def test_a_finding_with_no_session_does_not_clear_the_one_you_had(findings):
    """It returns no_update rather than None.

    Returning None would clear the header selection as a side effect of following a finding that
    has nothing to say about any one session, which is a worse outcome than not moving at all.
    """
    from dash import no_update
    rows = [dict(f) for f in findings]
    without = [i for i, f in enumerate(rows) if f.get("goes to") and not f.get("session_id")]
    if not without:
        pytest.skip("every finding in this store names a session")
    session, _tab = _finding_clicked({"row": without[0], "column": 0}, rows)
    assert session is no_update


def test_a_click_outside_a_row_does_nothing(findings):
    rows = [dict(f) for f in findings]
    for cell in (None, {"row": None}, {"row": len(rows) + 5}, {"row": -1}):
        with pytest.raises(PreventUpdate):
            _finding_clicked(cell, rows)
    with pytest.raises(PreventUpdate):
        _finding_clicked({"row": 0}, [])


def test_the_findings_table_carries_the_columns_the_click_reads(pane, has_store):
    """The callback reads `goes to` and `session_id` off the rendered row.

    If they were dropped from the column list to tidy the table, `derived_viewport_data` would
    stop carrying them and every click would become a no-op. Hiding them is the supported way to
    keep the data and not the display, and this is what pins that distinction.
    """
    table = extract.table_by_id(pane("tab-summary"), "tbl-findings")
    assert table is not None, "the Overview rendered no findings table"
    for column in ("goes to", "session_id"):
        assert column in table["columns"], f"{column} is not in the table the click reads"
    assert table["rows"], "the findings table is empty"


def test_the_session_tab_defaults_to_something_real(pane, has_store, q):
    """With nothing selected it renders the most recent session, not an apology."""
    body = pane("tab-session", session=None)
    assert extract.tables(body) or extract.figures(body), (
        "the Session tab still renders nothing when nothing is selected")
    newest = q("SELECT session_id FROM turns GROUP BY 1 ORDER BY MAX(ts) DESC LIMIT 1"
               ).iloc[0]["session_id"]
    assert most_recent_session() == newest


def test_the_session_tab_says_the_selection_is_a_default(pane, has_store):
    """A defaulted page that looks selected is worse than an empty one: the reader believes the
    numbers describe whatever they last had in mind."""
    said = extract.all_words(pane("tab-session", session=None)).lower()
    assert "most recently active session" in said


def test_the_default_is_the_only_thing_that_changes(pane, session_id, has_store):
    """Selecting a session explicitly must not carry the banner with it."""
    said = extract.all_words(pane("tab-session", session=session_id)).lower()
    assert "most recently active session" not in said


def test_a_cohort_narrows_the_default(cohort, has_store, store):
    """A reader who picked a project and no session gets that project's newest session."""
    picked = most_recent_session(cohort)
    if picked is None:
        pytest.skip("that cohort has no sessions with turns")
    assert picked in set(store.cohort_sessions(cohort))


def test_compare_defaults_arm_b_to_a_session_that_is_not_arm_a(has_store, q):
    """Comparing a session with itself renders 1.0 down the ratio column, which is a correct
    answer to a question nobody asked and reads as a broken table."""
    newest = q("SELECT session_id FROM turns GROUP BY 1 ORDER BY MAX(ts) DESC LIMIT 1"
               ).iloc[0]["session_id"]
    assert default_arm_b(None, None) == newest
    assert default_arm_b(newest, None) != newest


def test_compare_admits_arm_b_was_chosen_for_you(pane, has_store):
    said = extract.all_words(pane("tab-compare")).lower()
    assert "starting point" in said, "the Compare tab picks an arm and does not say so"

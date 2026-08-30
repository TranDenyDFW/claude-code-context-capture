"""Every tab, under every selection, renders content rather than an apology.

`_render_tab` catches exceptions and returns a panel saying the tab could not be rendered, which
means a broken tab does not crash the app and does not fail any check that only asks "did something
come back". This asks whether what came back is the tab or the apology.

The Session tab shipped a defect that this shape of test catches and none of the others did: it
rendered correctly on the server every time, and the page reset itself. That half is checked in the
browser pass, not here, but a tab that raises is caught right here.
"""
import pytest

from c4x.cli import extract

FAILURE_MARKERS = ("could not be rendered", "Traceback")


def selections(session_id, cohort):
    """The selection states a reader can actually put the header into."""
    return [
        ("nothing selected", None, "main", None),
        ("one session", session_id, "main", None),
        ("one session, subagents", session_id, "all", None),
        ("a cohort", None, "main", cohort),
        ("a session inside a cohort", session_id, "main", cohort),
    ]


@pytest.mark.parametrize("tab_id", [
    "tab-summary", "tab-sessions", "tab-session", "tab-compactions", "tab-window",
    "tab-cost", "tab-compare", "tab-diagnostics",
])
def test_tab_renders_under_every_selection(tab_id, pane, session_id, cohort, has_store):
    for label, sid, scope, coh in selections(session_id, cohort):
        body = pane(tab_id, session=sid, scope=scope, coh=coh)
        text = "\n".join(extract.texts(body))
        for marker in FAILURE_MARKERS:
            assert marker not in text, f"{tab_id} / {label} rendered a failure panel: {text[:300]}"


@pytest.mark.parametrize("tab_id", [
    "tab-summary", "tab-sessions", "tab-session", "tab-compactions", "tab-window",
    "tab-cost", "tab-diagnostics",
])
def test_tab_produces_something_a_reader_can_use(tab_id, pane, session_id, cohort, has_store):
    """A tab that renders an empty div is not a working tab.

    Compare is excluded: its body is delivered by its own callback, and the pane is a prompt to
    choose an arm until that runs. That is covered in test_compare.py instead.
    """
    body = pane(tab_id, session=session_id, coh=cohort)
    tables, figures = extract.tables(body), extract.figures(body)
    text = "\n".join(extract.texts(body))
    assert tables or figures or len(text) > 80, f"{tab_id} rendered nothing substantial"


@pytest.mark.parametrize("tab_id", ["tab-session", "tab-compactions", "tab-window"])
def test_a_tab_that_needs_a_selection_says_so_when_there_is_none(tab_id, pane, has_store):
    """Rather than rendering an empty chart that reads as "no activity"."""
    body = pane(tab_id, session=None)
    text = "\n".join(extract.texts(body))
    assert len(text) > 40, f"{tab_id} says nothing at all with no selection"


def test_every_tab_states_the_population_it_describes(pane, session_id, has_store):
    """A number with no denominator is how a reader mistakes one session for the whole store.

    Summary and Compare state their own scope in their own words, so they are exempt by name here
    rather than by a pattern that would quietly exempt a tab that simply stopped saying it.
    """
    exempt = {"tab-summary", "tab-compare"}
    for tab_id in ("tab-sessions", "tab-session", "tab-compactions", "tab-window",
                   "tab-cost", "tab-diagnostics"):
        if tab_id in exempt:
            continue
        text = "\n".join(extract.texts(pane(tab_id, session=session_id)))
        assert ("session" in text.lower() or "store" in text.lower()), (
            f"{tab_id} never names its population")


def test_the_failure_panel_itself_works(app, has_store):
    """The apology path is code too. If it raised, a broken tab would take the app down.

    Proven by rendering a tab index that does not exist, which is the one input guaranteed to
    reach it.
    """
    body = app._render_tab(999, None, "main", None)
    assert body is not None
    text = "\n".join(extract.texts(body))
    assert len(text) > 0

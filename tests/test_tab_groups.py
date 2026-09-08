"""A tab declares what the header selection reaches, on the same line as its name.

It used to be a set beside the list, `SELECTION_SCOPED`, holding five of the eight ids. That is the
shape the registry was built to remove: two things that must agree with nothing checking that they
do. It had already drifted once, in the exemption tuple that held "tab-waste" for a commit after the
tab became "tab-cost", which switched the exemption off silently.

The grouping is also the answer to a question that was being asked of the page: picking a session
changed five of the eight tabs and left three identical, and the list interleaved them at positions
1, 2 and 8, with the only statement of the difference sitting in a tooltip.
"""
import pytest

from c4x.cli import extract
from c4x.ui.layout import SELECTION, SELECTION_SCOPED, STORE, TAB_IDS, TABS, build_layout


def test_every_tab_declares_a_group():
    """A tab with no declaration would silently fall through whichever branch is written last."""
    undeclared = [t[0] for t in TABS if len(t) < 4 or t[3] not in (STORE, SELECTION)]
    assert undeclared == [], f"these declare no population: {undeclared}"


def test_the_set_is_derived_and_not_a_second_list():
    """THE DEFECT'S SHAPE. If this is ever hand-written again, the two can disagree."""
    assert SELECTION_SCOPED == {t[0] for t in TABS if t[3] == SELECTION}
    assert SELECTION_SCOPED, "an empty set would make the assertion above vacuous"


def test_both_groups_have_members():
    """Neither branch of every consumer is dead code."""
    assert [t[0] for t in TABS if t[3] == STORE]
    assert [t[0] for t in TABS if t[3] == SELECTION]


def test_the_store_wide_tabs_come_first_and_stay_together():
    """The order IS the grouping, so a tab appended to the wrong half is a visible defect."""
    kinds = [t[3] for t in TABS]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == STORE else 1), (
        f"the two groups are interleaved: {[(t[0], t[3]) for t in TABS]}")


def test_the_four_tabs_that_were_reported_are_in_the_half_the_report_named():
    """Named ids, because a rule with no instances passes on an empty registry."""
    by_id = {t[0]: t[3] for t in TABS}
    assert by_id["tab-summary"] == STORE
    assert by_id["tab-sessions"] == STORE
    assert by_id["tab-diagnostics"] == STORE
    assert by_id["tab-session"] == SELECTION
    assert by_id["tab-cost"] == SELECTION


def test_no_id_is_listed_twice():
    assert len(TAB_IDS) == len(set(TAB_IDS))


@pytest.mark.parametrize("heading", ["All", "Selection"])
def test_the_dash_page_names_both_groups(heading):
    """A gap alone is a difference somebody has to notice and then interpret, and the
    interpretation is the whole content."""
    assert heading in extract.texts(build_layout())


def test_the_dash_page_still_draws_every_tab():
    """THE FAILURE A GROUPING INTRODUCES. A tab matching no group would simply vanish, and the two
    heading assertions above would not notice."""
    drawn = extract.texts(build_layout())
    missing = [label for _, label, _, _ in TABS if label not in drawn]
    assert missing == [], f"these tabs are not on the page: {missing}"

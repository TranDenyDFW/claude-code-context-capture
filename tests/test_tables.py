"""Cross-cutting rules every table in the app must obey.

Two properties were made uniform deliberately, and both are the kind that rot silently: a new table
added next month will look identical to these whether or not it follows them.

1. Every table sorts. Half of them used to and half did not, with nothing on screen to tell them
   apart, because evidence_block set sort_action and the tables built from TABLE_STYLE did not.
2. A column whose meaning is not obvious carries that meaning as a header tooltip. Those sentences
   used to sit in paragraphs above the tables, which pushed the table below the fold and put the
   explanation in front of a reader who had not yet seen the column.
"""
import pytest

from c4x.cli import extract
from c4x.theme import COLUMN_HELP

TABS = ["tab-summary", "tab-sessions", "tab-session", "tab-compactions", "tab-window",
        "tab-cost", "tab-diagnostics"]


def every_body(pane, session_id, other_session_id=None):
    """(label, rendered body) for everything a reader can actually reach.

    Not just the panes `_render_tab` returns: Compare's body and the Session tab's diff panel are
    delivered by their own callbacks, and a population that omits them reports their columns as
    columns nothing renders.
    """
    for tab_id in TABS:
        yield tab_id, pane(tab_id, session=session_id)
    # Sub-panels. A tab body shows only its FIRST panel, so a population built from tab bodies
    # alone goes blind to the other two the moment a tab gains a strip. That is exactly what
    # happened when Breakdown and Sources became Window: nine column-help entries looked dead
    # because the panel rendering them was one click away.
    from c4x.tabs.window import PANELS as WINDOW_PANELS
    from c4x.tabs.window import panel_body as window_panel
    for index, (key, _label, _description) in enumerate(WINDOW_PANELS):
        if index == 0:
            continue                               # already covered by the tab body above
        yield f"tab-window/{key}", window_panel(index, session_id, "main", None)
    import app as module
    # The Session tab's A/B diff, which renders the tool and message tables for a turn range and
    # is the only place `result_bytes` appears.
    _figure, diff = module._session_controls(80, [1, 10 ** 6], session_id, "main")
    yield "tab-session/diff", diff
    if other_session_id:
        # Compare's body comes from its own callback, exactly as the browser gets it.
        yield "tab-compare", module._cmp_render("session", other_session_id, session_id, None,
                                                "main")


def every_table(pane, session_id, other_session_id=None):
    """(label, table) for every table the app renders."""
    for label, body in every_body(pane, session_id, other_session_id):
        for table in extract.tables(body):
            yield label, table


def test_every_table_carries_tooltips_for_the_columns_that_need_them(
        pane, session_id, other_session_id, has_store):
    """A column in COLUMN_HELP must carry its help wherever it is rendered.

    This is the check that stops the registry drifting away from the tables. Adding an entry does
    nothing on its own; it has to reach the table, and a table built without header_help would pass
    every other test in this suite.
    """
    missing = []
    for label, table in every_table(pane, session_id, other_session_id):
        described = set(table["tooltips"])
        for column in table["columns"] or []:
            if column in COLUMN_HELP and column not in described:
                missing.append(f"{label}/{table['id']}: {column}")
    assert not missing, ("columns with help in the registry that never reach the reader: "
                         + ", ".join(sorted(set(missing))[:8]))


def test_no_tooltip_is_empty(pane, session_id, has_store):
    """An empty tooltip is worse than none: it invites a hover that says nothing."""
    for label, table in every_table(pane, session_id):
        for column, text in table["tooltips"].items():
            assert str(text).strip(), f"{label}/{table['id']}: {column} has a blank tooltip"


def test_the_registry_has_no_dead_entries(pane, session_id, other_session_id, has_store):
    """Every entry in COLUMN_HELP should describe a column that actually exists somewhere.

    A stale entry is harmless on screen and misleading in the source: it reads as documentation of
    a column the app no longer has.
    """
    rendered = set()
    for _label, table in every_table(pane, session_id, other_session_id):
        rendered |= set(table["columns"] or [])
    dead = sorted(set(COLUMN_HELP) - rendered)
    assert not dead, f"COLUMN_HELP describes columns nothing renders: {dead}"


def test_a_caveat_that_moved_to_a_tooltip_is_still_reachable(pane, session_id, has_store):
    """The statements that were deleted from the page during the tooltip pass.

    Named individually rather than counted, because "the prose got shorter" is satisfied just as
    well by deleting a true warning, which is the one outcome that would make this change harmful.
    """
    sessions = extract.all_words(pane("tab-sessions", session=session_id)).lower()
    assert "subagent" in sessions, "the turns caveat vanished with the paragraph"
    assert "not known" in sessions or "no desktop record" in sessions, (
        "the archived caveat vanished with the paragraph")
    cost = extract.all_words(pane("tab-cost", session=session_id)).lower()
    assert "subagent" in cost, "the Cost tab scope caveat vanished"


def test_tooltips_do_not_simply_repeat_the_column_name(pane, session_id, has_store):
    """A tooltip that restates the header teaches nothing and costs a hover."""
    for label, table in every_table(pane, session_id):
        for column, text in table["tooltips"].items():
            assert str(text).strip().lower() != column.lower(), (
                f"{label}/{table['id']}: {column} tooltip only repeats the name")
            assert len(str(text)) > len(column) + 12, (
                f"{label}/{table['id']}: {column} tooltip says almost nothing")


def test_every_table_sorts(pane, session_id, other_session_id, has_store):
    """Uniform sorting, checked on the rendered component rather than on the style dict.

    Reading TABLE_STYLE would prove only that a default exists. This asks each table what it
    actually got, which is what a reader clicking a header depends on.
    """
    unsortable = [f"{label}/{table['id']}"
                  for label, table in every_table(pane, session_id, other_session_id)
                  if not table["sorts"]]
    assert not unsortable, f"tables that do not sort: {unsortable}"


def test_a_tooltip_stays_up_while_you_read_it(pane, session_id, other_session_id, has_store):
    """Dash hides a tooltip after 2000ms by default.

    These run to about 200 characters, so the default cut the `turns` caveat off mid-sentence. Any
    table carrying tooltips must set tooltip_duration=None, and the tables that build their props
    without spreading TABLE_STYLE are the ones that quietly miss it: tbl-session, which has the
    longest tooltip in the app, was exactly that case.
    """
    impatient = [f"{label}/{table['id']}"
                 for label, table in every_table(pane, session_id, other_session_id)
                 if table["tooltips"] and table["tooltip_duration"] is not None]
    assert not impatient, f"tables whose tooltips hide while being read: {impatient}"


@pytest.mark.parametrize("column", sorted(COLUMN_HELP))
def test_each_help_entry_is_a_sentence_not_a_label(column):
    """Blunt, and long enough to actually resolve the ambiguity it exists for."""
    text = COLUMN_HELP[column]
    assert len(text) > 30, f"{column}: too short to say anything useful"
    assert text[0].isupper() or text.startswith("0"), f"{column}: not written as a sentence"

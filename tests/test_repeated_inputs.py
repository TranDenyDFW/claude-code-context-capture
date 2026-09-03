"""The same tool input, issued in more than one session.

Two things are easy to get wrong here and both would be invisible on the page. The first is the
grouping: `input_sha1` is the identity, and grouping by anything else silently answers a different
question. The second is the framing: within one session a re-read is re-billed on every later
request, and across sessions it is not, so presenting these two counts as the same kind of cost
would overstate the second by whatever the first's multiplier happens to be.
"""
import pytest

from c4x.cli import extract

TITLE = "The same input, issued in more than one session"


@pytest.fixture(scope="module")
def cost(pane, has_store):
    """Rendered once. The Cost tab is one of the slower panes and this module reads it four times.

    The `pane` fixture does not cache: it calls _render_tab, which is the point, because a cached
    render could not catch a tab that only works the first time.
    """
    return pane("tab-cost")


@pytest.fixture(scope="module")
def table(cost):
    found = [t for t in extract.tables(cost) if "beyond_one_each" in t["columns"]]
    if not found:
        pytest.fail("the Cost tab renders no cross-session repeat table")
    return found[0]


def test_every_row_really_does_span_more_than_one_session(table):
    """The HAVING is the whole filter. A row with one session belongs to the table above."""
    assert table["rows"], "the table is empty, so nothing here is exercised"
    bad = [r for r in table["rows"] if int(r["sessions"]) < 2]
    assert not bad, f"{len(bad)} rows do not span two sessions, e.g. {bad[:2]}"


def test_the_counts_are_recomputed_from_sql_written_here(table, cost, q):
    """Grouped by input_sha1, which is the claim the whole panel rests on.

    Recomputed by hash rather than by the tool and target the row displays: two different inputs
    to the same tool on the same target are DIFFERENT groups, and a check that grouped by what is
    on screen would pass on a page that had grouped by the wrong thing.
    """
    truth = q("""SELECT COUNT(*) AS groups, COALESCE(SUM(calls), 0) AS calls FROM (
                   SELECT COUNT(*) AS calls, COUNT(DISTINCT session_id) AS sessions
                     FROM tool_calls WHERE input_sha1 IS NOT NULL
                    GROUP BY input_sha1 HAVING sessions > 1)""").iloc[0]
    said = extract.all_words(cost)
    assert f"{int(truth['groups']):,} inputs repeat across sessions" in said
    assert f"{int(truth['calls']):,} calls in total" in said
    # The ROWS, not only the sentence above them. Checking the note alone passes a page whose
    # table groups by the tool and target it displays while the note still counts by hash: with
    # `target` NULL for Bash and ToolSearch that merges hundreds of unrelated inputs into one row,
    # and the summary line goes on saying the right thing above it. Replanting that exact defect
    # is what showed the first version of this test was checking the wrong half.
    assert len(table["rows"]) == min(200, int(truth["groups"]))
    top = q("""SELECT tool_name, target, COUNT(*) AS calls,
                      COUNT(DISTINCT session_id) AS sessions
                 FROM tool_calls WHERE input_sha1 IS NOT NULL
                GROUP BY input_sha1 HAVING sessions > 1
                ORDER BY calls DESC, sessions DESC LIMIT 1""").iloc[0]
    first = table["rows"][0]
    assert int(first["calls"]) == int(top["calls"]), (
        "the busiest row does not match the busiest group by input hash")
    assert int(first["sessions"]) == int(top["sessions"])
    assert first["tool"] == top["tool_name"]


def test_beyond_one_each_separates_the_two_kinds_of_repetition(table):
    """68 sessions each asking once is a different problem from one session asking 68 times.

    calls - sessions is what tells them apart, and it can never be negative: a session cannot
    contribute more distinct sessions than calls.
    """
    for row in table["rows"]:
        assert int(row["beyond_one_each"]) == int(row["calls"]) - int(row["sessions"])
        assert int(row["beyond_one_each"]) >= 0


def test_the_page_refuses_to_call_this_the_same_cost_as_a_re_read(cost):
    """Across sessions each call is paid once, because no cache spans sessions.

    The table directly above counts within-session re-reads, which ARE re-billed on every later
    request. Two tables of superficially similar numbers, one multiplied and one not: the page has
    to say which is which or a reader will add them together.
    """
    said = extract.all_words(cost)
    assert "NOT the same cost as the table above" in said
    assert "no cache spans sessions" in said


def test_a_row_with_no_target_says_why_rather_than_looking_broken(table):
    """Bash and ToolSearch have no target, and the store keeps a hash of the input, never the
    input. A blank cell with no explanation reads as missing data."""
    blank = [r for r in table["rows"] if not r.get("target")]
    if not blank:
        # The skip reason carries what the table actually held, so a skip on a store that
        # SHOULD have a blank-target row is diagnosable from the runner's tail alone.
        seen = [(r.get("tool"), repr(r.get("target")), type(r.get("target")).__name__)
                for r in table["rows"] if r.get("tool") in ("Agent", "Bash")][:4]
        pytest.skip(f"every repeated input in this store names a target; "
                    f"{len(table['rows'])} rows, first: {seen}")
    from c4x.theme import COLUMN_HELP
    assert "target" in COLUMN_HELP
    assert "hash of the input" in COLUMN_HELP["target"]


def test_selecting_one_session_says_the_question_cannot_be_asked(pane, session_id, has_store):
    """Rather than the panel simply not appearing.

    The HAVING can never be satisfied inside one session, so with a session selected this table
    vanishes. "There are none" and "this cannot be asked from here" are different answers and only
    one of them is true.
    """
    text = "\n".join(extract.texts(pane("tab-cost", session=session_id)))
    assert TITLE in text
    assert "Not answerable with a single session selected" in text


def test_a_cohort_narrows_it_rather_than_emptying_it(pane, cost, cohort, has_store):
    """A project is a population of sessions, so the question is still askable inside one."""
    tables = [t for t in extract.tables(pane("tab-cost", coh=cohort))
              if "beyond_one_each" in t["columns"]]
    whole = [t for t in extract.tables(cost) if "beyond_one_each" in t["columns"]]
    if not tables:
        pytest.skip("this cohort has no input repeated across two of its sessions")
    assert len(tables[0]["rows"]) <= len(whole[0]["rows"])
    for row in tables[0]["rows"]:
        assert int(row["sessions"]) >= 2

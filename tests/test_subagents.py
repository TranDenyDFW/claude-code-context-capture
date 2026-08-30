"""Subagent identity: which kind of agent a call asked for, and which turns it spawned.

Both fields were in the transcripts from the first harvest and read by nothing, so this store held
827 Agent rows that could not say what any of them ran, over a population where subagent work is
about 70% of all API calls. That is the largest block the app could not attribute to anything.

The tests here are deliberately split. The COLUMN must exist and behave whatever the store holds;
the DATA only exists once a backfill has run, and a store harvested before that is a legitimate
state rather than a failure. Conflating the two would either fail on an un-backfilled store or
pass on a store where the capture had quietly stopped working.
"""
import pytest

from c4x.cli import extract


def test_the_columns_exist_wherever_this_runs(store):
    """A store the schema migration reached, whether or not anything has filled them yet."""
    turns = {r["name"] for r in store.q("SELECT name FROM pragma_table_info('turns')").to_dict(
        "records")}
    calls = {r["name"] for r in store.q(
        "SELECT name FROM pragma_table_info('tool_calls')").to_dict("records")}
    assert "parent_uuid" in turns, "turns never gained parent_uuid"
    assert "subagent_type" in calls, "tool_calls never gained subagent_type"


def test_a_type_is_recorded_only_where_one_was_asked_for(q, has_store):
    """`subagent_type IS NOT NULL` has to mean "this spawned an agent".

    Defaulting the column to '' or 'none' on every other tool would make that query true for every
    row in the table, which is the one question the column exists to answer.
    """
    stray = q("""SELECT tool_name, COUNT(*) AS n FROM tool_calls
                  WHERE subagent_type IS NOT NULL AND tool_name NOT IN ('Agent', 'Task')
                  GROUP BY tool_name""")
    assert stray.empty, f"tools that spawn no agent carry an agent type: {stray.to_dict('records')}"


def test_no_type_is_the_empty_string(q, has_store):
    """NULL and '' would both render blank and mean different things."""
    blank = int(q("SELECT COUNT(*) AS n FROM tool_calls WHERE subagent_type = ''").iloc[0]["n"])
    assert blank == 0, f"{blank} rows carry an empty agent type instead of NULL"


def test_a_parent_link_points_at_a_record_this_store_has(q, has_store):
    """A dangling parent is worse than no parent: it looks like a tree and does not resolve.

    Some parents legitimately are not turns, because only assistant records with usage become
    turns, so this checks the shape rather than demanding every link resolve: a parent_uuid must
    never equal its own row's uuid, which is the corruption a careless backfill produces.
    """
    self_parented = int(q(
        "SELECT COUNT(*) AS n FROM turns WHERE parent_uuid IS NOT NULL AND parent_uuid = uuid"
    ).iloc[0]["n"])
    assert self_parented == 0, f"{self_parented} turns are their own parent"


def test_the_tab_names_the_agent_kinds_when_the_store_knows_them(pane, q, has_store):
    """The whole point of the capture, checked on the page rather than in the schema."""
    known = q("""SELECT COALESCE(subagent_type, '(not recorded)') AS agent, COUNT(*) AS n
                   FROM tool_calls WHERE tool_name IN ('Agent', 'Task')
                  GROUP BY agent ORDER BY n DESC""")
    if known.empty:
        pytest.skip("this store recorded no Agent calls")
    tables = [t for t in extract.tables(pane("tab-cost")) if "agent" in t["columns"]]
    assert tables, "the Cost tab renders no subagent table"
    rows = {r["agent"]: int(r["calls"]) for r in tables[0]["rows"]}
    for row in known.itertuples():
        assert rows.get(row.agent) == int(row.n), (
            f"the page says {rows.get(row.agent)} calls for {row.agent}, SQL says {int(row.n)}")


def test_an_agent_call_with_no_recorded_type_says_so(pane, q, has_store):
    """Some Agent calls omit subagent_type and take the default. The transcript recorded the
    OMISSION, so the page reports the omission rather than filling in what it thinks was meant."""
    unknown = int(q("""SELECT COUNT(*) AS n FROM tool_calls
                        WHERE tool_name IN ('Agent', 'Task') AND subagent_type IS NULL
                    """).iloc[0]["n"])
    if not unknown:
        pytest.skip("every Agent call in this store names its type")
    said = extract.all_words(pane("tab-cost"))
    assert "(not recorded)" in said
    assert "rather than assumed to be the default" in said


def test_the_fixture_carries_more_than_one_agent_kind(q, has_store):
    """Groundwork for CI, checked here because CI runs exactly this file against the fixture.

    One agent type makes "group by subagent_type" indistinguishable from "count the Agent calls",
    so a fixture with one type cannot exercise the column at all. Two is the minimum that can.
    This asserts a property of whatever store it runs against and is therefore also a real check
    on the live one.
    """
    kinds = q("""SELECT COUNT(DISTINCT subagent_type) AS n FROM tool_calls
                  WHERE subagent_type IS NOT NULL""").iloc[0]["n"]
    agents = int(q("SELECT COUNT(*) AS n FROM tool_calls WHERE tool_name = 'Agent'").iloc[0]["n"])
    if not agents:
        pytest.skip("this store recorded no Agent calls")
    if not kinds:
        pytest.skip("this store has Agent calls but no backfilled types yet")
    assert kinds >= 2, (
        f"only {kinds} agent type in this store, so grouping by it is the same as counting Agent "
        f"calls and nothing here exercises the column")


def test_spawned_turns_hang_off_a_call_this_store_recorded(q, has_store):
    """The link that makes per-agent attribution possible at all.

    A subagent's turns carry the uuid of the record that spawned them, and that record is the one
    the Agent tool_use sits on. Where both halves are present the join has to resolve, or the
    capture stored two fields that cannot be put together.
    """
    # BOTH halves have to be populated before the join can be expected to resolve, and "populated"
    # means backfilled rather than merely present. A store still being harvested incrementally has
    # parent_uuid on its newest turns and nothing on its Agent calls, so the join is empty for a
    # reason that is about the backfill and not about the capture. That state failed an earlier
    # version of this test, which is how the precondition came to be this specific.
    typed = int(q("SELECT COUNT(*) AS n FROM tool_calls WHERE subagent_type IS NOT NULL"
                  ).iloc[0]["n"])
    parented = int(q("SELECT COUNT(*) AS n FROM turns WHERE parent_uuid IS NOT NULL").iloc[0]["n"])
    if not typed or not parented:
        pytest.skip(f"this store has {typed} typed agent calls and {parented} parented turns, so "
                    f"the backfill has not run over both halves yet")
    joined = int(q("""SELECT COUNT(*) AS n FROM turns t
                       JOIN tool_calls c ON c.turn_uuid = t.parent_uuid
                      WHERE t.parent_uuid IS NOT NULL AND c.tool_name = 'Agent'""").iloc[0]["n"])
    assert joined > 0, (
        "no spawned turn joins to the Agent call that spawned it, so the two captured fields "
        "cannot be put together and neither is useful")

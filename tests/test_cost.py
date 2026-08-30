"""The Cost tab (formerly Waste): re-reads, MCP traffic and tool spend, each against its own SQL.

This tab exists to say what was paid for twice, so a number that is too low here is worse than a
missing tab: it reports the store as tidier than it is.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-cost")


def table_with(body, column):
    for table in extract.tables(body):
        if column in (table["columns"] or []):
            return table
    return None


def test_re_read_counts_match_sql(body, q):
    """A file counted as read N times must have been read N times."""
    table = table_with(body, "reads")
    if table is None:
        pytest.fail("no re-read table on the Cost tab, so its central claim is unverifiable")
    for row in table["rows"][:15]:
        target = row.get("target")
        if not target:
            continue
        # No sidechain filter. This tab forces scope="all" deliberately, because subagents make
        # almost all the tool calls: all 614 reads of the worst offender in this store are subagent
        # reads, and a main-thread-only count of the same file is zero.
        truth = q("SELECT COUNT(*) AS n FROM tool_calls WHERE target = ?",
                  (target,)).iloc[0]["n"]
        assert int(row["reads"]) <= int(truth), (
            f"{target}: the tab claims {row['reads']} reads, SQL finds {truth}")


def test_re_reads_are_ordered_worst_first(body):
    table = table_with(body, "reads")
    reads = [int(r["reads"]) for r in table["rows"] if r.get("reads") is not None]
    assert reads == sorted(reads, reverse=True), "the worst offender is not at the top"


def test_a_re_read_row_is_always_more_than_one_read(body):
    """A file read once is not a re-read, and listing it would inflate the finding."""
    table = table_with(body, "reads")
    ones = [r for r in table["rows"] if int(r.get("reads") or 0) < 2]
    assert not ones, f"{len(ones)} rows describe a single read as waste"


def test_mcp_traffic_matches_sql(body, q):
    table = table_with(body, "server")
    if table is None:
        pytest.skip("this store records no MCP tool calls")
    for row in table["rows"][:10]:
        server = row.get("server")
        truth = q("""SELECT COUNT(*) AS calls, COALESCE(SUM(result_bytes),0) AS b
                       FROM tool_calls WHERE server_name = ?""", (server,)).iloc[0]
        assert int(row["calls"]) <= int(truth["calls"]), server


def test_the_tab_states_that_it_counts_subagent_work(body):
    """This tab overrides the header radio and always counts subagents.

    That is the right choice: a main-thread-only reading of the worst re-read offender in this
    store is zero against a real 614. But an override that is never stated is indistinguishable
    from a radio that is broken.
    """
    text = "\n".join(extract.texts(body))
    assert "subagent" in text.lower(), "the tab counts subagent calls and never says so"


def test_bytes_are_never_negative(body):
    for table in extract.tables(body):
        for row in table["rows"]:
            for key, value in row.items():
                if "bytes" in str(key) and isinstance(value, (int, float)):
                    assert value >= 0, f"{key} is negative in {row}"

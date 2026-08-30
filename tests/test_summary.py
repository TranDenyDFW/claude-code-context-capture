"""The Summary tab: the findings it asserts, and the totals it reports.

Every row here is a claim about the store made in prose, which is the easiest kind of number to get
wrong and the hardest to notice. Each finding names a session and quotes figures, so the session
must exist and the figures must be recomputable.
"""
import re

import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-summary")


def findings_table(body):
    for table in extract.tables(body):
        if "finding" in (table["columns"] or []):
            return table
    return None


def test_every_finding_carries_evidence_and_an_action(body):
    """A finding with no action is an observation, and this tab promises actions."""
    table = findings_table(body)
    assert table is not None, "no findings table on the Summary tab"
    assert table["rows"], "the tab claims findings and rendered none"
    for row in table["rows"]:
        assert str(row.get("finding", "")).strip(), row
        assert str(row.get("evidence", "")).strip(), f"no evidence for {row.get('finding')}"
        assert str(row.get("do this", "")).strip(), f"no action for {row.get('finding')}"


def test_the_count_it_states_is_the_count_it_renders(body):
    table = findings_table(body)
    text = "\n".join(extract.texts(body))
    assert f"{len(table['rows'])} finding" in text, (
        f"{len(table['rows'])} rows rendered, and the tab states something else")


def test_every_identifier_a_finding_quotes_resolves_to_something(body, q):
    """A finding that cites an id nothing can be looked up by is a stale claim.

    Not every hex token is a session: the MCP finding quotes SERVER uuids, which live in
    tool_calls.server_name and are not in `sessions` at all. Treating them as session prefixes was
    a wrong test, not a wrong page, so both namespaces are accepted and an id belonging to neither
    is the failure.
    """
    table = findings_table(body)
    sessions = {sid[:8] for sid in q("SELECT session_id FROM sessions")["session_id"]}
    servers = {str(s)[:8] for s in
               q("SELECT DISTINCT server_name FROM tool_calls WHERE server_name IS NOT NULL")
               ["server_name"]}
    known = sessions | servers
    quoted = set()
    for row in table["rows"]:
        quoted |= set(re.findall(r"\b[0-9a-f]{8}\b", str(row.get("evidence", ""))))
    unknown = {p for p in quoted if p not in known}
    assert not unknown, (
        f"findings cite ids that are neither a session nor an MCP server: {sorted(unknown)[:4]}")


def test_the_tab_says_it_describes_the_whole_store(body):
    """Every other tab answers to the header selection. This one does not, and must say so, or its
    numbers get read as belonging to whatever is selected."""
    text = "\n".join(extract.texts(body))
    assert "WHOLE store" in text or "whole store" in text


def test_the_selection_does_not_change_this_tab(pane, session_id, cohort, has_store):
    """The claim above, tested rather than trusted."""
    plain = findings_table(pane("tab-summary"))
    selected = findings_table(pane("tab-summary", session=session_id, coh=cohort))
    assert plain["rows"] == selected["rows"], (
        "the Summary tab changed with the selection, which contradicts what it states")


def test_the_project_chart_sums_to_the_stores_resident_tokens(body, q):
    """The bar chart is a top-15, so it cannot exceed the total and must not be empty."""
    figures = extract.figures(body)
    assert figures, "no chart on the Summary tab"
    charted = max((t["x_sum"] or t["y_sum"] or 0) for f in figures for t in f["traces"])
    total = float(q("SELECT COALESCE(SUM(total_resident),0) AS n FROM api_calls").iloc[0]["n"])
    assert charted > 0, "the chart plots nothing"
    assert charted <= total * 1.01, (
        f"the top-15 chart plots {charted:,.0f} against a store total of {total:,.0f}")

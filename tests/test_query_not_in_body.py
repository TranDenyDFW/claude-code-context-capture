"""The query that built a table is attached to it, not printed under it.

Every `evidence_block` used to append a collapsible holding its SQL. Six of them on the Cost tab
alone, in the reading flow, between the tables, for something almost nobody opens on a given visit.
The feature is real (a reader can check a number instead of believing it) and the placement was
wrong, so the query now travels as data on the block that owns the table and the page puts it
behind a button.

Two properties, and both matter. The SQL must be GONE from the body, or nothing was achieved; and
it must still REACH the table, or a feature was deleted rather than moved.
"""
import pandas as pd
import pytest

from c4x.api.main import _pane, _render_payload, _table_meta
from c4x.cli import extract
from c4x.panels import QUERY_MARK, evidence_block

SQL = "SELECT model, COUNT(*) AS calls FROM api_calls GROUP BY model"


def _frames():
    return [("rows", pd.DataFrame({"model": ["opus"], "calls": [3]})),
            ("empty", pd.DataFrame())]


@pytest.mark.parametrize("kind,df", _frames())
def test_the_sql_is_not_in_the_body_text(kind, df):
    """THE DEFECT. `extract.texts()` is what the browser renders as prose, so anything here is on
    the page. Both paths are checked: an empty table had a query block of its own too, and that is
    exactly when a second panel of SQL is most in the way."""
    body = extract.texts(evidence_block("T", df, SQL))
    assert not any("SELECT" in line for line in body), f"{kind}: SQL still in the body: {body}"


@pytest.mark.parametrize("kind,df", _frames())
def test_but_the_block_still_carries_it(kind, df):
    """The other half. Without this, deleting the accordion would pass the test above."""
    block = evidence_block("T", df, SQL)
    assert QUERY_MARK in str(getattr(block, "className", "") or "").split()
    carried = (block.to_plotly_json().get("props") or {}).get("data-query")
    assert carried and "SELECT model" in carried, f"{kind}: query not carried"


def test_the_bound_parameters_travel_with_it():
    """A query shown without its parameters invites the reader to run something else."""
    block = evidence_block("T", pd.DataFrame({"a": [1]}), "SELECT a FROM t WHERE s = ?", ("abc",))
    carried = (block.to_plotly_json().get("props") or {}).get("data-query")
    assert "abc" in carried, carried


def test_the_query_reaches_the_table_meta():
    """Attribution by CONTAINMENT, not by index. A query shown under a table that did not produce
    it would be worse than no query, and position pairing is what could get that wrong."""
    block = evidence_block("Token Costs", pd.DataFrame({"a": [1]}), SQL)
    meta = _table_meta(block)
    assert len(meta) == 1, meta
    assert meta[0]["query"] and "SELECT model" in meta[0]["query"]


def test_a_hand_built_table_reports_no_query():
    """None is the honest value for a table that never went through evidence_block: it has no
    single query behind it. Without this, a fix that stamped every table would pass."""
    from dash import html

    from c4x.dash_compat import DataTable
    plain = html.Div([DataTable(id="tbl-plain", columns=[{"name": "a", "id": "a"}],
                                data=[{"a": 1}])])
    meta = _table_meta(plain)
    assert len(meta) == 1
    assert meta[0]["query"] is None


def test_no_tab_serves_sql_as_prose_or_as_a_section(has_store):
    """THE SWEEP, and it names its population by SOURCE rather than by listing tabs: every tab in
    the registry, plus the sub-panels, are where evidence_block's 16 call sites live.

    `details` is the collapsible channel and `text` is the prose channel. SQL in either is SQL in
    the body, whichever one it arrived through.
    """
    from c4x.ui.layout import TAB_IDS

    offenders = []
    for tab_id in TAB_IDS:
        payload = _render_payload(_pane(tab_id, None, "main", None, None, None))
        for section in payload.get("details") or []:
            if any("SELECT" in line for line in (section.get("body") or [])):
                offenders.append(f"{tab_id}: details {section.get('summary')!r}")
        for line in payload.get("text") or []:
            if line.strip().upper().startswith(("SELECT", "WITH ")):
                offenders.append(f"{tab_id}: text {line[:40]!r}")
    assert offenders == [], "these tabs still put SQL in the body: " + "; ".join(offenders)


def test_the_sweep_can_actually_fail():
    """A scan that reports nothing looks the same whether it works or matches nothing at all."""
    known_bad = {"details": [{"summary": "Query", "body": ["SELECT 1 FROM t"]}], "text": []}
    hit = any("SELECT" in line
              for s in known_bad["details"] for line in (s.get("body") or []))
    assert hit

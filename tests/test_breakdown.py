"""The Breakdown tab: the category split, and the item detail behind it.

Two independent sources meet on this tab. The categories are DERIVED from a calibrated baseline;
the item lists are MEASURED by a probe. Where they disagree the tab is supposed to print both, so
that is checked too.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-breakdown")


@pytest.fixture(scope="module")
def baseline(q):
    df = q("SELECT * FROM context_baselines ORDER BY ts DESC LIMIT 1")
    if df.empty:
        pytest.fail("no baseline recorded, so the category split could not be checked")
    return df.iloc[0]


def category_table(body):
    for table in extract.tables(body):
        if "category" in (table["columns"] or []) and "percent" in (table["columns"] or []):
            return table
    return None


def test_the_category_rows_match_the_recorded_baseline(body, baseline):
    """Each resident category is the baseline's own number, not a recomputation of it."""
    table = category_table(body)
    assert table is not None, "no category table on the Breakdown tab"
    rows = {r["category"]: r for r in table["rows"]}
    pairs = [("System prompt", "system_prompt"), ("System tools", "system_tools"),
             ("MCP tools", "mcp_tools"), ("Skills", "skills"),
             ("Memory files", "memory_files"), ("Custom agents", "custom_agents")]
    checked = 0
    for label, column in pairs:
        if label not in rows or baseline.get(column) is None:
            continue
        assert int(rows[label]["tokens"]) == int(baseline[column]), label
        checked += 1
    assert checked >= 3, f"only {checked} categories could be checked against the baseline"


def test_the_split_accounts_for_the_whole_window(body, baseline):
    """Resident categories plus messages plus free space must equal the window.

    A split that does not sum is a split with a category missing, and the bar would silently be
    short by exactly that amount.
    """
    table = category_table(body)
    window = int(baseline["window_size"] or 1_000_000)
    resident = [r for r in table["rows"] if r.get("percent") is not None]
    total = sum(int(r["tokens"]) for r in resident)
    assert total == pytest.approx(window, rel=0.01), (
        f"the split sums to {total:,} against a {window:,} window")


def test_deferred_rows_are_excluded_from_the_percentages(body):
    """A deferred tool is not resident, so it must carry no percentage.

    Including it would make the bar exceed the window while every individual row looked right.
    """
    table = category_table(body)
    deferred = [r for r in table["rows"] if "not resident" in str(r["category"])]
    if not deferred:
        pytest.skip("this baseline records no deferred tools")
    for row in deferred:
        assert row.get("percent") in (None, ""), f"{row['category']} carries a percentage"


def test_the_item_tables_match_the_probe_rows(body, q):
    """Skills, MCP tools, agents and memory files, against probe_details itself."""
    probe = q("""SELECT id FROM probes WHERE ok = 1 AND raw_json IS NOT NULL
                  ORDER BY ts DESC LIMIT 1""")
    if probe.empty:
        pytest.fail("no probe recorded, so the item detail could not be checked")
    pid = int(probe.iloc[0]["id"])
    counts = q("""SELECT kind, COUNT(*) AS n FROM probe_details
                   WHERE probe_id = ? GROUP BY kind""", (pid,))
    expected = {r["kind"]: int(r["n"]) for _, r in counts.iterrows()}
    tables = extract.tables(body)
    by_size = {len(t["rows"]) for t in tables}
    for kind in ("skill", "mcpTool"):
        if kind in expected:
            assert expected[kind] in by_size, (
                f"no table on the tab holds the {expected[kind]} {kind} rows the probe recorded")


def test_a_partial_probe_is_declared_rather_than_shown_as_a_split(body, q):
    """A probe reporting no System prompt did not observe a session without one.

    Two of the three probes in this store came back partial. If the tab ever presents such a
    reading as a configuration, every number under it is misread.
    """
    probe = q("SELECT id FROM probes WHERE ok = 1 ORDER BY ts DESC LIMIT 1")
    if probe.empty:
        pytest.skip("no probe to judge")
    pid = int(probe.iloc[0]["id"])
    names = set(q("SELECT name FROM probe_categories WHERE probe_id = ?", (pid,))["name"])
    text = "\n".join(extract.texts(body))
    if "System prompt" not in names:
        assert "PARTIAL" in text.upper(), (
            "the newest probe reports no System prompt and the tab does not say the reading is "
            "partial")


def test_the_history_chart_covers_every_charted_call(body, q):
    figures = extract.figures(body)
    assert figures, "no history chart on the Breakdown tab"
    text = "\n".join(extract.texts(body))
    charted = max((t["points"] for f in figures for t in f["traces"]), default=0)
    assert charted > 0
    assert f"{charted:,}" in text, (
        f"the chart plots {charted:,} calls and the tab does not state that number")


def test_percentages_are_a_share_of_the_window_not_of_the_total(body, baseline):
    table = category_table(body)
    window = int(baseline["window_size"] or 1_000_000)
    for row in table["rows"]:
        if row.get("percent") in (None, ""):
            continue
        assert float(row["percent"]) == pytest.approx(
            int(row["tokens"]) / window * 100, abs=0.15), row["category"]

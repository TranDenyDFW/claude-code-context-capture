"""The Window tab: three panels replacing the Breakdown and Sources tabs.

Merged from tests/test_breakdown.py and tests/test_sources.py, which covered the two tabs this one
absorbed. Every check they made is here; the tab ids changed, the assertions did not.

Two sources meet on this tab and the tests keep them apart on purpose. Composition is DERIVED from
a calibrated baseline; Items is MEASURED by a probe. Where they disagree the tab prints both, and
that is checked rather than assumed.
"""
import pytest

from c4x.cli import extract
from c4x.tabs.window import PANELS, panel_body

# Panel indices, derived from the registry rather than written out, so adding a panel cannot leave
# a test asserting on the wrong one. Splitting Items into Configuration and Conversation renumbered
# Injected, and a hardcoded 2 would have kept passing against the wrong panel.
_KEYS = [key for key, _label, _description in PANELS]
COMPOSITION = _KEYS.index("composition")
CONFIGURATION = _KEYS.index("configuration")
CONVERSATION = _KEYS.index("conversation")
INJECTED = _KEYS.index("injected")


@pytest.fixture(scope="module")
def composition(session_id, has_store):
    return panel_body(COMPOSITION, session_id, "main", None)


@pytest.fixture(scope="module")
def items(session_id, has_store):
    """The configuration half, which is what the old Items panel's checks were written against."""
    return panel_body(CONFIGURATION, session_id, "main", None)


@pytest.fixture(scope="module")
def injected(session_id, has_store):
    return panel_body(INJECTED, session_id, "main", None)


@pytest.fixture(scope="module")
def baseline(q):
    df = q("SELECT * FROM context_baselines ORDER BY ts DESC LIMIT 1")
    if df.empty:
        pytest.fail("no baseline recorded, so the category split could not be checked")
    return df.iloc[0]


def category_table(body):
    for table in extract.tables(body):
        columns = table["columns"] or []
        if "category" in columns and "percent" in columns:
            return table
    return None


# ---------------------------------------------------------------------------
# The panels themselves
# ---------------------------------------------------------------------------
def test_the_tab_renders_its_first_panel_without_a_click(pane, session_id, has_store):
    """A strip above an empty container reads as a tab with nothing in it."""
    body = pane("tab-window", session=session_id)
    assert extract.tables(body) or extract.figures(body), "Window rendered no content on arrival"


@pytest.mark.parametrize("index", range(len(PANELS)))
def test_every_panel_renders(index, session_id, has_store):
    body = panel_body(index, session_id, "main", None)
    text = "\n".join(extract.texts(body))
    assert "could not be rendered" not in text
    assert extract.tables(body) or extract.figures(body) or len(text) > 80


def test_each_panel_states_what_it_is(session_id, has_store):
    """Three panels with different SOURCES. A reader who does not notice will compare a derived
    number against a measured one and conclude the app contradicts itself."""
    for index, (_key, _label, description) in enumerate(PANELS):
        said = extract.all_words(panel_body(index, session_id, "main", None))
        assert description[:40] in said, f"panel {index} does not state what it is"


def test_no_panel_drops_content_from_the_builder_behind_it(session_id, has_store):
    """Content conservation across the merge, as an invariant rather than a number.

    Splitting a page into panels is only a reorganisation if nothing is lost; otherwise it is a
    deletion wearing a nicer layout. The first version of this test asserted 11 tables and 2
    figures, which are facts about the author's store and not about the code: against the CI
    fixture the same correct panels render 14, and the test failed on a difference in the DATA.

    Each panel is compared against the builder it wraps instead, which holds on any store.
    """
    from c4x.breakdown import composition_blocks
    from c4x.probe_detail import conversation_blocks, probe_detail_blocks
    from c4x.tabs.sources import sources_layout
    from c4x.tabs.window import _latest_baseline

    baseline = _latest_baseline()
    expected = {
        "composition": composition_blocks(False, session_id, None),
        "configuration": probe_detail_blocks(baseline),
        "conversation": conversation_blocks(baseline),
        "injected": sources_layout(session_id, "main", None),
    }
    for index, (key, label, _description) in enumerate(PANELS):
        panel = panel_body(index, session_id, "main", None)
        built = expected[key]
        assert len(extract.tables(panel)) == len(extract.tables(built)), (
            f"{label} renders fewer tables than its builder produces")
        assert len(extract.figures(panel)) == len(extract.figures(built)), (
            f"{label} renders fewer figures than its builder produces")


def test_the_panels_cover_every_builder_the_two_old_tabs_used(session_id, has_store):
    """And that all four builders are actually reachable, so none was orphaned by the merge."""
    keys = {key for key, _label, _description in PANELS}
    assert keys == {"composition", "configuration", "conversation", "injected"}


def test_no_panel_is_a_scroll_in_disguise(session_id, has_store):
    """Splitting a page is only a fix if the pieces are actually smaller.

    The first version of this tab moved Breakdown's 5.7 screens into an Items panel of 5.4, which
    is a rename rather than a reorganisation. Table count is the proxy this can measure without a
    browser: every one carries a title, a note, twelve rows and a query accordion.
    """
    from c4x.cli import extract as ex
    fat = [(PANELS[i][1], len(ex.tables(panel_body(i, session_id, "main", None))))
           for i in range(len(PANELS))]
    over = [(label, n) for label, n in fat if n > 6]
    assert not over, f"panels carrying too much to read in one go: {over}"


def test_an_out_of_range_panel_index_does_not_raise(session_id, has_store):
    """The Store can hold a stale index after the panel list changes."""
    for index in (-3, 99, None):
        body = panel_body(index, session_id, "main", None)
        assert body is not None


# ---------------------------------------------------------------------------
# Composition, from tests/test_breakdown.py
# ---------------------------------------------------------------------------
def test_the_category_rows_match_the_recorded_baseline(composition, baseline):
    table = category_table(composition)
    assert table is not None, "no category table on the Composition panel"
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


def test_the_split_accounts_for_the_whole_window(composition, baseline):
    table = category_table(composition)
    window = int(baseline["window_size"] or 1_000_000)
    resident = [r for r in table["rows"] if r.get("percent") is not None]
    total = sum(int(r["tokens"]) for r in resident)
    assert total == pytest.approx(window, rel=0.01), (
        f"the split sums to {total:,} against a {window:,} window")


def test_deferred_rows_are_excluded_from_the_percentages(composition):
    """A deferred tool is not resident, so including it would push the bar past the window while
    every individual row still looked right."""
    table = category_table(composition)
    deferred = [r for r in table["rows"] if "not resident" in str(r["category"])]
    if not deferred:
        pytest.skip("this baseline records no deferred tools")
    for row in deferred:
        assert row.get("percent") in (None, ""), f"{row['category']} carries a percentage"


def test_percentages_are_a_share_of_the_window_not_of_the_total(composition, baseline):
    table = category_table(composition)
    window = int(baseline["window_size"] or 1_000_000)
    for row in table["rows"]:
        if row.get("percent") in (None, ""):
            continue
        assert float(row["percent"]) == pytest.approx(
            int(row["tokens"]) / window * 100, abs=0.15), row["category"]


def test_the_history_chart_covers_every_charted_call(composition):
    figures = extract.figures(composition)
    assert figures, "no history chart on the Composition panel"
    text = extract.all_words(composition)
    charted = max((t["points"] for f in figures for t in f["traces"]), default=0)
    assert charted > 0
    assert f"{charted:,}" in text, (
        f"the chart plots {charted:,} calls and the panel does not state that number")


# ---------------------------------------------------------------------------
# Items, from tests/test_breakdown.py
# ---------------------------------------------------------------------------
def test_the_item_tables_match_the_probe_rows(items, q):
    probe = q("""SELECT id FROM probes WHERE ok = 1 AND raw_json IS NOT NULL
                  ORDER BY ts DESC LIMIT 1""")
    if probe.empty:
        pytest.fail("no probe recorded, so the item detail could not be checked")
    pid = int(probe.iloc[0]["id"])
    counts = q("SELECT kind, COUNT(*) AS n FROM probe_details WHERE probe_id = ? GROUP BY kind",
               (pid,))
    expected = {r["kind"]: int(r["n"]) for _, r in counts.iterrows()}
    sizes = {len(t["rows"]) for t in extract.tables(items)}
    for kind in ("skill", "mcpTool"):
        if kind in expected:
            assert expected[kind] in sizes, (
                f"no table holds the {expected[kind]} {kind} rows the probe recorded")


def test_a_partial_probe_is_declared_rather_than_shown_as_a_split(items, q):
    """A probe reporting no System prompt did not observe a session without one."""
    probe = q("SELECT id FROM probes WHERE ok = 1 ORDER BY ts DESC LIMIT 1")
    if probe.empty:
        pytest.skip("no probe to judge")
    pid = int(probe.iloc[0]["id"])
    names = set(q("SELECT name FROM probe_categories WHERE probe_id = ?", (pid,))["name"])
    if "System prompt" not in names:
        assert "PARTIAL" in extract.all_words(items).upper(), (
            "the newest probe reports no System prompt and the panel does not say so")


# ---------------------------------------------------------------------------
# Injected, from tests/test_sources.py
# ---------------------------------------------------------------------------
def table_with(body, column):
    for table in extract.tables(body):
        if column in (table["columns"] or []):
            return table
    return None


def test_the_record_census_matches_the_store(injected, q):
    """record_types is (type, n): a census already, one row per type carrying its own count."""
    table = table_with(injected, "type") or table_with(injected, "record_type")
    if table is None:
        pytest.skip("no record census on this store")
    census = q("SELECT type, n FROM record_types")
    truth = {str(r["type"]): int(r["n"]) for _, r in census.iterrows()}
    if not truth:
        pytest.fail("record_types is empty, so the census could not be verified")
    compared = 0
    for row in table["rows"]:
        key = str(row.get("type") or row.get("record_type"))
        if key not in truth:
            continue
        shown = row.get("n") if isinstance(row.get("n"), (int, float)) else row.get("records")
        if not isinstance(shown, (int, float)):
            continue
        assert int(shown) == truth[key], f"{key}: panel {shown} vs store {truth[key]}"
        compared += 1
    assert compared > 0, "no census row could be compared"


def test_hook_events_are_reported_when_present(injected, q):
    n = int(q("SELECT COUNT(*) AS n FROM hook_events").iloc[0]["n"])
    if not n:
        pytest.skip("no hook events in this store")
    assert "hook" in extract.all_words(injected).lower(), (
        "the store holds hook events and the panel never mentions them")


def test_every_table_carries_the_query_that_produced_it(injected):
    """This panel is evidence-first: a number a reader cannot reproduce is not evidence."""
    assert extract.tables(injected), "the Injected panel rendered no tables at all"
    assert "SELECT" in extract.all_words(injected).upper(), "no SQL is shown beside any table"

"""The charts added where a table hid the shape, and the shading that marks outliers in place.

A chart is easy to get wrong in a way no render test notices: it draws, it looks plausible, and its
numbers are a share of the wrong denominator. Every check here recomputes what the chart claims,
from SQL or from the rows it was handed, rather than asserting that a figure came back.
"""
import pandas as pd
import pytest

from c4x.cli import extract
from c4x.theme import TABLE_STYLE, heat_cells, heated


def raw_figures(node, found=None):
    """Every plotly figure in a rendered tree, as the figure OBJECT.

    `extract.figures` summarises deliberately: it reports extents and point counts, which is what
    the CLI should print and is not enough to check that a cumulative series never goes down. That
    check needs the series, so this walks for the objects instead. The two are kept separate on
    purpose rather than widening the CLI payload to serve one test.
    """
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            raw_figures(child, found)
        return found
    if not hasattr(node, "_prop_names"):
        return found
    if type(node).__name__ == "Graph":
        if getattr(node, "figure", None) is not None:
            found.append(node.figure)
        return found
    for name in node._prop_names:
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
            raw_figures(value, found)
    return found


def figure_titled(node, fragment):
    """The one figure whose title contains `fragment`, or a failure naming what was there."""
    every = raw_figures(node)
    match = [f for f in every
             if fragment.lower() in str(getattr(getattr(f.layout, "title", None), "text", "")
                                        or "").lower()]
    assert match, ("no figure titled like %r; titles present: %s"
                   % (fragment, [str(getattr(getattr(f.layout, "title", None), "text", ""))
                                 for f in every]))
    return match[0]


# --- shading ---------------------------------------------------------------
def test_shading_is_emitted_shallowest_first():
    """Dash applies matching style rules in order and the LAST match wins.

    A value in the top 5% matches every band, so emitting deepest-first would give every shaded
    cell the shallowest colour and the column would be flat. Nothing about the rendered page says
    which order was used, so it is pinned here.
    """
    rows = [{"n": v} for v in range(1, 101)]
    rules = heat_cells(rows, "n")
    thresholds = [float(r["if"]["filter_query"].split(">=")[1]) for r in rules]
    assert thresholds == sorted(thresholds), "bands are not emitted shallowest first"
    assert len(rules) == 4


def test_shading_buckets_by_rank_not_by_value():
    """One 100x outlier must not put every other row in the same band.

    By value, a column of [1..10, 5000] has a range dominated by one row and every band edge lands
    beside it. By rank the edges sit inside the body of the data, which is the only version that
    says anything about the rows a reader is actually looking at.
    """
    rows = [{"n": v} for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 5000]]
    thresholds = [float(r["if"]["filter_query"].split(">=")[1]) for r in heat_cells(rows, "n")]
    assert max(thresholds) < 5000, "the deepest band starts at the outlier, so it shades only it"
    assert min(thresholds) <= 6


def test_shading_refuses_where_it_would_say_nothing():
    assert heat_cells([{"n": 1}, {"n": 2}], "n") == [], "four rows cannot support four bands"
    assert heat_cells([{"n": 5}] * 20, "n") == [], "a constant column has no outliers"
    assert heat_cells([{"n": None}] * 20, "n") == []
    assert heat_cells([{"other": 1}] * 20, "n") == []


def test_inverting_flips_both_the_order_and_the_comparison():
    """Reversing only the colours would shade the whole column its deepest tone."""
    rows = [{"n": v} for v in range(1, 101)]
    rules = heat_cells(rows, "n", invert=True)
    assert all("<=" in r["if"]["filter_query"] for r in rules)
    thresholds = [float(r["if"]["filter_query"].split("<=")[1]) for r in rules]
    assert thresholds == sorted(thresholds, reverse=True), "inverted bands are in the wrong order"


def test_shading_keeps_the_striping_every_other_table_has():
    """`heated` exists so no caller has to remember to merge these two."""
    rows = [{"n": v} for v in range(1, 60)]
    style = heated(rows, "n")
    assert style["style_data_conditional"][0] == TABLE_STYLE["style_data_conditional"][0]
    assert len(style["style_data_conditional"]) > len(TABLE_STYLE["style_data_conditional"])
    assert TABLE_STYLE["style_data_conditional"] == [
        {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"}], (
        "heated mutated the shared style rather than copying it")


# --- the composition treemap ----------------------------------------------
def test_the_composition_treemap_parent_is_the_sum_of_its_children(has_store):
    """branchvalues="total" means a parent states its own value.

    If Configuration's value were not exactly the sum of the category slices under it, Plotly would
    draw a parent larger or smaller than its contents and the areas would stop being comparable.
    The alternative, "remainder", hides that by inventing a residual slice, which is a quiet
    correction this app does not make anywhere.
    """
    from c4x.breakdown import breakdown_fields, composition_treemap, latest_baseline
    baseline = latest_baseline()
    if baseline is None:
        pytest.fail("no baseline in this store, so the composition treemap cannot be exercised")
    fields, _err = breakdown_fields()
    resident = [f["col"] for f in fields if f["kind"] == "resident"]
    labels = {f["col"]: f["label"] for f in fields}
    fig = composition_treemap(baseline, resident, labels, 5000, 100000, 1000000)
    trace = fig.data[0]
    assert trace.branchvalues == "total"
    by_label = dict(zip(trace.labels, trace.values))
    children = [v for label, parent, v in zip(trace.labels, trace.parents, trace.values)
                if parent == "Configuration"]
    if "Configuration" in by_label:
        assert by_label["Configuration"] == sum(children)
        assert by_label["Configuration"] == int(baseline["static_total"])


def test_the_composition_treemap_covers_the_whole_window(has_store):
    from c4x.breakdown import breakdown_fields, composition_treemap, latest_baseline
    baseline = latest_baseline()
    if baseline is None:
        pytest.fail("no baseline in this store")
    fields, _err = breakdown_fields()
    resident = [f["col"] for f in fields if f["kind"] == "resident"]
    labels = {f["col"]: f["label"] for f in fields}
    window, messages = 1000000, 5000
    free = window - messages - int(baseline["static_total"])
    trace = composition_treemap(baseline, resident, labels, messages, free, window).data[0]
    top = sum(v for parent, v in zip(trace.parents, trace.values) if parent == "")
    assert top == window, f"the top level sums to {top:,}, not the {window:,} window"


# --- the configuration treemap --------------------------------------------
def test_the_configuration_treemap_draws_every_sized_item(has_store, q):
    """321 skills is the case the proportional bar cannot serve, so all of them must be drawn."""
    from c4x.probe_detail import CONFIGURATION_KINDS, configuration_treemap, latest_probe
    probe = latest_probe()
    if probe is None:
        pytest.fail("no probe in this store, so the configuration treemap cannot be exercised")
    pid = int(probe["id"])
    figure, shown, dropped = configuration_treemap(pid)
    marks = ",".join("?" * len(CONFIGURATION_KINDS))
    truth = q(f"""SELECT SUM(tokens > 0) AS sized, SUM(COALESCE(tokens,0) = 0) AS flat,
                         COUNT(DISTINCT CASE WHEN tokens > 0 THEN kind END) AS kinds
                    FROM probe_details WHERE probe_id = ? AND kind IN ({marks})""",
              (pid, *CONFIGURATION_KINDS)).iloc[0]
    if not int(truth["sized"]):
        pytest.fail("this probe recorded no sized configuration items")
    assert shown == int(truth["sized"])
    assert dropped == int(truth["flat"])
    assert len(figure.data[0].labels) == shown + int(truth["kinds"]), (
        "the node count is not one per item plus one per kind")


def test_a_zero_token_item_is_never_given_an_area(has_store):
    """`loaded` and `tokens` mean different things: an item listed at zero was seen and is not
    resident. An area chart about what occupies the window must not give it one."""
    from c4x.probe_detail import configuration_treemap, latest_probe
    probe = latest_probe()
    if probe is None:
        pytest.fail("no probe in this store")
    figure, _shown, dropped = configuration_treemap(int(probe["id"]))
    if not dropped:
        pytest.skip("this probe recorded no zero-token items")
    assert all(v > 0 for v in figure.data[0].values)


def test_item_names_are_qualified_so_two_kinds_cannot_merge(has_store):
    """Plotly identifies a treemap node by its label. Two items sharing one become a single slice
    carrying both values, which is a wrong number rather than a cosmetic collision."""
    from c4x.probe_detail import configuration_treemap, latest_probe
    probe = latest_probe()
    if probe is None:
        pytest.fail("no probe in this store")
    figure, _shown, _dropped = configuration_treemap(int(probe["id"]))
    labels = list(figure.data[0].labels)
    assert len(labels) == len(set(labels)), "two treemap nodes share a label and would merge"


# --- the re-read curve, and the cards it corrected -------------------------
@pytest.fixture(scope="module")
def cost_pane(pane, has_store):
    return pane("tab-cost")


def test_the_curve_is_cumulative_and_reaches_the_whole_population(cost_pane):
    curve = figure_titled(cost_pane, "Concentration of re-reading")
    y = list(curve.data[0].y)
    assert y == sorted(y), "a cumulative share that goes down is not cumulative"
    assert 99.9 <= y[-1] <= 100.1, f"the curve ends at {y[-1]}, not 100%"
    assert y[0] > 0


def test_the_curve_is_measured_over_more_groups_than_the_table_shows(cost_pane, q):
    """The table carries LIMIT 200. A share measured inside it is a share of the head, and the top
    ten of a top-200 list is 5% of the rows by construction rather than by finding anything."""
    points = len(figure_titled(cost_pane, "Concentration of re-reading").data[0].y)
    total = int(q("""SELECT COUNT(*) AS n FROM (
                       SELECT session_id, target FROM tool_calls
                        WHERE tool_name IN ('Read', 'NotebookRead') AND target IS NOT NULL
                        GROUP BY session_id, target HAVING COUNT(*) >= 3)""").iloc[0]["n"])
    if total <= 200:
        pytest.skip("this store has fewer than 200 re-read groups, so the cap cannot be exercised")
    assert points == total, f"the curve plots {points} groups against a population of {total}"


def test_the_headline_cards_count_every_group_not_the_first_page(cost_pane, q):
    """They did not, and were understating this store's re-reads by 39%.

    Computed from the frame that carries the table's LIMIT 200, so each card was a sum over the
    head of its own display list, captioned as a total, beside a table that gave no sign it was
    truncated.
    """
    truth = q("""SELECT COUNT(*) AS groups, COALESCE(SUM(reads - 1), 0) AS repeats FROM (
                   SELECT COUNT(*) AS reads FROM tool_calls
                    WHERE tool_name IN ('Read', 'NotebookRead') AND target IS NOT NULL
                    GROUP BY session_id, target HAVING reads >= 3)""").iloc[0]
    # Read off the CARD, not off the page. The same population count also appears in the curve's
    # caption below, so a substring search over the whole tab passes while the card beside it
    # still says 200. That is exactly the defect, and the first version of this test did not
    # catch it when the defect was replanted.
    assert card_value(cost_pane, "Re-read groups") == f"{int(truth['groups']):,}"
    assert card_value(cost_pane, "Re-reads beyond the first") == f"{int(truth['repeats']):,}"


def test_the_table_says_it_is_capped(cost_pane):
    """A truncated table beside a total is only honest if it admits the truncation."""
    assert "WORST 200 GROUPS" in extract.all_words(cost_pane)


def card_value(node, label):
    """The number a stat card shows, found by its label.

    A card renders as label, value, sub-line, in that order, so the value is the text after the
    label. Positional rather than structural, and it fails loudly if the card ever stops being
    built that way, which is better than a substring search over the page: the figures on this tab
    appear in the prose too, so a page-wide search cannot tell a correct card from a wrong one.
    """
    texts = extract.texts(node)
    assert label in texts, f"no card labelled {label!r}; cards present: {texts[:20]}"
    return texts[texts.index(label) + 1]


# --- the sessions scatter --------------------------------------------------
def test_the_scatter_plots_every_session_the_table_lists(pane, has_store):
    body = pane("tab-sessions")
    table = extract.table_by_id(body, "tbl-session")
    described = extract.figures(body)
    assert described, "the All sessions tab drew no scatter"
    plotted = sum(t["points"] for t in described[0]["traces"])
    assert plotted == len(table["rows"]), (
        f"{plotted} points plotted against {len(table['rows'])} rows in the table")


def test_the_scatter_never_plots_a_zero_on_a_log_axis(pane, has_store):
    """A zero on a log axis is dropped silently by Plotly, so a session with no recorded peak
    would vanish from a chart captioned with the full population count."""
    for trace in extract.figures(pane("tab-sessions"))[0]["traces"]:
        assert trace["x_min"] is not None and trace["x_min"] > 0
        assert trace["y_min"] is not None and trace["y_min"] > 0


def test_a_cohort_narrows_the_scatter_with_the_table(pane, cohort, has_store):
    body = pane("tab-sessions", coh=cohort)
    table = extract.table_by_id(body, "tbl-session")
    plotted = sum(t["points"] for t in extract.figures(body)[0]["traces"])
    assert plotted == len(table["rows"])


# --- the message composition bar -------------------------------------------
def test_the_message_bar_refuses_a_single_segment(has_store):
    """A stacked bar with one segment is a rectangle. Drawing it would present "this store has one
    usable probe" as a finding about how conversations are composed."""
    from c4x.probe_detail import stacked_message_figure
    one = pd.DataFrame([{"probe_id": 1, "name": "attachmentTokens", "tokens": 5212,
                         "ts": "2026-01-01"}])
    assert stacked_message_figure(one) is None
    assert stacked_message_figure(pd.DataFrame(columns=["probe_id", "name", "tokens", "ts"])) is None


def test_the_message_bar_stacks_by_probe_when_there_is_a_shape_to_show():
    """Exercised on rows this store does not hold.

    Every probe here reports one non-zero category, so the query wrapper returns None and would in
    CI too. The drawing branch would otherwise ship untested and stay untested until the first
    probe that made it run, which is the worst moment to find out it does not work.
    """
    from c4x.probe_detail import stacked_message_figure
    rows = pd.DataFrame([
        {"probe_id": 1, "name": "toolResultTokens", "tokens": 1500, "ts": "2026-01-01"},
        {"probe_id": 1, "name": "userMessageTokens", "tokens": 500, "ts": "2026-01-01"},
        {"probe_id": 2, "name": "toolResultTokens", "tokens": 200, "ts": "2026-02-01"},
        {"probe_id": 2, "name": "userMessageTokens", "tokens": 700, "ts": "2026-02-01"},
    ])
    figure = stacked_message_figure(rows).figure
    assert figure.layout.barmode == "stack"
    assert len(figure.data) == 2, "one trace per category"
    for trace in figure.data:
        assert list(trace.x) == ["probe 1", "probe 2"], "probes are not ordered oldest first"
    total = sum(sum(trace.y) for trace in figure.data)
    assert total == int(rows["tokens"].sum()), "the stack does not carry every token"


def test_a_category_missing_from_one_probe_is_a_zero_not_a_gap():
    """Plotly aligns bars by x. A trace whose x list skips a probe would stack its value onto the
    wrong bar, which is a wrong chart rather than a missing one."""
    from c4x.probe_detail import stacked_message_figure
    rows = pd.DataFrame([
        {"probe_id": 1, "name": "toolResultTokens", "tokens": 1500, "ts": "2026-01-01"},
        {"probe_id": 1, "name": "userMessageTokens", "tokens": 500, "ts": "2026-01-01"},
        {"probe_id": 2, "name": "userMessageTokens", "tokens": 700, "ts": "2026-02-01"},
    ])
    figure = stacked_message_figure(rows).figure
    tool = next(t for t in figure.data if t.name == "toolResultTokens")
    assert list(tool.x) == ["probe 1", "probe 2"]
    assert list(tool.y) == [1500, 0]

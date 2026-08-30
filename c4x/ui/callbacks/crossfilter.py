"""Charts that filter the table beside them.

Every chart in this app was decoration in the strict sense: it showed the same numbers a table
already held, and nothing a reader did to it changed anything. That is the state a dashboard ends
up in when charts are added one at a time, and it is why the honest answer to "which of these 316
sessions are the outliers" was still "sort the table and scroll".

Two cross-filters, both on the same principle: the chart is the coarse instrument and the table is
the fine one, so a gesture on the chart narrows the table rather than replacing it. Both state what
they filtered and how to undo it, because a table showing 12 of 1,129 rows with nothing on screen
saying so is worse than no filter at all.

Registered by importing this module, like every other callback file.
"""
from dash import Input, Output, State, callback, html

from c4x.theme import ACCENT, MUTED, SECTION_NOTE


def _hint(text, filtered=False):
    """The line under a cross-filtered table. Coloured only when a filter is actually on."""
    return html.Div(text, style={**SECTION_NOTE,
                                 "color": ACCENT if filtered else MUTED,
                                 "marginTop": "6px"})


def selected_ids(selection, index=3):
    """The session ids inside a Plotly box or lasso selection.

    Plotly reports a selection as points carrying `curveNumber` and `pointIndex`, which identify a
    point WITHIN its trace. The scatter draws one trace per section, so an index alone names a
    different session depending on which trace it came from. The id travels in customdata instead,
    which is why the scatter puts it there.

    Returns None for "no selection", which is different from an empty selection: a box drawn over
    empty space selects nothing and must narrow the table to nothing, while no box at all must
    leave it whole.
    """
    if not selection:
        return None
    points = selection.get("points")
    if points is None:
        return None
    out = []
    for point in points:
        data = point.get("customdata")
        if isinstance(data, (list, tuple)) and len(data) > index:
            out.append(data[index])
    return out


@callback(
    Output("tbl-session", "data"),
    Output("sessions-filter-note", "children"),
    Input("fig-sessions", "selectedData"),
    State("sessions-rows", "data"),
)
def _sessions_crossfilter(selection, rows):
    """Drag a box on the scatter; the table below narrows to what is inside it.

    The scatter answers "are there outliers" and cannot answer "which ones"; the table answers
    "which ones" and cannot show that they are outliers. Selecting on one and reading the other is
    the whole point of having both.

    Not `prevent_initial_call`: the table is rendered empty of nothing in particular and this
    callback is what fills it on first paint. Every re-render of the tab creates a fresh graph with
    no selection, so the table comes back whole rather than keeping a filter the reader can no
    longer see the box for.
    """
    rows = rows or []
    ids = selected_ids(selection)
    if ids is None:
        return rows, _hint(f"All {len(rows):,} rows. Drag a box or lasso on the chart above to "
                           f"narrow this table to a region of it.")
    keep = set(ids)
    narrowed = [r for r in rows if r.get("session_id") in keep]
    if not narrowed:
        return [], _hint("That selection contains no sessions. Double-click the chart to clear it.",
                         filtered=True)
    return narrowed, _hint(
        f"Showing {len(narrowed):,} of {len(rows):,} sessions, selected on the chart above. "
        f"Double-click the chart to clear the selection.", filtered=True)


@callback(
    Output("tbl-reread", "data"),
    Output("reread-filter-note", "children"),
    Input("fig-reread", "clickData"),
    State("reread-rows", "data"),
)
def _reread_crossfilter(click, rows):
    """Click a point on the concentration curve; the table narrows to that many groups.

    The curve's x axis is a rank, so a click on it is already a "top N" choice: clicking where the
    curve reaches 50% shows exactly the groups that account for half the re-reading. Answering
    "which files are the half worth fixing" any other way means reading a percentage off the chart
    and then counting rows in the table.

    Clicks on the dotted reference diagonal are ignored. It is drawn to show what no concentration
    would look like and its x values are the two ends of the axis, so a click on it would filter to
    1 group or to all of them, neither of which means anything.
    """
    held = (rows or {}).get("rows") or []
    population = int((rows or {}).get("population") or len(held))
    point = (click or {}).get("points") or []
    if not point or point[0].get("curveNumber"):
        return held, _hint(f"All {len(held):,} rows. Click a point on the curve above to keep only "
                           f"the groups up to that rank.")
    try:
        rank = int(point[0].get("x"))
    except (TypeError, ValueError):
        return held, _hint(f"All {len(held):,} rows.")
    share = point[0].get("y")
    said = f", together {float(share):.1f}% of every re-read here" if share is not None else ""
    # The curve is drawn over the whole population and the table carries the worst 200 of it, so a
    # click past that rank asks for rows the table does not hold. Saying so is the difference
    # between a filter that did less than asked and a table that looks like the whole answer.
    if rank > len(held):
        return held, _hint(
            f"The worst {rank:,} groups{said}. The table holds the worst {len(held):,} of "
            f"{population:,}, so all of them are shown; the curve counts every group.",
            filtered=True)
    rank = max(1, rank)
    return held[:rank], _hint(
        f"Showing the worst {rank:,} groups of {population:,}{said}. Click the far right of the "
        f"curve to restore the table.", filtered=True)

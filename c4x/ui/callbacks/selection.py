"""What the header selects, and the live readout that follows it.

The session picker's options, the cohort list, picking a row from the browse table, and the tick
that refreshes the store and redraws the context bar.

Registered by importing this module. Dash's `callback` decorator writes into a global registry, so
these attach wherever they are defined; app.py imports them and re-exports the names, because the
tests and the table audit reach them as app._name and should not have to know where each one lives.
"""
from dash import Input, Output, State, callback, html, no_update
from dash.exceptions import PreventUpdate

from c4x.store import (
    cohort_options,
)
from c4x.theme import (
    DANGER,
    MONO,
)
from c4x.ui import header
from c4x.ui.header import quick_view, selector_options


@callback(
    Output("sel-session", "options"),
    Input("sel-cohort", "value"),
    Input("tick", "n_intervals"),
)
def _selector_options(cohort, _n):
    """Rebuild the selector on every tick, so its dates and peaks stay live.

    Built here rather than at import so the first paint is not blocked by the query. It DOES rebuild
    on each 5s tick: an earlier version of this docstring claimed a guard against that which the
    body never had. The rebuild is what keeps the figures current, and session_rows() is cached for
    45 seconds, so the cost is a list comprehension rather than a query.
    """
    return selector_options(cohort)

@callback(
    Output("sel-cohort", "options"),
    Input("tick", "n_intervals"),
    State("sel-cohort", "options"),
)
def _cohort_options(_n, existing):
    if existing:
        raise PreventUpdate
    return cohort_options()

@callback(
    Output("sel-session", "value", allow_duplicate=True),
    Output("active-tab", "data", allow_duplicate=True),
    Input("tbl-findings", "active_cell"),
    State("tbl-findings", "derived_viewport_data"),
    prevent_initial_call=True,
)
def _finding_clicked(active_cell, rows):
    """Send the reader to the evidence for the finding they clicked.

    allow_duplicate on both outputs: `_pick_from_table` also sets the session and `_switch_tab`
    also sets the tab. Two routes into the same state is the point, and it only works because the
    nav styles itself from the Store rather than from whichever button was pressed last.

    A finding with no destination raises PreventUpdate rather than selecting nothing, so clicking
    the fixed-overhead row does not silently clear a selection the reader had already made.
    """
    if not active_cell or not rows:
        raise PreventUpdate
    index = active_cell.get("row")
    if index is None or not (0 <= index < len(rows)):
        raise PreventUpdate
    row = rows[index]
    target, session_id = row.get("goes to"), row.get("session_id")
    if not target:
        raise PreventUpdate
    from c4x.ui.layout import TAB_IDS
    if target not in TAB_IDS:
        raise PreventUpdate
    return (session_id or no_update), TAB_IDS.index(target)


@callback(
    Output("sel-session", "value"),
    Input("tbl-session", "selected_rows"),
    State("tbl-session", "data"),
    prevent_initial_call=True,
)
def _pick_from_table(selected_rows, table_data):
    """The browse table sets the header selection, so there is only ever one selection."""
    if not selected_rows or not table_data:
        raise PreventUpdate
    i = selected_rows[0]
    if not (0 <= i < len(table_data)):
        raise PreventUpdate
    return table_data[i].get("session_id")

@callback(
    Output("live-context", "children"),
    Input("tick", "n_intervals"),
    Input("sel-session", "value"),
    Input("session-scope", "value"),
)
def _tick(_n, session_id=None, scope="main"):
    """Harvest, then re-render the live reading.

    Ordered deliberately: refresh first, read second, so the number rendered is the one just
    collected rather than the one from the previous tick.
    """
    # Module-qualified ON PURPOSE. The table audit replaces header.refresh_store with
    # a no-op before exercising the callbacks, because the real one harvests and an
    # audit must not write to the store it is auditing. A bare call would bind this
    # module's own global and the stub would silently stop applying.
    header.refresh_store()
    try:
        return quick_view(session_id, scope or "main")
    except Exception as exc:                        # noqa: BLE001 - never blank the header
        return html.Div(f"context unavailable: {str(exc)[:80]}",
                        style={"color": DANGER, "fontSize": "11px", "fontFamily": MONO})

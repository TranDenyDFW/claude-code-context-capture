"""The Window tab's sub-panel navigation.

Two callbacks, the same shape as `_switch_tab` and `_render_tab` in navigation.py: one turns a
click into an index and restyles the strip, the other renders the panel for whatever index is
stored. Copying that shape rather than inventing a second one means the panel that is showing is
always a function of the Store, never of which button was pressed last, and the header selection
re-renders the panel exactly as it re-renders a tab.
"""
from dash import Input, Output, State, callback, ctx

from c4x.tabs.window import PANELS, PREFIX, panel_body
from c4x.ui.subpanels import active_index, body_id, button_id, panel_style, store_id

_BUTTONS = [button_id(PREFIX, key) for key, _label, _description in PANELS]


@callback(
    [Output(button, "style") for button in _BUTTONS] + [Output(store_id(PREFIX), "data")],
    [Input(button, "n_clicks") for button in _BUTTONS],
    State(store_id(PREFIX), "data"),
    prevent_initial_call=True,
)
def _window_panel_chosen(*args):
    """Restyle the strip and record the choice. The rendering is the other callback's job."""
    index = active_index(ctx.triggered_id, PREFIX, PANELS, args[-1])
    return [panel_style(i == index) for i in range(len(PANELS))] + [index]


@callback(
    Output(body_id(PREFIX), "children"),
    Input(store_id(PREFIX), "data"),
    Input("sel-session", "value"),
    Input("session-scope", "value"),
    Input("sel-cohort", "value"),
)
def _window_panel(index, session_id, scope, cohort):
    """Render the chosen panel for the current header selection.

    Panels are built ON DEMAND, like the tabs above them: Items runs the probe queries and
    Composition builds a history chart over every charted call, and paying for all three on every
    render would reintroduce exactly the cost that made rendering every tab at import untenable.

    A failed panel says so rather than raising, which would leave the strip visible above an empty
    container and read as "this panel has nothing in it".
    """
    try:
        return panel_body(index, session_id, scope or "main", cohort)
    except Exception as exc:                        # noqa: BLE001 - a failed panel must say so
        from dash import html

        from c4x.theme import CODE_BLOCK, DANGER, SECTION_HEAD
        label = PANELS[max(0, min(int(index or 0), len(PANELS) - 1))][1]
        return html.Div([
            html.Div(f"{label} could not be rendered", style={**SECTION_HEAD, "color": DANGER}),
            html.Pre(f"{type(exc).__name__}: {exc}",
                     style={**CODE_BLOCK, "color": DANGER, "whiteSpace": "pre-wrap"}),
        ])

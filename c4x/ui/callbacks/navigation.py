"""Which tab is showing, and rendering the pane for it.

Registered by importing this module. Dash's `callback` decorator writes into a global registry, so
these attach wherever they are defined; app.py imports them and re-exports the names, because the
tests and the table audit reach them as app._name and should not have to know where each one lives.
"""
from dash import Input, Output, State, callback, html

from c4x.store import (
    population_label,
)
from c4x.theme import (
    CODE_BLOCK,
    DANGER,
    SECTION_HEAD,
    SECTION_NOTE,
)
from c4x.ui.layout import SELECTION_SCOPED, TAB_IDS, TABS, tab_style


@callback(
    [Output(f"btn-{t}", "style") for t in TAB_IDS] + [Output("active-tab", "data")],
    [Input(f"btn-{t}", "n_clicks") for t in TAB_IDS],
    State("active-tab", "data"),
    prevent_initial_call=True,
)
def _switch_tab(*args):
    from dash import ctx
    current = args[-1]
    which = ctx.triggered_id
    idx = TAB_IDS.index(which.replace("btn-", "")) if which else current
    return [tab_style(i == idx) for i in range(len(TABS))] + [idx]

@callback(
    Output("tab-content", "children"),
    Input("active-tab", "data"),
    Input("sel-session", "value"),
    Input("session-scope", "value"),
    Input("sel-cohort", "value"),
)
def _render_tab(idx, session_id, scope, cohort):
    """Render ONE pane, for the current selection.

    Re-runs when the tab changes or the selection changes, which is what makes every tab describe
    the same thing at the same time. A pane built once at import could not do that.
    """
    i = int(idx or 0)
    if not (0 <= i < len(TABS)):
        i = 0
    tab_id, label, fn = TABS[i]
    try:
        body = fn(session_id, scope or "main", cohort)
    except Exception as exc:                        # noqa: BLE001 - a failed tab must say so
        return html.Div([
            html.Div(f"{label} could not be rendered", style={**SECTION_HEAD, "color": DANGER}),
            html.Pre(f"{type(exc).__name__}: {exc}", style={**CODE_BLOCK, "color": DANGER,
                                                            "whiteSpace": "pre-wrap"}),
        ])
    # Say which population the page is describing, every time, on every tab.
    # Summary states its own scope in its first line; Compare labels each arm itself and takes the
    # header selection as arm A, so the generic "not affected by the selection" banner would be a
    # false statement on it.
    #
    # Waste is exempt for a sharper reason: it OVERRIDES the scope, calling scoped(..., "all", ...)
    # whatever the radio says, because subagents make nearly every tool call. The banner is built
    # from the header's scope and cannot know that, so on this tab it printed "main thread only"
    # over numbers that count subagents. That is not a mismatch of wording, it inverts the figure:
    # the worst re-read offender here is 614 reads, all of them subagent, and the main-thread
    # reading the banner promised is zero. The tab states its own population instead.
    # Named ids, and a test asserts every one of them exists in TABS. This tuple held "tab-waste"
    # for one commit after that tab was renamed to "tab-cost", which switched the exemption off
    # silently and put the contradiction it exists to prevent back onto the page.
    if tab_id in ("tab-summary", "tab-compare", "tab-cost"):
        banner = None
    elif tab_id in SELECTION_SCOPED:
        banner = html.Div(f"Describing {population_label(session_id, cohort, scope or 'main')}.",
                          style=SECTION_NOTE)
    else:
        banner = html.Div("Store-wide. Not affected by the header selection.", style=SECTION_NOTE)
    return html.Div([banner, body] if banner is not None else body)

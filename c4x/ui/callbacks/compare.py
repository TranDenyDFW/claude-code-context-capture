"""The Compare tab's two arms: what B can be, and the table between them.

Registered by importing this module. Dash's `callback` decorator writes into a global registry, so
these attach wherever they are defined; app.py imports them and re-exports the names, because the
tests and the table audit reach them as app._name and should not have to know where each one lives.
"""
from dash import Input, Output, callback, html

from c4x.panels import (
    compare_table,
    selection_metrics,
)
from c4x.store import (
    cohort_options,
    population_label,
    session_name,
)
from c4x.theme import (
    SECTION_NOTE,
    population_note,
)
from c4x.ui.header import selector_options


@callback(
    Output("cmp-target", "options"),
    Input("cmp-kind", "value"),
    Input("sel-cohort", "value"),
)
def _cmp_targets(kind, cohort):
    """Arm B's choices. Sessions are NOT narrowed to arm A's cohort: comparing a project against
    a different project is the point, and narrowing would make that impossible."""
    return cohort_options() if kind == "cohort" else selector_options(None)

@callback(
    Output("cmp-out", "children"),
    Input("cmp-kind", "value"),
    Input("cmp-target", "value"),
    Input("sel-session", "value"),
    Input("sel-cohort", "value"),
    Input("session-scope", "value"),
)
def _cmp_render(kind, target, session_id, cohort, scope):
    if not target:
        return html.Div("Pick something to compare against.", style=SECTION_NOTE)
    scope = scope or "main"
    # NAMED, not just counted, and only on this tab.
    #
    # Both arms said "1 session, main thread only", which is true of either one and identifies
    # neither. On screen the pickers above carry the names; in a CSV or a PDF of this table they do
    # not travel at all, so an exported comparison could not say what it had compared. Every other
    # tab describes ONE population and keeps the plain sentence.
    def arm(sid, coh):
        said = population_label(sid, coh, scope)
        name = session_name(sid)
        return f"{name}  ({said})" if name else said

    a = selection_metrics(session_id, cohort, scope)
    a_label = arm(session_id, cohort)
    if kind == "cohort":
        b = selection_metrics(None, target, scope)
        b_label = arm(None, target)
    else:
        b = selection_metrics(target, None, scope)
        b_label = arm(target, None)
    if not a["calls"] and not b["calls"]:
        return html.Div("Neither arm has any API calls under this scope.", style=SECTION_NOTE)
    # THE ONE TAB THAT DESCRIBES TWO POPULATIONS, so it states both in one sentence rather
    # than leaving the A and B blocks below to serve as the only record of what was compared. The
    # blocks stay: they are the colour key for the table's two columns. This is what travels with
    # an export, and it is what makes the population field non-null here like everywhere else.
    return html.Div([
        population_note(f"Comparing A, {a_label}, against B, {b_label}."),
        compare_table(a_label, a, b_label, b),
    ])

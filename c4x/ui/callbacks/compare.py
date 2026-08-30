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
)
from c4x.theme import (
    SECTION_NOTE,
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
    a = selection_metrics(session_id, cohort, scope)
    a_label = population_label(session_id, cohort, scope)
    if kind == "cohort":
        b = selection_metrics(None, target, scope)
        b_label = population_label(None, target, scope)
    else:
        b = selection_metrics(target, None, scope)
        b_label = population_label(target, None, scope)
    if not a["calls"] and not b["calls"]:
        return html.Div("Neither arm has any API calls under this scope.", style=SECTION_NOTE)
    return compare_table(a_label, a, b_label, b)

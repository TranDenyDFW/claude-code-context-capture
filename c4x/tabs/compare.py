"""The compare tab.

Two selections measured by the same function, so a difference cannot be an artefact.
"""
from dash import dcc, html

from c4x.theme import FIELD, MUTED, SECTION_NOTE


def compare_layout(session_id=None, scope="main", cohort=None):
    """Two selections, measured identically, with the differences named.

    Comparison is what makes this a research tool rather than an inspector: a number on its own
    invites a story, and the only question that constrains a story is "compared to what".

    Arm A is the header selection. Arm B is chosen here. Both are measured by the same function
    with the same scope, so a difference cannot be an artefact of asking two different questions.
    """
    return html.Div([
        html.Div("Arm A is the header selection. Pick arm B here. Both arms are measured by the "
                 "same function with the same population rules, so a difference between them "
                 "cannot come from having asked two different questions.", style=SECTION_NOTE),
        html.Div([
            html.Span("Compare against", style={"color": MUTED, "fontSize": "12px",
                                                "marginRight": "8px"}),
            dcc.Dropdown(id="cmp-kind", value="cohort", clearable=False,
                         options=[{"label": " a population", "value": "cohort"},
                                  {"label": " a single session", "value": "session"}],
                         style={"width": "200px", **FIELD}, className="c4x-dd"),
            dcc.Dropdown(id="cmp-target", options=[], value=None, optionHeight=44,
                         placeholder="Choose what to compare against",
                         style={"width": "460px", **FIELD}, className="c4x-dd"),
        ], style={"display": "flex", "alignItems": "center", "gap": "6px",
                  "marginBottom": "12px"}),
        html.Div(id="cmp-out"),
    ])

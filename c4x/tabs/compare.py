"""The compare tab.

Two selections measured by the same function, so a difference cannot be an artefact.
"""
from dash import dcc, html

from c4x.store import cohort_sessions, q
from c4x.theme import FIELD, MUTED, SECTION_NOTE
from c4x.ui.header import selector_options


def default_arm_b(session_id=None, cohort=None):
    """The most recently active session that is not already arm A.

    Excluding arm A matters: comparing a session with itself renders a table of 1.0 ratios and no
    verdicts, which is a correct answer to a pointless question and reads as a broken tab.
    """
    ids = cohort_sessions(cohort)
    where, args = "", []
    if ids:
        where += f" AND session_id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    if session_id:
        where += " AND session_id <> ?"
        args.append(session_id)
    df = q(f"""SELECT session_id FROM turns WHERE 1=1 {where}
                GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT 1""", tuple(args))
    return None if df.empty else df.iloc[0]["session_id"]


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
            # Defaults to comparing two SESSIONS rather than to a cohort, because the default
            # arm B below is a session, and a kind that disagreed with its target would render the
            # prompt again on first paint, which is the empty state this change exists to remove.
            dcc.Dropdown(id="cmp-kind", value="session", clearable=False,
                         options=[{"label": " a population", "value": "cohort"},
                                  {"label": " a single session", "value": "session"}],
                         style={"width": "200px", **FIELD}, className="c4x-dd"),
            # Arm B defaults to the most recent session that is not arm A, so the tab shows a
            # real comparison on arrival instead of 112 pixels of prompt. It is a DEFAULT, not a
            # claim: the dropdown names the session it picked, and changing it is one click.
            # Options are built HERE as well as by `_cmp_targets`, which looks redundant and is
            # not. A dcc.Dropdown carrying a value that appears in no option clears that value and
            # fires the change, so with `options=[]` the default below survived exactly as long as
            # it took the browser to mount the component: the CLI rendered a comparison and the
            # page rendered the prompt. The callback still owns them from the first kind change on.
            dcc.Dropdown(id="cmp-target", options=selector_options(None),
                         value=default_arm_b(session_id, cohort), optionHeight=44,
                         placeholder="Choose what to compare against",
                         style={"width": "460px", **FIELD}, className="c4x-dd"),
        ], style={"display": "flex", "alignItems": "center", "gap": "6px",
                  "marginBottom": "12px"}),
        html.Div("Arm B starts on the most recently active session that is not arm A. It is a "
                 "starting point, not a recommendation: change it above.",
                 style=SECTION_NOTE),
        html.Div(id="cmp-out"),
    ])

"""The mirror tab.

The window math, mirrored from mirror-core.mjs, and a calculator over it.
"""
from dash import dcc, html

from c4x.dash_compat import DataTable
from c4x.store import MATH
from c4x.theme import (
    FIELD,
    MUTED,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    about_note,
    fmt_tokens,
    header_help,
    numeric_columns,
)


# ---- Mirror ---------------------------------------------------------------
def mirror_layout(session_id=None, scope="main", cohort=None):
    rows = []
    for t in MATH["thresholds"]:
        rows.append({
            "window": int(t["window"]), "warn": int(t["warn"]),
            "compact": int(t["compact"]), "blocked": int(t["blocked"]),
        })
    return html.Div([
        html.Div([
            html.Span("Resident tokens", style={"color": MUTED, "fontSize": "12px",
                                                "marginRight": "8px"}),
            dcc.Input(id="in-tokens", type="number", value=850000, min=0, step=1000,
                      style={"width": "150px", "padding": "6px", **FIELD}),
            html.Span("Window", style={"color": MUTED, "fontSize": "12px",
                                       "margin": "0 8px 0 18px"}),
            dcc.Dropdown(
                id="dd-window",
                options=[{"label": fmt_tokens(t["window"]), "value": t["window"]}
                         for t in MATH["thresholds"]],
                value=1000000, clearable=False,
                style={"width": "150px", **FIELD}, className="c4x-dd",
            ),
        # DASH-ONLY. The calculator's inputs and its answer exist only in the Dash tree; the React
        # page has no calculator, and without the mark its labels and constants arrived there as
        # loose prose ("Resident tokens", "Window", the constants sentence) with nothing to act on.
        # /render lists the text under this class and the page leaves it out.
        ], className="dash-only",
           style={"display": "flex", "alignItems": "center", "marginBottom": "16px"}),
        html.Div(id="mirror-out", className="dash-only"),
        html.Div([
            html.Div("Window Thresholds", style=SECTION_HEAD),
            html.Div("The warn, compact and blocked lines the window math draws for each window "
                     "size, from the same constants the chart above uses.", style=SECTION_NOTE),
            DataTable(
                columns=(_cols := numeric_columns(["window", "warn", "compact", "blocked"],
                                        {"window", "warn", "compact", "blocked"})),
                tooltip_header=header_help(_cols),
                data=rows, **TABLE_STYLE,
            ),
        ]),
        about_note(
            f"Constants read from tools/mirror-core.mjs at startup: "
            f"autocompact buffer {MATH['K']['AUTOCOMPACT_BUFFER']}, "
            f"compact buffer {MATH['K']['COMPACT_BUFFER']}, "
            f"warn offset {MATH['K']['WARN_OFFSET']}, "
            f"max-output reserve {MATH['K']['MAX_OUTPUT_RESERVE']}. "
            f"Predictions are computed by tools/mirror.mjs, not reimplemented here.",
            # CONTENT, not a control label: these four constants are the only place either page
            # states them, and marking the sentence dash-only removed them from the React page
            # entirely. Only the calculator's own inputs and its answer are Dash-only. It is a
            # statement about the build rather than about any table, so it goes on the chip.
            style={"marginTop": "18px"},
        ),
    ])

"""The page around the tabs: the registry, the nav, the header bar and the skeleton.

One registry, `TABS`, carrying id, label and the function that renders each pane. It used to be two
index-aligned lists plus the count written out in four more places, so adding a tab meant editing
six things in step with nothing checking they agreed. That is the same defect class as every SYNC
finding in this store's own audit.
"""
from dash import dcc, html

from c4x.panels import stored_text_note
from c4x.store import COHORT_ALL, DB_PATH
from c4x.tabs import (
    compactions_layout,
    compare_layout,
    mirror_layout,
    probes_layout,
    session_layout,
    sessions_table_layout,
    summary_layout,
    waste_layout,
    window_layout,
)
from c4x.theme import ACCENT, BG, BORDER, FIELD, MONO, MUTED, PANEL, TEXT, WARN
from c4x.ui.header import quick_view, scope_radio


def tab_button(tab_id: str, label: str, active: bool) -> html.Button:
    return html.Button(
        label, id=f"btn-{tab_id}", n_clicks=0,
        style=tab_style(active),
    )


def tab_style(active: bool) -> dict:
    # Active state needs a border cue, not just a background swap.
    return {
        "background": PANEL if active else "transparent",
        "color": TEXT if active else MUTED,
        "border": "none",
        "borderBottom": f"3px solid {ACCENT}" if active else "3px solid transparent",
        "padding": "9px 18px", "fontSize": "13px", "cursor": "pointer",
        "fontFamily": MONO, "fontWeight": 700 if active else 400,
    }


header = html.Div(
    [
        html.Div(
            [
                html.Span("context capture", style={"fontWeight": 800, "fontSize": "15px"}),
                html.Span(f"  {DB_PATH}", style={"color": MUTED, "fontSize": "11px",
                                                 "marginLeft": "10px"}),
                # What is in that file, said on every tab rather than only in the README. Someone
                # can otherwise use this for an hour without learning that the store holds
                # conversation text rather than measurements of it.
                html.Div(
                    [
                        html.Span("PRIVACY  ", style={"color": WARN, "fontWeight": 700,
                                                      "letterSpacing": "0.06em"}),
                        html.Span(stored_text_note()),
                        html.Span("  Nothing leaves this machine, and uninstalling is the only "
                                  "way to stop capture: see the README.",
                                  style={"color": MUTED}),
                    ],
                    style={"color": TEXT, "fontSize": "10.5px", "fontFamily": MONO,
                           "marginTop": "3px", "maxWidth": "760px"},
                ),
            ],
        ),
        html.Div(
            [
                # The selector lives here, above the tabs, because it governs every one of them.
                # The header used to show "the latest call anywhere in the store" with no label,
                # while the tabs below showed a mix of store-wide totals and per-session numbers.
                # Nothing said which was which. One selection, stated, drives the whole page.
                html.Div([
                    dcc.Dropdown(
                        id="sel-cohort", options=[], value=COHORT_ALL, clearable=False,
                        placeholder="Population",
                        style={"width": "260px", **FIELD}, className="c4x-dd",
                    ),
                    dcc.Dropdown(
                        id="sel-session", options=[], value=None, optionHeight=44,
                        placeholder="All sessions in the population, or pick one",
                        style={"width": "420px", **FIELD}, className="c4x-dd",
                    ),
                    scope_radio("session-scope"),
                ], style={"display": "flex", "alignItems": "center", "gap": "6px",
                          "marginRight": "16px"}),
                html.Div(id="live-context", children=quick_view(None)),
                # There was a Quit button here. It is gone on purpose: a capture tool with a
                # one-click off switch produces a store that looks complete while silently missing
                # whatever happened after someone pressed it. Stop the server with Ctrl+C; stop
                # capture by uninstalling.
            ],
            style={"display": "flex", "alignItems": "center"},
        ),
    ],
    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
           "padding": "12px 20px", "borderBottom": f"1px solid {BORDER}", "background": PANEL},
)
























































# ONE registry: id, label, and the function that renders the pane.
#
# This used to be TAB_IDS and TAB_LABELS, two lists that had to stay index-aligned, plus the count
# 6 written out in four more places (the pane Divs, two range(6) calls in the callback, and the
# style list). Adding a tab meant editing six things in step, and nothing checked that they agreed.
# That is the same defect class as every SYNC finding in this store's own audit, so it went first.
TABS = [
    ("tab-summary", "Summary", summary_layout),
    ("tab-sessions", "All sessions", sessions_table_layout),
    ("tab-session", "Session", session_layout),
    ("tab-compactions", "Compactions", compactions_layout),
    ("tab-window", "Window", window_layout),
    ("tab-probes", "Probes", probes_layout),
    ("tab-waste", "Waste", waste_layout),
    ("tab-compare", "Compare", compare_layout),
    ("tab-mirror", "Mirror", mirror_layout),
]
TAB_IDS = [t[0] for t in TABS]

# Summary is store-wide. Everything after it describes the header selection, and each tab says so
# on the page rather than leaving the reader to work it out.
SELECTION_SCOPED = {"tab-session", "tab-compactions", "tab-window", "tab-waste"}
# Probes describes 3 control-protocol runs that belong to no session, and Mirror is a
# calculator over published constants. Labelling either as scoped would be the same false
# statement this restructure removed.

# Panes are rendered ON DEMAND, not up front.
#
# Building all of them at import took 56.7 seconds against this store, and every one of them was
# rebuilt whether or not it was ever looked at. Rendering only the active tab also makes the
# selection work at all: a pane built once at import cannot describe a session chosen later.
# Components created inside a callback are safe here because the ids are static and the app is
# constructed with suppress_callback_exceptions.
def build_layout():
    return html.Div(
        [
            header,
            html.Div([tab_button(tid, lbl, i == 0) for i, (tid, lbl, _) in enumerate(TABS)],
                     style={"display": "flex", "gap": "2px", "padding": "0 14px",
                            "borderBottom": f"1px solid {BORDER}", "background": BG}),
            dcc.Store(id="active-tab", data=0),
            # Drives the header readout. 5s is well under how fast a context window moves, and the
            # harvest behind it is rate-limited and lock-guarded, so a slow tick cannot pile up.
            dcc.Interval(id="tick", interval=5000, n_intervals=0),
            dcc.Loading(html.Div(id="tab-content"), type="dot", color=ACCENT),
        ],
        style={"background": BG, "color": TEXT, "minHeight": "100vh",
               "fontFamily": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"},
    )

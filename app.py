#!/usr/bin/env python
"""
Context capture explorer - a local Dash app over data/context.db.

Shows what the harvester collected: per-session context growth, every compaction on record
with the mirror's predicted trigger, and a live threshold calculator.

The window math is NOT reimplemented here. It is read from tools/mirror-core.mjs at startup
and computed by tools/mirror.mjs on demand, so the app and the validated JS cannot drift apart.

Run:  python app.py          then open http://127.0.0.1:8056
Stop: Ctrl+C. There is no in-app quit: closing this viewer must not look like stopping capture,
      and it does not - the hooks harvest on their own whether or not this is running.
"""

import html as _html
import os
import subprocess
import sys
import threading as _threading
import time as _time
from pathlib import Path

from dash import Dash, Input, Output, State, callback, dash_table, dcc, html
from dash.exceptions import PreventUpdate
from flask import request as _flask_request

from c4x.panels import (
    compare_table,
    selection_metrics,
    stored_text_note,
    text_panel,
    turn_diff_panel,
)
from c4x.store import (
    COHORT_ALL,
    THRESHOLDS,
    cohort_options,
    cohort_sessions,
    compaction_dropped,
    compaction_dropped_count,
    compaction_summary_text,
    message_text,
    population_label,
    predict,
    q,
    scoped,
    session_rows,
    session_turns,
    session_window,
)
from c4x.tabs import (
    breakdown_layout,
    compactions_layout,
    compare_layout,
    mirror_layout,
    probes_layout,
    session_layout,
    session_view,
    sessions_table_layout,
    sources_layout,
    summary_layout,
    waste_layout,
)
from c4x.theme import (
    ACCENT,
    BG,
    BORDER,
    CODE_BLOCK,
    DANGER,
    FIELD,
    GOOD,
    MONO,
    MUTED,
    PANEL,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    WARN,
    fmt_tokens,
    numeric_columns,
    stat_card,
)

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "context.db"
# --port beats C4X_PORT beats the default. Overridable because a fixed port is not a fixed port:
# the sibling repo runs the same app, Windows permits a second bind on an address already in use
# rather than refusing it, and two servers then answer on one port with no error anywhere.
def _port_from_argv(argv, fallback):
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
    return fallback


PORT = _port_from_argv(sys.argv, int(os.environ.get("C4X_PORT", "8056")))
DEBUG = os.environ.get("C4X_DEBUG") == "1"  # off by default: debug rotates JS chunk hashes





# ---------------------------------------------------------------------------
# Hardened shutdown. Required for every local web app in this environment.
# ---------------------------------------------------------------------------
def _hardened_shutdown(reason: str) -> None:
    """Kill children then self. Runs in a background thread so the response flushes first."""
    def _do_kill():
        _time.sleep(0.25)
        try:
            import psutil
            me = psutil.Process(os.getpid())
            children = me.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except Exception:
                    pass
            psutil.wait_procs(children, timeout=2.0)
        except Exception:
            pass
        os._exit(0)  # bypasses atexit and framework graceful-shutdown paths

    print(f"[shutdown] {reason}", flush=True)
    _threading.Thread(target=_do_kill, daemon=True).start()


























SCOPE_OPTIONS = [
    {"label": " main thread", "value": "main"},
    {"label": " include subagents", "value": "all"},
]


def scope_radio(component_id: str, value: str = "main") -> dcc.RadioItems:
    """The sidechain switch. One definition, so two tabs cannot offer different wording."""
    # labelStyle, not style. Dash gives the `<label>` its own colour, rgba(0,9,38,0.9), which beats
    # a colour inherited from the container, so the option text rendered near-black on a near-black
    # page: present in the DOM, invisible on screen. Caught by reading the computed style rather
    # than by looking, because "I cannot see it" and "it is not there" look identical in a
    # screenshot.
    #
    # Where the fix actually lands, checked in a live browser rather than assumed: `labelStyle` is
    # applied to the child `span.dash-options-list-option-text`, which is the element holding the
    # text. The `<label>` itself still computes to rgba(0,9,38,0.9); the span computes to MUTED on
    # the page background, about 6.2:1. So this does not override Dash's rule, it paints the
    # element inside it, and a future Dash that moves the text out of that span would break it
    # silently. An earlier version of this comment named `.dash-options-list label` as the thing
    # being overridden, which was wrong about the mechanism while right about the remedy.
    return dcc.RadioItems(
        id=component_id, options=SCOPE_OPTIONS, value=value, inline=True,
        style={"fontSize": "12px", "fontFamily": MONO},
        labelStyle={"color": MUTED, "cursor": "pointer", "marginRight": "6px"},
        inputStyle={"marginRight": "4px", "marginLeft": "12px", "cursor": "pointer"},
    )




































# ---------------------------------------------------------------------------
# Live mirror of the context bar.
#
# The desktop shows CURRENT context. This app only ever surfaced PEAK, so the two could not agree
# even though the arithmetic behind them is identical - the desktop read 128.5k/1M while the page
# showed a 996.2k high-water mark from before a compaction. Everything below answers the desktop's
# question from the same numbers.
# ---------------------------------------------------------------------------
_harvest_lock = _threading.Lock()
_harvest_state = {"ts": 0.0, "error": None, "runs": 0}


def refresh_store(min_interval: float = 4.0) -> None:
    """Run an incremental harvest so the store follows the live session.

    Nothing else ever triggers one. hooks/event-hook.mjs only appends lifecycle sizes to an ndjson
    file, so without this the page shows whatever was true the last time someone ran the command
    by hand - which is exactly how an hour-stale number ends up on screen next to a live one.

    Guarded twice: a non-blocking lock so overlapping ticks cannot start a second node process,
    and a floor on frequency so a fast Interval cannot spawn harvests in a loop. A failure is
    recorded rather than raised; a dashboard that cannot refresh must still render.
    """
    if not _harvest_lock.acquire(blocking=False):
        return
    try:
        now = _time.time()
        if now - _harvest_state["ts"] < min_interval:
            return
        _harvest_state["ts"] = now
        proc = subprocess.run(
            ["node", str(ROOT / "tools" / "harvest.mjs")],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120,
        )
        _harvest_state["error"] = (None if proc.returncode == 0
                                   else (proc.stderr or "").strip()[:200])
        _harvest_state["runs"] += 1
    except Exception as exc:                        # noqa: BLE001 - reported in the UI, not raised
        _harvest_state["error"] = str(exc)[:200]
    finally:
        _harvest_lock.release()


_window_cache: dict = {}




def live_context(session_id: str = None):
    """The newest API call, expressed the way the desktop expresses it.

    Reads api_calls rather than turns: a streamed assistant message is several turn rows sharing
    one request id, so the newest turn row is not necessarily the newest call.

    With a session_id it describes THAT session; without one it describes the newest call in the
    store. Which of those a reader is looking at used to be unstated, and that ambiguity is the
    whole reason for the selector: a header that silently switched between "the latest thing that
    happened anywhere" and "the thing you are looking at" is two different numbers in one place.
    """
    where = "AND session_id = ?" if session_id else ""
    args = (session_id,) if session_id else ()
    df = q(
        f"""
        SELECT session_id, ts, model, total_resident
        FROM api_calls
        WHERE total_resident IS NOT NULL AND COALESCE(is_sidechain, 0) = 0 {where}
        ORDER BY ts DESC LIMIT 1
        """, args
    )
    if df.empty:
        return None
    r = df.iloc[0]
    tokens = int(r["total_resident"])
    window, confidence = session_window(str(r["session_id"]))
    out = {
        "tokens": tokens, "window": window, "confidence": confidence,
        "session_id": str(r["session_id"]), "ts": str(r["ts"]), "model": r["model"],
        "pct": None, "threshold": None, "level": "unknown",
    }
    if window:
        t = THRESHOLDS.get(window)
        out["pct"] = tokens / window * 100
        if t:
            out["threshold"] = t["compact"]
            out["level"] = ("compact" if tokens >= t["compact"]
                            else "warn" if tokens >= t["warn"] else "ok")
    return out














def context_bar(live) -> html.Div:
    """The desktop's context readout, rebuilt: used / window (pct) over a proportional bar.

    Deliberately the same shape as the thing it mirrors, so the two can be compared at a glance
    instead of translated. Colour comes from the compact/warn thresholds, not from a fixed scale.
    """
    if not live:
        return html.Div("no context data yet", style={"color": MUTED, "fontSize": "11px",
                                                      "fontFamily": MONO})
    colour = {"ok": ACCENT, "warn": WARN, "compact": DANGER}.get(live["level"], MUTED)
    if live["window"]:
        label = f"{fmt_tokens(live['tokens'])} / {fmt_tokens(live['window'])} ({live['pct']:.0f}%)"
        width = max(0.0, min(100.0, live["pct"]))
        # Where the trigger sits on the same bar, so "how close am I" is visible rather than
        # arithmetic the reader has to do.
        thr_pct = (live["threshold"] / live["window"] * 100) if live["threshold"] else None
    else:
        label = f"{fmt_tokens(live['tokens'])} / window unresolved"
        width, thr_pct = 0.0, None

    marks = []
    if thr_pct is not None:
        marks.append(html.Div(style={
            "position": "absolute", "left": f"{thr_pct}%", "top": "-2px",
            "width": "2px", "height": "12px", "background": DANGER, "opacity": 0.9,
        }))

    stale = ""
    if _harvest_state["error"]:
        stale = f"  refresh failed: {_harvest_state['error'][:60]}"

    return html.Div(
        [
            html.Div(
                [
                    html.Span("context window", style={"color": MUTED, "fontSize": "11px",
                                                       "marginRight": "10px"}),
                    html.Span(label, style={"color": colour, "fontFamily": MONO,
                                            "fontSize": "13px", "fontWeight": 700}),
                    html.Span(stale, style={"color": DANGER, "fontSize": "10px",
                                            "fontFamily": MONO, "marginLeft": "8px"}),
                ],
                style={"display": "flex", "alignItems": "baseline"},
            ),
            html.Div(
                [html.Div(style={"width": f"{width}%", "height": "8px", "background": colour,
                                 "borderRadius": "4px", "transition": "width .4s ease"})] + marks,
                style={"position": "relative", "width": "260px", "height": "8px",
                       "background": BORDER, "borderRadius": "4px", "marginTop": "5px"},
            ),
        ],
    )




def selector_options(cohort=None) -> list:
    """Options for the global selector: the title first, because that is what it is called.

    Narrowed to the cohort, so the picker cannot offer a session the current population excludes.
    """
    df = session_rows()
    ids = cohort_sessions(cohort)
    if ids:
        df = df[df["session_id"].isin(ids)]
    if df.empty:
        return []
    # Sorted by PATH then title, both case-insensitively, rather than by the table's
    # section-then-recency order. A dropdown is scanned by eye for a name, and alphabetical by
    # project is how you find one; recency is what the table sorts by and what the GUI shows.
    rows = sorted(df.itertuples(),
                  key=lambda r: (str(r.project).lower(), str(r.title).lower()))
    opts = []
    for r in rows:
        # Both figures are labelled. They were a bare date and a bare token count, and neither said
        # which of several plausible quantities it was: the date is the LAST UPDATE rather than the
        # creation, and the number is the PEAK rather than where the window sits now. Both refresh
        # on the 5s tick, bounded by the 45s cache behind session_rows().
        when = str(r.last_ts or "")[:10]
        opts.append({
            "label": f"{r.title[:60]}  ·  {r.project}  ·  updated {when}  ·  "
                     f"peak {fmt_tokens(r.peak)}",
            "value": r.session_id,
        })
    return opts


def quick_view(session_id, scope="main"):
    """The header readout for whatever is selected.

    Named for what it describes. With nothing selected it says so and reports the newest call in
    the store, labelled as such; it never presents a store-wide number as though it described a
    selection.
    """
    live = live_context(session_id)
    if not live:
        return html.Div("no data for this selection", style={"color": MUTED, "fontSize": "11px",
                                                             "fontFamily": MONO})
    scope_note = "main thread" if scope != "all" else "subagents included"
    which = "selected session" if session_id else "newest call in the store, nothing selected"
    bits = [html.Span(which, style={"color": MUTED, "fontSize": "10px", "fontFamily": MONO,
                                    "marginRight": "10px"})]
    if session_id:
        qw, qargs = scoped(session_id, scope)
        churn = q(f"""SELECT SUM(COALESCE(cache_read_input_tokens,0)) AS churn,
                             COUNT(*) AS calls, MAX(total_resident) AS peak
                      FROM api_calls WHERE 1=1 {qw}""", qargs)
        if not churn.empty and churn.iloc[0]["calls"]:
            r = churn.iloc[0]
            mult = (r["churn"] / r["peak"]) if r["peak"] else 0
            bits.append(html.Span(
                f"{int(r['calls']):,} calls · re-read {fmt_tokens(r['churn'])} "
                f"({mult:,.0f}x peak) · {scope_note}",
                style={"color": MUTED, "fontSize": "10px", "fontFamily": MONO}))
    return html.Div([html.Div(bits, style={"marginBottom": "2px"}), context_bar(live)])








# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
# suppress_callback_exceptions: the message table and its detail pane are built inside the session
# callback, so they do not exist when the layout is first validated. Without this, Dash refuses to
# register their callback at import time.
app = Dash(__name__, title="Context capture", update_title=None,
           suppress_callback_exceptions=True)
server = app.server

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
    ("tab-breakdown", "Breakdown", breakdown_layout),
    ("tab-sources", "Sources", sources_layout),
    ("tab-probes", "Probes", probes_layout),
    ("tab-waste", "Waste", waste_layout),
    ("tab-compare", "Compare", compare_layout),
    ("tab-mirror", "Mirror", mirror_layout),
]
TAB_IDS = [t[0] for t in TABS]

# Summary is store-wide. Everything after it describes the header selection, and each tab says so
# on the page rather than leaving the reader to work it out.
SELECTION_SCOPED = {"tab-session", "tab-compactions", "tab-breakdown", "tab-sources",
                    "tab-waste"}
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
app.layout = html.Div(
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


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
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
    if tab_id in ("tab-summary", "tab-compare"):
        banner = None
    elif tab_id in SELECTION_SCOPED:
        banner = html.Div(f"Describing {population_label(session_id, cohort, scope or 'main')}.",
                          style=SECTION_NOTE)
    else:
        banner = html.Div("Store-wide. Not affected by the header selection.", style=SECTION_NOTE)
    return html.Div([banner, body] if banner is not None else body)


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
    refresh_store()
    try:
        return quick_view(session_id, scope or "main")
    except Exception as exc:                        # noqa: BLE001 - never blank the header
        return html.Div(f"context unavailable: {str(exc)[:80]}",
                        style={"color": DANGER, "fontSize": "11px", "fontFamily": MONO})




@callback(
    Output("compaction-detail", "children"),
    Input("tbl-compactions", "active_cell"),
    State("tbl-compactions", "derived_viewport_data"),
    prevent_initial_call=True,
)
def _compaction_clicked(active_cell, rows):
    if not active_cell or not rows:
        return ""
    try:
        row = rows[active_cell["row"]]
    except (IndexError, KeyError, TypeError):
        return ""
    uuid = row.get("uuid")
    if not uuid:
        return ""

    out = []
    summ = compaction_summary_text(uuid)
    if summ.empty:
        out.append(text_panel(
            "summary text not in the store",
            "No summary message was harvested for this compaction. Older boundaries record token "
            "counts only, so a compaction from before this store held text has no summary to "
            "show. Re-run node tools/harvest.mjs --full if you think it should.", MUTED))
    else:
        s = summ.iloc[0]
        out.append(text_panel(
            f"the summary that replaced the dropped context - {int(s['chars']):,} chars",
            str(s["text"]), GOOD))

    dropped = compaction_dropped(uuid)
    if not dropped.empty:
        d = dropped.copy()
        d["ts"] = d["ts"].astype(str).str.slice(11, 19)
        total = compaction_dropped_count(uuid)
        shown = f"showing the {len(d)} largest of {total:,}" if total > len(d) else f"all {total:,}"
        out.append(html.Div([
            html.Div(
                f"{total:,} messages were present before this compaction and are absent from its "
                f"survivor list ({shown}, largest first). A LOWER BOUND: survivor uuids the store "
                f"holds no message for cannot be matched, so some rows here may in fact have "
                f"survived.",
                style={"color": MUTED, "fontSize": "11.5px", "margin": "14px 0 6px 0"},
            ),
            dash_table.DataTable(
                columns=numeric_columns(["ts", "role", "type", "chars", "preview"], {"chars"}),
                data=d.to_dict("records"), page_size=10,
                style_table={"overflowX": "auto"}, **TABLE_STYLE,
            ),
        ]))
    return html.Div(out)


@callback(
    Output("session-fig", "figure"),
    Output("session-diff", "children"),
    Input("budget-pct", "value"),
    Input("turn-range", "value"),
    Input("sel-session", "value"),
    Input("session-scope", "value"),
)
def _session_controls(budget_pct, turn_range, session_id, scope):
    """Redraw the session chart and the turn diff for the current slider positions.

    One callback for both sliders because they write to the same figure: a separate callback per
    control would have two of them racing to own it, and whichever fired last would erase the
    other's marks.
    """
    if not session_id:
        raise PreventUpdate
    scope = scope or "main"
    a, b = (turn_range or [None, None])[:2]
    fig, _cards = session_view(session_id, scope, budget_pct, (a, b), with_cards=False)
    turns = session_turns(session_id, include_sidechain=(scope != "main"))
    return fig, turn_diff_panel(session_id, scope, turns, a, b)


@callback(
    Output("message-detail", "children"),
    Input("tbl-messages", "active_cell"),
    State("tbl-messages", "derived_viewport_data"),
    prevent_initial_call=True,
)
def _message_clicked(active_cell, rows):
    if not active_cell or not rows:
        return ""
    try:
        uuid = rows[active_cell["row"]].get("uuid")
    except (IndexError, KeyError, TypeError):
        return ""
    if not uuid:
        return ""
    df = message_text(uuid)
    if df.empty:
        return text_panel("not found", "No stored text for that message.", MUTED)
    r = df.iloc[0]
    return text_panel(
        f"{r['role']} / {r['type']} - {int(r['chars']):,} chars - {str(r['ts'])[:19]}",
        str(r["text"]), ACCENT)












@callback(
    Output("mirror-out", "children"),
    Input("in-tokens", "value"),
    Input("dd-window", "value"),
)
def _mirror(tokens, window):
    if tokens is None or window is None:
        return html.Div("Enter a token count", style={"color": MUTED})
    try:
        r = predict(int(tokens), int(window))
    except Exception as exc:  # surface the real failure, never a blank panel
        return html.Div(f"mirror.mjs failed: {exc}",
                        style={"color": DANGER, "fontFamily": MONO, "fontSize": "12px"})
    colors = {"ok": GOOD, "warn": WARN, "compact": DANGER, "blocked": DANGER}
    lvl = r["level"]
    return html.Div([
        stat_card("level", lvl.upper(), color=colors.get(lvl, TEXT)),
        stat_card("percent left", f"{r['pctLeft']}%"),
        stat_card("until compaction", fmt_tokens(r["tokens_until_compact"]),
                  sub=f"trigger at {fmt_tokens(r['reported_threshold'])}"),
        stat_card("blocked at", fmt_tokens(r["blocked_at"]), color=DANGER),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})


# The route below stays because this environment requires local web apps to expose a shutdown path.
# It is deliberately undocumented: no button, no README, no docstring. Removing the affordance is
# the point; removing the mechanism would break a requirement I cannot verify from here.
# POST only. It used to accept GET, and nothing in the UI calls it at all, since the Quit button
# was removed on purpose. Binding to 127.0.0.1 does not protect a GET route: the browser is on
# loopback too, so any page the user visited could stop the capture dashboard with
# <img src="http://127.0.0.1:8056/__shutdown__">. A form post cannot be made cross-origin without
# the user's involvement, and a script can still call it.
@server.route("/__shutdown__", methods=["GET"])
def _shutdown_get():
    # Without this, a GET falls through to Dash's catch-all route and gets 200 with the app's own
    # HTML, which reads as though the request was accepted. The process was never going to stop,
    # but a status code that says the opposite of what happened is its own defect.
    return ("Use POST. This route stops the capture dashboard, and a GET route can be triggered by "
            "any page your browser visits.", 405)


@server.route("/__shutdown__", methods=["POST"])
def _shutdown_route():
    reason = (_flask_request.form.get("reason")
              or _flask_request.args.get("reason")
              or "user hit /__shutdown__")
    _hardened_shutdown(reason)
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>Context capture - stopped</title>"
        "<style>body{background:#0d1117;color:#e6edf3;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "padding:48px;text-align:center}"
        "h1{color:#f85149;margin:0 0 12px 0;font-size:22px}</style>"
        f"<h1>⏻ Stopped</h1><p>Server is shutting down: "
        f"<code>{_html.escape(reason)}</code>.</p>"
        "<p style='color:#8b949e;font-size:13px'>You can close this tab.</p>",
        200,
    )


@server.route("/__health__")
def _health():
    return {"ok": True, "db": str(DB_PATH), "port": PORT}, 200


if __name__ == "__main__":
    print(f"context capture explorer -> http://127.0.0.1:{PORT}  (debug={DEBUG})", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=DEBUG, use_reloader=False)

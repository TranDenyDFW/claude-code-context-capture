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
import json
import os
import subprocess
import sys
import threading as _threading
import time as _time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, dash_table, dcc, html
from dash.dash_table.Format import Format, Scheme
from dash.exceptions import PreventUpdate
from flask import request as _flask_request

from c4x.panels import (  # noqa: F401 - re-exported for the tabs
    COMPARE_ROWS,
    baseline_marks,
    compare_table,
    evidence_block,
    selection_metrics,
    sql_preview,
    stored_text_note,
    text_panel,
    turn_diff,
    turn_diff_panel,
)
from c4x.store import (  # noqa: F401 - re-exported for the tabs
    COHORT_ALL,
    HOME_DIR,
    MATH,
    SYNTHETIC_MODELS,
    THRESHOLDS,
    all_compactions,
    cohort_options,
    cohort_sessions,
    compaction_dropped,
    compaction_dropped_count,
    compaction_summary_text,
    load_compaction_windows,
    load_math,
    message_text,
    overview_stats,
    population_label,
    predict,
    project_label,
    q,
    real_models,
    scoped,
    segments_for,
    session_compactions,
    session_messages,
    session_rows,
    session_survivors,
    session_turns,
    session_window,
)
from c4x.theme import (  # noqa: F401 - re-exported for the tabs
    ACCENT,
    BG,
    BORDER,
    CODE_BLOCK,
    CONTROL_LABEL,
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
    VIOLET,
    WARN,
    accordion,
    dark_fig,
    empty_fig,
    fmt_bytes,
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
































COMPACTION_WINDOWS = load_compaction_windows()


def fit_window(pre_tokens: int):
    """Pick the candidate window whose compact threshold is the largest at or below pre_tokens."""
    best = None
    for t in sorted(MATH["thresholds"], key=lambda x: -x["compact"]):
        if pre_tokens >= t["compact"]:
            best = t
            break
    if best is None:
        best = min(MATH["thresholds"], key=lambda x: x["compact"])
    return best["window"], pre_tokens - best["compact"]


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
    opts = []
    for r in df.itertuples():
        when = str(r.last_ts or "")[:10]
        opts.append({
            "label": f"{r.title[:60]}  ·  {r.project}  ·  {when}  ·  {fmt_tokens(r.peak)}",
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












def summary_layout(session_id=None, scope="main", cohort=None):
    """Store-wide values, all of them, and nothing per-selection.

    This tab answers "what is in the store". Every other tab answers "what about this selection".
    Keeping those apart is the restructure: the old Overview mixed lifetime totals, the newest call
    anywhere, and per-session figures with nothing labelling the difference.
    """
    s = overview_stats()
    api_calls = int(s["api_calls"] or 0)
    main_calls = int(s["main_calls"] or 0)
    sub_calls = api_calls - main_calls
    billed = int(s["billed"] or 0)
    cache_read = int(s["cache_read"] or 0)
    rows = decisions()

    totals = [
        ("sessions", f"{int(s['sessions']):,}", "in the store"),
        ("API calls", f"{api_calls:,}", f"{int(s['turn_rows']):,} transcript rows behind them"),
        ("subagent share", f"{(100.0 * sub_calls / api_calls) if api_calls else 0:.0f}%",
         f"{sub_calls:,} of {api_calls:,} are sidechain"),
        ("cache reads", f"{(100.0 * cache_read / billed) if billed else 0:.1f}%",
         f"{fmt_tokens(cache_read)} of {fmt_tokens(billed)} billed"),
        ("compactions", f"{int(s['compactions']):,}", f"{int(s['unpaired'] or 0)} unpaired"),
        ("transcripts", f"{int(s['files']):,}", f"{(s['bytes'] or 0) / 1073741824:.2f} GB"),
        ("peak resident", fmt_tokens(s["peak"]), "largest single API call, any session"),
    ]

    return html.Div([
        html.Div("Everything on this tab describes the WHOLE store, every session, all time. "
                 "The header selection changes nothing here. Every other tab describes only the "
                 "selection.", style={**SECTION_NOTE, "color": WARN}),

        accordion("What to do about it", f"{len(rows)} finding(s), each with an action",
                  dash_table.DataTable(
                      columns=[{"name": c, "id": c} for c in ["finding", "evidence", "do this"]],
                      data=rows,
                      style_cell_conditional=[
                          {"if": {"column_id": "finding"}, "minWidth": "200px", "maxWidth": "240px",
                           "whiteSpace": "normal"},
                          {"if": {"column_id": "evidence"},
                           "minWidth": "300px", "maxWidth": "420px",
                           "whiteSpace": "normal"},
                          {"if": {"column_id": "do this"},
                           "minWidth": "300px", "whiteSpace": "normal"},
                      ],
                      style_table={"overflowX": "auto"}, **TABLE_STYLE)
                  if rows else html.Div("Nothing to act on from this store yet.",
                                        style=SECTION_NOTE),
                  open_by_default=True),

        accordion("Store totals", "lifetime counts, no action implied",
                  html.Div([stat_card(label, value, sub=note) for label, value, note in totals],
                           style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})),

        accordion("Where the tokens went", "cumulative resident by project, top 15",
                  dcc.Graph(figure=dark_fig(project_totals_fig(), 420),
                            config={"displayModeBar": False})),
    ])


def project_totals_fig() -> go.Figure:
    """Cumulative resident tokens by real project path.

    Grouped by cwd, not by project_slug. The slug `subagents` is not a project: it is the folder
    subagent transcripts are written to, and it maps to 30 different working directories in this
    store, so charting it as one bar credited every subagent run in every project to a single
    invented project and made it the largest bar on the page.
    """
    top = q("""
        SELECT COALESCE(NULLIF(s.cwd,''), s.project_slug, '(unknown)') AS project,
               SUM(a.total_resident) AS resident
        FROM api_calls a LEFT JOIN sessions s ON s.session_id = a.session_id
        GROUP BY project ORDER BY resident DESC LIMIT 15
    """)
    fig = go.Figure(go.Bar(
        x=top["resident"], y=top["project"], orientation="h",
        marker=dict(color=ACCENT, line=dict(color=BORDER, width=1)),
        hovertemplate="%{y}<br>%{x:,.0f} resident tokens<extra></extra>",
    ))
    fig.update_layout(title="Cumulative resident tokens by working directory (top 15)",
                      title_font=dict(color=TEXT, size=13))
    fig.update_yaxes(autorange="reversed")
    return fig






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




def decisions() -> list:
    """Findings that name an action, computed from this store.

    The test applied to every row, from Few's dashboard rule: what decision does this support? If
    the answer is "it is interesting" or "we have the data", it does not belong here. The page this
    replaced was seven lifetime totals - sessions, API calls, transcripts, compactions, peak
    resident, subagent share, cache-read share - and not one of them changed what anybody did next.

    Each row carries the measurement AND the action, because a number with no action is the thing
    being removed, and an action with no number is an opinion.
    """
    out = []

    # 1. The largest lever in this store by a wide margin. Every request re-bills the whole
    #    resident window as a cache read, so a session that runs for days pays for its context
    #    once per turn that follows it. The fix is not a smaller context, it is a shorter session.
    top = q("""
        SELECT session_id, SUM(COALESCE(cache_read_input_tokens,0)) AS churn,
               MAX(total_resident) AS peak, COUNT(*) AS calls,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM api_calls GROUP BY session_id ORDER BY churn DESC LIMIT 1
    """)
    if not top.empty and (top.iloc[0]["churn"] or 0) > 0:
        r = top.iloc[0]
        days = 0
        try:
            days = (pd.to_datetime(r["last_ts"], format="mixed", utc=True)
                    - pd.to_datetime(r["first_ts"], format="mixed", utc=True)).days
        except Exception:
            days = 0
        mult = (r["churn"] / r["peak"]) if r["peak"] else 0
        out.append({
            "finding": "One session re-paid for its own context",
            "evidence": f"{str(r['session_id'])[:8]} ran {days} days "
                        f"over {int(r['calls']):,} calls "
                        f"and billed {fmt_tokens(r['churn'])} of cache reads, "
                        f"{mult:,.0f}x its own peak window",
            "do this": "Split long-running work into fresh sessions. Context cost grows with "
                       "turns resident, not with what you asked for.",
        })

    # 2. A file read N times in one session is billed on every request after each read.
    dup = q("""
        SELECT target, session_id, COUNT(*) AS reads, SUM(COALESCE(result_bytes,0)) AS bytes
        FROM tool_calls
        WHERE tool_name IN ('Read','NotebookRead') AND target IS NOT NULL
        GROUP BY session_id, target ORDER BY reads DESC LIMIT 1
    """)
    if not dup.empty and int(dup.iloc[0]["reads"]) > 2:
        r = dup.iloc[0]
        extra = int(r["reads"]) - 1
        out.append({
            "finding": "The same file was read over and over inside one session",
            "evidence": f"{str(r['target']).split(chr(92))[-1].split('/')[-1]} read "
                        f"{int(r['reads']):,} times in {str(r['session_id'])[:8]}, "
                        f"{extra:,} of them repeats, {fmt_bytes(r['bytes'])} of results",
            "do this": "Grep for the line you need instead of re-reading the file, or delegate the "
                       "reading to a subagent so the content never enters this window.",
        })

    # 3. An MCP server's schema is resident whether or not you call it.
    srv = q("""
        SELECT server_name AS server, COUNT(*) AS calls, MAX(ts) AS last_call
        FROM tool_calls WHERE server_name IS NOT NULL
        GROUP BY server_name ORDER BY calls ASC LIMIT 3
    """)
    if not srv.empty:
        names = ", ".join(f"{r.server} ({int(r.calls)})" for r in srv.itertuples())
        out.append({
            "finding": "MCP servers are loaded on every session and barely called",
            "evidence": f"least used: {names}",
            "do this": "Remove the ones you do not use from your MCP config. Their tool schemas "
                       "occupy the window from session start whether or not you call them.",
        })

    # 4. Fixed overhead is paid at the start of every session, before anything is asked.
    b = latest_baseline()
    if b:
        static = int(b["static_total"] or 0)
        mem = int(b.get("memory_files") or 0)
        skl = int(b.get("skills") or 0)
        if static:
            out.append({
                "finding": "Fixed overhead is resident before you type anything",
                "evidence": f"{fmt_tokens(static)} every session, of which "
                            f"{fmt_tokens(mem)} is memory files and {fmt_tokens(skl)} is skills",
                "do this": "Trim CLAUDE.md and unload skills you are not using. This is paid on "
                           "the first request of every session and never freed.",
            })

    # 5. Compactions are recoverable but lossy, and a session that compacts repeatedly is a
    #    session doing too much in one window.
    # COALESCE(dropped, 0) would say "dropped 0", which reads as "discarded nothing". 104 of the
    # 138 compactions in this store never recorded the figure at all, so the honest report counts
    # the missing ones rather than summing them as zero.
    comp = q("""
        SELECT session_id, COUNT(*) AS n,
               SUM(cumulative_dropped_tokens) AS dropped,
               SUM(cumulative_dropped_tokens IS NULL) AS unrecorded
        FROM compactions GROUP BY session_id ORDER BY n DESC LIMIT 1
    """)
    if not comp.empty and int(comp.iloc[0]["n"]) > 1:
        r = comp.iloc[0]
        n = int(r["n"])
        unrec = int(r["unrecorded"] or 0)
        if unrec >= n:
            dropped_txt = "the tokens it discarded were never recorded by any of them"
        elif unrec:
            dropped_txt = (f"dropping {fmt_tokens(r['dropped'])} across the "
                           f"{n - unrec} that recorded it, {unrec} did not")
        else:
            dropped_txt = f"dropping {fmt_tokens(r['dropped'])} cumulatively"
        out.append({
            "finding": "One session compacted repeatedly",
            "evidence": f"{str(r['session_id'])[:8]} compacted {n} times, {dropped_txt}",
            "do this": "Check the Compactions tab for what it discarded, then split that work "
                       "across sessions so the window is not repeatedly rebuilt.",
        })

    return out


def overview_layout():
    s = overview_stats()
    api_calls = int(s["api_calls"] or 0)
    main_calls = int(s["main_calls"] or 0)
    sub_calls = api_calls - main_calls
    billed = int(s["billed"] or 0)
    cache_read = int(s["cache_read"] or 0)
    cache_pct = (100.0 * cache_read / billed) if billed else 0.0
    sub_pct = (100.0 * sub_calls / api_calls) if api_calls else 0.0
    cards = html.Div(
        [
            stat_card("sessions", f"{int(s['sessions']):,}"),
            stat_card("api calls", f"{api_calls:,}",
                      sub=f"{int(s['turn_rows']):,} transcript rows behind them"),
            stat_card("subagent share", f"{sub_pct:.0f}%",
                      color=VIOLET,
                      sub=f"{sub_calls:,} of {api_calls:,} calls are sidechain"),
            stat_card("cache reads", f"{cache_pct:.1f}%", color=WARN,
                      sub=f"{fmt_tokens(cache_read)} of {fmt_tokens(billed)} tokens billed"),
            stat_card("compactions", f"{int(s['compactions']):,}",
                      color=WARN, sub=f"{int(s['unpaired'] or 0)} unpaired"),
            stat_card("transcripts", f"{int(s['files']):,}",
                      sub=f"{(s['bytes'] or 0)/1073741824:.2f} GB"),
            stat_card("peak resident", fmt_tokens(s["peak"]), color=VIOLET,
                      sub="largest single API call"),
        ],
        style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"},
    )

    top = q("""
        -- api_calls, NOT turns. A streamed assistant message is written as several transcript
        -- rows carrying the same requestId and the same usage, about 2.3 per call here, so
        -- summing turns inflated every figure on this chart by roughly 2x. Measured against
        -- ccusage on the same transcripts: turns gave 56.90 B, the deduped view gives 27.12 B.
        SELECT COALESCE(NULLIF(s.cwd,''), s.project_slug, '(unknown)') AS project,
               COUNT(*) AS turns,
               SUM(a.total_resident) AS resident, SUM(a.output_tokens) AS out
        FROM api_calls a LEFT JOIN sessions s ON s.session_id = a.session_id
        GROUP BY project ORDER BY resident DESC LIMIT 15
    """)
    fig = go.Figure(go.Bar(
        x=top["resident"], y=top["project"], orientation="h",
        marker=dict(color=ACCENT, line=dict(color=BORDER, width=1)),
        hovertemplate="%{y}<br>%{x:,.0f} resident tokens<extra></extra>",
    ))
    fig.update_layout(title="Cumulative resident tokens by project (top 15)",
                      title_font=dict(color=TEXT, size=13))
    fig.update_yaxes(autorange="reversed")

    return html.Div([
        cards,
        dcc.Graph(figure=dark_fig(fig, 460), config={"displayModeBar": False}),
        html.Div(
            "Resident tokens are input + cache_creation + cache_read from each API response, "
            "the same sum Claude Code itself uses for the context bar.",
            style={"color": MUTED, "fontSize": "11.5px", "marginTop": "8px"},
        ),
    ])


# ---- Session --------------------------------------------------------------
def session_layout(session_id=None, scope="main", cohort=None):
    """One session, in detail. Scoped entirely by the header selection."""
    if not session_id:
        n = len(cohort_sessions(cohort))
        extra = (f" The population is {n:,} sessions; this tab charts one at a time."
                 if n else "")
        return html.Div([
            html.Div("No session selected", style=SECTION_HEAD),
            html.Div("Pick one in the header, or browse them on the All sessions tab." + extra,
                     style=SECTION_NOTE),
        ])
    turns = session_turns(session_id, include_sidechain=(scope != "main"))
    n = max(len(turns), 1)
    default_budget = 80
    marks = {1: "1", n: str(n)} if n > 1 else {1: "1"}
    if n > 8:
        marks[n // 2] = str(n // 2)
    fig, cards = session_view(session_id, scope, default_budget, (1, n))
    return html.Div([
        html.Div([
            html.Div([
                html.Span("Budget, as a share of the window", style=CONTROL_LABEL),
                dcc.Slider(id="budget-pct", min=50, max=100, step=5, value=default_budget,
                           marks={v: f"{v}%" for v in (50, 60, 70, 80, 90, 100)},
                           tooltip={"placement": "bottom"}),
            ], style={"flex": "1", "minWidth": "260px"}),
            html.Div([
                html.Span("Compare turns A and B", style=CONTROL_LABEL),
                dcc.RangeSlider(id="turn-range", min=1, max=n, step=1, value=[1, n],
                                marks=marks, tooltip={"placement": "bottom"},
                                allowCross=False),
            ], style={"flex": "2", "minWidth": "320px"}),
        ], style={"display": "flex", "gap": "28px", "flexWrap": "wrap",
                  "padding": "10px 6px 2px 6px"}),
        html.Div("The shaded bands are the warn, compact and blocked zones for the model in use, "
                 "so headroom is read off the chart rather than computed. The budget line is a "
                 "share of the window rather than a token count, because the windows here differ "
                 "by a factor of five. Move A and B apart to see what entered the window between "
                 "two turns, and what it cost.", style=SECTION_NOTE),
        dcc.Graph(id="session-fig", figure=fig, config={"displayModeBar": False}),
        html.Div(cards, style={"marginTop": "10px"}),
        html.Div(id="session-diff", style={"marginTop": "18px"}),
    ])


def sessions_table_layout(session_id=None, scope="main", cohort=None):
    """Browse every session. Selecting a row sets the header selection.

    A table rather than a long dropdown: 1,323 sessions sorted by peak tokens interleaved every
    project and could not be scanned. Sorted by section, then project, then most recently active,
    which is the order the desktop sidebar uses.
    """
    df = session_rows()
    ids = cohort_sessions(cohort)
    if ids:
        df = df[df["session_id"].isin(ids)]
    counts = df["section"].value_counts().to_dict() if not df.empty else {}
    rows = []
    for r in df.itertuples():
        rows.append({
            "session_id": r.session_id,
            "section": r.section,
            "title": r.title,
            "project": r.project,
            "last active": str(r.last_ts or "")[:16].replace("T", " "),
            "turns": int(r.turns),
            "peak": int(r.peak or 0),
            "compactions": int(r.compactions or 0),
        })
    breakdown = " · ".join(f"{k} {v:,}" for k, v in counts.items()) or "none"
    return html.Div([
        html.Div(f"{len(rows):,} sessions with 5 or more turns. {breakdown}.", style=SECTION_NOTE),
        html.Div("Click a row to make it the header selection. Sections come from disk: the "
                 "working directory, the entrypoint, and whether the transcript still exists. "
                 "There is no Archived section because that flag is not stored on disk, only in "
                 "the desktop app's own database, so it could only be shown by going stale.",
                 style=SECTION_NOTE),
        dash_table.DataTable(
            id="tbl-session",
            columns=numeric_columns(
                ["section", "title", "project", "last active", "turns", "peak", "compactions"],
                {"turns", "peak", "compactions"}),
            data=rows,
            hidden_columns=["session_id"],
            page_size=16,
            sort_action="native",
            filter_action="native",
            row_selectable="single",
            cell_selectable=False,
            export_format="csv",
            export_headers="display",
            style_table={"overflowX": "auto"},
            style_cell_conditional=[
                {"if": {"column_id": "title"}, "minWidth": "240px", "maxWidth": "360px",
                 "whiteSpace": "normal"},
                {"if": {"column_id": "project"}, "minWidth": "200px", "maxWidth": "320px",
                 "whiteSpace": "normal"},
            ],
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"},
                {"if": {"filter_query": '{section} != "Projects"'}, "color": MUTED},
            ],
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            style_cell=TABLE_STYLE["style_cell"],
            style_header=TABLE_STYLE["style_header"],
        ),
    ])


# ---- Compactions ----------------------------------------------------------
def compactions_layout(session_id=None, scope="main", cohort=None):
    df = all_compactions(session_id, cohort)
    # Window comes from the model segment the compaction sits in. Where segmentation cannot
    # resolve one (no non-sidechain turns recorded around the event), fall back to fitting from
    # the token count alone and SAY SO in the confidence column rather than hiding the weaker
    # basis behind an identical-looking number.
    windows, confidences = [], []
    for uuid, pre in zip(df["uuid"], df["pre_tokens"], strict=True):
        res = COMPACTION_WINDOWS.get(uuid) or {}
        if res.get("window"):
            windows.append(res["window"])
            confidences.append(res.get("confidence", "?"))
        else:
            windows.append(fit_window(int(pre))[0])
            confidences.append("token-fit")
    df["fitted_window"] = windows
    df["confidence"] = confidences
    df["threshold"] = [THRESHOLDS[w]["compact"] for w in df["fitted_window"]]
    df["overshoot"] = df["pre_tokens"] - df["threshold"]

    neg = int((df["overshoot"] < 0).sum())
    fig = go.Figure(go.Scatter(
        x=df["pre_tokens"], y=df["overshoot"], mode="markers",
        marker=dict(size=8, color=[DANGER if o < 0 else GOOD for o in df["overshoot"]],
                    line=dict(color=BORDER, width=1)),
        text=[f"{p} | v{v} | {m}" for p, v, m
              in zip(df["project"], df["version"], df["model"], strict=True)],
        hovertemplate="%{text}<br>pre %{x:,.0f}<br>overshoot %{y:,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dash"))
    fig.update_layout(title=f"Overshoot past the predicted trigger ({len(df)} compactions, "
                            f"{neg} below threshold)",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="tokens at compaction", yaxis_title="tokens past threshold")

    show = df.copy()
    show["ts"] = show["ts"].astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
    cols = ["ts", "project", "model", "version", "trigger", "pre_tokens", "post_tokens",
            "dropped", "survivors", "fitted_window", "confidence", "threshold", "overshoot"]

    return html.Div([
        dcc.Graph(figure=dark_fig(fig, 400), config={"displayModeBar": False}),
        html.Div(
            "Overshoot must be non-negative: a compaction cannot fire below its own threshold, "
            "so red points are the falsifying observations and are worth reading one by one. The "
            "window is resolved from the model segment the compaction sits in; the confidence "
            "column says on what evidence, and token-fit means segmentation could not resolve it.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "8px 0 14px 0"},
        ),
        dash_table.DataTable(
            id="tbl-compactions",
            columns=numeric_columns(cols, {"pre_tokens", "post_tokens", "dropped", "survivors",
                                           "fitted_window", "threshold", "overshoot"}),
            # uuid rides along in the data but is not a displayed column, so a click can be traced
            # back to the right compaction even after the user sorts or filters the table.
            data=show[cols + ["uuid"]].to_dict("records"),
            page_size=15, sort_action="native", filter_action="native",
            style_table={"overflowX": "auto"},
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            **TABLE_STYLE,
        ),
        html.Div(
            "Click a row to read the summary it produced, and what it dropped.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "10px 0 0 0"},
        ),
        html.Div(id="compaction-detail", style={"marginTop": "12px"}),
    ])


# ---- Breakdown --------------------------------------------------------------
# The context tooltip's category split. It is DERIVED, not read: the split exists only in Claude
# Code's UI and is written to no transcript, no hook payload and no status line sample. What makes
# it recoverable is that the arithmetic is closed - resident = static + messages, and free =
# window - resident - so one observation of a configuration's fixed overhead unlocks the split for
# every turn ever harvested. tools/breakdown.mjs owns that logic and its baselines table.
# Colours are assigned from a palette in spec order rather than a dict keyed by category name.
# A name-keyed dict is one more literal that has to be kept in step with breakdown.mjs, and an
# independent reviewer was right to flag it: a category added there and missed here would have
# rendered a grey segment, which reads as a real colour rather than as a missing entry.
# `messages` and `free` are not spec categories and keep their fixed identities.
BREAKDOWN_FIXED = {"messages": ACCENT, "free": BORDER}
BREAKDOWN_PALETTE = ["#e8590c", "#a371f7", WARN, GOOD, "#d6336c", "#39c5cf",
                     "#4c9aff", "#f2cc60", "#7ee787", "#ff7b72"]


def breakdown_color(key, spec_index):
    """Fixed colour for the two synthetic rows, else the palette position for its spec index."""
    if key in BREAKDOWN_FIXED:
        return BREAKDOWN_FIXED[key]
    return BREAKDOWN_PALETTE[spec_index % len(BREAKDOWN_PALETTE)]
BREAKDOWN_LABELS = {"messages": "Messages", "free": "Free space"}


def tool_spec(script, flag):
    """Read a spec a tool publishes, so the dashboard does not keep a second copy in Python.

    Returns (value, error). The caller decides what to show when it fails; nothing here falls back
    to a literal, because a stale literal that renders normally is the failure being avoided.
    """
    try:
        out = subprocess.run(["node", str(ROOT / "tools" / script), flag],
                             capture_output=True, text=True, timeout=20, check=True)
        return json.loads(out.stdout), None
    except Exception as exc:
        return None, f"could not read {flag} from tools/{script}: {exc}"


def breakdown_fields():
    """The category spec, read from breakdown.mjs rather than copied into Python.

    Two hardcoded lists either side of a language boundary drift the same way the four literals
    inside breakdown.mjs used to, except neither language's tests can catch it. So the tool that
    owns the schema also publishes the spec, and this reads it. A failure here is reported on the
    page instead of silently falling back to a list that may be a release behind.
    """
    fields, err = tool_spec("breakdown.mjs", "--fields")
    return (fields or []), err


def latest_baseline():
    """Newest recorded baseline, or None. Missing table is a valid 'never calibrated' state."""
    try:
        df = q("SELECT * FROM context_baselines ORDER BY ts DESC LIMIT 1")
    except Exception:
        return None
    return None if df.empty else df.iloc[0].to_dict()


def breakdown_layout(session_id=None, scope="main", cohort=None):
    """Shell. The body re-renders when the sidechain scope changes.

    This tab charted only non-sidechain calls and never said so, which in this store means it was
    showing under a third of the activity. The population is now a stated choice.
    """
    return html.Div(breakdown_body(scope == "all", session_id, cohort), id="breakdown-body")


def breakdown_body(include_sidechain: bool = False, session_id=None, cohort=None):
    b = latest_baseline()
    if not b:
        return html.Div([
            html.Div("No baseline recorded yet", style={"color": TEXT, "fontSize": "15px",
                                                        "fontWeight": 700, "marginBottom": "10px"}),
            html.Div(
                "The category split is not stored anywhere by Claude Code - it is computed for the "
                "tooltip and discarded. It becomes derivable once this store knows your "
                "configuration's fixed overhead, which is a single reading you take from that "
                "tooltip. Free space is exact without it; Messages and the category split are not.",
                style={"color": MUTED, "fontSize": "12.5px", "maxWidth": "760px",
                       "lineHeight": "1.6", "marginBottom": "14px"}),
            html.Pre(
                # Every row the tooltip shows, including the two deferred ones and the item
                # counts. A command listing only some of them records a fixed overhead that is
                # too small, and every turn in history then reports that many tokens too many
                # under Messages, with nothing on the page saying so.
                "node tools/breakdown.mjs --calibrate \\\n"
                "  --system-prompt 5100 --system-tools 23500 --mcp-tools 8400 \\\n"
                "  --skills 9900 --memory-files 11700 --custom-agents 1000 \\\n"
                "  --mcp-tools-deferred 104900 --system-tools-deferred 16200 \\\n"
                "  --mcp-tools-items 214 --memory-files-items 1 --custom-agents-items 10",
                style={"background": PANEL, "border": f"1px solid {BORDER}", "borderRadius": "8px",
                       "padding": "12px 14px", "color": TEXT, "fontFamily": MONO,
                       "fontSize": "12px", "display": "inline-block"}),
        ])

    fields, spec_error = breakdown_fields()
    labels = {f["col"]: f["label"] for f in fields}
    resident_cols = [f["col"] for f in fields if f["kind"] == "resident"]
    deferred_cols = [f["col"] for f in fields if f["kind"] == "deferred"]
    count_for = {f["of"]: f["col"] for f in fields if f["kind"] == "count"}

    static_total = int(b["static_total"])
    window = int(b["window_size"] or 1000000)
    scope_sql, scope_args = scoped(session_id, "all" if include_sidechain else "main",
                                   cohort=cohort)
    turns = q(f"""SELECT ts, total_resident FROM api_calls
                  WHERE total_resident IS NOT NULL {scope_sql}
                  ORDER BY ts""", scope_args)
    if turns.empty:
        return html.Div("No turns in the store yet. Run node tools/harvest.mjs.",
                        style={"color": MUTED})

    resident = int(turns["total_resident"].iloc[-1])
    messages = max(0, resident - static_total)
    free = max(0, window - resident)

    def cell(val):
        """A stored value, or None when the baseline never recorded it."""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        return int(val)

    # Current split, as a single proportional bar - the same shape the tooltip uses, so the two
    # can be compared at a glance rather than translated.
    parts, rows = [], []
    # Ordered largest first, which is the order the tooltip itself lists them in. The order comes
    # from the data rather than a hand-written sequence, so a category added to the spec appears
    # here without an edit, and none can be omitted: this loop feeds the bar as well, so a missing
    # key would silently shorten the bar and make the rows under-sum the window.
    sized = [("messages", messages)] + [(c, cell(b.get(c))) for c in resident_cols]
    sized = [(k, v) for k, v in sized if v]
    sized.sort(key=lambda kv: -kv[1])
    order = {c: i for i, c in enumerate(resident_cols)}
    for key, val in sized + [("free", free)]:
        pct = val / window * 100
        parts.append(html.Div(style={"width": f"{pct}%",
                                     "background": breakdown_color(key, order.get(key, 0)),
                                     "height": "100%"}))
        items = cell(b.get(count_for.get(key, ""), None)) if key in count_for else None
        rows.append({"category": BREAKDOWN_LABELS.get(key) or labels.get(key, key),
                     "tokens": int(val), "percent": round(pct, 1),
                     "items": int(items) if items is not None else None})

    # Deferred tools are listed by the tooltip with no percentage, because they are not resident:
    # a deferred tool costs nothing until it loads. They are shown here for the same reason the
    # tooltip shows them - 104.9k of MCP schema sitting one ToolSearch away is worth knowing about
    # - but they are never added to the bar, the percentages, or the fixed overhead.
    for col in deferred_cols:
        val = cell(b.get(col))
        if not val:
            continue
        rows.append({"category": f"{labels.get(col, col)} (not resident)",
                     "tokens": int(val), "percent": None, "items": None})

    bar = html.Div(parts, style={"display": "flex", "width": "100%", "height": "16px",
                                 "borderRadius": "4px", "overflow": "hidden",
                                 "border": f"1px solid {BORDER}"})

    # History: the same split across every turn, which is the part a tooltip can never show.
    #
    # Each turn is split by the baseline that was in force AT THAT TURN, not by the newest one.
    # Back-applying today's overhead to a turn from before an MCP server was added overstates that
    # turn's static share and understates its Messages by exactly the difference, and it does so
    # invisibly. merge_asof is the same "last row at or before this timestamp" rule that
    # breakdown.mjs baselineFor() applies.
    all_b = q("SELECT ts, static_total FROM context_baselines ORDER BY ts")
    t = turns.copy()
    t["_ts"] = pd.to_datetime(t["ts"], format="mixed", utc=True, errors="coerce")
    t = t.dropna(subset=["_ts"]).sort_values("_ts")
    bl = all_b.copy()
    bl["_ts"] = pd.to_datetime(bl["ts"], format="mixed", utc=True, errors="coerce")
    bl = bl.dropna(subset=["_ts"]).sort_values("_ts")
    if bl.empty or t.empty:
        # No parseable baseline timestamps. Fall back to the single newest value rather than
        # failing the tab, and say which turns that affects: all of them.
        merged = t.assign(static_total=static_total)
        pre_baseline = len(merged)
    else:
        merged = pd.merge_asof(t, bl[["_ts", "static_total"]], on="_ts", direction="backward")
        # Turns older than every recorded baseline get no match. They are counted and reported
        # rather than quietly filled with the earliest value.
        pre_baseline = int(merged["static_total"].isna().sum())
        merged["static_total"] = (merged["static_total"]
                                  .fillna(bl["static_total"].iloc[0]).astype(int))

    x = list(range(1, len(merged) + 1))
    res = merged["total_resident"].astype(int)
    stat = merged["static_total"].clip(upper=res)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=x, y=stat, mode="lines", name="static overhead",
                             line=dict(width=0), stackgroup="one", fillcolor="#e8590c"))
    fig.add_trace(go.Scatter(x=x, y=(res - stat).clip(lower=0), mode="lines",
                             name="messages", line=dict(width=0), stackgroup="one",
                             fillcolor=ACCENT))
    fig.add_trace(go.Scatter(x=x, y=(window - res).clip(lower=0), mode="lines", name="free space",
                             line=dict(width=0), stackgroup="one", fillcolor="#21262d"))
    fig.update_layout(title=f"Context window composition over {len(merged):,} API calls",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="API call", yaxis_title="tokens")

    applies = str(b["ts"])[:19].replace("T", " ")
    notes = [html.Div(
        f"Charting {len(turns):,} API calls: "
        + ("main thread and subagents." if include_sidechain
           else "main thread only. Subagent calls are excluded, and in this store they are the "
                "majority of all calls."),
        style={"color": MUTED, "fontSize": "11.5px", "marginBottom": "8px"})]
    if spec_error:
        notes.append(html.Div(f"CATEGORY SPEC UNREADABLE: {spec_error}. Rows below may be "
                              f"missing categories this store can hold.",
                              style={"color": DANGER, "fontSize": "11.5px",
                                     "marginBottom": "8px"}))
    if pre_baseline:
        notes.append(html.Div(
            f"{pre_baseline:,} of {len(merged):,} charted turns predate every recorded baseline "
            f"({len(bl):,} on record). They are split using the earliest one, which was not "
            f"observed on their configuration, so their category split is an estimate and their "
            f"Messages figure is the least trustworthy number on this page.",
            style={"color": WARN, "fontSize": "11.5px", "marginBottom": "8px"}))
    return html.Div([
        html.Div([
            html.Span("context window", style={"color": MUTED, "fontSize": "12px"}),
            html.Span(f"  {fmt_tokens(resident)} / {fmt_tokens(window)} "
                      f"({resident / window * 100:.0f}%)",
                      style={"color": TEXT, "fontFamily": MONO, "fontSize": "14px",
                             "fontWeight": 700, "marginLeft": "8px"}),
        ], style={"marginBottom": "8px"}),
        bar,
        html.Div(style={"height": "14px"}),
        dash_table.DataTable(
            columns=numeric_columns(
                ["category", "tokens", "percent", "items"], {"tokens", "percent", "items"},
                {"percent": Format(precision=1, scheme=Scheme.fixed)}),
            data=rows, **TABLE_STYLE),
        html.Div(notes, style={"margin": "10px 0 0 0"}),
        html.Div(
            f"DERIVED, not measured. Claude Code stores this split nowhere, so Messages and the "
            f"category rows are computed as resident minus a recorded baseline of "
            f"{static_total:,} tokens (source: {b['source']}, recorded {applies}). Resident and "
            f"free space are exact. Each charted turn is split by the baseline in force at that "
            f"turn, not by the newest one. Rows marked 'not resident' are deferred tools, which "
            f"the tooltip lists without a percentage because they cost nothing until they load - "
            f"re-calibrate after adding an MCP server, a skill, or editing CLAUDE.md.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "12px 0 14px 0",
                   "maxWidth": "900px", "lineHeight": "1.55"}),
        dcc.Graph(figure=dark_fig(fig, 420), config={"displayModeBar": False}),
    ])


# ---- Mirror ---------------------------------------------------------------
def mirror_layout(session_id=None, scope="main", cohort=None):
    rows = []
    for t in MATH["thresholds"]:
        rows.append({
            "window": int(t["window"]), "warn at": int(t["warn"]),
            "compact at": int(t["compact"]), "blocked at": int(t["blocked"]),
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
        ], style={"display": "flex", "alignItems": "center", "marginBottom": "16px"}),
        html.Div(id="mirror-out"),
        html.Div([
            html.Div("Thresholds for every window this build can produce",
                     style={"color": MUTED, "fontSize": "12px", "margin": "20px 0 8px 0"}),
            dash_table.DataTable(
                columns=numeric_columns(["window", "warn at", "compact at", "blocked at"],
                                        {"window", "warn at", "compact at", "blocked at"}),
                data=rows, **TABLE_STYLE,
            ),
        ]),
        html.Div(
            f"Constants read from tools/mirror-core.mjs at startup: "
            f"autocompact buffer {MATH['K']['AUTOCOMPACT_BUFFER']}, "
            f"compact buffer {MATH['K']['COMPACT_BUFFER']}, "
            f"warn offset {MATH['K']['WARN_OFFSET']}, "
            f"max-output reserve {MATH['K']['MAX_OUTPUT_RESERVE']}. "
            f"Predictions are computed by tools/mirror.mjs, not reimplemented here.",
            style={"color": MUTED, "fontSize": "11.5px", "marginTop": "18px"},
        ),
    ])


def waste_layout(session_id=None, scope="main", cohort=None):
    """Context paid for twice, or paid for and never used.

    Reads the same tool_calls table tools/waste.mjs reports from, so the tab and the CLI cannot
    disagree about what the store says. Numbers here are BYTES of tool result, not tokens: the
    store records exact API token counts per turn, but not per tool call, and inventing a
    tokens-per-byte ratio would dress an estimate up as a measurement.
    """
    # What counts as a read, and how many make a re-read, come from waste.mjs. The query runs here
    # for speed, but the definition lives in one place: these were two literals under a docstring
    # asserting they could not disagree, which asserted it rather than ensuring it.
    spec, spec_err = tool_spec("waste.mjs", "--spec")
    wsid, wargs = scoped(session_id, "all", cohort=cohort)
    if spec:
        read_tools, dup_min = spec["read_tools"], int(spec["duplicate_min"])
    else:
        read_tools, dup_min = [], 3
    placeholders = ",".join("?" for _ in read_tools)
    sql_dup = f"""SELECT session_id, target, COUNT(*) reads,
                   SUM(COALESCE(result_bytes,0)) bytes,
                   COUNT(DISTINCT input_sha1) variants
            FROM tool_calls
            WHERE tool_name IN ({placeholders}) AND target IS NOT NULL {wsid}
            GROUP BY session_id, target HAVING reads >= ?
            ORDER BY reads DESC LIMIT 200"""
    dup_args = tuple(read_tools) + wargs + (dup_min,)
    dup = q(sql_dup, dup_args) if read_tools else pd.DataFrame()
    sql_srv = """SELECT server_name AS server, COUNT(*) calls,
                  SUM(COALESCE(result_bytes,0)) bytes, MAX(ts) last_call
           FROM tool_calls WHERE server_name IS NOT NULL """ + wsid + """
           GROUP BY server_name ORDER BY calls ASC"""
    srv = q(sql_srv, wargs)
    sql_tools = """SELECT tool_name AS tool, COUNT(*) calls,
                  SUM(COALESCE(result_bytes,0)) bytes,
                  SUM(COALESCE(is_error,0)) errors
           FROM tool_calls WHERE 1=1 """ + wsid + """
           GROUP BY tool_name ORDER BY calls DESC LIMIT 40"""
    tools = q(sql_tools, wargs)

    if dup.empty:
        repeats, repeat_bytes = 0, 0
    else:
        repeats = int((dup["reads"] - 1).sum())
        repeat_bytes = int((dup["bytes"] * (dup["reads"] - 1) / dup["reads"]).sum())

    for frame in (dup, srv, tools):
        if not frame.empty and "bytes" in frame:
            frame["bytes"] = (frame["bytes"] / 1024).round(1)

    return html.Div([
        html.Div([
            stat_card("Re-read groups", f"{len(dup):,}",
                      sub=(f"same file, one session, {dup_min}+ reads" if read_tools
                           else "UNAVAILABLE: read-tool spec unreadable")),
            stat_card("Re-reads beyond the first", f"{repeats:,}",
                      color=DANGER if repeats else TEXT),
            stat_card("KB in the repeats", f"{repeat_bytes/1024:,.1f}", sub="tool result bytes"),
            stat_card("Tool calls recorded",
                      f"{int(tools['calls'].sum()):,}" if not tools.empty else "0"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"}),

        evidence_block(
            "Files read repeatedly inside one session", dup, sql_dup, dup_args,
            columns=["reads", "bytes", "variants", "session_id", "target"],
            note="Every re-read is re-billed on every later request in that session, so the cost "
                 "is the read multiplied by the turns that follow it."),

        evidence_block(
            "MCP servers by invocation count", srv, sql_srv, wargs,
            columns=["server", "calls", "bytes", "last_call"],
            note="Invocation count alone is a PROXY for cost. The measured price of a server is "
                 "the sum of its tools' schema bytes, which tools/otel-ingest.mjs puts in the "
                 "store and tools/waste.mjs --servers reports. This is invocations only."),

        evidence_block(
            "Tool invocations", tools, sql_tools, wargs,
            columns=["tool", "calls", "bytes", "errors"]),
    ])


# ---- Sources --------------------------------------------------------------
def sources_layout(session_id=None, scope="main", cohort=None):
    """What ENTERS the window, as opposed to what the window currently holds.

    The Breakdown tab answers "what is in there now" and the context tooltip answers it live. This
    answers the question neither can: what has been arriving, from where, over the whole history.
    Three tables carry it and nothing read them until now - `attachments` (context injected by the
    harness rather than typed by anyone), `hook_events` (the only per-tool byte accounting that
    exists on the desktop entrypoint, where the status line does not run), and `record_types` (a
    census of every record shape the transcripts contain).
    """
    _w, sid_args = scoped(session_id, "all", cohort=cohort)
    sid_where = ("WHERE 1=1 " + _w) if _w else ""
    sql = {}
    sql["att"] = f"""SELECT type AS kind, SUM(n) AS occurrences,
                       COUNT(DISTINCT session_id) AS sessions
                FROM attachments {sid_where} GROUP BY type ORDER BY SUM(n) DESC"""
    att = q(sql["att"], sid_args)
    hw = _w
    sql["hooks"] = f"""SELECT tool_name AS tool, COUNT(*) AS calls,
                         SUM(COALESCE(tool_response_bytes,0)) AS response_bytes,
                         SUM(COALESCE(tool_input_bytes,0))    AS input_bytes
                  FROM hook_events WHERE tool_name IS NOT NULL {hw}
                  GROUP BY tool_name ORDER BY SUM(COALESCE(tool_response_bytes,0)) DESC LIMIT 40"""
    hooks = q(sql["hooks"], sid_args)
    sql["ev"] = f"""SELECT event, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions,
                      MIN(captured_at) AS first_seen, MAX(captured_at) AS last_seen
               FROM hook_events {sid_where} GROUP BY event ORDER BY COUNT(*) DESC"""
    ev = q(sql["ev"], sid_args)
    sql["rec"] = "SELECT type AS record_type, n FROM record_types ORDER BY n DESC"
    rec = q(sql["rec"])

    if att.empty and ev.empty and rec.empty:
        return html.Div("Nothing captured yet. Run node tools/harvest.mjs.",
                        style={"color": MUTED})

    total_inj = int(att["occurrences"].sum()) if not att.empty else 0
    top = att.iloc[0]["kind"] if not att.empty else "n/a"
    hook_bytes = int(hooks["response_bytes"].sum()) if not hooks.empty else 0

    fig = go.Figure()
    if not att.empty:
        head = att.head(14).iloc[::-1]
        fig.add_trace(go.Bar(x=head["occurrences"], y=head["kind"], orientation="h",
                             marker_color=ACCENT))
        fig.update_layout(title="Injected context by type, all sessions",
                          title_font=dict(color=TEXT, size=13),
                          xaxis_title="occurrences", yaxis_title="")

    # Numbers stay numbers. Comma-formatting them into strings here made the table sort
    # lexicographically (so 9 came after 80,000) and put quoted text in the CSV export, which is
    # the opposite of what an exportable evidence table is for.
    for frame in (att, hooks, ev, rec):
        for col in ("occurrences", "calls", "response_bytes", "input_bytes", "n", "sessions"):
            if col in frame.columns:
                frame[col] = frame[col].fillna(0).astype(int)

    return html.Div([
        html.Div([
            stat_card("Injected records", f"{total_inj:,}", sub="harness-inserted, not typed"),
            stat_card("Distinct kinds", f"{len(att):,}", sub=f"most common: {top}"),
            stat_card("Hook-observed bytes", fmt_bytes(hook_bytes),
                      sub="tool responses, desktop entrypoint"),
            # ev['n'] is an int column now. It was being un-comma'd back into a number here,
            # which only worked while the display formatting was mutating the frame in place.
            stat_card("Lifecycle events", f"{int(ev['n'].sum()):,}" if not ev.empty else "0",
                      sub="from the hook channel"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),

        html.Div("Context injected by the harness", style=SECTION_HEAD),
        html.Div("Not typed by you and not returned by a tool: reminders, hook output, skill and "
                 "agent listings, deferred-tool deltas. It occupies the same window as everything "
                 "else, and no native view shows it historically.",
                 style=SECTION_NOTE),
        dcc.Graph(figure=dark_fig(fig, 360), config={"displayModeBar": False}),
        evidence_block("Injected context by type", att, sql["att"], sid_args),

        evidence_block(
            "Tool response size, measured by the hooks", hooks, sql["hooks"], sid_args,
            note="Bytes, not tokens: the store has exact token counts per request and never per "
                 "tool call, so a ratio here would dress an estimate as a measurement."),

        html.Div([
            html.Div(evidence_block("Lifecycle events", ev, sql["ev"], sid_args, page_size=8),
                     style={"flex": "1.4"}),
            html.Div(evidence_block("Transcript record census", rec, sql["rec"], (), page_size=8),
                     style={"flex": "1"}),
        ], style={"display": "flex", "gap": "14px", "alignItems": "flex-start"}),
    ])


# ---- Probes ---------------------------------------------------------------
def probes_layout(session_id=None, scope="main", cohort=None):
    """What the control protocol returns, and what the app's own refresh loop costs.

    tools/probe.mjs asks a spawned Claude Code session for its context breakdown over the control
    protocol. That is the only route to a per-ITEM cost - which skills, which MCP tools - and the
    result has lived only in SQL. It is also a research result in its own right: the same probe on
    a different build returns a different vocabulary, which is why the rows are shown per probe
    rather than merged into one number.
    """
    probes = q("""SELECT id, ts, ok, model, total_tokens, percentage,
                         auto_compact_threshold, is_auto_compact_enabled, error
                  FROM probes ORDER BY id""")
    details = q("""SELECT probe_id, kind, COUNT(*) AS items, SUM(COALESCE(tokens,0)) AS tokens
                   FROM probe_details GROUP BY probe_id, kind
                   ORDER BY probe_id, SUM(COALESCE(tokens,0)) DESC""")
    named = q("""SELECT probe_id, kind, name, COALESCE(tokens,0) AS tokens
                 FROM probe_details WHERE COALESCE(tokens,0) > 0
                 ORDER BY tokens DESC LIMIT 60""")
    runs = q("""SELECT COUNT(*) AS runs,
                       SUM(CASE WHEN files_read = 0 THEN 1 ELSE 0 END) AS empty_runs,
                       SUM(COALESCE(files_read,0)) AS files_read,
                       ROUND(AVG(COALESCE(ms,0)), 1) AS avg_ms,
                       MAX(ts) AS last_run
                FROM harvest_runs""")

    if probes.empty:
        body = [html.Div("No probe has been run against this store.", style={"color": MUTED}),
                html.Pre("node tools/probe.mjs", style=CODE_BLOCK)]
    else:
        body = [
            html.Div("Probe runs", style=SECTION_HEAD),
            html.Div("Each row is one spawned session answering the control protocol. A spawned "
                     "CLI session is NOT configured like the desktop app, so these numbers "
                     "describe the probe, not your live work. That difference is the finding.",
                     style=SECTION_NOTE),
            dash_table.DataTable(
                columns=[{"name": c, "id": c} for c in
                         ["id", "ts", "ok", "model", "total_tokens", "auto_compact_threshold"]],
                data=probes.astype(object).where(probes.notna(), "").to_dict("records"),
                **TABLE_STYLE),
            html.Div("Per-category items and cost", style=SECTION_HEAD),
            html.Div("A count with zero tokens means the channel named the items but priced none "
                     "of them, which is exactly what makes the per-item cost unrecoverable from "
                     "this route alone.", style=SECTION_NOTE),
            dash_table.DataTable(
                columns=[{"name": c, "id": c} for c in ["probe_id", "kind", "items", "tokens"]],
                data=details.to_dict("records"), page_size=12,
                style_table={"overflowX": "auto"}, **TABLE_STYLE),
        ]
        if not named.empty:
            body += [
                html.Div("The items that carry a price", style=SECTION_HEAD),
                dash_table.DataTable(
                    columns=[{"name": c, "id": c} for c in ["probe_id", "kind", "name", "tokens"]],
                    data=named.to_dict("records"), page_size=12,
                    style_table={"overflowX": "auto"}, **TABLE_STYLE),
            ]

    r = runs.iloc[0] if not runs.empty else None
    cards = []
    if r is not None and r["runs"]:
        empty_pct = 100.0 * int(r["empty_runs"] or 0) / int(r["runs"])
        cards = [
            stat_card("Harvest runs", f"{int(r['runs']):,}", sub="incremental, all time"),
            stat_card("Read nothing", f"{empty_pct:.0f}%",
                      color=WARN if empty_pct > 40 else TEXT,
                      sub=f"{int(r['empty_runs'] or 0):,} runs found no new bytes"),
            stat_card("Files read", f"{int(r['files_read'] or 0):,}", sub="across all runs"),
            stat_card("Avg duration", f"{float(r['avg_ms'] or 0):.0f} ms", sub="per run"),
        ]

    return html.Div([
        html.Div(cards, style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
        html.Div("The refresh loop, measured", style=SECTION_HEAD),
        html.Div("Most harvests find nothing, because the dashboard polls on a timer while the "
                 "hooks already harvest on SessionEnd and UserPromptSubmit. A high percentage "
                 "here is not an error, it is the cost of the tick being shorter than the work.",
                 style=SECTION_NOTE),
    ] + body)


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
    """Populate the selector once, then leave it alone.

    Built here rather than at import so the first paint is not blocked by the query, and guarded
    so a 5s tick does not rebuild a 300-row option list forever.
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


def band_zones(fig, segs, ts_list, n_turns):
    """Shade warn and compact zones per model segment, so headroom is read rather than computed.

    Per segment, not flat across the session: the window belongs to the model in use, and a session
    that switched models has more than one. The dashed compact line was already drawn this way; a
    flat band would contradict it for every segment but one.

    Drawn below the traces, so a line never disappears into its own background.
    """
    drawn = 0
    for seg in segs:
        window = seg.get("window")
        if not window or window not in THRESHOLDS:
            continue
        t = THRESHOLDS[window]
        x0 = next((i + 1 for i, v in enumerate(ts_list) if v and v >= seg["startTs"]), 1)
        x1 = next((i + 1 for i in range(len(ts_list) - 1, -1, -1)
                   if ts_list[i] and ts_list[i] <= seg["endTs"]), n_turns)
        x1 = max(x1, x0)
        for y0, y1, colour, opacity in (
                (t["warn"], t["compact"], WARN, 0.10),
                (t["compact"], t["blocked"], DANGER, 0.10),
                (t["blocked"], window, DANGER, 0.20)):
            fig.add_shape(type="rect", x0=x0, x1=x1, y0=y0, y1=y1, xref="x", yref="y",
                          fillcolor=colour, opacity=opacity, line_width=0, layer="below")
        drawn += 1
    return drawn


def budget_line(fig, segs, ts_list, n_turns, budget_pct, latest):
    """A target budget, as a share of each segment's window, and the headroom left against it.

    A percentage rather than a token count, because the windows in this store differ by a factor of
    five and a single absolute number would be meaningless on most of them.
    """
    if not budget_pct:
        return None
    headroom = None
    for seg in segs:
        window = seg.get("window")
        if not window:
            continue
        target = window * budget_pct / 100.0
        x0 = next((i + 1 for i, v in enumerate(ts_list) if v and v >= seg["startTs"]), 1)
        x1 = next((i + 1 for i in range(len(ts_list) - 1, -1, -1)
                   if ts_list[i] and ts_list[i] <= seg["endTs"]), n_turns)
        fig.add_shape(type="line", x0=x0, x1=max(x1, x0), y0=target, y1=target,
                      line=dict(color=GOOD, width=1.5, dash="dot"))
        headroom = target - latest
    if headroom is not None:
        fig.add_annotation(
            x=n_turns, y=headroom + latest, xanchor="right", yanchor="bottom", showarrow=False,
            text=f"budget {budget_pct}% | {fmt_tokens(abs(headroom))} "
                 f"{'left' if headroom >= 0 else 'OVER'}",
            font=dict(color=GOOD if headroom >= 0 else DANGER, size=10, family=MONO))
    return headroom






def session_view(session_id, scope="main", budget_pct=None, mark=None, with_cards=True):
    """Everything the Session tab shows, for ONE selection.

    This was a callback bound to a picker that lived on the tab. The picker is now in the header
    and governs every tab, so this is a plain function the renderer calls: the tab has no state of
    its own to disagree with the header about.
    """
    if not session_id:
        return empty_fig("Select a session in the header"), ""
    include_sidechain = (scope == "all")
    turns = session_turns(session_id, include_sidechain)
    if turns.empty:
        return (empty_fig("No turns recorded for that session"
                          + ("" if include_sidechain else " on the main thread")), "")
    comps = session_compactions(session_id)

    x = list(range(1, len(turns) + 1))
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, y=turns["total_resident"], mode="lines", name="resident",
        line=dict(color=ACCENT, width=2),
        hovertemplate="turn %{x}<br>%{y:,.0f} resident<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=turns["cache_read_input_tokens"], mode="lines", name="cache read",
        line=dict(color=VIOLET, width=1, dash="dot"),
        hovertemplate="turn %{x}<br>%{y:,.0f} cache read<extra></extra>",
    ))
    # Cumulative churn on its own axis. Per-call cache read tracks the resident line and says
    # little on its own; the running total is what shows the session re-paying for the same
    # context, turn after turn, and it is the largest single cost in this store.
    fig.add_trace(go.Scatter(
        x=x, y=turns["cache_read_input_tokens"].fillna(0).cumsum(), mode="lines",
        name="cache read, cumulative", yaxis="y2",
        line=dict(color=WARN, width=1.5),
        hovertemplate="turn %{x}<br>%{y:,.0f} re-read so far<extra></extra>",
    ))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False,
                                  title="cumulative cache read",
                                  title_font=dict(color=WARN, size=10),
                                  tickfont=dict(color=WARN, size=9)))

    peak = float(turns["total_resident"].max())

    # Threshold lines are drawn PER MODEL SEGMENT, not flat across the session. The window
    # belongs to the model in use, so a session that switched models has more than one, and a
    # flat line would be wrong for every segment but one.
    ts_list = list(turns["ts"])
    try:
        seg_info = segments_for(session_id)
        segs = seg_info.get("segments", [])
    except Exception as exc:
        segs = []
        fig.add_annotation(text=f"segmentation unavailable: {exc}", showarrow=False,
                           xref="paper", yref="paper", x=0.5, y=1.08,
                           font=dict(color=DANGER, size=10, family=MONO))

    unresolved = 0
    for s in segs:
        if not s.get("window"):
            unresolved += 1
            continue
        thr = THRESHOLDS[s["window"]]["compact"]
        x0 = next((i + 1 for i, v in enumerate(ts_list) if v and v >= s["startTs"]), 1)
        x1 = next((i + 1 for i in range(len(ts_list) - 1, -1, -1)
                   if ts_list[i] and ts_list[i] <= s["endTs"]), len(ts_list))
        fig.add_shape(type="line", x0=x0, x1=max(x1, x0), y0=thr, y1=thr,
                      line=dict(color=WARN, width=1.5, dash="dash"))
        fig.add_annotation(
            x=x0, y=thr, xanchor="left", yanchor="bottom", showarrow=False,
            text=f"{s['model']} | compact at {fmt_tokens(thr)} ({fmt_tokens(s['window'])} window)",
            font=dict(color=WARN, size=9, family=MONO))

    # Compaction boundaries, placed at the nearest turn by timestamp. Label them only when
    # there are few: a session with 46 compactions printed the word 46 times into one stack.
    ts = list(turns["ts"])
    label_them = len(comps) <= 6
    for c in comps.itertuples():
        pos = sum(1 for v in ts if v and c.ts and v <= c.ts)
        pos = max(1, min(len(x), pos))
        fig.add_vline(x=pos, line=dict(color=DANGER, width=1.5))
        if label_them:
            fig.add_annotation(x=pos, y=peak, text="compaction", showarrow=False,
                               font=dict(color=DANGER, size=10, family=MONO), textangle=-90,
                               xanchor="right", yanchor="top")

    comp_note = f" | {len(comps)} compactions (red)" if len(comps) else ""
    if len(segs) > 1:
        comp_note += f" | {len(segs)} model segments"
    if unresolved:
        comp_note += f" | {unresolved} segment(s) with an undetermined window"
    fig.update_layout(title=f"{session_id[:8]} | {len(turns)} turns | "
                            f"peak {fmt_tokens(peak)}{comp_note}",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="turn", yaxis_title="tokens")

    # Dated calibrations, so a step in the fixed overhead reads as an event rather than a glitch.
    _ts_index = {v: i + 1 for i, v in enumerate(ts_list) if v}

    _stamps = [str(v) for v in ts_list if v]
    _first, _last = (min(_stamps), max(_stamps)) if _stamps else (None, None)

    def _x_for(ts):
        """Turn index for a calibration, or None when it did not happen during this session.

        The earlier version only guarded ONE end. A calibration LATER than the last turn matched
        nothing and was correctly dropped, but one EARLIER than the first turn matched every turn,
        so the nearest-at-or-after rule returned turn 1 and the marker was drawn clamped to the
        left edge, implying a calibration happened at the start of a session that predated it. A
        session with 44 turns over 17 minutes and no calibration inside it drew all three stacked
        at x=1, and the count grew with every session, since it equalled the number of baselines
        recorded before that session began.
        """
        if not _stamps or ts < _first or ts > _last:
            return None
        later = [i + 1 for i, v in enumerate(ts_list) if v and str(v) >= ts]
        return later[0] if later else None

    baseline_marks(fig, _x_for, ts_list)

    # Zones and budget go on after the threshold lines so they share the same segment geometry.
    band_zones(fig, segs, ts_list, len(x))
    _latest_for_budget = int(turns["total_resident"].iloc[-1]) if len(turns) else 0
    budget_line(fig, segs, ts_list, len(x), budget_pct, _latest_for_budget)

    # The scrubber's two handles. A and B are turn numbers, and the diff panel below the chart
    # reports what happened between them, so the marks are what tie the two together.
    if mark:
        for label, pos in zip(("A", "B"), mark, strict=False):   # mark may hold fewer than two
            if pos and 1 <= pos <= len(x):
                fig.add_vline(x=pos, line=dict(color=ACCENT, width=1, dash="dot"))
                fig.add_annotation(x=pos, y=0, text=label, showarrow=False, yanchor="top",
                                   font=dict(color=ACCENT, size=11, family=MONO))

    # Mark the turns that survived a compaction. Unmatched survivor uuids are user or attachment
    # records the store does not hold, so the marked set is a lower bound and is labelled as one.
    surv = session_survivors(session_id)
    matched = surv[surv["ts"].notna()] if not surv.empty else surv
    if not matched.empty:
        idx_by_ts = {v: i + 1 for i, v in enumerate(ts_list)}
        xs, ys = [], []
        for r in matched.itertuples():
            if r.ts in idx_by_ts:
                xs.append(idx_by_ts[r.ts])
                ys.append(r.total_resident)
        if xs:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="markers", name=f"survived ({len(xs)} matched)",
                marker=dict(color=GOOD, size=7, symbol="diamond",
                            line=dict(color=BG, width=1)),
                hovertemplate="turn %{x}<br>survived a compaction<extra></extra>",
            ))

    # The slider callback wants the figure and nothing else. Building the cards anyway meant two
    # tables, one of them 400 rows, constructed and dropped on every drag, plus the queries behind
    # them. Found by the table audit, which reported them as built but never walked.
    if not with_cards:
        return dark_fig(fig, 460), None

    total_out = int(turns["output_tokens"].sum())
    think = int(turns["thinking_tokens"].sum())

    # The session's LATEST reading leads, because that is the number the context bar shows and the
    # one a reader compares against. Peak stays - it is the right number for a finished session -
    # but it is a high-water mark, and leading with it is what made the page disagree with the
    # desktop even when both were correct.
    latest = int(turns["total_resident"].iloc[-1]) if len(turns) else 0
    win, conf = session_window(session_id)
    latest_sub = f"{latest / win * 100:.0f}% of {fmt_tokens(win)}" if win else "window unresolved"

    # Cache-read churn. Resident says how big the context IS; this says how many times it was
    # PAID FOR. Every request re-bills the whole resident window as a cache read, so a long
    # session pays for the same tokens once per turn that follows them. The multiple is the
    # honest way to say it: total cache read divided by the largest window ever resident.
    # From api_calls, NOT from the turn rows above. A streamed assistant message is written as
    # several turn rows sharing one request id and carrying the same usage, so summing the frame
    # this chart is drawn from overcounted this session's re-reads by 1.96x, 852M against 434M.
    # That is the exact defect the api_calls view exists for, and it put two different figures for
    # one quantity on one screen: the header said 682M while this card said 852M.
    cw, cargs = scoped(session_id, scope)
    cdf = q(f"""SELECT SUM(COALESCE(cache_read_input_tokens,0)) AS churn,
                       MAX(total_resident) AS peak
                FROM api_calls WHERE 1=1 {cw}""", cargs)
    cache_total = int(cdf.iloc[0]["churn"] or 0) if not cdf.empty else 0
    churn_peak = int(cdf.iloc[0]["peak"] or 0) if not cdf.empty else 0
    rebill = (cache_total / churn_peak) if churn_peak else 0.0

    cards = html.Div([
        stat_card("current", fmt_tokens(latest), color=ACCENT, sub=latest_sub),
        stat_card("peak resident", fmt_tokens(peak), color=VIOLET, sub="high-water mark"),
        stat_card("cache re-reads", fmt_tokens(cache_total), color=WARN,
                  sub=f"{rebill:,.0f}x the peak window, re-billed"),
        stat_card("rows", f"{len(turns):,}",
                  sub="transcript rows, subagents included" if include_sidechain
                      else "transcript rows, main thread"),
        stat_card("output", fmt_tokens(total_out), sub=f"{fmt_tokens(think)} thinking"),
        stat_card("compactions", str(len(comps)), color=DANGER if len(comps) else TEXT),
        stat_card("models", ", ".join(real_models(turns["model"])[:2]) or "-"),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})

    if not comps.empty:
        show = comps.copy()
        show["ts"] = show["ts"].astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
        cols = ["ts", "trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens",
                "duration_ms", "version"]
        cards = html.Div([cards, html.Div(style={"height": "14px"}),
                          dash_table.DataTable(
                              columns=numeric_columns(cols, {"pre_tokens", "post_tokens",
                                                             "cumulative_dropped_tokens",
                                                             "duration_ms"}),
                              data=show[cols].to_dict("records"), **TABLE_STYLE)])

    # What was actually said. The chart shows the window filling; this shows what filled it.
    msgs = session_messages(session_id)
    if not msgs.empty:
        m = msgs.copy()
        m["ts"] = m["ts"].astype(str).str.slice(11, 19)
        # The query is capped, so len(m) is how many are shown, not how many exist. Saying
        # "400 messages" when 400 is the LIMIT reports the cap as if it were a measurement.
        total_msgs = int(q("SELECT COUNT(*) AS n FROM messages WHERE session_id = ?",
                           (session_id,)).iloc[0]["n"])
        note = (f"{total_msgs:,} messages in this session, showing the first {len(m):,}"
                if total_msgs > len(m) else f"{total_msgs:,} messages in this session")
        cards = html.Div([
            cards,
            html.Div(f"{note}, oldest first. Click a row to read it in full.",
                     style={"color": MUTED, "fontSize": "11.5px", "margin": "16px 0 6px 0"}),
            dash_table.DataTable(
                id="tbl-messages",
                columns=numeric_columns(["ts", "role", "type", "chars", "preview"], {"chars"}),
                data=m[["ts", "role", "type", "chars", "preview", "uuid"]].to_dict("records"),
                page_size=12, sort_action="native", filter_action="native",
                style_table={"overflowX": "auto"},
                style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
                **TABLE_STYLE,
            ),
            html.Div(id="message-detail", style={"marginTop": "12px"}),
        ])
    return dark_fig(fig, 460), cards


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

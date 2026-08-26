#!/usr/bin/env python
"""
Context capture explorer - a local Dash app over data/context.db.

Shows what the harvester collected: per-session context growth, every compaction on record
with the mirror's predicted trigger, and a live threshold calculator.

The window math is NOT reimplemented here. It is read from tools/mirror-core.mjs at startup
and computed by tools/mirror.mjs on demand, so the app and the validated JS cannot drift apart.

Run:  python app.py          then open http://127.0.0.1:8056
Quit: the red button in the header, or POST/GET /__shutdown__
"""

import html as _html
import json
import os
import sqlite3
import subprocess
import threading as _threading
import time as _time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, dash_table, dcc, html, no_update
from flask import request as _flask_request

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "context.db"
PORT = int(os.environ.get("C4X_PORT", "8056"))
DEBUG = os.environ.get("C4X_DEBUG") == "1"  # off by default: debug rotates JS chunk hashes

# ---------------------------------------------------------------------------
# Dark palette. Every surface sets its colors explicitly; nothing inherits.
# ---------------------------------------------------------------------------
BG = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#1f6feb"
GOOD = "#3fb950"
WARN = "#d29922"
DANGER = "#f85149"
VIOLET = "#a371f7"
MONO = "ui-monospace, SFMono-Regular, Consolas, monospace"

# Form controls get an explicit background AND text color, or they render dark on dark.
FIELD = {"backgroundColor": "#ffffff", "color": "#10141a", "border": f"1px solid {BORDER}"}

TABLE_STYLE = dict(
    style_cell={
        "backgroundColor": PANEL, "color": TEXT, "border": f"1px solid {BORDER}",
        "fontFamily": MONO, "fontSize": "12px", "padding": "6px 10px", "textAlign": "left",
    },
    style_header={
        "backgroundColor": "#21262d", "color": TEXT, "fontWeight": "700",
        "border": f"1px solid {BORDER}", "fontFamily": MONO, "fontSize": "11.5px",
    },
    style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#12171e"}],
)


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


# ---------------------------------------------------------------------------
# Single source of truth for the math: read it out of the JS module.
# ---------------------------------------------------------------------------
def _node_json(script: str):
    """Run a node snippet that prints JSON, return the parsed value."""
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"node produced no stdout. stderr: {proc.stderr.strip()[:400]}")
    return json.loads(out)


def load_math():
    """Constants and thresholds, straight from tools/mirror-core.mjs.

    A Windows absolute path is not a legal import specifier, so the path is converted to a
    file:// URL before import.
    """
    core = (ROOT / "tools" / "mirror-core.mjs").as_uri()
    script = (
        f"import({json.dumps(core)}).then(m => {{"
        "  const ws = [200000, 500000, 967000, 1000000];"
        "  console.log(JSON.stringify({"
        "    K: m.K,"
        "    thresholds: ws.map(w => ({"
        "      window: w,"
        "      compact: m.reportedAutoCompactThreshold(w),"
        "      warn: m.reportedAutoCompactThreshold(w) - m.K.WARN_OFFSET,"
        "      blocked: w - m.K.COMPACT_BUFFER"
        "    }))"
        "  }));"
        "}).catch(e => { console.error(e.message); process.exit(1); });"
    )
    return _node_json(script)


def _node_json_argv(args, timeout=120):
    """Run a node script that prints JSON on stdout, return the parsed value."""
    proc = subprocess.run(["node", *args], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"node {args[0]} exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    if not proc.stdout.strip():
        raise RuntimeError(f"node {args[0]} produced no stdout. stderr: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def predict(tokens: int, window: int):
    """Ask tools/mirror.mjs, so the answer is the validated implementation's answer."""
    proc = subprocess.run(
        ["node", str(ROOT / "tools" / "mirror.mjs"),
         "--predict", str(int(tokens)), "--window", str(int(window))],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mirror.mjs exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Data access. Read-only; the app never writes to the store.
# ---------------------------------------------------------------------------
def q(sql: str, params=()) -> pd.DataFrame:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No store at {DB_PATH}. Run `node tools/harvest.mjs` first."
        )
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def overview_stats() -> dict:
    row = q("""
        SELECT (SELECT COUNT(*) FROM sessions)                       AS sessions,
               (SELECT COUNT(*) FROM turns)                          AS turns,
               (SELECT COUNT(*) FROM compactions)                    AS compactions,
               (SELECT SUM(summary_uuid IS NULL) FROM compactions)   AS unpaired,
               (SELECT COUNT(*) FROM files)                          AS files,
               (SELECT SUM(bytes_read) FROM files)                   AS bytes,
               (SELECT SUM(output_tokens) FROM api_calls)            AS out_tokens,
               (SELECT MAX(total_resident) FROM turns)               AS peak
    """).iloc[0]
    return row.to_dict()


def session_options() -> list:
    df = q("""
        SELECT t.session_id,
               COALESCE(s.project_slug, '?')                 AS project,
               COUNT(*)                                      AS turns,
               MAX(t.total_resident)                         AS peak,
               MIN(t.ts)                                     AS started,
               (SELECT COUNT(*) FROM compactions c WHERE c.session_id = t.session_id) AS compactions
        FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id
        GROUP BY t.session_id
        HAVING COUNT(*) >= 5
        ORDER BY peak DESC
    """)
    opts = []
    for r in df.itertuples():
        started = (r.started or "")[:10]
        mark = f" | {r.compactions} compaction(s)" if r.compactions else ""
        opts.append({
            "label": f"{r.project} | {started} | {r.turns} turns | peak {r.peak/1000:.0f}k{mark}",
            "value": r.session_id,
        })
    return opts


def session_turns(session_id: str) -> pd.DataFrame:
    return q("""
        SELECT uuid, ts, model, input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
               output_tokens, thinking_tokens, total_resident, is_sidechain
        FROM turns WHERE session_id = ? ORDER BY ts
    """, (session_id,))


def session_survivors(session_id: str) -> pd.DataFrame:
    """Turns that lived through a compaction in this session.

    Only assistant turns can be matched, since those are the only uuids the store holds. The
    survivor set also names user and attachment records, which stay unmatched by design.
    """
    return q("""
        SELECT v.compaction_uuid, v.kind, v.uuid, t.ts, t.total_resident
        FROM compaction_survivors v
        JOIN compactions c ON c.uuid = v.compaction_uuid
        LEFT JOIN turns t ON t.uuid = v.uuid
        WHERE c.session_id = ?
    """, (session_id,))


def session_compactions(session_id: str) -> pd.DataFrame:
    return q("""
        SELECT ts, trigger, pre_tokens, post_tokens, cumulative_dropped_tokens,
               duration_ms, version, summary_chars
        FROM compactions WHERE session_id = ? ORDER BY ts
    """, (session_id,))


def all_compactions() -> pd.DataFrame:
    return q("""
        SELECT c.uuid, c.ts, COALESCE(s.project_slug,'?') AS project, c.trigger, c.version,
               c.pre_tokens, c.post_tokens, c.cumulative_dropped_tokens AS dropped,
               c.duration_ms,
               (SELECT t.model FROM turns t
                 WHERE t.session_id = c.session_id AND t.ts <= c.ts
                 ORDER BY t.ts DESC LIMIT 1) AS model,
               (SELECT COUNT(*) FROM compaction_survivors v
                 WHERE v.compaction_uuid = c.uuid) AS survivors
        FROM compactions c LEFT JOIN sessions s ON s.session_id = c.session_id
        WHERE c.pre_tokens IS NOT NULL
        ORDER BY c.pre_tokens DESC
    """)


def load_compaction_windows():
    """Window per compaction, resolved by model segment in one node pass.

    The window is a property of the model in use, not of the session, so a session that switched
    models has more than one. Resolved once at startup rather than per row.
    """
    return _node_json_argv([str(ROOT / "tools" / "segments.mjs"), "--windows-for-compactions"])


def segments_for(session_id: str):
    return _node_json_argv([str(ROOT / "tools" / "segments.mjs"), "--session", session_id])


MATH = load_math()
THRESHOLDS = {t["window"]: t for t in MATH["thresholds"]}
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
# Presentation helpers
# ---------------------------------------------------------------------------
def fmt_tokens(n) -> str:
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "-"
    n = float(n)
    if n >= 1e6:
        return f"{n/1e6:.2f}M".replace(".00M", "M")
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return f"{n:.0f}"


def stat_card(label: str, value: str, color: str = TEXT, sub: str = "") -> html.Div:
    return html.Div(
        [
            html.Div(label, style={"color": MUTED, "fontSize": "11px",
                                   "textTransform": "uppercase", "letterSpacing": "0.06em"}),
            html.Div(value, style={"color": color, "fontSize": "26px",
                                   "fontWeight": 700, "fontFamily": MONO, "marginTop": "4px"}),
            html.Div(sub, style={"color": MUTED, "fontSize": "11px", "marginTop": "2px"}),
        ],
        style={"background": PANEL, "border": f"1px solid {BORDER}", "borderRadius": "8px",
               "padding": "14px 16px", "minWidth": "150px", "flex": "1"},
    )


def dark_fig(fig: go.Figure, height: int = 420) -> go.Figure:
    """Plotly does not follow the page theme. Every color is set here."""
    fig.update_layout(
        paper_bgcolor=BG, plot_bgcolor=PANEL, font=dict(color=TEXT, family=MONO, size=11),
        height=height, margin=dict(l=60, r=24, t=40, b=48),
        legend=dict(bgcolor=PANEL, bordercolor=BORDER, borderwidth=1, font=dict(color=TEXT)),
        hoverlabel=dict(bgcolor=PANEL, font=dict(color=TEXT, family=MONO), bordercolor=BORDER),
    )
    fig.update_xaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER,
                     tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    fig.update_yaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER,
                     tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    return fig


def empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=MUTED, size=13, family=MONO))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return dark_fig(fig, height=300)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Dash(__name__, title="Context capture", update_title=None)
server = app.server

TAB_IDS = ["tab-overview", "tab-session", "tab-compactions", "tab-mirror", "tab-waste"]
TAB_LABELS = ["Overview", "Session", "Compactions", "Mirror", "Waste"]


def tab_button(i: int, label: str, active: bool) -> html.Button:
    return html.Button(
        label, id=f"btn-{TAB_IDS[i]}", n_clicks=0,
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
            ],
        ),
        html.Div(
            [
                html.Span(f"compact at window - {MATH['K']['MAX_OUTPUT_RESERVE']} - "
                          f"{MATH['K']['AUTOCOMPACT_BUFFER']}",
                          style={"color": MUTED, "fontSize": "11px", "fontFamily": MONO}),
                html.Button(
                    "⏻ Quit", id="btn-quit-app", n_clicks=0,
                    title="Stop the Dash server and exit the Python process",
                    style={"background": "#f8514922", "color": DANGER,
                           "border": f"1px solid #f8514966", "borderRadius": "4px",
                           "padding": "4px 10px", "marginLeft": "14px", "fontSize": "11.5px",
                           "fontWeight": 700, "cursor": "pointer", "fontFamily": MONO},
                ),
            ],
            style={"display": "flex", "alignItems": "center"},
        ),
    ],
    style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
           "padding": "12px 20px", "borderBottom": f"1px solid {BORDER}", "background": PANEL},
)


# ---- Overview -------------------------------------------------------------
def overview_layout():
    s = overview_stats()
    cards = html.Div(
        [
            stat_card("sessions", f"{int(s['sessions']):,}"),
            stat_card("turns", f"{int(s['turns']):,}", sub="deduped by uuid"),
            stat_card("compactions", f"{int(s['compactions']):,}",
                      color=WARN, sub=f"{int(s['unpaired'] or 0)} unpaired"),
            stat_card("transcripts", f"{int(s['files']):,}",
                      sub=f"{(s['bytes'] or 0)/1073741824:.2f} GB"),
            stat_card("peak resident", fmt_tokens(s["peak"]), color=VIOLET),
            stat_card("output tokens", fmt_tokens(s["out_tokens"]), sub="all time"),
        ],
        style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"},
    )

    top = q("""
        -- api_calls, NOT turns. A streamed assistant message is written as several transcript
        -- rows carrying the same requestId and the same usage, about 2.3 per call here, so
        -- summing turns inflated every figure on this chart by roughly 2x. Measured against
        -- ccusage on the same transcripts: turns gave 56.90 B, the deduped view gives 27.12 B.
        SELECT COALESCE(s.project_slug,'?') AS project, COUNT(*) AS turns,
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
def session_layout():
    return html.Div([
        html.Div([
            html.Span("Session", style={"color": MUTED, "fontSize": "12px",
                                        "marginRight": "10px"}),
            dcc.Dropdown(
                id="dd-session", options=session_options(), value=None,
                placeholder="Search by project, date, size...",
                style={"width": "640px", **FIELD},
                className="c4x-dd",
            ),
        ], style={"display": "flex", "alignItems": "center", "marginBottom": "14px"}),
        dcc.Graph(id="fig-session", figure=empty_fig("Pick a session above"),
                  config={"displayModeBar": False}),
        html.Div(id="session-summary", style={"marginTop": "10px"}),
    ])


# ---- Compactions ----------------------------------------------------------
def compactions_layout():
    df = all_compactions()
    # Window comes from the model segment the compaction sits in. Where segmentation cannot
    # resolve one (no non-sidechain turns recorded around the event), fall back to fitting from
    # the token count alone and SAY SO in the confidence column rather than hiding the weaker
    # basis behind an identical-looking number.
    windows, confidences = [], []
    for uuid, pre in zip(df["uuid"], df["pre_tokens"]):
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
        text=[f"{p} | v{v} | {m}" for p, v, m in zip(df["project"], df["version"], df["model"])],
        hovertemplate="%{text}<br>pre %{x:,.0f}<br>overshoot %{y:,.0f}<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color=MUTED, width=1, dash="dash"))
    fig.update_layout(title=f"Overshoot past the predicted trigger ({len(df)} compactions, "
                            f"{neg} below threshold)",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="tokens at compaction", yaxis_title="tokens past threshold")

    show = df.copy()
    for c in ("pre_tokens", "post_tokens", "dropped", "threshold", "overshoot"):
        show[c] = show[c].map(fmt_tokens)
    show["ts"] = show["ts"].str.slice(0, 19).str.replace("T", " ", regex=False)
    show["fitted_window"] = show["fitted_window"].map(fmt_tokens)
    show["survivors"] = show["survivors"].map(lambda n: str(n) if n else "-")
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
            columns=[{"name": c, "id": c} for c in cols],
            data=show[cols].to_dict("records"),
            page_size=15, sort_action="native", filter_action="native",
            style_table={"overflowX": "auto"},
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            **TABLE_STYLE,
        ),
    ])


# ---- Mirror ---------------------------------------------------------------
def mirror_layout():
    rows = []
    for t in MATH["thresholds"]:
        rows.append({
            "window": fmt_tokens(t["window"]), "warn at": fmt_tokens(t["warn"]),
            "compact at": fmt_tokens(t["compact"]), "blocked at": fmt_tokens(t["blocked"]),
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
                columns=[{"name": c, "id": c} for c in
                         ["window", "warn at", "compact at", "blocked at"]],
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


def waste_layout():
    """Context paid for twice, or paid for and never used.

    Reads the same tool_calls table tools/waste.mjs reports from, so the tab and the CLI cannot
    disagree about what the store says. Numbers here are BYTES of tool result, not tokens: the
    store records exact API token counts per turn, but not per tool call, and inventing a
    tokens-per-byte ratio would dress an estimate up as a measurement.
    """
    dup = q(
        """SELECT session_id, target, COUNT(*) reads,
                  SUM(COALESCE(result_bytes,0)) bytes,
                  COUNT(DISTINCT input_sha1) variants
           FROM tool_calls
           WHERE tool_name IN ('Read','NotebookRead') AND target IS NOT NULL
           GROUP BY session_id, target HAVING reads >= 3
           ORDER BY reads DESC LIMIT 200"""
    )
    srv = q(
        """SELECT server_name AS server, COUNT(*) calls,
                  SUM(COALESCE(result_bytes,0)) bytes, MAX(ts) last_call
           FROM tool_calls WHERE server_name IS NOT NULL
           GROUP BY server_name ORDER BY calls ASC"""
    )
    tools = q(
        """SELECT tool_name AS tool, COUNT(*) calls,
                  SUM(COALESCE(result_bytes,0)) bytes,
                  SUM(COALESCE(is_error,0)) errors
           FROM tool_calls GROUP BY tool_name ORDER BY calls DESC LIMIT 40"""
    )

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
            stat_card("Re-read groups", f"{len(dup):,}", sub="same file, one session, 3+ reads"),
            stat_card("Re-reads beyond the first", f"{repeats:,}", color=DANGER if repeats else TEXT),
            stat_card("KB in the repeats", f"{repeat_bytes/1024:,.1f}", sub="tool result bytes"),
            stat_card("Tool calls recorded", f"{int(tools['calls'].sum()):,}" if not tools.empty else "0"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"}),

        html.Div("Files read repeatedly inside one session", style={"color": TEXT, "fontSize": "14px", "fontWeight": "600", "margin": "18px 0 4px"}),
        html.Div("Every re-read is re-billed on every later request in that session, so the cost "
                 "is the read multiplied by the turns that follow it.",
                 style={"color": MUTED, "fontSize": "12px", "marginBottom": "8px"}),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ["reads", "bytes", "variants", "session_id", "target"]],
            data=dup.to_dict("records"), page_size=15,
            style_table={"overflowX": "auto"}, **TABLE_STYLE),

        html.Div("MCP servers by invocation count", style={"color": TEXT, "fontSize": "14px", "fontWeight": "600", "margin": "18px 0 4px"}),
        html.Div("Invocation count alone is a PROXY for cost. The measured price of a server is "
                 "the sum of its tools' schema bytes, which tools/otel-ingest.mjs puts in the store "
                 "and tools/waste.mjs --servers reports. This tab shows invocations only; run the "
                 "CLI for measured schema cost.",
                 style={"color": MUTED, "fontSize": "12px", "marginBottom": "8px"}),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ["server", "calls", "bytes", "last_call"]],
            data=srv.to_dict("records"), page_size=10,
            style_table={"overflowX": "auto"}, **TABLE_STYLE),

        html.Div("Tool invocations", style={"color": TEXT, "fontSize": "14px", "fontWeight": "600", "margin": "18px 0 4px"}),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ["tool", "calls", "bytes", "errors"]],
            data=tools.to_dict("records"), page_size=12,
            style_table={"overflowX": "auto"}, **TABLE_STYLE),
    ])


# Every tab is rendered up front and toggled by display, so no interactive component is
# created inside a callback. That sidesteps the pattern-matched-id trap entirely.
app.layout = html.Div(
    [
        header,
        html.Div([tab_button(i, lbl, i == 0) for i, lbl in enumerate(TAB_LABELS)],
                 style={"display": "flex", "gap": "2px", "padding": "0 14px",
                        "borderBottom": f"1px solid {BORDER}", "background": BG}),
        dcc.Store(id="active-tab", data=0),
        html.Div(
            [
                html.Div(overview_layout(), id="pane-0"),
                html.Div(session_layout(), id="pane-1", style={"display": "none"}),
                html.Div(compactions_layout(), id="pane-2", style={"display": "none"}),
                html.Div(mirror_layout(), id="pane-3", style={"display": "none"}),
                html.Div(waste_layout(), id="pane-4", style={"display": "none"}),
            ],
            style={"padding": "20px"},
        ),
    ],
    style={"background": BG, "color": TEXT, "minHeight": "100vh",
           "fontFamily": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"},
)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
@callback(
    [Output(f"pane-{i}", "style") for i in range(5)]
    + [Output(f"btn-{t}", "style") for t in TAB_IDS]
    + [Output("active-tab", "data")],
    [Input(f"btn-{t}", "n_clicks") for t in TAB_IDS],
    State("active-tab", "data"),
    prevent_initial_call=True,
)
def _switch_tab(*args):
    from dash import ctx
    current = args[-1]
    which = ctx.triggered_id
    idx = TAB_IDS.index(which.replace("btn-", "")) if which else current
    panes = [{"display": "block"} if i == idx else {"display": "none"} for i in range(5)]
    tabs = [tab_style(i == idx) for i in range(5)]
    return panes + tabs + [idx]


@callback(
    Output("fig-session", "figure"),
    Output("session-summary", "children"),
    Input("dd-session", "value"),
    prevent_initial_call=True,
)
def _session_selected(session_id):
    if not session_id:
        return empty_fig("Pick a session above"), ""
    turns = session_turns(session_id)
    if turns.empty:
        return empty_fig("No turns recorded for that session"), ""
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

    total_out = int(turns["output_tokens"].sum())
    think = int(turns["thinking_tokens"].sum())
    cards = html.Div([
        stat_card("turns", f"{len(turns):,}"),
        stat_card("peak resident", fmt_tokens(peak), color=VIOLET),
        stat_card("output", fmt_tokens(total_out), sub=f"{fmt_tokens(think)} thinking"),
        stat_card("compactions", str(len(comps)), color=DANGER if len(comps) else TEXT),
        stat_card("models", ", ".join(sorted({m for m in turns["model"] if m})[:2]) or "-"),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})

    if not comps.empty:
        show = comps.copy()
        for c in ("pre_tokens", "post_tokens", "cumulative_dropped_tokens"):
            show[c] = show[c].map(fmt_tokens)
        show["ts"] = show["ts"].str.slice(0, 19).str.replace("T", " ", regex=False)
        cols = ["ts", "trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens",
                "duration_ms", "version"]
        cards = html.Div([cards, html.Div(style={"height": "14px"}),
                          dash_table.DataTable(
                              columns=[{"name": c, "id": c} for c in cols],
                              data=show[cols].to_dict("records"), **TABLE_STYLE)])
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


@callback(
    Output("btn-quit-app", "title"),
    Input("btn-quit-app", "n_clicks"),
    prevent_initial_call=True,
)
def _quit_clicked(n_clicks):
    if not n_clicks:
        return no_update
    _hardened_shutdown(f"Quit button clicked (n={n_clicks})")
    return "Server shutting down..."


@server.route("/__shutdown__", methods=["POST", "GET"])
def _shutdown_route():
    reason = (_flask_request.args.get("reason")
              or _flask_request.form.get("reason")
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

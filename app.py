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
import sqlite3
import subprocess
import threading as _threading
import time as _time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, callback, dash_table, dcc, html
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
    """Headline figures, each one tied to a stated population.

    These cards used to mix populations silently. `turns` counted every transcript row, which
    includes subagent work AND counts one streamed assistant message two to eight times, while
    `output tokens` came from the deduped `api_calls` view and the Breakdown tab charted only
    non-sidechain calls. Three different denominators on one dashboard, none of them labelled,
    and the raw count sat under the words "deduped by uuid" - true of harvest-time uuid dedup,
    and the exact misreading the README warns about under "Token numbers look about twice too
    high". So the API-call count leads now, and the transcript row count is shown beside it as
    what it is.

    The api_calls figures come from ONE pass. Each subquery against that view is a full GROUP BY
    over every turn, so asking it five separate questions would have cost five scans.
    """
    small = q("""
        SELECT (SELECT COUNT(*) FROM sessions)                     AS sessions,
               (SELECT COUNT(*) FROM turns)                        AS turn_rows,
               (SELECT COUNT(*) FROM compactions)                  AS compactions,
               (SELECT SUM(summary_uuid IS NULL) FROM compactions) AS unpaired,
               (SELECT COUNT(*) FROM files)                        AS files,
               (SELECT SUM(bytes_read) FROM files)                 AS bytes
    """).iloc[0].to_dict()
    calls = q("""
        SELECT COUNT(*)                                                     AS api_calls,
               SUM(CASE WHEN COALESCE(is_sidechain,0)=0 THEN 1 ELSE 0 END)   AS main_calls,
               SUM(COALESCE(output_tokens,0))                                AS out_tokens,
               SUM(COALESCE(cache_read_input_tokens,0))                      AS cache_read,
               SUM(COALESCE(input_tokens,0) + COALESCE(cache_creation_input_tokens,0)
                   + COALESCE(cache_read_input_tokens,0) + COALESCE(output_tokens,0)) AS billed,
               MAX(total_resident)                                           AS peak
        FROM api_calls
    """).iloc[0].to_dict()
    return {**small, **calls}


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


def session_turns(session_id: str, include_sidechain: bool = False) -> pd.DataFrame:
    """Turn records for one session.

    Sidechain is EXCLUDED by default, and that default is now stated on the page rather than
    applied silently. Subagent work is 70% of the API calls in this store, so a chart that
    quietly folded it in beside main-thread turns was answering a question nobody asked, while
    a chart that quietly dropped it looked like the whole session.
    """
    where = "" if include_sidechain else "AND COALESCE(is_sidechain,0) = 0"
    return q(f"""
        SELECT uuid, ts, model, input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
               output_tokens, thinking_tokens, total_resident, is_sidechain
        FROM turns WHERE session_id = ? {where} ORDER BY ts
    """, (session_id,))


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


def compaction_summary_text(compaction_uuid: str) -> pd.DataFrame:
    """The summary a compaction produced, as prose.

    Until the messages table existed this was only ever a character count, so the page could tell
    you 14,115 chars had replaced 981k tokens without showing you a word of it.
    """
    return q(
        """
        SELECT m.text, m.chars, m.ts
        FROM compactions c JOIN messages m ON m.uuid = c.summary_uuid
        WHERE c.uuid = ?
        """,
        (compaction_uuid,),
    )


def compaction_dropped(compaction_uuid: str, limit: int = 300) -> pd.DataFrame:
    """Messages from before a compaction that are absent from its survivor list.

    A LOWER BOUND in both directions, and labelled as one in the UI. Survivor uuids that the store
    holds no message for cannot be matched, and a message with no readable text was never stored,
    so this lists what can be shown to have gone rather than everything that went.
    """
    return q(
        """
        SELECT m.uuid, m.ts, m.role, m.type, m.chars,
               substr(replace(replace(m.text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM compactions c
        JOIN messages m ON m.session_id = c.session_id AND m.ts < c.ts
        WHERE c.uuid = ?
          AND m.uuid NOT IN (SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
          AND m.uuid <> COALESCE(c.summary_uuid, '')
        ORDER BY m.chars DESC
        LIMIT ?
        """,
        (compaction_uuid, limit),
    )


def compaction_dropped_count(compaction_uuid: str) -> int:
    """How many dropped messages EXIST, as opposed to how many the table shows.

    compaction_dropped() caps its result, and reporting the capped length as the count states the
    limit as though it were a finding.
    """
    df = q(
        """
        SELECT COUNT(*) AS n
        FROM compactions c
        JOIN messages m ON m.session_id = c.session_id AND m.ts < c.ts
        WHERE c.uuid = ?
          AND m.uuid NOT IN (SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
          AND m.uuid <> COALESCE(c.summary_uuid, '')
        """,
        (compaction_uuid,),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def session_messages(session_id: str, limit: int = 400) -> pd.DataFrame:
    return q(
        """
        SELECT uuid, ts, role, type, chars,
               substr(replace(replace(text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM messages WHERE session_id = ? ORDER BY ts LIMIT ?
        """,
        (session_id, limit),
    )


def message_text(uuid: str) -> pd.DataFrame:
    return q("SELECT text, chars, ts, role, type FROM messages WHERE uuid = ?", (uuid,))


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
        _harvest_state["error"] = None if proc.returncode == 0 else (proc.stderr or "").strip()[:200]
        _harvest_state["runs"] += 1
    except Exception as exc:                        # noqa: BLE001 - reported in the UI, not raised
        _harvest_state["error"] = str(exc)[:200]
    finally:
        _harvest_lock.release()


_window_cache: dict = {}


def session_window(session_id: str, ttl: float = 60.0):
    """The window a session is actually running, resolved from evidence and cached.

    A model-name lookup is not sufficient: claude-opus-5 is listed in SMALL_WINDOW_MODELS, yet
    this build demonstrably runs it at 1M, and the proof is the session's own compaction and peak.
    tools/segments.mjs already performs that reasoning, so it is asked rather than reimplemented -
    once per session per ttl, to keep a node spawn off the per-tick path.
    """
    hit = _window_cache.get(session_id)
    now = _time.time()
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    window, confidence = None, "unresolved"
    try:
        info = segments_for(session_id)
        segs = [s for s in info.get("segments", []) if s.get("window")]
        if segs:
            window = segs[-1]["window"]
            confidence = segs[-1].get("confidence") or "segment"
    except Exception:                               # noqa: BLE001 - unresolved is a valid answer
        pass
    _window_cache[session_id] = (now, window, confidence)
    return window, confidence


def live_context():
    """The newest API call in the store, expressed the way the desktop expresses it.

    Reads api_calls rather than turns: a streamed assistant message is several turn rows sharing
    one request id, so the newest turn row is not necessarily the newest call.
    """
    df = q(
        """
        SELECT session_id, ts, model, total_resident
        FROM api_calls
        WHERE total_resident IS NOT NULL AND COALESCE(is_sidechain, 0) = 0
        ORDER BY ts DESC LIMIT 1
        """
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


# Pseudo-models that appear in the transcript but are not models anyone ran. Printing <synthetic>
# beside claude-opus-5 in a MODELS card invites the reader to think they used two. segments.mjs
# already treats it as not-a-model-switch; this is the display half of the same rule.
SYNTHETIC_MODELS = {"<synthetic>", "synthetic", "", None}


def real_models(values) -> list:
    seen = []
    for m in values:
        if m in SYNTHETIC_MODELS or (isinstance(m, float) and pd.isna(m)):
            continue
        if m not in seen:
            seen.append(m)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def fmt_tokens(n) -> str:
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "-"
    n = float(n)
    # Billions became reachable once cache reads were totalled: this store has 31.4B of them, and
    # without this tier that rendered as "31417.43M", which is a number nobody can read at a glance.
    if n >= 1e9:
        return f"{n/1e9:.2f}B".replace(".00B", "B")
    if n >= 1e6:
        return f"{n/1e6:.2f}M".replace(".00M", "M")
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return f"{n:.0f}"


def fmt_bytes(n) -> str:
    """Bytes, said as bytes. Never converted to tokens.

    The store holds exact API token counts per request and no token count per tool call, so a
    bytes-to-tokens ratio here would dress an estimate as a measurement. The Waste tab already
    refuses that conversion for the same reason.
    """
    if n is None or (isinstance(n, float) and pd.isna(n)):
        return "-"
    n = float(n)
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n:.0f} B"


# The two styles every tab's prose already uses inline. Named once so a new tab cannot drift into
# its own heading size, which is how a dashboard stops looking like one product.
SECTION_HEAD = {"color": TEXT, "fontSize": "14px", "fontWeight": "600", "margin": "18px 0 4px"}
SECTION_NOTE = {"color": MUTED, "fontSize": "12px", "marginBottom": "8px", "maxWidth": "900px",
                "lineHeight": "1.55"}
CODE_BLOCK = {"background": PANEL, "border": f"1px solid {BORDER}", "borderRadius": "8px",
              "padding": "12px 14px", "color": TEXT, "fontFamily": MONO, "fontSize": "12px",
              "display": "inline-block"}


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
            ],
        ),
        html.Div(
            [
                # The live reading, in the one place that is visible from every tab. The constants
                # that used to sit here are documented on the Mirror tab, which is where someone
                # goes to read them; this is where someone goes to see where they stand.
                html.Div(id="live-context", children=context_bar(None)),
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


# ---- Overview -------------------------------------------------------------
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
            scope_radio("session-scope"),
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


def breakdown_layout():
    """Shell. The body re-renders when the sidechain scope changes.

    This tab charted only non-sidechain calls and never said so, which in this store means it was
    showing under a third of the activity. The population is now a stated choice.
    """
    return html.Div([
        html.Div([
            html.Span("Population", style={"color": MUTED, "fontSize": "12px"}),
            scope_radio("breakdown-scope"),
        ], style={"display": "flex", "alignItems": "center", "marginBottom": "10px"}),
        html.Div(breakdown_body(False), id="breakdown-body"),
    ])


def breakdown_body(include_sidechain: bool = False):
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
    scope_sql = "" if include_sidechain else "AND COALESCE(is_sidechain,0) = 0"
    turns = q(f"""SELECT ts, total_resident FROM api_calls
                  WHERE total_resident IS NOT NULL {scope_sql}
                  ORDER BY ts""")
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
                     "tokens": fmt_tokens(val), "percent": f"{pct:.1f}%",
                     "items": f"{items:,}" if items is not None else ""})

    # Deferred tools are listed by the tooltip with no percentage, because they are not resident:
    # a deferred tool costs nothing until it loads. They are shown here for the same reason the
    # tooltip shows them - 104.9k of MCP schema sitting one ToolSearch away is worth knowing about
    # - but they are never added to the bar, the percentages, or the fixed overhead.
    for col in deferred_cols:
        val = cell(b.get(col))
        if not val:
            continue
        rows.append({"category": labels.get(col, col), "tokens": fmt_tokens(val),
                     "percent": "not resident", "items": ""})

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
        merged["static_total"] = merged["static_total"].fillna(bl["static_total"].iloc[0]).astype(int)

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
            columns=[{"name": c, "id": c} for c in ["category", "tokens", "percent", "items"]],
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
    # What counts as a read, and how many make a re-read, come from waste.mjs. The query runs here
    # for speed, but the definition lives in one place: these were two literals under a docstring
    # asserting they could not disagree, which asserted it rather than ensuring it.
    spec, spec_err = tool_spec("waste.mjs", "--spec")
    if spec:
        read_tools, dup_min = spec["read_tools"], int(spec["duplicate_min"])
    else:
        read_tools, dup_min = [], 3
    placeholders = ",".join("?" for _ in read_tools)
    dup = q(
        f"""SELECT session_id, target, COUNT(*) reads,
                   SUM(COALESCE(result_bytes,0)) bytes,
                   COUNT(DISTINCT input_sha1) variants
            FROM tool_calls
            WHERE tool_name IN ({placeholders}) AND target IS NOT NULL
            GROUP BY session_id, target HAVING reads >= ?
            ORDER BY reads DESC LIMIT 200""",
        tuple(read_tools) + (dup_min,),
    ) if read_tools else pd.DataFrame()
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
            stat_card("Re-read groups", f"{len(dup):,}",
                      sub=(f"same file, one session, {dup_min}+ reads" if read_tools
                           else "UNAVAILABLE: read-tool spec unreadable")),
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


# ---- Sources --------------------------------------------------------------
def sources_layout():
    """What ENTERS the window, as opposed to what the window currently holds.

    The Breakdown tab answers "what is in there now" and the context tooltip answers it live. This
    answers the question neither can: what has been arriving, from where, over the whole history.
    Three tables carry it and nothing read them until now - `attachments` (context injected by the
    harness rather than typed by anyone), `hook_events` (the only per-tool byte accounting that
    exists on the desktop entrypoint, where the status line does not run), and `record_types` (a
    census of every record shape the transcripts contain).
    """
    att = q("""SELECT type AS kind, SUM(n) AS occurrences, COUNT(DISTINCT session_id) AS sessions
               FROM attachments GROUP BY type ORDER BY SUM(n) DESC""")
    hooks = q("""SELECT tool_name AS tool, COUNT(*) AS calls,
                        SUM(COALESCE(tool_response_bytes,0)) AS response_bytes,
                        SUM(COALESCE(tool_input_bytes,0))    AS input_bytes
                 FROM hook_events WHERE tool_name IS NOT NULL
                 GROUP BY tool_name ORDER BY SUM(COALESCE(tool_response_bytes,0)) DESC LIMIT 40""")
    ev = q("""SELECT event, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions,
                     MIN(captured_at) AS first_seen, MAX(captured_at) AS last_seen
              FROM hook_events GROUP BY event ORDER BY COUNT(*) DESC""")
    rec = q("SELECT type AS record_type, n FROM record_types ORDER BY n DESC")

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

    for frame in (att, hooks, ev, rec):
        for col in ("occurrences", "calls", "response_bytes", "input_bytes", "n", "sessions"):
            if col in frame.columns:
                frame[col] = frame[col].fillna(0).astype(int).map(lambda v: f"{v:,}")

    return html.Div([
        html.Div([
            stat_card("Injected records", f"{total_inj:,}", sub="harness-inserted, not typed"),
            stat_card("Distinct kinds", f"{len(att):,}", sub=f"most common: {top}"),
            stat_card("Hook-observed bytes", fmt_bytes(hook_bytes),
                      sub="tool responses, desktop entrypoint"),
            stat_card("Lifecycle events", f"{int(ev['n'].str.replace(',','').astype(int).sum()):,}"
                      if not ev.empty else "0", sub="from the hook channel"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),

        html.Div("Context injected by the harness", style=SECTION_HEAD),
        html.Div("Not typed by you and not returned by a tool: reminders, hook output, skill and "
                 "agent listings, deferred-tool deltas. It occupies the same window as everything "
                 "else, and no native view shows it historically.",
                 style=SECTION_NOTE),
        dcc.Graph(figure=dark_fig(fig, 360), config={"displayModeBar": False}),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ["kind", "occurrences", "sessions"]],
            data=att.to_dict("records"), page_size=12,
            style_table={"overflowX": "auto"}, **TABLE_STYLE),

        html.Div("Tool response size, measured by the hooks", style=SECTION_HEAD),
        html.Div("The transcripts carry tool results, but the hook channel is the only place that "
                 "records their size on the entrypoint where the status line never runs. Bytes, "
                 "not tokens: the store has exact token counts per request, never per tool call.",
                 style=SECTION_NOTE),
        dash_table.DataTable(
            columns=[{"name": c, "id": c} for c in ["tool", "calls", "response_bytes", "input_bytes"]],
            data=hooks.to_dict("records"), page_size=10,
            style_table={"overflowX": "auto"}, **TABLE_STYLE),

        html.Div("Lifecycle events, and the record census", style=SECTION_HEAD),
        html.Div([
            html.Div(dash_table.DataTable(
                columns=[{"name": c, "id": c} for c in ["event", "n", "sessions", "first_seen", "last_seen"]],
                data=ev.to_dict("records"), page_size=8,
                style_table={"overflowX": "auto"}, **TABLE_STYLE), style={"flex": "1.4"}),
            html.Div(dash_table.DataTable(
                columns=[{"name": c, "id": c} for c in ["record_type", "n"]],
                data=rec.to_dict("records"), page_size=8,
                style_table={"overflowX": "auto"}, **TABLE_STYLE), style={"flex": "1"}),
        ], style={"display": "flex", "gap": "14px", "alignItems": "flex-start"}),
    ])


# ---- Probes ---------------------------------------------------------------
def probes_layout():
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
                   FROM probe_details GROUP BY probe_id, kind ORDER BY probe_id, SUM(COALESCE(tokens,0)) DESC""")
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
    ("tab-overview", "Overview", overview_layout),
    ("tab-session", "Session", session_layout),
    ("tab-compactions", "Compactions", compactions_layout),
    ("tab-breakdown", "Breakdown", breakdown_layout),
    ("tab-sources", "Sources", sources_layout),
    ("tab-probes", "Probes", probes_layout),
    ("tab-mirror", "Mirror", mirror_layout),
    ("tab-waste", "Waste", waste_layout),
]
TAB_IDS = [t[0] for t in TABS]

# Every tab is rendered up front and toggled by display, so no interactive component is
# created inside a callback. That sidesteps the pattern-matched-id trap entirely.
app.layout = html.Div(
    [
        header,
        html.Div([tab_button(tid, lbl, i == 0) for i, (tid, lbl, _) in enumerate(TABS)],
                 style={"display": "flex", "gap": "2px", "padding": "0 14px",
                        "borderBottom": f"1px solid {BORDER}", "background": BG}),
        dcc.Store(id="active-tab", data=0),
        # Drives the live mirror. 5s is well under how fast a context window moves, and the
        # harvest behind it is rate-limited and lock-guarded, so a slow tick cannot pile up.
        dcc.Interval(id="tick", interval=5000, n_intervals=0),
        html.Div(
            [
                html.Div(fn(), id=f"pane-{i}",
                         style={} if i == 0 else {"display": "none"})
                for i, (_, _, fn) in enumerate(TABS)
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
    [Output(f"pane-{i}", "style") for i in range(len(TABS))]
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
    panes = [{"display": "block"} if i == idx else {"display": "none"} for i in range(len(TABS))]
    tabs = [tab_style(i == idx) for i in range(len(TABS))]
    return panes + tabs + [idx]


@callback(
    Output("breakdown-body", "children"),
    Input("breakdown-scope", "value"),
    prevent_initial_call=True,
)
def _breakdown_scope(scope):
    return breakdown_body(scope == "all")


@callback(
    Output("live-context", "children"),
    Input("tick", "n_intervals"),
)
def _tick(_n):
    """Harvest, then re-render the live reading.

    Ordered deliberately: refresh first, read second, so the number rendered is the one just
    collected rather than the one from the previous tick.
    """
    refresh_store()
    try:
        return context_bar(live_context())
    except Exception as exc:                        # noqa: BLE001 - never blank the header
        return html.Div(f"context unavailable: {str(exc)[:80]}",
                        style={"color": DANGER, "fontSize": "11px", "fontFamily": MONO})


def text_panel(title: str, body: str, colour: str = TEXT) -> html.Div:
    """A scrollable block of real text. Pre-wrapped, because this is prose, not a data grid."""
    return html.Div([
        html.Div(title, style={"color": colour, "fontSize": "12px", "fontWeight": 700,
                               "marginBottom": "6px", "fontFamily": MONO}),
        html.Pre(body, style={
            "whiteSpace": "pre-wrap", "wordBreak": "break-word", "margin": 0,
            "maxHeight": "420px", "overflowY": "auto", "background": PANEL,
            "border": f"1px solid {BORDER}", "borderRadius": "8px", "padding": "12px 14px",
            "color": TEXT, "fontSize": "12px", "fontFamily": MONO, "lineHeight": "1.5",
        }),
    ])


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
        d["chars"] = d["chars"].map(lambda n: f"{int(n):,}")
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
                columns=[{"name": c, "id": c} for c in ["ts", "role", "type", "chars", "preview"]],
                data=d.to_dict("records"), page_size=10,
                style_table={"overflowX": "auto"}, **TABLE_STYLE,
            ),
        ]))
    return html.Div(out)


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
    Output("fig-session", "figure"),
    Output("session-summary", "children"),
    Input("dd-session", "value"),
    Input("session-scope", "value"),
    prevent_initial_call=True,
)
def _session_selected(session_id, scope):
    if not session_id:
        return empty_fig("Pick a session above"), ""
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
    cache_total = int(turns["cache_read_input_tokens"].fillna(0).sum())
    rebill = (cache_total / peak) if peak else 0.0

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
        for c in ("pre_tokens", "post_tokens", "cumulative_dropped_tokens"):
            show[c] = show[c].map(fmt_tokens)
        show["ts"] = show["ts"].str.slice(0, 19).str.replace("T", " ", regex=False)
        cols = ["ts", "trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens",
                "duration_ms", "version"]
        cards = html.Div([cards, html.Div(style={"height": "14px"}),
                          dash_table.DataTable(
                              columns=[{"name": c, "id": c} for c in cols],
                              data=show[cols].to_dict("records"), **TABLE_STYLE)])

    # What was actually said. The chart shows the window filling; this shows what filled it.
    msgs = session_messages(session_id)
    if not msgs.empty:
        m = msgs.copy()
        m["ts"] = m["ts"].astype(str).str.slice(11, 19)
        m["chars"] = m["chars"].map(lambda n: f"{int(n):,}")
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
                columns=[{"name": c, "id": c} for c in ["ts", "role", "type", "chars", "preview"]],
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

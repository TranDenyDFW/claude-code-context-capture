"""The bar above the tabs: scope, selection, and the live context readout.

The desktop app shows CURRENT context. This app only ever surfaced PEAK, so the two could not agree
even though the arithmetic behind them is identical: the desktop read 128.5k/1M while the page
showed a 996.2k high-water mark from before a compaction. Everything here answers the desktop's
question from the same numbers.

`refresh_store` lives here, but app.py imports it into its own namespace and the tick callback
calls it unqualified. That is deliberate: the table audit rebinds app.refresh_store to a no-op
before exercising the callbacks, because the real one harvests and an audit must not write to the
store it is auditing.
"""
import subprocess
import threading as _threading
import time as _time

from dash import dcc, html

from c4x.store import (
    ROOT,
    THRESHOLDS,
    cohort_sessions,
    q,
    scoped,
    session_rows,
    session_window,
)
from c4x.theme import (
    ACCENT,
    BORDER,
    DANGER,
    MONO,
    MUTED,
    WARN,
    fmt_tokens,
)

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
    """Options for the global selector: the path first, then the title, then when it last ran.

    Leading with the path puts the label in the same order as the sort, so a list scanned top to
    bottom reads as one grouped by project rather than as an alphabetical jumble of titles.

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
        # The date is the LAST UPDATE, not the creation. It carried that word for a while, which
        # was worth the width only until the question it answered had been asked once. It refreshes
        # on the 5s tick, bounded by the 45s cache behind session_rows().
        #
        # No peak here either. A high-water token count is not how anyone finds a conversation in a
        # dropdown, and it was the widest field in the label; the All sessions table carries peak
        # beside current, which is where comparing the two is the point.
        # To the minute, not the day. Five imported sessions shared an ingest run, a project and a
        # last day, so their whole labels were byte-identical and picking one was a coin flip. The
        # day was never enough precision for any row; the generated names are just where it showed.
        when = str(r.last_ts or "")[:16].replace("T", " ")
        opts.append({
            "label": f"{r.project}  ·  {r.title[:60]}  ·  {when}",
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

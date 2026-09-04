"""The session tab.

One session in detail: the resident line, the danger bands, and the turn diff.
"""
import plotly.graph_objects as go
from dash import dcc, html

from c4x.dash_compat import DataTable
from c4x.frames import records
from c4x.panels import baseline_marks
from c4x.pricing import PRICE_TABLE_DATE, cost_of_rows
from c4x.store import (
    THRESHOLDS,
    cohort_sessions,
    q,
    real_models,
    scoped,
    segments_for,
    session_compactions,
    session_messages,
    session_survivors,
    session_turns,
    session_window,
)
from c4x.theme import (
    ACCENT,
    BG,
    CONTROL_LABEL,
    DANGER,
    GOOD,
    MONO,
    MUTED,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    VIOLET,
    WARN,
    chart_note,
    dark_fig,
    empty_fig,
    fmt_cost,
    fmt_tokens,
    header_help,
    numeric_columns,
    stat_card,
)

# ---- Session --------------------------------------------------------------
# The detector's two settings, together, chosen by measurement rather than by convention.
#
# The plan called for a 20-call window at 2 sigma. Measured against this store, that flags 15.6%
# of the calls in its largest session and 8.3% in the next: cache reads are heavily skewed and
# bursty, so a two-sigma band over a short window describes the texture of the series rather than
# anything unusual in it. Widening to 50 calls at three sigma flags 1.0% of both, which is a rate
# a reader can actually look at.
#
# Recorded here rather than tuned silently, because a threshold with no stated basis is the part
# of an anomaly detector nobody can check.
ANOMALY_WINDOW = 50
ANOMALY_SIGMA = 3.0
# Enough history for a standard deviation to mean anything. Below this the band is not drawn at
# all, rather than drawn from four points and read as if it were a measurement.
ANOMALY_MIN = 12
# A chart with 1,018 markers on it has no markers on it. The most extreme are drawn and the note
# says how many were outside in total, so the cap can never read as the count.
ANOMALY_MARKS = 40


def rolling_band(series, window=ANOMALY_WINDOW, sigma=ANOMALY_SIGMA):
    """Rolling mean, its band, and which points fall outside it.

    Returns (mean, upper, lower, outside) or None when the series is too short for the statistic
    to say anything. Trailing window: each point is judged against the calls BEFORE it, which is
    the only version that could be computed live, and it means the first ANOMALY_MIN points are
    never judged rather than being judged against the future.
    """
    if series is None or len(series) < ANOMALY_MIN:
        return None
    mean = series.rolling(window, min_periods=ANOMALY_MIN).mean()
    deviation = series.rolling(window, min_periods=ANOMALY_MIN).std()
    upper = mean + sigma * deviation
    lower = (mean - sigma * deviation).clip(lower=0)
    outside = (series > upper) | (series < lower)
    return mean, upper, lower, outside.fillna(False)


def anomaly_band(fig, x, series):
    """Draw the band behind the cache-read line, and mark the calls that left it.

    The threshold lines elsewhere on this chart mark PUBLISHED limits: they say "you are near the
    ceiling", which is a fact about the model. This says "this call was unlike the rest of this
    session", which is a fact about the session and usually arrives earlier. A session can be
    nowhere near a compaction threshold and still have one call that read six times its usual
    context, and nothing else on this page would show it.

    Returns (drawn, note). The note is the caller's to place; a band with no stated window and no
    stated sigma is a shaded area a reader has to guess the meaning of.
    """
    result = rolling_band(series)
    if result is None:
        return False, (f"Fewer than {ANOMALY_MIN} calls, so no anomaly band is drawn: a rolling "
                       f"deviation over that few points is not a measurement.")
    mean, upper, lower, outside = result
    fig.add_trace(go.Scatter(x=x, y=upper, mode="lines", line=dict(width=0),
                             hoverinfo="skip", showlegend=False, name="band upper"))
    fig.add_trace(go.Scatter(
        x=x, y=lower, mode="lines", line=dict(width=0), fill="tonexty",
        fillcolor="rgba(163,113,247,0.13)", hoverinfo="skip",
        name=f"usual range ({ANOMALY_WINDOW}-call mean +/- {ANOMALY_SIGMA:g} sd)"))
    total = int(outside.sum())
    if total:
        # Ranked by DISTANCE outside the band, in band widths, so the marks are the calls least
        # like their neighbours rather than simply the largest ones. The largest call in a session
        # that is uniformly large is not an anomaly.
        width = (upper - mean).replace(0, float("nan"))
        distance = ((series - mean).abs() / width)[outside].dropna()
        picked = list(distance.sort_values(ascending=False).index[:ANOMALY_MARKS])
        fig.add_trace(go.Scatter(
            x=[x[i] for i in range(len(x)) if series.index[i] in picked],
            y=[series.iloc[i] for i in range(len(x)) if series.index[i] in picked],
            mode="markers", name=f"outside it ({total:,})",
            marker=dict(color=DANGER, size=7, symbol="circle-open", line=dict(width=1.5)),
            hovertemplate="turn %{x}<br>%{y:,.0f} cache read, outside the usual range"
                          "<extra></extra>"))
    shown = min(total, ANOMALY_MARKS)
    return True, (
        f"The shaded band is this session's own usual range for cache read: a trailing "
        f"{ANOMALY_WINDOW}-call mean, plus and minus {ANOMALY_SIGMA:g} standard deviations. "
        f"{total:,} of {len(series):,} calls fell outside it"
        + (f"; the {shown} furthest outside are circled. " if total > shown else ". ")
        + "This is a different claim from the dashed threshold lines, which mark published model "
          "limits. A call can be well under every limit and still be unlike the rest of its own "
          "session, which is usually the earlier signal.")


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


def most_recent_session(cohort=None):
    """The newest session in the population that has any turns, or None.

    Newest by last activity, which is the order the desktop sidebar uses and the order a reader
    coming back to the app expects the default to follow.
    """
    ids = cohort_sessions(cohort)
    if ids:
        placeholders = ",".join("?" * len(ids))
        df = q(f"""SELECT session_id FROM turns WHERE session_id IN ({placeholders})
                    GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT 1""", tuple(ids))
    else:
        df = q("SELECT session_id FROM turns GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT 1")
    return None if df.empty else df.iloc[0]["session_id"]


def session_layout(session_id=None, scope="main", cohort=None):
    """One session, in detail. Scoped entirely by the header selection."""
    defaulted = None
    if not session_id:
        # Show the most recent rather than an empty page, and say that is what happened. The
        # header still reads "nothing selected", so a tab quietly rendering a session the header
        # does not name would be read as the selection, which is worse than showing nothing.
        session_id = most_recent_session(cohort)
        if not session_id:
            n = len(cohort_sessions(cohort))
            extra = (f" The population is {n:,} sessions; this tab charts one at a time."
                     if n else "")
            return html.Div([
                html.Div("No session to show", style=SECTION_HEAD),
                html.Div("Nothing in this population has turns recorded." + extra,
                         style=SECTION_NOTE),
            ])
        # THE WARNING SAYS WHICH SESSION THE CHART IS OF, so it belongs to the chart. It used to
        # open the tab as the first of five loose paragraphs, which is where a reader stops reading
        # them.
        defaulted = chart_note(
            "Nothing is selected in the header, so this is the most recently active session in "
            "the population. Pick one in the header, or click a row on All sessions, to change it.",
            style={"color": WARN})
    turns = session_turns(session_id, include_sidechain=(scope != "main"))
    n = max(len(turns), 1)
    default_budget = 80
    marks = {1: "1", n: str(n)} if n > 1 else {1: "1"}
    if n > 8:
        marks[n // 2] = str(n // 2)
    fig, cards = session_view(session_id, scope, default_budget, (1, n))
    return html.Div([
        defaulted,
        html.Div([
            html.Div([
                html.Span("Budget, as a share of the window", className="dash-only",
                          style=CONTROL_LABEL),
                dcc.Slider(id="budget-pct", min=50, max=100, step=5, value=default_budget,
                           marks={v: f"{v}%" for v in (50, 60, 70, 80, 90, 100)},
                           tooltip={"placement": "bottom"}),
            ], style={"flex": "1", "minWidth": "260px"}),
            html.Div([
                html.Span("Compare turns A and B", className="dash-only", style=CONTROL_LABEL),
                dcc.RangeSlider(id="turn-range", min=1, max=n, step=1, value=[1, n],
                                marks=marks, tooltip={"placement": "bottom"},
                                allowCross=False),
            ], style={"flex": "2", "minWidth": "320px"}),
        ], style={"display": "flex", "gap": "28px", "flexWrap": "wrap",
                  "padding": "10px 6px 2px 6px"}),
        chart_note("The shaded bands are the warn, compact and blocked zones for the model in "
                   "use, so headroom is read off the chart rather than computed. The budget line "
                   "is a share of the window rather than a token count, because the windows here "
                   "differ by a factor of five."),
        # The last sentence of that paragraph told the reader to move two sliders. It is marked
        # rather than deleted: the Dash page still has the sliders, and the React page never drew
        # them, so it was an instruction to use a control that was not on screen.
        html.Div("Move A and B apart to see what entered the window between two turns, and what "
                 "it cost.", className="dash-only", style=SECTION_NOTE),
        dcc.Graph(id="session-fig", figure=fig, config={"displayModeBar": False}),
        html.Div(cards, style={"marginTop": "10px"}),
        html.Div(id="session-diff", style={"marginTop": "18px"}),
    ])


def _model_names(turns) -> list[str]:
    """Every model this session actually ran, in the order the store reports them."""
    return real_models(turns["model"])


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
    # The band goes on FIRST so it sits behind the lines. Plotly draws in the order traces are
    # added, and a filled ribbon added last covers the series it is describing.
    band, band_note = anomaly_band(fig, x, turns["cache_read_input_tokens"].fillna(0))
    fig.add_trace(go.Scatter(
        x=x, y=turns["total_resident"], mode="lines", name="Resident",
        line=dict(color=ACCENT, width=2),
        hovertemplate="turn %{x}<br>%{y:,.0f} resident<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=x, y=turns["cache_read_input_tokens"], mode="lines", name="Cache Read",
        line=dict(color=VIOLET, width=1, dash="dot"),
        hovertemplate="turn %{x}<br>%{y:,.0f} cache read<extra></extra>",
    ))
    # Cumulative churn on its own axis. Per-call cache read tracks the resident line and says
    # little on its own; the running total is what shows the session re-paying for the same
    # context, turn after turn, and it is the largest single cost in this store.
    fig.add_trace(go.Scatter(
        x=x, y=turns["cache_read_input_tokens"].fillna(0).cumsum(), mode="lines",
        name="Cache Read, Cumulative", yaxis="y2",
        line=dict(color=WARN, width=1.5),
        hovertemplate="turn %{x}<br>%{y:,.0f} re-read so far<extra></extra>",
    ))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False,
                                  title="Cumulative Cache Read",
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

    # THE TITLE NAMES THE CHART AND NOTHING ELSE. It used to read
    # "1d3708a2 | 1822 turns | peak 995.6k | 1 compactions (red) | 5 model segments", which is a
    # note wearing a title's slot: five facts, four of them already on a stat card, in the one
    # place on the page that should say what is being plotted. They move to the hover, where the
    # colour key at least has somewhere to live.
    facts = [f"Session {session_id[:8]}, {len(turns):,} turns, peak {fmt_tokens(peak)}."]
    if len(comps):
        facts.append(f"{len(comps)} compaction{'s' if len(comps) != 1 else ''}, marked in red.")
    if len(segs) > 1:
        facts.append(f"{len(segs)} model segments: the model changed {len(segs) - 1} time"
                     f"{'s' if len(segs) > 2 else ''} inside this session.")
    if unresolved:
        facts.append(f"{unresolved} segment(s) with an undetermined window.")
    chart_facts = " ".join(facts)
    fig.update_layout(title="Context Window Over the Session",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="Turn", yaxis_title="Tokens")

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
    # Per MODEL, because a session that switched models has more than one price and possibly only
    # one of them in the table. Grouped here rather than multiplied off the totals above, for the
    # same reason the threshold lines are drawn per segment rather than flat across the session.
    _by_model = q(f"""SELECT model, COUNT(*) AS calls,
                            SUM(COALESCE(input_tokens,0))                AS input_tokens,
                            SUM(COALESCE(output_tokens,0))               AS output_tokens,
                            SUM(COALESCE(cache_read_input_tokens,0))     AS cache_read_input_tokens,
                            SUM(COALESCE(cache_creation_input_tokens,0))
                              AS cache_creation_input_tokens
                       FROM api_calls WHERE 1=1 {cw} GROUP BY model""", cargs)
    cost_usd, cost_calls, _unpriced = cost_of_rows(_by_model.to_dict("records"))
    cost_usd = cost_usd if cost_calls else None
    cache_total = int(cdf.iloc[0]["churn"] or 0) if not cdf.empty else 0
    churn_peak = int(cdf.iloc[0]["peak"] or 0) if not cdf.empty else 0
    rebill = (cache_total / churn_peak) if churn_peak else 0.0

    # The band's method, directly under the chart that draws it. A shaded area with no stated
    # window and no stated sigma is something a reader has to guess the meaning of, and the guess
    # available here is the wrong one: the dashed lines above it are published model limits, so an
    # unexplained band reads as another of those rather than as this session's own spread.
    # BOUND TO THE CHART BY ID, not by position: this sits inside the card block, which is a
    # different list from the one holding the chart, so nothing about where it is drawn says what
    # it describes. `for_id` says it.
    band_explainer = chart_note([
        html.Div(chart_facts, style={"marginBottom": "4px"}),
        html.Div(band_note, style={"color": VIOLET if band else MUTED}),
    ], for_id="session-fig", style={"margin": "2px 0 12px 0"})

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
        # THE COUNT IS THE FIGURE; THE NAMES ARE THE CAPTION. This put two model names in the
        # value slot, in the same size and weight as every other card's number, where they wrapped
        # onto four lines and stretched the whole row to match. It also showed the first two and
        # dropped the rest in silence: the session this was written against ran three.
        stat_card("models", str(len(_model_names(turns))),
                  sub=", ".join(_model_names(turns)) or "none recorded"),
        # The one fact the old chart title carried that no card did. Segments are not models: this
        # session has three models and five segments, because it changed model and changed back.
        stat_card("model segments", str(max(len(segs), 1)),
                  sub=f"the model changed {max(len(segs) - 1, 0)} time"
                      f"{'' if len(segs) == 2 else 's'} inside the session"),
        # BLANK, not zero, when this session ran a model the price table does not carry. A cost
        # card reading $0.00 says the session was free, which is a claim; an empty one says this
        # app does not know what it cost, which is true. The sub-line carries the price table's
        # date, so a figure derived from a stale table cannot look current.
        stat_card("estimated cost", fmt_cost(cost_usd) or "not priced",
                  color=WARN if cost_usd else MUTED,
                  # `_by_model["calls"]`, NOT `len(turns)`. This divided by the transcript row
                  # count and called the result calls: for one session it read "5,683 of 8,202
                  # calls", which says a third of the calls went unpriced when every one of them
                  # was priced. The card two along already calls that same 8,202 "transcript
                  # rows", so the page contradicted itself in one line.
                  sub=(f"prices of {PRICE_TABLE_DATE}, {cost_calls:,} of "
                       f"{int(_by_model['calls'].sum()):,} calls"
                       if cost_calls else
                       f"no price in c4x/pricing.py for "
                       f"{', '.join(real_models(turns['model'])[:2]) or 'this model'}")),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})
    cards = html.Div([band_explainer, cards])

    if not comps.empty:
        show = comps.copy()
        show["ts"] = show["ts"].astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
        cols = ["ts", "trigger", "pre_tokens", "post_tokens", "cumulative_dropped_tokens",
                "duration_ms", "version"]
        cards = html.Div([cards, html.Div(style={"height": "14px"}),
                          html.Div("Compactions in this session", style=SECTION_HEAD),
                          html.Div("One row per compaction, oldest first: what it was told to "
                                   "keep, what it kept, and how much it has dropped so far.",
                                   style=SECTION_NOTE),
                          DataTable(
                              columns=(_cols := numeric_columns(cols, {"pre_tokens", "post_tokens",
                                                             "cumulative_dropped_tokens",
                                                             "duration_ms"})),
                              tooltip_header=header_help(_cols),
                              data=records(show[cols]), **TABLE_STYLE)])

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
                     style=SECTION_NOTE),
            DataTable(
                id="tbl-messages",
                columns=(_cols :=
                    numeric_columns(["ts", "role", "type", "chars", "preview"], {"chars"})),
                tooltip_header=header_help(_cols),
                data=records(m[["ts", "role", "type", "chars", "preview", "uuid"]]),
                page_size=12, filter_action="native",  # sort comes from TABLE_STYLE
                style_table={"overflowX": "auto"},
                style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
                **TABLE_STYLE,
            ),
            html.Div(id="message-detail", style={"marginTop": "12px"}),
        ])
    return dark_fig(fig, 460), cards

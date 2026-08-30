"""The waste tab.

What was paid for twice: files re-read, and tools loaded but never called.
"""
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html

from c4x.breakdown import tool_spec
from c4x.panels import evidence_block
from c4x.store import q, scoped
from c4x.theme import (
    DANGER,
    MUTED,
    SECTION_NOTE,
    TEXT,
    dark_fig,
    fmt_tokens,
    stat_card,
)


def _reread_curve(read_tools, where, args, dup_min):
    """How concentrated the re-reading is: cumulative share against rank.

    The table above answers "which file was read most". This answers the question that decides
    whether the table is worth acting on: if the top ten groups are 60% of the repeats, ten fixes
    end most of it; if the curve is a straight line, the re-reading is diffuse and no small number
    of fixes will help.

    Computed over EVERY group, not the 200 the table shows. The table's LIMIT is a display cap and
    a share measured inside it would be a share of the head, which is the shape of answer that
    always looks concentrated: the top ten of a top-200 list is 5% of the rows by construction.
    This store has 1,129 groups behind that 200, so the difference is not academic.
    """
    if not read_tools:
        return html.Div()
    placeholders = ",".join("?" for _ in read_tools)
    every = q(f"""SELECT COUNT(*) - 1 AS repeats FROM tool_calls
                   WHERE tool_name IN ({placeholders}) AND target IS NOT NULL {where}
                   GROUP BY session_id, target HAVING COUNT(*) >= ?
                   ORDER BY COUNT(*) DESC""",
              tuple(read_tools) + args + (dup_min,))
    if len(every) < 5:
        return html.Div()
    repeats = every["repeats"].astype(int).to_numpy()
    total = int(repeats.sum())
    if total <= 0:
        return html.Div()
    share = repeats.cumsum() / total * 100
    rank = list(range(1, len(share) + 1))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=rank, y=share, mode="lines", name="cumulative share",
                            line=dict(color=DANGER, width=2), fill="tozeroy",
                            fillcolor="rgba(248,81,73,0.12)",
                            hovertemplate="top %{x:,} groups<br>%{y:.1f}% of all "
                                          "re-reads<extra></extra>"))
    # The diagonal is what "no concentration at all" looks like. Without it a curve that bends
    # slightly reads as concentrated, because every cumulative curve bends.
    fig.add_trace(go.Scatter(x=[1, len(share)], y=[100 / len(share), 100], mode="lines",
                            name="if every group were equal",
                            line=dict(color=MUTED, width=1, dash="dot"),
                            hoverinfo="skip"))
    fig.update_layout(title=f"Concentration of re-reading across {len(share):,} groups",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="groups, largest first",
                      yaxis_title="% of all re-reads")
    fig.update_yaxes(range=[0, 101])
    top10 = float(share[min(9, len(share) - 1)])
    half = int(next((i + 1 for i, v in enumerate(share) if v >= 50), len(share)))
    return html.Div([
        dcc.Graph(figure=dark_fig(fig, 360), config={"displayModeBar": False}),
        html.Div(
            f"The ten worst groups are {top10:.1f}% of all {total:,} re-reads, and half of them "
            f"sit in the worst {half:,} of {len(share):,} groups. The table above shows the "
            f"first 200 of those groups; this curve is computed over all of them, so the two "
            f"denominators are different on purpose.",
            style=SECTION_NOTE),
    ])


def _rebill_card(session_id=None, cohort=None):
    """Cache reads as a multiple of the peak window: how many times the context was paid for.

    Scoped to the header selection like the rest of the tab, and computed off api_calls rather than
    turns, because a streamed message writes several rows under one request id and summing turns
    would multiply the answer by the streaming.
    """
    where, args = scoped(session_id, "all", alias="", cohort=cohort)
    row = q(f"""SELECT COALESCE(SUM(cache_read_input_tokens), 0) AS churn,
                       COALESCE(MAX(total_resident), 0)          AS peak
                  FROM api_calls WHERE 1=1 {where}""", args).iloc[0]
    churn, peak = int(row["churn"] or 0), int(row["peak"] or 0)
    if not peak:
        return stat_card("Re-billed", "-", sub="no resident reading in this population")
    return stat_card("Re-billed", f"{churn / peak:,.0f}x", color=DANGER,
                     sub=f"{fmt_tokens(churn)} of cache reads against a "
                         f"{fmt_tokens(peak)} peak window")


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
    # scope="all", ALWAYS, whatever the header radio says. Tool calls are overwhelmingly subagent
    # work in this store, and a main-thread-only reading of the worst offender on this tab is zero
    # against a real 614. The override is deliberate; the tab states it below rather than letting
    # the header's banner claim otherwise.
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

    # The three cards below count EVERY group, not the 200 the table shows.
    #
    # They were computed from `dup`, which carries LIMIT 200, so every headline on this tab was a
    # sum over the head of its own display list. In this store that reported 200 groups against a
    # real 1,129, 4,494 re-reads against 7,323, and 41.7 MB against 71.1: a 39% understatement in
    # a figure captioned as a total, presented next to a table that gave no sign it was truncated.
    # The cumulative curve below made it visible, because its denominator is the whole population
    # and its first ten points did not agree with the card above it.
    if not read_tools:
        groups, repeats, repeat_bytes = 0, 0, 0
    else:
        totals = q(f"""SELECT COUNT(*) AS groups,
                              COALESCE(SUM(reads - 1), 0) AS repeats,
                              COALESCE(SUM(bytes * (reads - 1) / reads), 0) AS bytes
                         FROM (SELECT COUNT(*) AS reads,
                                      SUM(COALESCE(result_bytes,0)) AS bytes
                                 FROM tool_calls
                                WHERE tool_name IN ({placeholders})
                                  AND target IS NOT NULL {wsid}
                                GROUP BY session_id, target HAVING reads >= ?)""",
                   dup_args).iloc[0]
        groups = int(totals["groups"])
        repeats = int(totals["repeats"])
        repeat_bytes = int(totals["bytes"])

    for frame in (dup, srv, tools):
        if not frame.empty and "bytes" in frame:
            frame["bytes"] = (frame["bytes"] / 1024).round(1)

    # Why subagents are counted is on the `reads` column tooltip. What stays here is the one thing
    # a tooltip cannot say: which population this page is describing right now.
    scope_note = html.Div(
        "Every tool call, subagent work included, whichever way the scope radio is set. "
        + (f"Narrowed to {'this session' if session_id else 'this cohort'}."
           if (session_id or cohort) else "Describing the whole store."),
        style=SECTION_NOTE)

    return html.Div([
        scope_note,
        html.Div([
            stat_card("Re-read groups", f"{groups:,}",
                      sub=(f"same file, one session, {dup_min}+ reads" if read_tools
                           else "UNAVAILABLE: read-tool spec unreadable")),
            stat_card("Re-reads beyond the first", f"{repeats:,}",
                      color=DANGER if repeats else TEXT),
            stat_card("KB in the repeats", f"{repeat_bytes/1024:,.1f}", sub="tool result bytes"),
            _rebill_card(session_id, cohort),
            stat_card("Tool calls recorded",
                      f"{int(tools['calls'].sum()):,}" if not tools.empty else "0"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"}),

        evidence_block(
            "Files read repeatedly inside one session", dup, sql_dup, dup_args,
            columns=["reads", "bytes", "variants", "session_id", "target"],
            heat=["reads", "bytes"],
            note=f"Every re-read is re-billed on every later request in that session, so the cost "
                 f"is the read multiplied by the turns that follow it. THE WORST 200 GROUPS of "
                 f"{groups:,}: the cards above and the curve below count all of them."),

        _reread_curve(read_tools, wsid, wargs, dup_min),

        evidence_block(
            "MCP servers by invocation count", srv, sql_srv, wargs,
            columns=["server", "calls", "bytes", "last_call"],
            note="Invocation count alone is a PROXY for cost. The measured price of a server is "
                 "the sum of its tools' schema bytes, which tools/otel-ingest.mjs puts in the "
                 "store and tools/waste.mjs --servers reports. This is invocations only."),

        evidence_block(
            "Tool invocations", tools, sql_tools, wargs,
            columns=["tool", "calls", "bytes", "errors"], heat=["calls", "bytes", "errors"]),
    ])

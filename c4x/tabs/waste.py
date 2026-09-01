"""The waste tab.

What was paid for twice: files re-read, and tools loaded but never called.
"""
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
from dash.dash_table.Format import Format, Scheme

from c4x.breakdown import tool_spec
from c4x.panels import evidence_block
from c4x.pricing import PRICE_TABLE_DATE, cost_of, cost_of_rows, coverage_note
from c4x.store import q, scoped
from c4x.theme import (
    DANGER,
    MUTED,
    SECTION_HEAD,
    SECTION_NOTE,
    TEXT,
    WARN,
    dark_fig,
    fmt_cost,
    fmt_tokens,
    numeric_columns,
    population_note,
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
        dcc.Graph(id="fig-reread", figure=dark_fig(fig, 360),
                  config={"displayModeBar": False}),
        html.Div(
            f"The ten worst groups are {top10:.1f}% of all {total:,} re-reads, and half of them "
            f"sit in the worst {half:,} of {len(share):,} groups. The table above shows the "
            f"first 200 of those groups; this curve is computed over all of them, so the two "
            f"denominators are different on purpose.",
            style=SECTION_NOTE),
    ])


def _estimated_cost(where, args):
    """What the recorded tokens would have cost, per model, with the price table on the page.

    The only derived-money figure in this app, so it carries more caveat than anything else here.
    A model the table does not price renders a BLANK cost, never a zero, and the note beneath
    counts how many calls that leaves out. The alternative, quietly summing only what is priced
    under a label that says "total", is a smaller number wearing the right name.

    The price table's date is printed here rather than only in the source, because a price has a
    shelf life and a figure derived from a stale one is wrong in a way nothing on screen shows.
    """
    sql = """SELECT model,
                    COUNT(*)                                        AS calls,
                    SUM(COALESCE(input_tokens, 0))                  AS input_tokens,
                    SUM(COALESCE(output_tokens, 0))                 AS output_tokens,
                    SUM(COALESCE(cache_read_input_tokens, 0))       AS cache_read_input_tokens,
                    SUM(COALESCE(cache_creation_input_tokens, 0))   AS cache_creation_input_tokens
               FROM api_calls WHERE 1=1 """ + where + """
              GROUP BY model ORDER BY calls DESC"""
    df = q(sql, args)
    if df.empty:
        return html.Div()
    rows = df.to_dict("records")
    total, priced_calls, missing = cost_of_rows(rows)
    for row in rows:
        value = cost_of(row["model"], row["input_tokens"], row["output_tokens"],
                        row["cache_read_input_tokens"], row["cache_creation_input_tokens"])
        # The estimate as a NUMBER, so it sorts and exports as one, and a separate rendered
        # string, because Dash cannot express "blank for missing" through a numeric format and
        # numeric_columns would print a missing value as an empty cell only by accident.
        row["est_usd"] = None if value is None else round(value, 2)
        row["priced"] = "yes" if value is not None else ""
    return html.Div([
        html.Div([
            stat_card("Estimated cost", fmt_cost(total) or "-", color=WARN,
                      sub=f"priced models only, table of {PRICE_TABLE_DATE}"),
            stat_card("Calls priced", f"{priced_calls:,}",
                      sub=f"of {int(df['calls'].sum()):,} in this population"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                  "margin": "18px 0 10px 0"}),
        evidence_block(
            "What these tokens would have cost", pd.DataFrame(rows), sql, args,
            columns=numeric_columns(
                ["model", "calls", "input_tokens", "output_tokens",
                 "cache_read_input_tokens", "cache_creation_input_tokens", "est_usd", "priced"],
                {"calls", "input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens", "est_usd"},
                {"est_usd": Format(precision=2, scheme=Scheme.fixed)}),
            heat=["est_usd", "cache_read_input_tokens"], page_size=10,
            help_for={"calls": "Every API call this model answered in this population."},
            note=coverage_note(missing, priced_calls)),
    ])


def _subagent_types(where, args):
    """Which kinds of subagent were spawned, and what their calls returned.

    Subagent work is roughly 70% of the API calls in this store and was the largest block the app
    could not attribute to anything: `tool_calls` held 827 Agent rows and not one of them could say
    what it ran. The type was in the transcripts the whole time, in the tool's own input, and was
    discarded at ingest.

    A row of `Agent` with no type is not an error. Some Agent calls omit `subagent_type` and take
    the default, and the transcript records the omission rather than the default, so this reports
    them as unknown rather than filling in what it thinks was meant.
    """
    sql = """SELECT COALESCE(subagent_type, '(not recorded)') AS agent,
                    COUNT(*)                                 AS calls,
                    COUNT(DISTINCT session_id)               AS sessions,
                    SUM(COALESCE(result_bytes, 0))           AS bytes,
                    SUM(COALESCE(is_error, 0))               AS errors,
                    MIN(ts) AS first_seen, MAX(ts) AS last_seen
               FROM tool_calls
              WHERE tool_name IN ('Agent', 'Task') """ + where + """
              GROUP BY agent ORDER BY calls DESC"""
    df = q(sql, args)
    if df.empty:
        return html.Div()
    df["bytes"] = (df["bytes"] / 1024).round(1)
    for column in ("first_seen", "last_seen"):
        df[column] = df[column].astype(str).str.slice(0, 16).str.replace("T", " ")
    unknown = int(df.loc[df["agent"] == "(not recorded)", "calls"].sum())
    return evidence_block(
        "Subagents, by the kind that was asked for", df, sql, args,
        columns=numeric_columns(
            ["agent", "calls", "sessions", "bytes", "errors", "first_seen", "last_seen"],
            {"calls", "sessions", "bytes", "errors"},
            {"bytes": Format(precision=1, scheme=Scheme.fixed)}),
        heat=["calls", "bytes"], page_size=10,
        help_for={
            "calls": "Agent calls that asked for this subagent type.",
            "sessions": "How many different sessions used this subagent type.",
        },
        note="Read from the Agent call's own input, which this store began keeping in the "
             "harvest and backfilled over every transcript already on disk. "
             + (f"{unknown:,} calls named no type and are reported as such rather than assumed to "
                f"be the default. " if unknown else "")
             + "bytes is tool RESULT bytes, not tokens: an agent's own turns are counted "
               "elsewhere, under the session that spawned it.")


def _repeated_inputs(where, args, session_id=None):
    """Identical tool INPUTS issued in more than one session.

    The table above counts a file read repeatedly inside one session, which is the expensive kind:
    every re-read is re-billed on every later request in that window. This counts something else
    and must not be read as more of it. An identical input issued in two different sessions is
    paid once in each, not multiplied, because no cache spans sessions.

    What it is good for is the thing the per-session view cannot see at all: work being redone
    from scratch across a project. A brief read 480 times across 4 sessions, or one ToolSearch
    query issued in 68 separate sessions, is a standing answer being re-derived rather than
    written down.

    `input_sha1` is a hash of the tool's input, so identity here is exact: same tool, same
    arguments, byte for byte. The store does NOT keep the input itself, which is why a Bash or
    ToolSearch group shows a blank target and can only be identified by its tool and its shape.
    """
    sql = """SELECT tool_name AS tool, target,
                    COUNT(DISTINCT session_id) AS sessions,
                    COUNT(*)                   AS calls,
                    COUNT(*) - COUNT(DISTINCT session_id) AS beyond_one_each,
                    SUM(COALESCE(result_bytes, 0)) AS bytes,
                    MIN(ts) AS first_seen, MAX(ts) AS last_seen
               FROM tool_calls
              WHERE input_sha1 IS NOT NULL """ + where + """
              GROUP BY input_sha1
             HAVING sessions > 1
              ORDER BY calls DESC, sessions DESC
              LIMIT 200"""
    # With one session selected the HAVING can never be satisfied, so the panel would simply not
    # appear. Saying why is the difference between "there are none" and "this question cannot be
    # asked from here", and only one of those is true.
    if session_id:
        return html.Div([
            html.Div("The same input, issued in more than one session", style=SECTION_HEAD),
            html.Div("Not answerable with a single session selected: this table compares sessions "
                     "to each other. Clear the session in the header, or pick a project, to see "
                     "which inputs repeat across a whole population.", style=SECTION_NOTE),
        ])
    df = q(sql, args)
    if df.empty:
        return html.Div()
    df["bytes"] = (df["bytes"] / 1024).round(1)
    for column in ("first_seen", "last_seen"):
        df[column] = df[column].astype(str).str.slice(0, 16).str.replace("T", " ")
    totals = q("""SELECT COUNT(*) AS groups, COALESCE(SUM(calls), 0) AS calls,
                         COALESCE(SUM(sessions), 0) AS pairs FROM (
                    SELECT COUNT(*) AS calls, COUNT(DISTINCT session_id) AS sessions
                      FROM tool_calls WHERE input_sha1 IS NOT NULL """ + where + """
                     GROUP BY input_sha1 HAVING sessions > 1)""", args).iloc[0]
    return evidence_block(
        "The same input, issued in more than one session", df, sql, args,
        columns=numeric_columns(
            ["tool", "target", "sessions", "calls", "beyond_one_each", "bytes",
             "first_seen", "last_seen"],
            {"sessions", "calls", "beyond_one_each", "bytes"},
            {"bytes": Format(precision=1, scheme=Scheme.fixed)}),
        heat=["sessions", "calls"], page_size=12,
        note=f"{int(totals['groups']):,} inputs repeat across sessions, "
             f"{int(totals['calls']):,} calls in total. NOT the same cost as the table above: "
             f"across sessions each call is paid once, not re-billed, because no cache spans "
             f"sessions. This is work being re-derived rather than written down. Identity is a "
             f"hash of the tool input, so it is exact; the input itself is not stored, which is "
             f"why a Bash or ToolSearch row has no target to show.")


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
    scope_note = population_note(
        ("Describing this session. " if session_id else
         "Describing this cohort. " if cohort else
         "Describing the whole store. ")
        + "Every tool call, subagent work included, whichever way the scope radio is set.")

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
            heat=["reads", "bytes"], table_id="tbl-reread",
            note=f"Every re-read is re-billed on every later request in that session, so the cost "
                 f"is the read multiplied by the turns that follow it. THE WORST 200 GROUPS of "
                 f"{groups:,}: the cards above and the curve below count all of them."),

        # The rows the table was built from, unfiltered, plus the size of the population behind
        # them. The curve is drawn over every group and the table holds the worst 200, so the
        # cross-filter needs both numbers to say what a click actually did.
        dcc.Store(id="reread-rows",
                  data={"rows": dup.to_dict("records") if not dup.empty else [],
                        "population": groups}),
        html.Div(id="reread-filter-note"),
        _reread_curve(read_tools, wsid, wargs, dup_min),

        _estimated_cost(wsid, wargs),

        _subagent_types(wsid, wargs),

        _repeated_inputs(wsid, wargs, session_id),

        evidence_block(
            "MCP servers by invocation count", srv, sql_srv, wargs,
            columns=["server", "calls", "bytes", "last_call"],
            # The shared help for "calls" was written for the repeated-inputs table and says these
            # count calls "with this exact input". Here they count every call to the server.
            help_for={"calls": "Every call to this server, whatever the input."},
            note="Invocation count alone is a PROXY for cost. The measured price of a server is "
                 "the sum of its tools' schema bytes, which tools/otel-ingest.mjs puts in the "
                 "store and tools/waste.mjs --servers reports. This is invocations only."),

        evidence_block(
            "Tool invocations", tools, sql_tools, wargs,
            columns=["tool", "calls", "bytes", "errors"], heat=["calls", "bytes", "errors"],
            help_for={"calls": "Every call to this tool, whatever the input."}),
    ])

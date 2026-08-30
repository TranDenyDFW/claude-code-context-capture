"""The waste tab.

What was paid for twice: files re-read, and tools loaded but never called.
"""
import pandas as pd
from dash import html

from c4x.breakdown import tool_spec
from c4x.panels import evidence_block
from c4x.store import q, scoped
from c4x.theme import DANGER, SECTION_NOTE, TEXT, stat_card


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

    if dup.empty:
        repeats, repeat_bytes = 0, 0
    else:
        repeats = int((dup["reads"] - 1).sum())
        repeat_bytes = int((dup["bytes"] * (dup["reads"] - 1) / dup["reads"]).sum())

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

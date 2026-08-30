"""The panels several tabs share: evidence blocks, the compare table, and the turn diff.

Everything here builds a component from data the store returned, so it sits above both theme.py and
store.py and below the tabs. `evidence_block` is the busiest thing in the app, built from eight
different callers, which is why the audit measures coverage per CALLER rather than per line.
"""
import time as _time

from dash import dash_table, html
from dash.dash_table.Format import Format, Scheme

from c4x.store import (
    q,
    scoped,
)
from c4x.theme import (
    ACCENT,
    BORDER,
    CODE_BLOCK,
    DANGER,
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
    fmt_bytes,
    fmt_tokens,
    header_help,
    numeric_columns,
    stat_card,
)

_text_note_cache = {"at": 0.0, "note": None}


def stored_text_note(ttl: float = 300.0) -> str:
    """One line naming what the store holds, with the scale, for the header.

    Cached the way the rest of this file caches, because the header is rebuilt by every callback and
    a COUNT over messages on each of those would be a self-inflicted cost on the page that exists to
    show what things cost.
    """
    now = _time.time()
    if _text_note_cache["note"] is not None and now - _text_note_cache["at"] < ttl:
        return _text_note_cache["note"]
    try:
        row = q("SELECT COUNT(*) AS n, COALESCE(SUM(chars), 0) AS chars FROM messages").iloc[0]
        note = (f"this store keeps the TEXT of {int(row['n']):,} records "
                f"({fmt_bytes(int(row['chars']))} of it), not just their sizes")
    except Exception:                               # noqa: BLE001 - a header must always render
        note = "this store keeps the text of your conversations, not just their sizes"
    _text_note_cache["at"] = now
    _text_note_cache["note"] = note
    return note


# ---- Overview -------------------------------------------------------------
def sql_preview(sql: str, params=()) -> str:
    """The query as run, with its bound values listed rather than interpolated.

    Not substituted into the string: showing `session_id = 'abc'` would suggest the query was built
    that way, and someone copying it would learn the wrong lesson about how this app talks to
    SQLite. The parameters are listed beneath instead, in bind order.
    """
    text = "\n".join(line.rstrip() for line in str(sql).strip().splitlines() if line.strip())
    if not params:
        return text
    shown = list(params)
    # A cohort binds one id per session and there can be 300 of them. The count is the useful part.
    if len(shown) > 8:
        head = ", ".join(repr(p) for p in shown[:6])
        return f"{text}\n\n-- {len(shown)} bound parameters, first 6:\n-- {head}, ..."
    return f"{text}\n\n-- bound parameters, in order:\n-- " + ", ".join(repr(p) for p in shown)


def evidence_block(title: str, df, sql: str, params=(), columns=None, page_size: int = 12,
                   note: str = None, style_data_conditional=None):
    """A table, the query that produced it, and a way to take the rows away.

    Every figure on this page should be reproducible by someone who does not have this app open.
    That is the difference between a dashboard number and a research one, and it is how this repo
    already treats evidence everywhere else: a claim carries the command that proves it.

    The row count is stated on the table itself, so a filtered or truncated view can never be read
    as the whole population.
    """
    if df is None or (hasattr(df, "empty") and df.empty):
        return html.Div([
            html.Div(title, style=SECTION_HEAD),
            html.Div("No rows.", style=SECTION_NOTE),
            accordion("The query that returned nothing", "reproduce it yourself",
                      html.Pre(sql_preview(sql, params),
                               style={**CODE_BLOCK, "whiteSpace": "pre-wrap", "display": "block"})),
        ])
    records = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    cols = columns or list(df.columns if hasattr(df, "columns") else records[0].keys())
    # A caller may hand over plain names OR ready-made specs from numeric_columns(). Wrapping a
    # spec again put a dict in `name`, dash_table rendered it as a React child, and React threw
    # error #31 and remounted the whole page: every component fell back to its default, which for
    # the header picker is no selection. It looked like a broken selection rather than a broken
    # table, and it cost an afternoon. Accept both shapes instead of trusting the caller.
    cols = [c if isinstance(c, dict) else {"name": c, "id": c} for c in cols]
    truncated = (" (table shows the first page; export gives every row)"
                 if len(records) > page_size else "")
    return html.Div([
        html.Div(title, style=SECTION_HEAD),
        html.Div(f"{len(records):,} rows.{truncated}" + (f" {note}" if note else ""),
                 style=SECTION_NOTE),
        dash_table.DataTable(
            columns=(_cols := cols),
            tooltip_header=header_help(_cols),
            data=records,
            page_size=page_size,
            sort_action="native",
            tooltip_duration=None,   # stay up while hovering; see TABLE_STYLE for why
            filter_action="native",
            export_format="csv",
            export_headers="display",
            style_table={"overflowX": "auto"},
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            style_data_conditional=(style_data_conditional or
                                    [{"if": {"row_index": "odd"}, "backgroundColor": "#12171e"}]),
            style_cell=TABLE_STYLE["style_cell"],
            style_header=TABLE_STYLE["style_header"],
        ),
        accordion("The query behind this table", "copy it, or export the rows above",
                  html.Pre(sql_preview(sql, params),
                           style={**CODE_BLOCK, "whiteSpace": "pre-wrap", "display": "block"})),
    ])


def baseline_marks(fig, x_for_ts=None, ts_list=None):
    """Mark every recorded calibration on a time chart.

    A configuration's fixed overhead moves when an MCP server or a skill is added, and the store
    dates every observation of it. Without these the reader sees a step in the data and has no way
    to know whether something changed or something broke. With them it is an event with a date.

    Charts here are indexed by turn number rather than by time, so a timestamp has to be mapped to
    the nearest turn; where that cannot be done the mark is omitted rather than placed at a guess.
    """
    try:
        bl = q("SELECT ts, static_total, source FROM context_baselines ORDER BY ts")
    except Exception:
        return 0
    if bl.empty or not ts_list:
        return 0
    drawn = 0
    for r in bl.itertuples():
        pos = x_for_ts(str(r.ts)) if x_for_ts else None
        if not pos:
            continue
        fig.add_vline(x=pos, line=dict(color=GOOD, width=1, dash="dot"))
        fig.add_annotation(x=pos, yref="paper", y=1.02, showarrow=False,
                           text=f"calibrated {fmt_tokens(r.static_total)}",
                           font=dict(color=GOOD, size=9, family=MONO), xanchor="left")
        drawn += 1
    return drawn


def selection_metrics(session_id=None, cohort=None, scope="main") -> dict:
    """The numbers that describe any selection, session or cohort, from one query shape.

    Both sides of a comparison go through this, so the two arms cannot be measured differently.
    Matching the arms is the whole point: a difference produced by asking two different questions
    is not a finding.
    """
    w, args = scoped(session_id, scope, cohort=cohort)
    row = q(f"""
        SELECT COUNT(*)                                      AS calls,
               COUNT(DISTINCT session_id)                     AS sessions,
               SUM(COALESCE(cache_read_input_tokens,0))       AS cache_read,
               SUM(COALESCE(cache_creation_input_tokens,0))   AS cache_creation,
               SUM(COALESCE(output_tokens,0))                 AS output,
               SUM(COALESCE(thinking_tokens,0))               AS thinking,
               MAX(total_resident)                            AS peak,
               AVG(total_resident)                            AS mean_resident,
               MIN(ts)                                        AS first_ts,
               MAX(ts)                                        AS last_ts
        FROM api_calls WHERE 1=1 {w}
    """, args).iloc[0].to_dict()
    cw, cargs = scoped(session_id, "all", alias="c", cohort=cohort)
    comp = q(f"SELECT COUNT(*) AS n FROM compactions c WHERE 1=1 {cw}", cargs).iloc[0]["n"]
    tw, targs = scoped(session_id, scope, cohort=cohort)
    tools = q(f"""SELECT COUNT(*) AS n, SUM(COALESCE(result_bytes,0)) AS bytes
                  FROM tool_calls WHERE 1=1 {tw}""", targs).iloc[0].to_dict()
    calls = int(row["calls"] or 0)
    peak = int(row["peak"] or 0)
    cache = int(row["cache_read"] or 0)
    return {
        "sessions": int(row["sessions"] or 0),
        "calls": calls,
        "cache_read": cache,
        "rebill_multiple": (cache / peak) if peak else 0.0,
        "cache_per_call": (cache / calls) if calls else 0.0,
        "cache_creation": int(row["cache_creation"] or 0),
        "output": int(row["output"] or 0),
        "thinking": int(row["thinking"] or 0),
        "peak": peak,
        "mean_resident": float(row["mean_resident"] or 0),
        "compactions": int(comp or 0),
        "tool_calls": int(tools["n"] or 0),
        "tool_bytes": int(tools["bytes"] or 0),
        "first_ts": str(row["first_ts"] or "")[:19].replace("T", " "),
        "last_ts": str(row["last_ts"] or "")[:19].replace("T", " "),
    }


# How each metric is rendered, and whether a bigger number is worse. Declared once so the compare
# table cannot label one row "worse" by one rule and another by a different one.
# key, label, format, which direction is worse, and whether the figure is size-independent.
#
# That last flag is the one that stops the table lying by arithmetic. Comparing 3 sessions against
# 303 makes almost every total 100x larger on one side, and that ratio says nothing except that one
# population is bigger. Only the per-unit rows compare arms of different sizes honestly, so they
# are the ones marked comparable and the totals are labelled as scaling with population.
COMPARE_ROWS = [
    ("sessions", "sessions", "count", None, False),
    ("calls", "API calls", "count", None, False),
    ("cache_read", "cache re-reads", "tokens", "higher", False),
    ("rebill_multiple", "re-billed, as a multiple of peak", "multiple", "higher", True),
    ("cache_per_call", "cache read per call", "tokens", "higher", True),
    ("peak", "peak resident", "tokens", None, True),
    ("mean_resident", "mean resident", "tokens", "higher", True),
    ("output", "output tokens", "tokens", None, False),
    ("thinking", "thinking tokens", "tokens", None, False),
    ("compactions", "compactions", "count", "higher", False),
    ("tool_calls", "tool calls", "count", None, False),
    ("tool_bytes", "tool result bytes", "bytes", "higher", False),
]


def compare_table(a_label, a, b_label, b) -> html.Div:
    """Render two metric dicts side by side, with the delta and its direction.

    Each row carries its own unit, which is why the unit is a column rather than a suffix glued
    onto the value. A and B used to hold "18.83B", "51.5 MB" and "1,291", so the arms of a research
    comparison were display text: they sorted lexicographically and the CSV export, which is the
    point of the tab, received the labels instead of the numbers.
    """
    same_size = a.get("sessions") == b.get("sessions")
    rows = []
    for key, label, kind, worse, per_unit in COMPARE_ROWS:
        av, bv = a.get(key, 0), b.get(key, 0)
        if not av and not bv:
            continue
        comparable = per_unit or same_size
        if av and bv:
            ratio = bv / av if av else 0
            # Only claim better or worse where the metric has a direction AND the ratio means
            # something. A total is 100x larger because one arm holds 100x the sessions, which is
            # a fact about the populations, not about how they behaved.
            if worse and comparable and abs(ratio - 1) >= 0.05:
                verdict = "B worse" if (ratio > 1) == (worse == "higher") else "B better"
            else:
                verdict = ""
        else:
            ratio, verdict = None, "one arm has none"
        rows.append({"metric": label, "unit": kind,
                     "A": round(av, 1), "B": round(bv, 1),
                     "B vs A": None if ratio is None else round(ratio, 2), "verdict": verdict,
                     "basis": "per unit" if per_unit else "total, scales with population"})
    return html.Div([
        html.Div([
            html.Div([html.Div("A", style={"color": ACCENT, "fontWeight": 700, "fontFamily": MONO}),
                      html.Div(a_label, style={"color": MUTED, "fontSize": "11px"})],
                     style={"flex": "1"}),
            html.Div([html.Div("B", style={"color": VIOLET, "fontWeight": 700, "fontFamily": MONO}),
                      html.Div(b_label, style={"color": MUTED, "fontSize": "11px"})],
                     style={"flex": "1"}),
        ], style={"display": "flex", "gap": "16px", "marginBottom": "10px"}),
        dash_table.DataTable(
            columns=(_cols := numeric_columns(
                ["metric", "unit", "A", "B", "B vs A", "verdict", "basis"],
                {"A", "B", "B vs A"},
                {"B vs A": Format(precision=2, scheme=Scheme.fixed)})),
            tooltip_header=header_help(_cols),
            data=rows,
            export_format="csv",
            export_headers="display",
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"},
                {"if": {"filter_query": '{verdict} contains "worse"'}, "color": DANGER},
                {"if": {"filter_query": '{verdict} contains "better"'}, "color": GOOD},
            ],
            sort_action="native",   # uniform with every other table in the app
            tooltip_duration=None,
            style_cell=TABLE_STYLE["style_cell"], style_header=TABLE_STYLE["style_header"],
            style_table={"overflowX": "auto"}),
        html.Div("Values are raw, in the unit each row names, so the export carries numbers "
                 "rather than labels. Only rows where a bigger number is unambiguously worse are "
                 "marked, and only "
                 "where the comparison means something. Peak resident and output tokens carry no "
                 "direction, since a longer session is not a worse one. Rows marked \"total\" "
                 "scale with how many sessions each arm holds, so when the arms are different "
                 "sizes their ratio describes the populations rather than the behaviour; the "
                 "per-unit rows are the ones that compare unequal arms honestly.",
                 style=SECTION_NOTE),
    ])


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


def turn_diff(session_id, scope, ts_a, ts_b):
    """What entered the window between two turns, and what it cost.

    The question the rest of this tab could not answer. A resident line says the context grew; it
    does not say what grew it, and the answer is almost always a specific tool result or a specific
    file, which is the thing a reader can act on. Everything here is between the two timestamps,
    exclusive of A and inclusive of B, so the numbers add up to the delta shown beside them.
    """
    w, args = scoped(session_id, scope)
    span = (ts_a, ts_b)

    spend = q(f"""SELECT COUNT(*) AS calls,
                         COALESCE(SUM(output_tokens), 0) AS output,
                         COALESCE(SUM(thinking_tokens), 0) AS thinking,
                         COALESCE(SUM(cache_read_input_tokens), 0) AS cache_read,
                         COALESCE(SUM(cache_creation_input_tokens), 0) AS cache_write
                    FROM api_calls WHERE ts > ? AND ts <= ? {w}""",
              (*span, *args))

    tools = q(f"""SELECT tool_name AS tool,
                         COUNT(*) AS calls,
                         COALESCE(SUM(result_bytes), 0) AS result_bytes,
                         COALESCE(SUM(input_bytes), 0) AS input_bytes,
                         SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS errors
                    FROM tool_calls WHERE ts > ? AND ts <= ? {w}
                   GROUP BY tool ORDER BY result_bytes DESC LIMIT 40""",
                (*span, *args))

    targets = q(f"""SELECT target, tool_name AS tool, COUNT(*) AS reads,
                           COALESCE(SUM(result_bytes), 0) AS result_bytes
                      FROM tool_calls
                     WHERE ts > ? AND ts <= ? AND target IS NOT NULL AND target != '' {w}
                     GROUP BY target, tool ORDER BY result_bytes DESC LIMIT 40""",
                  (*span, *args))

    said = q(f"""SELECT role, type, COUNT(*) AS messages,
                        COALESCE(SUM(chars), 0) AS chars
                   FROM messages WHERE ts > ? AND ts <= ? {w}
                  GROUP BY role, type ORDER BY chars DESC""",
              (*span, *args))
    return spend, tools, targets, said


def turn_diff_panel(session_id, scope, turns, a, b):
    """Render the diff between turns a and b, with the SQL that produced each table."""
    if turns.empty or not a or not b or a >= b:
        return html.Div("Move the two handles apart to compare a range of turns.",
                        style=SECTION_NOTE)
    a = max(1, min(a, len(turns)))
    b = max(1, min(b, len(turns)))
    ts_a = str(turns["ts"].iloc[a - 1])
    ts_b = str(turns["ts"].iloc[b - 1])
    resident_a = int(turns["total_resident"].iloc[a - 1] or 0)
    resident_b = int(turns["total_resident"].iloc[b - 1] or 0)
    delta = resident_b - resident_a

    spend, tools, targets, said = turn_diff(session_id, scope, ts_a, ts_b)
    row = spend.iloc[0] if not spend.empty else None

    # The unexplained remainder is stated rather than hidden. Resident growth is not the sum of the
    # tool results in the range: a compaction can drop context inside it, and the store holds no
    # size for every record. A panel that implied the parts added up would be inviting a wrong
    # conclusion, which is the same defect as a number with no context.
    accounted = int(tools["result_bytes"].sum()) if not tools.empty else 0

    cards = html.Div([
        stat_card("turns", f"{a} to {b}", sub=f"{b - a} turns"),
        stat_card("resident at A", fmt_tokens(resident_a)),
        stat_card("resident at B", fmt_tokens(resident_b), color=ACCENT),
        stat_card("delta", ("+" if delta >= 0 else "-") + fmt_tokens(abs(delta)),
                  color=DANGER if delta > 0 else GOOD,
                  sub="a drop means a compaction fired in the range"),
        stat_card("re-read in range", fmt_tokens(int(row["cache_read"]) if row is not None else 0),
                  color=WARN, sub=f"{int(row['calls']) if row is not None else 0} API calls"),
        stat_card("output", fmt_tokens(int(row["output"]) if row is not None else 0),
                  sub=f"{fmt_tokens(int(row['thinking']) if row is not None else 0)} thinking"),
        stat_card("tool results", fmt_bytes(accounted),
                  sub="bytes returned, not tokens: no conversion is recorded"),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})

    blocks = [
        html.Div(f"Between turn {a} ({ts_a[:19]}) and turn {b} ({ts_b[:19]})", style=SECTION_HEAD),
        cards,
    ]
    if not tools.empty:
        blocks.append(evidence_block(
            "tools called in this range", tools,
            "SELECT tool_name, COUNT(*), SUM(result_bytes), SUM(input_bytes), SUM(is_error) "
            "FROM tool_calls WHERE ts > ? AND ts <= ? GROUP BY tool_name",
            (ts_a, ts_b),
            columns=numeric_columns(list(tools.columns),
                                    {"calls", "result_bytes", "input_bytes", "errors"})))
    if not targets.empty:
        blocks.append(evidence_block(
            "what was read, largest first", targets,
            "SELECT target, tool_name, COUNT(*), SUM(result_bytes) FROM tool_calls "
            "WHERE ts > ? AND ts <= ? AND target IS NOT NULL GROUP BY target, tool_name",
            (ts_a, ts_b),
            columns=numeric_columns(list(targets.columns), {"reads", "result_bytes"})))
    if not said.empty:
        blocks.append(evidence_block(
            "what was said", said,
            "SELECT role, type, COUNT(*), SUM(chars) FROM messages "
            "WHERE ts > ? AND ts <= ? GROUP BY role, type",
            (ts_a, ts_b),
            columns=numeric_columns(list(said.columns), {"messages", "chars"})))
    return html.Div(blocks)

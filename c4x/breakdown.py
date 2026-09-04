"""Deriving the category split of the context window.

Not a tab. `latest_baseline` and `tool_spec` were the only things one tab borrowed from
another, and they are derivation rather than drawing, so they sit beside panels.py at the
layer above the store.

The split is DERIVED, not read: Claude Code computes it for its tooltip and writes it
nowhere, so this subtracts a recorded baseline from the resident total. Resident and free
space are exact; everything between them is inferred and labelled as such.
"""
import json
import subprocess

import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
from dash.dash_table.Format import Format, Scheme

from c4x.dash_compat import DataTable
from c4x.store import ROOT, q, scoped
from c4x.theme import (
    ACCENT,
    BORDER,
    DANGER,
    GOOD,
    MONO,
    MUTED,
    PANEL,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    WARN,
    chart_note,
    dark_fig,
    fmt_tokens,
    header_help,
    numeric_columns,
)

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


def composition_treemap(baseline, resident_cols, labels, messages, free, window,
                        resident=0, population=''):
    """The window as area, grouped by what a reader can actually do about each part.

    Three groups, because that is the decision the flat bar cannot express: Configuration is fixed
    for the whole session and yours to change, Messages grows with the conversation and is not,
    and Free space is what is left. The bar puts all eight categories side by side in one row, so
    a reader comparing "my skills" against "this conversation" has to add up the segments first.

    branchvalues="total" is safe here because the category columns sum EXACTLY to static_total by
    construction: `breakdown.mjs --calibrate` records both, and a mismatch would mean the
    calibration itself disagreed with its own parts. Asserted rather than assumed, and a mismatch
    drops to a flat one-level map rather than drawing a wrong one.
    """
    from c4x.theme import treemap
    parts = [(BREAKDOWN_LABELS.get(c) or labels.get(c, c), int(baseline[c] or 0))
             for c in resident_cols]
    parts = [(name, value) for name, value in parts if value > 0]
    static = sum(value for _name, value in parts)
    labels_, parents, values, colors = [], [], [], []
    if static and static == int(baseline["static_total"] or 0):
        labels_.append("Configuration")
        parents.append("")
        values.append(static)
        colors.append("#e8590c")
        for index, (name, value) in enumerate(parts):
            labels_.append(name)
            parents.append("Configuration")
            values.append(value)
            colors.append(breakdown_color(name, index))
    elif static:
        # The calibration does not agree with its own parts. Draw the categories at the top level
        # rather than under a parent whose value would be a number this store cannot support.
        for index, (name, value) in enumerate(parts):
            labels_.append(name)
            parents.append("")
            values.append(value)
            colors.append(breakdown_color(name, index))
    for name, value, color in (("Messages", messages, ACCENT), ("Free space", free, "#21262d")):
        if value > 0:
            labels_.append(name)
            parents.append("")
            values.append(int(value))
            colors.append(color)
    # THE NUMBER IS THE TITLE. The figure it describes was drawn directly under a card stating
    # the same 782.8k, the same percentage and the same free space, so the page said it twice and
    # the picture was the only one of the two that showed the shape. The title also names the
    # POPULATION, because "right now" is a different moment for one chat than for a whole store
    # and nothing on the chart said which one was being sized.
    share = f" ({resident / window * 100:.0f}%)" if window else ""
    where = f" - {population}" if population else ""
    return treemap(labels_, parents, values, colors=colors, height=380,
                   title=f"Context Window Composition: {fmt_tokens(resident)} of "
                         f"{fmt_tokens(window)}{share}{where}")


def composition_blocks(include_sidechain: bool = False, session_id=None, cohort=None):
    """The category split as it stands, and how it moved. DERIVED from a calibrated baseline.

    The item detail that used to be appended here lives in c4x/probe_detail.py and is rendered as
    its own panel of the Window tab. Keeping a `with_items` flag would have left a branch nothing
    took, which the table audit reported as a call that can build a table on a path never
    exercised: the accurate description of dead code.
    """
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
    # WHICH POPULATION THIS IS THE WINDOW OF, in the words the title will use. "Right now" is a
    # different moment for one chat than for a whole store, and the chart said neither.
    if session_id:
        population = "one chat"
    elif cohort:
        population = str(cohort).split("::", 1)[-1] or "a population"
    else:
        population = "store-wide"
    if include_sidechain:
        population += ", subagents included"
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
    fig.add_trace(go.Scatter(x=x, y=stat, mode="lines", name="Static Overhead",
                             line=dict(width=0), stackgroup="one", fillcolor="#e8590c"))
    fig.add_trace(go.Scatter(x=x, y=(res - stat).clip(lower=0), mode="lines",
                             name="Messages", line=dict(width=0), stackgroup="one",
                             fillcolor=ACCENT))
    fig.add_trace(go.Scatter(x=x, y=(window - res).clip(lower=0), mode="lines", name="Free Space",
                             line=dict(width=0), stackgroup="one", fillcolor="#21262d"))
    fig.update_layout(title="Context Window Over Time",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="API Call", yaxis_title="Tokens")

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
    composition = [
        bar,
        html.Div(style={"height": "14px"}),
        dcc.Graph(id="fig-window-treemap",
                  figure=composition_treemap(b, resident_cols, labels, messages, free, window,
                                             resident=resident, population=population),
                  config={"displayModeBar": False}),
        chart_note(f"Resident {fmt_tokens(resident)} of a {fmt_tokens(window)} window. Sized by "
                   "tokens, and grouped by the one distinction that decides what you can do about "
                   "any of it: Configuration is fixed and yours to change, Messages grows and is "
                   "not.", for_id="fig-window-treemap",
                   style={"margin": "4px 0 14px 0"}),
        # The flat bar is an html.Div of spans, not a figure, so the React page never draws it and
        # a sentence comparing it against the treemap describes something that is not on screen.
        html.Div(
            "The bar above is the same figure flat. It is the shape the tooltip uses, which makes "
            "the two comparable at a glance, and it loses the one distinction that decides what "
            "you can do about any of it: Configuration is fixed and yours to change, Messages "
            "grows and is not. The treemap groups them; the bar cannot.",
            className="dash-only",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "4px 0 14px 0",
                   "maxWidth": "900px", "lineHeight": "1.55"}),
        html.Div("Itemized Window", style=SECTION_HEAD),
        html.Div("One row per item the window holds right now, with its share of the "
                 "resident total. Resident and free space are exact; the category rows are "
                 "the baseline's split and are not.", style=SECTION_NOTE),
        DataTable(
            columns=(_cols := numeric_columns(
                ["category", "tokens", "percent", "items"], {"tokens", "percent", "items"},
                {"percent": Format(precision=1, scheme=Scheme.fixed)})),
            tooltip_header=header_help(_cols),
            data=rows, **TABLE_STYLE),
        # BOUND TO THE CHART BELOW, by id. Two of these sit above the chart and one below the
        # table, so nothing about where they are drawn says which of the two charts they are
        # about; all three describe the composition series.
        chart_note(notes + [html.Div(
            f"DERIVED, not measured. Claude Code stores this split nowhere, so Messages and the "
            f"category rows are computed as resident minus a recorded baseline of "
            f"{static_total:,} tokens (source: {b['source']}, recorded {applies}). Resident and "
            f"free space are exact. Each charted turn is split by the baseline in force at that "
            f"turn, not by the newest one. Rows marked 'not resident' are deferred tools, which "
            f"the tooltip lists without a percentage because they cost nothing until they load - "
            f"re-calibrate after adding an MCP server, a skill, or editing CLAUDE.md.",
            style={"color": MUTED, "fontSize": "11.5px", "margin": "12px 0 0 0",
                   "maxWidth": "900px", "lineHeight": "1.55"})],
            for_id="fig-window-composition", style={"margin": "10px 0 14px 0"}),
        dcc.Graph(id="fig-window-composition", figure=dark_fig(fig, 420),
                  config={"displayModeBar": False}),
    ]
    # The categories above are DERIVED from a baseline. The item detail is a direct reading, and
    # the only place this store can say WHICH skill or WHICH tool the tokens went to. Two subjects,
    # returned separately so a caller can show one without the other.
    return html.Div(composition)

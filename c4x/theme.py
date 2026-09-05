"""How the app looks: colours, shared styles, formatters, and the small shared builders.

The leaf of the dependency graph. Nothing here knows what a session is or how to reach the store,
which is what lets every other module import it without a cycle.

Split out of app.py, which had grown to 3124 lines. The colours were always in one place; the
builders that use them were scattered through it.
"""
from typing import Any

import pandas as pd  # fmt_tokens asks pandas whether a value is NA
import plotly.graph_objects as go
from dash import html
from dash.dash_table.Format import Format, Group

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


# Export lives here rather than on each table, because it belongs to every one of them: a figure
# nobody can take away and check is a dashboard number, not a research one. Every table that
# spreads TABLE_STYLE gets a CSV button; the tables that set style_cell explicitly (evidence_block,
# the compare table) already ask for it directly.
TABLE_STYLE = dict(
    export_format="csv",
    export_headers="display",
    # Sorting on EVERY table, not just the ones built through evidence_block. Half of them sorted
    # and half did not, with nothing on screen to tell a reader which kind they were looking at.
    sort_action="native",
    # Tooltips stay up while the pointer is on the header. Dash hides them after 2000ms by
    # default, and these run to about 200 characters: the `turns` caveat vanished mid-sentence
    # while being read. None means "as long as you are hovering".
    tooltip_duration=None,
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


def fmt_cost(usd) -> str:
    """A money figure, or BLANK when there is no price for it.

    Blank, not "-", and never 0. `fmt_tokens` returns "-" for a missing value because a missing
    token count is a gap in a measurement; a missing cost is a gap in this app's KNOWLEDGE of a
    price, and "-" reads as "measured, and it was nothing". An empty cell reads as what it is.

    Sub-cent amounts collapse to "<$0.01" rather than rendering $0.0004. Six decimal places on a
    dashboard implies a precision the price table does not have, and a column of them is unreadable
    next to a $12,000 total.
    """
    if usd is None or (isinstance(usd, float) and pd.isna(usd)):
        return ""
    usd = float(usd)
    if usd and abs(usd) < 0.01:
        return "<$0.01"
    if abs(usd) >= 1000:
        return f"${usd:,.0f}"
    return f"${usd:,.2f}"


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


CONTROL_LABEL = {"color": MUTED, "fontSize": "11.5px", "fontFamily": MONO}


SECTION_NOTE = {"color": MUTED, "fontSize": "12px", "marginBottom": "8px", "maxWidth": "900px",
                "lineHeight": "1.55"}


CODE_BLOCK = {"background": PANEL, "border": f"1px solid {BORDER}", "borderRadius": "8px",
              "padding": "12px 14px", "color": TEXT, "fontFamily": MONO, "fontSize": "12px",
              "display": "inline-block"}


def population_note(text: str, store_wide: bool = True) -> html.Div:
    """The one sentence saying what a tab's numbers cover.

    MARKED WITH A CLASSNAME, not recognised by its opening words. The API used to decide whether a
    line was this one by testing whether it started with "Describing " or "Store-wide.", so the
    three tabs that state their own population in their own wording reported none at all, and any
    rewording anywhere would have switched the field off silently.

    Same device as `stat_card`: the server marks what a thing IS rather than leaving a reader of
    the payload to infer it from the text. Inert for Dash, which just puts the class on the div.
    """
    return html.Div(text, className="population-note", style=SECTION_NOTE,
                    # WHAT IT IS DESCRIBING, carried as data rather than left to be read out of
                    # the sentence. The page shows a chip for this, and the chip was derived from
                    # whether the tab RESPONDS to the header selection, which is a different fact:
                    # with nothing selected, Compactions, Window, Cost and Compare all described
                    # the whole store while the chip said "This Selection".
                    # dash accepts data-* and aria-* attributes at runtime and models neither in
                    # html.Div's signature, so every declared keyword is offered this dict in turn
                    # and none of them fit. Three errors, one unsupported-but-supported attribute.
                    **{"data-population": "store" if store_wide else "selection"})  # type: ignore[arg-type]


def stat_card(label: str, value: str, color: str = TEXT, sub: str = "") -> html.Div:
    # CLASSNAME SO IT CAN BE FOUND AGAIN. `extract.texts()` flattens a pane to a list of strings, so
    # over the API seven of these arrive as twenty-one loose lines that happen to be in groups of
    # three. A frontend could infer the triples and would be wrong the moment one card gained a
    # fourth child, silently and with every later card shifted by one. Marked here instead, so the
    # server can hand over cards rather than a list to be re-grouped by guesswork.
    # Inert for Dash, which just puts the class on the div.
    return html.Div(
        className="stat-card",
        children=[
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
    # HOVER ANSWERS ANYWHERE ON THE CHART, not only within 20 px of a point. Plotly's default is
    # `closest`, which on a turn-by-turn line chart means the pointer must sit on the line, and one
    # trace answers at a time. Measured on the Session chart: three pointer positions inside the
    # plot, two tooltips, one blank. A chart with lines or areas now hovers `x unified`: every
    # series reports its value at that x wherever the pointer is vertically, and the band traces
    # that set hoverinfo="skip" stay out of it. The reach is finite on purpose: with an unlimited
    # one, the sparse marker traces (survivors, out-of-range points) joined every tooltip with
    # their NEAREST point, hundreds of turns away, and read as if it were at the pointer. Forty
    # pixels keeps a dense line answering everywhere and a sparse marker only when it is there.
    # Markers-only charts keep `closest` with the same reach, since there is no x to unify along.
    # Read through each trace's own JSON rather than getattr(): tools/table_audit.py treats a
    # getattr() call as one it cannot name, and refuses the file for it, on purpose.
    specs = []
    for trace in fig.data:
        spec = trace.to_plotly_json()
        specs.append((trace, spec))
    kinds = [(spec.get("type"), spec.get("mode") or "", spec.get("fill")) for _, spec in specs]
    lined = any(k == "scatter" and ("lines" in m or f) for k, m, f in kinds)
    if lined:
        fig.update_layout(hovermode="x unified", hoverdistance=40)
        for trace, spec in specs:
            if (spec.get("type") == "scatter" and not spec.get("hovertemplate")
                    and spec.get("hoverinfo") is None):
                trace.hovertemplate = "%{y:,.0f}"
    elif any(k == "scatter" for k, _, _ in kinds):
        fig.update_layout(hovermode="closest", hoverdistance=40)
    fig.update_xaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER,
                     tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    fig.update_yaxes(gridcolor=BORDER, zerolinecolor=BORDER, linecolor=BORDER,
                     tickfont=dict(color=MUTED), title_font=dict(color=MUTED))
    return fig


def heat_cells(rows, column, *, invert=False):
    """Background shading for one numeric column, by where each value sits in that column's range.

    A 316-row table hides its own outliers. Sorting finds them one column at a time and destroys
    whatever order the reader had; shading shows them in whatever order the table is already in.

    Returns style_data_conditional entries, so it MUST be combined with the striping in
    TABLE_STYLE rather than replacing it. `heated()` below does that; call it rather than this.

    Banded, not continuous. Dash matches these with filter expressions, one per band, and a
    per-row rule on 316 rows is 316 filter expressions evaluated in the browser. Four bands read
    the same and cost nothing. Values are bucketed by RANK, not by value, because token counts are
    heavily skewed: by value, one 24x outlier puts every other row in the bottom band and the
    shading says nothing.

    Rows on the quiet side of the median take no rule at all, so they keep the odd-row striping
    and the table still reads as a table rather than as a heat map with text on it.

    ORDER IS LOAD-BEARING. Dash applies matching rules in order and the last one wins, so a value
    in the top 5% matches every band and takes whichever is emitted last. Emitted shallowest
    first, deepest last, which is the reverse of how the bands read.

    `invert` shades SMALL values hot, for a column where low is the bad end.
    """
    values = sorted(v for v in (r.get(column) for r in rows)
                    if isinstance(v, (int, float)) and not pd.isna(v))
    if len(values) < 5 or values[0] == values[-1]:
        return []
    # Shallow to deep, matching the emit order above. Deliberately low-saturation: this is a
    # background behind monospace figures, and anything stronger makes the numbers themselves
    # harder to read than no shading at all.
    shades = ["#26241d", "#3d2a1d", "#5c2c1c", "#7d2d1e"]
    if invert:
        operator = "<="
        edges = [values[int(len(values) * f)] for f in (0.50, 0.30, 0.15, 0.05)]
    else:
        operator = ">="
        edges = [values[int(len(values) * f) - 1] for f in (0.50, 0.70, 0.85, 0.95)]
    out, previous = [], None
    for shade, edge in zip(shades, edges, strict=True):
        # A tie across a boundary would emit two rules with the same threshold, and the deeper one
        # would win for every row the shallower one covers. Skipped rather than emitted, so a
        # column with few distinct values gets fewer bands instead of one flat block of colour.
        if edge == previous:
            continue
        previous = edge
        out.append({"if": {"filter_query": f"{{{column}}} {operator} {edge}",
                           "column_id": column},
                    "backgroundColor": shade, "color": TEXT})
    return out


def heated(rows, *columns, invert=()):
    """TABLE_STYLE with shading applied to some of its columns, as kwargs to spread.

    The combining is done HERE so no caller has to remember it. A caller passing
    `style_data_conditional=heat_cells(...)` by hand would silently drop the odd-row striping that
    every other table on the page has, and the table would read as a different component.
    """
    # TABLE_STYLE's values are a union of several shapes, so `style_data_conditional` reads back
    # as that whole union rather than as the list it is, making both the read and the write below
    # errors against it.
    style: dict[str, Any] = dict(TABLE_STYLE)
    conditional = list(style["style_data_conditional"])
    for column in columns:
        conditional += heat_cells(rows, column, invert=column in invert)
    style["style_data_conditional"] = conditional
    return style


def toward_background(color: str, amount: float) -> str:
    """A colour mixed towards the page background. `amount` 0 keeps it, 1 is the background.

    Used to shade a treemap's leaves by rank inside their parent. Written here rather than reached
    for from a colour library because it is six lines and this app has no colour dependency: every
    surface sets its own values, which is the reason the dark theme holds together at all.
    """
    amount = min(max(float(amount), 0.0), 1.0)
    base = BG.lstrip("#")
    top = color.lstrip("#")
    mixed = [round(int(top[i:i + 2], 16) * (1 - amount) + int(base[i:i + 2], 16) * amount)
             for i in (0, 2, 4)]
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def treemap(labels, parents, values, *, title, height=420, colors=None, hover=None) -> go.Figure:
    """A proportional area chart with two levels: category, then the items inside it.

    Built for the case a stacked bar cannot serve. The composition bar reads well for eight
    categories; the configuration behind it holds 321 skills, and 321 segments of a bar is a
    solid block. Area gives every item a size a reader can compare without a legend and without
    scrolling a table sorted by the column they happened to think of.

    `branchvalues="total"` means a parent's value is its own, not the sum of its children, so the
    caller must pass parent totals that really are the sum. Passing "remainder" instead hides
    mismatches by inventing a residual slice, which is exactly the kind of quiet correction this
    app does not make anywhere else.
    """
    fig = go.Figure(go.Treemap(
        labels=labels, parents=parents, values=values, branchvalues="total",
        marker=dict(colors=colors, line=dict(color=BG, width=1)) if colors else
        dict(line=dict(color=BG, width=1)),
        textinfo="label+value+percent parent",
        hovertemplate=(hover or "%{label}<br>%{value:,} tokens<br>"
                                "%{percentParent} of %{parent}<extra></extra>"),
        tiling=dict(pad=2),
    ))
    fig.update_layout(title=title, title_font=dict(color=TEXT, size=13))
    return dark_fig(fig, height)


def empty_fig(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=MUTED, size=13, family=MONO))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return dark_fig(fig, height=300)


# What a column means, where the meaning is not obvious from its name.
#
# Every entry here replaces prose that used to sit above a table. The rule for adding one: if a
# reader could take the number at face value and be wrong, the column needs a line. If the name
# already says it, it does not.
#
# These are deliberately blunt. A tooltip that hedges is worse than no tooltip, because the reader
# still has to go and check.
COLUMN_HELP = {
    # The Messages table. Both of these had no explanation at all, which is how the first one went
    # unnoticed: it says "user" for a directory listing.
    "role": ("The transcript RECORD's own type, which is a transport fact and not authorship. "
             "Claude Code files a tool result as a record of type `user`, so this column says "
             "`user` whether a person typed it or a tool produced it. `Written By` is the column "
             "that answers who."),
    "type": ("What actually wrote this message: `typed` by a person, `tool_result` produced by a "
             "tool, `compact_summary` written by the compactor, `attachment` for an image or "
             "document, or `assistant`. `unknown` means the transcript it came from is no longer "
             "on this machine, so nothing can say."),
    # All sessions
    "turns": ("Every transcript row for this session, subagent work included, whichever way the "
              "main thread / include subagents radio is set. The Session tab respects that radio, "
              "so its row count for the same session is usually much smaller."),
    "current": ("Where the window sat at this session's newest recorded call. `peak` is the "
                "high-water mark; the gap between them is what a compaction took out."),
    "peak": "The highest resident total this session ever reached, not where it sits now.",
    "compactions": "How many times this session's context window was compacted.",
    "section": ("Read from disk, not stored: the working directory, the entrypoint, and whether "
                "the transcript file still exists on this machine."),
    "project": ("The working directory. A path ending in \\archived is a chat the desktop app "
                "has archived. An unmarked path means NOT KNOWN to be archived: the flag is only "
                "readable for chats the app kept a record of, which is a minority of these."),
    "last active": "The last recorded activity, not when the session was created.",
    "title": ("Read from the transcript. Imported sessions carry a generated Imported_YYYYMMDD "
              "name instead, because the only titler there is walks this machine's transcripts."),

    # Breakdown and the probe detail
    "percent": "Share of the whole context window, not of the resident total.",
    "items": "How many items the calibrated baseline counted in this category.",
    "pct_of_kind": "This item's share of its own category, not of the whole window.",
    "source": ("Where it comes from: built-in ships with Claude Code, userSettings is one you "
               "wrote, plugin arrives with a plugin. Only the last two are yours to remove."),
    "server": ("The MCP server the tool belongs to. A server is the unit you can remove from your "
               "config; a single tool is not."),
    "loaded": ("0 means the tool was deferred when this reading was taken, so it cost nothing "
               "yet. That is a statement about residency, NOT a measurement that its schema is "
               "free: the same tool loaded is worth its full schema."),
    "not loaded": ("Blank where the kind carries no residency flag at all, rather than 0, which "
                   "would claim a measurement that was never made."),
    "items recorded": "What the probe actually saw, which is one session's configuration.",
    "items in baseline": ("What the calibration counted. A gap means the two readings saw "
                          "different configurations, which is expected for MCP tools and would "
                          "not be for skills."),
    "tokens recorded": "Measured by the probe. 0 can mean deferred rather than free; see `loaded`.",
    "tokens in baseline": "Taken from the calibrated baseline, not from this probe.",

    # Compare
    "basis": ("Whether this row is a total, which grows with the size of the population, or a "
              "per-unit figure. Comparing one session against a cohort makes every total larger "
              "for the cohort by construction."),
    "B / A": "B divided by A. Blank where A is zero.",
    "verdict": "Which arm is better, where for a cost metric lower is better.",
    "unit": "What the two numbers are counted in.",

    # Cost / waste
    "reads": ("Times this exact target was read. Counts subagent calls: they make almost every "
              "tool call, so a main-thread-only reading of this column is close to zero."),
    "variants": "Distinct spellings of the same target, which is how a re-read hides.",
    "result_bytes": "Bytes of tool RESULT, not tokens. This store records no per-call token count.",

    # Repeated inputs across sessions. Every one of these describes a row grouped by input_sha1,
    # which is a hash of the tool's arguments, so "the same" here means byte-identical rather
    # than similar.
    "sessions": ("How many DIFFERENT sessions issued this identical input. Two or more is the "
                 "whole filter for this table: one session repeating itself is the table above."),
    "calls": "Total calls with this exact input, across every session that made one.",
    "beyond_one_each": ("Calls minus sessions: how much of the repetition is WITHIN sessions "
                        "rather than across them. A row of 68 sessions and 0 here is 68 separate "
                        "sessions each asking once, which is a different problem from one session "
                        "asking 68 times."),
    "first_seen": "Earliest call with this input, anywhere in the population.",
    "agent": ("Which KIND of subagent the Agent call asked for, read from the call's own input. "
              "\"(not recorded)\" means the call named none and took the default: the transcript "
              "recorded the omission, so this reports the omission rather than the default."),
    "errors": "Calls whose result block was flagged as an error.",
    "last_seen": "Latest call with this input. Still recent means the repetition is not history.",
    "target": ("The file or resource the call named. BLANK where the tool has no target, such as "
               "Bash or ToolSearch: the store keeps a hash of the input, never the input itself, "
               "so those rows are identified by their tool and their shape alone."),

    # Compactions
    "dropped": "Tokens the compaction discarded, as the transcript recorded it.",
    "pre_tokens": "Resident tokens immediately before the compaction.",
    "post_tokens": "Resident tokens immediately after it.",
}


# ---------------------------------------------------------------------------
# Human names
# ---------------------------------------------------------------------------
# Every table in this app declared `name == id`, so a reader saw `ts`, `pre_tokens` and
# `fitted_window`. That was never a regression; it was work nobody had done. These are the names a
# READER sees. The id stays the key for lookups, tooltips and the SQL shown beneath the table.
#
# FAITHFUL, NOT REWRITTEN. `pre_tokens` becomes "Pre Tokens", not "Tokens Before". The query that
# built the table is one click away under every table, and a label that no longer matches the
# column in that query costs more than the plainer wording buys.
#
# Only the ones the generic rule below gets WRONG are listed. Anything absent is formatted by rule,
# so a new column gets a reasonable name without an edit here.
COLUMN_LABEL = {
    # The one that prompted this. A timestamp column called `ts` tells a reader nothing.
    "ts": "Date & Time",
    # NOT "Turns". It counts transcript ROWS, and a streamed assistant message is written as
    # several rows carrying one request id: measured per session it runs 1.86x to 3.98x the number
    # of API calls. The tooltip always said "every transcript row"; the header said "Turns", and
    # the header is what a reader takes the number's meaning from.
    "turns": "Transcript Rows",
    # NOT "Role" and "Type". Both were the transcript record's own `type` field, so a directory
    # listing and a question both read "user" and nothing on the page distinguished them.
    # Measured: 86.5% of the records typed 'user' were tool results.
    "role": "Record Type",
    "type": "Written By",
    "est_usd": "Est. USD",
    "duration_ms": "Duration (ms)",
    "auto_compact_threshold": "Auto-Compact Threshold",
    "pct_of_kind": "% of Kind",
    # Identifiers. The suffix is for a schema, not for a person reading a column heading.
    "session_id": "Session",
    "probe_id": "Probe",
    # A project IS a working directory in this store, and `cwd` is the schema's word for it, not a
    # reader's. Every other surface in this app already says "Project".
    "cwd": "Project",
    "excluded_at": "Date & Time Excluded",
    "id": "ID",
    "ok": "OK",
    # Already written for a reader; the rule would only mangle the casing.
    "B / A": "B / A",
    "A": "A",
    "B": "B",
}

# Words that stay lowercase inside a title, unless they lead it. Without this the rule produces
# "Blocked At" and "Goes To", which read as though something was capitalised by a machine.
_MINOR = {"at", "of", "to", "vs", "in", "for", "and", "on", "per", "by", "a", "an", "the"}

# Words that are acronyms, not words. Without this the rule produces "Api Calls" and "Est. Usd",
# which reads as a machine having capitalised something it did not understand.
_ACRONYMS = {"api", "id", "ok", "usd", "sql", "cli", "csv", "pdf", "url", "uuid", "mcp", "ms"}

# One heading per table, for the ones that HAVE an id. `tbl-reread` is a DOM id; "Files Read More
# Than Once" is what the table is. Anonymous tables get no heading rather than an invented one.
TABLE_LABEL = {
    "tbl-compactions": "Compactions",
    "tbl-session": "Sessions",
    "tbl-messages": "Messages",
    "tbl-findings": "Findings",
    "tbl-reread": "Session Rereads",
}


# One line per tab, saying what it answers. Same idea as COLUMN_HELP and the same discipline: it is
# only worth a line if the label does not already say it.
#
# These are TOOLTIPS, not body text. A sentence explaining a tab has no business taking a paragraph
# of vertical space above the numbers every time the tab is opened; it belongs on the thing it
# describes, which is the tab itself.
TAB_HELP = {
    "tab-summary": "Findings worth acting on, and what the whole store adds up to.",
    "tab-sessions": "Each listed session as a row and a point: transcript rows against the peak "
                    "it reached.",
    "tab-session": "One session's context growing turn by turn, with every compaction marked.",
    "tab-compactions": "Every compaction on record, what triggered it, and what it discarded.",
    "tab-window": "What is in the context window right now, item by item, as area.",
    "tab-cost": "What was paid for twice: re-reads, repeated inputs, and the estimated cost.",
    "tab-compare": "Two chats or two populations, measured the same way, side by side.",
    "tab-diagnostics": "Is the capture healthy, and does the window model agree with reality.",
}


def tab_help(tab_id) -> str:
    return TAB_HELP.get(tab_id, "")


def column_label(column_id) -> str:
    """The name a reader sees for a column. Never the raw id."""
    if column_id in COLUMN_LABEL:
        return COLUMN_LABEL[column_id]
    words = str(column_id).replace("_", " ").split()
    if not words:
        return str(column_id)
    def cased(word, first):
        bare = word.lower().strip(".")
        if bare in _ACRONYMS:
            return word.upper()
        # A SINGLE LETTER IS A NAME, NOT A MINOR WORD. The Compare table's ratio column is
        # "B / A" and the minor-word rule read that trailing A as the article, so the header
        # rendered "B / a" over a column of ratios between two arms called A and B.
        if len(bare) == 1 and bare.isalpha():
            return word.upper()
        if not first and bare in _MINOR:
            return word.lower()
        return word.capitalize()

    return " ".join(cased(w, i == 0) for i, w in enumerate(words))


# THE MARKER A FAILED TAB CARRIES, defined once because it had two producers and five readers.
#
# `c4x/ui/callbacks/navigation.py` and `c4x/ui/callbacks/window.py` both build the apology panel a
# raised tab renders instead of content, and both spelled this string out. `tools/table_audit.py`
# kept its own copy to detect it. Two producers of one fact is how a rename lands on one of them:
# the audits would have gone quiet and reported PASS over every broken tab, which is precisely the
# failure the audits exist to catch.
#
# The TESTS keep their own literals on purpose. A test that imports the constant it is checking
# cannot notice the constant changing, and what a reader sees on screen is worth asserting
# independently of what the code calls it.
RENDER_FAILED = "could not be rendered"


def table_label(table_id):
    """The heading for a table, or None for one with no id worth showing.

    `(anonymous)` is what `extract.describe()` calls a table with no DOM id. It is a placeholder,
    not a name, and printing it would put the word down the page five times on the Cost tab.
    """
    if not table_id or table_id == "(anonymous)":
        return None
    # The fallback strips the DOM prefix and reads the rest as words, so a table added later gets a
    # sane heading without an edit here. Hyphens are separators in a DOM id, not punctuation.
    return TABLE_LABEL.get(table_id) or column_label(
        table_id.removeprefix("tbl-").replace("-", " "))


def header_help(cols, extra=None):
    """tooltip_header for a DataTable, for whichever of `cols` have something worth saying.

    Returns only the columns that HAVE help, so a table gains tooltips on the columns that need
    them and none on the rest, rather than a tooltip everywhere repeating the column name.
    """
    help_for = dict(COLUMN_HELP)
    help_for.update(extra or {})
    names = [c if isinstance(c, str) else c.get("id") for c in cols]
    return {name: {"value": help_for[name], "type": "markdown"}
            for name in names if name in help_for}


def numeric_columns(cols, numeric, formats=None):
    """Column specs that keep numbers as numbers while still reading well.

    Mapping a token count through fmt_tokens produced "997.8k", a STRING. That sorted
    lexicographically, so 9 came after 80,000, and once these tables gained a CSV export it put
    formatted text in the file instead of values. Dash formats the display and leaves the
    underlying value numeric, which is what sorting and export need.

    Every table in this app inherits export_format="csv" from TABLE_STYLE, so this applies to all
    of them, not only the ones that also sort. `formats` overrides the default grouping for a
    column that needs its own precision, such as a percentage.
    """
    formats = formats or {}
    out = []
    for c in cols:
        if c in numeric:
            out.append({"name": c, "id": c, "type": "numeric",
                        "format": formats.get(c) or Format(group=Group.yes)})
        else:
            out.append({"name": c, "id": c})
    return out


def chart_note(text, for_id: str | None = None, style: dict | None = None):
    """The sentence that explains a chart, marked so it reaches the chart over the API.

    A table's heading and note are paired by position, which works because they are always written
    directly above the rows. A chart's caption is not: it is written above the chart on one tab and
    below it on four others, and on the Window tab it sits between a table and the chart it
    describes. Ten of them therefore reached the browser as loose paragraphs, and two more were
    picked up by the forward-only pairing and shown as the hover of a TABLE they say nothing about.

    So the pairing is declared rather than guessed. `for_id` names the chart exactly and is the
    right answer whenever the caption is not a sibling of its `dcc.Graph`; without it the caption
    binds to the nearest chart in its own list, preferring the one above it, which is where a
    caption written below a chart belongs. A caption that binds to nothing is caught by the gate on
    loose prose rather than disappearing.

    Dash renders it exactly as `SECTION_NOTE` always did, so the page it was written for is
    unchanged; `style` overrides that for the few that are warnings.
    """
    return _marked_note("chart-note", text, for_id, style)


def table_note(text, for_id: str | None = None, style: dict | None = None):
    """The sentence that explains a table when it is written BELOW the rows.

    A heading and a note written above a table are paired by position and need no mark. Six
    captions in this app are written under their table instead, because that is where a "click a
    row to..." instruction belongs on the page, and a forward-only pairing cannot see them: all six
    reached the browser as loose paragraphs at the bottom of a tab.

    Use this only for the ones written below. Above the table, keep writing a plain `SECTION_NOTE`:
    the pairing already handles it and a mark there would be noise.
    """
    return _marked_note("table-note", text, for_id, style)


def empty_panel(title: str, note: str):
    """A block that names what would be here and says why it is not.

    Not prose, and not a hover. "Multi-Session Input" followed by
    "Not answerable with a single session selected" is the most useful thing on that part of the
    page when a session is selected: it distinguishes "there are none" from "this question cannot
    be asked from here", and only one of those is true. Hiding it behind a glyph on a heading that
    does not exist would lose it, and printing it loose is the wall this app just stopped printing.

    So it travels as its own kind and the page draws it as a placeholder where the table would be.
    Dash renders exactly what it rendered before.
    """
    return html.Div([
        html.Div(title, style=SECTION_HEAD),
        html.Div(note, style=SECTION_NOTE),
    ], className="empty-panel")


def about_note(children, style: dict | None = None):
    """What this view IS, as opposed to what any one chart or table on it shows.

    Some sentences are about the page. "Nothing on this tab answers to the header selection",
    "A reading describes the moment it was taken", a sub-panel's statement of which half of the
    window it covers: none of them belong on a chart's hover, because they are not about a chart,
    and all of them were printing as loose paragraphs for want of anywhere else.

    They go where a reader already looks to ask what they are reading: the population chip in the
    header, which states what the numbers cover and now states this too. The Dash page draws them
    exactly where they are written.
    """
    return html.Div(children, className="about-note", style={**SECTION_NOTE, **(style or {})})


def _marked_note(mark: str, text, for_id: str | None, style: dict | None):
    # THE TARGET RIDES IN THE CLASS, NOT IN THE ID. An id must be unique in a Dash page, so binding
    # by id allowed exactly one caption per chart and the Window tab needs three on one of them.
    classes = f"{mark} {mark}-for-{for_id}" if for_id else mark
    return html.Div(text, className=classes, style={**SECTION_NOTE, **(style or {})})


def accordion(title: str, sub: str, children, open_by_default: bool = False):
    """A collapsible block. Native details/summary, so it needs no callback and no state.

    Every store-wide number lives inside one of these on the Summary tab. That is the whole point
    of the restructure: a figure that describes the entire store is never rendered beside a figure
    that describes one session, where a reader has to guess which is which.
    """
    return html.Details([
        html.Summary([
            html.Span(title, style={"color": TEXT, "fontSize": "13px", "fontWeight": 600}),
            # MARKED, so the API can tell the two apart again. Dash keeps them apart with 13px/600
            # against 11px/muted and two literal spaces; `extract.texts()` strips and joins, so over
            # the API the pair arrived as one string and the page drew "Recommendation(s) 6
            # finding(s), each with an action" as a heading, with no hover, because a summary has
            # only one slot. The class says which half is the caption.
            html.Span(f"  {sub}", className="accordion-sub",
                      style={"color": MUTED, "fontSize": "11px", "marginLeft": "8px"}),
        ], style={"cursor": "pointer", "padding": "8px 10px", "background": PANEL,
                  "border": f"1px solid {BORDER}", "borderRadius": "6px",
                  "fontFamily": MONO, "listStyle": "none"}),
        html.Div(children, style={"padding": "12px 10px 4px 10px"}),
    ], open=open_by_default, style={"marginBottom": "8px"})

"""How the app looks: colours, shared styles, formatters, and the small shared builders.

The leaf of the dependency graph. Nothing here knows what a session is or how to reach the store,
which is what lets every other module import it without a cycle.

Split out of app.py, which had grown to 3124 lines. The colours were always in one place; the
builders that use them were scattered through it.
"""
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
    for shade, edge in zip(shades, edges):
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
    style = dict(TABLE_STYLE)
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
    return "#%02x%02x%02x" % tuple(mixed)


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
    "B vs A": "B divided by A. Blank where A is zero.",
    "verdict": "Which arm is better, where for a cost metric lower is better.",
    "unit": "What the two numbers are counted in.",

    # Cost / waste
    "reads": ("Times this exact target was read. Counts subagent calls: they make almost every "
              "tool call, so a main-thread-only reading of this column is close to zero."),
    "variants": "Distinct spellings of the same target, which is how a re-read hides.",
    "result_bytes": "Bytes of tool RESULT, not tokens. This store records no per-call token count.",

    # Compactions
    "dropped": "Tokens the compaction discarded, as the transcript recorded it.",
    "pre_tokens": "Resident tokens immediately before the compaction.",
    "post_tokens": "Resident tokens immediately after it.",
}


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


def accordion(title: str, sub: str, children, open_by_default: bool = False):
    """A collapsible block. Native details/summary, so it needs no callback and no state.

    Every store-wide number lives inside one of these on the Summary tab. That is the whole point
    of the restructure: a figure that describes the entire store is never rendered beside a figure
    that describes one session, where a reader has to guess which is which.
    """
    return html.Details([
        html.Summary([
            html.Span(title, style={"color": TEXT, "fontSize": "13px", "fontWeight": 600}),
            html.Span(f"  {sub}", style={"color": MUTED, "fontSize": "11px", "marginLeft": "8px"}),
        ], style={"cursor": "pointer", "padding": "8px 10px", "background": PANEL,
                  "border": f"1px solid {BORDER}", "borderRadius": "6px",
                  "fontFamily": MONO, "listStyle": "none"}),
        html.Div(children, style={"padding": "12px 10px 4px 10px"}),
    ], open=open_by_default, style={"marginBottom": "8px"})

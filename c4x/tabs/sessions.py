"""The sessions tab.

Every session as a sortable, filterable table. Clicking a row sets the header selection.
"""
import plotly.graph_objects as go
from dash import dcc, html

from c4x.dash_compat import DataTable
from c4x.store import SESSION_TURN_FLOOR, cohort_sessions, session_rows
from c4x.theme import (
    ACCENT,
    GOOD,
    MUTED,
    TABLE_STYLE,
    TEXT,
    VIOLET,
    WARN,
    chart_note,
    dark_fig,
    empty_fig,
    header_help,
    heat_cells,
    numeric_columns,
    stat_card,
    table_note,
)

# One colour per section, assigned by sorted position so the same section keeps the same colour
# across renders. Taken from the palette rather than from a Plotly default, which would ignore the
# page theme and put a light-mode qualitative scale on a dark background.
SECTION_COLORS = (ACCENT, GOOD, VIOLET, WARN, "#e8590c", MUTED)


def archived_counts(df):
    """(marked, recorded as not archived, no record at all) for these sessions."""
    if df.empty or "archived" not in df:
        return 0, 0, 0
    return (int((df["archived"] == True).sum()),        # noqa: E712 - tri-state, not truthiness
            int((df["archived"] == False).sum()),        # noqa: E712 - None must not count here
            int(df["archived"].isna().sum()))


def sessions_scatter(rows):
    """Every session as a point: how long it ran against how full it got.

    The table is a lookup tool and a bad survey. Sixteen rows at a time cannot show that this
    store's sessions form two groups, a dense cluster of short ones and a thin tail that ran for
    thousands of turns, or that length and peak are only loosely related: a session can run 900
    turns and never fill the window, and another can hit the ceiling in fifty.

    Coloured by section, which is the one attribute here that comes from disk rather than from the
    store, so the chart also shows whether the outliers are real project work or leftovers.

    Both axes are logarithmic. Turns span 5 to 59,864 in this store and peaks span three orders of
    magnitude; on a linear axis every point but a dozen lands in one corner, and the chart becomes
    a picture of the outliers with the population as a smudge behind them.
    """
    if len(rows) < 2:
        return empty_fig("Not enough sessions to plot")
    fig = go.Figure()
    for index, section in enumerate(sorted({r["section"] for r in rows})):
        group = [r for r in rows if r["section"] == section]
        fig.add_trace(go.Scatter(
            x=[max(1, r["turns"]) for r in group],
            y=[max(1, r["peak"]) for r in group],
            mode="markers", name=f"{section} ({len(group):,})",
            marker=dict(size=7, opacity=0.72, color=SECTION_COLORS[index % len(SECTION_COLORS)],
                        line=dict(width=0)),
            # session_id rides along as customdata[3] so a box selection can be turned back into
            # rows. Plotly reports a selection as point indices WITHIN A TRACE, and there is one
            # trace per section here, so an index alone cannot identify a session.
            customdata=[[r["title"], r["project"], r["compactions"], r["session_id"]]
                        for r in group],
            hovertemplate="%{customdata[0]}<br>%{customdata[1]}<br>"
                          "%{x:,} turns, peak %{y:,}<br>"
                          "%{customdata[2]} compactions<extra></extra>",
        ))
    # "sessions" alone read as all of them. It is the LISTED ones, and the chart is the surface
    # where a reader is most likely to take the count as the population.
    fig.update_layout(title="Transcript Rows Against Peak Resident Tokens",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="Turns (Log)", yaxis_title="Peak Resident Tokens (Log)")
    fig.update_xaxes(type="log")
    fig.update_yaxes(type="log")
    return dark_fig(fig, 400)


def sessions_table_layout(session_id=None, scope="main", cohort=None):
    """Browse every session. Selecting a row sets the header selection.

    A table rather than a long dropdown: 1,323 sessions sorted by peak tokens interleaved every
    project and could not be scanned. Sorted by section, then project, then most recently active,
    which is the order the desktop sidebar uses.
    """
    df = session_rows()
    ids = cohort_sessions(cohort)
    if ids:
        df = df[df["session_id"].isin(ids)]
    counts = df["section"].value_counts().to_dict() if not df.empty else {}
    rows = []
    for r in df.itertuples():
        rows.append({
            "session_id": r.session_id,
            "section": r.section,
            "title": r.title,
            "project": r.project,
            "last active": str(r.last_ts or "")[:16].replace("T", " "),
            "turns": int(r.turns),
            "peak": int(r.peak or 0),
            "current": int(r.current or 0),
            "compactions": int(r.compactions or 0),
        })
    marked, known_not, unknown = archived_counts(df)
    return html.Div([
        # THE COUNTS ARE CARDS. This tab opened with three grey paragraphs and no figures at all,
        # while every number in those paragraphs is exactly what a card is for. The sentences that
        # remain say what to DO, and they now say it on the thing they are about.
        html.Div([
            stat_card("listed sessions", f"{len(rows):,}",
                      sub=f"{SESSION_TURN_FLOOR} or more turns"),
            # COUNTS PROJECTS, which is what it says. It counted SESSIONS whose section is
            # "Projects" and called them projects, so a store with 518 distinct working directories
            # reported "Projects 269"; on the committed fixture it read "Projects 0" over a caption
            # saying 49 sessions were imported, a card contradicting itself in two lines. The label
            # was written first and the number was whatever was already in hand.
            stat_card("projects", f"{df['project'].nunique():,}" if not df.empty else "0",
                      sub="distinct working directories"),
            # SHORT ENOUGH TO READ. A caption is one line in a 144px box and the browser
            # truncates the rest, so a caption that runs to 367px is a caption whose second half
            # only exists on a hover nothing says is there.
            stat_card("archived", f"{marked:,}" if marked or known_not or unknown else "-",
                      sub=(f"{unknown:,} with no desktop record" if unknown
                           else "all accounted for")),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                  "marginBottom": "12px"}),
        # Written above the chart and above the table, and about the table, so it is marked: the
        # chart between them resets the forward pairing, and this reached the reader as a
        # paragraph at the top of the tab instead of as the table's hover.
        # The section breakdown lives HERE, on the table whose `section` column it describes,
        # rather than crammed into a card caption where the browser cut it in half.
        table_note("Click a row to make it the header selection. Sections come from disk: the "
                   "working directory, the entrypoint, and whether the transcript still exists. "
                   + (" · ".join(f"{k} {v:,}" for k, v in counts.items()) + "."
                      if counts else ""),
                   for_id="tbl-session"),
        # The mode bar stays ON here, unlike every other chart in the app, because it carries the
        # box and lasso tools that drive the cross-filter below. A hidden mode bar would leave the
        # feature reachable only by a drag nobody was told about.
        dcc.Graph(id="fig-sessions", figure=sessions_scatter(rows),
                  config={"displayModeBar": True, "displaylogo": False,
                          "modeBarButtonsToRemove": ["autoScale2d", "toggleSpikelines"]}),
        # THE CHART'S OWN CAPTION, and it says so. The forward pairing carried this over the
        # chart and served it as the hover of the table below, which opens "One point per session"
        # over a table of rows.
        chart_note(
            f"{len(rows):,} listed sessions, one point each. The table below holds the same rows "
            "sixteen at a time, which is the right shape for looking one session up and the wrong "
            "shape for seeing that a handful of them are unlike all the others. Shaded cells in "
            "the table mark the same outliers in whatever order you have sorted it into.",
            for_id="fig-sessions"),
        # Every row, unfiltered, so the cross-filter can narrow AND restore without a query. The
        # table's own `data` is the filtered view, so it cannot be the source: reading it back
        # would make each selection narrow the previous one and never widen.
        dcc.Store(id="sessions-rows", data=rows),
        html.Div(id="sessions-filter-note"),
        DataTable(
            id="tbl-session",
            columns=(_cols := numeric_columns(
                ["section", "title", "project", "last active", "turns", "current", "peak",
                 "compactions"],
                {"turns", "current", "peak", "compactions"})),
            tooltip_header=header_help(_cols),
            data=rows,
            hidden_columns=["session_id"],
            page_size=16,
            sort_action="native",
            # This table does not spread TABLE_STYLE, so it needs the tooltip setting explicitly.
            # It carries the longest tooltip in the app and was the one still hiding it after two
            # seconds, which is less time than the sentence takes to read.
            tooltip_duration=None,
            filter_action="native",
            row_selectable="single",
            cell_selectable=False,
            export_format="csv",
            export_headers="display",
            style_table={"overflowX": "auto"},
            style_cell_conditional=[
                {"if": {"column_id": "title"}, "minWidth": "240px", "maxWidth": "360px",
                 "whiteSpace": "normal"},
                {"if": {"column_id": "project"}, "minWidth": "200px", "maxWidth": "320px",
                 "whiteSpace": "normal"},
            ],
            # ORDER MATTERS, and the three rules here do different jobs. Striping first. Then the
            # shading, which sets a background AND a text colour. Then the muted rule for
            # non-project rows, which sets ONLY a colour, so it wins the text of a shaded cell
            # while leaving its background: a session outside Projects stays visibly outside
            # Projects even where it is one of the largest in the store.
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"},
                *heat_cells(rows, "turns"),
                *heat_cells(rows, "peak"),
                *heat_cells(rows, "compactions"),
                {"if": {"filter_query": '{section} != "Projects"'}, "color": MUTED},
            ],
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            style_cell=TABLE_STYLE["style_cell"],
            style_header=TABLE_STYLE["style_header"],
        ),
    ])

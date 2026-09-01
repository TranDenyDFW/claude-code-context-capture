"""The sessions tab.

Every session as a sortable, filterable table. Clicking a row sets the header selection.
"""
import plotly.graph_objects as go
from dash import dash_table, dcc, html

from c4x.store import cohort_sessions, session_rows
from c4x.theme import (
    ACCENT,
    GOOD,
    MUTED,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    VIOLET,
    WARN,
    dark_fig,
    empty_fig,
    header_help,
    heat_cells,
    numeric_columns,
)

# One colour per section, assigned by sorted position so the same section keeps the same colour
# across renders. Taken from the palette rather than from a Plotly default, which would ignore the
# page theme and put a light-mode qualitative scale on a dark background.
SECTION_COLORS = (ACCENT, GOOD, VIOLET, WARN, "#e8590c", MUTED)


def archived_note(df):
    """How much of the archived flag is actually known, in words, on the page.

    An archived chat has \\archived appended to its path so it sorts beside the project it came
    from rather than into a section of its own. The flag is real and readable, but it is knowable
    only for chats the desktop app has a record of, and that is a minority here. Without this
    sentence an unmarked path reads as "not archived" when it usually means "no record".
    """
    if df.empty or "archived" not in df:
        return ""
    marked = int((df["archived"] == True).sum())          # noqa: E712 - tri-state, not truthiness
    known_not = int((df["archived"] == False).sum())       # noqa: E712 - None must not count here
    unknown = int(df["archived"].isna().sum())
    # The COUNTS stay on the page; what the marker MEANS moved to the `project` column tooltip.
    # A reader needs to know that 226 of these are unknown without hovering to find out.
    return (f"Archived: {marked:,} marked, {known_not:,} recorded as not archived, "
            f"{unknown:,} with no desktop record at all.")


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
    fig.update_layout(title=f"{len(rows):,} listed sessions: transcript rows against peak "
                            f"resident tokens",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="turns (log)", yaxis_title="peak resident tokens (log)")
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
    breakdown = " · ".join(f"{k} {v:,}" for k, v in counts.items()) or "none"
    return html.Div([
        html.Div(f"{len(rows):,} sessions with 5 or more turns. {breakdown}.", style=SECTION_NOTE),
        html.Div("Click a row to make it the header selection. Sections come from disk: the "
                 "working directory, the entrypoint, and whether the transcript still exists.",
                 style=SECTION_NOTE),
        # This paragraph is now the `turns` column tooltip. It was written here because there was
        # nowhere else to put it; the gap it describes is real (one session holds 59,864 rows of
        # which 690 are main thread) and the tooltip states it on the column it concerns.
        html.Div(archived_note(df), style=SECTION_NOTE),
        # The mode bar stays ON here, unlike every other chart in the app, because it carries the
        # box and lasso tools that drive the cross-filter below. A hidden mode bar would leave the
        # feature reachable only by a drag nobody was told about.
        dcc.Graph(id="fig-sessions", figure=sessions_scatter(rows),
                  config={"displayModeBar": True, "displaylogo": False,
                          "modeBarButtonsToRemove": ["autoScale2d", "toggleSpikelines"]}),
        html.Div(
            "One point per session. The table below holds the same rows sixteen at a time, "
            "which is the right shape for looking one session up and the wrong shape for seeing "
            "that a handful of them are unlike all the others. Shaded cells in the table mark "
            "the same outliers in whatever order you have sorted it into.",
            style=SECTION_NOTE),
        # Every row, unfiltered, so the cross-filter can narrow AND restore without a query. The
        # table's own `data` is the filtered view, so it cannot be the source: reading it back
        # would make each selection narrow the previous one and never widen.
        dcc.Store(id="sessions-rows", data=rows),
        html.Div(id="sessions-filter-note"),
        dash_table.DataTable(
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

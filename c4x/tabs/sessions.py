"""The sessions tab.

Every session as a sortable, filterable table. Clicking a row sets the header selection.
"""
from dash import dash_table, html

from c4x.store import cohort_sessions, session_rows
from c4x.theme import MUTED, SECTION_NOTE, TABLE_STYLE, header_help, numeric_columns


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
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"},
                {"if": {"filter_query": '{section} != "Projects"'}, "color": MUTED},
            ],
            style_filter={"backgroundColor": "#ffffff", "color": "#10141a"},
            style_cell=TABLE_STYLE["style_cell"],
            style_header=TABLE_STYLE["style_header"],
        ),
    ])

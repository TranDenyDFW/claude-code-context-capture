"""The sources tab.

Where the context came from: harness injection, tool responses, lifecycle events.
"""
import plotly.graph_objects as go
from dash import dcc, html

from c4x.panels import evidence_block
from c4x.store import q, scoped
from c4x.theme import (
    ACCENT,
    MUTED,
    SECTION_HEAD,
    SECTION_NOTE,
    TEXT,
    dark_fig,
    fmt_bytes,
    stat_card,
)


# ---- Sources --------------------------------------------------------------
def sources_layout(session_id=None, scope="main", cohort=None):
    """What ENTERS the window, as opposed to what the window currently holds.

    The Breakdown tab answers "what is in there now" and the context tooltip answers it live. This
    answers the question neither can: what has been arriving, from where, over the whole history.
    Three tables carry it and nothing read them until now - `attachments` (context injected by the
    harness rather than typed by anyone), `hook_events` (the only per-tool byte accounting that
    exists on the desktop entrypoint, where the status line does not run), and `record_types` (a
    census of every record shape the transcripts contain).
    """
    _w, sid_args = scoped(session_id, "all", cohort=cohort)
    sid_where = ("WHERE 1=1 " + _w) if _w else ""
    sql = {}
    sql["att"] = f"""SELECT type AS kind, SUM(n) AS occurrences,
                       COUNT(DISTINCT session_id) AS sessions
                FROM attachments {sid_where} GROUP BY type ORDER BY SUM(n) DESC"""
    att = q(sql["att"], sid_args)
    hw = _w
    sql["hooks"] = f"""SELECT tool_name AS tool, COUNT(*) AS calls,
                         SUM(COALESCE(tool_response_bytes,0)) AS response_bytes,
                         SUM(COALESCE(tool_input_bytes,0))    AS input_bytes
                  FROM hook_events WHERE tool_name IS NOT NULL {hw}
                  GROUP BY tool_name ORDER BY SUM(COALESCE(tool_response_bytes,0)) DESC LIMIT 40"""
    hooks = q(sql["hooks"], sid_args)
    sql["ev"] = f"""SELECT event, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions,
                      MIN(captured_at) AS first_seen, MAX(captured_at) AS last_seen
               FROM hook_events {sid_where} GROUP BY event ORDER BY COUNT(*) DESC"""
    ev = q(sql["ev"], sid_args)
    sql["rec"] = "SELECT type AS record_type, n FROM record_types ORDER BY n DESC"
    rec = q(sql["rec"])

    if att.empty and ev.empty and rec.empty:
        return html.Div("Nothing captured yet. Run node tools/harvest.mjs.",
                        style={"color": MUTED})

    total_inj = int(att["occurrences"].sum()) if not att.empty else 0
    top = att.iloc[0]["kind"] if not att.empty else "n/a"
    hook_bytes = int(hooks["response_bytes"].sum()) if not hooks.empty else 0

    fig = go.Figure()
    if not att.empty:
        head = att.head(14).iloc[::-1]
        fig.add_trace(go.Bar(x=head["occurrences"], y=head["kind"], orientation="h",
                             marker_color=ACCENT))
        fig.update_layout(title="Injected Context by Type",
                          title_font=dict(color=TEXT, size=13),
                          xaxis_title="occurrences", yaxis_title="")

    # Numbers stay numbers. Comma-formatting them into strings here made the table sort
    # lexicographically (so 9 came after 80,000) and put quoted text in the CSV export, which is
    # the opposite of what an exportable evidence table is for.
    for frame in (att, hooks, ev, rec):
        for col in ("occurrences", "calls", "response_bytes", "input_bytes", "n", "sessions"):
            if col in frame.columns:
                frame[col] = frame[col].fillna(0).astype(int)

    return html.Div([
        html.Div([
            stat_card("Injected records", f"{total_inj:,}", sub="harness-inserted, not typed"),
            stat_card("Distinct kinds", f"{len(att):,}", sub=f"most common: {top}"),
            stat_card("Hook-observed bytes", fmt_bytes(hook_bytes),
                      sub="tool responses, desktop entrypoint"),
            # ev['n'] is an int column now. It was being un-comma'd back into a number here,
            # which only worked while the display formatting was mutating the frame in place.
            stat_card("Lifecycle events", f"{int(ev['n'].sum()):,}" if not ev.empty else "0",
                      sub="from the hook channel"),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),

        html.Div("Context injected by the harness", style=SECTION_HEAD),
        html.Div("All sessions. Not typed by you and not returned by a tool: reminders, hook "
                 "output, skill and agent listings, deferred-tool deltas. It occupies the same "
                 "window as everything else, and no native view shows it historically.",
                 style=SECTION_NOTE),
        dcc.Graph(id="fig-sources", figure=dark_fig(fig, 360),
                  config={"displayModeBar": False}),
        evidence_block("Injected context by type", att, sql["att"], sid_args),

        evidence_block(
            "Tool response size, measured by the hooks", hooks, sql["hooks"], sid_args,
            note="Bytes, not tokens: the store has exact token counts per request and never per "
                 "tool call, so a ratio here would dress an estimate as a measurement."),

        html.Div([
            html.Div(evidence_block("Lifecycle events", ev, sql["ev"], sid_args, page_size=8),
                     style={"flex": "1.4"}),
            html.Div(evidence_block("Transcript record census", rec, sql["rec"], (), page_size=8),
                     style={"flex": "1"}),
        ], style={"display": "flex", "gap": "14px", "alignItems": "flex-start"}),
    ])

"""The probes tab.

What a probe session measured about the fixed overhead of your configuration.
"""
from dash import dash_table, html

from c4x.store import q
from c4x.theme import (
    CODE_BLOCK,
    MUTED,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    WARN,
    header_help,
    stat_card,
)


# ---- Probes ---------------------------------------------------------------
def probes_layout(session_id=None, scope="main", cohort=None):
    """What the control protocol returns, and what the app's own refresh loop costs.

    tools/probe.mjs asks a spawned Claude Code session for its context breakdown over the control
    protocol. That is the only route to a per-ITEM cost - which skills, which MCP tools - and the
    result has lived only in SQL. It is also a research result in its own right: the same probe on
    a different build returns a different vocabulary, which is why the rows are shown per probe
    rather than merged into one number.
    """
    probes = q("""SELECT id, ts, ok, model, total_tokens, percentage,
                         auto_compact_threshold, is_auto_compact_enabled, error
                  FROM probes ORDER BY id""")
    details = q("""SELECT probe_id, kind, COUNT(*) AS items, SUM(COALESCE(tokens,0)) AS tokens
                   FROM probe_details GROUP BY probe_id, kind
                   ORDER BY probe_id, SUM(COALESCE(tokens,0)) DESC""")
    named = q("""SELECT probe_id, kind, name, COALESCE(tokens,0) AS tokens
                 FROM probe_details WHERE COALESCE(tokens,0) > 0
                 ORDER BY tokens DESC LIMIT 60""")
    runs = q("""SELECT COUNT(*) AS runs,
                       SUM(CASE WHEN files_read = 0 THEN 1 ELSE 0 END) AS empty_runs,
                       SUM(COALESCE(files_read,0)) AS files_read,
                       ROUND(AVG(COALESCE(ms,0)), 1) AS avg_ms,
                       MAX(ts) AS last_run
                FROM harvest_runs""")

    if probes.empty:
        body = [html.Div("No probe has been run against this store.", style={"color": MUTED}),
                html.Pre("node tools/probe.mjs", style=CODE_BLOCK)]
    else:
        body = [
            html.Div("Probe runs", style=SECTION_HEAD),
            html.Div("Each row is one spawned session answering the control protocol. A spawned "
                     "CLI session is NOT configured like the desktop app, so these numbers "
                     "describe the probe, not your live work. That difference is the finding.",
                     style=SECTION_NOTE),
            dash_table.DataTable(
                columns=(_cols := [{"name": c, "id": c} for c in
                         ["id", "ts", "ok", "model", "total_tokens", "auto_compact_threshold"]]),
                tooltip_header=header_help(_cols),
                data=probes.astype(object).where(probes.notna(), "").to_dict("records"),
                **TABLE_STYLE),
            html.Div("Per-category items and cost", style=SECTION_HEAD),
            html.Div("A count with zero tokens means the channel named the items but priced none "
                     "of them, which is exactly what makes the per-item cost unrecoverable from "
                     "this route alone.", style=SECTION_NOTE),
            dash_table.DataTable(
                columns=(_cols :=
                    [{"name": c, "id": c} for c in ["probe_id", "kind", "items", "tokens"]]),
                tooltip_header=header_help(_cols),
                data=details.to_dict("records"), page_size=12,
                style_table={"overflowX": "auto"}, **TABLE_STYLE),
        ]
        if not named.empty:
            body += [
                html.Div("The items that carry a price", style=SECTION_HEAD),
                dash_table.DataTable(
                    columns=(_cols :=
                        [{"name": c, "id": c} for c in ["probe_id", "kind", "name", "tokens"]]),
                    tooltip_header=header_help(_cols),
                    data=named.to_dict("records"), page_size=12,
                    style_table={"overflowX": "auto"}, **TABLE_STYLE),
            ]

    r = runs.iloc[0] if not runs.empty else None
    cards = []
    if r is not None and r["runs"]:
        empty_pct = 100.0 * int(r["empty_runs"] or 0) / int(r["runs"])
        cards = [
            stat_card("Harvest runs", f"{int(r['runs']):,}", sub="incremental, all time"),
            stat_card("Read nothing", f"{empty_pct:.0f}%",
                      color=WARN if empty_pct > 40 else TEXT,
                      sub=f"{int(r['empty_runs'] or 0):,} runs found no new bytes"),
            stat_card("Files read", f"{int(r['files_read'] or 0):,}", sub="across all runs"),
            stat_card("Avg duration", f"{float(r['avg_ms'] or 0):.0f} ms", sub="per run"),
        ]

    return html.Div([
        html.Div(cards, style={"display": "flex", "gap": "12px", "flexWrap": "wrap"}),
        html.Div("The refresh loop, measured", style=SECTION_HEAD),
        html.Div("Most harvests find nothing, because the dashboard polls on a timer while the "
                 "hooks already harvest on SessionEnd and UserPromptSubmit. A high percentage "
                 "here is not an error, it is the cost of the tick being shorter than the work.",
                 style=SECTION_NOTE),
    ] + body)

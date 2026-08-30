"""Controls that live inside a tab.

The Session tab's budget and turn range, the row-click detail panes for messages and
compactions, and the Mirror calculator.

Registered by importing this module. Dash's `callback` decorator writes into a global registry, so
these attach wherever they are defined; app.py imports them and re-exports the names, because the
tests and the table audit reach them as app._name and should not have to know where each one lives.
"""
from dash import Input, Output, State, callback, dash_table, html
from dash.exceptions import PreventUpdate

from c4x.panels import (
    text_panel,
    turn_diff_panel,
)
from c4x.store import (
    compaction_dropped,
    compaction_dropped_count,
    compaction_summary_text,
    message_text,
    predict,
    session_turns,
)
from c4x.tabs import session_view
from c4x.theme import (
    ACCENT,
    DANGER,
    GOOD,
    MONO,
    MUTED,
    TABLE_STYLE,
    TEXT,
    WARN,
    fmt_tokens,
    numeric_columns,
    stat_card,
)


@callback(
    Output("session-fig", "figure"),
    Output("session-diff", "children"),
    Input("budget-pct", "value"),
    Input("turn-range", "value"),
    Input("sel-session", "value"),
    Input("session-scope", "value"),
)
def _session_controls(budget_pct, turn_range, session_id, scope):
    """Redraw the session chart and the turn diff for the current slider positions.

    One callback for both sliders because they write to the same figure: a separate callback per
    control would have two of them racing to own it, and whichever fired last would erase the
    other's marks.
    """
    if not session_id:
        raise PreventUpdate
    scope = scope or "main"
    a, b = (turn_range or [None, None])[:2]
    fig, _cards = session_view(session_id, scope, budget_pct, (a, b), with_cards=False)
    turns = session_turns(session_id, include_sidechain=(scope != "main"))
    return fig, turn_diff_panel(session_id, scope, turns, a, b)

@callback(
    Output("message-detail", "children"),
    Input("tbl-messages", "active_cell"),
    State("tbl-messages", "derived_viewport_data"),
    prevent_initial_call=True,
)
def _message_clicked(active_cell, rows):
    if not active_cell or not rows:
        return ""
    try:
        uuid = rows[active_cell["row"]].get("uuid")
    except (IndexError, KeyError, TypeError):
        return ""
    if not uuid:
        return ""
    df = message_text(uuid)
    if df.empty:
        return text_panel("not found", "No stored text for that message.", MUTED)
    r = df.iloc[0]
    return text_panel(
        f"{r['role']} / {r['type']} - {int(r['chars']):,} chars - {str(r['ts'])[:19]}",
        str(r["text"]), ACCENT)

@callback(
    Output("compaction-detail", "children"),
    Input("tbl-compactions", "active_cell"),
    State("tbl-compactions", "derived_viewport_data"),
    prevent_initial_call=True,
)
def _compaction_clicked(active_cell, rows):
    if not active_cell or not rows:
        return ""
    try:
        row = rows[active_cell["row"]]
    except (IndexError, KeyError, TypeError):
        return ""
    uuid = row.get("uuid")
    if not uuid:
        return ""

    out = []
    summ = compaction_summary_text(uuid)
    if summ.empty:
        out.append(text_panel(
            "summary text not in the store",
            "No summary message was harvested for this compaction. Older boundaries record token "
            "counts only, so a compaction from before this store held text has no summary to "
            "show. Re-run node tools/harvest.mjs --full if you think it should.", MUTED))
    else:
        s = summ.iloc[0]
        out.append(text_panel(
            f"the summary that replaced the dropped context - {int(s['chars']):,} chars",
            str(s["text"]), GOOD))

    dropped = compaction_dropped(uuid)
    if not dropped.empty:
        d = dropped.copy()
        d["ts"] = d["ts"].astype(str).str.slice(11, 19)
        total = compaction_dropped_count(uuid)
        shown = f"showing the {len(d)} largest of {total:,}" if total > len(d) else f"all {total:,}"
        out.append(html.Div([
            html.Div(
                f"{total:,} messages were present before this compaction and are absent from its "
                f"survivor list ({shown}, largest first). A LOWER BOUND: survivor uuids the store "
                f"holds no message for cannot be matched, so some rows here may in fact have "
                f"survived.",
                style={"color": MUTED, "fontSize": "11.5px", "margin": "14px 0 6px 0"},
            ),
            dash_table.DataTable(
                columns=numeric_columns(["ts", "role", "type", "chars", "preview"], {"chars"}),
                data=d.to_dict("records"), page_size=10,
                style_table={"overflowX": "auto"}, **TABLE_STYLE,
            ),
        ]))
    return html.Div(out)

@callback(
    Output("mirror-out", "children"),
    Input("in-tokens", "value"),
    Input("dd-window", "value"),
)
def _mirror(tokens, window):
    if tokens is None or window is None:
        return html.Div("Enter a token count", style={"color": MUTED})
    try:
        r = predict(int(tokens), int(window))
    except Exception as exc:  # surface the real failure, never a blank panel
        return html.Div(f"mirror.mjs failed: {exc}",
                        style={"color": DANGER, "fontFamily": MONO, "fontSize": "12px"})
    colors = {"ok": GOOD, "warn": WARN, "compact": DANGER, "blocked": DANGER}
    lvl = r["level"]
    return html.Div([
        stat_card("level", lvl.upper(), color=colors.get(lvl, TEXT)),
        stat_card("percent left", f"{r['pctLeft']}%"),
        stat_card("until compaction", fmt_tokens(r["tokens_until_compact"]),
                  sub=f"trigger at {fmt_tokens(r['reported_threshold'])}"),
        stat_card("blocked at", fmt_tokens(r["blocked_at"]), color=DANGER),
    ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})

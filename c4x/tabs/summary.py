"""The summary tab.

Store-wide values and the findings that name an action. The only tab that ignores
the header selection, and it says so in its first line.
"""
import pandas as pd
import plotly.graph_objects as go
from dash import dash_table, dcc, html

from c4x.breakdown import latest_baseline
from c4x.store import overview_stats, q
from c4x.theme import (
    ACCENT,
    BORDER,
    MUTED,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    VIOLET,
    WARN,
    accordion,
    dark_fig,
    fmt_bytes,
    fmt_tokens,
    header_help,
    stat_card,
)

# Carried for the click, never shown. Named once so the table and the findings cannot drift.
_HIDDEN = ("session_id", "goes to")


def summary_layout(session_id=None, scope="main", cohort=None):
    """Store-wide values, all of them, and nothing per-selection.

    This tab answers "what is in the store". Every other tab answers "what about this selection".
    Keeping those apart is the restructure: the old Overview mixed lifetime totals, the newest call
    anywhere, and per-session figures with nothing labelling the difference.
    """
    s = overview_stats()
    api_calls = int(s["api_calls"] or 0)
    main_calls = int(s["main_calls"] or 0)
    sub_calls = api_calls - main_calls
    billed = int(s["billed"] or 0)
    cache_read = int(s["cache_read"] or 0)
    rows = decisions()

    totals = [
        # Both numbers, because they answer different questions and the page shows both
        # elsewhere: this card counts every session in the store, the picker and All sessions list
        # only those with five or more transcript rows.
        ("sessions", f"{int(s['sessions']):,}",
         f"in the store, {int(s['listed'] or 0):,} listed on All sessions"),
        ("API calls", f"{api_calls:,}", f"{int(s['turn_rows']):,} transcript rows behind them"),
        ("subagent share", f"{(100.0 * sub_calls / api_calls) if api_calls else 0:.0f}%",
         f"{sub_calls:,} of {api_calls:,} are sidechain"),
        ("cache reads", f"{(100.0 * cache_read / billed) if billed else 0:.1f}%",
         f"{fmt_tokens(cache_read)} of {fmt_tokens(billed)} billed"),
        ("compactions", f"{int(s['compactions']):,}", f"{int(s['unpaired'] or 0)} unpaired"),
        ("transcripts", f"{int(s['files']):,}", f"{(s['bytes'] or 0) / 1073741824:.2f} GB"),
        ("peak resident", fmt_tokens(s["peak"]), "largest single API call, any session"),
    ]

    return html.Div([
        # Already the right sentence, now MARKED as the population line so the API reports it.
        # It keeps its warning colour, which the shared style does not carry, because this is the
        # one tab where a reader is most likely to assume the header selection applies.
        html.Div("Everything on this tab describes the WHOLE store, every session, all time. "
                 "The header selection changes nothing here. Every other tab describes only the "
                 "selection.",
                 className="population-note", style={**SECTION_NOTE, "color": WARN}),

        accordion("What to do about it", f"{len(rows)} finding(s), each with an action",
                  dash_table.DataTable(
                      # Clickable. A finding names the tab that proves it, and usually a session,
                      # so a reader is not left copying an 8-character prefix into a dropdown of
                      # 316. A finding about the whole store, like fixed overhead, names a tab and
                      # no session: it moves the reader without touching their selection.
                      #
                      # Both travel as DECLARED columns that are then hidden. hidden_columns only
                      # hides a column that exists; naming one that was never declared hides
                      # nothing and, worse, keeps it out of `derived_viewport_data`, which is the
                      # row the click callback reads. That was the state this shipped in for one
                      # commit: the table rendered, the rows looked clickable, and every click was
                      # a no-op that raised PreventUpdate where nothing could see it.
                      id="tbl-findings",
                      columns=(_cols :=
                          [{"name": c, "id": c} for c in ["finding", "evidence", "do this"]]) +
                          [{"name": c, "id": c} for c in _HIDDEN],
                      tooltip_header=header_help(_cols),
                      hidden_columns=list(_HIDDEN),
                      data=rows,
                      style_cell_conditional=[
                          {"if": {"column_id": "finding"}, "minWidth": "200px", "maxWidth": "240px",
                           "whiteSpace": "normal"},
                          {"if": {"column_id": "evidence"},
                           "minWidth": "300px", "maxWidth": "420px",
                           "whiteSpace": "normal"},
                          {"if": {"column_id": "do this"},
                           "minWidth": "300px", "whiteSpace": "normal"},
                      ],
                      style_table={"overflowX": "auto"}, **TABLE_STYLE)
                  if rows else html.Div("Nothing to act on from this store yet.",
                                        style=SECTION_NOTE),
                  open_by_default=True),

        accordion("Store totals", "lifetime counts, no action implied",
                  html.Div([stat_card(label, value, sub=note) for label, value, note in totals],
                           style={"display": "flex", "gap": "12px", "flexWrap": "wrap"})),

        accordion("Where the tokens went", "cumulative resident by project, top 15",
                  dcc.Graph(figure=dark_fig(project_totals_fig(), 420),
                            config={"displayModeBar": False})),
    ])


def project_totals_fig() -> go.Figure:
    """Cumulative resident tokens by real project path.

    Grouped by cwd, not by project_slug. The slug `subagents` is not a project: it is the folder
    subagent transcripts are written to, and it maps to 30 different working directories in this
    store, so charting it as one bar credited every subagent run in every project to a single
    invented project and made it the largest bar on the page.
    """
    top = q("""
        SELECT COALESCE(NULLIF(s.cwd,''), s.project_slug, '(unknown)') AS project,
               SUM(a.total_resident) AS resident
        FROM api_calls a LEFT JOIN sessions s ON s.session_id = a.session_id
        GROUP BY project ORDER BY resident DESC LIMIT 15
    """)
    fig = go.Figure(go.Bar(
        x=top["resident"], y=top["project"], orientation="h",
        marker=dict(color=ACCENT, line=dict(color=BORDER, width=1)),
        hovertemplate="%{y}<br>%{x:,.0f} resident tokens<extra></extra>",
    ))
    fig.update_layout(title="Cumulative resident tokens by working directory (top 15)",
                      title_font=dict(color=TEXT, size=13))
    fig.update_yaxes(autorange="reversed")
    return fig


def decisions() -> list:
    """Findings that name an action, computed from this store.

    The test applied to every row, from Few's dashboard rule: what decision does this support? If
    the answer is "it is interesting" or "we have the data", it does not belong here. The page this
    replaced was seven lifetime totals - sessions, API calls, transcripts, compactions, peak
    resident, subagent share, cache-read share - and not one of them changed what anybody did next.

    Each row carries the measurement AND the action, because a number with no action is the thing
    being removed, and an action with no number is an opinion.
    """
    out = []

    # 1. The largest lever in this store by a wide margin. Every request re-bills the whole
    #    resident window as a cache read, so a session that runs for days pays for its context
    #    once per turn that follows it. The fix is not a smaller context, it is a shorter session.
    top = q("""
        SELECT session_id, SUM(COALESCE(cache_read_input_tokens,0)) AS churn,
               MAX(total_resident) AS peak, COUNT(*) AS calls,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM api_calls GROUP BY session_id ORDER BY churn DESC LIMIT 1
    """)
    if not top.empty and (top.iloc[0]["churn"] or 0) > 0:
        r = top.iloc[0]
        days = 0
        try:
            days = (pd.to_datetime(r["last_ts"], format="mixed", utc=True)
                    - pd.to_datetime(r["first_ts"], format="mixed", utc=True)).days
        except Exception:
            days = 0
        mult = (r["churn"] / r["peak"]) if r["peak"] else 0
        out.append({
            "finding": "One session re-paid for its own context",
            "evidence": f"{str(r['session_id'])[:8]} ran {days} days "
                        f"over {int(r['calls']):,} calls "
                        f"and billed {fmt_tokens(r['churn'])} of cache reads, "
                        f"{mult:,.0f}x its own peak window",
            "do this": "Split long-running work into fresh sessions. Context cost grows with "
                       "turns resident, not with what you asked for.",
            "session_id": str(r["session_id"]),
            "goes to": "tab-session",
        })

    # 2. A file read N times in one session is billed on every request after each read.
    dup = q("""
        SELECT target, session_id, COUNT(*) AS reads, SUM(COALESCE(result_bytes,0)) AS bytes
        FROM tool_calls
        WHERE tool_name IN ('Read','NotebookRead') AND target IS NOT NULL
        GROUP BY session_id, target ORDER BY reads DESC LIMIT 1
    """)
    if not dup.empty and int(dup.iloc[0]["reads"]) > 2:
        r = dup.iloc[0]
        extra = int(r["reads"]) - 1
        out.append({
            "finding": "The same file was read over and over inside one session",
            "evidence": f"{str(r['target']).split(chr(92))[-1].split('/')[-1]} read "
                        f"{int(r['reads']):,} times in {str(r['session_id'])[:8]}, "
                        f"{extra:,} of them repeats, {fmt_bytes(r['bytes'])} of results",
            "do this": "Grep for the line you need instead of re-reading the file, or delegate the "
                       "reading to a subagent so the content never enters this window.",
            "session_id": str(r["session_id"]),
            "goes to": "tab-cost",
        })

    # 3. An MCP server's schema is resident whether or not you call it.
    srv = q("""
        SELECT server_name AS server, COUNT(*) AS calls, MAX(ts) AS last_call
        FROM tool_calls WHERE server_name IS NOT NULL
        GROUP BY server_name ORDER BY calls ASC LIMIT 3
    """)
    if not srv.empty:
        names = ", ".join(f"{r.server} ({int(r.calls)})" for r in srv.itertuples())
        out.append({
            "finding": "MCP servers are loaded on every session and barely called",
            "evidence": f"least used: {names}",
            "do this": "Remove the ones you do not use from your MCP config. Their tool schemas "
                       "occupy the window from session start whether or not you call them.",
            "goes to": "tab-cost",
        })

    # 4. Fixed overhead is paid at the start of every session, before anything is asked.
    b = latest_baseline()
    if b:
        static = int(b["static_total"] or 0)
        mem = int(b.get("memory_files") or 0)
        skl = int(b.get("skills") or 0)
        if static:
            out.append({
                "finding": "Fixed overhead is resident before you type anything",
                "evidence": f"{fmt_tokens(static)} every session, of which "
                            f"{fmt_tokens(mem)} is memory files and {fmt_tokens(skl)} is skills",
                "do this": "Trim CLAUDE.md and unload skills you are not using. This is paid on "
                           "the first request of every session and never freed.",
                # No session: the baseline is one measurement of the machine, not of a session.
                # Window's Configuration panel is where the memory files and skills are itemised.
                "goes to": "tab-window",
            })

    # 5. Compactions are recoverable but lossy, and a session that compacts repeatedly is a
    #    session doing too much in one window.
    # COALESCE(dropped, 0) would say "dropped 0", which reads as "discarded nothing". 104 of the
    # 138 compactions in this store never recorded the figure at all, so the honest report counts
    # the missing ones rather than summing them as zero.
    comp = q("""
        SELECT session_id, COUNT(*) AS n,
               SUM(cumulative_dropped_tokens) AS dropped,
               SUM(cumulative_dropped_tokens IS NULL) AS unrecorded
        FROM compactions GROUP BY session_id ORDER BY n DESC LIMIT 1
    """)
    if not comp.empty and int(comp.iloc[0]["n"]) > 1:
        r = comp.iloc[0]
        n = int(r["n"])
        unrec = int(r["unrecorded"] or 0)
        if unrec >= n:
            dropped_txt = "the tokens it discarded were never recorded by any of them"
        elif unrec:
            dropped_txt = (f"dropping {fmt_tokens(r['dropped'])} across the "
                           f"{n - unrec} that recorded it, {unrec} did not")
        else:
            dropped_txt = f"dropping {fmt_tokens(r['dropped'])} cumulatively"
        out.append({
            "finding": "One session compacted repeatedly",
            "evidence": f"{str(r['session_id'])[:8]} compacted {n} times, {dropped_txt}",
            "do this": "Check the Compactions tab for what it discarded, then split that work "
                       "across sessions so the window is not repeatedly rebuilt.",
            # It tells the reader to check a tab, so clicking it goes there. A finding that names
            # its evidence and then makes the reader navigate by hand is half a finding.
            "session_id": str(r["session_id"]),
            "goes to": "tab-compactions",
        })

    return out


def overview_layout():
    s = overview_stats()
    api_calls = int(s["api_calls"] or 0)
    main_calls = int(s["main_calls"] or 0)
    sub_calls = api_calls - main_calls
    billed = int(s["billed"] or 0)
    cache_read = int(s["cache_read"] or 0)
    cache_pct = (100.0 * cache_read / billed) if billed else 0.0
    sub_pct = (100.0 * sub_calls / api_calls) if api_calls else 0.0
    cards = html.Div(
        [
            stat_card("sessions", f"{int(s['sessions']):,}"),
            stat_card("api calls", f"{api_calls:,}",
                      sub=f"{int(s['turn_rows']):,} transcript rows behind them"),
            stat_card("subagent share", f"{sub_pct:.0f}%",
                      color=VIOLET,
                      sub=f"{sub_calls:,} of {api_calls:,} calls are sidechain"),
            stat_card("cache reads", f"{cache_pct:.1f}%", color=WARN,
                      sub=f"{fmt_tokens(cache_read)} of {fmt_tokens(billed)} tokens billed"),
            stat_card("compactions", f"{int(s['compactions']):,}",
                      color=WARN, sub=f"{int(s['unpaired'] or 0)} unpaired"),
            stat_card("transcripts", f"{int(s['files']):,}",
                      sub=f"{(s['bytes'] or 0)/1073741824:.2f} GB"),
            stat_card("peak resident", fmt_tokens(s["peak"]), color=VIOLET,
                      sub="largest single API call"),
        ],
        style={"display": "flex", "gap": "12px", "flexWrap": "wrap", "marginBottom": "18px"},
    )

    top = q("""
        -- api_calls, NOT turns. A streamed assistant message is written as several transcript
        -- rows carrying the same requestId and the same usage, about 2.3 per call here, so
        -- summing turns inflated every figure on this chart by roughly 2x. Measured against
        -- ccusage on the same transcripts: turns gave 56.90 B, the deduped view gives 27.12 B.
        SELECT COALESCE(NULLIF(s.cwd,''), s.project_slug, '(unknown)') AS project,
               COUNT(*) AS turns,
               SUM(a.total_resident) AS resident, SUM(a.output_tokens) AS out
        FROM api_calls a LEFT JOIN sessions s ON s.session_id = a.session_id
        GROUP BY project ORDER BY resident DESC LIMIT 15
    """)
    fig = go.Figure(go.Bar(
        x=top["resident"], y=top["project"], orientation="h",
        marker=dict(color=ACCENT, line=dict(color=BORDER, width=1)),
        hovertemplate="%{y}<br>%{x:,.0f} resident tokens<extra></extra>",
    ))
    fig.update_layout(title="Cumulative resident tokens by project (top 15)",
                      title_font=dict(color=TEXT, size=13))
    fig.update_yaxes(autorange="reversed")

    return html.Div([
        cards,
        dcc.Graph(figure=dark_fig(fig, 460), config={"displayModeBar": False}),
        html.Div(
            "Resident tokens are input + cache_creation + cache_read from each API response, "
            "the same sum Claude Code itself uses for the context bar.",
            style={"color": MUTED, "fontSize": "11.5px", "marginTop": "8px"},
        ),
    ])

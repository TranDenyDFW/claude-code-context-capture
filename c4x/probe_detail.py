"""Item-by-item detail behind the window categories: which skill, which tool, which file.

The category rows say a configuration costs 9,900 tokens of skills. They do not say WHICH skills,
and for research that is the part that matters: a category total tells you the bill, an item list
tells you what to cancel.

Everything here comes from a probe, which is a real reading rather than a derivation. That makes it
the most trustworthy material on the Breakdown tab and also the narrowest, because a probe
describes the session that ran it. Where a probe's own numbers disagree with the calibrated
baseline, both are shown side by side rather than one being quietly preferred.
"""
import pandas as pd
import plotly.graph_objects as go
from dash import dcc, html
from dash.dash_table.Format import Format, Scheme
from dash.development.base_component import Component

from c4x.dash_compat import DataTable
from c4x.panels import evidence_block
from c4x.store import q
from c4x.theme import (
    BORDER,
    DANGER,
    MONO,
    MUTED,
    PANEL,
    SECTION_HEAD,
    SECTION_NOTE,
    TABLE_STYLE,
    TEXT,
    about_note,
    chart_note,
    header_help,
    numeric_columns,
    toward_background,
)

# What each probe kind is called on the page, and which baseline column counts the same thing.
# Paired here rather than in two places, so a kind can never be listed with the wrong denominator.
KINDS = [
    ("skill", "Skills", "skills", None),
    ("mcpTool", "MCP tools", "mcp_tools", "mcp_tools_items"),
    ("agent", "Custom agents", "custom_agents", "custom_agents_items"),
    ("memoryFile", "Memory files", "memory_files", "memory_files_items"),
    ("attachment", "Attachments in the conversation", None, None),
    ("toolCallType", "Tool calls in the conversation", None, None),
]


def latest_probe():
    """The newest probe that returned a payload, or None.

    Newest by timestamp rather than by id, because a backfilled or imported row can carry an id
    that does not order the same way.
    """
    df = q("""SELECT id, ts, model, total_tokens, max_tokens, percentage
                FROM probes WHERE ok = 1 AND raw_json IS NOT NULL
               ORDER BY ts DESC LIMIT 1""")
    return None if df.empty else df.iloc[0]


def probe_completeness(probe_id):
    """Which categories a probe actually reported.

    A probe is a live reading and can come back partial: two of the three in this store returned
    only Skills and Free space, with no System prompt and no System tools, which is not a
    configuration a session can have. Reporting that alongside the detail is the difference between
    "your window holds no system tools" and "this reading did not include them".
    """
    cats = q("SELECT name, tokens FROM probe_categories WHERE probe_id = ? ORDER BY tokens DESC",
             (int(probe_id),))
    names = set(cats["name"]) if not cats.empty else set()
    return cats, names


def detail_rollup(probe_id, baseline):
    """One row per kind: what the probe recorded, beside what the baseline counts.

    Two independent sources for the same quantity. Printing only one of them is how a page ends up
    asserting a configuration it never saw.
    """
    counts = q("""SELECT kind, COUNT(*) AS items, SUM(COALESCE(tokens,0)) AS tokens,
                         SUM(CASE WHEN loaded = 0 THEN 1 ELSE 0 END) AS unloaded,
                         SUM(CASE WHEN loaded IS NULL THEN 0 ELSE 1 END) AS flagged
                    FROM probe_details WHERE probe_id = ? GROUP BY kind""", (int(probe_id),))
    seen = {r["kind"]: r for _, r in counts.iterrows()}
    rows = []
    for kind, label, token_col, count_col in KINDS:
        r = seen.get(kind)
        if r is None and not (baseline is not None and token_col and baseline.get(token_col)):
            continue
        base_tokens = baseline.get(token_col) if (baseline is not None and token_col) else None
        base_items = baseline.get(count_col) if (baseline is not None and count_col) else None
        rows.append({
            "category": label,
            "items recorded": int(r["items"]) if r is not None else 0,
            "items in baseline": None if pd.isna(base_items) or base_items is None
                                 else int(base_items),
            "tokens recorded": int(r["tokens"]) if r is not None else 0,
            "tokens in baseline": None if pd.isna(base_tokens) or base_tokens is None
                                  else int(base_tokens),
            # Blank, not 0, where the kind carries no residency flag. Skills and memory files
            # are simply resident; printing 0 there claimed a measurement that does not exist.
            "not loaded": (int(r["unloaded"])
                           if r is not None and not pd.isna(r["flagged"]) and r["flagged"]
                           else None),
        })
    return rows


def item_table(probe_id, kind, title, note, columns_sql, columns, numeric):
    """One evidence_block per kind, or nothing when the probe recorded none of that kind.

    Every row carries its share of its own category. A raw token count is not readable on its own:
    523 says nothing until it is 5.3% of what skills cost, and the share is what decides whether an
    item is worth removing.
    """
    sql = (f"SELECT {columns_sql},\n"
           f"       ROUND(100.0 * COALESCE(tokens,0) / NULLIF((SELECT SUM(COALESCE(tokens,0))\n"
           f"         FROM probe_details WHERE probe_id = ? AND kind = '{kind}'), 0), 2)\n"
           f"         AS pct_of_kind\n"
           f"  FROM probe_details WHERE probe_id = ? AND kind = '{kind}'\n"
           f" ORDER BY tokens DESC, name")
    args = (int(probe_id), int(probe_id))
    df = q(sql, args)
    if df.empty:
        return None
    return evidence_block(
        title, df, sql, args,
        columns=numeric_columns(columns + ["pct_of_kind"], numeric | {"pct_of_kind"},
                                {"pct_of_kind": Format(precision=2, scheme=Scheme.fixed)}),
        page_size=12, note=note, heat=["tokens"])


def mcp_by_server(probe_id):
    """MCP tools grouped by the thing a reader can actually switch off.

    A single tool is rarely the decision; a server is. This is the table the Summary tab's advice
    ("remove the ones you do not use from your MCP config") needs in order to be actionable.
    """
    sql = ("SELECT extra AS server, COUNT(*) AS tools,\n"
           "       SUM(CASE WHEN loaded = 1 THEN 1 ELSE 0 END) AS loaded_tools,\n"
           "       SUM(COALESCE(tokens,0)) AS tokens\n"
           "  FROM probe_details WHERE probe_id = ? AND kind = 'mcpTool'\n"
           " GROUP BY extra ORDER BY tokens DESC, tools DESC")
    df = q(sql, (int(probe_id),))
    if df.empty:
        return None
    return evidence_block(
        "MCP servers, and what each one costs", df, sql, (int(probe_id),),
        columns=numeric_columns(["server", "tools", "loaded_tools", "tokens"],
                                {"tools", "loaded_tools", "tokens"}),
        page_size=12, heat=["tokens", "tools"],
        note="A server is the unit you can remove; a tool is not. tokens is what its schemas cost "
             "IN THIS READING, so a server whose tools were all deferred shows 0 and would not "
             "show 0 in a session that loaded them.")


CONFIGURATION_KINDS = ("skill", "mcpTool", "agent", "memoryFile")

# One base colour per kind, in the order the kinds appear. Every leaf under a kind is this colour
# faded towards the page background by its rank, so the treemap stays inside the app's palette
# instead of taking Plotly's light-mode qualitative default.
KIND_COLORS = ("#1f6feb", "#a371f7", "#3fb950", "#d29922", "#e8590c", "#f85149")


def configuration_treemap(probe_id):
    """Every configured item this probe saw, sized, grouped by kind. Two levels, one picture.

    This is the case the proportional bar upstairs cannot serve and the reason the treemap exists
    at all. The Skills category is one segment of that bar and one row of that table; behind it
    are 321 skills, and a bar with 321 segments is a solid block while a table with 321 rows
    answers "which is biggest" only after the reader sorts it and only for one column.

    Items with zero tokens are dropped rather than drawn. A probe records `loaded` separately, and
    a zero-token item is one that was listed but not resident: giving it a slice of an area chart
    about what OCCUPIES the window would state the opposite of what it means. How many were
    dropped is reported by the caller, because a picture that silently omits 17 of 346 items is
    the kind of quiet trimming this app does not do.

    Returns (figure, shown, dropped), or (None, 0, 0) when there is nothing with a size.
    """
    from c4x.theme import treemap
    rows = q(f"""SELECT kind, name, extra, tokens FROM probe_details
                  WHERE probe_id = ? AND kind IN ({','.join('?' * len(CONFIGURATION_KINDS))})
                  ORDER BY tokens DESC""",
             (int(probe_id), *CONFIGURATION_KINDS))
    if rows.empty:
        return None, 0, 0
    sized = rows[rows["tokens"].fillna(0) > 0]
    dropped = len(rows) - len(sized)
    if sized.empty:
        return None, 0, dropped
    names = {kind: label for kind, label, _col, _items in KINDS}
    labels, parents, values, colors = [], [], [], []
    for index, (kind, group) in enumerate(sized.groupby("kind", sort=False)):
        parent = names.get(kind, kind)
        base = KIND_COLORS[index % len(KIND_COLORS)]
        labels.append(parent)
        parents.append("")
        values.append(int(group["tokens"].sum()))
        colors.append(base)
        # Leaves shade from the kind's colour down towards the page background, by RANK within
        # the kind. Left to itself Plotly assigns a light qualitative palette that ignores the
        # theme entirely: 321 pastel rectangles on a dark page, and no relationship between the
        # colour of a tile and anything about it. This way the colour carries the same ordering
        # the area does, so the eye and the size agree instead of competing.
        span = max(len(group) - 1, 1)
        for rank, row in enumerate(group.itertuples()):
            # Names repeat across kinds and, for MCP tools, within one. Plotly identifies a node
            # by its label, so two nodes sharing one would merge into a single slice carrying both
            # values. Qualified with the parent, and the hover template shows the plain name.
            labels.append(f"{parent}: {row.name}")
            parents.append(parent)
            values.append(int(row.tokens))
            colors.append(toward_background(base, 0.15 + 0.7 * rank / span))
    return (treemap(labels, parents, values, colors=colors, height=460,
                    title=f"Every configured item probe {int(probe_id)} saw, by size",
                    hover="%{label}<br>%{value:,} tokens<br>%{percentParent} of "
                          "%{parent}<extra></extra>"),
            len(sized), dropped)


def probe_detail_blocks(baseline=None):
    """The whole item-by-item section, or a single line saying why there is none."""
    probe = latest_probe()
    if probe is None:
        return [
            html.Div("No probe reading yet", style=SECTION_HEAD),
            html.Div(
                "The category totals above are derived from a calibrated baseline. A probe asks "
                "the running Claude Code for the same numbers directly and, unlike the tooltip, "
                "returns the ITEMS behind them: every skill by name and cost, every MCP tool and "
                "its server, every memory file. Run node tools/probe.mjs to record one.",
                style=SECTION_NOTE),
        ]

    pid = int(probe["id"])
    when = str(probe["ts"])[:19].replace("T", " ")
    cats, names = probe_completeness(pid)
    # Annotated because the first element is a Div and a dcc.Graph is appended later; inferred
    # from the literal this is a list[Div] and that append is an error.
    blocks: list[Component] = [
        # NAMED FOR THIS PANEL, not for the window as a whole. The composition panel already
        # writes "What is in the window, item by item" over its own table, and this block
        # became the title of the first table under it once every table started carrying a
        # heading, so two different tables answered to one name. Only one of the two is
        # served today, which is why nothing showed it. The parallel is the conversation
        # panel's heading below.
        html.Div("What the configuration put in the window", style=SECTION_HEAD),
        about_note(
            f"Read directly from Claude Code by probe {pid} at {when} on {probe['model']}. This is "
            f"a measurement, not a derivation, which makes it the most trustworthy material on "
            f"this tab and also the narrowest: it describes the session that ran the probe, and "
            f"that session is not the one you are reading this in."),
    ]

    # A probe that reported no system prompt did not observe a session without one. Say so before
    # any number below is read, because everything else here inherits that doubt.
    missing = [n for n in ("System prompt", "System tools") if n not in names]
    if missing:
        # A caveat over every number on the panel, so it is a statement about the view rather
        # than a caption on any one of them.
        blocks.append(about_note(
            f"THIS READING IS PARTIAL. It reports no {' and no '.join(missing)}, which no session "
            f"can actually run without, so the probe returned less than the full payload. Its item "
            f"lists are still real, but treat them as what this reading saw rather than as your "
            f"whole configuration, and prefer the baseline totals above for category sizes.",
            style={"color": DANGER, "margin": "0 0 10px 0"}))

    rows = detail_rollup(pid, baseline)
    if rows:
        blocks += [
            html.Div("Recorded against calibrated, per category",
                     style={"color": MUTED, "fontSize": "11.5px", "margin": "10px 0 6px 0"}),
            DataTable(
                columns=(_cols := numeric_columns(
                    ["category", "items recorded", "items in baseline", "tokens recorded",
                     "tokens in baseline", "not loaded"],
                    {"items recorded", "items in baseline", "tokens recorded",
                     "tokens in baseline", "not loaded"})),
                tooltip_header=header_help(_cols),
                data=rows, **TABLE_STYLE),
            html.Div(
                "Where the two columns disagree, the probe and the calibration saw different "
                "configurations. That is a fact about the readings, not an error to reconcile: a "
                "headless probe does not load the interactive MCP servers, so a large gap in the "
                "MCP row is expected and a gap in the skills row would not be.",
                style={"color": MUTED, "fontSize": "11.5px", "margin": "6px 0 14px 0",
                       "maxWidth": "900px", "lineHeight": "1.55"}),
        ]

    figure, shown, dropped = configuration_treemap(pid)
    if figure is not None:
        blocks.append(dcc.Graph(id="fig-probe-kinds", figure=figure,
                                config={"displayModeBar": False}))
        note = (f"{shown:,} items with a recorded size, grouped by kind. The tables below carry "
                f"the same items with their sources and their loaded state.")
        if dropped:
            note += (f" {dropped:,} more were listed by the probe with no tokens against them and "
                     f"are not drawn: an item recorded at zero was seen but is not occupying the "
                     f"window, and giving it an area would say the opposite. They are still in "
                     f"the tables.")
        blocks.append(chart_note(note))

    tables = [
        item_table(pid, "skill", "Every skill, largest first",
                   "Largest first. Only userSettings and plugin skills are yours to remove.",
                   "name, extra AS source, tokens", ["name", "source", "tokens"], {"tokens"}),
        mcp_by_server(pid),
        item_table(pid, "mcpTool", "Every MCP tool, by server",
                   "A server is the unit you can switch off; a tool is not.",
                   "name, extra AS server, loaded, tokens",
                   ["name", "server", "loaded", "tokens"], {"loaded", "tokens"}),
        item_table(pid, "agent", "Custom agents",
                   "Every agent definition resident in the window, whether or not it is used.",
                   "name, extra AS source, tokens", ["name", "source", "tokens"], {"tokens"}),
        item_table(pid, "memoryFile", "Memory files",
                   "CLAUDE.md and anything it pulls in. Paid on the first request of every "
                   "session and never freed.",
                   "name AS path, extra AS type, tokens", ["path", "type", "tokens"], {"tokens"}),
    ]
    blocks += [t for t in tables if t is not None]
    blocks.append(about_note(
        "A reading describes the moment it was taken. Record another after adding an MCP server, "
        "installing a skill, or editing CLAUDE.md.",
        style={"margin": "14px 0 4px 0"}))
    blocks.append(probe_command_hint())
    return blocks


def conversation_blocks(baseline=None):
    """What the CONVERSATION put in the window, as opposed to what the configuration holds.

    Separated from probe_detail_blocks because together they were eight tables and 5.4 screens, and
    they answer different questions: the configuration half is identical on turn 1 and turn 900,
    while this half is the part that grows. Nothing else in this store records the split at all.
    """
    probe = latest_probe()
    if probe is None:
        return [
            html.Div("No probe reading yet", style=SECTION_HEAD),
            html.Div(
                "The message half of the window is computed by Claude Code for its tooltip and "
                "discarded. A probe is the only way this store learns it. Run "
                "node tools/probe.mjs to record one.", style=SECTION_NOTE),
        ]
    pid = int(probe["id"])
    when = str(probe["ts"])[:19].replace("T", " ")
    # ONE `about_note`, HEADING INCLUDED. The panel draws no chart at all when a probe recorded
    # no message breakdown, so a heading bound to a chart would be bound to nothing on exactly the
    # stores where it is the only thing naming the panel.
    return [
        about_note([
            html.Div("What the conversation put in the window", style=SECTION_HEAD),
            html.Div(
                f"Read by probe {pid} at {when}. A probe spawns a fresh session, so its "
                f"conversation half is small by construction: what matters here is the SHAPE, "
                f"which categories carry the tokens, not the totals."),
        ]),
    ] + message_blocks(pid)


def message_composition_bar(probe_id):
    """Every probe's message half as one stacked bar, so the shape can be compared across probes.

    One bar per probe, oldest left. The question is which categories carry the conversation, and a
    single reading cannot answer it: a probe spawns a fresh session, so its conversation half is
    small by construction, and the way to tell a real shape from an artefact of that is to see
    whether it holds across readings.

    Returns None rather than a chart when no probe recorded more than one non-zero category.
    A stacked bar with one segment is a rectangle, and drawing it would present "this store has
    one usable reading" as a finding about how conversations are composed. The table below states
    the same fact in words, which is the honest form for it.
    """
    return stacked_message_figure(q("""
        SELECT b.probe_id, b.name, b.tokens, p.ts
          FROM probe_message_breakdown b JOIN probes p ON p.id = b.probe_id
         WHERE b.tokens > 0 ORDER BY p.ts, b.tokens DESC"""))


def stacked_message_figure(rows):
    """The drawing half, separated from the query so it can be exercised on data this store lacks.

    Every probe recorded here reports one non-zero category, so the wrapper above returns None on
    this store and would return None in CI too. Code that only ever takes its refusal branch is
    code nothing has checked: the branch that draws would ship untested and stay untested until
    the first probe that made it run, which is the worst moment to find out it does not.
    """
    if rows.empty:
        return None
    per_probe = rows.groupby("probe_id")["name"].nunique()
    if int(per_probe.max()) < 2:
        return None
    from c4x.theme import BG, dark_fig
    order = list(dict.fromkeys(rows["probe_id"]))
    palette = ("#1f6feb", "#3fb950", "#a371f7", "#d29922", "#e8590c", "#f85149", "#8b949e")
    fig = go.Figure()
    for index, name in enumerate(dict.fromkeys(rows["name"])):
        by_probe = {int(r.probe_id): int(r.tokens) for r in
                    rows[rows["name"] == name].itertuples()}
        fig.add_trace(go.Bar(
            x=[f"probe {p}" for p in order], y=[by_probe.get(int(p), 0) for p in order],
            name=name, marker=dict(color=palette[index % len(palette)],
                                   line=dict(color=BG, width=1)),
            hovertemplate="%{x}<br>" + name + ": %{y:,} tokens<extra></extra>"))
    fig.update_layout(barmode="stack", title="The message half, by category, per probe",
                      title_font=dict(color=TEXT, size=13),
                      xaxis_title="", yaxis_title="tokens")
    return dcc.Graph(id="fig-probe-messages", figure=dark_fig(fig, 340),
                     config={"displayModeBar": False})


def message_blocks(probe_id):
    """What the CONVERSATION half of the window is made of, as opposed to the configuration half.

    Nothing else in this store records it. The categories above describe fixed overhead, which is
    the same on turn 1 and turn 900; this describes the part that grows, and it names the thing
    doing the growing.
    """
    sql = ("SELECT name, tokens FROM probe_message_breakdown "
           "WHERE probe_id = ? AND tokens > 0 ORDER BY tokens DESC")
    df = q(sql, (int(probe_id),))
    zero = q("SELECT COUNT(*) AS n FROM probe_message_breakdown WHERE probe_id = ? AND tokens = 0",
             (int(probe_id),))
    n_zero = int(zero.iloc[0]["n"]) if not zero.empty else 0
    out = []
    bar = message_composition_bar(probe_id)
    if bar is not None:
        out.append(bar)
    if not df.empty:
        out.append(evidence_block(
            "What the messages are made of", df, sql, (int(probe_id),),
            columns=numeric_columns(["name", "tokens"], {"tokens"}), page_size=10,
            note=(f"{n_zero} further categories read zero and are omitted. "
                  if n_zero else "")
            + "This is the only record anywhere of how the message half of the window splits: "
              "Claude Code computes it for the tooltip and discards it."))
    # One construction site, walked once per kind, rather than one site per kind. Written as two
    # sites the audit correctly failed: a fresh probe records no tool calls, so that second site
    # built no table and nothing checked what it would have produced. A shared site is exercised by
    # whichever kind has rows, and the kind that has none simply yields nothing.
    for kind, title, note in (
        ("attachment", "Attachments, by type",
         "Injected context rather than anything typed. Hook output lands here, which is how a "
         "hook that prints on every turn becomes a permanent resident of the window."),
        ("toolCallType", "Tool calls, by type",
         "Present only once a session has actually called tools; a freshly spawned probe has "
         "none, so this is usually absent."),
    ):
        block = item_table(probe_id, kind, title, note,
                           "name, tokens", ["name", "tokens"], {"tokens"})
        if block is not None:
            out.append(block)
    if not out:
        out.append(html.Div(
            "The probe recorded no message breakdown. A probe spawns a fresh session, so its "
            "conversation half is empty by construction unless the payload carried it.",
            style=SECTION_NOTE))
    return out


def probe_command_hint():
    """How to record a fresh reading, shown where a stale one is being displayed."""
    # Marked with the sentence above it: how to refresh a reading is a fact about this view, not
    # a caption on any chart or table on it.
    return html.Pre(
        "node tools/probe.mjs            # record a new reading\n"
        "node tools/probe.mjs --backfill # recover fields from readings already stored",
        className="about-note",
        style={"background": PANEL, "border": f"1px solid {BORDER}", "borderRadius": "8px",
               "padding": "10px 12px", "color": TEXT, "fontFamily": MONO, "fontSize": "11.5px",
               "display": "inline-block", "margin": "4px 0 0 0"})

"""Diagnostics: is the capture itself working, and what does the window math say.

Two tabs merged. Probes and Mirror were both about the MACHINERY rather than about any session's
context: whether the harvest loop is doing anything, whether the control-protocol probe returns a
payload, and what thresholds this build produces. Neither answers to the header selection, and
between them they occupied 2.6 screens across two tabs a reader had no reason to visit separately.

No sub-panel strip here. The combined page is under three screens, and a strip over two short
sections is navigation for its own sake.
"""
from dash import dash_table, html

from c4x.tabs.mirror import mirror_layout
from c4x.tabs.probes import probes_layout
from c4x.theme import MUTED, SECTION_HEAD, SECTION_NOTE, TABLE_STYLE, header_help


def exclusions_layout():
    """Projects harvest has been told to stop capturing.

    ON THIS TAB BECAUSE A PROJECT THAT HAS SILENTLY STOPPED BEING CAPTURED IS A CAPTURE FAULT.
    It looks identical to a project nobody has worked on lately: the session count simply stops
    rising, and nothing anywhere says why. Every other way this store can quietly go wrong is
    reported here, and an exclusion is the one the user themselves caused.
    """
    from c4x import projects
    rows = projects.excluded()
    if not rows:
        return [
            html.Div("Excluded projects", style=SECTION_HEAD),
            html.Div("None. Every project with a working directory in the transcripts is being "
                     "captured.", style={"color": MUTED, "fontSize": "12px"}),
        ]
    cols = [{"name": c, "id": c} for c in ("cwd", "excluded_at", "note")]
    return [
        html.Div("Excluded projects", style=SECTION_HEAD),
        html.Div(f"{len(rows)} project(s) deleted from this store and skipped by every harvest "
                 "since. The rule is keyed on the working directory, not the transcript folder: "
                 "one folder can hold several projects, so excluding by folder would stop "
                 "capturing unrelated work. Lifting an exclusion re-reads the transcript from the "
                 "beginning.", style=SECTION_NOTE),
        dash_table.DataTable(columns=cols, tooltip_header=header_help(cols),
                             data=rows, page_size=10,
                             style_table={"overflowX": "auto"}, **TABLE_STYLE),
    ]


def diagnostics_layout(session_id=None, scope="main", cohort=None):
    """Capture health first, then the reference math.

    Composed from the two layouts rather than rewritten, so nothing either tab reported is lost in
    the merge and the mirror's calculator keeps the component ids its callback is bound to.
    """
    return html.Div([
        html.Div(
            "Nothing on this tab answers to the header selection. It describes the capture "
            "machinery and the window math, both of which are the same whichever session is "
            "chosen.", style=SECTION_NOTE),
        probes_layout(session_id, scope, cohort),
        html.Div(style={"height": "26px"}),
        *exclusions_layout(),
        html.Div(style={"height": "26px"}),
        html.Div("The window math, and a calculator over it", style=SECTION_HEAD),
        html.Div(
            "Read from tools/mirror-core.mjs at startup and computed by tools/mirror.mjs, not "
            "reimplemented here. Every threshold band on the Session chart comes from these "
            "numbers, so a wrong row here would be wrong everywhere at once.",
            style=SECTION_NOTE),
        mirror_layout(session_id, scope, cohort),
    ])

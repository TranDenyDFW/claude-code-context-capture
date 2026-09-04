"""Diagnostics: is the capture itself working, and what does the window math say.

Two tabs merged. Probes and Mirror were both about the MACHINERY rather than about any session's
context: whether the harvest loop is doing anything, whether the control-protocol probe returns a
payload, and what thresholds this build produces. Neither answers to the header selection, and
between them they occupied 2.6 screens across two tabs a reader had no reason to visit separately.

No sub-panel strip here. The combined page is under three screens, and a strip over two short
sections is navigation for its own sake.
"""
from dash import html

# AT MODULE LEVEL, not inside the function that uses it. `tools/table_audit.py` reads the app's
# import graph before it walks the layouts, so a module that only appears once a function has run
# makes the audit read a different set of files than the one that executed, and it says so.
from c4x import projects
from c4x.dash_compat import DataTable
from c4x.tabs.mirror import mirror_layout
from c4x.tabs.probes import probes_layout
from c4x.theme import SECTION_HEAD, SECTION_NOTE, TABLE_STYLE, about_note, header_help


def exclusions_layout():
    """Projects harvest has been told to stop capturing.

    ON THIS TAB BECAUSE A PROJECT THAT HAS SILENTLY STOPPED BEING CAPTURED IS A CAPTURE FAULT.
    It looks identical to a project nobody has worked on lately: the session count simply stops
    rising, and nothing anywhere says why. Every other way this store can quietly go wrong is
    reported here, and an exclusion is the one the user themselves caused.

    ONE PATH, NOT TWO. An earlier version showed a sentence when the list was empty and built the
    table only when it was not, and `tools/table_audit.py` correctly refused to pass: it renders
    every tab against the real store, where there are normally no exclusions, so the table was
    never reached and therefore never audited. A branch that is unreachable in the normal case is
    a branch nothing checks.
    """
    rows = projects.excluded()
    cols = [{"name": c, "id": c} for c in ("cwd", "excluded_at", "note")]
    note = (
        "None. Every project with a working directory in the transcripts is being captured."
        if not rows else
        f"{len(rows)} project(s) deleted from this store and skipped by every harvest since.")
    return [
        html.Div("Excluded projects", style=SECTION_HEAD),
        html.Div(f"{note} The rule is keyed on the working directory, not the transcript folder: "
                 "one folder can hold several projects, so excluding by folder would stop "
                 "capturing unrelated work. Lifting an exclusion re-reads the transcript from the "
                 "beginning.", style=SECTION_NOTE),
        DataTable(columns=cols, tooltip_header=header_help(cols),
                             data=rows, page_size=10,
                             style_table={"overflowX": "auto"}, **TABLE_STYLE),
    ]


def diagnostics_layout(session_id=None, scope="main", cohort=None):
    """Capture health first, then the reference math.

    Composed from the two layouts rather than rewritten, so nothing either tab reported is lost in
    the merge and the mirror's calculator keeps the component ids its callback is bound to.
    """
    return html.Div([
        # The first half of this used to restate the population banner word for word. What it
        # adds is what the tab is ABOUT, which is where that now goes.
        about_note(
            "This tab describes the capture machinery and the window math, both of which are the "
            "same whichever session is chosen."),
        probes_layout(session_id, scope, cohort),
        html.Div(style={"height": "26px"}),
        *exclusions_layout(),
        html.Div(style={"height": "26px"}),
        about_note([
            html.Div("The window math", style=SECTION_HEAD),
            html.Div(
                "Read from tools/mirror-core.mjs at startup and computed by tools/mirror.mjs, not "
                "reimplemented here. Every threshold band on the Session chart comes from these "
                "numbers, so a wrong row here would be wrong everywhere at once."),
        ]),
        # The calculator is Dash-only, so the half of that heading naming it is too.
        html.Div("A calculator over it", className="dash-only", style=SECTION_HEAD),
        mirror_layout(session_id, scope, cohort),
    ])

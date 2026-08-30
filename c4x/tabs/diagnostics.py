"""Diagnostics: is the capture itself working, and what does the window math say.

Two tabs merged. Probes and Mirror were both about the MACHINERY rather than about any session's
context: whether the harvest loop is doing anything, whether the control-protocol probe returns a
payload, and what thresholds this build produces. Neither answers to the header selection, and
between them they occupied 2.6 screens across two tabs a reader had no reason to visit separately.

No sub-panel strip here. The combined page is under three screens, and a strip over two short
sections is navigation for its own sake.
"""
from dash import html

from c4x.tabs.mirror import mirror_layout
from c4x.tabs.probes import probes_layout
from c4x.theme import SECTION_HEAD, SECTION_NOTE


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
        html.Div("The window math, and a calculator over it", style=SECTION_HEAD),
        html.Div(
            "Read from tools/mirror-core.mjs at startup and computed by tools/mirror.mjs, not "
            "reimplemented here. Every threshold band on the Session chart comes from these "
            "numbers, so a wrong row here would be wrong everywhere at once.",
            style=SECTION_NOTE),
        mirror_layout(session_id, scope, cohort),
    ])

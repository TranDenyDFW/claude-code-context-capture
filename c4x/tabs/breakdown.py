"""The breakdown tab.

The category split of the window. DERIVED from a recorded baseline, never read.
"""
from dash import html

from c4x.breakdown import breakdown_body


def breakdown_layout(session_id=None, scope="main", cohort=None):
    """Shell. The body re-renders when the sidechain scope changes.

    This tab charted only non-sidechain calls and never said so, which in this store means it was
    showing under a third of the activity. The population is now a stated choice.
    """
    return html.Div(breakdown_body(scope == "all", session_id, cohort), id="breakdown-body")

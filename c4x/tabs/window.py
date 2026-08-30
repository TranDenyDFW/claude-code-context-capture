"""The Window tab: what is in the context, and where it came from.

Three panels replacing two tabs. Breakdown was 5.7 screens carrying two subjects, and Sources was a
third subject on a tab of its own even though it answers the same question: what is in this window
that nobody typed.

    Composition  the category split now, and how it moved. DERIVED from a calibrated baseline.
    Items        which skills, tools, agents and files. MEASURED by a probe.
    Injected     attachments, hook output and lifecycle events. Read from the transcripts.

They are separate panels rather than sections of one page because they have different SOURCES, and
a reader who does not notice that will compare a derived number against a measured one and conclude
the app is inconsistent. The strip makes the boundary visible.
"""
from dash import html

from c4x.breakdown import composition_blocks
from c4x.probe_detail import conversation_blocks, probe_detail_blocks
from c4x.store import q
from c4x.tabs.sources import sources_layout
from c4x.ui.subpanels import body_id, description_note, register, strip

PREFIX = "window"

PANELS = [
    ("composition", "Composition",
     "The category split as it stands, and how it moved across every charted call. DERIVED: "
     "Claude Code stores this split nowhere, so it is computed as resident minus a recorded "
     "baseline. Resident and free space are exact; the category rows are not."),
    ("configuration", "Configuration",
     "Which skills, MCP tools, agents and memory files the configuration holds. MEASURED by a "
     "probe, so it is the most trustworthy material on this tab and also the narrowest: it "
     "describes the session that ran the probe. This half is the same on turn 1 and turn 900."),
    ("conversation", "Conversation",
     "The other half of the window: what the conversation itself put there, split by category. "
     "Claude Code computes this for its tooltip and discards it, so a probe is the only way this "
     "store ever learns it."),
    ("injected", "Injected",
     "Context you did not type and no tool returned: reminders, hook output, skill and agent "
     "listings. It occupies the same window as everything else."),
]


def panel_body(index, session_id=None, scope="main", cohort=None):
    """One panel, built on demand.

    Rendered by key rather than by position, so reordering PANELS cannot silently swap two panels'
    contents, which is the failure mode the single TABS registry exists to prevent upstairs.
    """
    index = max(0, min(int(index or 0), len(PANELS) - 1))
    key = PANELS[index][0]
    if key == "composition":
        body = composition_blocks(scope == "all", session_id, cohort)
    elif key == "configuration":
        body = html.Div(probe_detail_blocks(_latest_baseline()))
    elif key == "conversation":
        body = html.Div(conversation_blocks(_latest_baseline()))
    else:
        body = sources_layout(session_id, scope, cohort)
    # The panel states what it is at the top of its OWN body, so switching panels replaces the
    # statement along with the content rather than leaving the previous panel's description above
    # the new one.
    return html.Div([description_note(PANELS, index), body])


def _latest_baseline():
    """The newest calibrated baseline, or None.

    probe_detail_blocks compares what a probe recorded against what the calibration counted, and
    prints both. Without a baseline it prints only what the probe saw, which is still true.
    """
    df = q("SELECT * FROM context_baselines ORDER BY ts DESC LIMIT 1")
    return None if df.empty else df.iloc[0]


def window_layout(session_id=None, scope="main", cohort=None):
    """The strip plus the active panel.

    The active panel is rendered here as well as by the callback, so the tab shows content on first
    paint rather than an empty container waiting for a click.
    """
    return html.Div([
        strip(PREFIX, PANELS, active=0),
        html.Div(panel_body(0, session_id, scope, cohort), id=body_id(PREFIX)),
    ])


# Registered so the table audit and the tests walk all three panels rather than only the one a tab
# body happens to render first.
register(PREFIX, PANELS, panel_body)

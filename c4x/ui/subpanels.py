"""Sub-panels: a second level of navigation inside one tab.

Breakdown was 5.7 screens and three subjects: what is in the window now, which items make it up,
and how it moved over time. A reader looking for one of those scrolled past the other two.

This is the same mechanism as the top-level tabs, deliberately. `_switch_tab` and `_render_tab` in
c4x/ui/callbacks/navigation.py already solve "one strip of buttons, a Store holding which is
active, and a container rendered on demand"; copying that shape rather than inventing a second one
means a reader who understands the tabs understands these, and the audit's callback coverage works
the same way for both.

Panels are rendered ON DEMAND, like the tabs. Building all three of Window's panels on every render
would cost the probe queries and the history chart whether or not anyone looked at them.
"""
from dash import dcc, html

from c4x.theme import ACCENT, BORDER, MONO, MUTED, PANEL, TEXT

# Every tab that carries sub-panels, so a checker can walk them the way it walks TABS.
#
# A tab body renders only its FIRST panel, so anything that enumerates tabs sees a third of a
# panelled tab and reports the rest as absent. The table audit found exactly that: 40 tables where
# there had been 74. Registering here means the next panelled tab is covered the moment it exists,
# rather than when someone remembers to add it to a checker.
PANELLED = {}


def register(prefix, panels, body):
    """Record a panelled tab: its prefix, its [(key, label, description)], and its body builder."""
    PANELLED[prefix] = {"panels": panels, "body": body}


def button_id(prefix, key):
    """The id of one sub-panel button. One definition, so the strip and its callback agree."""
    return f"{prefix}-panel-{key}"


def store_id(prefix):
    return f"{prefix}-panel-active"


def body_id(prefix):
    return f"{prefix}-panel-body"


def panel_style(active: bool) -> dict:
    """Quieter than a top-level tab: this is navigation WITHIN a tab, and styling it identically
    would leave two rows of equally loud buttons and no sense of which contains which."""
    return {
        "background": PANEL if active else "transparent",
        "color": TEXT if active else MUTED,
        "border": f"1px solid {ACCENT if active else BORDER}",
        "borderRadius": "999px",
        "padding": "4px 14px",
        "marginRight": "6px",
        "fontSize": "11.5px",
        "fontFamily": MONO,
        "fontWeight": 700 if active else 400,
        "cursor": "pointer",
    }


def strip(prefix, panels, active=0):
    """The button row and the Store that remembers the choice. NOT the body.

    The body is a separate container the caller owns, because one callback has to replace it, and
    a description rendered inside the strip would go stale the moment a different panel was chosen
    while the strip stayed put. Each panel states what it is at the top of its own body instead.
    """
    active = max(0, min(int(active or 0), len(panels) - 1))
    buttons = [
        html.Button(label, id=button_id(prefix, key), n_clicks=0,
                    style=panel_style(index == active))
        for index, (key, label, _description) in enumerate(panels)
    ]
    return html.Div([
        html.Div(buttons, style={"display": "flex", "alignItems": "center",
                                 "flexWrap": "wrap", "margin": "2px 0 10px 0"}),
        dcc.Store(id=store_id(prefix), data=active),
    ])


def description_note(panels, index):
    """The active panel's own one-line statement of what it is, for the top of its body."""
    text = panels[max(0, min(int(index or 0), len(panels) - 1))][2]
    if not text:
        return None
    return html.Div(text, style={"color": MUTED, "fontSize": "11.5px",
                                 "margin": "0 0 12px 0", "maxWidth": "900px",
                                 "lineHeight": "1.55"})


def active_index(triggered_id, prefix, panels, current):
    """Which panel a click selected, or the current one when nothing was clicked.

    Kept here rather than in the callback so the strip and the switch cannot disagree about which
    button maps to which index, which is the defect the single TABS registry exists to prevent.
    """
    keys = [button_id(prefix, key) for key, _label, _description in panels]
    if triggered_id in keys:
        return keys.index(triggered_id)
    return max(0, min(int(current or 0), len(panels) - 1))

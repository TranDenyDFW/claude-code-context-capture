#!/usr/bin/env python
"""
Context capture explorer - a local Dash app over data/context.db.

Shows what the harvester collected: per-session context growth, every compaction on record
with the mirror's predicted trigger, and a live threshold calculator.

The window math is NOT reimplemented here. It is read from tools/mirror-core.mjs at startup
and computed by tools/mirror.mjs on demand, so the app and the validated JS cannot drift apart.

Run:  python app.py          then open http://127.0.0.1:8056
Stop: Ctrl+C. There is no in-app quit: closing this viewer must not look like stopping capture,
      and it does not - the hooks harvest on their own whether or not this is running.

This file is a COMPOSITION ROOT. It creates the Dash instance, attaches the layout, imports the
callbacks so Dash registers them, and mounts the server routes. Nothing is implemented here:

    c4x/ui/header.py      the bar above the tabs
    c4x/ui/layout.py      the tab registry, the nav and the page skeleton
    c4x/ui/callbacks/     the twelve callbacks, grouped by what they drive
    c4x/server.py         the port, the shutdown route and the health check
    c4x/tabs/             one module per tab
"""

import os
import sys
from pathlib import Path

from dash import Dash

from c4x.server import port_from_argv, register_routes, run

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "context.db"
# --port beats C4X_PORT beats the default. Overridable because a fixed port is not a fixed port:
# the sibling repo runs the same app, Windows permits a second bind on an address already in use
# rather than refusing it, and two servers then answer on one port with no error anywhere.
PORT = port_from_argv(sys.argv, int(os.environ.get("C4X_PORT", "8056")))
DEBUG = os.environ.get("C4X_DEBUG") == "1"  # off by default: debug rotates JS chunk hashes

# suppress_callback_exceptions: the message table and its detail pane are built inside the session
# callback, so they do not exist when the layout is first validated. Without this, Dash refuses to
# register their callback at import time.
app = Dash(__name__, title="Context capture", update_title=None,
           suppress_callback_exceptions=True)
server = app.server

from c4x.ui.layout import build_layout  # noqa: E402

app.layout = build_layout()

# Importing these registers them: Dash's decorator writes into a global registry at import time.
# The names are bound here as well, because the checks below read them through this module.
# ---------------------------------------------------------------------------
# The surface other directories read
# ---------------------------------------------------------------------------
# tools/table_audit.py, tests/ and c4x/cli/ all reach into this module by name: app.TABS,
# app.session_view, app.THRESHOLDS and the rest. Those checks live outside this package on purpose,
# so that they see what a caller sees rather than what an insider knows.
#
# Listed explicitly rather than left as incidental imports. During the split that created these
# modules an automatic "unused import" cleanup removed several of them, and the result was an
# AttributeError in a test two directories away rather than a lint warning, which is a poor way to
# find out that an import was load-bearing.
from c4x.panels import turn_diff_panel  # noqa: E402, F401
from c4x.store import (  # noqa: E402, F401
    THRESHOLDS,
    cohort_options,
    compaction_dropped,
    q,
    session_turns,
)
from c4x.tabs import session_view  # noqa: E402, F401
from c4x.theme import GOOD, fmt_tokens  # noqa: E402, F401
from c4x.ui.callbacks.compare import _cmp_render, _cmp_targets  # noqa: E402, F401
from c4x.ui.callbacks.navigation import (  # noqa: E402, F401
    _render_tab,
    _switch_tab,
    _tab_styles,
)
from c4x.ui.callbacks.panels import (  # noqa: E402, F401
    _compaction_clicked,
    _message_clicked,
    _mirror,
    _session_controls,
)
from c4x.ui.callbacks.selection import (  # noqa: E402, F401
    _cohort_options,
    _finding_clicked,
    _pick_from_table,
    _selector_options,
    _tick,
)
from c4x.ui.callbacks.window import _window_panel, _window_panel_chosen  # noqa: E402, F401
from c4x.ui.header import refresh_store  # noqa: E402, F401
from c4x.ui.layout import SELECTION_SCOPED, TAB_IDS, TABS, tab_button, tab_style  # noqa: E402, F401

# The route below stays because this environment requires local web apps to expose a shutdown path.
# It is deliberately undocumented: no button, no README, no docstring. Removing the affordance is
# the point; removing the mechanism would break a requirement I cannot verify from here. The routes
# themselves, and why the shutdown one is POST-only, are in c4x/server.py.
register_routes(server, DB_PATH, PORT)


if __name__ == "__main__":
    run(app, PORT, DEBUG)

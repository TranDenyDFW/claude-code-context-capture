#!/usr/bin/env python
"""
Context capture explorer - the pane builders behind data/context.db.

Shows what the harvester collected: per-session context growth, every compaction on record
with the mirror's predicted trigger, and a live threshold calculator.

The window math is NOT reimplemented here. It is read from tools/mirror-core.mjs at startup
and computed by tools/mirror.mjs on demand, so the app and the validated JS cannot drift apart.

HEADLESS. This file no longer serves a page:

    python -m c4x.api      then open http://127.0.0.1:8059

It is still where every pane is built. The API renders through `_render_tab`, the same callback
the browser used to dispatch, which is what makes `tools/parity.py` a check on the transport
rather than on twenty reimplemented queries, and what let a React frontend replace the page
without reimplementing a single query. What ended is the second page: two dashboards on two ports
over one store is a way to read a stale number and not know it, which happened more than once
while both existed.

Stop: Ctrl+C on the API. There is no in-app quit: closing the viewer must not look like stopping
      capture, and it does not, because the hooks harvest whether or not anything is running.

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

# --db, BEFORE any c4x import.
#
# Every node tool in tools/ takes --db and the dashboard did not, so pointing it at another store
# meant setting C4X_DB in the environment, which is fine in a shell and unreliable everywhere else:
# a backgrounded launcher that re-spawns the process drops it, and the failure is silent because
# the app cheerfully opens the default store instead. A flag cannot be dropped.
#
# It has to run here rather than in main(), because c4x.store resolves its path at IMPORT time and
# every module below imports it. Setting the variable after that would change nothing and look
# like it had worked, which is the same silent failure with an extra step.
if "--db" in sys.argv:
    _at = sys.argv.index("--db")
    if _at + 1 < len(sys.argv):
        os.environ["C4X_DB"] = str(Path(sys.argv[_at + 1]).expanduser().resolve())

# --read-only: serve the store and never write to it.
#
# THE DASHBOARD IS A WRITER. Its refresh tick runs an incremental harvest so the page follows a
# live session, and that harvest writes into whatever store it was pointed at. Pointing it at a
# copy therefore does not leave the copy alone, which is surprising exactly when it matters: a
# redacted store built for public screenshots was verified clean, served for one tick, and had
# every real working directory written straight back into it.
if "--read-only" in sys.argv:
    os.environ["C4X_READ_ONLY"] = "1"

from dash import Dash  # noqa: E402 - after the --db handling above, deliberately

from c4x.server import port_from_argv, register_routes  # noqa: E402

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("C4X_DB") or (ROOT / "data" / "context.db"))
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
from c4x.ui.callbacks.crossfilter import (  # noqa: E402, F401
    _reread_crossfilter,
    _sessions_crossfilter,
    selected_ids,
)
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

# The routes stay registered on this Flask server even though nothing serves it any more, because
# removing the mechanism would break a requirement I cannot verify from here: this environment
# requires local web apps to expose a shutdown path. The app that ACTUALLY listens is now
# `c4x/api/`, which carries the same routes and imports this same implementation, so the two cannot
# disagree about what "stopped" means. Deliberately undocumented in both: no button, no link.
register_routes(server, DB_PATH, PORT)


def _headless_notice():
    """What to run instead. Printed rather than serving a second page.

    THIS FILE NO LONGER SERVES A UI. It is still the composition root, and everything below it is
    still where the panes are built: the API renders through `_render_tab`, the same callback the
    browser used to dispatch, which is what makes `tools/parity.py` a check on the transport rather
    than on twenty reimplemented queries. What changed is that there is one page now instead of two.

    Two dashboards on two ports, drawing the same store, is a way to read a stale number and not
    know it. That happened repeatedly while both existed.
    """
    port = int(os.environ.get("C4X_API_PORT", "8059"))
    print("app.py no longer serves a page. It is the data source behind the API now.")
    print()
    print("  python -m c4x.api")
    print(f"  then open http://127.0.0.1:{port}")
    print()
    print("Capture is unaffected either way: the hooks harvest whether or not anything is running.")
    return 0


if __name__ == "__main__":
    sys.exit(_headless_notice())

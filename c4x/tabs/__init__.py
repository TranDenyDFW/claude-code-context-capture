"""One module per tab, and the layouts app.py registers.

Ten files rather than one of 1394 lines. They have no edges between them: the two helpers a tab
borrowed from another, `latest_baseline` and `tool_spec`, are derivation rather than drawing and
live in c4x/breakdown.py now. Each imports downward only: theme, store, panels, breakdown.

Re-exported here so app.py names a tab rather than a file, and so moving a layout between modules
does not ripple into the TABS registry.
"""
from c4x.tabs.compactions import compactions_layout
from c4x.tabs.compare import compare_layout
from c4x.tabs.mirror import mirror_layout
from c4x.tabs.probes import probes_layout
from c4x.tabs.session import session_layout, session_view
from c4x.tabs.sessions import sessions_table_layout
from c4x.tabs.sources import sources_layout
from c4x.tabs.summary import decisions, overview_layout, project_totals_fig, summary_layout
from c4x.tabs.waste import waste_layout
from c4x.tabs.window import window_layout

__all__ = [
    "compactions_layout", "compare_layout", "decisions", "mirror_layout",
    "overview_layout", "probes_layout", "project_totals_fig", "session_layout", "session_view",
    "sessions_table_layout", "sources_layout", "summary_layout", "waste_layout",
    "window_layout",
]

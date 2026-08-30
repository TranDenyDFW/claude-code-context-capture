"""Every Dash callback, grouped by what it drives.

Importing this package registers all of them, because Dash's decorator writes into a global
registry at import time. app.py imports the names it re-exports from here.
"""
from c4x.ui.callbacks import compare, navigation, panels, selection  # noqa: F401

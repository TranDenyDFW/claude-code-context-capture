"""The one place dash's DataTable typing gap is acknowledged, instead of fifteen.

`dash.dash_table` ships a `DataTable` SUBMODULE and rebinds that same name to the component class
when the package is imported. Runtime therefore hands you the class and every call works; mypy
reads the source tree, finds the submodule, and reports `Module has no attribute "DataTable"` at the
attribute access and `Module not callable` at the call. Importing the class directly does not help,
because `from dash.dash_table import DataTable` resolves to the submodule for exactly the same
reason.

Fifteen call sites across eleven modules hit this. Naming it once here means one suppression to
read and one line to delete when dash publishes types, rather than fifteen identical comments that
would drift apart the moment someone edited one of them.

The annotation is deliberate. Without it the name keeps the module type the suppression hid, and
every call becomes `Module not callable` instead: the error moves rather than going away. mypy does
not check DataTable's arguments either way, since it has no signature for the class at all.
"""

from typing import Any

from dash import dash_table

DataTable: Any = dash_table.DataTable  # type: ignore[attr-defined]

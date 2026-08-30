"""The dashboard's own presentation layer, as opposed to what it reads.

`c4x/` below this is about the store and the tabs. This package is about the page around them: the
header bar, the shell, and the callbacks that connect the two. Nothing here imports upward into
app.py, so app.py is left as a composition root that wires the pieces together.
"""

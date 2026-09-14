"""Child processes of a server that has no console.

On Windows the hook starts the server as `pythonw.exe`, which owns no console, and a console
program started from a console-less parent is given a console of its own: a window that flashes
open and shut on every request that spawns one. That was `tasklist` on every page load and `node`
on every mirror prediction. `CREATE_NO_WINDOW` gives the child a console that is never shown, so
its stdout and stderr still flow through the pipes the caller asked for and nothing appears.

`subprocess.run` is looked up at call time, on purpose: `tests/test_delete_layers.py` patches it
to prove nothing shells out to the command the delete design avoids, and a reference bound at
import would slip past that guard.
"""
import subprocess
import sys

# 0x08000000 on Windows; absent elsewhere, where a child never gets a window of its own anyway.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(args, **kw):
    """`subprocess.run`, with no console window for the child on Windows. Other arguments pass
    through unchanged."""
    if sys.platform == "win32":
        kw["creationflags"] = int(kw.get("creationflags", 0)) | NO_WINDOW
    return subprocess.run(args, **kw)

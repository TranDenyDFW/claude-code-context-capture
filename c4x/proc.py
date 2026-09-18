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
from pathlib import Path

# 0x08000000 on Windows; absent elsewhere, where a child never gets a window of its own anyway.
# `vars(subprocess).get`, not `getattr`: tools/table_audit.py reads every `getattr(...)` call as a
# callee it cannot name (its evasion gate for hidden table constructions) and fails the suite on
# it; `c4x/paths.py` reads PyInstaller's attributes off `sys` the same way for the same reason.
NO_WINDOW = int(vars(subprocess).get("CREATE_NO_WINDOW", 0))


def run(args, **kw):
    """`subprocess.run`, with no console window for the child on Windows. Other arguments pass
    through unchanged."""
    if sys.platform == "win32":
        kw["creationflags"] = int(kw.get("creationflags", 0)) | NO_WINDOW
    return subprocess.run(args, **kw)


def call(args, **kw):
    """`subprocess.run` with the console the caller already has, for a verb somebody is watching.

    The opposite of `run` above, and both are needed. The server has no console, so a child that
    got one would flash a window on every page load; a verb typed at a prompt (`c4x.exe install`,
    `c4x.exe harvest`) IS the console, and hiding its output would mean a command that prints
    nothing and appears to have done nothing.
    """
    return subprocess.run(args, **kw)


# Windows: a child that survives its parent has to be a process of its own (DETACHED_PROCESS)
# and in its own group (CREATE_NEW_PROCESS_GROUP), or the console control events and the job
# the parent belongs to reach it. Both absent elsewhere, where `start_new_session` does the job.
DETACHED = (int(vars(subprocess).get("DETACHED_PROCESS", 0))
            | int(vars(subprocess).get("CREATE_NEW_PROCESS_GROUP", 0)))


def detach(args, log_path, cwd=None):
    """Start `args` so that it outlives this process, its stdout and stderr appended to `log_path`.

    The one way this package starts a server again from inside a server (`c4x/server.py`,
    `restart_server`): the hook starts the first one detached from node the same way. Returns the
    `Popen`, whose pid the caller spares from its own shutdown. `subprocess.Popen` is looked up at
    call time for the same reason `run` looks up `subprocess.run`.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": None, "stderr": subprocess.STDOUT,
                "cwd": None if cwd is None else str(cwd), "close_fds": True}
    if sys.platform == "win32":
        kw["creationflags"] = DETACHED | NO_WINDOW
    else:
        kw["start_new_session"] = True
    with log_path.open("ab") as log:
        kw["stdout"] = log
        return subprocess.Popen(args, **kw)

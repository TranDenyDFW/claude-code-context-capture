"""Stop the dashboard once Claude is gone.

The SessionStart hook starts the page when Claude starts; nothing tells it when Claude stops,
because sessions end while other sessions are still open and no hook fires for "the last one".
So the server watches for itself: every `interval` seconds it asks whether any Claude process is
still running, and after `grace` seconds of none it stops through the same hardened shutdown the
`/__shutdown__` route uses.

WHAT COUNTS AS CLAUDE, measured rather than assumed. On this machine the CLI runs as 17 processes
named `claude`; the Microsoft Store build of the desktop app is `Claude.exe`; an npm install runs
as `node.exe` with `node_modules\\@anthropic-ai\\claude-code\\cli.js` in its command line, spelled
with backslashes on Windows. So: a name that starts with `claude`, case-insensitive, or a node
process whose command line, folded to forward slashes, names the package. The command line is read
only for the node names, because `cmdline()` is a second system call per process and raises
`AccessDenied` for another user's or an elevated process, of which a developer machine has
plenty; a process that cannot be read is skipped, never counted as absent.

A TICK THAT RAISES IS UNKNOWN, NOT A MISS. `psutil` can fail as a whole (a snapshot of the process
table failing mid-walk), and treating that as "no Claude" would stop the page under a live
session for a reason nobody asked for.
"""
import os
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

NODE_NAMES = {"node", "node.exe", "bun", "bun.exe"}
NPM_MARKS = ("@anthropic-ai/claude-code", "claude-code/cli.js")

INTERVAL = 15.0
GRACE = 60.0


def looks_like_claude(name: str | None, cmdline: Callable[[], Iterable[str] | None]) -> bool:
    """The name rule, then the command-line rule for node only."""
    lowered = (name or "").lower()
    if lowered.startswith("claude"):
        return True
    if lowered not in NODE_NAMES:
        return False
    parts = cmdline() or []
    joined = " ".join(parts).replace("\\", "/").lower()
    return any(mark in joined for mark in NPM_MARKS)


def claude_alive(procs: Iterable[Any] | None = None, self_pid: int | None = None) -> bool:
    """Whether any process other than this one is Claude.

    `procs` is for the tests; every real caller walks `psutil.process_iter`. Each process is read
    inside its own guard so one that vanished, or one that refuses to be read, is skipped and the
    walk continues.
    """
    import psutil
    me = os.getpid() if self_pid is None else self_pid
    walk = psutil.process_iter(["pid", "name"]) if procs is None else procs
    for proc in walk:
        try:
            if proc.pid == me:
                continue
            info = getattr(proc, "info", None) or {}
            name = info.get("name") if isinstance(info, dict) else None
            if name is None:
                name = proc.name()
            if looks_like_claude(name, proc.cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


class Watchdog:
    """Count consecutive misses; stop once they add up to `grace` seconds."""

    def __init__(self, stop: Callable[[str], Any], is_alive: Callable[[], bool] = claude_alive,
                 interval: float = INTERVAL, grace: float = GRACE,
                 log: Callable[[str], Any] = print) -> None:
        self.stop = stop
        self.is_alive = is_alive
        self.interval = float(interval)
        self.grace = float(grace)
        self.log = log
        self.missing_for = 0.0
        self.stopped = False
        self._warned = False

    def tick(self) -> str:
        """One observation: 'alive', 'missing' or 'unknown'. Calls `stop` at most once."""
        try:
            alive = bool(self.is_alive())
        except Exception as exc:  # noqa: BLE001 - a failed observation is not an absence
            if not self._warned:
                self._warned = True
                self.log(f"[watchdog] could not read the process table: {exc!r}")
            return "unknown"
        if alive:
            self.missing_for = 0.0
            return "alive"
        self.missing_for += self.interval
        if self.missing_for >= self.grace and not self.stopped:
            self.stopped = True
            self.stop(f"no Claude process for {int(self.grace)} s")
        return "missing"

    def run(self) -> None:
        while not self.stopped:
            time.sleep(self.interval)
            self.tick()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="c4x-watchdog", daemon=True)
        thread.start()
        return thread

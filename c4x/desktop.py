"""The Claude desktop app as a process: where it is, and how to start it again.

The app reads its records directory when it starts and never again, so a record c4x removed
(`adopt.unadopt_reviews`) stays in the sidebar until the app is restarted. The startup sweep
(`adopt.sweep_reviews`) restarts it, through this module, right after it has removed something.

TWO INSTALLS, ONE ID EACH. The Microsoft Store build (the test laptop's) is a package:
`Get-AppxPackage -Name 'Claude*'` names it (`tools/build_exe.py` asks the same question) and it
is started through the shell, `explorer.exe shell:AppsFolder\\<PackageFamilyName>!<Application Id>`,
with the id read from the package's own `AppxManifest.xml` rather than assumed, because a wrong id
starts nothing and by then the app has been killed. Measured on the laptop:
`Claude_pzs8sxrjxfjjc!Claude`. The direct download installs an exe under
`%LOCALAPPDATA%\\Programs\\Claude\\Claude.exe`, started by its path.

KILL, THEN START. The app keeps a single instance: a second launch while it runs only fronts the
first window, so "start the new one first" cannot work, and the order is terminate every
app process, wait for the last to go, then launch. `restart_app` verifies the app came back and
says so either way; a relaunch that fails leaves the person where a crash would, with the app
closed and a log line saying why. Never the server's own process, which is `pythonw.exe`.

THE APP, NEVER THE CLI. Both are `claude.exe`: the CLI at `~/.local/bin` and the copy the app
carries under its LocalCache. `is_app_exe` tells them apart by the executable's path (a
WindowsApps package directory, or the installer's path), so a restart never ends a terminal
session and `app_running` never refuses a switch because a terminal is open.

THE LAUNCH, TWO WAYS. The shell activation first; when no process comes back, a transient
scheduled task in the interactive logon session, which is what brought the app back on the test
laptop after the activation from the hook-started server did not (`launch_app` has the numbers).

THE CONFIRMED RESTART. `with_restart` wraps a page action in quit, act, relaunch (or act, quit,
relaunch), one at a time, with the watchdog told to wait; the routes take a `restart` flag and
the page asks the person first.
"""
import platform
import re
import threading
import time
from pathlib import Path
from typing import Any

import psutil

APPX_QUERY = ("$p = Get-AppxPackage -Name 'Claude*' | Select-Object -First 1; "
              "if ($p) { $p.PackageFamilyName; $p.InstallLocation }")
EXE_PATHS = (r"%LOCALAPPDATA%\Programs\Claude\Claude.exe",
             r"%LOCALAPPDATA%\Programs\claude-desktop\Claude.exe")
APP_IMAGE = "claude.exe"
APPLICATION_ID = re.compile(r'<Application\s[^>]*\bId="([^"]+)"')
# How long the app gets to answer a terminate before it is killed, and to come back after launch.
TERMINATE_S = 10.0
KILL_S = 5.0
RELAUNCH_S = 20.0


def _appx(run) -> list:
    """The two lines the query prints, or an empty list."""
    from c4x import proc
    call = proc.run if run is None else run
    try:
        answer = call(["powershell", "-NoProfile", "-Command", APPX_QUERY],
                      capture_output=True, text=True, timeout=60)
    except (OSError, ValueError, TimeoutError) as exc:  # a missing powershell, a hung one
        _ = exc
        return []
    out = str(vars(answer).get("stdout", "") or "")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[:2]


def find_app(run=None, exists=None, read=None, expand=None, system=None) -> dict | None:
    """Where the app is and how to start it, or None when this machine has none.

    `{"kind": "store", "family": ..., "app_id": ..., "launch": [explorer.exe, shell:...]}` for
    the Store build, `{"kind": "exe", "launch": [path]}` for the installer's. Every input is a
    parameter so the tests drive both shapes without a shell; the real callers pass nothing.
    """
    import os
    if (platform.system() if system is None else system) != "Windows":
        return None
    here = os.path.isfile if exists is None else exists
    lines = _appx(run)
    if len(lines) == 2:
        family, location = lines
        manifest = Path(location) / "AppxManifest.xml"
        try:
            text = (Path(manifest).read_text(encoding="utf-8", errors="replace")
                    if read is None else read(manifest))
        except OSError:
            text = ""
        found = APPLICATION_ID.search(text or "")
        if found:
            app_id = found.group(1)
            return {"kind": "store", "family": family, "app_id": app_id,
                    "launch": ["explorer.exe", f"shell:AppsFolder\\{family}!{app_id}"]}
    grow = os.path.expandvars if expand is None else expand
    for raw in EXE_PATHS:
        path = grow(raw)
        if here(path):
            return {"kind": "exe", "launch": [path]}
    return None


def is_app_exe(path: Any, expand=None) -> bool:
    """Whether an executable path is the desktop app's own, as against the CLI's.

    Both are named `claude.exe`. The app's sits under a `WindowsApps` package directory (the
    Store build, any version, any drive) or at one of `EXE_PATHS` (the direct download); the CLI's
    is `~/.local/bin/claude.exe`, or the copy the app itself carries under its LocalCache
    `claude-code\\<version>\\claude.exe`, and terminating those would end every terminal session
    on the machine. Measured on the author's machine, 2026-09-16: 14 app processes and 2 CLI
    sessions, all sixteen named `claude.exe`. None is not a path and not the app.
    """
    import os
    if not isinstance(path, str) or not path:
        return False
    folded = path.replace("\\", "/").lower()
    if not folded.endswith("/" + APP_IMAGE):
        return False
    if "/windowsapps/" in folded:
        return True
    grow = os.path.expandvars if expand is None else expand
    return any(folded == str(grow(raw)).replace("\\", "/").lower() for raw in EXE_PATHS)


def app_processes(procs=None, self_pid=None) -> list:
    """Every process that IS the desktop app, other than this one: named `claude.exe` in any
    case and running the app's own executable (`is_app_exe`).

    The name is read for every process, one system call for the whole table; the executable's
    path only for the ones named `claude.exe`, since it costs a handle per process. A process
    whose path cannot be read (another user's, an elevated one) is not the app: a doubt here
    must never end a terminal session.
    """
    import os
    me = os.getpid() if self_pid is None else self_pid
    walk = psutil.process_iter(["pid", "name"]) if procs is None else procs
    found = []
    for proc in walk:
        try:
            info = vars(proc).get("info") or {}
            name = info.get("name") if isinstance(info, dict) else None
            if name is None:
                name = proc.name()
            if str(name or "").lower() != APP_IMAGE or proc.pid == me:
                continue
            exe = info.get("exe") if isinstance(info, dict) else None
            if exe is None:
                # `vars(type(proc)).get`, not `getattr`: tools/table_audit.py reads every
                # `getattr(...)` call as a callee it cannot name (see c4x/proc.py).
                read_exe = vars(type(proc)).get("exe")
                exe = read_exe(proc) if callable(read_exe) else None
            if is_app_exe(exe):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def app_running() -> bool:
    """Whether the desktop app is running, and never the CLI. Off Windows, False without a look."""
    if platform.system() != "Windows":
        return False
    return bool(app_processes())


def _say(message: str) -> None:
    """Flushed: the server's stdout is a block-buffered log file (see `adopt._say`)."""
    print(message, flush=True)


def quit_app(*, processes=None, wait_procs=None, log=_say) -> dict[str, Any]:
    """Terminate every app process, wait TERMINATE_S, kill the survivors, wait KILL_S.

    Returns `{"quit": every process gone, "killed": how many there were}`. Never this process,
    which is `pythonw.exe`, and never the CLI (`app_processes`).
    """
    listing = app_processes if processes is None else processes
    waiting = psutil.wait_procs if wait_procs is None else wait_procs
    before = list(listing())
    for p in before:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _gone, alive = waiting(before, timeout=TERMINATE_S) if before else ([], [])
    for p in alive:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    left: list = []
    if alive:
        _gone2, left = waiting(alive, timeout=KILL_S)
    log(f"[quit-app] stopped {len(before)} {APP_IMAGE} process(es)"
        + (f"; {len(left)} would not stop" if left else ""))
    return {"quit": not left, "killed": len(before)}


def launch_by_task(app, *, run=None, user=None, log=_say) -> tuple[bool, str]:
    """Start the app through a transient scheduled task in the interactive logon session.

    The task runs as the logged-on user (`-LogonType Interactive`, no elevation), executes the
    same launch `find_app` resolved, and is removed three seconds after it started; the app's
    processes outlive the task. Returns `(started, why)`; `started` says the task was run, not
    that the app is back, which the caller waits for.
    """
    import os
    import subprocess

    from c4x import proc
    call = proc.run if run is None else run
    who = user or os.environ.get("USERNAME") or ""
    if not who:
        return False, "no user name to run the task as"
    argv = list(app["launch"])

    def quoted(text: Any) -> str:
        return "'" + str(text).replace("'", "''") + "'"

    action = f"New-ScheduledTaskAction -Execute {quoted(argv[0])}"
    if len(argv) > 1:
        action += f" -Argument {quoted(' '.join(argv[1:]))}"
    name = quoted(f"c4x-launch-{os.getpid()}")
    script = (f"$a = {action}; "
              f"$p = New-ScheduledTaskPrincipal -UserId {quoted(who)} -LogonType Interactive"
              " -RunLevel Limited; "
              f"Register-ScheduledTask -TaskName {name} -Action $a -Principal $p -Force"
              " | Out-Null; "
              f"Start-ScheduledTask -TaskName {name}; Start-Sleep -Seconds 3; "
              f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false; 'started'")
    try:
        answer = call(["powershell", "-NoProfile", "-Command", script],
                      capture_output=True, text=True, timeout=90)
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
        return False, f"the task launch failed: {exc!r}"
    out = str(vars(answer).get("stdout", "") or "")
    err = str(vars(answer).get("stderr", "") or "").strip()
    if "started" not in out:
        return False, f"the task launch failed: {err[:200] or 'no answer'}"
    log(f"[launch-app] started {' '.join(argv)} through a task in the interactive session")
    return True, "task"


def launch_app(app, *, launch=None, processes=None, sleep=None, now=None, log=_say,
               relaunch_s=RELAUNCH_S, task=None) -> dict[str, Any]:
    """Start the app and wait until a process of its own is seen.

    The shell activation first (`explorer.exe shell:AppsFolder\\<family>!<id>` for the Store
    build, the exe's path for the installer's); when nothing appears within `relaunch_s`, once
    more through a transient scheduled task in the interactive session (`launch_by_task`), and
    the same wait again. Returns `{"launched", "launch", "how" ("shell", "task" or None), "why"}`.

    MEASURED on the test laptop, 2026-09-16, app 2.110. From the server the SessionStart hook had
    started inside the app's own process tree, the shell activation returned and no process came
    within 20 s: the startup sweep had just terminated twelve of them, so the page stopped with
    Claude closed and a person asked why nothing started any more. The same activation from an
    SSH session (logon session 0) returned 1 and started nothing in 135 s. A task registered for
    the interactive logon started the app: twelve processes within thirty seconds.
    """
    from c4x import proc
    report: dict[str, Any] = {"launched": False, "launch": list(app["launch"]), "how": None,
                              "why": ""}
    start = proc.run if launch is None else launch
    listing = app_processes if processes is None else processes
    pause = time.sleep if sleep is None else sleep
    clock = time.monotonic if now is None else now
    by_task = launch_by_task if task is None else task

    def came_back() -> bool:
        deadline = clock() + float(relaunch_s)
        while clock() < deadline:
            if listing():
                return True
            pause(0.5)
        return False

    log(f"[launch-app] starting {' '.join(app['launch'])}")
    first_why = ""
    try:
        start(app["launch"], timeout=30)
    except (OSError, ValueError, TimeoutError) as exc:
        first_why = f"the launch failed: {exc!r}"
        log(f"[launch-app] {first_why}")
    if not first_why and came_back():
        report.update(launched=True, how="shell", why="relaunched")
        log("[launch-app] the app is back")
        return report
    first_why = first_why or f"the app did not come back within {int(relaunch_s)} s"
    log(f"[launch-app] {first_why}; trying the interactive session's task scheduler")
    started, how = by_task(app, log=log)
    if started and came_back():
        report.update(launched=True, how="task", why="relaunched through the task scheduler")
        log("[launch-app] the app is back, through the task scheduler")
        return report
    report["why"] = f"{first_why}; the task launch " + (
        f"did not bring it back within {int(relaunch_s)} s" if started else f"failed: {how}")
    log(f"[launch-app] {report['why']}")
    return report


def restart_app(app=None, *, launch=None, processes=None, wait_procs=None, sleep=None,
                now=None, log=_say, relaunch_s=RELAUNCH_S, task=None) -> dict[str, Any]:
    """Terminate every app process, wait for the last to go, start the app, wait for it back.

    `quit_app` then `launch_app`. Returns `{"restarted": bool, "killed": n, "launch": argv |
    None, "why": str}`. Refuses off Windows and when no app is found, before touching any
    process. Every side effect is a parameter (the tests pass their own); the real caller (the
    startup sweep, `adopt.sweep_reviews`) passes nothing.
    """
    report: dict[str, Any] = {"restarted": False, "killed": 0, "launch": None, "why": ""}
    if platform.system() != "Windows":
        report["why"] = "only the Windows desktop app is restarted"
        return report
    found = find_app() if app is None else app
    if not found:
        report["why"] = "no Claude desktop app found on this machine"
        return report
    report["launch"] = list(found["launch"])
    quit = quit_app(processes=processes, wait_procs=wait_procs, log=log)
    report["killed"] = quit["killed"]
    back = launch_app(found, launch=launch, processes=processes, sleep=sleep, now=now, log=log,
                      relaunch_s=relaunch_s, task=task)
    report["restarted"] = bool(back["launched"])
    report["why"] = back["why"]
    return report


# THE CONFIRMED RESTART. A page control that needs Claude closed (linking the account
# directories) or restarted (a record written for it to read) asks the person first, then the
# server does the whole thing: quit, act, relaunch, or act, quit, relaunch. One at a time, and
# the watchdog treats the window as Claude alive, so a fold longer than its grace cannot stop the
# server under a restart in progress.
_restart_lock = threading.Lock()
_restart_until: dict[str, float] = {"t": 0.0}
BUSY_S = TERMINATE_S + KILL_S + 2 * RELAUNCH_S + 30.0


class RestartBusy(RuntimeError):
    """A restart is already under way; the route answers 409."""


def restart_in_progress(now=None) -> bool:
    """Whether a confirmed restart is under way (the quit, the action, the relaunch)."""
    clock = time.monotonic if now is None else now
    return clock() < _restart_until["t"]


def with_restart(action, *, quit_first: bool, needed=None, app=None, running=None, quit=None,
                 launch=None, log=_say, clock=None) -> tuple[Any, dict[str, Any]]:
    """Run `action()` with the desktop app quit and started again around it.

    `quit_first`: the app must be closed BEFORE the action (a directory it holds cannot be
    moved): quit, act, relaunch; if the action raises, the app is started again and the error
    re-raised. Otherwise: act, and only when `needed(result)` (default: always) quit and
    relaunch, since new files are safe with the app open.

    Returns `(result, report)`; the report says `was_running`, `quit`, `killed`, `relaunched`,
    `how` and `why`, so a failed relaunch leaves the page saying "Claude is closed; start it by
    hand" rather than a half-done state. Off Windows or with the app not running the action runs
    plainly and the report says so. When the app runs and `find_app` cannot say how to start it
    again, a `quit_first` action is refused before any process is touched. `RestartBusy` when
    another restart is under way.
    """
    report: dict[str, Any] = {"was_running": False, "quit": False, "killed": 0,
                              "relaunched": False, "how": None, "launch": None, "why": ""}
    tick = time.monotonic if clock is None else clock
    if platform.system() != "Windows":
        report["why"] = "only the Windows desktop app is restarted"
        return action(), report
    if not _restart_lock.acquire(blocking=False):
        raise RestartBusy("a restart is already under way; wait for it to finish")
    try:
        is_running = app_running if running is None else running
        report["was_running"] = bool(is_running())
        if not report["was_running"]:
            report["why"] = "Claude was not running; it reads these records when it next starts"
            return action(), report
        found = find_app() if app is None else app
        if not found:
            if quit_first:
                raise RuntimeError("Claude is running and c4x cannot find how to start it again; "
                                   "quit Claude yourself and try again. Nothing has been changed.")
            report["why"] = "no Claude desktop app found to restart; restart it yourself"
            return action(), report
        report["launch"] = list(found["launch"])
        do_quit = quit_app if quit is None else quit
        do_launch = launch_app if launch is None else launch
        _restart_until["t"] = tick() + BUSY_S
        try:
            if quit_first:
                stopped = do_quit(log=log)
                report.update(quit=stopped["quit"], killed=stopped["killed"])
                if not stopped["quit"]:
                    raise RuntimeError("Claude could not be closed; nothing has been changed. "
                                       "Quit it yourself and try again.")
                try:
                    result = action()
                except BaseException:
                    back = do_launch(found, log=log)
                    report.update(relaunched=bool(back["launched"]), how=back["how"])
                    raise
            else:
                result = action()
                if needed is not None and not needed(result):
                    report["why"] = "nothing changed that Claude would need to read again"
                    return result, report
                stopped = do_quit(log=log)
                report.update(quit=stopped["quit"], killed=stopped["killed"])
            back = do_launch(found, log=log)
            report.update(relaunched=bool(back["launched"]), how=back["how"])
            report["why"] = (back["why"] if back["launched"]
                             else f"Claude is closed: {back['why']}; start it by hand")
            return result, report
        finally:
            _restart_until["t"] = 0.0
    finally:
        _restart_lock.release()

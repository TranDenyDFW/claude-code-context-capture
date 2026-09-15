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
`claude.exe`, wait for the last to go, then launch. `restart_app` verifies the app came back and
says so either way; a relaunch that fails leaves the person where a crash would, with the app
closed and a log line saying why. Never the server's own process, which is `pythonw.exe`.
"""
import platform
import re
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


def app_processes(procs=None, self_pid=None) -> list:
    """Every process whose image is `claude.exe`, in any case, other than this one."""
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
            if str(name or "").lower() == APP_IMAGE and proc.pid != me:
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def _say(message: str) -> None:
    """Flushed: the server's stdout is a block-buffered log file (see `adopt._say`)."""
    print(message, flush=True)


def restart_app(app=None, *, launch=None, processes=None, wait_procs=None, sleep=None,
                now=None, log=_say, relaunch_s=RELAUNCH_S) -> dict[str, Any]:
    """Terminate every `claude.exe`, wait for the last to go, start the app, wait for it back.

    Returns `{"restarted": bool, "killed": n, "launch": argv | None, "why": str}`. Refuses off
    Windows and when no app is found, before touching any process. Every side effect is a
    parameter (the tests pass their own); the real caller passes nothing.
    """
    from c4x import proc
    report: dict[str, Any] = {"restarted": False, "killed": 0, "launch": None, "why": ""}
    if platform.system() != "Windows":
        report["why"] = "only the Windows desktop app is restarted"
        return report
    found = find_app() if app is None else app
    if not found:
        report["why"] = "no Claude desktop app found on this machine"
        return report
    report["launch"] = list(found["launch"])
    listing = app_processes if processes is None else processes
    waiting = psutil.wait_procs if wait_procs is None else wait_procs
    pause = time.sleep if sleep is None else sleep
    clock = time.monotonic if now is None else now
    start = proc.run if launch is None else launch

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
    if alive:
        waiting(alive, timeout=KILL_S)
    report["killed"] = len(before)
    log(f"[restart-app] stopped {len(before)} {APP_IMAGE} process(es); starting "
        f"{' '.join(found['launch'])}")
    try:
        start(found["launch"], timeout=30)
    except (OSError, ValueError, TimeoutError) as exc:
        report["why"] = f"the launch failed: {exc!r}"
        log(f"[restart-app] {report['why']}")
        return report
    deadline = clock() + float(relaunch_s)
    while clock() < deadline:
        if listing():
            report["restarted"] = True
            report["why"] = "relaunched"
            log("[restart-app] the app is back")
            return report
        pause(0.5)
    report["why"] = f"the app did not come back within {int(relaunch_s)} s"
    log(f"[restart-app] {report['why']}")
    return report

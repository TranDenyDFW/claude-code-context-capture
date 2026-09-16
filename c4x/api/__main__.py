"""Run the API.

    python -m c4x.api                       # 127.0.0.1:8059
    python -m c4x.api --port 8060
    python -m c4x.api --db tmp/demo-store.db
    python -m c4x.api --reload              # restart on source changes, for development
    python -m c4x.api --no-writes           # refuse project export, import and delete
    python -m c4x.api --watchdog            # stop about a minute after the last Claude process
    python -m c4x.api --self-test           # no store

Port 8059 by default, beside the dashboard's 8056 rather than on top of it, because both are meant
to run at once during the migration and Windows lets a second process bind a port already in use
rather than refusing it. Two servers answering one port, one of them stale, is a bug this repo has
already paid for once. So before binding, the port is ASKED who holds it (`already_running`): a
c4x server for this store means there is nothing to do and the exit is 0, anything else means
exit 3, because binding on top would give two answers to one address.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8059
OURS = "ours"
NOT_C4X = "something that is not a c4x dashboard"


def port_from_argv(argv, default=DEFAULT_PORT):
    """--port beats C4X_API_PORT beats the default. Malformed values fall back rather than crash."""
    if "--port" in argv:
        at = argv.index("--port")
        if at + 1 < len(argv):
            try:
                return int(argv[at + 1])
            except ValueError:
                pass
    try:
        return int(os.environ.get("C4X_API_PORT", default))
    except ValueError:
        return default


def db_from_argv(argv):
    """--db, resolved and exported before anything imports the store.

    Same handling as app.py, and for the same reason: `c4x.store` resolves its path at import time,
    so setting the variable afterwards changes nothing while looking like it worked.
    """
    if "--db" in argv:
        at = argv.index("--db")
        if at + 1 < len(argv):
            return str(Path(argv[at + 1]).expanduser().resolve())
    return None


def wants_watchdog(argv):
    return "--watchdog" in argv


def wants_review_sweep(argv, env=None):
    """The sweep at startup (`adopt.sweep_reviews`) is on unless `--no-review-sweep` or
    `C4X_NO_REVIEW_SWEEP=1` says otherwise. The install receipt's `reviewSweep: false` reaches
    here as the flag, through `tools/dashboard.mjs launchArgv`."""
    environment = os.environ if env is None else env
    return "--no-review-sweep" not in argv and environment.get("C4X_NO_REVIEW_SWEEP") != "1"


def after_pid(argv):
    """`--after <pid>`: the process to outwait before binding, or None.

    A restart (`c4x.server.restart_server`) starts its replacement while it still holds the port
    and passes its own pid; the replacement waits for it to go rather than finding the port held
    and exiting with "already running".
    """
    if "--after" in argv:
        i = argv.index("--after")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
    return None


def wait_for_exit(pid, timeout=30.0, exists=None, sleep=time.sleep, now=time.monotonic):
    """Block until `pid` is gone or `timeout` passes. True when it went."""
    import psutil
    there = psutil.pid_exists if exists is None else exists
    deadline = now() + float(timeout)
    while now() < deadline:
        if not there(pid):
            return True
        sleep(0.25)
    return not there(pid)


def _say(message):
    """Flushed: stdout is a block-buffered log file when the hook starts this server, and a
    line written from a thread after startup would otherwise wait in the buffer for the exit."""
    print(message, flush=True)


def _reconcile_summary(report) -> str:
    """One line for the log: what the reconcile did, or why it did nothing."""
    if not report.get("ran"):
        return f"skipped: {report.get('why') or 'nothing'}"
    roots = report.get("roots", [])
    linked = sum(len(r["linked"]) for r in roots)
    moved = sum(len(r["moved"]) for r in roots)
    aside = sum(len(r["set_aside"]) for r in roots)
    relinked = sum(len(r.get("relinked", [])) for r in roots)
    copied = sum(len(d.get("copied", [])) for r in roots for d in r.get("relinked", []))
    parts = [report.get("why") or "ran"]
    if linked or moved or aside:
        parts.append(f"{linked} linked, {moved} moved, {aside} set aside")
    if relinked:
        parts.append(f"{relinked} re-pointed, {copied} copied through")
    if report.get("backup"):
        parts.append(f"backup {report['backup']}")
    if report.get("marker_written"):
        parts.append("marker written")
    return "; ".join(parts)


def reconcile_then_stop(reason, *, reconcile=None, stop=None, log=_say):
    """The watchdog's stop: cover the pairs the app created since sharing, then shut down.

    WHY HERE. Sharing every account's records is done with junctions, and a junction covers the
    pair it was made for. The app creates `<account>/<org>` fresh when an account signs in with an
    organisation the junctions never named, and from then on that account reads a private list;
    at the next switch the app folds that directory into the shared one and the account comes
    back to nothing (measured 2026-09-15, fifteen chats). c4x cannot move a directory the app
    holds open, and the watchdog's stop is the one moment the server is alive with the app closed:
    sixty seconds after the last Claude process. So the reconcile runs first, and the stop runs
    whatever the reconcile did: a raise in the fold is logged and the server still goes down.
    Skipped under `C4X_NO_WRITES` (a read-only server moves nothing).
    """
    if stop is None:
        from c4x.server import hardened_shutdown
        stop = hardened_shutdown
    try:
        if os.environ.get("C4X_NO_WRITES"):
            log("[reconcile] skipped: --no-writes")
            return
        if reconcile is None:
            from c4x import accounts
            reconcile = accounts.reconcile
        report = reconcile()
        log(f"[reconcile] {_reconcile_summary(report)}")
    except Exception as exc:  # noqa: BLE001 - the stop must run whatever the fold did
        log(f"[reconcile] failed: {exc}")
    finally:
        stop(reason)


def reconcile_at_start(*, run=None, running=None, log=_say):
    """At server start, cover the pairs the app created since sharing, when the app is not open.

    A server started by hand or from a terminal session, with the desktop app closed, is a server
    alive at the moment the reconcile needs; the watchdog's stop is the other. Logged, never
    raised: the server starts whatever the reconcile did. Returns the report, or None when it
    did not run.
    """
    if os.environ.get("C4X_NO_WRITES"):
        log("[reconcile] at start: skipped (--no-writes)")
        return None
    try:
        if running is None:
            from c4x import accounts
            running = accounts.app_running
        if running():
            log("[reconcile] at start: Claude is running; covered when it next closes")
            return None
        if run is None:
            from c4x import accounts
            run = accounts.reconcile
        report = run()
        log(f"[reconcile] at start: {_reconcile_summary(report)}")
        return report
    except Exception as exc:  # noqa: BLE001 - the server starts whatever the fold did
        log(f"[reconcile] at start: failed: {exc}")
        return None


def start_review_sweep(port, run, db_path, *, probe=None, tries=60, every=0.5, log=_say,
                       sleep=time.sleep):
    """A thread that waits for our own `/__health__` answer, then calls `run()` once.

    Started before `uvicorn.run` (which never returns), so the wait is what makes it "after the
    server is up": the sweep removes records and restarts the app, and a page opened by the
    restarted app's first session must find the server answering. Never raises into the server:
    a failed sweep is a log line.
    """
    ask = already_running if probe is None else probe

    def _go():
        for _ in range(int(tries)):
            if ask(port, db_path, timeout=1.0) == OURS:
                break
            sleep(every)
        else:
            log("[review sweep] the port never answered; not run")
            return
        try:
            report = run()
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the server
            log(f"[review sweep] failed: {exc!r}")
            return
        log(f"[review sweep] done: {json.dumps(report, default=str)}")

    thread = threading.Thread(target=_go, name="c4x-review-sweep", daemon=True)
    thread.start()
    return thread


def already_running(port, db_path, timeout=1.0):
    """Who answers `/__health__` on this port: None, OURS, or a description of the holder.

    None means nothing answered (refused, or a listener that never replied within `timeout`), and
    the caller may bind. OURS means a c4x server for THIS store, compared as resolved paths and
    case-folded on Windows, because the server reports `DB_PATH.as_posix()` and a caller's spelling
    of the same file need not match it character for character. Anything else is described so the
    caller can print it: another store's dashboard, or a listener that is not one at all.

    The store path is a parameter rather than `store.DB_PATH` so this can be asked without a store,
    which is what the self-test does.

    No proxy. `urllib` honours `http_proxy` from the environment, and a loopback probe routed
    through a corporate proxy would answer for the wrong machine or not at all.
    """
    url = f"http://127.0.0.1:{int(port)}/__health__"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            if response.status != 200:
                return NOT_C4X
            body = response.read(4096)
    except urllib.error.HTTPError:
        return NOT_C4X
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        answer = json.loads(body)
    except ValueError:
        return NOT_C4X
    if not isinstance(answer, dict) or not answer.get("ok"):
        return NOT_C4X
    if not isinstance(answer.get("db"), str):
        return NOT_C4X
    theirs = os.path.normcase(str(Path(answer["db"]).resolve()))
    ours = os.path.normcase(str(Path(db_path).resolve()))
    if theirs == ours:
        return OURS
    return f"a c4x dashboard for {answer['db']}"


def self_test():
    """What can be checked without a store: the flags, and the probe against real sockets."""
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from c4x import paths

    def free_port():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def serve(body, status=200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    here = str(Path("tmp/self-test-store.db").resolve())
    posix_here = Path(here).as_posix()
    respelled = f"{Path(here).parent}{os.sep}.{os.sep}{Path(here).name}"
    if os.name == "nt":
        respelled = respelled.replace("\\", "/").upper()
    ours = serve(json.dumps({"ok": True, "db": posix_here, "port": 1}))
    theirs = serve(json.dumps({"ok": True, "db": Path("tmp/other.db").resolve().as_posix()}))
    garbage = serve("<html>not json</html>")
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    started = time.monotonic()
    silent_answer = already_running(silent.getsockname()[1], here, timeout=0.5)
    silent_took = time.monotonic() - started
    was_frozen = paths.FROZEN
    paths.FROZEN = True
    try:
        frozen_reload = main(["--reload"])
    finally:
        paths.FROZEN = was_frozen

    # The restart's wait and the sweep thread, with a fake clock and a fake port.
    sweep_checks: dict = {}
    alive = {"n": 3}

    def fading(pid):
        alive["n"] -= 1
        return alive["n"] > 0
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 1.0
        return ticks["t"]
    sweep_checks["waited"] = wait_for_exit(1, timeout=30.0, exists=fading, sleep=lambda s: None,
                                           now=clock)
    sweep_checks["timed_out"] = wait_for_exit(1, timeout=2.0, exists=lambda pid: True,
                                              sleep=lambda s: None, now=clock)
    asked = {"n": 0}

    def probe(port, db, timeout=1.0):
        asked["n"] += 1
        return OURS if asked["n"] >= 2 else None
    ran = {"n": 0}

    def run():
        ran["n"] += 1
        return {"removed": 0}
    start_review_sweep(1, run, here, probe=probe, every=0, log=lambda m: None,
                       sleep=lambda s: None).join(5)
    sweep_checks["ran"], sweep_checks["asked"] = ran["n"], asked["n"]
    never = {"n": 0}

    def never_run():
        never["n"] += 1
    start_review_sweep(1, never_run, here, probe=lambda *a, **k: None, tries=3, every=0,
                       log=lambda m: None, sleep=lambda s: None).join(5)
    sweep_checks["never_ran"] = never["n"]

    cases = [
        ("--port wins over everything", port_from_argv(["--port", "9999"]) == 9999),
        ("a missing --port value falls back", port_from_argv(["--port"]) == DEFAULT_PORT),
        ("a non-numeric --port falls back", port_from_argv(["--port", "abc"]) == DEFAULT_PORT),
        ("the default is not the dashboard's port", DEFAULT_PORT != 8056),
        ("--db resolves to an absolute path",
         Path(db_from_argv(["--db", "tmp/x.db"]) or "").is_absolute()),
        ("no --db means no override", db_from_argv([]) is None),
        ("a missing --db value is not an override", db_from_argv(["--db"]) is None),
        # The port has to reach `/__health__`, which reads C4X_API_PORT and nothing else. With
        # --port 8061 and the variable unset it reported 8059 while serving 8061.
        ("--port is a number main() can export", port_from_argv(["--port", "8061"]) == 8061),
        ("--watchdog is parsed", wants_watchdog(["--watchdog"]) and not wants_watchdog([])),
        ("the review sweep is on by default and off by flag or by environment",
         wants_review_sweep([], env={}) and not wants_review_sweep(["--no-review-sweep"], env={})
         and not wants_review_sweep([], env={"C4X_NO_REVIEW_SWEEP": "1"})),
        ("--after names the predecessor, and a missing or odd value is none",
         after_pid(["--after", "4242"]) == 4242 and after_pid(["--after"]) is None
         and after_pid(["--after", "x"]) is None and after_pid([]) is None),
        ("the predecessor is waited for, and the wait ends when it goes",
         sweep_checks["waited"] is True and sweep_checks["timed_out"] is False),
        ("the sweep runs once, after the port answers as ours",
         sweep_checks["ran"] == 1 and sweep_checks["asked"] >= 2),
        ("a port that never answers runs no sweep (gate can fail)",
         sweep_checks["never_ran"] == 0),
        ("nothing answers on a closed port", already_running(free_port(), here) is None),
        ("a listener that never answers is nobody, within the timeout",
         silent_answer is None and silent_took < 3.0),
        # A second spelling of the same file: a `.` segment everywhere, and upper case only on
        # Windows, where the filesystem folds case. Upper-casing on Linux names a different file,
        # and this check said OURS there once, which CI caught.
        ("our own store answers OURS, whatever the spelling",
         already_running(ours.server_port, respelled) == OURS),
        ("another store's dashboard is named",
         (already_running(theirs.server_port, here) or "").startswith("a c4x dashboard for ")),
        ("a listener that is not a dashboard is said to be one",
         already_running(garbage.server_port, here) == NOT_C4X),
        ("--reload is refused when frozen", frozen_reload == 2),
    ]
    for server in (ours, theirs, garbage):
        server.shutdown()
    silent.close()
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()

    reload = "--reload" in argv
    from c4x.paths import FROZEN, install_root
    if reload and FROZEN:
        # uvicorn's reloader re-executes the interpreter, which frozen is this exe, with its own
        # arguments: a recursive launch rather than an error.
        print("--reload is for a source checkout; the executable has no source to watch")
        return 2

    chosen = db_from_argv(argv)
    if chosen:
        os.environ["C4X_DB"] = chosen

    # Set before the app is imported, like C4X_DB, because the routes read it per request but
    # /api/health is what the frontend asks once at startup to decide whether to show the controls.
    if "--no-writes" in argv:
        os.environ["C4X_NO_WRITES"] = "1"

    port = port_from_argv(argv)
    # EXPORTED, not just used to bind. `/__health__` reports the port from this variable, so with
    # `--port 8061` and the variable unset it answered 8061 requests by saying 8059. Anything using
    # that endpoint to find the server got the wrong answer, and the wrong answer looked ordinary.
    # Same handling as C4X_DB above, and for the same reason: set before the app is imported.
    os.environ["C4X_API_PORT"] = str(port)

    from c4x import store
    predecessor = after_pid(argv)
    if predecessor is not None:
        # A restart's replacement: the server that started us holds the port until it has gone.
        if not wait_for_exit(predecessor):
            print(f"pid {predecessor} did not exit within 30 s; asking the port anyway")
    holder = already_running(port, store.DB_PATH)
    if holder == OURS:
        print(f"c4x api already running on http://127.0.0.1:{port} for {store.DB_PATH}")
        return 0
    if holder is not None:
        print(f"port {port} is held by {holder}; not binding on top of it")
        return 3

    import uvicorn

    print(f"c4x api on http://127.0.0.1:{port}/api/docs")
    print(f"  store: {store.DB_PATH}")
    print("  read-only: this server never harvests, unlike the dashboard")
    # The shutdown token, printed here and nowhere else. No route reports it, so a page the browser
    # visits cannot read it, and it dies with the process.
    from c4x.server import announce_shutdown_token
    announce_shutdown_token(port)
    print("  project export/import/delete: "
          + ("OFF (--no-writes)" if os.environ.get("C4X_NO_WRITES") else "on"))
    if wants_watchdog(argv):
        from c4x import desktop
        from c4x.watchdog import GRACE, Watchdog, claude_alive
        # A confirmed restart (quit, act, relaunch) is Claude alive to the watchdog, so a fold
        # longer than the grace cannot stop this server under it.
        Watchdog(stop=reconcile_then_stop,
                 is_alive=lambda: desktop.restart_in_progress() or claude_alive()).start()
        print(f"  watchdog: stops once no Claude process has been seen for {int(GRACE)} s, and "
              "first covers any account pair the app created since sharing")
    # THE RECONCILE AT START: a server started with the app closed can cover the pairs now.
    reconcile_at_start()
    if reload:
        print("  reloading on source changes")
    # THE SWEEP AT STARTUP, once the port answers. Exported as an environment variable too, so
    # `/api/adopt/sweep` can say the sweep is off on this server rather than only silent.
    if "--no-review-sweep" in argv:
        os.environ["C4X_NO_REVIEW_SWEEP"] = "1"
    sweep_off = ("--no-writes" if os.environ.get("C4X_NO_WRITES")
                 else "--no-review-sweep" if not wants_review_sweep(argv)
                 else "--reload" if reload else None)
    if sweep_off:
        print(f"  review sweep: off ({sweep_off})", flush=True)
    else:
        from c4x import adopt
        start_review_sweep(port, adopt.sweep_reviews, store.DB_PATH)
        print("  review sweep: once the port answers, records c4x wrote for review runs are "
              "taken back and Claude is restarted (--no-review-sweep turns it off)", flush=True)

    # host is fixed, not configurable. This process can read every conversation on the machine.
    #
    # RELOAD TAKES AN IMPORT STRING, not the app object: uvicorn's reloader re-imports the module in
    # a child process, and handing it an already-constructed app silently disables reloading while
    # printing nothing. Worth the branch, because a server quietly serving code from before the last
    # edit cost two rounds of "the frontend is broken" during this migration, and both times the
    # frontend was fine.
    if reload:
        uvicorn.run("c4x.api.main:api", host="127.0.0.1", port=port, log_level="warning",
                    reload=True, reload_dirs=[str(install_root() / "c4x")])
    else:
        from c4x.api.main import api
        uvicorn.run(api, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    # FIRST, before anything else: a frozen build that spawns a child re-executes this exe, and
    # without this the child runs main() again instead of the child's own work.
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())

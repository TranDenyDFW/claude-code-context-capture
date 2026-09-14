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
        from c4x.server import hardened_shutdown
        from c4x.watchdog import GRACE, Watchdog
        Watchdog(stop=hardened_shutdown).start()
        print(f"  watchdog: stops once no Claude process has been seen for {int(GRACE)} s")
    if reload:
        print("  reloading on source changes")

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

"""Serving concerns: which port, how to stop, and how to say the app is alive.

Separated from the page because none of it is about the dashboard. A reader tracing why a tab shows
a wrong number never needs to read a signal handler, and a reader auditing the shutdown route
should not have to scroll past nine hundred lines of layout to find it.
"""
import html as _html
import os
import threading as _threading
import time as _time

from flask import request as _flask_request

# `{reason}` is substituted with str.replace, NOT str.format.
#
# This template carries literal CSS, `body{background:#0d1117;...}`, and str.format reads that as a
# replacement field: the call raised KeyError('background') and the route returned HTTP 500. The
# process still exited, because hardened_shutdown() had already started its thread, so every caller
# saw the server stop and read the 500 as success. It went unnoticed for several runs.
STOPPED_PAGE = (
    "<!doctype html><meta charset=utf-8>"
    "<title>Context capture - stopped</title>"
    "<style>body{background:#0d1117;color:#e6edf3;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "padding:48px;text-align:center}"
    "h1{color:#f85149;margin:0 0 12px 0;font-size:22px}</style>"
    "<h1>&#9211; Stopped</h1><p>Server is shutting down: <code>{reason}</code>.</p>"
    "<p style='color:#8b949e;font-size:13px'>You can close this tab.</p>"
)

GET_REFUSED = (
    "Use POST. This route stops the capture dashboard, and a GET route can be triggered by "
    "any page your browser visits."
)


def stopped_page(reason: str) -> str:
    """The shutdown confirmation page, with the reason escaped.

    A function rather than an inline expression so it can be tested without stopping the process
    running the test.
    """
    return STOPPED_PAGE.replace("{reason}", _html.escape(reason))


def port_from_argv(argv, fallback):
    """--port beats C4X_PORT beats the default.

    Overridable because a fixed port is not a fixed port: the sibling repo runs the same app,
    Windows permits a second bind on an address already in use rather than refusing it, and two
    servers then answer on one port with no error anywhere.
    """
    if "--port" in argv:
        i = argv.index("--port")
        if i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1])
    return fallback


def hardened_shutdown(reason: str) -> None:
    """Kill children then self. Runs in a background thread so the response flushes first."""
    def _do_kill():
        _time.sleep(0.25)
        try:
            import psutil
            me = psutil.Process(os.getpid())
            children = me.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except Exception:
                    pass
            psutil.wait_procs(children, timeout=2.0)
        except Exception:
            pass
        os._exit(0)  # bypasses atexit and framework graceful-shutdown paths

    print(f"[shutdown] {reason}", flush=True)
    _threading.Thread(target=_do_kill, daemon=True).start()


def register_routes(server, db_path, port):
    """Attach the shutdown and health routes to a Flask server.

    POST only on the shutdown route. It used to accept GET, and nothing in the UI calls it at all,
    since the Quit button was removed on purpose. Binding to 127.0.0.1 is no defence against a GET
    route, because the browser is on loopback too: any page the user visited could have stopped the
    capture dashboard with <img src="http://127.0.0.1:8056/__shutdown__">. A form post cannot be
    made cross-origin without the user's involvement, and a script can still call it.
    """
    @server.route("/__shutdown__", methods=["GET"])
    def _shutdown_get():
        # Without this a GET falls through to Dash's catch-all route and returns 200 with the app's
        # own HTML, which reads as though the request was accepted. The process was never going to
        # stop, and a status code saying the opposite of what happened is its own defect.
        return (GET_REFUSED, 405)

    @server.route("/__shutdown__", methods=["POST"])
    def _shutdown_route():
        reason = (_flask_request.form.get("reason")
                  or _flask_request.args.get("reason")
                  or "user hit /__shutdown__")
        # Rendered BEFORE the kill, so a template fault surfaces as a failed response rather than
        # as a 500 from a process that is already on its way out.
        page = stopped_page(reason)
        hardened_shutdown(reason)
        return (page, 200)

    @server.route("/__health__")
    def _health():
        return {"ok": True, "db": str(db_path), "port": port}, 200

    return server


def run(app, port, debug=False):
    """Loopback only, and never the reloader.

    The reloader would run every module twice, which for this app means opening the store twice and
    registering every callback twice.
    """
    print(f"context capture explorer -> http://127.0.0.1:{port}  (debug={debug})", flush=True)
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=False)

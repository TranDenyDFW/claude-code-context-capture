"""Serving concerns: which port, how to stop, and how to say the app is alive.

Separated from the page because none of it is about the dashboard. A reader tracing why a tab shows
a wrong number never needs to read a signal handler, and a reader auditing the shutdown route
should not have to scroll past nine hundred lines of layout to find it.
"""
import html as _html
import os
import secrets as _secrets
import threading as _threading
import time as _time
from urllib.parse import urlsplit as _urlsplit

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

# One sentence for every refusal, whatever the reason.
#
# It names neither which half failed nor the token. A message that distinguished "wrong token" from
# "wrong origin" would tell an attacker which half to work on, and one that echoed the expected
# value would hand it over.
REFUSED = (
    "Refused. This route stops the server and requires the shutdown token this process printed "
    "when it started, from a request that did not come from another site."
)

# The token, generated once per process.
#
# POST-ONLY WAS NEVER A DEFENCE, and the docstring below used to say so while relying on it: a
# cross-origin form POST is a SIMPLE request, so CORS does not stop it being sent, it only stops the
# response being read. Any page the browser visited could stop this server with a form and a script.
#
# `token_urlsafe(32)` is 256 bits. It is printed to the console at startup and nowhere else: no
# route reports it, and nothing in this repository calls the shutdown route, so nothing needed
# rewiring to carry it.
SHUTDOWN_TOKEN = _secrets.token_urlsafe(32)

# Where a request is allowed to come FROM. A browser always sends `Origin` on a cross-origin POST,
# so a foreign one is the attack; curl and scripts send none at all and are unaffected.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def origin_is_local(origin) -> bool:
    """True when an Origin header may stop this server.

    Absent or empty passes: a command line client sends no Origin, and refusing those would break
    the only way anyone actually uses this route while stopping none of the attack.

    "null" is REFUSED. A sandboxed iframe and a file:// page both send it, and neither is something
    that should be able to end the process.
    """
    if origin is None or origin == "":
        return True
    if origin == "null":
        return False
    parts = _urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    return (parts.hostname or "").lower() in _LOOPBACK_HOSTS


def shutdown_allowed(origin, token) -> bool:
    """Whether a shutdown request may proceed. Both halves must hold.

    `compare_digest` rather than `==`, so the comparison does not leak the token's length or its
    matching prefix through timing. It needs str or bytes, and a missing header arrives as None.
    """
    if not origin_is_local(origin):
        return False
    return _secrets.compare_digest(str(token or ""), SHUTDOWN_TOKEN)


def announce_shutdown_token(port) -> None:
    """Print the token beside the URL at startup.

    The console is the only place it appears. Printed rather than written to a file so it dies with
    the process, and so a reader who did not start the server cannot pick it up later.
    """
    print(f"  stop it with: curl -X POST http://127.0.0.1:{port}/__shutdown__ "
          f"-H \"X-C4X-Shutdown: {SHUTDOWN_TOKEN}\"", flush=True)


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

    POST only on the shutdown route, AND an origin check AND a token, because POST alone is not a
    defence. Binding to 127.0.0.1 does not help either: the browser is on loopback too, so any page
    the user visited could have stopped the dashboard with
    <img src="http://127.0.0.1:8056/__shutdown__">, which is why GET is refused.

    This used to end "a form post cannot be made cross-origin without the user's involvement", and
    that is false. A cross-origin form POST is a SIMPLE request: CORS does not stop it being sent,
    only the response being read, and a script can submit one without the user doing anything. See
    `shutdown_allowed` for what actually closes it.
    """
    @server.route("/__shutdown__", methods=["GET"])
    def _shutdown_get():
        # Without this a GET falls through to Dash's catch-all route and returns 200 with the app's
        # own HTML, which reads as though the request was accepted. The process was never going to
        # stop, and a status code saying the opposite of what happened is its own defect.
        return (GET_REFUSED, 405)

    @server.route("/__shutdown__", methods=["POST"])
    def _shutdown_route():
        # BOTH HALVES, before anything else happens. The origin check is what stops a page the
        # browser visited; the token is what stops everything else that guessed the path.
        if not shutdown_allowed(_flask_request.headers.get("Origin"),
                                _flask_request.headers.get("X-C4X-Shutdown")
                                or _flask_request.args.get("token")):
            return (REFUSED, 403)
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
    announce_shutdown_token(port)
    app.run(host="127.0.0.1", port=port, debug=debug, use_reloader=False)

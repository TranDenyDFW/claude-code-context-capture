"""The serving layer: the shutdown page and the route that refuses a GET.

The POST route itself is deliberately NOT exercised here. It kills the process, which would take
the test runner with it, so the page is a function and the function is what gets tested. That
separation is the reason this file can exist at all.
"""
import pytest

from c4x import server


def test_the_shutdown_page_renders():
    """It did not, for several commits, and the failure was invisible from the caller's side.

    STOPPED_PAGE carries literal CSS, `body{background:#0d1117;...}`. Built with str.format, that
    is read as a replacement field and raises KeyError('background'), so the route returned HTTP
    500. The process still exited, because hardened_shutdown() had already started its thread, so
    every caller saw the server stop and took the 500 for success.
    """
    page = server.stopped_page("user hit /__shutdown__")
    assert "user hit /__shutdown__" in page
    assert "Stopped" in page


def test_the_page_keeps_its_stylesheet():
    """The CSS is what broke it. A page that renders without its braces is not the fix."""
    page = server.stopped_page("any reason")
    assert "background:#0d1117" in page
    assert "{" in page and "}" in page


def test_the_reason_is_escaped():
    """The reason arrives from a query string or a form field."""
    page = server.stopped_page("<script>alert(1)</script>")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize("reason", ["", "{}", "{reason}", "{background}", "100% {done}"])
def test_a_reason_containing_braces_does_not_break_the_page(reason):
    """Substitution is str.replace, not str.format, so a brace in the REASON is data too.

    `{reason}` is the interesting case: with format-style substitution a reason containing the
    placeholder could recurse or throw. With replace it is inserted once and left alone.
    """
    page = server.stopped_page(reason)
    assert "Stopped" in page
    assert "background:#0d1117" in page


def test_a_get_on_the_shutdown_route_is_refused(app):
    """Loopback binding is no defence: the browser is on loopback too, so a GET route could be
    triggered by any page the user visited with an img tag.

    This used to add "a form post cannot be made cross-origin without the user's involvement",
    which is wrong and was the belief the POST-only defence rested on. A cross-origin form POST is
    a simple request and a script can submit one; the token and origin checks below are what
    actually close it."""
    client = app.server.test_client()
    response = client.get("/__shutdown__")
    assert response.status_code == 405
    assert b"POST" in response.data


def test_health_answers(app):
    client = app.server.test_client()
    response = client.get("/__health__")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_the_port_flag_beats_the_environment():
    """--port beats C4X_PORT beats the default, because a fixed port is not a fixed port: Windows
    permits a second bind on an address already in use rather than refusing it."""
    assert server.port_from_argv(["app.py", "--port", "8099"], 8056) == 8099
    assert server.port_from_argv(["app.py"], 8056) == 8056
    assert server.port_from_argv(["app.py", "--port"], 8056) == 8056
    assert server.port_from_argv(["app.py", "--port", "notanumber"], 8056) == 8056


# ---------------------------------------------------------------------------
# The guard on the route that ends the process
# ---------------------------------------------------------------------------
@pytest.fixture
def no_kill(monkeypatch):
    """Record the shutdown instead of performing it.

    The accepted case genuinely calls os._exit, which would take the test runner with it. This is
    why the POST route went untested for as long as it did, and why the guard has to be tested
    through the route rather than only through `shutdown_allowed`.
    """
    calls = []
    monkeypatch.setattr(server, "hardened_shutdown", lambda reason: calls.append(reason))
    return calls


def post(app, *, origin=None, token=None, query_token=None):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["X-C4X-Shutdown"] = token
    path = "/__shutdown__" + (f"?token={query_token}" if query_token else "")
    return app.server.test_client().post(path, headers=headers)


def test_a_page_on_another_site_cannot_stop_the_server(app, no_kill):
    """The attack POST-only never stopped.

    A cross-origin form POST is a SIMPLE request: CORS does not stop it being sent, it only stops
    the response being read. So any page the browser visited could end this process with a form and
    a script, and the route's own docstring said as much while relying on POST-only anyway.
    """
    response = post(app, origin="https://evil.example", token=server.SHUTDOWN_TOKEN)
    assert response.status_code == 403
    assert no_kill == [], "a foreign origin stopped the server"


def test_a_sandboxed_or_file_page_cannot_stop_it_either(app, no_kill):
    """`null` is what a sandboxed iframe and a file:// page send, and neither should qualify."""
    assert post(app, origin="null", token=server.SHUTDOWN_TOKEN).status_code == 403
    assert no_kill == []


def test_a_hostname_that_merely_starts_with_a_loopback_address_is_refused(app, no_kill):
    """`127.0.0.1.evil.com` is a domain somebody else controls. A prefix test would pass it."""
    assert post(app, origin="http://127.0.0.1.evil.com",
                token=server.SHUTDOWN_TOKEN).status_code == 403
    assert no_kill == []


def test_the_right_origin_without_the_token_is_refused(app, no_kill):
    assert post(app, token="not-the-token").status_code == 403
    assert post(app).status_code == 403
    assert no_kill == []


def test_the_token_from_this_process_is_accepted(app, no_kill):
    """The positive control. Without it the checks above would pass on a route that refuses
    everything, which is not a working shutdown."""
    response = post(app, token=server.SHUTDOWN_TOKEN)
    assert response.status_code == 200
    assert b"Stopped" in response.data
    assert len(no_kill) == 1


def test_the_token_may_also_arrive_as_a_query_parameter(app, no_kill):
    assert post(app, query_token=server.SHUTDOWN_TOKEN).status_code == 200
    assert len(no_kill) == 1


def test_a_refusal_gives_nothing_away(app, no_kill):
    """It must not say WHICH half failed, or an attacker learns which one to work on, and it must
    not echo the token, or it hands it over."""
    body = post(app, origin="https://evil.example").data.decode()
    assert server.SHUTDOWN_TOKEN not in body
    lowered = body.lower()
    assert "origin" not in lowered, f"the refusal names the failing half: {body!r}"
    # Same sentence whichever half failed.
    assert body == post(app, token="wrong").data.decode()


def test_the_token_is_not_served_by_any_route(app):
    """It is printed to the console and nowhere else. A route that reported it would undo the
    whole mechanism, because a page the browser visits could then read it."""
    client = app.server.test_client()
    for path in ("/__health__", "/"):
        body = client.get(path).data.decode(errors="replace")
        assert server.SHUTDOWN_TOKEN not in body, f"{path} discloses the shutdown token"

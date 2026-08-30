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
    triggered by any page the user visited with an img tag. A form post cannot be made
    cross-origin without the user's involvement."""
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

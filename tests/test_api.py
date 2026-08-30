"""The HTTP surface, and the one property that makes the migration checkable: it agrees with Dash.

The API exists so a frontend that is not Dash can read the store. While both exist they must not be
able to disagree, so every tab payload here is produced by the same callback the browser dispatches.
That makes agreement true by construction, and these tests are what stop it quietly becoming false:
the moment someone reimplements a query in the API "for speed", the parity check below fails.

`extract.describe` is used to read BOTH sides, so a difference reported here is a difference in the
data, not in how two test helpers happened to walk it.
"""
import pytest

from c4x.cli import extract

fastapi = pytest.importorskip(
    "fastapi", reason="the API is optional; the dashboard alone does not need it")


@pytest.fixture(scope="module")
def client(has_store):
    from fastapi.testclient import TestClient

    from c4x.api.main import api
    return TestClient(api)


@pytest.fixture(scope="module")
def tab_ids(client):
    return [t["id"] for t in client.get("/api/tabs").json()]


def test_health_names_the_store_it_is_serving(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["db"].endswith(".db")


def test_the_api_never_harvests(client):
    """The dashboard is a writer through its refresh tick. Two writers into one store is how a
    redacted copy got un-redacted, and an API server is exactly the second one nobody expects."""
    assert client.get("/api/health").json()["read_only"] is True


def test_the_tab_list_comes_from_the_app(client, app):
    """Not from a list written in the API module, which would be a second source of truth for
    something that already has one, and would go stale the way the migration proposal did."""
    served = [(t["id"], t["label"]) for t in client.get("/api/tabs").json()]
    assert served == [(t[0], t[1]) for t in app.TABS]


def test_an_unknown_tab_says_what_it_does_know(client):
    response = client.get("/api/tab/tab-does-not-exist")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "tab-summary" in detail["known"], "the 404 does not name the real tabs"


def test_the_scope_parameter_refuses_a_value_the_app_has_no_meaning_for(client):
    """`scope` is main or all. Anything else would silently fall through to a default and the
    caller would get a population they did not ask for."""
    assert client.get("/api/tab/tab-sessions?scope=sideways").status_code == 422


def test_every_tab_answers_on_both_endpoints(client, tab_ids, session_id):
    for tab in tab_ids:
        verify = client.get(f"/api/tab/{tab}", params={"session": session_id})
        render = client.get(f"/api/tab/{tab}/render", params={"session": session_id})
        assert verify.status_code == 200, f"{tab} verify: {verify.text[:200]}"
        assert render.status_code == 200, f"{tab} render: {render.text[:200]}"


def test_the_payload_survives_json_at_all(client, tab_ids, session_id):
    """This is not a formality. `extract.describe` reports chart extents as numpy scalars, which
    FastAPI's encoder refuses with a 500 naming nothing useful, and it does NOT happen on every
    tab: the Cost tab returned 200 while the Session tab did not. A test that checked one tab
    would have shipped it."""
    for tab in tab_ids:
        for suffix in ("", "/render"):
            response = client.get(f"/api/tab/{tab}{suffix}", params={"session": session_id})
            assert response.status_code == 200, f"{tab}{suffix} did not serialise"
            response.json()


@pytest.mark.parametrize("scope", ["main", "all"])
def test_the_api_and_dash_report_the_same_tables(client, tab_ids, app, session_id, scope):
    """The property the whole migration rests on.

    Both sides are read with the same extractor, so a difference here is a difference in the data.
    """
    for tab in tab_ids:
        served = client.get(f"/api/tab/{tab}",
                            params={"session": session_id, "scope": scope}).json()
        if tab == "tab-compare":
            from c4x.tabs.compare import default_arm_b
            target = default_arm_b(session_id, None)
            pane = app._cmp_render("session", target, session_id, None, scope)
        else:
            pane = app._render_tab([t[0] for t in app.TABS].index(tab), session_id, scope, None)
        direct = extract.describe(pane)
        assert len(served["tables"]) == len(direct["tables"]), f"{tab}/{scope}: table count differs"
        for a, b in zip(served["tables"], direct["tables"], strict=True):
            assert a["id"] == b["id"], f"{tab}/{scope}: table ids differ"
            assert a["columns"] == b["columns"], f"{tab}/{scope}: {a['id']} columns differ"
            assert len(a["rows"]) == len(b["rows"]), f"{tab}/{scope}: {a['id']} row count differs"


def test_compare_returns_its_body_not_its_shell(client):
    """Compare's pane is two dropdowns; its content arrives from a separate callback. Serving the
    pane alone returns zero tables, which reads as a broken tab. The CLI shipped exactly that bug
    for as long as it existed."""
    body = client.get("/api/tab/tab-compare").json()
    assert body["tables"], "Compare served its shell instead of its body"
    assert sum(len(t["rows"]) for t in body["tables"]) > 0


def test_the_render_endpoint_carries_a_drawable_figure(client, tab_ids, session_id):
    """The verify shape summarises a chart to its extents, which is unusable for drawing. If
    `/render` carried the same thing, a frontend would have nothing to plot."""
    drawn = 0
    for tab in tab_ids:
        body = client.get(f"/api/tab/{tab}/render", params={"session": session_id}).json()
        assert len(body["plotly"]) == len(body["figures"]), f"{tab}: plotly and figures disagree"
        for figure in body["plotly"]:
            assert "data" in figure, f"{tab}: a figure carries no traces"
            for trace in figure["data"]:
                if trace.get("x") is not None or trace.get("y") is not None:
                    drawn += 1
    assert drawn, "no figure anywhere carries a series, so nothing could be drawn"


def test_sessions_lists_the_store_and_says_how_many_there_are(client):
    body = client.get("/api/sessions", params={"limit": 5}).json()
    assert len(body["rows"]) <= 5
    assert body["total"] >= len(body["rows"]), "the page is larger than the population"
    if body["rows"]:
        assert "session_id" in body["rows"][0]


def test_the_session_limit_is_bounded(client):
    """An unbounded limit over a 1,300-session store is a way to ask this process to build a very
    large response by accident."""
    assert client.get("/api/sessions", params={"limit": 99999}).status_code == 422
    assert client.get("/api/sessions", params={"limit": 0}).status_code == 422


def test_the_entry_point_checks_its_own_argument_handling():
    from c4x.api.__main__ import db_from_argv, port_from_argv
    assert port_from_argv(["--port", "9999"]) == 9999
    assert port_from_argv([]) != 8056, "the API must not default to the dashboard's port"
    assert db_from_argv([]) is None

"""Every table is named and explained the same way, every chart answers a hover anywhere, and an
export is never the preview. All three are contracts on the /render surface, checked on every tab.

The defect these pin: twelve of eighteen tables reached the page with no title, and every heading
and note the server had written for them arrived as loose prose far above the rows, because
`describe()` flattens a pane and nothing paired the text back to its table.
"""
import pytest

fastapi = pytest.importorskip(
    "fastapi", reason="the API is optional; the dashboard alone does not need it")


@pytest.fixture(scope="module")
def client(has_store):
    from fastapi.testclient import TestClient

    from c4x.api.main import api
    return TestClient(api)


@pytest.fixture(scope="module")
def rendered(client):
    """Every tab's /render payload, once."""
    ids = [t["id"] for t in client.get("/api/tabs").json()]
    return {tid: client.get(f"/api/tab/{tid}/render").json() for tid in ids}


def test_every_table_on_every_tab_has_a_title(rendered):
    untitled = [(tid, i) for tid, p in rendered.items()
                for i, m in enumerate(p["meta"]) if not m.get("title")]
    assert not untitled, f"tables the page would have to invent a name for: {untitled}"


def test_the_meta_is_paired_to_every_table(rendered):
    """A payload whose meta count differs from its table count is served with meta EMPTY, by
    design; this makes sure that never quietly becomes the state of a tab."""
    for tid, p in rendered.items():
        assert len(p["meta"]) == len(p["tables"]), f"{tid}: meta not paired"


def test_a_note_never_repeats_the_row_count_or_the_paging_clause(rendered):
    """The page states the count from the rows it holds and pages the table itself."""
    for tid, p in rendered.items():
        for m in p["meta"]:
            note = m.get("note") or ""
            assert " rows." not in note[:16], f"{tid}: {note[:60]!r}"
            assert "table shows the first page" not in note, f"{tid}: {note[:60]!r}"


def test_every_absorbed_line_is_a_real_text_line(rendered):
    """`absorbed` is the page's licence to drop a line from the prose block. A line that is not in
    `text` would be a licence for nothing; a heading that IS in text and is not absorbed is the
    defect this file exists for, printed twice."""
    for tid, p in rendered.items():
        for m in p["meta"]:
            for line in m.get("absorbed", []):
                assert line in p["text"], f"{tid}: absorbed {line[:60]!r} is not a text line"
            if m.get("title") and m["title"] in p["text"]:
                assert m["title"] in m.get("absorbed", []), (
                    f"{tid}: heading {m['title']!r} is in text and would print twice")


def test_the_cost_tab_notes_are_on_their_tables(rendered):
    """The six evidence blocks each wrote a note; the page used to print all six as one paragraph
    block above the first table."""
    notes = [m.get("note") for m in rendered["tab-cost"]["meta"]]
    assert sum(1 for n in notes if n) >= 4, notes


def test_a_chart_with_lines_hovers_unified_and_a_scatter_reaches_further(rendered):
    """Plotly's default is `closest` within 20 px, which on a turn chart answers only on the line
    and one series at a time; measured on the Session chart, one pointer position in three showed
    nothing. Lines and areas hover `x unified`; markers-only charts keep `closest` with 40 px."""
    seen = 0
    for tid, p in rendered.items():
        for fig in p.get("plotly") or []:
            traces = fig.get("data", [])
            scatter = [t for t in traces if t.get("type") == "scatter"]
            if not scatter:
                continue
            seen += 1
            lined = any("lines" in (t.get("mode") or "") or t.get("fill") for t in scatter)
            layout = fig.get("layout", {})
            if lined:
                assert layout.get("hovermode") == "x unified", (tid, layout.get("hovermode"))
                assert layout.get("hoverdistance") == 40, (tid, "an unlimited reach lets a sparse "
                                                                "marker trace answer from far away")
                for t in scatter:
                    assert t.get("hovertemplate") or t.get("hoverinfo") == "skip", (
                        tid, t.get("name"), "a series with no template answers a hover with x, y")
            else:
                assert layout.get("hovermode") == "closest", (tid, layout.get("hovermode"))
                assert layout.get("hoverdistance") == 40, (tid, layout.get("hoverdistance"))
    assert seen >= 4, "the charts this checks were not rendered"


def test_the_messages_table_says_where_its_full_text_is(rendered):
    p = rendered["tab-session"]
    # Not strict: on the documented fallback (meta empty) the paired-meta test above is the one
    # that should fail readably, not this one with a ValueError.
    previews = [(t, m) for t, m in zip(p["tables"], p["meta"], strict=False)
                if "preview" in t["columns"]]
    assert previews, "the session tab has no messages table"
    for _, m in previews:
        assert m.get("full_text") == {"url": "/api/messages/text", "key": "uuid",
                                      "column": "preview", "as": "text"}


def test_the_full_text_route_returns_more_than_the_preview(client, q):
    """On the session that HOLDS a long message, found in the store rather than assumed of the
    default session: on the synthetic fixture the default session's messages were all short and
    this had nothing to exercise, which a strict run correctly refused to call a pass."""
    hit = q("SELECT session_id FROM messages WHERE chars > 220 ORDER BY chars LIMIT 1")
    if hit.empty:
        pytest.fail("no message in this store is longer than the preview; the fixture must "
                    "carry one, or the route is untested everywhere")
    p = client.get(f"/api/tab/tab-session/render?session={hit.iloc[0]['session_id']}").json()
    table = next(t for t in p["tables"] if "preview" in t["columns"])
    cut = [r for r in table["rows"] if (r.get("chars") or 0) > 220][:5]
    assert cut, "the session that holds a long message did not render it in its first 400"
    got = client.post("/api/messages/text", json={"uuids": [r["uuid"] for r in cut]}).json()
    for r in cut:
        assert len(got[r["uuid"]]) > len(r["preview"]), r["uuid"]
        assert got[r["uuid"]].replace("\n", " ").replace("\r", " ").startswith(
            r["preview"][:100]), "the full text is not the text the preview was cut from"


def test_the_full_text_route_refuses_an_unbounded_request(client):
    response = client.post("/api/messages/text", json={"uuids": ["x"] * 1001})
    assert response.status_code == 422
    assert client.post("/api/messages/text", json={"uuids": []}).json() == {}


def test_the_page_shell_is_never_cached_and_the_hashed_assets_may_be(client):
    """A browser holding a stale index.html runs last week's page against this week's API. The
    shell names the bundle by hash, so it is the one file that must be fetched every time."""
    shell = client.get("/")
    if shell.status_code == 404:
        pytest.skip("frontend/dist is not built here; the shell is served only when it exists")
    assert shell.headers.get("cache-control") == "no-cache"
    assert client.get("/api/health").headers.get("cache-control") != "no-cache"


def test_a_meta_mismatch_serves_empty_meta_and_not_a_500(client, monkeypatch):
    """The fallback the docstring above promises. A strict zip over the empty fallback turned it
    into a 500 for the whole tab; the review forced the mismatch and watched all eight tabs fail."""
    import c4x.api.main as main
    real = main._table_meta
    monkeypatch.setattr(main, "_table_meta", lambda pane, found=None: real(pane, found)[:-1])
    response = client.get("/api/tab/tab-cost/render?no_cache=true")
    assert response.status_code == 200, response.text[:200]
    body = response.json()
    assert body["tables"] and body["meta"] == []


def _tree_meta(children):
    from dash import html

    from c4x.api.main import _table_meta
    return _table_meta(html.Div(children))


def _table(table_id=None):
    from c4x.dash_compat import DataTable
    return DataTable(**({"id": table_id} if table_id else {}),
                     columns=[{"name": "a", "id": "a"}], data=[])


def test_an_unstyled_caption_directly_above_a_table_is_its_note_whatever_its_length():
    """A 73-character caption ending in a period was neither heading nor note under a length
    rule, and printed as loose prose above the Messages table on every session."""
    from dash import html
    caption = "6 messages in this session, oldest first. Click a row to read it in full."
    meta = _tree_meta([html.Div(caption, style={"color": "#888"}), _table("tbl-messages")])
    assert meta[0]["note"] == caption
    assert meta[0]["absorbed"] == [caption]
    short = "Short line"
    meta = _tree_meta([html.Div(short, style={"color": "#888"}), _table()])
    assert meta[0]["note"] == short and meta[0]["title"] is None


def test_a_heading_before_a_chart_names_the_chart_not_the_table_after_it():
    from dash import dcc, html

    from c4x.theme import SECTION_HEAD
    meta = _tree_meta([html.Div("Chart title", style=SECTION_HEAD), dcc.Graph(figure={}),
                       _table()])
    assert meta[0]["title"] is None and meta[0]["note"] is None and meta[0]["absorbed"] == []


def test_the_heading_written_above_a_table_beats_the_label_table():
    from dash import html

    from c4x.theme import SECTION_HEAD, table_label
    assert table_label("tbl-reread") not in (None, "Written heading")
    meta = _tree_meta([html.Div("Written heading", style=SECTION_HEAD), _table("tbl-reread")])
    assert meta[0]["title"] == "Written heading"
    assert _tree_meta([_table("tbl-reread")])[0]["title"] == table_label("tbl-reread")


def test_every_spelling_of_the_shell_is_uncached_and_an_asset_is_not(client):
    from pathlib import Path
    if client.get("/").status_code == 404:
        pytest.skip("frontend/dist is not built here; the shell is served only when it exists")
    for alias in ("/INDEX.HTML", "/Index.Html", "/index.html/", "/index.html?v=1"):
        response = client.get(alias, follow_redirects=True)
        if response.status_code == 200:
            assert response.headers.get("cache-control") == "no-cache", alias
    assets = sorted((Path(__file__).resolve().parents[1] / "frontend" / "dist" / "assets")
                    .glob("*.js"))
    assert assets, "no built asset to check"
    asset = client.get(f"/assets/{assets[0].name}")
    assert asset.status_code == 200
    assert asset.headers.get("cache-control") != "no-cache"


def test_a_revalidation_of_the_shell_carries_the_header_too(client):
    """A 304 has no content type. Without the header on it, a browser that cached the shell
    before the rule kept that copy until the next rebuild changed the ETag."""
    first = client.get("/")
    if first.status_code == 404:
        pytest.skip("frontend/dist is not built here; the shell is served only when it exists")
    etag = first.headers.get("etag")
    assert etag, "the shell is served without an ETag, so revalidation cannot be exercised"
    again = client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers.get("cache-control") == "no-cache"


def test_a_one_row_evidence_block_says_row_not_rows():
    import pandas as pd

    from c4x.cli import extract
    from c4x.panels import evidence_block
    one = extract.texts(evidence_block("T", pd.DataFrame({"a": [1]}), "SELECT 1"))
    two = extract.texts(evidence_block("T", pd.DataFrame({"a": [1, 2]}), "SELECT 1"))
    assert any(line.startswith("1 row.") for line in one), one
    assert not any("1 rows" in line for line in one), one
    assert any(line.startswith("2 rows.") for line in two), two


def test_dash_only_lines_are_named_and_are_real_text_lines(rendered):
    """The window calculator lives only in the Dash tree. Its labels reached the React page as
    loose prose; now /render names them and the page leaves them out."""
    diagnostics = rendered["tab-diagnostics"]
    lines = diagnostics.get("dash_only") or []
    assert any("Resident tokens" in line for line in lines), lines
    for line in lines:
        assert line in diagnostics["text"], f"dash_only line is not a text line: {line[:60]!r}"
    # CONTENT IS NOT A CONTROL LABEL. The constants sentence is the only place either page states
    # the four window-math numbers, and marking it dash-only removed them from the React page
    # entirely; a review caught it. It stays in the text the page prints.
    constants = [line for line in diagnostics["text"] if "Constants read from" in line]
    assert constants, "the window-math constants are not stated at all"
    for line in constants:
        assert line not in lines, "the constants sentence is hidden from the page"
    for tid, p in rendered.items():
        for line in p.get("dash_only") or []:
            assert line in p["text"], (tid, line[:60])

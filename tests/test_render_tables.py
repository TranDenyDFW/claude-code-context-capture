"""Every table is named and explained the same way, every chart answers a hover anywhere, and an
export is never the preview. All three are contracts on the /render surface, checked on every tab.

The defect these pin: twelve of eighteen tables reached the page with no title, and every heading
and note the server had written for them arrived as loose prose far above the rows, because
`describe()` flattens a pane and nothing paired the text back to its table.
"""
import re

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
            # BOTH SPELLINGS. This asserted the plural only, so when evidence_block learned to
            # write "1 row." the count came back through: the heading said "1 row" and the note it
            # carries opened "1 row. Invocation count alone is...", the same number twice, on every
            # one-row table. A guard that names one half of a pair is how the other half returns.
            assert not re.match(r"\s*[\d,]+ rows?\.", note), f"{tid}: {note[:60]!r}"
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
        pytest.fail("frontend/dist is TRACKED in this repository, so a 404 here is a broken "
                    "checkout or a mount that stopped mounting, not a missing build")
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
        pytest.fail("frontend/dist is TRACKED in this repository, so a 404 here is a broken "
                    "checkout or a mount that stopped mounting, not a missing build")
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
        pytest.fail("frontend/dist is TRACKED in this repository, so a 404 here is a broken "
                    "checkout or a mount that stopped mounting, not a missing build")
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


def test_the_shell_rule_is_installed_and_holds_without_any_bundle(client):
    """The three tests above need `frontend/dist`. This one does not, and it also asserts the
    middleware is REGISTERED rather than merely defined: a dispatch nobody installed is a comment.

    The 304 leg is checked directly, because a conditional request is the one case no content type
    can reach and no unbuilt checkout can produce.
    """
    import asyncio

    from starlette.requests import Request
    from starlette.responses import Response

    import c4x.api.main as main

    assert any(getattr(m, "kwargs", {}).get("dispatch") is main._no_cache_shell
               for m in main.api.user_middleware), "the no-cache middleware is not installed"

    # HTML this process serves with no bundle at all: its own schema page.
    docs = client.get("/api/docs")
    assert docs.status_code == 200
    assert docs.headers["content-type"].startswith("text/html")
    assert docs.headers.get("cache-control") == "no-cache"
    assert client.get("/api/health").headers.get("cache-control") != "no-cache"

    def through(path, response):
        scope = {"type": "http", "method": "GET", "path": path, "headers": [],
                 "query_string": b"", "scheme": "http", "server": ("test", 80)}

        async def call_next(_request):
            return response

        return asyncio.run(main._no_cache_shell(Request(scope), call_next))

    def cached(path):
        return through(path, Response(status_code=304)).headers.get("cache-control")

    assert cached("/index.html") == "no-cache"
    assert cached("/") == "no-cache"
    assert cached("/assets/index-abc.js") is None

def test_no_two_tables_of_one_surface_share_a_title(app):
    """A title is how a reader tells one table from another, so two tables must not answer to the
    same one.

    The collision this pins was mine: the composition table gained the heading "What is in the
    window, item by item" in the same pass that made every table carry one, and a sub-panel had
    written that sentence for its own block since before. Only one of the two is served today, so
    nothing showed it; a reader who switched panels would have seen one heading over two different
    tables. Checked over every tab AND every registered sub-panel, because the sub-panels are
    exactly where nothing else looks.
    """
    from c4x.api.main import _pane, _table_meta
    from c4x.ui.subpanels import PANELLED

    surfaces = {}
    for tab, *_ in app.TABS:
        surfaces[f"tab {tab}"] = _table_meta(_pane(tab, None, "main", None, None, "session"))
    for prefix, spec in PANELLED.items():
        for index in range(len(spec["panels"])):
            surfaces[f"panel {prefix}[{index}]"] = _table_meta(
                spec["body"](index, None, "main", None))

    titles = {name: [m["title"] for m in meta if m.get("title")] for name, meta in surfaces.items()}

    for name, names in titles.items():
        assert len(names) == len(set(names)), f"{name} ships one title twice: {names}"

    # Across the panels of one registry: switching panels must not leave the same heading over
    # different content. A tab that renders one of its own panels inline is the same table twice,
    # not a collision, so tabs are not compared against panels here.
    for prefix, spec in PANELLED.items():
        panels = [(i, set(titles[f"panel {prefix}[{i}]"])) for i in range(len(spec["panels"]))]
        for i, mine in panels:
            for j, theirs in panels:
                if i < j:
                    shared = mine & theirs
                    assert not shared, f"panels {prefix}[{i}] and {prefix}[{j}] share {shared}"


def _loose_prose(payload):
    """The lines this payload would print as a wall, replicating `Pane.tsx`'s own claim pass.

    Kept in step with the page by construction: every field the component consults to drop a line
    is consulted here, in the same order and with the same case-insensitive compare. A test that
    approximated the filter would go green on lines the reader still sees.
    """
    claimed = set()

    def claim(line):
        if line:
            claimed.add(str(line).strip().lower())

    for stat in payload.get("stats") or []:
        claim(stat.get("label"))
        claim(stat.get("value"))
        claim(stat.get("sub"))
    sections = payload.get("details") or []
    for section in sections:
        for line in section.get("body") or []:
            claim(line)
        claim(section.get("summary"))
        claim(section.get("summary_note"))
    for entry in payload.get("meta") or []:
        for line in entry.get("absorbed") or []:
            claim(line)
    for entry in payload.get("figure_meta") or []:
        for line in entry.get("absorbed") or []:
            claim(line)
    for line in payload.get("dash_only") or []:
        claim(line)
    for line in payload.get("about") or []:
        claim(line)
    population = payload.get("population")
    return [line for line in payload["text"]
            if line != population
            and line.strip().lower() not in claimed
            and not any(line in (s.get("summary") or "") for s in sections)]


def _surfaces(app):
    """Every tab and every registered sub-panel, as {name: render payload}.

    The sub-panels are included because they are exactly what nothing else looks at: three of them
    are unreachable from the React page today, and their button labels reached the reader as
    sentences for as long as the port has existed.
    """
    from c4x.api.main import _pane, _render_payload
    from c4x.ui.subpanels import PANELLED

    out = {}
    for tab, *_ in app.TABS:
        out[f"tab {tab}"] = _render_payload(_pane(tab, None, "main", None, None, "session"))
    for prefix, spec in PANELLED.items():
        for index in range(len(spec["panels"])):
            pane = spec["body"](index, None, "main", None)
            out[f"panel {prefix}[{index}]"] = _render_payload(pane)
    return out


def test_no_surface_prints_a_wall_of_loose_prose(app):
    """EVERY LINE HAS AN OWNER: a card, a table, a chart, a section, the population, or a control
    this page does not draw.

    This is the whole shape of the page stated once. A tab that grows a sentence with nowhere to
    put it fails here rather than shipping it as a paragraph in the body, which is how twenty-eight
    lines across seven tabs arrived: chart captions with no channel to reach a chart, table captions
    written below their table where the forward-only pairing never sees them, the labels of sliders
    and buttons the React page never draws, and five numbers that wanted to be stat cards.
    """
    homeless = {name: _loose_prose(payload) for name, payload in _surfaces(app).items()}
    homeless = {name: lines for name, lines in homeless.items() if lines}
    report = "\n".join(f"  {name}: {len(lines)} line(s)\n"
                       + "\n".join(f"      {line[:96]!r}" for line in lines)
                       for name, lines in homeless.items())
    assert not homeless, f"text with no owner, which the page prints as a wall:\n{report}"


def test_every_chart_on_every_surface_has_a_name(app):
    """A chart with no title is drawn under nothing at all: `Pane.tsx` renders no heading rather
    than inventing one, so the reader gets a plot and no statement of what it plots."""
    unnamed = [(name, i) for name, payload in _surfaces(app).items()
               for i, figure in enumerate(payload["figures"]) if not (figure.get("title") or "")]
    assert not unnamed, f"charts the page would draw with no heading: {unnamed}"

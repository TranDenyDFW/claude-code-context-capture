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


def test_render_carries_the_collapsible_sections_the_verify_shape_flattens(client):
    """"Every table carries the query that built it" is a feature, and `describe()` destroys it.

    `extract.texts()` flattens a pane to a list of strings, so over the API the SQL arrives as loose
    paragraphs with nothing saying where a query starts or which table it belongs to. A frontend
    rendering that faithfully prints six queries down the Cost tab as body text.
    """
    body = client.get("/api/tab/tab-cost/render").json()
    assert body["details"], "the Cost tab's queries did not survive the API"
    assert any("SELECT" in "\n".join(s["body"]).upper() for s in body["details"])


def test_each_section_is_attributed_to_a_real_table(client):
    """By INDEX, because five of the Cost tab's six tables have no id at all.

    A section pointing at the wrong table would show a query under a table that did not produce it,
    which is worse than showing no query: it is a wrong answer that looks checkable.
    """
    body = client.get("/api/tab/tab-cost/render").json()
    tables = body["tables"]
    indices = [s["table_index"] for s in body["details"]]
    for index in indices:
        assert index is None or -1 <= index < len(tables), f"{index} is not a table on this tab"
    real = [i for i in indices if isinstance(i, int) and i >= 0]
    # One query per table, each to a different one. If the walk ever fell out of step with the
    # extractor's ordering, this is where it would show.
    assert len(real) == len(set(real)), "two queries were attributed to the same table"


def test_the_verify_shape_does_NOT_carry_sections(client, session_id):
    """The parity surface stays frozen.

    `/api/tab/{id}` is compared field for field against Dash and is byte for byte what the CLI
    prints. Growing a field there for a browser's benefit would change both.
    """
    assert "details" not in client.get("/api/tab/tab-cost", params={"session": session_id}).json()


def test_sessions_lists_the_store_and_says_how_many_there_are(client):
    body = client.get("/api/sessions", params={"limit": 5}).json()
    assert len(body["rows"]) <= 5
    assert body["total"] >= len(body["rows"]), "the page is larger than the population"
    if body["rows"]:
        assert "session_id" in body["rows"][0]


def test_sessions_are_newest_first_and_sorted_before_they_are_sliced(client):
    """The order is the whole value of a browse index, and the bug is invisible either way.

    `session_rows()` does not come back in time order, so `head(limit)` on it returns an arbitrary
    50 of 1,325 sessions. Both that and the correct answer look like a plausible list of sessions.
    The way it would have been noticed is `c4x.cli sessions --via api` listing different sessions
    than `c4x.cli sessions`, which is a comparison nobody runs by accident.

    The WHOLE listing is checked, not the first page of it. The first draft of this test asked for
    ten rows and compared them against the first ten of a larger request. It passed with the sort
    removed, twice: the head of the unsorted frame happens to be in time order, and two slices of
    one unsorted frame always agree with each other. A gate that cannot fail is not a gate, and the
    only reason this one was caught is that it was deliberately fed the broken code.
    """
    everything = client.get("/api/sessions", params={"limit": 2000}).json()["rows"]
    stamps = [r["last_ts"] for r in everything if r.get("last_ts") is not None]
    # SKIPPED, not failed, below two. The first version demanded more than ten sessions to stop the
    # check being vacuous, and CI runs against a synthetic fixture with five: a store too small to
    # prove anything became a store that failed the suite. Two is the real threshold, because one
    # session is in order by definition and two can be out of it.
    #
    # Said plainly: on a five-session fixture this is a weak gate, and it is the run against the
    # real store that would actually catch an unsorted listing.
    if len(stamps) < 2:
        pytest.skip(f"this store has {len(stamps)} dated sessions, so order cannot be wrong")
    assert stamps == sorted(stamps, reverse=True), "the session list is not newest first"

    page = client.get("/api/sessions", params={"limit": 10}).json()["rows"]
    assert [r["session_id"] for r in page] == [r["session_id"] for r in everything[:len(page)]], \
        "a page of the list is not the head of the list"


def test_compare_accepts_a_named_arm(client, session_id, other_session_id):
    """`c4x.cli dump --compare-with` could do this in-process and could not over HTTP.

    Without the parameter the API always answers with `default_arm_b`, so the CLI's compare flag
    would have silently ignored the arm the caller asked for and returned a different comparison.
    """
    named = client.get("/api/tab/tab-compare",
                       params={"session": session_id, "compare_with": other_session_id})
    assert named.status_code == 200
    assert named.json()["tables"], "a named arm returned no body"


def test_compare_refuses_a_kind_it_has_no_meaning_for(client):
    assert client.get("/api/tab/tab-compare",
                      params={"compare_kind": "sideways"}).status_code == 422


def test_the_session_limit_is_bounded(client):
    """An unbounded limit over a 1,300-session store is a way to ask this process to build a very
    large response by accident."""
    assert client.get("/api/sessions", params={"limit": 99999}).status_code == 422
    assert client.get("/api/sessions", params={"limit": 0}).status_code == 422


def test_a_repeat_request_is_served_from_the_cache(client, session_id):
    """The whole of phase 3. Without this the migration cannot meet "don't make it slower":
    a React frontend leaves every query where it is and adds a hop."""
    params = {"session": session_id}
    client.get("/api/tab/tab-compactions", params=params)
    again = client.get("/api/tab/tab-compactions", params=params)
    assert again.headers.get("x-c4x-cache") == "hit"


def test_the_cache_does_not_change_the_answer(client, session_id):
    """A cache that returns something different from a fresh build is not a cache, it is a bug
    with good latency. Compared against a build that explicitly bypasses it."""
    params = {"session": session_id}
    client.get("/api/tab/tab-cost", params=params)
    cached = client.get("/api/tab/tab-cost", params=params)
    assert cached.headers.get("x-c4x-cache") == "hit"
    fresh = client.get("/api/tab/tab-cost", params={**params, "no_cache": "1"})
    assert [t["id"] for t in cached.json()["tables"]] == [t["id"] for t in fresh.json()["tables"]]
    assert [len(t["rows"]) for t in cached.json()["tables"]] == \
           [len(t["rows"]) for t in fresh.json()["tables"]]


def test_no_cache_actually_bypasses_it(client, session_id):
    """The escape hatch has to work, because it is what the bench uses to measure a first view
    and what a reader uses when they suspect the cache."""
    params = {"session": session_id}
    client.get("/api/tab/tab-compactions", params=params)
    forced = client.get("/api/tab/tab-compactions", params={**params, "no_cache": "1"})
    assert forced.headers.get("x-c4x-cache") is None, "no_cache was served from the cache"


def test_different_selections_do_not_share_a_cache_entry(client, session_id):
    """The key carries every parameter that changes the answer. A key that dropped one would serve
    a pane for the wrong scope, which looks entirely plausible on screen.

    The cache is emptied first, and that is not tidiness. The first version of this test asserted
    that a scope=all request was not a hit, and it failed: an earlier test in this module is
    parametrized over both scopes and had already cached them, so the hit proved nothing about the
    key and everything about test order. Starting from empty makes each assertion mean what it says.
    """
    from c4x.api import cache
    cache.clear()
    params = {"session": session_id}
    first = client.get("/api/tab/tab-session", params={**params, "scope": "main"})
    assert first.headers.get("x-c4x-cache") == "miss", "the cache was not actually empty"
    other = client.get("/api/tab/tab-session", params={**params, "scope": "all"})
    assert other.headers.get("x-c4x-cache") == "miss", "a different scope reused the entry"
    again = client.get("/api/tab/tab-session", params={**params, "scope": "main"})
    assert again.headers.get("x-c4x-cache") == "hit", "the first entry was lost, nothing is keyed"
    assert first.status_code == other.status_code == 200


def test_health_reports_the_cache_so_it_can_be_checked_in_the_wild(client):
    body = client.get("/api/health").json()
    assert "cache" in body
    assert set(body["cache"]) >= {"entries", "bytes", "hits", "misses"}


def test_the_cohort_list_comes_from_the_store(client):
    """Not from every distinct working directory, which is what the frontend built for itself.

    That version filled the picker with per-run temp directories, dropped the four SECTION cohorts,
    and sent the bare path as `cohort=`. `cohort_sessions()` recognises no kind in a bare path and
    returns an empty list, which means NO RESTRICTION, so choosing a project silently filtered
    nothing while the header said it had.
    """
    from c4x import store
    served = client.get("/api/cohorts").json()
    assert served == store.cohort_options()
    kinds = {v.split("::")[0] for v in (o["value"] for o in served) if "::" in v}
    assert "section" in kinds, "the section cohorts are missing, which is what was lost before"


def a_narrowing_cohort():
    """A cohort that is a PROPER subset of the store, or None if this store has none.

    Not "the first project cohort", which is what these tests asked for first and why both of them
    failed on the synthetic fixture: `make_fixture.mjs` gives every session the same project slug,
    so the fixture's only project cohort covers all five sessions and "strictly narrows" cannot
    hold there. That is not a flaw in the fixture. It is a store with one project, which is a
    perfectly ordinary store, and a test that demands two is asserting something about the data
    rather than about the code.

    So the strict half of each check runs against a cohort that genuinely excludes something, and
    is skipped, out loud, on a store where no such cohort exists.
    """
    from c4x import store
    total = len(store.session_rows())
    for option in store.cohort_options():
        if option["value"] == "__all__":
            continue
        held = len(store.cohort_sessions(option["value"]))
        if 0 < held < total:
            return option["value"], held
    return None, 0


def test_a_cohort_value_actually_restricts_the_population(client):
    """The bug was invisible: both the filtered and the unfiltered page look like a list."""
    from c4x import store
    rows = lambda body: sum(len(t["rows"]) for t in body["tables"])   # noqa: E731
    everything = rows(client.get("/api/tab/tab-sessions").json())

    # HOLDS ON ANY STORE: a cohort shows exactly the sessions the store says it contains, whether
    # that is a subset or all of them.
    for option in store.cohort_options():
        if option["value"] == "__all__":
            continue
        held = store.cohort_sessions(option["value"])
        shown = rows(client.get("/api/tab/tab-sessions",
                                params={"cohort": option["value"]}).json())
        assert shown == len(held), f"{option['value']} shows {shown} rows for {len(held)} sessions"

    narrowing, held = a_narrowing_cohort()
    if narrowing:
        shown = rows(client.get("/api/tab/tab-sessions", params={"cohort": narrowing}).json())
        assert shown < everything, f"{narrowing} did not narrow anything"
        # The bare value, which is what the frontend used to send, must NOT be taken for a cohort.
        bare = rows(client.get("/api/tab/tab-sessions",
                               params={"cohort": narrowing.split("::", 1)[1]}).json())
        assert bare == everything, "a bare path is not a cohort and must not filter"
    else:
        pytest.skip("every cohort in this store covers the whole store, so nothing can narrow")


def test_the_session_picker_is_narrowed_to_the_cohort(client):
    """`selector_options` promises the picker cannot offer a session the population excludes.

    The frontend built its own from a capped session list and ignored the cohort, so it offered
    sessions the chosen population does not contain and hid seventeen of 317 behind a limit.
    """
    from c4x import store
    everything = client.get("/api/selector").json()
    # The uncapped half holds on any store and is the half that catches the 300-of-317 defect.
    assert len(everything) == len(store.session_rows()), "the picker is not offering every session"

    narrowing, held = a_narrowing_cohort()
    if not narrowing:
        pytest.skip("every cohort in this store covers the whole store, so nothing can narrow")
    narrowed = client.get("/api/selector", params={"cohort": narrowing}).json()
    assert {o["value"] for o in narrowed} <= set(store.cohort_sessions(narrowing))
    assert len(narrowed) == held
    assert len(narrowed) < len(everything)


def test_render_carries_the_column_contract(client):
    """Label, type, alignment and d3 format, which `describe()` drops.

    Without it the browser guesses. A column declared `.1f` was rendered 43.30, 2.40, 1.20 and then
    a bare 1, because the guess asked `Number.isInteger` instead of asking the app.
    """
    body = client.get("/api/tab/tab-window/render").json()
    assert len(body["meta"]) == len(body["tables"])
    columns = [c for table in body["meta"] for c in table["columns"]]
    assert columns, "no column metadata at all"
    for column in columns:
        assert column["label"], f"{column['id']} has no label"
        assert column["align"] == ("right" if column["numeric"] else "left")
    percent = next((c for c in columns if c["id"] == "percent"), None)
    if percent:
        assert percent["specifier"] == ".1f", "the declared format did not survive the API"


def test_every_column_is_named_for_a_reader(client, session_id):
    """`ts` is a schema field. "Date & Time" is a column heading."""
    for tab in ("tab-compactions", "tab-cost", "tab-sessions"):
        body = client.get(f"/api/tab/{tab}/render", params={"session": session_id}).json()
        for table in body["meta"]:
            for column in table["columns"]:
                assert column["label"] != column["id"] or column["id"][:1].isupper(), \
                    f"{tab}/{column['id']} is shown as its raw id"


def test_compare_actually_answers_to_the_selection_it_claims_to(client, session_id,
                                                               other_session_id):
    """Not a restatement of SELECTION_SCOPED. Measured against what the pane returns.

    Compare was in neither the scoped set nor the help text, so the page told the reader "the
    header selection does not change this tab" while `compare_layout()` was taking arm A straight
    off it. Measured on the real store, the pane goes from 1,292 sessions to 1 the moment a session
    is chosen. Asserting membership in the set proves nothing; asserting that the payload MOVES is
    what would have caught it.
    """
    def pane(**params):
        return client.get("/api/tab/tab-compare/render", params=params).json()

    whole = pane()
    one = pane(session=session_id)
    assert whole["tables"] != one["tables"], (
        "choosing a session did not change Compare, so calling it scoped would be the false "
        "statement this test exists to prevent")
    assert one["scoped"] is True, "Compare answers to the selection but does not say so"

    # And arm B is steerable, which is the whole reason a fork can be compared to its parent.
    default_b = pane(session=session_id)
    chosen_b = pane(session=session_id, compare_with=other_session_id, compare_kind="session")
    assert default_b["tables"] != chosen_b["tables"], (
        "compare_with did not change the pane, so the page cannot choose arm B at all")


def test_compare_names_the_two_chats_rather_than_counting_them(client, session_id,
                                                              other_session_id, cohort):
    """An export has to say what it compared.

    Both arms read "1 session, main thread only", which is true of either one and identifies
    neither. On screen the pickers above carry the names; in a CSV or a PDF of this table they do
    not travel at all, so an exported comparison could not say what it had compared.
    """
    from c4x import store
    body = client.get("/api/tab/tab-compare/render",
                      params={"session": session_id, "compare_with": other_session_id,
                              "compare_kind": "session"}).json()
    text = " ".join(body["text"])
    for sid in (session_id, other_session_id):
        name = store.session_name(sid)
        assert name, f"no name for {sid}, so this test would pass on an empty string"
        assert name in text, f"the pane does not name arm {sid}"
    # The count is kept beside the name, not replaced by it: a name says WHICH, a count says how
    # much of the store the number covers, and the second question is the one this app exists for.
    assert "1 session" in text

    # A population arm keeps the plain sentence. Naming applies to a single chat, and every other
    # tab describes one population and is untouched.
    #
    # THE COHORT COMES FROM THE FIXTURE, not from a value written here. An earlier version of this
    # named "section::Projects", which exists in the real store and in no fixture, so the check
    # passed on this machine and failed in the suite's second pytest run. That is the third time
    # this session a test has been written against a store rather than against a fixture.
    assert cohort, "no cohort in this store, so the population arm is untested"
    against_cohort = client.get("/api/tab/tab-compare/render",
                                params={"session": session_id, "compare_with": cohort,
                                        "compare_kind": "cohort"}).json()
    kind = cohort.split("::")[0]
    assert any(f"sessions in {kind}" in line for line in against_cohort["text"]), \
        f"the population arm lost its plain sentence: {against_cohort['text'][:4]}"


def test_no_column_tooltip_describes_a_different_table(client, tab_ids):
    """One shared help map, keyed on a bare column id, so a name means one thing everywhere.

    "calls" and "sessions" were written for the repeated-inputs table and say the number counts
    calls "with this exact input" and sessions that "issued this identical input". Every other
    table with a column of either name inherited that: the per-model table, the MCP server table,
    the tool table and the subagent-type table all told the reader something false about their own
    number.

    A generic guard rather than four assertions, so the next table to gain a Calls column is
    covered without anyone remembering to add a case.
    """
    # The one table those sentences are TRUE of. Identified by a column only it has, not by an
    # index or a title, both of which move.
    OWNER = "Beyond One Each"
    CLAIMS = ("this exact input", "this identical input")
    offenders = []
    for tab in tab_ids:
        body = client.get(f"/api/tab/{tab}/render").json()
        metas = body.get("meta") or []
        for i, table in enumerate(body.get("tables", [])):
            labels = {c["id"]: c["label"] for c in (metas[i]["columns"] if i < len(metas) else [])}
            if OWNER in labels.values():
                continue
            for col, tip in (table.get("tooltips") or {}).items():
                text = tip["value"] if isinstance(tip, dict) else str(tip)
                if any(claim in text for claim in CLAIMS):
                    offenders.append(
                        f"{tab} table {i} column {labels.get(col, col)!r}: {text[:70]}")
    assert offenders == [], (
        "these columns are explained with the repeated-inputs wording on a table that counts "
        "something else: " + " | ".join(offenders))


def test_the_cost_card_counts_calls_not_transcript_rows(client, session_id):
    """It divided by the wrong denominator and called the result calls.

    "5,683 of 8,202 calls" reads as a third of the calls going unpriced. 8,202 was the transcript
    ROW count, which the card two along on the same page correctly calls transcript rows, and
    every one of the 5,683 calls was priced. A reader acting on that would go looking for a
    pricing gap that does not exist.
    """
    import re
    body = client.get("/api/tab/tab-session/render", params={"session": session_id}).json()
    cards = {c["label"].lower(): c for c in body.get("stats", [])}
    cost = cards.get("estimated cost")
    assert cost, "the Session tab has no cost card"
    if "no price" in cost["sub"]:
        pytest.skip("this session ran an unpriced model, so there is no coverage line to check")
    m = re.search(r"([\d,]+) of ([\d,]+) calls", cost["sub"])
    assert m, f"the cost card no longer states its coverage: {cost['sub']!r}"
    priced, of = (int(g.replace(",", "")) for g in m.groups())

    rows = cards.get("rows")
    assert rows, "the Session tab has no rows card to compare against"
    transcript_rows = int(rows["value"].replace(",", ""))
    assert of != transcript_rows or priced == of, (
        f"the cost card is dividing by the {transcript_rows:,} transcript rows and calling them "
        f"calls")

    from c4x import store
    real_calls = int(store.q("SELECT COUNT(*) AS n FROM api_calls WHERE session_id = ? "
                             "AND is_sidechain = 0", (session_id,)).iloc[0]["n"])
    assert of == real_calls, f"the card says {of:,} calls, the store has {real_calls:,}"


def test_the_tab_says_whether_the_selection_applies_to_it(client, session_id):
    """The answer to "why does this table never change?" has to be ON the tab.

    It always was, as prose line 1 of up to 27 identical grey lines. Promoted to its own field so
    the page can put it where the question gets asked.
    """
    from c4x.ui.layout import SELECTION_SCOPED
    for tab in ("tab-diagnostics", "tab-session"):
        body = client.get(f"/api/tab/{tab}/render", params={"session": session_id}).json()
        assert body["scoped"] is (tab in SELECTION_SCOPED)
        assert body["population"], f"{tab} does not say which population it describes"
    unscoped = client.get("/api/tab/tab-diagnostics/render",
                          params={"session": session_id}).json()
    assert "Not affected by the header selection" in unscoped["population"]


def test_heat_bands_survive_and_keep_their_order(client):
    """Rank shading is how a 317-row table shows its outliers without being sorted.

    ORDER MATTERS: rules run shallowest to deepest and a top value matches them all, so the LAST
    match wins. Served out of order, every outlier would be painted the palest colour.
    """
    body = client.get("/api/tab/tab-sessions/render").json()
    banded = [c for table in body["meta"] for c in table["columns"] if c["bands"]]
    if not banded:
        # Same assumption that broke the two cohort tests above, caught by re-reading the batch
        # rather than by a run: `heat_cells()` returns nothing below five distinct values, so a
        # small store has no shading to check and that is correct behaviour, not a regression.
        pytest.skip("this store is too small for rank shading, so there are no bands to order")
    for column in banded:
        thresholds = [b["at"] for b in column["bands"]]
        assert thresholds == sorted(thresholds), f"{column['id']} bands are not shallowest first"


def test_a_section_says_what_it_wraps(client):
    """A collapsible is only a collapsible when it holds PROSE.

    `theme.accordion()` takes any children, and the Summary tab uses it three times: around the
    findings table, around the stat cards, and around the project chart. `extract.texts()` reads
    prose and nothing else, so all three arrived with an empty body and the page drew a heading over
    nothing, twice, and printed the stat cards' own text a second time in the third.
    """
    body = client.get("/api/tab/tab-summary/render").json()
    kinds = {s["wraps"] for s in body["details"]}
    assert kinds == {"table", "stats", "figure"}, f"Summary's sections classified as {kinds}"
    for section in body["details"]:
        if section["wraps"] == "table":
            assert 0 <= section["wraps_index"] < len(body["tables"])
        if section["wraps"] == "figure":
            assert 0 <= section["wraps_index"] < len(body["plotly"])
        if section["wraps"] in ("table", "figure"):
            # These are exactly the ones that came back empty and drew a box over nothing.
            assert not section["body"], "a wrapped section should have no prose of its own"


def test_a_query_section_is_still_prose(client):
    """The SQL sections must NOT be swept up by the same rule: they are the feature."""
    body = client.get("/api/tab/tab-cost/render").json()
    assert body["details"], "the Cost tab's queries vanished"
    for section in body["details"]:
        assert section["wraps"] == "text"
        assert any("SELECT" in line.upper() for line in section["body"])


def test_the_tab_list_says_what_each_tab_is_and_whether_it_is_scoped(client):
    """So the navigation can carry it as a tooltip WITHOUT rendering eight panes to find out.

    Both facts are already declared, one in `SELECTION_SCOPED` and one in `TAB_HELP`. Rendering
    every tab to populate every tooltip would cost seconds on this store.
    """
    from c4x.ui.layout import SELECTION_SCOPED
    served = client.get("/api/tabs").json()
    for tab in served:
        assert tab["scoped"] is (tab["id"] in SELECTION_SCOPED)
        assert tab["help"], f"{tab['id']} has no description for its tooltip"
        assert tab["help"] != tab["label"], f"{tab['id']}'s help just repeats its name"


def test_the_shutdown_route_refuses_GET(client):
    """It moved here when app.py stopped serving, and the refusal moved with it.

    Loopback is no defence against a GET route: the browser is on loopback too, so any page the
    user visits could stop this server with `<img src=".../__shutdown__">`. Without the refusal the
    request falls through to the static mount and returns 200 with the app's own HTML, which reads
    as accepted by a process that was never going to stop. NOT called with POST here, for reasons
    that should be obvious from what POST does.
    """
    response = client.get("/__shutdown__")
    assert response.status_code == 405
    assert "POST" in response.text


def test_the_legacy_health_route_still_answers(client):
    """Anything that watched the dashboard's `/__health__` keeps working."""
    body = client.get("/__health__").json()
    assert body["ok"] is True
    assert body["db"].endswith(".db")


def test_the_entry_point_checks_its_own_argument_handling():
    from c4x.api.__main__ import db_from_argv, port_from_argv
    assert port_from_argv(["--port", "9999"]) == 9999
    assert port_from_argv([]) != 8056, "the API must not default to the dashboard's port"
    assert db_from_argv([]) is None

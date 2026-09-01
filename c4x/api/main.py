"""The routes. One per thing the dashboard can be asked.

TWO PAYLOADS PER TAB, and the split is the whole design.

`extract.describe()` summarises a chart: name, type, point count, and the extent of each axis. That
is the right shape to CHECK a chart with and the wrong shape to DRAW one, because it contains no
series. So `/api/tab/{id}` serves the summary, byte for byte what `c4x.cli dump --json` already
emits, which is what lets the existing CLI and the render-path tests be pointed at this server
unchanged. `/api/tab/{id}/render` serves the same plus the full Plotly JSON, which is what a
frontend actually needs. Measured, the difference is 128 KB against 439 KB on the largest tab.

Serving only the summary would give React nothing to draw. Serving only the raw figure would throw
away the one shape the whole test suite already knows how to read.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# THE API IS A READER. The dashboard is not: its refresh tick runs an incremental harvest, so
# pointing two of those at one store means two writers. This is set before `app` is imported,
# because that import is what registers the tick.
os.environ.setdefault("C4X_READ_ONLY", "1")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, HTTPException, Query, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from c4x.api import cache  # noqa: E402

api = FastAPI(
    title="c4x",
    summary="What the context-capture dashboard renders, as JSON.",
    version="0",
    # The docs are left on. This binds to loopback and serves a local store; an interactive schema
    # is worth more here than the byte it saves to hide it.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# The Vite dev server runs on another port, so the browser treats it as another origin. Listed
# explicitly rather than "*": this process can read every conversation on the machine, and a
# wildcard would let any page the user visits read it too.
api.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _jsonable(payload):
    """Coerce a payload to types JSON actually has.

    `extract.describe()` reports chart extents as whatever numpy handed it, so a trace's `y_max` is
    an `np.float64` and its `points` an `np.int64`. FastAPI's encoder refuses both, and the failure
    is a 500 with "when serializing dict item 'traces'" rather than anything naming numpy.

    It does NOT bite every tab, which is what makes it dangerous: the Cost tab's extents happen to
    be plain floats and returned 200, while the Session tab's did not. Testing one endpoint would
    have shipped this.

    Plotly's own encoder because it already handles numpy scalars, pandas timestamps and NaN (which
    it writes as null, the right answer for a figure that has no value there rather than a zero).
    """
    import json

    import plotly.utils as plotly_utils
    return json.loads(json.dumps(payload, cls=plotly_utils.PlotlyJSONEncoder))


def _cached(key, build):
    """Serve `key` from the cache, or build it, serialise it once, and keep it.

    The endpoint returns raw bytes rather than a dict, which is not a micro-optimisation: FastAPI
    would otherwise re-encode a 543 KB payload on every hit, and re-encoding on a hit would put back
    most of what the cache is for.

    `no_cache=1` on any request skips it entirely. There is no invalidation endpoint on purpose,
    because an endpoint that empties a cache is a thing to remember to call; the version stamp means
    there is nothing to remember.
    """
    from c4x import store
    version = cache.stamp(str(store.DB_PATH))
    found = cache.get(key, version)
    if found is not None:
        return Response(content=found, media_type="application/json",
                        headers={"x-c4x-cache": "hit"})
    import json
    payload = json.dumps(build()).encode("utf-8")
    cache.put(key, version, payload)
    return Response(content=payload, media_type="application/json",
                    headers={"x-c4x-cache": "miss"})


def _app():
    """The Dash module, imported once, on first use.

    Not at module import: bringing in `app` builds the layout and runs every tab's import-time
    work, which makes `--help` on this server take a second and a half for no reason.
    """
    import app as module
    return module


def _tab_ids():
    return [t[0] for t in _app().TABS]


def _pane(tab_id, session, scope, cohort, compare_with=None, compare_kind="session"):
    """One rendered pane, or a 404 naming what does exist.

    A wrong tab id is the most likely mistake a caller makes, so the error lists the real ones
    rather than saying "not found" and leaving them to guess.
    """
    ids = _tab_ids()
    if tab_id not in ids:
        raise HTTPException(status_code=404,
                            detail={"error": f"unknown tab {tab_id!r}", "known": ids})
    # Compare's pane is a pair of dropdowns; its BODY arrives from its own callback. Rendering the
    # pane alone returns zero tables and zero figures, which reads as a broken tab rather than as a
    # tab whose content is one callback away. The CLI shipped that bug for as long as it existed
    # and it is not being reproduced here.
    #
    # Without `compare_with` the arm is the tab's own `default_arm_b`, so a caller who names nothing
    # sees what a reader sees on arrival rather than an empty room. With it, the caller picks the
    # arm, which is what `c4x.cli dump --compare-with` has always been able to do in-process and
    # could not do over HTTP until this parameter existed.
    if tab_id == "tab-compare":
        target = compare_with
        if not target:
            from c4x.tabs.compare import default_arm_b
            target = default_arm_b(session, cohort)
        if target:
            return _app()._cmp_render(compare_kind, target, session, cohort, scope or "main")
    return _app()._render_tab(ids.index(tab_id), session, scope or "main", cohort)


@api.get("/api/health")
def health():
    from c4x import store
    return {"ok": True,
            "db": str(store.DB_PATH),
            # TWO SEPARATE FACTS, reported separately on purpose. `read_only` means this process
            # never harvests and is always true here. `writes_enabled` means the export, import and
            # delete routes will answer. Collapsing them into one flag would leave the UI unable to
            # say WHY a control is disabled, so it would fail on click instead.
            "read_only": bool(os.environ.get("C4X_READ_ONLY")),
            "writes_enabled": _writes_enabled(),
            # Reported so the cache can be checked on a running server rather than trusted. A hit
            # rate of zero in the wild would mean the version stamp is moving on every request,
            # which is a failure that costs nothing visible and undoes the whole of phase 3.
            "cache": cache.stats()}


@api.get("/api/tabs")
def tabs():
    """The tab registry, read from the app rather than restated here.

    A hand-written list in this file would be a second source of truth for something that already
    has one, and it would go stale exactly the way the migration proposal did.

    `scoped` and `help` ride along so the navigation can say what a tab is and whether the header
    selection reaches it WITHOUT rendering that tab. Rendering eight panes to populate eight
    tooltips would cost seconds; both facts are static and already declared, one in
    `SELECTION_SCOPED` and one in `TAB_HELP`.
    """
    from c4x.theme import tab_help
    from c4x.ui.layout import SELECTION_SCOPED
    return [{"id": t[0], "label": t[1],
             "scoped": t[0] in SELECTION_SCOPED,
             "help": tab_help(t[0])} for t in _app().TABS]


@api.get("/api/tab/{tab_id}")
def tab(tab_id: str,
        session: str | None = Query(None),
        scope: str = Query("main", pattern="^(main|all)$"),
        cohort: str | None = Query(None),
        compare_with: str | None = Query(None, description="tab-compare only: the other arm"),
        compare_kind: str = Query("session", pattern="^(session|cohort)$"),
        no_cache: bool = Query(False, description="rebuild rather than serving a cached answer")):
    """The verification shape: tables with their rows and tooltips, charts as extents, text.

    Identical to `python -m c4x.cli dump --tab <id> --json`, which is what makes this the surface
    the parity differ compares and the surface the existing tests can be re-pointed at.
    """
    from c4x.cli import extract

    def build():
        payload = extract.describe(
            _pane(tab_id, session, scope, cohort, compare_with, compare_kind))
        payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
        return _jsonable(payload)

    if no_cache:
        return build()
    return _cached(("verify", tab_id, session, scope, cohort, compare_with, compare_kind), build)


@api.get("/api/tab/{tab_id}/render")
def tab_render(tab_id: str,
               session: str | None = Query(None),
               scope: str = Query("main", pattern="^(main|all)$"),
               cohort: str | None = Query(None),
               compare_with: str | None = Query(None),
               compare_kind: str = Query("session", pattern="^(session|cohort)$"),
               no_cache: bool = Query(False)):
    """The drawing shape: everything above, plus each chart as full Plotly JSON.

    Charts are returned in the order they appear in the pane, so `plotly[i]` describes the same
    figure as `figures[i]`. A frontend that pairs them by index is relying on something real.
    """
    from c4x.cli import extract

    def build():
        pane = _pane(tab_id, session, scope, cohort, compare_with, compare_kind)
        payload = extract.describe(pane)
        payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
        payload["plotly"] = [f.to_plotly_json() for f in _figures(pane)]
        sections = _details(pane)
        # The pairing is only meaningful if this walk saw the same tables the extractor did. If the
        # two ever diverge, an index would point at the wrong table and a query would be shown under
        # a table it did not produce, which is worse than showing no query at all. Dropped rather
        # than served wrong, and said out loud in the payload.
        limit = len(payload["tables"])
        for section in sections:
            if not -1 <= section["table_index"] < limit:
                section["table_index"] = None
        payload["details"] = sections

        # Per-table presentation the verify shape drops. Paired to `payload["tables"]` BY INDEX and
        # only when both walks saw the same number of tables: a label attached to the wrong table
        # would rename a column that does not exist on it, which is worse than showing the raw id.
        meta = _table_meta(pane)
        payload["meta"] = meta if len(meta) == len(payload["tables"]) else []

        # WHICH POPULATION THIS TAB DESCRIBES, as a field rather than as prose.
        #
        # The app has always said it: `_render_tab` puts a banner at the top of every pane, and it
        # arrives here inside `text` because `extract.texts()` flattens the pane. But it arrives as
        # one grey line among twenty-seven identical grey lines, so on the Diagnostics tab the
        # sentence "Store-wide. Not affected by the header selection." sat far above the table
        # somebody was actually looking at, and the honest answer to "why does this never change?"
        # was on screen and unfindable. Promoted to its own field so the page can put it where the
        # question gets asked.
        payload["stats"] = _stats(pane)

        from c4x.ui.layout import SELECTION_SCOPED
        payload["scoped"] = tab_id in SELECTION_SCOPED
        payload["population"] = _population(pane)
        return _jsonable(payload)

    if no_cache:
        return build()
    return _cached(("render", tab_id, session, scope, cohort, compare_with, compare_kind), build)


def _population(node):
    """The one sentence saying what this tab's numbers cover, or None if it states none.

    FOUND BY ITS CLASSNAME. This used to test whether the pane's first line started with
    "Describing " or "Store-wide.", which meant the three tabs that state their population in their
    own wording reported none at all, and any rewording anywhere would have switched the field off
    with nothing failing. `theme.population_note` marks the line at the point it is written.
    """
    from c4x.cli import extract
    found = []

    def walk(item):
        if found:
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
            return
        if not hasattr(item, "_prop_names"):
            return
        if "population-note" in str(getattr(item, "className", "") or "").split():
            found.append(" ".join(extract.texts(item)).strip())
            return
        for name in item._prop_names:
            value = getattr(item, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value)

    walk(node)
    return found[0] if found else None


def _stats(node, found=None):
    """The headline figures a tab leads with, as {label, value, sub}, in document order.

    `theme.stat_card()` builds three stacked divs: a label, the number, and a caption. Seven of them
    reach the API as twenty-one loose strings in `text`, because `extract.texts()` flattens the
    pane, and a frontend re-grouping those into threes would be inferring a structure rather than
    reading one. It would also be wrong the first time a card gained a fourth child, quietly, with
    every later card shifted by one.

    So the cards are marked with a className at the point they are built and read back here. Found
    by class rather than by shape, because a rule like "a div with exactly three text children"
    would also match things that are not stat cards.
    """
    from c4x.cli import extract
    found = [] if found is None else found

    def walk(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        if not hasattr(node, "_prop_names"):
            return
        classes = str(getattr(node, "className", "") or "")
        if "stat-card" in classes.split():
            from c4x.theme import column_label
            parts = [" ".join(extract.texts(c)) for c in (node.children or [])]
            found.append({
                # Through the SAME naming rule the columns use, so "api calls" and "Turns" are
                # capitalised the one way rather than each by whoever wrote them. The card labels
                # were authored lowercase and the page used to shout them in CSS instead, which is
                # a third casing.
                "label": column_label(parts[0]) if parts else "",
                "value": parts[1] if len(parts) > 1 else "",
                "sub": parts[2] if len(parts) > 2 else "",
            })
            return
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value)

    walk(node)
    return found


def _heat_bands(conditional):
    """The rank-banded cell shading, per column, read off the table's own rules.

    `theme.heat_cells()` shades a numeric column by which band of its own distribution a value
    falls in, so a 300-row table shows its outliers without being sorted, in whatever order the
    reader already has it. Dash expresses that as `style_data_conditional` entries; this reads them
    back rather than recomputing the bands, so there is no second copy of the thresholds to drift.

    ORDER IS LOAD-BEARING and preserved. The rules are emitted shallowest first, and a value in the
    top band matches every rule below it too, so the LAST match is the one that wins. A consumer
    that stopped at the first match would shade every outlier the palest colour.
    """
    import re
    out = {}
    for rule in conditional:
        test = (rule or {}).get("if") or {}
        column, query = test.get("column_id"), test.get("filter_query")
        colour = (rule or {}).get("backgroundColor")
        if not (column and query and colour):
            continue
        found = re.search(r"(>=|<=)\s*(-?[\d.]+)", str(query))
        if not found:
            continue
        try:
            threshold = float(found.group(2))
        except ValueError:
            continue
        out.setdefault(column, []).append(
            {"op": found.group(1), "at": threshold, "background": colour,
             "color": (rule or {}).get("color")})
    return out


def _table_meta(node, found=None):
    """Everything about a table that `describe()` throws away, in document order.

    `extract.tables()` reduces a column to its ID and stops. The live `DataTable` also declares the
    column's TYPE, the d3 format its numbers are written with, which columns are hidden, whether the
    table filters, and how many rows a page holds. All of that is presentation the app already
    decided, and dropping it did not make the frontend simpler, it made the frontend guess:

    - `percent` is declared `.1f`, so Dash writes 43.3, 2.4, 1.2, 1.0. The browser guessed from the
      runtime value with `Number.isInteger`, and wrote 43.30, 2.40, 1.20, 1. Three different
      precisions in one column, and the bare 1 is not a rounding nit: it reads as a different
      quantity from the rows above it.
    - `filter_action="native"` was on the tables and the port silently lost filtering entirely.
    - `session_id` is a HIDDEN column: in every row, absent from the column list. That is how the
      identifier travels without showing a uuid, and it is why row-click has to read the row.

    THE FORMAT TRAVELS AS ITS d3 SPECIFIER, verbatim, because that is what a Dash `Format` reduces
    to: `,` for grouped thousands, `.1f` and `.2f` for fixed decimals. Sending the specifier rather
    than an interpretation of it keeps `numeric_columns()` the one authority on how a number in this
    app is written.

    Built at RENDER time from the pane, not from a static map, so a table that only appears inside a
    sub-panel is covered without anybody remembering to register it. Three of this app's explicit
    formats live in exactly such a panel.
    """
    from c4x.cli import extract
    from c4x.theme import column_label, table_label
    found = [] if found is None else found

    def walk(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
            return
        if not hasattr(node, "_prop_names"):
            return
        if type(node).__name__ == extract.TABLE_TYPE:
            hidden = set(getattr(node, "hidden_columns", None) or [])
            bands = _heat_bands(getattr(node, "style_data_conditional", None) or [])
            columns = []
            for spec in (getattr(node, "columns", None) or []):
                if not isinstance(spec, dict):
                    continue
                cid = spec.get("id")
                specifier = None
                fmt = spec.get("format")
                if fmt is not None:
                    try:
                        specifier = fmt.to_plotly_json().get("specifier")
                    except Exception:      # noqa: BLE001 - a format we cannot read is not a crash
                        specifier = None
                numeric = spec.get("type") == "numeric"
                columns.append({
                    "id": cid,
                    "label": column_label(cid),
                    "numeric": numeric,
                    "specifier": specifier,
                    # The header follows the cells. Dash left-aligned both, which is consistent and
                    # hard to read down a column of numbers; aligning only the cells is neither.
                    "align": "right" if numeric else "left",
                    "hidden": cid in hidden,
                    "bands": bands.get(cid, []),
                })
            found.append({
                "id": getattr(node, "id", None) or "(anonymous)",
                "title": table_label(getattr(node, "id", None)),
                "columns": columns,
                "filterable": getattr(node, "filter_action", "none") != "none",
                "page_size": getattr(node, "page_size", None),
            })
            return
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value)

    walk(node)
    return found


def _wrapped_kind(children):
    """What a collapsible section actually contains: a table, a chart, stat cards, or prose.

    The whole point is that a section is NOT always prose. `theme.accordion()` takes any children,
    and on the Summary tab it is used three times: around the findings table, around the stat cards,
    and around the project chart. `extract.texts()` reads prose and nothing else, so all three came
    back with an empty body and the page drew a heading over nothing, twice, and printed the stat
    cards' text a second time in the third.

    Checked in order of specificity. A section holding both a table and prose is a table section:
    the prose is its caption, and the table is the thing.
    """
    from c4x.cli import extract

    def contains(node, wanted):
        if isinstance(node, (list, tuple)):
            return any(contains(child, wanted) for child in node)
        if not hasattr(node, "_prop_names"):
            return False
        if type(node).__name__ == wanted:
            return True
        if "stat-card" in str(getattr(node, "className", "") or "").split() and wanted == "@stat":
            return True
        return any(contains(getattr(node, name, None), wanted)
                   for name in node._prop_names
                   if isinstance(getattr(node, name, None), (list, tuple))
                   or hasattr(getattr(node, name, None), "_prop_names"))

    if contains(children, extract.TABLE_TYPE):
        return "table"
    if contains(children, "Graph"):
        return "figure"
    if contains(children, "@stat"):
        return "stats"
    return "text"


def _details(node, found=None):
    """Every collapsible section in a pane, as {summary, body}, in document order.

    This is the app's signature feature and it does not survive `describe()`. Each table carries the
    SQL that produced it in a `<details>`, and `extract.texts()` flattens a pane to a list of
    strings, so over the API the query arrives as loose paragraphs among the prose with nothing
    saying where it starts, what it belongs to, or that it was collapsed. A frontend rendering that
    list faithfully would print six queries down the Cost tab as body text.

    ON THE RENDER SURFACE ONLY, deliberately. `/api/tab/{id}` is the parity surface: it is compared
    field for field against Dash by `tools/parity.py`, and it is byte for byte what the CLI emits.
    Adding a field there would change what the CLI prints and what the differ compares, for the
    benefit of a browser. `/render` already exists to carry what only a browser wants.

    Nested sections are not descended into: the outermost is the one a reader collapses.

    EACH SECTION NAMES THE TABLE IT BELONGS TO, BY INDEX, not by id. Five of the Cost tab's six
    tables have no id at all, so an id would attribute one query and leave five pointing at nothing,
    which is exactly the tab where the queries matter most. The index is unambiguous.

    It is a real index into `payload["tables"]`, which means this walk has to visit the tree in the
    same order `extract.tables()` does. It uses the extractor's own table-type constant and the same
    traversal rather than a copy that looks similar, and `/render` checks the two counts agree
    before trusting the pairing.
    """
    from c4x.cli import extract
    found = [] if found is None else found
    state = {"seen": 0, "figures": 0}

    def walk(node, inside_details=False):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, inside_details)
            return
        if not hasattr(node, "_prop_names"):
            return
        kind = type(node).__name__
        if kind == extract.TABLE_TYPE:
            state["seen"] += 1
            return
        if kind == "Graph":
            # Counted the same way `_figures()` counts, so a section that wraps a chart can name
            # which chart by the index the frontend already uses to pair `figures` with `plotly`.
            state["figures"] += 1
            return
        if kind == "Details" and not inside_details:
            children = list(node.children) if isinstance(node.children, (list, tuple)) \
                else [node.children]
            summary, body = "", []
            for child in children:
                if hasattr(child, "_prop_names") and type(child).__name__ == "Summary":
                    summary = " ".join(extract.texts(child.children)) if child.children else ""
                else:
                    body.extend(extract.texts(child))
            # WHAT THIS SECTION WRAPS, which is the difference between a useful collapsible and an
            # empty box. `extract.texts()` reads prose and nothing else, so a section wrapping a
            # table, a chart or the stat cards came back with `body: []` and the page drew a heading
            # with nothing under it. On the Summary tab that was two of the three sections, and the
            # third returned the stat cards' own text and printed it a second time.
            #
            # Named rather than guessed: the tree is right here, so it is read.
            inner = _wrapped_kind([c for c in children
                                  if not (hasattr(c, "_prop_names")
                                          and type(c).__name__ == "Summary")])
            found.append({"summary": summary, "body": body, "table_index": state["seen"] - 1,
                          "wraps": inner,
                          # Which table or figure, counted the same way the extractor counts them.
                          "wraps_index": state["seen"] if inner == "table" else (
                              state["figures"] if inner == "figure" else None)})
            # Descended into anyway, so tables inside a collapsed section still advance the count
            # and every later section points at the right one. Nested Details are not collected.
            walk(node.children, inside_details=True)
            return
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value, inside_details)

    walk(node)
    return found


def _figures(node, found=None):
    """Every Plotly figure OBJECT in a pane, in document order.

    `extract.figures` deliberately returns summaries, so it cannot serve this. The walk is the same
    one, kept here rather than widening the extractor, because the CLI payload should not grow a
    half-megabyte field that only a browser wants.
    """
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            _figures(child, found)
        return found
    if not hasattr(node, "_prop_names"):
        return found
    if type(node).__name__ == "Graph":
        if getattr(node, "figure", None) is not None:
            found.append(node.figure)
        return found
    for name in node._prop_names:
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
            _figures(value, found)
    return found


@api.get("/api/cohorts")
def cohorts():
    """The populations worth asking a question about, from the app rather than derived.

    THIS EXISTS BECAUSE THE FRONTEND GOT IT WRONG BY BUILDING ITS OWN. It listed every distinct
    working directory in the store, which filled the picker with per-run temp directories nobody
    would call a project, dropped the four SECTION cohorts entirely, and sent the bare path as
    `cohort=`. The store expects `project::<path>`: `cohort_sessions()` partitions on `::`,
    recognises no kind in a bare path, and returns an empty list, which means NO RESTRICTION. So
    picking a project silently filtered nothing while the header said it had. Measured: 57 sessions
    under `project::P:\\ClaudeExt\\QuestionExtension`, 0 under the bare path.

    That is the same class of mistake as a hand-written tab list, and it is why `/api/tabs` reads
    `app.TABS` instead of restating it. Values here are opaque to the caller ON PURPOSE: the label
    is for a reader and the value is for `cohort_sessions`, and a frontend that takes them apart is
    inventing a rule the store already owns.
    """
    from c4x import store
    return store.cohort_options()


@api.get("/api/selector")
def selector(cohort: str | None = Query(None)):
    """The session picker's options, from the app, NARROWED TO THE COHORT.

    Same reasoning as `/api/cohorts`, and the frontend got this one wrong in three ways by building
    it instead: it capped the list at 300 of 317 so seventeen sessions could not be selected at all,
    it ignored the cohort so the picker offered sessions the chosen population excludes, and it
    sorted by recency and labelled by title, where the app sorts by project path then title and
    labels with the path first so a list scanned top to bottom reads grouped by project.

    None of that is presentation trivia. "The picker cannot offer a session the current population
    excludes" is an invariant the header relies on, and a picker that breaks it lets a reader select
    a session and then see a pane describing a population that does not contain it.
    """
    from c4x.ui.header import selector_options
    return selector_options(cohort)


@api.get("/api/sessions")
def sessions(limit: int = Query(50, ge=1, le=2000), cohort: str | None = Query(None)):
    """The session list, straight from the store.

    The one endpoint that does not render a pane, because the CLI's `sessions` command does not
    either: this is a browse index, not a tab.

    NEWEST FIRST, and the sort happens BEFORE the slice. `session_rows()` does not come back in
    time order, so taking `head(limit)` of it returns an arbitrary 50 of 1,325 sessions rather than
    the 50 most recent. The CLI's `sessions` command already sorts by `last_ts` before slicing, so
    without this the same command run over HTTP would list DIFFERENT sessions than run in-process,
    and both would look correct.
    """
    from c4x import store
    frame = store.session_rows()
    if cohort:
        ids = store.cohort_sessions(cohort)
        if ids:
            frame = frame[frame["session_id"].isin(ids)]
    total = int(len(frame))
    if "last_ts" in frame.columns:
        frame = frame.sort_values("last_ts", ascending=False)
    return {"rows": frame.head(limit).to_dict("records"), "total": total}


@api.get("/__shutdown__")
def shutdown_refused():
    """GET is refused, and the refusal is the point.

    Binding to loopback is no defence against a GET route, because the browser is on loopback too:
    any page the user visits could stop this server with `<img src=".../__shutdown__">`. Without an
    explicit refusal the request would fall through to the static mount and return 200 with the
    app's own HTML, which reads as though it was accepted by a process that was never going to stop.
    """
    from c4x.server import GET_REFUSED
    return Response(content=GET_REFUSED, status_code=405, media_type="text/plain")


@api.post("/__shutdown__")
def shutdown(reason: str = Query("user hit /__shutdown__")):
    """Stop the server.

    This environment requires a local web app to expose a shutdown path, and that requirement moved
    here when `app.py` stopped serving a page: the affordance has to live on whatever is actually
    listening. Deliberately undocumented, no button and no link, exactly as it was on the dashboard.

    The implementation is `c4x/server.py`'s, imported rather than rewritten, so the two cannot
    differ on what "stopped" means or on the order the page is rendered in.
    """
    from c4x.server import hardened_shutdown, stopped_page
    # Rendered BEFORE the kill, so a template fault surfaces as a failed response rather than as a
    # 500 from a process already on its way out. That exact bug shipped once.
    page = stopped_page(reason)
    hardened_shutdown(reason)
    return Response(content=page, media_type="text/html")


@api.get("/__health__")
def legacy_health():
    """The dashboard's health shape, kept so anything watching for it still works."""
    from c4x import store
    return {"ok": True, "db": str(store.DB_PATH), "port": int(os.environ.get("C4X_API_PORT", 8059))}


@api.post("/api/mirror/predict")
def mirror_predict(tokens: int, window: int = 1_000_000):
    """What the window arithmetic says about a token count.

    Delegates to `tools/mirror.mjs`, the same module the chart's threshold lines come from, so this
    endpoint cannot drift from the picture.
    """
    from c4x import store
    try:
        return store.predict(tokens, window)
    except Exception as exc:                        # noqa: BLE001 - reported, not raised as a 500
        raise HTTPException(
            status_code=503,
            detail={"error": "mirror unavailable", "cause": str(exc)[:200]}) from exc


# ---------------------------------------------------------------------------
# Moving a project in and out of the store.
#
# THE ONLY ROUTES IN THIS FILE THAT WRITE, and they get their own switch rather than borrowing
# `C4X_READ_ONLY`. That variable means "this process never harvests", which is what stopped two
# harvesters fighting over one store, and it is set unconditionally at the top of this module.
# Writing is a different thing. Reusing the harvest flag would make `--no-writes` also claim the
# server harvests, and `/api/health` would have no way to say which of the two is true.
# ---------------------------------------------------------------------------


def _writes_enabled():
    return not os.environ.get("C4X_NO_WRITES")


def _require_writes():
    if not _writes_enabled():
        raise HTTPException(status_code=403, detail={
            "error": "this server was started with --no-writes",
            "fix": "restart it without that flag to export, import or delete"})


def _project_of(cohort):
    """The working directory a cohort names, refusing anything that does not name one.

    `store.cohort_parts` rather than another `partition("::")` here. A bare path with no prefix
    resolves to NO RESTRICTION, which for a read is a wrong session count and for a delete would be
    the entire store, so the two callers must not be allowed to disagree about it.
    """
    from c4x import store
    kind, value = store.cohort_parts(cohort)
    if kind != "project":
        raise HTTPException(status_code=400, detail={
            "error": "that cohort does not name a project",
            "got": cohort,
            "want": "project::<the working directory>"})
    return value


@api.get("/api/project/excluded")
def project_excluded():
    """Projects harvest has been told to stop capturing.

    A GET, and readable with writes turned off, because a project that has silently stopped being
    captured is exactly the kind of fact this app exists to surface.
    """
    from c4x import projects
    return {"excluded": projects.excluded(), "writes_enabled": _writes_enabled()}


@api.get("/api/project/export")
def project_export(cohort: str = Query(..., description="project::<the working directory>")):
    """The whole project as one SQLite file, with its manifest inside it.

    A read, but it writes the file it serves, so it is gated with the others: on a server started
    with --no-writes there is nowhere to put it.
    """
    from fastapi.responses import FileResponse

    from c4x import projects
    _require_writes()
    project = _project_of(cohort)
    out_dir = ROOT / "tmp" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / projects.file_name(project)
    try:
        manifest = projects.export(project, out)
    except ValueError as exc:
        raise HTTPException(status_code=404,
                            detail={"error": str(exc), "project": project}) from exc
    return FileResponse(
        out, media_type="application/vnd.sqlite3", filename=out.name,
        # The counts are in the file too. They are repeated in headers so a caller that streams the
        # download straight to disk can check what it got without opening it.
        headers={"X-C4X-Project": project,
                 "X-C4X-Sessions": str(manifest["sessions"]),
                 "X-C4X-Exported-At": str(manifest["exported_at"])})


@api.post("/api/project/import")
async def project_import(file: UploadFile = File(...)):
    """Load an exported project. Verified before a single row is written."""
    from c4x import projects
    _require_writes()
    staged = ROOT / "tmp" / "imports"
    staged.mkdir(parents=True, exist_ok=True)
    # Staged to disk rather than held in memory: the largest project in this store exports to
    # 180 MB, and ATTACH needs a path in any case.
    path = staged / (Path(file.filename or "upload.db").name or "upload.db")
    with open(path, "wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    try:
        return projects.import_(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400,
                            detail={"error": str(exc), "file": path.name}) from exc


@api.post("/api/project/delete")
def project_delete(body: dict):
    """Export, verify, remove, then stop capturing. It stops at the first thing that fails.

    `confirm` must be the project path exactly. A boolean cannot tell the wrong project from the
    right one, and that is the entire risk here.
    """
    from c4x import projects
    _require_writes()
    project = _project_of(body.get("cohort"))
    try:
        return projects.delete(project,
                               confirm=str(body.get("confirm", "")),
                               keep_capturing=bool(body.get("keep_capturing")))
    except ValueError as exc:
        # 409, not 400: the request was well formed and the server refused it. A wrong confirmation
        # string is the guard working, and it should read differently from a malformed cohort.
        raise HTTPException(status_code=409,
                            detail={"error": str(exc), "project": project}) from exc


@api.post("/api/project/include")
def project_include(body: dict):
    """Lift an exclusion so harvest picks the project up again."""
    from c4x import projects
    _require_writes()
    project = str(body.get("project") or "")
    if not project:
        raise HTTPException(status_code=400, detail={"error": "no project given"})
    return {"project": project, "removed": projects.include(project)}


# The built frontend, served by this same process when it exists.
#
# LAST, and after every /api route, because a mount at "/" catches whatever did not match above it.
# Registered earlier it would swallow the API and every request would be answered with index.html,
# which a browser renders as a blank page and no error anywhere says why.
#
# It is OPTIONAL. In development the page is served by Vite on 5173 with hot reload and proxies /api
# here, so `frontend/dist` is usually absent and this does nothing. After `npm run build` the same
# server answers both, which means one process and no CORS rather than two ports to remember.
_dist = ROOT / "frontend" / "dist"
if _dist.is_dir():
    from fastapi.staticfiles import StaticFiles

    # html=True serves index.html for "/" itself. The app keeps its state in memory rather than in
    # the URL, so there are no client-side routes needing a catch-all beyond that.
    api.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")

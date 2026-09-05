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
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent.parent

# THE API IS A READER. The dashboard is not: its refresh tick runs an incremental harvest, so
# pointing two of those at one store means two writers. This is set before `app` is imported,
# because that import is what registers the tick.
os.environ.setdefault("C4X_READ_ONLY", "1")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import re  # noqa: E402

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from c4x.api import cache  # noqa: E402
from c4x.frames import jsonable, records  # noqa: E402

api = FastAPI(
    title="C4X",
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
    # One definition, shared with the CLI, so `dump --json` and this endpoint cannot disagree
    # on what a missing value looks like. c4x/frames.py says why that mattered.
    return jsonable(payload)


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


def _figure_meta(node, count):
    """The caption belonging to each chart, by figure index, for `count` charts.

    Two sources, in the order a reader meets them: the heading and note written directly ABOVE the
    chart, which `_table_meta` sees and throws away with the comment "a heading before a chart
    names the chart", and every caption explicitly marked for it by `theme.chart_note()`. The first
    needs no mark because it is already unambiguous; the second exists for the captions written
    below a chart, or in a different list from it, which position cannot resolve.
    """
    above = _preceding_notes(node, count, "Graph")
    marked = _marked_notes(node, count, "chart-note", "Graph")
    out = []
    for first, second in zip(above, marked, strict=True):
        lines = [t for t in (first["note"], second["note"]) if t]
        out.append({"note": "\n".join(lines) or None,
                    "absorbed": list(first["absorbed"]) + list(second["absorbed"])})
    return out


def _preceding_notes(node, count, kind):
    """The heading and note written directly above each chart, which `_table_meta` discards.

    It discards them for a good reason: they are not the next table's. But it had nowhere to put
    them, so "Context injected by the harness" and the paragraph under it, written above the
    Injected panel's chart, reached the reader as two loose lines while the chart they name was
    drawn with no explanation at all.

    The same rule as `_table_meta` uses, so the two cannot disagree about what a heading is: a
    SECTION_HEAD or an H-tag is the heading, any other text sibling is the note, and a table or a
    section resets both.
    """
    from c4x.cli import extract
    from c4x.theme import SECTION_HEAD
    found = []

    def plain_text(node):
        if not hasattr(node, "_prop_names") or type(node).__name__ not in (
                "Div", "P", "H2", "H3", "H4", "H5", "Span", "Small"):
            return None
        children = getattr(node, "children", None)
        if isinstance(children, str):
            return children
        if isinstance(children, (list, tuple)) and children and all(
                isinstance(c, str) for c in children):
            return " ".join(children)
        return None

    def walk(node, pending):
        if isinstance(node, (list, tuple)):
            pending = {"head": None, "note": None, "after_table": False}
            for child in node:
                walk(child, pending)
            return
        if not hasattr(node, "_prop_names"):
            return
        seen = type(node).__name__
        marks = {"chart-note", "dash-only", "table-note", "about-note", "empty-panel"}
        if set(str(getattr(node, "className", "") or "").split()) & marks:
            return
        text = plain_text(node) if pending is not None else None
        if text is not None and text.strip():
            if getattr(node, "style", None) == SECTION_HEAD or seen.startswith("H"):
                # A HEADING STARTS A NEW SECTION, which is what makes the rule below safe: a
                # heading after a table is the next thing's heading, not the table's caption.
                pending["head"], pending["note"] = text, None
                pending["after_table"] = False
            elif not pending["after_table"]:
                pending["note"] = text
            else:
                # A BARE CAPTION AFTER A TABLE BELONGS TO THAT TABLE, not to the next chart.
                # `probe_detail.py` writes "Where the two columns disagree..." under its rollup
                # table with a treemap next in the same list, and this walk served it as the
                # TREEMAP's hover: a sentence about two columns, on a chart that has none. It is
                # left unclaimed instead, which fails the gate on loose prose and makes the author
                # mark it, rather than being shown in the wrong place with nothing saying so.
                pass
            return
        if seen == kind:
            head = (pending or {}).get("head")
            note = (pending or {}).get("note")
            if pending is not None:
                pending["head"], pending["note"] = None, None
            lines = [t for t in (head, note) if t]
            found.append({"note": " ".join(lines) or None, "absorbed": lines})
            return
        if pending is not None and seen in (extract.TABLE_TYPE, "Details"):
            pending["head"], pending["note"] = None, None
            pending["after_table"] = True
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value, None if seen in (extract.TABLE_TYPE, "Details") else pending)

    walk(node, None)
    return (found + [{"note": None, "absorbed": []}] * count)[:count]


def _marked_notes(node, count, mark, kind):
    """The captions MARKED for each chart or table, by index, for `count` of them.

    THE COUNTERPART OF `_table_meta`, AND IT DOES NOT GUESS. A table's heading and note are always
    written directly above the rows, so pairing them by position works. A chart's caption is not:
    `sources.py` writes it above the chart, `waste.py` and `compactions.py` write it below, and
    `breakdown.py` writes one between a table and the chart it describes. Position alone therefore
    handed two captions to the wrong owner in `main` today, where the All sessions scatter's caption
    and the Compactions scatter's caption are both served as the hover of a table they say nothing
    about, and left ten more as loose paragraphs because a chart had no note field at all.

    So a caption is paired because `theme.chart_note()` or `theme.table_note()` MARKED it. `for_id`
    binds it to that chart or table exactly; otherwise it binds to the nearest one in its own list,
    preferring the one above it, because a caption written below a thing is about the thing above
    it. Anything that binds to nothing stays in `text` and fails the gate on loose prose, which is
    the point: an unreachable caption should be a failure, not a disappearance.

    Tables come through here as well as charts. `_table_meta` pairs FORWARD ONLY, because a
    heading is always written above its rows, and six captions across the tabs are written below
    them: "Click a row to read the summary it produced, and what it dropped." sits under the
    Compactions table and reached the reader as a paragraph at the bottom of the page.
    """
    from c4x.cli import extract
    events = []
    prefix = mark + "-for-"

    def walk(node, owner):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, id(node))
            return
        if not hasattr(node, "_prop_names"):
            return
        seen = type(node).__name__
        classes = str(getattr(node, "className", "") or "").split()
        if mark in classes:
            target = next((c[len(prefix):] for c in classes if c.startswith(prefix)), None)
            events.append(("note", extract.texts(node), target, owner))
            return
        if seen == kind:
            events.append(("graph", getattr(node, "id", None), None, owner))
            return
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value, owner)

    walk(node, None)

    # Indices in the order the extractor counts them, which is the order the frontend pairs by.
    index_at, seen = {}, 0
    for at, event in enumerate(events):
        if event[0] == "graph":
            index_at[at] = seen
            seen += 1

    notes: dict[int, list[str]] = {i: [] for i in range(count)}
    absorbed: dict[int, list[str]] = {i: [] for i in range(count)}
    for at, event in enumerate(events):
        if event[0] != "note":
            continue
        _, lines, target, owner = event
        chosen = None
        if target is not None:
            chosen = next((index_at[j] for j, other in enumerate(events)
                           if other[0] == "graph" and other[1] == target), None)
        else:
            # DOCUMENT ORDER, not the enclosing list. A table is often wrapped in a Div of its own,
            # so "the nearest table in my own list" found nothing for a caption sitting right
            # beside it on the page. The preceding one still wins, because a caption written below
            # a thing is about the thing above it.
            before = [j for j in range(at) if events[j][0] == "graph"]
            after = [j for j in range(at + 1, len(events)) if events[j][0] == "graph"]
            if before:
                chosen = index_at[before[-1]]
            elif after:
                chosen = index_at[after[0]]
        if chosen is None or chosen not in notes:
            continue
        notes[chosen].extend(line for line in lines if line and line.strip())
        absorbed[chosen].extend(lines)

    # Joined with a newline, not a space: the Window tab's composition chart carries three separate
    # statements and running them together makes one unreadable sentence in a tooltip.
    return [{"note": "\n".join(notes[i]) or None, "absorbed": absorbed[i]}
            for i in range(count)]


def _render_payload(pane):
    """Everything the drawing shape adds to `describe()`, assembled from a pane.

    A function rather than a closure in the route because the route is not the only caller that
    matters: the sub-panels are rendered by their own bodies and never pass through a tab, so a
    contract checked only through `/api/tab/{id}/render` is a contract three sub-panels are exempt
    from. The gate on loose prose runs this over the panels too, which is where it found the four
    orphaned button labels the page had been printing as sentences.
    """
    from c4x.cli import extract
    payload = extract.describe(pane)
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
    # THE CAPTIONS WRITTEN BELOW A TABLE, which the forward-only pairing above cannot see. Appended
    # to whatever it did pair, so a table with both a note above it and a caption below it carries
    # them in the order they were written.
    if payload["meta"]:
        marked = _marked_notes(pane, len(payload["meta"]), "table-note", extract.TABLE_TYPE)
        for entry, extra in zip(payload["meta"], marked, strict=True):
            joined = "\n".join(
                t for t in (entry.get("note"), extra["note"]) if t)
            entry["note"] = joined or None
            entry["absorbed"] = list(entry.get("absorbed") or []) + list(extra["absorbed"])
    # TEXT THAT BELONGS TO A DASH-ONLY CONTROL. The window calculator's labels and constants
    # sentence are marked in the tree; the page drops these lines rather than printing the
    # labels of inputs it does not draw. The parity surface keeps them: they are real text.
    payload["dash_only"] = _dash_only(pane)
    # WHAT THIS VIEW IS, as opposed to what any one chart on it shows. The page puts these on the
    # population chip, which is the thing a reader already looks at to ask what they are reading.
    payload["about"] = _marked_lines(pane, "about-note")
    # A PANEL THAT CANNOT BE FILLED still has to say so. These are titled blocks with no table
    # under them, and they are the one thing on the page that tells a reader the difference between
    # "there are none" and "this question cannot be asked from the selection you are in".
    payload["empty"] = _empty_panels(pane)
    # THE CAPTION EACH CHART ANSWERS TO, paired by index the same way `meta` is, and only when this
    # walk saw the same charts the extractor did. A caption under the wrong chart is a caption that
    # lies, which is worse than one the reader has to look for.
    figure_meta = _figure_meta(pane, len(payload["figures"]))
    payload["figure_meta"] = figure_meta if len(figure_meta) == len(payload["figures"]) else []
    # WHERE THE FULL TEXT IS. The messages table carries a 220-character `preview` per row,
    # because 400 full messages are 841 KB on the largest session and a tab is re-fetched on
    # every selection. An export is not a render: it happens once, on purpose, and a CSV of
    # previews is a CSV of the first sentence of everything. So the row says where the rest is,
    # keyed by the uuid the row already holds, and the page fetches it at export time.
    # Only when the pairing held. On a mismatch `meta` is EMPTY by design, and a strict zip
    # over it turned that documented fallback into a 500 for the whole tab; the review that
    # found it forced the mismatch and watched all eight tabs fail.
    if payload["meta"]:
        for table, entry in zip(payload["tables"], payload["meta"], strict=True):
            rows = table.get("rows") or []
            if "preview" in table.get("columns", []) and rows and "uuid" in rows[0]:
                entry["full_text"] = {"url": "/api/messages/text", "key": "uuid",
                                      "column": "preview", "as": "text"}
            # WHAT A COMPACTION REPLACED, on the row that records it. This table has told
            # the reader to click a row and read the summary since the tab existed, and on
            # this page clicking did nothing: no route, no handler. The instruction was
            # the promise; this is the thing that keeps it.
            if entry.get("id") == "tbl-compactions" and rows and "uuid" in rows[0]:
                entry["detail"] = {"url": "/api/compaction", "key": "uuid"}

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

    payload["population"], payload["population_scope"] = _population(pane)
    return payload


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
    def build():
        pane = _pane(tab_id, session, scope, cohort, compare_with, compare_kind)
        payload = _render_payload(pane)
        payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
        from c4x.ui.layout import SELECTION_SCOPED
        payload["scoped"] = tab_id in SELECTION_SCOPED
        return _jsonable(payload)
    if no_cache:
        return build()
    return _cached(("render", tab_id, session, scope, cohort, compare_with, compare_kind), build)


def _population(node):
    """(sentence, scope) for what this tab's numbers cover, or (None, None) if it states none.

    FOUND BY ITS CLASSNAME. This used to test whether the pane's first line started with
    "Describing " or "Store-wide.", which meant the three tabs that state their population in their
    own wording reported none at all, and any rewording anywhere would have switched the field off
    with nothing failing. `theme.population_note` marks the line at the point it is written.
    """
    from c4x.cli import extract
    found: list[tuple[str, str]] = []   # (text, scope), first match wins

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
            # The scope rides on the node as data, because the chip on the page needs to say what
            # this tab is describing RIGHT NOW and was deriving it from whether the tab responds to
            # the selection at all. Four tabs said "This Selection" over the whole store.
            scope = getattr(item, "data-population", None)
            if scope is None:
                scope = (item.to_plotly_json().get("props", {}) or {}).get("data-population")
            found.append((" ".join(extract.texts(item)).strip(), scope or "store"))
            return
        for name in item._prop_names:
            value = getattr(item, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value)

    walk(node)
    return found[0] if found else (None, None)


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
    out: dict[str, list[dict[str, Any]]] = {}
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
    from c4x.theme import SECTION_HEAD, column_label, table_label
    found = [] if found is None else found

    def plain_text(node):
        """The one string a text-only Div/H*/P holds, or None for anything with structure."""
        if not hasattr(node, "_prop_names") or type(node).__name__ not in (
                "Div", "P", "H2", "H3", "H4", "H5", "Span", "Small"):
            return None
        children = getattr(node, "children", None)
        if isinstance(children, str):
            return children
        if isinstance(children, (list, tuple)) and children and all(
                isinstance(c, str) for c in children):
            return " ".join(children)
        return None

    def walk(node, pending=None):
        # THE HEADING AND THE NOTE ABOVE A TABLE BELONG TO IT. evidence_block() and every hand-built
        # table put a SECTION_HEAD line and a SECTION_NOTE line before the DataTable, as siblings.
        # `describe()` flattens those into `text`, so over the API twelve of eighteen tables had no
        # title and every note was a loose paragraph far above the rows it explained: on the
        # Diagnostics tab six headings and their notes opened the page as a wall of prose. The
        # pairing is done here, where the tree is, by carrying the most recent text siblings
        # forward to the next DataTable in the same list and resetting at a chart or a section.
        if isinstance(node, (list, tuple)):
            pending = {"head": None, "note": None}
            for child in node:
                walk(child, pending)
            return
        if not hasattr(node, "_prop_names"):
            return
        kind = type(node).__name__
        # TEXT THAT ANOTHER CHANNEL OWNS IS NOT A TABLE'S NOTE. A chart caption written between a
        # table and the chart it describes would otherwise become that table's hover, which is
        # exactly what happens on All sessions and on Compactions today, and the label of a control
        # this page never draws would become the note of whatever table came next.
        if set(str(getattr(node, "className", "") or "").split()) & {
                "chart-note", "dash-only", "table-note", "about-note", "empty-panel"}:
            return
        text = plain_text(node) if pending is not None else None
        if text is not None and text.strip():
            # A heading is a SECTION_HEAD or an H-tag, nothing else. Every other text sibling is
            # the note, whatever its length or punctuation: a length rule left a 73-character
            # caption ending in a period as neither, and it printed as loose prose above the
            # Messages table on every one of fifty sessions the review walked.
            if getattr(node, "style", None) == SECTION_HEAD or kind.startswith("H"):
                pending["head"], pending["note"] = text, None
            else:
                pending["note"] = text
            return
        if pending is not None and kind in ("Graph", "Details"):
            # A heading before a chart names the chart; a section starts fresh.
            pending["head"], pending["note"] = None, None
        if kind == extract.TABLE_TYPE:
            head = (pending or {}).get("head")
            note = (pending or {}).get("note")
            if pending is not None:
                pending["head"], pending["note"] = None, None
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
            # The note as the reader should see it on the heading: without the row count, which
            # the page states beside the heading from the rows it holds, and without the Dash
            # paging clause, which describes a table this page does not draw.
            shown = note or ""
            # BOTH SPELLINGS. evidence_block writes "1 row." for a one-row table, and this
            # stripped the plural only, so the count survived into the note and the page
            # stated it twice: "MCP Calls, 1 row" over a note opening
            # "1 row. Invocation count alone is a PROXY for cost."
            shown = re.sub(r"^\s*[\d,]+ rows?\.\s*", "", shown)
            shown = shown.replace("(table shows the first page; export gives every row)", "")
            shown = shown.strip()
            found.append({
                "id": getattr(node, "id", None) or "(anonymous)",
                # The heading WRITTEN above the table first; the label table is for a table that
                # has none. The other order served "Session Rereads" from the label
                # table while the heading actually written above tbl-reread was absorbed into
                # nothing and shown nowhere.
                "title": head or table_label(getattr(node, "id", None)),
                "note": shown or None,
                # The exact `text` lines folded into this entry, so the page can drop them from
                # the prose block instead of printing each heading twice and each note far from
                # its table.
                "absorbed": [t for t in (head, note) if t],
                "columns": columns,
                "filterable": getattr(node, "filter_action", "none") != "none",
                "page_size": getattr(node, "page_size", None),
            })
            return
        for name in node._prop_names:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
                walk(value, None if kind in ("Graph", "Details") else pending)

    walk(node)
    return found


def _dash_only(node):
    """Every text line under a component marked className="dash-only", in document order."""
    return _marked_lines(node, "dash-only")


def _empty_panels(node, found=None):
    """Every block marked `empty-panel`, as {title, note}, in document order.

    Built the way `theme.empty_panel` builds them: the first line is the title and everything after
    it is the note. Read from the tree rather than passed alongside it, for the same reason
    `_table_meta` is: a second source of truth for what a block says is a second thing to keep in
    step with it.
    """
    from c4x.cli import extract
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            _empty_panels(child, found)
        return found
    if not hasattr(node, "_prop_names"):
        return found
    if "empty-panel" in str(getattr(node, "className", "") or "").split():
        lines = [t for t in extract.texts(node) if t and t.strip()]
        if lines:
            found.append({"title": lines[0], "note": " ".join(lines[1:]) or None})
        return found
    for name in node._prop_names:
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
            _empty_panels(value, found)
    return found


def _marked_lines(node, mark, found=None):
    """Every text line under a component carrying `mark`, in document order."""
    from c4x.cli import extract
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            _marked_lines(child, mark, found)
        return found
    if not hasattr(node, "_prop_names"):
        return found
    if mark in str(getattr(node, "className", "") or "").split():
        found.extend(extract.texts(node))
        return found
    for name in node._prop_names:
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or hasattr(value, "_prop_names"):
            _marked_lines(value, mark, found)
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
            summary, summary_note, body = "", "", []
            for child in children:
                if hasattr(child, "_prop_names") and type(child).__name__ == "Summary":
                    # THE TWO HALVES STAY TWO. `theme.accordion()` writes a title and a caption as
                    # two styled spans, and joining them gave the page one string with no hover:
                    # "Recommendation(s) 6 finding(s), each with an action" was the heading of the
                    # Summary tab's findings table, over a table whose own name is "Findings". The
                    # caption is marked, so the title is everything else.
                    parts = child.children
                    if not isinstance(parts, (list, tuple)):
                        parts = [parts] if parts else []
                    heads: list[str] = []
                    subs: list[str] = []
                    for part in parts:
                        target = subs if "accordion-sub" in str(
                            getattr(part, "className", "") or "").split() else heads
                        target.extend(extract.texts(part))
                    summary = " ".join(t for t in heads if t).strip()
                    summary_note = " ".join(t for t in subs if t).strip()
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
            found.append({"summary": summary, "summary_note": summary_note or None,
                          "body": body, "table_index": state["seen"] - 1,
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


class _Uuids(BaseModel):
    uuids: list[str]


@api.post("/api/messages/text")
def messages_text(body: _Uuids):
    """Full text for up to 1,000 message uuids, for an export that must not stop at the preview.

    A POST with the keys in the body rather than a GET with a session, because the table that
    needs this does not carry its session id in its rows and 400 uuids do not fit a URL.
    """
    from c4x.store import messages_text as full
    if len(body.uuids) > 1000:
        raise HTTPException(status_code=422, detail="at most 1,000 uuids per request")
    return full(body.uuids)


@api.get("/api/compaction/{uuid}")
def compaction(uuid: str, limit: int = Query(300, ge=1, le=5000)):
    """What a compaction REPLACED, and what it dropped, for one boundary.

    THE MOST IMPORTANT THING ON THAT TAB AND THE ONE THING THE BROWSER COULD NOT REACH. The store
    has held it all along: `compactions.summary_uuid` joins to the summary message, and
    `compaction_survivors` is what makes the dropped set computable. The Dash page has shown both
    on a row click since the tab existed, through a callback into `compaction-detail`. The React
    port drew the token counts and never built this, while the table's own note told the reader to
    click a row to read the summary. A page that instructs a reader to do something it does not
    implement is worse than one that says nothing.

    Returns the summary as TEXT, not a preview. A compaction summary is 12,000 to 17,000 characters
    in this store, it is written once and read deliberately, and truncating it would defeat the
    only reason to open it.
    """
    from c4x.store import (
        compaction_dropped,
        compaction_dropped_count,
        compaction_kept,
        compaction_kept_count,
        compaction_summary_text,
        compaction_survivors_recorded,
    )
    summary = compaction_summary_text(uuid)
    dropped = compaction_dropped(uuid, limit=limit)
    kept = compaction_kept(uuid, limit=limit)
    row = None
    if not summary.empty:
        first = summary.iloc[0]
        row = {"text": str(first["text"]), "chars": int(first["chars"]), "ts": str(first["ts"])}
    return _jsonable({
        "uuid": uuid,
        "summary": row,
        # Said out loud rather than implied by a row count: the dropped set is a LOWER BOUND,
        # because it is computed by subtracting recorded survivors, and a survivor the store never
        # saw counts as dropped here.
        "dropped": records(dropped) if not dropped.empty else [],
        "dropped_total": compaction_dropped_count(uuid),
        "dropped_shown": int(len(dropped)),
        # THE OTHER HALF. A boundary keeps a chosen few messages verbatim beside the summary it
        # writes, and which ones it chose is the most legible thing about it. Same lower bound: a
        # survivor uuid the store holds no message for cannot be shown.
        "kept": records(kept) if not kept.empty else [],
        # THREE NUMBERS, NOT TWO, because they differ by a factor of two and a half here and the
        # page printed one under the other's name: 697 survivors recorded, 268 of them messages
        # this store holds. `kept_recorded` is what the row the reader just clicked reports.
        "kept_recorded": compaction_survivors_recorded(uuid),
        "kept_total": compaction_kept_count(uuid),
        "kept_shown": int(len(kept)),
    })


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
    return {"rows": records(frame.head(limit)), "total": total}


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
def shutdown(request: Request, reason: str = Query("user hit /__shutdown__"),
             token: str | None = Query(None)):
    """Stop the server.

    This environment requires a local web app to expose a shutdown path, and that requirement moved
    here when `app.py` stopped serving a page: the affordance has to live on whatever is actually
    listening. Deliberately undocumented, no button and no link, exactly as it was on the dashboard.

    The implementation is `c4x/server.py`'s, imported rather than rewritten, so the two cannot
    differ on what "stopped" means or on the order the page is rendered in.
    """
    from c4x.server import REFUSED, hardened_shutdown, shutdown_allowed, stopped_page
    # BOTH HALVES, and the same function the Flask surface calls, so the two cannot drift on what
    # counts as authorised. POST-only was never a defence: a cross-origin form POST is a simple
    # request, so CORS does not stop it being sent, only the response being read.
    if not shutdown_allowed(request.headers.get("origin"),
                            request.headers.get("x-c4x-shutdown") or token):
        return Response(content=REFUSED, status_code=403, media_type="text/plain")
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


class _Predict(BaseModel):
    tokens: int
    window: int = 1_000_000


@api.post("/api/mirror/predict")
def mirror_predict(body: _Predict):
    """What the window arithmetic says about a token count.

    Delegates to `tools/mirror.mjs`, the same module the chart's threshold lines come from, so this
    endpoint cannot drift from the picture.

    A BODY, NOT QUERY PARAMETERS. Declared as bare scalars this bound `?tokens=1`, which makes a
    POST with no body and no content type - a CORS SIMPLE REQUEST any web page could send, landing
    in `store.predict` and spawning a node process per call. A JSON body cannot be sent
    cross-origin without a preflight, so the shape of the request is itself half the guard; the
    middleware above is the other half.
    """
    from c4x import store
    try:
        return store.predict(body.tokens, body.window)
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
    from starlette.background import BackgroundTask

    from c4x import projects
    _require_writes()
    project = _project_of(cohort)
    # A DIRECTORY PER CALL, DELETED AFTER THE RESPONSE. One shared directory kept every export
    # anyone had ever asked for - 180 MB each here - and nothing in the repo removed them. The
    # response streams from the file, so the delete has to run after the body is sent, which is
    # what a BackgroundTask is for; a per-call directory means two concurrent exports of the same
    # project cannot delete each other's file.
    out_dir = ROOT / "tmp" / "exports" / uuid4().hex
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / projects.file_name(project)
    try:
        manifest = projects.export(project, out)
    except ValueError as exc:
        raise HTTPException(status_code=404,
                            detail={"error": str(exc), "project": project}) from exc
    return FileResponse(
        out, media_type="application/vnd.sqlite3", filename=out.name,
        background=BackgroundTask(shutil.rmtree, out_dir, ignore_errors=True),
        # The counts are in the file too. They are repeated in headers so a caller that streams the
        # download straight to disk can check what it got without opening it.
        headers={"X-C4X-Project": project,
                 "X-C4X-Sessions": str(manifest["sessions"]),
                 "X-C4X-Exported-At": str(manifest["exported_at"])})


# The largest project in this store exports to 180 MB, so 1 GB is generous for a real file and
# still a bound. Without one, an upload is limited only by the disk.
_IMPORT_MAX_BYTES = 1 << 30


@api.post("/api/project/import")
async def project_import(file: UploadFile = File(...)):
    """Load an exported project. Verified before a single row is written."""
    from c4x import projects
    _require_writes()
    staged = ROOT / "tmp" / "imports"
    staged.mkdir(parents=True, exist_ok=True)
    # Staged to disk rather than held in memory: the largest project in this store exports to
    # 180 MB, and ATTACH needs a path in any case.
    #
    # THE NAME IS OURS AND THE FILE IS TEMPORARY. It used to be `file.filename`, so the caller
    # chose what appeared in this directory, and a rejected upload was left there afterwards -
    # every refusal added a file nothing would ever delete. A uuid4 cannot collide, cannot
    # traverse, and carries nothing of the caller's; the `finally` means the only import that
    # leaves anything behind is one that succeeded, and that leaves rows, not a file.
    given = Path(file.filename or "upload.db").name
    path = staged / f"upload-{uuid4().hex}.db"
    written = 0
    try:
        with open(path, "wb") as fh:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > _IMPORT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail={
                        "error": f"an import is capped at {_IMPORT_MAX_BYTES // (1 << 20)} MB",
                        "file": given})
                fh.write(chunk)
        return projects.import_(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400,
                            detail={"error": str(exc), "file": given}) from exc
    finally:
        path.unlink(missing_ok=True)


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
# THE PAGE SHELL IS NEVER CACHED. index.html names the hashed bundle, so a browser that keeps a
# stale copy of it runs last week's page against this week's API: after one rebuild here the page
# kept a bundle that had no export hydration and no heading notes, twice in an hour, and nothing
# said so until the script names were read out of the DOM. The hashed assets can be cached for as
# long as a browser likes, because a new build is a new name; the shell that names them cannot.
# EVERY ROUTE THAT CHANGES SOMETHING, GUARDED IN ONE PLACE, AND IT HAS TO BE MIDDLEWARE.
#
# The trust model is "any local process may call anything", and that is fine: a local process can
# read the store directly. A BROWSER PAGE IS NOT A LOCAL PROCESS. The CORS allow-list below stops
# a foreign page READING a response; it stops nothing being SENT, because a multipart form POST is
# a CORS SIMPLE REQUEST and is dispatched with no preflight at all. `c4x/server.py:157` already
# writes that threat model out for `/__shutdown__`, which is the one route that was guarded.
#
# WHY NOT A ROUTE DEPENDENCY. Because it would run too late to matter. FastAPI awaits
# `request.form()` while resolving the request BEFORE it calls `solve_dependencies`, so the whole
# multipart body is received and spooled - past Starlette's 1 MB `spool_max_size`, onto a real
# file - and only then would `Depends(...)` return the 403. The bytes are already on disk. HTTP
# middleware runs before routing, which is the only layer that can refuse before the read.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# A read that writes: it builds the export file it serves, so it is guarded like a write.
_GUARDED_READS = frozenset({"/api/project/export"})


def _host_is_local(host) -> bool:
    """The Host header names this machine's own loopback.

    DNS rebinding is the hole an Origin check alone leaves: a name the attacker controls resolves
    to 127.0.0.1, so the page becomes same-origin with this server and every Origin test passes.
    The server binds 127.0.0.1 (c4x/api/__main__.py:124), so a Host that is not loopback did not
    come from anything that should be talking to it.
    """
    if not host:
        return False
    # THREE FORMS, and the naive "split on the last colon" gets two of them wrong. A bare IPv6
    # literal is all colons and must not be split at all; a bracketed one carries its port OUTSIDE
    # the brackets. Written as `count(":") == 1` first, which left `[::1]:8059` as the nonsense
    # string `::1]:8059` and refused a caller that is loopback by definition.
    if host.startswith("["):
        name = host[1:host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    name = name.lower()
    # `*.localhost` as well as the literals: RFC 6761 reserves that suffix for loopback, and a dev
    # setup addressing this server as `app.localhost:8059` is a legitimate local caller. Anything
    # else is a name that resolved somewhere the server did not bind.
    return name in {"localhost", "127.0.0.1", "::1"} or name.endswith(".localhost")


@api.middleware("http")
async def _local_only_mutations(request: Request, call_next):
    from c4x.server import origin_is_local

    guarded = request.method not in _SAFE_METHODS or request.url.path in _GUARDED_READS
    if guarded:
        # Three independent refusals. Origin covers every browser that sends one; Sec-Fetch-Site
        # covers the case where it does not but the browser still labels the hop; Host closes
        # rebinding. Each is checked because each fails differently.
        why = None
        if not origin_is_local(request.headers.get("origin")):
            why = "a cross-origin request cannot change this store"
        elif request.headers.get("sec-fetch-site") == "cross-site":
            why = "a cross-site request cannot change this store"
        elif not _host_is_local(request.headers.get("host")):
            why = "this server answers only to a loopback Host"
        if why:
            return JSONResponse(status_code=403, content={"detail": {
                "error": why,
                "hint": "c4x is a local tool; call it from this machine, not from a web page"}})
    return await call_next(request)


@api.middleware("http")
async def _no_cache_shell(request: Request, call_next):
    response = await call_next(request)
    # By content type, not by path: /INDEX.HTML, /Index.Html and /index.html/ all served the
    # shell cacheable under a path rule, on a case-insensitive filesystem. The hashed assets are
    # script, style and image. The other HTML this process serves is FastAPI's own schema pages,
    # /api/docs and /redoc, and the stopped page from /__shutdown__; no-cache costs nothing on any
    # of them, and naming only one of the three was the previous version of this comment being
    # tidier than it was true. A 304 carries no content type, so the shell's revalidation is
    # matched by path as well: without the header there, a copy cached before this rule stayed
    # cached until the next rebuild changed the ETag.
    shell = request.url.path.rstrip("/").lower() in ("", "/index.html")
    if (response.headers.get("content-type", "").startswith("text/html")
            or (response.status_code == 304 and shell)):
        response.headers["Cache-Control"] = "no-cache"
    return response


_dist = ROOT / "frontend" / "dist"

if _dist.is_dir():

    from fastapi.staticfiles import StaticFiles

    # html=True serves index.html for "/" itself. The app keeps its state in memory rather than in
    # the URL, so there are no client-side routes needing a catch-all beyond that.
    api.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")

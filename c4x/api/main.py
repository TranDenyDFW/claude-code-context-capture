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

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

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
            "read_only": bool(os.environ.get("C4X_READ_ONLY"))}


@api.get("/api/tabs")
def tabs():
    """The tab registry, read from the app rather than restated here.

    A hand-written list in this file would be a second source of truth for something that already
    has one, and it would go stale exactly the way the migration proposal did.
    """
    return [{"id": t[0], "label": t[1]} for t in _app().TABS]


@api.get("/api/tab/{tab_id}")
def tab(tab_id: str,
        session: str | None = Query(None),
        scope: str = Query("main", pattern="^(main|all)$"),
        cohort: str | None = Query(None),
        compare_with: str | None = Query(None, description="tab-compare only: the other arm"),
        compare_kind: str = Query("session", pattern="^(session|cohort)$")):
    """The verification shape: tables with their rows and tooltips, charts as extents, text.

    Identical to `python -m c4x.cli dump --tab <id> --json`, which is what makes this the surface
    the parity differ compares and the surface the existing tests can be re-pointed at.
    """
    from c4x.cli import extract
    payload = extract.describe(_pane(tab_id, session, scope, cohort, compare_with, compare_kind))
    payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
    return _jsonable(payload)


@api.get("/api/tab/{tab_id}/render")
def tab_render(tab_id: str,
               session: str | None = Query(None),
               scope: str = Query("main", pattern="^(main|all)$"),
               cohort: str | None = Query(None),
               compare_with: str | None = Query(None),
               compare_kind: str = Query("session", pattern="^(session|cohort)$")):
    """The drawing shape: everything above, plus each chart as full Plotly JSON.

    Charts are returned in the order they appear in the pane, so `plotly[i]` describes the same
    figure as `figures[i]`. A frontend that pairs them by index is relying on something real.
    """
    from c4x.cli import extract
    pane = _pane(tab_id, session, scope, cohort, compare_with, compare_kind)
    payload = extract.describe(pane)
    payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
    payload["plotly"] = [f.to_plotly_json() for f in _figures(pane)]
    return _jsonable(payload)


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

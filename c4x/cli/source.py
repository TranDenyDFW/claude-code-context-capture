"""Where the CLI gets its data: the Dash app in this process, or the API over HTTP.

    python -m c4x.cli all                                   # in-process, the default
    python -m c4x.cli all --via api                         # against http://127.0.0.1:8059
    python -m c4x.cli all --via api --api-url http://...    # somewhere else

WHY THIS EXISTS AS ITS OWN FILE. Both backends have to answer the same four questions for as long
as the migration runs, and `python -m c4x.cli all` is the health check used to decide whether a
phase landed. Putting the fork here rather than sprinkling `if via == "api"` through `commands.py`
means the sweep, the dump and the session list get the switch for free, and it means retiring Dash
is deleting ONE branch of ONE file instead of auditing every command for an assumption about how
panes are built.

The payloads are the same shape from either side, because the API returns exactly what
`extract.describe()` returns. That is not a coincidence to be grateful for, it is the contract the
whole migration rests on, and `tools/parity.py` is what keeps it true.
"""
DASH = "dash"
API = "api"
DEFAULT_URL = "http://127.0.0.1:8059"

_via = DASH
_url = DEFAULT_URL


def configure(via=DASH, url=None):
    """Pick the backend. Called once from argument parsing, never from a command."""
    global _via, _url
    if via not in (DASH, API):
        raise SystemExit(f"unknown --via {via!r}. Known: {DASH}, {API}")
    _via = via
    _url = url or DEFAULT_URL
    return _via


def via():
    return _via


def describe_source():
    """One line naming where the numbers came from.

    Printed by the sweep. Two backends that agree are useless if the reader cannot tell which one
    produced the output in front of them.
    """
    return "the dashboard, in this process" if _via == DASH else f"the API at {_url}"


def _client():
    """An HTTP client, or a message saying what to start.

    A refused connection here means the server is not running, and "ConnectError" alone sends the
    reader looking for a bug instead of a terminal.
    """
    import httpx
    return httpx.Client(base_url=_url, timeout=120)


def _get(path, params=None):
    try:
        response = _client().get(path, params=params or {})
    except Exception as exc:                       # noqa: BLE001 - reported as advice, not a stack
        raise SystemExit(
            f"cannot reach the API at {_url}: {type(exc).__name__}.\n"
            f"  start it with:  python -m c4x.api") from exc
    if response.status_code != 200:
        raise SystemExit(f"API {response.status_code} for {path}: {response.text[:300]}")
    return response.json()


def _app():
    import app as module
    return module


def tab_ids():
    if _via == API:
        return [t["id"] for t in _get("/api/tabs")]
    return [tab[0] for tab in _app().TABS]


def tab_labels():
    """(id, label) pairs, for `c4x.cli tabs`."""
    if _via == API:
        return [(t["id"], t["label"]) for t in _get("/api/tabs")]
    return [(t[0], t[1]) for t in _app().TABS]


def render_tab(tab_id, session=None, scope="main", cohort=None):
    """One tab's pane, as data, from whichever backend is configured."""
    ids = tab_ids()
    if tab_id not in ids:
        raise SystemExit(f"unknown tab {tab_id!r}. Known: {', '.join(ids)}")
    if _via == API:
        return _get(f"/api/tab/{tab_id}",
                    {"session": session, "scope": scope, "cohort": cohort})
    from c4x.cli import extract
    pane = _app()._render_tab(ids.index(tab_id), session, scope, cohort)
    payload = extract.describe(pane)
    payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
    return payload


def render_compare(kind, target, session=None, cohort=None, scope="main"):
    """Compare's body, which its own callback delivers.

    The pane renders a placeholder until that callback runs, so dumping the tab alone shows nothing
    and would read as "compare produces no data".
    """
    if _via == API:
        payload = _get("/api/tab/tab-compare",
                       {"session": session, "scope": scope, "cohort": cohort,
                        "compare_with": target, "compare_kind": kind})
    else:
        from c4x.cli import extract
        payload = extract.describe(_app()._cmp_render(kind, target, session, cohort, scope))
    payload.update({"tab": "tab-compare", "kind": kind, "target": target, "session": session,
                    "scope": scope, "cohort": cohort})
    return payload


def default_compare_arm(session=None, cohort=None):
    """The arm a reader lands on. `None` when the store has nothing to compare against.

    Over HTTP this is not asked for separately: the API applies the same default when no arm is
    named, so the API path returns the pane already filled and reports the arm it chose.
    """
    if _via == API:
        return None
    from c4x.tabs.compare import default_arm_b
    return default_arm_b(session, cohort)


def sessions(limit=20, cohort=None):
    """The session list, newest first, from either backend."""
    if _via == API:
        return _get("/api/sessions", {"limit": limit, "cohort": cohort})["rows"]
    from c4x import store
    frame = store.session_rows()
    if frame.empty:
        return []
    return frame.sort_values("last_ts", ascending=False).head(limit).to_dict("records")


def self_test():
    """What can be checked with no server and no store."""
    original = (_via, _url)
    cases = [
        ("the default backend is the dashboard", configure() == DASH),
        ("the api backend can be selected", configure(API) == API),
        ("a url is remembered", (configure(API, "http://x:1"), _url)[1] == "http://x:1"),
        ("no url falls back to the default port", (configure(API), _url)[1] == DEFAULT_URL),
        ("the default url is not the dashboard's port", "8056" not in DEFAULT_URL),
        ("the source is named, not guessed at",
         "API" in describe_source() or "api" in describe_source()),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    configure(original[0], original[1])
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0

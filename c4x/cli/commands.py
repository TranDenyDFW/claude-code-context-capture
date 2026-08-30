"""What each CLI subcommand does. One function per command, so a broken one is one file to open.

Every command renders through the SAME callback the browser calls. Calling a tab builder directly
would prove the builder works and say nothing about the path that delivers it, which is where two
of this repo's defects actually lived: a table that was correct in its builder and invisible on the
page, and a tab that rendered fine on the server while the page reset itself.
"""
from c4x.cli import extract, render


def _app():
    """Import the Dash app lazily.

    Importing it costs a Dash registration pass and opens the store, which `--help` has no business
    paying for.
    """
    import app as module
    return module


def tab_ids():
    return [tab[0] for tab in _app().TABS]


def cmd_tabs(_args):
    for tab_id, label, _fn in _app().TABS:
        print(f"{tab_id:18} {label}")
    return 0


def cmd_sessions(args):
    from c4x import store
    df = store.session_rows()
    if df.empty:
        print("no sessions in the store")
        return 1
    columns = ["session_id", "title", "project", "section", "turns", "current", "peak",
               "compactions"]
    df = df.sort_values("last_ts", ascending=False)[columns].head(args.limit)
    if args.json:
        print(render.as_json({"sessions": df.to_dict("records")}))
    else:
        print(df.to_string(index=False))
    return 0


def render_tab(tab_id, session=None, scope="main", cohort=None):
    """One tab's pane, as data."""
    module = _app()
    ids = tab_ids()
    if tab_id not in ids:
        raise SystemExit(f"unknown tab {tab_id!r}. Known: {', '.join(ids)}")
    pane = module._render_tab(ids.index(tab_id), session, scope, cohort)
    payload = extract.describe(pane)
    payload.update({"tab": tab_id, "session": session, "scope": scope, "cohort": cohort})
    return payload


def render_compare(kind, target, session=None, cohort=None, scope="main"):
    """Compare's body, which its own callback delivers.

    The pane renders a placeholder until that callback runs, so dumping the tab alone shows nothing
    and would read as "compare produces no data".
    """
    body = _app()._cmp_render(kind, target, session, cohort, scope)
    payload = extract.describe(body)
    payload.update({"tab": "tab-compare", "kind": kind, "target": target, "session": session,
                    "scope": scope, "cohort": cohort})
    return payload


def cmd_dump(args):
    if args.tab == "tab-compare" and args.compare_with:
        payload = render_compare(args.compare_kind, args.compare_with, args.session, args.cohort,
                                 args.scope)
    else:
        payload = render_tab(args.tab, args.session, args.scope, args.cohort)
    print(render.as_json(payload) if args.json else render.human(payload))
    return 0


def cmd_all(args):
    """Every tab in one pass, which is what you want when asking "did anything break?".

    A tab that raises is reported and the sweep continues, because stopping at the first failure
    hides how many others are also broken.
    """
    failed = []
    for tab_id in tab_ids():
        try:
            payload = render_tab(tab_id, args.session, args.scope, args.cohort)
        except Exception as exc:                   # noqa: BLE001 - report, do not abort the sweep
            failed.append(tab_id)
            print(f"== {tab_id}\n   RAISED {type(exc).__name__}: {exc}")
            continue
        counts = (f"{len(payload['tables'])} tables, {len(payload['figures'])} figures, "
                  f"{sum(len(t['rows']) for t in payload['tables'])} rows")
        print(f"== {tab_id:18} {counts}")
        if args.verbose:
            print(render.human(payload))
    if failed:
        print(f"\n{len(failed)} tab(s) raised: {', '.join(failed)}")
    return 1 if failed else 0

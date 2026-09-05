"""What each CLI subcommand does. One function per command, so a broken one is one file to open.

Every command renders through the SAME callback the browser calls. Calling a tab builder directly
would prove the builder works and say nothing about the path that delivers it, which is where two
of this repo's defects actually lived: a table that was correct in its builder and invisible on the
page, and a tab that rendered fine on the server while the page reset itself.

WHERE the panes come from is `source.py`'s problem, not this file's. These commands read the same
payload shape whether it was built in this process or fetched from `python -m c4x.api`, which is
what makes `python -m c4x.cli all --via api` a real check on the API rather than a second program
that happens to print similar numbers.
"""
from c4x.cli import render, source
from c4x.theme import RENDER_FAILED


def tab_ids():
    return source.tab_ids()


def cmd_tabs(_args):
    for tab_id, label in source.tab_labels():
        print(f"{tab_id:18} {label}")
    return 0


def cmd_sessions(args):
    rows = source.sessions(args.limit)
    if not rows:
        print("no sessions in the store")
        return 1
    if args.json:
        print(render.as_json({"sessions": rows}))
        return 0
    import pandas as pd
    columns = ["session_id", "title", "project", "section", "turns", "current", "peak",
               "compactions"]
    frame = pd.DataFrame(rows)
    print(frame[[c for c in columns if c in frame.columns]].to_string(index=False))
    return 0


def render_tab(tab_id, session=None, scope="main", cohort=None):
    """One tab's pane, as data."""
    return source.render_tab(tab_id, session, scope, cohort)


def render_compare(kind, target, session=None, cohort=None, scope="main"):
    """Compare's body, which its own callback delivers."""
    return source.render_compare(kind, target, session, cohort, scope)


def _compare_by_default(args):
    """Compare, rendered the way the browser renders it on arrival.

    The sweep reported `tab-compare  0 tables, 0 figures, 0 rows` for as long as this command has
    existed, because the pane is a pair of dropdowns and its body arrives from a callback. Read as
    a health check, that line says the tab is broken. It was reporting the truth about the wrong
    thing.

    The tab now defaults arm B, so the sweep can ask for exactly what a reader sees: the same
    default, through the same callback. Over the API the default is applied server-side, so no arm
    is named here and the same pane comes back.
    """
    target = source.default_compare_arm(args.session, args.cohort)
    if not target:
        return render_tab("tab-compare", args.session, args.scope, args.cohort)
    return render_compare("session", target, args.session, args.cohort, args.scope)


def raised(payload):
    """Whether this payload is the apology panel rather than the tab.

    THE EXIT CODE BELOW WAS UNREACHABLE WITHOUT THIS. Both commands wrapped `render_tab` in a
    try/except and counted failures, but `render_tab` cannot raise: it goes through
    `_render_tab`, which catches the exception and returns the panel as ordinary content. So the
    `except` never fired, `failed` was always empty, and `cli all` exited 0 over a tab that had
    been broken on every request since it was written. The README calls that command the fastest
    way to ask whether anything broke.
    """
    return any(RENDER_FAILED in str(line) for line in payload.get("text", []))


def cmd_dump(args):
    if args.tab == "tab-compare" and args.compare_with:
        payload = render_compare(args.compare_kind, args.compare_with, args.session, args.cohort,
                                 args.scope)
    else:
        payload = render_tab(args.tab, args.session, args.scope, args.cohort)
    print(render.as_json(payload) if args.json else render.human(payload))
    return 1 if raised(payload) else 0


def cmd_all(args):
    """Every tab in one pass, which is what you want when asking "did anything break?".

    A tab that raises is reported and the sweep continues, because stopping at the first failure
    hides how many others are also broken.
    """
    print(f"source: {source.describe_source()}")
    failed = []
    for tab_id in tab_ids():
        try:
            if tab_id == "tab-compare":
                payload = _compare_by_default(args)
            else:
                payload = render_tab(tab_id, args.session, args.scope, args.cohort)
        except SystemExit:
            raise
        except Exception as exc:                   # noqa: BLE001 - report, do not abort the sweep
            failed.append(tab_id)
            print(f"== {tab_id}\n   RAISED {type(exc).__name__}: {exc}")
            continue
        if raised(payload):
            # Caught upstream, so it arrives as content rather than as an exception. Same bucket
            # as a raise: the sweep continues and the exit code remembers.
            failed.append(tab_id)
            first = next((str(t) for t in payload.get("text", [])), "")
            print(f"== {tab_id}")
            print(f"   RAISED {first[:120]}")
            continue
        counts = (f"{len(payload['tables'])} tables, {len(payload['figures'])} figures, "
                  f"{sum(len(t['rows']) for t in payload['tables'])} rows")
        print(f"== {tab_id:18} {counts}")
        if args.verbose:
            print(render.human(payload))
    if failed:
        print(f"\n{len(failed)} tab(s) raised: {', '.join(failed)}")
    return 1 if failed else 0

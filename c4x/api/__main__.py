"""Run the API.

    python -m c4x.api                       # 127.0.0.1:8059
    python -m c4x.api --port 8060
    python -m c4x.api --db tmp/demo-store.db
    python -m c4x.api --reload              # restart on source changes, for development
    python -m c4x.api --no-writes           # refuse project export, import and delete
    python -m c4x.api --self-test           # no network, no store

Port 8059 by default, beside the dashboard's 8056 rather than on top of it, because both are meant
to run at once during the migration and Windows lets a second process bind a port already in use
rather than refusing it. Two servers answering one port, one of them stale, is a bug this repo has
already paid for once.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PORT = 8059


def port_from_argv(argv, default=DEFAULT_PORT):
    """--port beats C4X_API_PORT beats the default. Malformed values fall back rather than crash."""
    if "--port" in argv:
        at = argv.index("--port")
        if at + 1 < len(argv):
            try:
                return int(argv[at + 1])
            except ValueError:
                pass
    try:
        return int(os.environ.get("C4X_API_PORT", default))
    except ValueError:
        return default


def db_from_argv(argv):
    """--db, resolved and exported before anything imports the store.

    Same handling as app.py, and for the same reason: `c4x.store` resolves its path at import time,
    so setting the variable afterwards changes nothing while looking like it worked.
    """
    if "--db" in argv:
        at = argv.index("--db")
        if at + 1 < len(argv):
            return str(Path(argv[at + 1]).expanduser().resolve())
    return None


def self_test():
    """What can be checked without a network or a store."""
    cases = [
        ("--port wins over everything", port_from_argv(["--port", "9999"]) == 9999),
        ("a missing --port value falls back", port_from_argv(["--port"]) == DEFAULT_PORT),
        ("a non-numeric --port falls back", port_from_argv(["--port", "abc"]) == DEFAULT_PORT),
        ("the default is not the dashboard's port", DEFAULT_PORT != 8056),
        ("--db resolves to an absolute path",
         Path(db_from_argv(["--db", "tmp/x.db"]) or "").is_absolute()),
        ("no --db means no override", db_from_argv([]) is None),
        ("a missing --db value is not an override", db_from_argv(["--db"]) is None),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()

    chosen = db_from_argv(argv)
    if chosen:
        os.environ["C4X_DB"] = chosen

    # Set before the app is imported, like C4X_DB, because the routes read it per request but
    # /api/health is what the frontend asks once at startup to decide whether to show the controls.
    if "--no-writes" in argv:
        os.environ["C4X_NO_WRITES"] = "1"

    import uvicorn

    port = port_from_argv(argv)
    reload = "--reload" in argv

    from c4x import store
    print(f"c4x api on http://127.0.0.1:{port}/api/docs")
    print(f"  store: {store.DB_PATH}")
    print("  read-only: this server never harvests, unlike the dashboard")
    print("  project export/import/delete: "
          + ("OFF (--no-writes)" if os.environ.get("C4X_NO_WRITES") else "on"))
    if reload:
        print("  reloading on source changes")

    # host is fixed, not configurable. This process can read every conversation on the machine.
    #
    # RELOAD TAKES AN IMPORT STRING, not the app object: uvicorn's reloader re-imports the module in
    # a child process, and handing it an already-constructed app silently disables reloading while
    # printing nothing. Worth the branch, because a server quietly serving code from before the last
    # edit cost two rounds of "the frontend is broken" during this migration, and both times the
    # frontend was fine.
    if reload:
        uvicorn.run("c4x.api.main:api", host="127.0.0.1", port=port, log_level="warning",
                    reload=True, reload_dirs=[str(ROOT / "c4x")])
    else:
        from c4x.api.main import api
        uvicorn.run(api, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

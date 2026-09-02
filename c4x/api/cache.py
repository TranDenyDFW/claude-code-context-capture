"""A response cache for the API, and the reason the migration can meet its one hard constraint.

    python -m c4x.api.cache --self-test

THE CONSTRAINT. The migration was accepted on "just make sure it doesn't get slower". A React
frontend cannot make it faster: measured on this store, a Summary pane is 1,581 ms of which almost
all is SQL, and moving the drawing into a browser leaves every query where it is and ADDS an HTTP
hop and a serialise. So the browser is not where the time is won. This file is.

WHAT DASH CANNOT DO AND THIS CAN. Dash rebuilds a pane on every callback: switch to Cost, switch
back to Summary, and Summary's 1,581 ms is paid again. A server that answers over HTTP can hold the
answer it already computed, so the second view of a tab costs a dictionary lookup. That is the whole
mechanism, and it is why the end state is faster than the thing it replaces rather than merely level
with it.

BYTES, NOT OBJECTS. An entry holds the SERIALISED response, because that is what the endpoint
returns: a hit then skips the Plotly encode as well as the SQL, and the memory it occupies is known
exactly rather than estimated. Measured worst case is the Session tab's `/render` at 543 KB.

INVALIDATION IS FILE STATE, NOT A TIMER. The store is harvested while the server runs, so a cache
keyed on nothing but a clock hands back a pane that is missing turns the reader can see in the
dashboard beside it. The stamp is the size and mtime of the database and its write-ahead log, which
costs microseconds and moves on ANY write, including one that does not change the row count. The
alternative measured here was `SELECT MAX(rowid) FROM turns`, which is 12.4 ms because `store.q`
opens a fresh read-only connection per call, and 12 ms is a large price on an answer that is
otherwise free.

AND A STAMP ALONE IS NOT ENOUGH, WHICH WAS MEASURED, NOT GUESSED. The first version invalidated on
the stamp and nothing else, and on this machine the Summary and Cost tabs MISSED on their second
request every time. The cause is not a bug in the stamp: a dashboard and a harvester both run
against this store continuously, and any write to any session moves a whole-store stamp, so a tab
that takes 1.5 seconds to build is usually invalidated before it can be asked for twice. A cache
that is correct and never hits is an elaborate way of adding a dictionary lookup to every request.

So an entry is served when EITHER the store has not moved since it was built, OR it is younger than
MAX_AGE_S. The second clause is the one that does the work here, and it is a real trade, stated
exactly: a reader can see a pane up to five seconds behind the store.

Five, because that is the dashboard's own refresh interval, so the page beside it is already up to
five seconds behind; and because `store.py` caches `session_rows()` for FORTY-FIVE seconds, which
means this app has always been willing to show data an order of magnitude older than this. Nothing
here is more stale than what shipped before it. That is the honest claim, and the earlier draft of
this docstring made a stronger one ("never serves anything from before the last write") which the
age bound makes false.
"""
import os
import sys
import threading
import time
from collections import OrderedDict

# Seconds an entry may be served after the store has moved. See the docstring: this is a deliberate
# staleness bound, not an optimisation, and it is the only reason the cache hits at all on a machine
# where the store is being harvested while it is read.
MAX_AGE_S = 5.0

# 64 MB, chosen against the measured worst case rather than picked round. The largest payload this
# serves is the Session tab's `/render` at 543 KB, so the budget holds about 120 of the worst entry
# and many more of a typical one. It is a bound, not a target: an idle server holds nothing.
MAX_BYTES = 64 * 1024 * 1024

_lock = threading.Lock()
_entries: "OrderedDict[tuple, tuple]" = OrderedDict()
_bytes = 0
_hits = 0
_misses = 0
_evictions = 0


def stamp(db_path):
    """A value that changes whenever the store does. Microseconds, no query, no connection.

    Both the database and its write-ahead log are read: in WAL mode a fresh turn lands in the -wal
    file and the main database's mtime does not move for some time afterwards, so watching only the
    database would serve stale panes for exactly as long as the harvester was busy.
    """
    # The None is deliberate and documented below, so the type says so rather than the reader
    # having to reach the except clause to find out.
    parts: list[tuple[int, int] | None] = []
    for path in (db_path, f"{db_path}-wal"):
        try:
            info = os.stat(path)
            parts.append((int(info.st_mtime_ns), int(info.st_size)))
        except OSError:
            # A missing -wal is normal (no WAL, or checkpointed). A missing database is not, but it
            # is not this function's business to raise about it: the caller is about to fail more
            # informatively than "cache could not stat a file".
            parts.append(None)
    return tuple(parts)


def get(key, version, now=None):
    """The cached bytes for this key, or None.

    Served when the store has not moved, OR when the entry is younger than `MAX_AGE_S`. An entry
    that fails both is dropped rather than returned: past the age bound there is no argument left
    for it, and a fast wrong answer would be worse than a slow right one.
    """
    global _hits, _misses, _bytes
    now = time.monotonic() if now is None else now
    with _lock:
        found = _entries.get(key)
        if found is None:
            _misses += 1
            return None
        stored_version, built_at, payload = found
        if stored_version != version and now - built_at >= MAX_AGE_S:
            del _entries[key]
            _bytes -= len(payload)
            _misses += 1
            return None
        _entries.move_to_end(key)
        _hits += 1
        return payload


def put(key, version, payload, now=None):
    """Store bytes against a version, evicting least-recently-used entries to stay in budget."""
    global _bytes, _evictions
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("the cache holds serialised bytes, not objects")
    now = time.monotonic() if now is None else now
    size = len(payload)
    if size > MAX_BYTES:
        # One entry larger than the whole budget is not cached rather than emptying the cache to
        # hold it. Nothing this app produces is close, but a future tab could be.
        return payload
    with _lock:
        existing = _entries.pop(key, None)
        if existing is not None:
            _bytes -= len(existing[2])
        _entries[key] = (version, now, payload)
        _bytes += size
        while _bytes > MAX_BYTES and len(_entries) > 1:
            _, evicted = _entries.popitem(last=False)
            _bytes -= len(evicted[2])
            _evictions += 1
    return payload


def stats():
    with _lock:
        return {"entries": len(_entries), "bytes": _bytes, "max_bytes": MAX_BYTES,
                "hits": _hits, "misses": _misses, "evictions": _evictions}


def clear():
    global _bytes, _hits, _misses, _evictions
    with _lock:
        _entries.clear()
        _bytes = _hits = _misses = _evictions = 0


def self_test():
    """No store, no server. Every check is about the cache's own contract."""
    global MAX_BYTES
    clear()
    original_budget = MAX_BYTES
    cases = []

    put(("a",), "v1", b"hello")
    cases.append(("a stored entry comes back", get(("a",), "v1") == b"hello"))
    cases.append(("a different key misses", get(("b",), "v1") is None))
    # The age bound, checked with an injected clock rather than by sleeping. Both halves matter:
    # inside the window a moved store is tolerated on purpose, and outside it the entry must go.
    clear()
    put(("a",), "v1", b"hello", now=1000.0)
    cases.append(("a moved store is tolerated inside the age bound",
                  get(("a",), "v2", now=1000.0 + MAX_AGE_S - 0.1) == b"hello"))
    cases.append(("a moved store is NOT tolerated outside it",
                  get(("a",), "v2", now=1000.0 + MAX_AGE_S + 0.1) is None))
    cases.append(("and the too-old entry was dropped, not kept", stats()["entries"] == 0))

    clear()
    put(("a",), "v1", b"hello", now=1000.0)
    cases.append(("an unchanged store is served however old the entry is",
                  get(("a",), "v1", now=1000.0 + 3600) == b"hello"))
    cases.append(("the staleness bound is well under store.py's own 45s session cache",
                  MAX_AGE_S < 45.0))

    clear()
    cases.append(("objects are refused, because entries are wire bytes",
                  _raises(lambda: put(("x",), "v", {"not": "bytes"}), TypeError)))

    clear()
    MAX_BYTES = 100
    put(("one",), "v", b"x" * 60)
    put(("two",), "v", b"x" * 60)
    cases.append(("the budget evicts rather than growing without limit",
                  stats()["bytes"] <= 100))
    cases.append(("the oldest entry is the one evicted", get(("one",), "v") is None))
    cases.append(("the newest entry survives", get(("two",), "v") == b"x" * 60))

    clear()
    MAX_BYTES = 10
    returned = put(("huge",), "v", b"x" * 50)
    cases.append(("an entry bigger than the whole budget is not cached",
                  stats()["entries"] == 0))
    cases.append(("and it is still returned to the caller", returned == b"x" * 50))
    MAX_BYTES = original_budget

    clear()
    put(("k",), "v", b"1")
    get(("k",), "v")
    get(("nope",), "v")
    counted = stats()
    cases.append(("hits and misses are counted, so the cache can be checked in the wild",
                  counted["hits"] == 1 and counted["misses"] == 1))

    # A stamp must MOVE when the file does. Checked against a real file rather than asserted.
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "s.db")
        with open(path, "wb") as handle:
            handle.write(b"a")
        before = stamp(path)
        with open(path, "ab") as handle:
            handle.write(b"bbbbbbbb")
        cases.append(("the stamp moves when the store is written to", stamp(path) != before))
        cases.append(("and is stable when it is not", stamp(path) == stamp(path)))
        cases.append(("a missing store does not raise", stamp(os.path.join(folder, "nope.db"))
                      is not None))

    clear()
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def _raises(call, kind):
    try:
        call()
    except kind:
        return True
    except Exception:                              # noqa: BLE001 - the wrong error is still a fail
        return False
    return False


if __name__ == "__main__":
    sys.exit(self_test())

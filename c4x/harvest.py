"""A harvest the page asked for: the Update data button.

THE SERVER STILL NEVER HARVESTS ON ITS OWN. It has no tick and no timer, which is what
`C4X_READ_ONLY` has always meant (`c4x/api/main.py`). What this module adds is the one harvest a
person asks for from the page, because until it existed the only way to update the store by hand
was a terminal, and the user's words on finding that out were "there's no button for this in the
app?".

IT CAN ONLY EVER WRITE THE INSTALL'S OWN STORE, BY CONSTRUCTION. A harvest reads this machine's
real transcripts and writes them into a store, and the transcripts root is not something the
harvester can be pointed away from (`tools/harvest.mjs`, `PROJECTS`). Pointed at a redacted COPY,
that un-redacts the copy, which happened once through the old dashboard's refresh tick
(`c4x/ui/header.py`, `refresh_store`). `python -m c4x.api --db X` exports `C4X_DB`, and a child
that inherits the environment writes into whatever is being served. So the child here is never
told which store is served: no `--db` in its argv, and `C4X_DB` taken out of its environment
(`argv_for`, `child_env`). Node then resolves its own default, `<root>/data/context.db`, from the
path of the script it was given. A wrong answer from the gates below can therefore cost honesty
(the page updating a store other than the one it shows) and never a redaction; and the gates
refuse that case too (`capability`).

ONE AT A TIME, IN THE BACKGROUND. A harvest is usually under a second (the hooks measured p50
792 ms, p99 2.8 s) and sometimes is not: a catch-up on the machine this was written on read 9,599
transcripts, 12.5 GB, in 53.7 minutes. So the POST starts a job and answers at once, the page
follows `status`, and a reload mid-run finds the job still named there. One process-local lock, in
the manner of `c4x/desktop.py`'s restart lock. It does not stop a hook's harvest running at the
same moment, and nothing should: the store has three writers by design and SQLite serialises them
(`tools/harvest.mjs`, `openDb`).

EVERY SIDE EFFECT IS A PARAMETER. No test may run the real harvester, since it would read this
machine's real transcripts, so `tests/conftest.py` makes the default runner raise and every test
passes its own.
"""
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from c4x import proc, store
from c4x.labels import plural

# WHAT THE PAGE MAY ASK FOR. A closed dictionary from a request to the flags it becomes, looked up
# and never assembled: `--full`, `--yes` and `--db` have no entry, so no request can reach them.
KINDS: tuple[str, ...] = ("incremental", "tool-outcomes", "runs")
FLAGS: dict[tuple[str, bool], list[str]] = {
    ("incremental", False): [],
    # THE TWO ONE-OFF PASSES (Store maintenance, on the Diagnostics tab). Only the fold has a dry
    # run: `--backfill-tool-outcomes --dry-run` is dispatched before the harvester looks at
    # `--dry-run`, so that combination would WRITE, and it has no entry here.
    ("tool-outcomes", False): ["--backfill-tool-outcomes"],
    ("runs", False): ["--backfill-runs"],
    ("runs", True): ["--backfill-runs", "--dry-run"],
}
# A ceiling that frees the lock from a node that hung, NOT a bound on a slow run: the 53.7 minute
# catch-up above is a real run, and the old tick's 120 s would have killed it every time, leaving
# a run that can never finish (it commits every 200 files, so it would at least creep forward).
TIMEOUT_S: dict[str, int] = {"incremental": 7200, "tool-outcomes": 7200, "runs": 3600}
# What makes a printed JSON object the report of THIS job, by kind.
SENTINELS: dict[str, tuple[str, str]] = {
    "incremental": ("mode", "files_read"),
    "tool-outcomes": ("files_scanned", "rows_unchanged"),
    "runs": ("links_after", "wrote"),
}
# The one failure worth a second try. A hook's detached harvest started by the prompt just typed
# is the likeliest thing to be holding the store at the moment somebody clicks, and a read that
# has to become a write while another writer commits fails at once rather than waiting out
# `busy_timeout`. Once, after a pause; a second failure is said plainly.
BUSY = re.compile(r"database is locked|SQLITE_BUSY", re.IGNORECASE)
RETRY_AFTER_S = 2.0
# Every key of the harvester's report this module reads. `tests/test_harvest.py` holds this to
# `RUN_REPORT_KEYS` in `tools/harvest.mjs`, whose self-test holds the real report to the same list.
READS: frozenset[str] = frozenset({
    "mode", "chains", "sidecars", "desktop_records", "files_seen", "files_read",
    "rewritten_files", "excluded_files", "lines", "mb", "turn_records_seen",
    "compaction_records_seen", "unknown_record_types", "seconds"})
# The same for the two one-off passes, held to `TOOL_OUTCOMES_REPORT_KEYS` and `RUNS_REPORT_KEYS`.
READS_TOOL_OUTCOMES: frozenset[str] = frozenset({
    "files_scanned", "files_unreadable", "rows_before", "rows_after", "rows_unchanged",
    "outcome", "result_ts_filled", "calls_still_without_an_outcome"})
READS_OUTCOMES_NESTED: dict[str, frozenset[str]] = {
    "outcome": frozenset({"filled_from_transcripts", "filled_from_the_stored_flag"})}
READS_RUNS: frozenset[str] = frozenset({
    "one_shots", "already", "unchanged", "unlinked", "linked", "batched", "misses", "heads",
    "projects", "links_before", "links_after", "by_how", "calls_without_result_ts", "wrote"})
# And the keys read INSIDE its three nested objects, held to `RUN_REPORT_NESTED` the same way.
READS_NESTED: dict[str, frozenset[str]] = {
    "chains": frozenset({"directories", "links", "reviews", "runs", "failed"}),
    "sidecars": frozenset({"read", "failed"}),
    "desktop_records": frozenset({"deleted", "gone", "returned", "failed"}),
}
ERROR_MAX = 200
FAILURES_MAX = 5


class Capability(TypedDict):
    enabled: bool
    reason: str | None
    why_not: str | None
    fix: str | None
    db: str
    own: str


class Report(TypedDict):
    kind: str
    dry_run: bool
    ok: bool
    partial: bool
    sentence: str
    short: str
    summary: dict[str, Any] | None
    seconds: float
    error: str | None
    exit_code: int | None
    attempts: int


class Job(TypedDict):
    id: str
    kind: str
    dry_run: bool
    state: str
    started_at: str
    finished_at: str | None
    report: Report | None


class Disabled(RuntimeError):
    """The server will not harvest; `.capability` says why and what to do."""

    def __init__(self, capability: Capability):
        super().__init__(capability["why_not"] or "harvesting is off on this server")
        self.capability = capability


class NoStore(RuntimeError):
    """A one-off pass was asked for and there is no store yet. The harvester would create an
    empty one and report zeros, which reads as "nothing to do" and is not. 409."""


class Busy(RuntimeError):
    """A job is already running; `.job` is that job. The route answers 409."""

    def __init__(self, job: dict[str, Any] | None):
        super().__init__("an update is already running")
        self.job = job


# ---------------------------------------------------------------------------------------------
# Whether, and against what
# ---------------------------------------------------------------------------------------------

def own_store(root: Path | str | None = None) -> Path:
    """The one store a harvest can write: the install's own."""
    return Path(store.ROOT if root is None else root) / "data" / "context.db"


def same_file(a: Path | str, b: Path | str) -> bool:
    """Whether two paths name one file, however they are spelled. Anything unanswerable is no.

    `samefile` when both exist: it compares what the paths resolve to, so drive-letter case, a
    forward slash, an 8.3 name, a subst drive and a junction all compare equal, and a COPY
    compares unequal however similar its name. When one does not exist yet (the first run, before
    any store) the spelled paths are compared after `realpath` and `normcase`, the idiom of
    `c4x/api/__main__.py` and `tools/paths.mjs` `sameStorePath`.
    """
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def carries_mark(path: Path | str) -> bool:
    """Whether the file at `path` is a redacted copy: it holds the table `tools/redact.py` stamps
    into every copy it writes. False when nothing is there yet (a first run has no copy to be).

    READ FROM THE FILE EVERY TIME, and any SQLite error RAISES. `store.store_is_redacted` answers
    the same question for the title overlay and is wrong for a write gate on both counts: it
    memoises per path for the life of the process (a copy swapped into place under a running
    server would still read as the real store, and this server asks once at startup), and it
    reads through `tables_present`, which turns "database is locked", "file is not a database"
    and a half-copied file into False, that is into "not a copy, go ahead". One `sqlite_master`
    read, independent of the store's size.
    """
    target = Path(path)
    if not target.exists():
        return False
    con = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (store.REDACTION_MARK,)).fetchone() is not None
    finally:
        con.close()


def capability(*, db: Path | str | None = None, root: Path | str | None = None,
               env: Mapping[str, str] | None = None,
               redacted: Callable[[], bool] | None = None) -> Capability:
    """Whether this server will run the harvester, and the sentence for the page when it will not.

    Three gates, each failing closed. `--no-writes` is the operator's switch. The served store
    must BE the install's own, which is what stops a copy or a fixture (the `c4x-api-fixture`
    launch config serves `tmp/test.db`) from being offered the button; a store that does not exist
    yet still counts as the install's own, so a first run can create it. And a redacted copy
    placed AT the install's own path passes any path rule, so its own mark is read, from the
    file, on every call (`carries_mark` says why `store.store_is_redacted` is not used).

    WHAT THIS DOES NOT CLOSE: a file swapped in between this check and the moment the child
    opens the store. Only a check inside `tools/harvest.mjs` could, and the hooks harvest into
    that same path with no check at all, so the button is not the way a copy left there gets
    written; this gate is about not OFFERING the button for a store that says it is a copy.
    """
    served = Path(store.DB_PATH if db is None else db)
    own = own_store(root)
    environ = os.environ if env is None else env
    found: Capability = {"enabled": True, "reason": None, "why_not": None, "fix": None,
                         "db": str(served), "own": str(own)}

    def off(reason: str, why_not: str, fix: str) -> Capability:
        return {**found, "enabled": False, "reason": reason, "why_not": why_not, "fix": fix}

    if environ.get("C4X_NO_WRITES"):
        return off("no-writes", "This server was started with --no-writes, so it does not update "
                   "the store.", "Restart it without that flag.")
    if not same_file(served, own):
        return off("not-own-store",
                   f"This server is serving {served}, not this install's own store ({own}). An "
                   "update reads this machine's real transcripts, so it only ever runs against "
                   "the install's own store.",
                   f"Start the server without --db, or with --db {own}.")
    try:
        is_copy = (redacted if redacted is not None else (lambda: carries_mark(served)))()
    except Exception as exc:  # noqa: BLE001 - a store that cannot be asked is not offered
        return off("unreadable-store", f"The store could not be read to check what it is: {exc}",
                   "Look at the store file; nothing was changed.")
    if is_copy:
        return off("redacted-copy", "This store is a redacted copy (it carries the "
                   "redacted_store mark). An update would write real transcripts back into it.",
                   "Serve the real store to update it.")
    return found


def parse_request(body: Any) -> tuple[str, bool]:
    """`{"kind": ..., "dry_run": ...}` as `(kind, dry_run)`, or `ValueError` with the sentence.

    `dry_run` is `true` or `false` and nothing else: `"yes"` or `1` from a caller who meant a dry
    run would otherwise be read as false and run as a job that WRITES. A kind with no dry run is
    REFUSED rather than run without the flag, and the flag is never passed through:
    `--backfill-tool-outcomes --dry-run` is dispatched before the harvester looks at `--dry-run`,
    so it would write.
    """
    if body is not None and not isinstance(body, dict):
        raise ValueError("the body is a JSON object")
    given = (body or {}).get("kind", "incremental")
    if not isinstance(given, str) or given not in KINDS:
        raise ValueError(f"kind is one of: {', '.join(KINDS)}")
    dry_run = (body or {}).get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run is true or false")
    if (given, dry_run) not in FLAGS:
        raise ValueError(f"{given} has no dry run")
    return given, dry_run


def argv_for(kind: str, dry_run: bool = False, *, node: str | None = None,
             root: Path | str | None = None) -> list[str]:
    """The command line: node, the harvester, and the flags `FLAGS` holds for this request.

    NEVER `--db`. See the module docstring: not naming the store is what makes the install's own
    store the only one this can write.
    """
    if (kind, dry_run) not in FLAGS:
        raise ValueError(f"no such job: {kind}{' (dry run)' if dry_run else ''}")
    base = Path(store.ROOT if root is None else root)
    return [store.NODE if node is None else node, str(base / "tools" / "harvest.mjs"),
            *FLAGS[(kind, dry_run)]]


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """This process's environment without `C4X_DB`, in any case (Windows folds the name).

    Everything else stays: the harvester reads `C4X_UNKNOWN_LOG`, `C4X_SESSIONS_ROOT`, `APPDATA`
    and `LOCALAPPDATA`, and none of those names a store.
    """
    environ = os.environ if env is None else env
    return {name: value for name, value in environ.items() if name.upper() != "C4X_DB"}


def write_stamp(root: Path | str | None = None, now: Callable[[], str] | None = None) -> bool:
    """Write the hooks' debounce stamp, so a prompt in the next fifteen seconds does not start a
    second harvester against this one (`hooks/event-hook.mjs`, `harvestDue`). Written BEFORE the
    spawn, as the hook does, for the reason recorded there.

    ONLY WHEN `data/raw` IS ALREADY THERE. Every directory under `data/` is created through
    `ensureStoreDir`, which hardens its ACL, and the harvester does that itself when it opens the
    store; a bare `mkdir` here would create one that skipped it. No directory, no stamp, and the
    harvest still runs.
    """
    path = Path(store.ROOT if root is None else root) / "data" / "raw" / ".last-harvest"
    if not path.parent.is_dir():
        return False
    try:
        path.write_text(_utc_now() if now is None else now(), encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# What the harvester said
# ---------------------------------------------------------------------------------------------

def read_report(stdout: str, kind: str = "incremental") -> dict[str, Any] | None:
    """The JSON object the harvester printed, or None when there is none this build can read.

    The whole text first; failing that, from the first `{` to the last `}`, so one stray line
    before or after the report does not lose it. It must be an object carrying the two keys
    that make it THIS job's report (`SENTINELS`): a JSON string or list is not a report, and
    neither is another job's.
    """
    text = stdout or ""
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1]):
        if not candidate.strip():
            continue
        try:
            found = json.loads(candidate)
        except ValueError:
            continue
        first, second = SENTINELS.get(kind, SENTINELS["incremental"])
        if isinstance(found, dict) and first in found and second in found:
            return found
    return None


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _failures(raw: dict[str, Any]) -> list[str]:
    """What failed INSIDE an exit 0, as phrases. The three sub-passes report three shapes:
    `chains.failed` is a list of `{dir, error}`, `sidecars.failed` a list of strings,
    `desktop_records.failed` a string or null. The harvester's self-test pins the CLEAN-run shapes
    (an empty list, a list, null); the failing ones are read defensively here, by shape, and a
    failure this cannot phrase is still said as a failure with whatever it holds."""
    said: list[str] = []
    chains = (raw.get("chains") or {}).get("failed") or []
    if isinstance(chains, list) and chains:
        first = chains[0]
        why = first.get("error") if isinstance(first, dict) else first
        said.append(f"the chains pass failed for {plural(len(chains), 'folder')} "
                    f"({str(why)[:ERROR_MAX]})")
    elif chains:
        said.append(f"the chains pass failed ({str(chains)[:ERROR_MAX]})")
    sidecars = (raw.get("sidecars") or {}).get("failed") or []
    if sidecars:
        why = sidecars[0] if isinstance(sidecars, list) else sidecars
        said.append(f"the sidecar pass failed ({str(why)[:ERROR_MAX]})")
    records = (raw.get("desktop_records") or {}).get("failed")
    if records:
        said.append(f"the desktop records pass failed ({str(records)[:ERROR_MAX]})")
    return said


def _summary(raw: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    chains = raw.get("chains") or {}
    sidecars = raw.get("sidecars") or {}
    records = raw.get("desktop_records") or {}
    unknown = raw.get("unknown_record_types") or []
    return {
        "mode": str(raw.get("mode") or ""),
        "files_seen": _count(raw.get("files_seen")), "files_read": _count(raw.get("files_read")),
        "rewritten_files": _count(raw.get("rewritten_files")),
        "excluded_files": _count(raw.get("excluded_files")),
        "lines": _count(raw.get("lines")), "mb": raw.get("mb") or 0,
        "turn_records_seen": _count(raw.get("turn_records_seen")),
        "compaction_records_seen": _count(raw.get("compaction_records_seen")),
        "chains": {"directories": _count(chains.get("directories")),
                   "links": _count(chains.get("links")), "reviews": _count(chains.get("reviews")),
                   "runs": _count(chains.get("runs"))},
        "sidecars": {"read": _count(sidecars.get("read"))},
        "desktop_records": {"deleted": _count(records.get("deleted")),
                            "gone": _count(records.get("gone")),
                            "returned": _count(records.get("returned"))},
        "unknown_record_types": [str(name) for name in unknown][:FAILURES_MAX]
        if isinstance(unknown, list) else [],
        "harvester_seconds": raw.get("seconds"),
        "failures": failures[:FAILURES_MAX],
    }


def summarise(kind: str, dry_run: bool, raw: dict[str, Any] | None, *, exit_code: int | None,
              stderr: str = "", seconds: float = 0.0, timed_out: bool = False,
              spawn_error: str | None = None, attempts: int = 1, node: str = "") -> Report:
    """One report shape for every outcome, with the sentences the page shows as they are.

    `ok` false is a FAILURE: the child never started, ran out its ceiling, or left no report.
    `partial` is an exit 0 whose report names a sub-pass that failed; what was read is stored, and
    the page says which part was not done rather than calling it clean. `short` is the header's
    line and `sentence` the hover's.
    """
    took = f"{seconds:.1f} s"
    base: Report = {"kind": kind, "dry_run": dry_run, "ok": False, "partial": False,
                    "sentence": "", "short": "", "summary": None,
                    "seconds": round(float(seconds), 1), "error": None, "exit_code": exit_code,
                    "attempts": attempts}

    def failed(sentence: str, short: str) -> Report:
        return {**base, "sentence": sentence, "short": short, "error": sentence}

    if spawn_error is not None:
        return failed(f"The update could not start: node was not found or would not run "
                      f"({node or 'node'}: {spawn_error[:ERROR_MAX]}). Nothing was changed.",
                      "Update failed: node would not start.")
    if timed_out:
        return failed(f"The update was stopped after {TIMEOUT_S.get(kind, 0):,} s without "
                      "finishing. What it had already stored is kept; run it again to continue.",
                      "Update failed: it did not finish.")
    tail = " ".join((stderr or "").split())[-ERROR_MAX:]
    if raw is None:
        if exit_code not in (0, None) and BUSY.search(stderr or ""):
            tries = "twice" if attempts > 1 else "once"
            return failed(f"The update failed {tries} because another harvest held the store "
                          "(database is locked). Wait a few seconds and try again.",
                          "Update failed: another harvest held the store.")
        if exit_code not in (0, None):
            return failed(f"The update failed: the harvester exited {exit_code}"
                          f"{': ' + tail if tail else ''}. What it had already stored is kept.",
                          f"Update failed: the harvester exited {exit_code}.")
        return failed(f"The harvester finished (exit {exit_code}) but printed no report this "
                      "build can read, so what it did is unknown. The store may have changed.",
                      "Update failed: no report came back.")

    if kind == "tool-outcomes":
        return _outcomes_report(base, raw, exit_code, took)
    if kind == "runs":
        return _runs_report(base, raw, dry_run, took, failed)

    failures = _failures(raw)
    summary = _summary(raw, failures)
    read, seen = summary["files_read"], summary["files_seen"]
    also: list[str] = []
    if summary["rewritten_files"]:
        also.append(f"{plural(summary['rewritten_files'], 'transcript')} had shrunk and "
                    "were read again from the start")
    if summary["excluded_files"]:
        also.append(f"{plural(summary['excluded_files'], 'transcript')} skipped because "
                    "their project is excluded")
    if summary["desktop_records"]["deleted"]:
        also.append(f"{plural(summary['desktop_records']['deleted'], 'chat')} deleted in the "
                    "app taken off the lists")
    if summary["unknown_record_types"]:
        also.append("record types this build does not know: "
                    + ", ".join(summary["unknown_record_types"]))
    tail_also = f" Also: {'; '.join(also)}." if also else ""
    if failures:
        return {**base, "ok": True, "partial": True, "summary": summary,
                "sentence": f"Read {plural(read, 'transcript')}, but part of the update failed: "
                            f"{'; '.join(failures)}. What was read is stored; the next update "
                            f"retries the rest ({took}).{tail_also}",
                "short": f"Updated in part: {plural(read, 'transcript')} read, "
                         f"{plural(len(failures), 'pass', 'passes')} failed."}
    if read == 0:
        return {**base, "ok": True, "summary": summary,
                "sentence": f"Nothing new: all {plural(seen, 'transcript')} were already read "
                            f"({took}).{tail_also}",
                "short": "Nothing new."}
    folders = summary["chains"]["directories"]
    return {**base, "ok": True, "summary": summary,
            "sentence": f"Read {read:,} of {plural(seen, 'transcript')} ({summary['mb']} MB, "
                        f"{plural(summary['lines'], 'line')}): "
                        f"{plural(summary['turn_records_seen'], 'turn record')}, "
                        f"{plural(summary['compaction_records_seen'], 'compaction')}; "
                        f"{plural(folders, 'folder')} re-chained ({took}).{tail_also}",
            "short": f"Updated: {plural(read, 'transcript')} read."}


def _outcomes_report(base: Report, raw: dict[str, Any], exit_code: int | None,
                     took: str) -> Report:
    """`--backfill-tool-outcomes`: how each tool call ended and when its result came back, for
    rows from before those columns existed. It only ever fills empty columns.

    A NON-ZERO EXIT WITH A REPORT IS PARTIAL, NOT A FAILURE. The pass exits 1 when the rows that
    existed before it are not the rows that exist after it, and its own comment says a harvest
    replacing a row at the same moment does exactly that, harmlessly.
    """
    outcome = raw.get("outcome") or {}
    filled = (_count(outcome.get("filled_from_transcripts"))
              + _count(outcome.get("filled_from_the_stored_flag")))
    timed = _count(raw.get("result_ts_filled"))
    scanned = _count(raw.get("files_scanned"))
    unreadable = _count(raw.get("files_unreadable"))
    still = _count(raw.get("calls_still_without_an_outcome"))
    before, after = _count(raw.get("rows_before")), _count(raw.get("rows_after"))
    moved = raw.get("rows_unchanged") is not True
    summary = {"files_scanned": scanned, "files_unreadable": unreadable, "outcomes_filled": filled,
               "result_ts_filled": timed, "calls_still_without_an_outcome": still,
               "rows_before": before, "rows_after": after, "rows_unchanged": not moved}
    notes: list[str] = []
    if unreadable:
        notes.append(f"{plural(unreadable, 'transcript')} could not be read and "
                     f"{'was' if unreadable == 1 else 'were'} skipped.")
    if moved or exit_code not in (0, None):
        notes.append(f"The rows that existed before the pass went from {before:,} to {after:,}; a "
                     "harvest rewriting a row at the same moment does that. Run it again to "
                     "confirm.")
    tail = (" " + " ".join(notes)) if notes else ""
    if not filled and not timed:
        return {**base, "ok": True, "partial": bool(notes), "summary": summary,
                "sentence": f"Nothing to record: {plural(scanned, 'transcript')} scanned and no "
                            f"tool call was missing an outcome or a result time ({took}).{tail}",
                "short": "Nothing to record."}
    return {**base, "ok": True, "partial": bool(notes), "summary": summary,
            "sentence": f"Recorded the outcome of {plural(filled, 'tool call')} and the result "
                        f"time of {timed:,}, from {plural(scanned, 'transcript')}; "
                        f"{plural(still, 'call')} still without an outcome ({took}).{tail}",
            "short": (f"Recorded{' in part' if notes else ''}: {plural(filled, 'outcome')}, "
                      f"{plural(timed, 'result time')}.")}


def _runs_report(base: Report, raw: dict[str, Any], dry_run: bool, took: str,
                 failed: Callable[[str, str], Report]) -> Report:
    """`--backfill-runs`: headless one-shots folded under the chat that spawned them, or under
    the project above a batch of them. A dry run reports the same numbers and writes nothing, and
    the page puts them in the question it asks before the real one.

    `wrote` MUST SAY WHAT WAS ASKED. A dry run that reports it wrote, or a real one that reports
    it did not, is a harvester this build does not understand, and neither is called a success.
    """
    if (raw.get("wrote") is True) == dry_run:
        return failed("The fold reported the opposite of what was asked (a dry run that wrote, or "
                      "a run that did not), so its numbers are not trusted. Nothing else was done.",
                      "Update failed: the fold did not do what was asked.")
    linked, batched = _count(raw.get("linked")), _count(raw.get("batched"))
    unlinked, misses = _count(raw.get("unlinked")), _count(raw.get("misses"))
    # WHAT CAN BE PLACED NOWHERE IS TWO NUMBERS. `misses` counts only the misses this pass
    # recorded; a run it already knew it could not place, whose circumstances have not changed,
    # is skipped and counted as `unchanged`. Quoting `misses` alone said "0 can be placed
    # nowhere" about a store with 271 of them, on the first live run of this.
    unplaced = misses + _count(raw.get("unchanged"))
    heads, projects = _count(raw.get("heads")), _count(raw.get("projects"))
    open_spans = _count(raw.get("calls_without_result_ts"))
    by_how = raw.get("by_how") if isinstance(raw.get("by_how"), dict) else {}
    summary = {"one_shots": _count(raw.get("one_shots")), "already": _count(raw.get("already")),
               "unchanged": _count(raw.get("unchanged")), "unlinked": unlinked, "linked": linked,
               "batched": batched, "misses": misses, "unplaced": unplaced, "heads": heads,
               "projects": projects,
               "links_before": _count(raw.get("links_before")),
               "links_after": _count(raw.get("links_after")),
               "by_how": {str(k): _count(v) for k, v in (by_how or {}).items()},
               "calls_without_result_ts": open_spans, "outcomes_first": open_spans > 0}
    loose = (f" {plural(open_spans, 'shell call')} carry no result time, so some spans are loose: "
             "record tool outcomes first, because a link made on a loose span stays."
             if open_spans else "")
    changes = linked + batched + unlinked
    if dry_run:
        if not changes:
            text = (f"Nothing to fold: none of {plural(summary['one_shots'], 'one-shot run')} "
                    f"would change ({unplaced:,} can be placed nowhere). Nothing was written.")
            return {**base, "ok": True, "summary": summary, "sentence": text + loose,
                    "short": "Nothing to fold."}
        return {**base, "ok": True, "summary": summary,
                "sentence": f"Would fold {plural(linked, 'run')} under the "
                            f"{plural(heads, 'chat')} that spawned them and {batched:,} under "
                            f"{plural(projects, 'project')}; {plural(unlinked, 'existing link')} "
                            f"would be dropped and {unplaced:,} can be placed nowhere. Nothing was "
                            f"written.{loose}",
                "short": f"Would fold {plural(linked + batched, 'run')}."}
    if not changes:
        return {**base, "ok": True, "summary": summary,
                "sentence": f"Nothing to fold: all {plural(summary['one_shots'], 'one-shot run')} "
                            f"were already placed or can be placed nowhere ({took}).{loose}",
                "short": "Nothing to fold."}
    return {**base, "ok": True, "summary": summary,
            "sentence": f"Folded {plural(linked, 'run')} under their chats and {batched:,} under "
                        f"their projects; run links went from {summary['links_before']:,} to "
                        f"{summary['links_after']:,} ({took}).{loose}",
            "short": f"Folded {plural(linked + batched, 'run')}."}


# ---------------------------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------------------------

def _default_run(args, **kw):
    """`proc.run`, looked up at call time (`c4x/proc.py` says why). `tests/conftest.py` replaces
    this name with a function that raises, so a test that forgot to pass its own runner fails
    loudly instead of reading this machine's transcripts."""
    return proc.run(args, **kw)


def _default_spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="c4x-harvest", daemon=True).start()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run_once(kind: str, dry_run: bool = False, *, run: Callable[..., Any] | None = None,
             sleep: Callable[[float], None] | None = None,
             clock: Callable[[], float] | None = None,
             stamp: Callable[[], bool] | None = None, node: str | None = None,
             root: Path | str | None = None,
             env: Mapping[str, str] | None = None) -> Report:
    """Run the harvester once (twice, when the first try found the store held) and say what came
    of it. Never raises for anything the child did: every outcome is a `Report`."""
    runner = _default_run if run is None else run
    pause = time.sleep if sleep is None else sleep
    tick = time.monotonic if clock is None else clock
    base = Path(store.ROOT if root is None else root)
    argv = argv_for(kind, dry_run, node=node, root=base)
    if stamp is None:
        write_stamp(base)
    else:
        stamp()
    attempts = 0
    started = tick()
    while True:
        attempts += 1
        try:
            done = runner(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(base), env=child_env(env),
                          timeout=TIMEOUT_S[kind])
        except proc.TimeoutExpired:
            return summarise(kind, dry_run, None, exit_code=None, seconds=tick() - started,
                             timed_out=True, attempts=attempts)
        except OSError as exc:
            return summarise(kind, dry_run, None, exit_code=None, seconds=tick() - started,
                             spawn_error=str(exc), attempts=attempts, node=argv[0])
        code = int(done.returncode)
        out, err = str(done.stdout or ""), str(done.stderr or "")
        # UTF-8, SAID ABOVE, because `text=True` alone decodes as the ANSI code page on Windows
        # and the report carries folder names: one non-ASCII name would turn a good run into a
        # report this cannot parse.
        raw = read_report(out, kind)
        busy = raw is None and code != 0 and bool(BUSY.search(err))
        if busy and kind == "incremental" and attempts < 2:
            pause(RETRY_AFTER_S)
            continue
        return summarise(kind, dry_run, raw, exit_code=code, stderr=err,
                         seconds=tick() - started, attempts=attempts)


class Jobs:
    """The one job this process may be running, and the last one that finished.

    `last` is kept for the life of the process, so a page reloaded at any moment finds the result
    of the run it started; after a server restart it is gone, and `harvest_runs` is the durable
    record. No queue and no cancel: Stop C4X and Restart C4X already end the child.
    """

    def __init__(self, boot: str | None = None) -> None:
        # THE ID NAMES THE PROCESS TOO. The page calls a run a success only when the server names
        # the same id back, and a counter alone restarts at 1 with every server: a run interrupted
        # by Restart C4X and a later, unrelated run on the new server would both be job 1.
        self._boot = uuid.uuid4().hex[:8] if boot is None else boot
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._seq = 0
        self._started = 0.0
        self.current: Job | None = None
        self.last: Job | None = None

    def start(self, kind: str = "incremental", dry_run: bool = False, *,
              run: Callable[..., Any] | None = None,
              spawn: Callable[[Callable[[], None]], None] | None = None,
              invalidate: Callable[[], Any] | None = None,
              after: Callable[[], Any] | None = None,
              capability_of: Callable[[], Capability] | None = None,
              clock: Callable[[], float] | None = None,
              sleep: Callable[[float], None] | None = None,
              stamp: Callable[[], bool] | None = None,
              now: Callable[[], str] | None = None) -> Job:
        """Start a job and return it as it stood when it started. `Disabled` when this server
        will not harvest, `Busy` when one is running, `ValueError` for a job that does not exist.
        """
        if (kind, dry_run) not in FLAGS:
            raise ValueError(f"no such job: {kind}{' (dry run)' if dry_run else ''}")
        allowed = (capability if capability_of is None else capability_of)()
        if not allowed["enabled"]:
            raise Disabled(allowed)
        if kind != "incremental" and not Path(allowed["own"]).exists():
            raise NoStore("There is no store yet; run Update data first.")
        if not self._lock.acquire(blocking=False):
            running = self.snapshot()
            # The worker clears `current` a moment before it frees the lock. Nothing named means
            # the job that held it has just ended, so this is not busy: take the lock it is about
            # to free rather than answer 409 about a job nobody can point at.
            if running is not None or not self._lock.acquire(timeout=2.0):
                raise Busy(running)
        # WHOEVER TOOK THE LOCK FREES IT, ON EVERY PATH, AND ONLY ITS OWN. `freed` is this call's
        # claim on it. The first version asked `self._lock.locked()` on the way out, which is a
        # question about the lock and not about this call: it could free one a later job had
        # taken. And a clock or a stamp that raised before the spawn left it held for good.
        freed = {"yes": False}

        def free() -> None:
            if not freed["yes"]:
                freed["yes"] = True
                self._lock.release()

        try:
            tick = time.monotonic if clock is None else clock
            stamp_time = _utc_now if now is None else now
            with self._state:
                self._seq += 1
                job: Job = {"id": f"{self._boot}-{self._seq}", "kind": kind, "dry_run": dry_run,
                            "state": "running", "started_at": stamp_time(),
                            "finished_at": None, "report": None}
                self._started = tick()
                self.current = job
        except BaseException:
            free()
            raise
        began: Job = {**job}

        def work() -> None:
            try:
                try:
                    report = run_once(kind, dry_run, run=run, sleep=sleep, clock=clock,
                                      stamp=stamp)
                except Exception as exc:  # noqa: BLE001 - a bug here must not wedge the lock
                    report = summarise(kind, dry_run, None, exit_code=None)
                    text = f"The update failed inside the server: {str(exc)[:ERROR_MAX]}"
                    report = {**report, "sentence": text, "error": text,
                              "short": "Update failed: a fault in the server."}
                # INVALIDATE WHATEVER THE OUTCOME. A run that failed half way has still stored
                # what it committed, and `store` would go on answering from before it for 45 s.
                for step in (store.invalidate if invalidate is None else invalidate, after):
                    if step is None:
                        continue
                    try:
                        step()
                    except Exception as exc:  # noqa: BLE001 - the report must still be kept
                        report = {**report, "error": (report["error"] or "")
                                  + f" (and the caches were not cleared: {str(exc)[:80]})"}
                with self._state:
                    self.last = {**job, "state": "done", "finished_at": stamp_time(),
                                 "report": report}
            finally:
                # Whatever ended the work, this job is no longer the running one.
                with self._state:
                    if self.current is job:
                        self.current = None
                free()

        try:
            (_default_spawn if spawn is None else spawn)(work)
        except BaseException:
            # The work never ran, so nothing else will free the lock: a thread that could not
            # start would otherwise answer 409 to every click until the server was restarted.
            # (`free` does nothing when the work did run and has freed it already.)
            with self._state:
                if self.current is job:
                    self.current = None
            free()
            raise
        return began

    def snapshot(self, clock: Callable[[], float] | None = None) -> dict[str, Any] | None:
        """The running job with how long it has run, or None."""
        tick = time.monotonic if clock is None else clock
        with self._state:
            if self.current is None:
                return None
            return {"id": self.current["id"], "kind": self.current["kind"],
                    "dry_run": self.current["dry_run"],
                    "started_at": self.current["started_at"],
                    "elapsed_s": round(max(0.0, tick() - self._started), 1)}

    def in_progress(self) -> bool:
        with self._state:
            return self.current is not None

    def finished(self) -> dict[str, Any] | None:
        """The last finished job, flat: its report's fields beside its id and times."""
        with self._state:
            if self.last is None:
                return None
            report: dict[str, Any] = dict(self.last["report"] or {})
            return {"id": self.last["id"], "started_at": self.last["started_at"],
                    "finished_at": self.last["finished_at"], **report}


JOBS = Jobs()


def start(kind: str = "incremental", dry_run: bool = False, **kw: Any) -> Job:
    return JOBS.start(kind, dry_run, **kw)


def in_progress() -> bool:
    """True while a job runs. The watchdog reads it (`c4x/api/__main__.py`), so a long catch-up
    is not stopped a minute after the last Claude window closes."""
    return JOBS.in_progress()


def status(*, jobs: Jobs | None = None, last_run: Callable[[], dict | None] | None = None,
           capability_of: Callable[[], Capability] | None = None,
           clock: Callable[[], float] | None = None) -> dict[str, Any]:
    """What `GET /api/store/harvest` answers. Runs nothing, whoever asks."""
    book = JOBS if jobs is None else jobs
    allowed = (capability if capability_of is None else capability_of)()
    try:
        newest = (store.last_harvest if last_run is None else last_run)()
    except Exception:  # noqa: BLE001 - a status that cannot say how fresh the store is still answers
        newest = None
    running = book.snapshot(clock)
    return {**allowed, "kinds": list(KINDS), "running": running is not None, "job": running,
            "last": book.finished(), "last_harvest": newest}

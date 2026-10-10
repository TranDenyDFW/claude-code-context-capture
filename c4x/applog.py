r"""Did the desktop app REFUSE to write a chat record? Only its own log says so.

WHY THIS MODULE EXISTS. `accounts.py` shares every account's chats by pointing eight of nine
`<account>/<org>` directories at the ninth with a junction, and its `verify()` checks that those
links still land where they were asked to. That is the READ side. Measured on this machine on
2026-10-10, build 2.26454.0.0 refuses the WRITE side: every save into a junctioned pair fails with

    Failed to save session local_<uuid>: Refusing non-directory at private dir path
    (symlink/file plant): <records root>\<account>\<org> { name: 'PlantDetectedError', ... }

thrown from `ensureStorageDir` on the app's `writeSessionToDisk` path. 2,843 of those lines sit in
`%LOCALAPPDATA%\Claude\Logs\main.log`, across 16 days from 2026-09-14, naming three junctioned
pairs and never the real one, for 73 distinct sessions. Nothing reaches the interface: the chat
list loads through a junction perfectly well, so an archive, a rename or a brand new chat is
accepted on screen and silently dropped. The user's report that started this was one chat archived
twice, once per account, which is the same failure seen from the outside.

WHAT IT CANNOT DO. A log is evidence of the past. A clean log proves that nothing has been refused
since the file was last rotated, never that the next write will land, so a caller that must not
make things worse treats any refusal as a reason to stop rather than a count to compare against.
"""
import os
import re
import sys
from pathlib import Path
from typing import Any

# The message the app logs when it will not write into a directory that is a reparse point. The
# path is captured up to the object that follows it, because the same path appears a second time
# inside the escaped stack on the very same line and a count of matches would be double.
REFUSED = re.compile(
    r"Refusing non-directory at private dir path \(symlink/file plant\): (.+?)(?:\s\{|$)")
SESSION = re.compile(r"Failed to save session (local_[0-9a-f][0-9a-f-]*)")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
PAIRS_DIR = "claude-code-sessions"
UNKNOWN = "unknown"


def _default_candidates() -> list:
    """Every directory this build, or the one before it, writes its log into.

    THE LOCATION MOVED. Until 2026-08-20 the Store build logged inside its own package, under
    `<app data>\\logs`; today's build logs to `%LOCALAPPDATA%\\Claude\\Logs`, and the older files
    are still there with their own history in them. Both are offered, newest first, and a caller
    that wants neither passes its own list.
    """
    out: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Claude" / "Logs")
    try:
        from c4x import store
        for appdata in store._claude_appdata_candidates():
            out.append(Path(appdata) / "logs")
    except Exception:                        # the store is not importable in a bare fixture run
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Claude" / "logs")
    seen: set[str] = set()
    kept: list[Path] = []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            kept.append(p)
    return kept


def log_dirs(candidates=None, exists=None) -> list:
    """The candidate log directories that are actually there, newest location first."""
    isdir = os.path.isdir if exists is None else exists
    return [Path(p) for p in (_default_candidates() if candidates is None else candidates)
            if isdir(str(p))]


def log_files(dirs=None, glob=None) -> list:
    """Every `.log` file in those directories. Sorted, so a report reads the same twice."""
    found: list[Path] = []
    for d in (log_dirs() if dirs is None else [Path(x) for x in dirs]):
        listing = sorted(Path(d).glob("*.log")) if glob is None else [Path(x) for x in glob(d)]
        found += listing
    return found


def pair_of(path: str) -> str:
    r"""`<account>\<org>` out of a refused path, or `unknown`.

    The path names the pair directory the app would not write into. Only the two components under
    `claude-code-sessions` identify it, and the rest of the path is this machine's spelling (the
    Store build's virtual `%APPDATA%` or the package's own `LocalCache`), which says nothing about
    which account was refused.
    """
    parts = [p for p in re.split(r"[\\/]+", str(path).strip().strip('"')) if p]
    low = [p.lower() for p in parts]
    if PAIRS_DIR in low:
        i = low.index(PAIRS_DIR)
        rest = parts[i + 1:i + 3]
        if len(rest) == 2:
            return "\\".join(rest)
    return UNKNOWN


def refusals(lines) -> dict:
    """Count refused record writes in an iterable of log lines, by pair, by day and by session.

    ONE LINE IS ONE REFUSAL, counted on the message and never on the stack beneath it: the app logs
    the error object inline, with the same path escaped inside it, so a pattern applied to the whole
    text answers twice per failure. Returns zeroes for a log that holds none, which is the answer a
    machine that was never shared should give.
    """
    out: dict[str, Any] = {"count": 0, "pairs": {}, "days": {}, "sessions": [],
                           "first": None, "last": None}
    sessions: dict[str, int] = {}
    for line in lines:
        hit = REFUSED.search(line)
        if hit is None:
            continue
        out["count"] += 1
        pair = pair_of(hit.group(1))
        out["pairs"][pair] = out["pairs"].get(pair, 0) + 1
        stamp = STAMP.match(line)
        day = stamp.group(1) if stamp else UNKNOWN
        out["days"][day] = out["days"].get(day, 0) + 1
        if stamp:
            when = f"{stamp.group(1)} {stamp.group(2)}"
            out["first"] = when if out["first"] is None or when < out["first"] else out["first"]
            out["last"] = when if out["last"] is None or when > out["last"] else out["last"]
        said = SESSION.search(line)
        if said:
            sessions[said.group(1)] = sessions.get(said.group(1), 0) + 1
    out["sessions"] = sorted(sessions)
    out["session_counts"] = sessions
    return out


def read_lines(path):
    """Stream a log file, never buffering it: these grow to tens of megabytes."""
    with open(str(path), encoding="utf-8", errors="replace") as fh:
        yield from fh


def scan(paths=None, read=None) -> dict:
    """Refusals across every log file, with the files that were read named in the report."""
    reader = read_lines if read is None else read
    files = log_files() if paths is None else [Path(p) for p in paths]
    total = refusals([])
    total["files"] = []
    for f in files:
        try:
            one = refusals(reader(f))
        except OSError as exc:
            total["files"].append({"path": str(f), "error": str(exc)})
            continue
        total["files"].append({"path": str(f), "count": one["count"]})
        total["count"] += one["count"]
        for key in ("pairs", "days", "session_counts"):
            for name, n in one[key].items():
                total[key][name] = total[key].get(name, 0) + n
        for edge, pick in (("first", min), ("last", max)):
            seen = [v for v in (total[edge], one[edge]) if v]
            total[edge] = pick(seen) if seen else None
    total["sessions"] = sorted(total["session_counts"])
    return total


MAPPED = re.compile(
    r"Mapping internal session (local_[0-9a-f][0-9a-f-]*) to CLI session ([0-9a-f][0-9a-f-]*)")


def mappings(lines) -> dict:
    """`local_<uuid>` to the CLI session uuid the app paired it with, from its own log.

    WHY IT IS THE ONLY SOURCE for a refused record: the record file is what ties a desktop id to a
    transcript, and a refused write means that file was never created. The app logs the pairing
    anyway ("Mapping internal session ... to CLI session ..."), so a chat whose record never landed
    can still be traced to the transcript it was running. The two uuids are usually equal, which is
    worth not assuming: a forked session carries a new desktop id for an existing transcript.
    """
    out: dict[str, str] = {}
    for line in lines:
        hit = MAPPED.search(line)
        if hit:
            out[hit.group(1)] = hit.group(2)
    return out


def trace(paths=None, read=None) -> dict:
    """The mapping over every log file, newest last so a later pairing wins."""
    reader = read_lines if read is None else read
    files = log_files() if paths is None else [Path(p) for p in paths]
    out: dict[str, str] = {}
    for f in files:
        try:
            out.update(mappings(reader(f)))
        except OSError:
            continue
    return out


def newest_log(files=None, mtime=None):
    """The log file written most recently, or None. The app rotates `main.log` to `main1.log`."""
    when = os.path.getmtime if mtime is None else mtime
    found = log_files() if files is None else [Path(f) for f in files]
    dated = []
    for f in found:
        try:
            dated.append((when(str(f)), f))
        except OSError:
            continue
    return max(dated)[1] if dated else None


def tail_lines(path, limit=2_000_000, read=None):
    """The last `limit` bytes of a log, as whole lines, never the whole file.

    THESE REACH TENS OF MEGABYTES and the page asks on every load, so a full read is the wrong
    shape here (the house rule against buffering a large file says the same). The first line of the
    window is dropped when the window starts mid line, so a half line cannot be parsed as a record.
    """
    if read is not None:
        return read(path, limit)
    size = os.path.getsize(str(path))
    with open(str(path), "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[1:] if size > limit and lines else lines


def recent(limit=2_000_000, newest=None, tail=None) -> dict:
    """Refusals in the newest log's tail: the cheap question, for a page that asks on every load."""
    pick = newest_log if newest is None else newest
    path = pick() if callable(pick) else pick
    if path is None:
        out = refusals([])
        out["file"] = None
        return out
    reader = tail_lines if tail is None else tail
    out = refusals(reader(path, limit))
    out["file"] = str(path)
    return out


def sentence(report) -> str:
    """One line for a log, a report or the page: what was refused, where, and when."""
    if not report.get("count"):
        return "no refused record writes in the app's log"
    pairs = ", ".join(f"{name} {n}" for name, n in
                      sorted(report["pairs"].items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"{report['count']} refused record write(s) for {len(report['sessions'])} session(s), "
            f"{report['first']} to {report['last']}, by pair: {pairs}")


def main(argv=None):
    """`python -m c4x.applog [--json] [<log file> ...]`"""
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    paths = [a for a in argv if not a.startswith("-")] or None
    report = scan(paths)
    if as_json:
        import json
        print(json.dumps(report, indent=1))
    else:
        print(sentence(report))
        for f in report["files"]:
            print(f"  {f.get('count', f.get('error'))}  {f['path']}")
    return 0 if report["count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

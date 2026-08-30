"""Build a redacted COPY of the store, for screenshots that go in a public README.

The images in this README have to show real shape: real session lengths, real compaction overshoot,
real re-read counts, real cost. Those are what make the tool legible in one second. What they must
not show is the author's working directories, session titles, file names, skill names or message
text, because a README lives in a public repository and a screenshot is forever once it is in git
history.

A COPY, not a render-time flag. The alternative was a `--demo` switch applied wherever a path
becomes display text, which is a dozen call sites across the tabs plus every chart's hover
template, and missing one leaks. This is a single transformation over the store, and its result can
be checked: the verification pass below greps the copy for every real fragment it was supposed to
remove and fails if any survived. A flag scattered across the render path cannot be checked that
way, which is the whole argument.

    python tools/redact.py                       # data/context.db  ->  tmp/demo-store.db
    C4X_DB=tmp/demo-store.db python app.py       # the dashboard, over the copy
    python tools/screenshots.py                  # photograph it

The mapping is DETERMINISTIC and rank-ordered, so the same store always produces the same names and
the charts keep their shape: the biggest project is always `project-a`. Regenerating the images
after a UI change therefore produces images that differ only where the UI changed.

The original store is opened read-only and is never written to.
"""
import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT / "data" / "context.db"
DEFAULT_OUT = ROOT / "tmp" / "demo-store.db"

# A message preview is the one field that can carry conversation content, so it is replaced whole
# rather than transformed. The Session tab shows a `chars` column beside it, and that stays real,
# so the table still demonstrates what it is for.
MESSAGE_STAND_IN = ("Redacted for the public screenshots. The sizes, counts and timings on this "
                    "page are real; the text is not.")


def letters(n):
    """a, b, ... z, aa, ab, ... Stable and unbounded, so a 300-project store does not run out."""
    out = ""
    while True:
        out = chr(ord("a") + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def build_maps(db):
    """The name each real value becomes, ordered by size so the charts keep their shape."""
    maps = {}

    projects = [r[0] for r in db.execute(
        """SELECT s.cwd FROM sessions s LEFT JOIN turns t ON t.session_id = s.session_id
            WHERE s.cwd IS NOT NULL AND s.cwd <> ''
            GROUP BY s.cwd ORDER BY COALESCE(SUM(t.total_resident), 0) DESC""")]
    maps["project"] = {real: f"project-{letters(i)}" for i, real in enumerate(projects)}

    servers = [r[0] for r in db.execute(
        """SELECT server_name FROM tool_calls WHERE server_name IS NOT NULL
            GROUP BY server_name ORDER BY COUNT(*) DESC""")]
    maps["server"] = {real: f"server-{i + 1}" for i, real in enumerate(servers)}

    # Targets keep their EXTENSION and their position in the ranking. A reader looking at the Cost
    # tab should still see that the worst offender is a .json read 614 times, because that is the
    # finding; which json it was is not.
    targets = [r[0] for r in db.execute(
        """SELECT target FROM tool_calls WHERE target IS NOT NULL AND target <> ''
            GROUP BY target ORDER BY COUNT(*) DESC""")]
    maps["target"] = {}
    for i, real in enumerate(targets):
        ext = Path(str(real).replace("\\", "/")).suffix or ""
        maps["target"][real] = f"/work/project-a/file-{i + 1}{ext}"

    items = [r[0] for r in db.execute(
        """SELECT DISTINCT name FROM probe_details WHERE name IS NOT NULL""")]
    maps["item"] = {real: f"item-{i + 1}" for i, real in enumerate(items)}

    titles = [r[0] for r in db.execute(
        """SELECT DISTINCT title FROM session_titles WHERE title IS NOT NULL""")]
    maps["title"] = {real: f"Working session {i + 1}" for i, real in enumerate(titles)}
    return maps


def apply(db, maps):
    """Rewrite every column that can carry a real name. Reports what it touched."""
    touched = {}

    def rewrite(table, column, mapping):
        """One UPDATE joined against a temp mapping table, not one UPDATE per distinct value.

        The target mapping has 181,000 entries in this store. Issued as individual statements that
        is 181,000 index lookups and it does not finish in ten minutes; joined against a temp table
        it is one pass. The first version of this tool was written the slow way and had to be
        killed, which is the only reason this comment exists.
        """
        if not mapping:
            touched[f"{table}.{column}"] = 0
            return 0
        db.execute("DROP TABLE IF EXISTS _redact_map")
        db.execute("CREATE TEMP TABLE _redact_map (real TEXT PRIMARY KEY, fake TEXT)")
        db.executemany("INSERT OR REPLACE INTO _redact_map VALUES (?, ?)", list(mapping.items()))
        cur = db.execute(
            f"UPDATE {table} SET {column} = "
            f"  COALESCE((SELECT fake FROM _redact_map WHERE real = {table}.{column}), {column}) "
            f"WHERE {column} IN (SELECT real FROM _redact_map)")
        db.execute("DROP TABLE IF EXISTS _redact_map")
        touched[f"{table}.{column}"] = cur.rowcount
        return cur.rowcount

    rewrite("sessions", "cwd", maps["project"])
    rewrite("sessions", "project_slug", dict(maps["project"]))
    rewrite("session_titles", "title", maps["title"])
    rewrite("tool_calls", "server_name", maps["server"])
    rewrite("tool_calls", "target", maps["target"])
    rewrite("probe_details", "name", maps["item"])

    # Free-text and path columns with no small distinct set. Replaced wholesale rather than mapped,
    # because a mapping over 275,000 distinct message bodies is neither possible nor useful.
    touched["messages.text"] = db.execute(
        "UPDATE messages SET text = ? WHERE text IS NOT NULL", (MESSAGE_STAND_IN,)).rowcount
    touched["messages.preview"] = db.execute(
        "UPDATE messages SET preview = ? WHERE preview IS NOT NULL",
        (MESSAGE_STAND_IN[:120],)).rowcount if _has(db, "messages", "preview") else 0
    # Distinct per row, not one shared value: `files.path` is a UNIQUE key, so collapsing every
    # row onto one string fails the constraint. Deriving the name from the rowid also keeps the
    # file COUNT honest, which is a figure the Diagnostics tab reports.
    for table, column in (("turns", "file_path"), ("tool_calls", "file_path"),
                          ("messages", "file_path"), ("files", "path"),
                          ("compactions", "file_path")):
        if _has(db, table, column):
            touched[f"{table}.{column}"] = db.execute(
                f"UPDATE {table} SET {column} = "
                f"'/work/transcripts/session-' || rowid || '.jsonl' "
                f"WHERE {column} IS NOT NULL").rowcount
    # An MCP tool is named mcp__<server>__<tool>, so a tool name spells out which servers are
    # installed. Mapped through the same server numbering the Cost tab uses, so the two tabs stay
    # consistent with each other in the screenshots.
    for table in ("hook_events", "tool_calls"):
        if not _has(db, table, "tool_name"):
            continue
        mcp = {}
        for (name,) in db.execute(
                f"SELECT DISTINCT tool_name FROM {table} "
                f"WHERE tool_name LIKE 'mcp!_!_%' ESCAPE '!'"):
            parts = str(name).split("__")
            if len(parts) >= 3:
                mcp[name] = f"mcp__{maps['server'].get(parts[1], 'server-x')}__{parts[2]}"
        if mcp:
            touched[f"{table}.tool_name"] = _map_update(db, table, "tool_name", mcp)

    for table, column in (("probe_details", "extra"), ("compactions", "discovered_tools_json"),
                          ("compactions", "preserved_json"), ("hook_events", "extra")):
        if _has(db, table, column):
            touched[f"{table}.{column}"] = db.execute(
                f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL").rowcount

    # THE SWEEP, and the reason this tool can be trusted as the schema grows.
    #
    # Everything above names a column by hand, and a hand-written list is exactly as complete as
    # the day it was written. The first run of this tool redacted eight columns and its own gate
    # then found ninety-one more still holding real paths, in tables nobody thought about:
    # hook_events.cwd, transcript_path, extra. Adding those three by name would have fixed that
    # run and left the next new column to leak in silence.
    #
    # So instead: every text column in every table is swept for anything that LOOKS like a local
    # path or carries the user name, and rewritten. A column added next year is covered without
    # anyone remembering this file exists.
    touched.update(sweep(db, maps))
    return touched


# A Windows drive path, a UNC share, a POSIX home, or a bare user directory. Deliberately broad:
# a false positive costs one redacted string in a screenshot, a false negative costs a leak.
PATHISH = re.compile(
    r"[A-Za-z]:[\\/]"          # C:\ or C:/
    r"|\\\\[^\\]+\\"           # \\server\share
    r"|/(?:home|Users)/",      # /home/you or /Users/you
    re.I)


def sweep(db, maps):
    """Rewrite any remaining value that looks like a local path, column by column."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    project_map = {str(k): v for k, v in maps["project"].items()}
    touched = {}
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
    for table in tables:
        if table.startswith("sqlite_"):
            continue
        cols = [(c, k) for _i, c, k, *_r in db.execute(f"PRAGMA table_info({table})")]
        for column, kind in cols:
            if str(kind).upper() in ("INTEGER", "REAL", "NUMERIC", "BLOB"):
                continue
            try:
                values = [v for (v,) in db.execute(
                    f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL "
                    f"LIMIT 200000") if isinstance(v, str)]
            except sqlite3.Error:
                continue
            changes = {}
            for value in values:
                if not PATHISH.search(value) and not (user and user in value):
                    continue
                # The WHOLE value, not a prefix substitution.
                #
                # Replacing only the known project prefix looked tidier and leaked: a transcript
                # path became "/work/project-qb/.claude/projects/P--ClaudeExt-ExtIndex/..." where
                # the tail still spells out the real project. Anywhere a path's SHAPE matters for
                # a screenshot has a targeted map applied above this; everything reaching the
                # sweep is incidental, so it is replaced outright.
                stem = None
                for real, name in sorted(project_map.items(), key=lambda kv: -len(kv[0])):
                    if value.startswith(real):
                        stem = name
                        break
                suffix = ".jsonl" if value.endswith(".jsonl") else ""
                changes[value] = (f"/work/{stem or 'redacted'}/"
                                  f"{abs(hash(value)) % 100000}{suffix}")
            if changes:
                touched[f"{table}.{column} (swept)"] = _map_update(db, table, column, changes)
    return touched


def _map_update(db, table, column, mapping):
    db.execute("DROP TABLE IF EXISTS _sweep_map")
    db.execute("CREATE TEMP TABLE _sweep_map (real TEXT PRIMARY KEY, fake TEXT)")
    db.executemany("INSERT OR REPLACE INTO _sweep_map VALUES (?, ?)", list(mapping.items()))
    cur = db.execute(
        f"UPDATE {table} SET {column} = "
        f"  COALESCE((SELECT fake FROM _sweep_map WHERE real = {table}.{column}), {column}) "
        f"WHERE {column} IN (SELECT real FROM _sweep_map)")
    db.execute("DROP TABLE IF EXISTS _sweep_map")
    return cur.rowcount


def _has(db, table, column):
    return any(r[1] == column for r in db.execute(f"PRAGMA table_info({table})"))


def leaks(db, needles):
    """Every place a real fragment survived. This is the gate, not a courtesy check.

    A redaction nobody verified is a redaction that half worked, and the half that did not is
    invisible until the image is already public.
    """
    found = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
    # Case-EXACT. Lowercasing both sides matched a five-letter user name inside an opaque request
    # id (`req_011CeKdqShaKeb5jtPMmTmzU` contains "ShaKe") and reported it as a leak. A gate that
    # cries wolf on random identifiers is a gate that gets ignored, and the values being checked
    # all come from one source, so their case is consistent.
    checks = [n for n in needles if n]
    for table in tables:
        for _cid, column, kind, *_rest in db.execute(f"PRAGMA table_info({table})"):
            # Integer and real columns cannot hold a path. Skipping them is not an optimisation
            # for its own sake: scanning every column of every table against every needle is what
            # made the first version of this pass unusable.
            if str(kind).upper() in ("INTEGER", "REAL", "NUMERIC", "BLOB"):
                continue
            try:
                values = db.execute(
                    f"SELECT DISTINCT {column} FROM {table} "
                    f"WHERE {column} IS NOT NULL LIMIT 200000").fetchall()
            except sqlite3.Error:
                continue
            # Tested in Python over DISTINCT values rather than as SQL LIKE over every row. The
            # message table has 275,000 rows and, after redaction, one distinct body.
            seen = {}
            for (value,) in values:
                text = str(value)
                for needle in checks:
                    if needle in text:
                        seen[needle] = seen.get(needle, 0) + 1
            for needle, hits in seen.items():
                found.append((table, column, needle, hits))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="store to copy FROM, read-only")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="redacted copy to write")
    ap.add_argument("--needles", default="",
                    help="extra comma-separated fragments the copy must not contain")
    args = ap.parse_args(argv)

    src, out = Path(args.src), Path(args.out)
    if not src.exists():
        print(f"no store at {src}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() == src.resolve():
        print("refusing to redact the store in place; --out must be a different file")
        return 2

    # Copied through sqlite's own backup API rather than by copying bytes, so a store being
    # written to right now yields a consistent snapshot instead of a torn one.
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    if out.exists():
        out.unlink()
    target = sqlite3.connect(str(out))
    source.backup(target)
    source.close()

    maps = build_maps(target)
    touched = apply(target, maps)
    target.commit()

    # What must not survive: every real project path, every drive letter seen in one, the user
    # name, and anything the caller added. Derived from the mapping rather than hard-coded, so a
    # store from another machine is checked against ITS values.
    needles = set()
    for real in maps["project"]:
        needles.add(str(real))
        head = re.match(r"^[A-Za-z]:[\\/]+[^\\/]+", str(real))
        if head:
            needles.add(head.group(0))
    # Long enough to be identifying. A title of "status" or a server called "git" matches
    # unrelated words in every table and turns the gate into noise, which is how a gate stops
    # being read.
    for real in list(maps["server"]) + list(maps["title"]):
        if real and len(str(real)) > 12:
            needles.add(str(real))
    if os.environ.get("USERNAME"):
        needles.add(os.environ["USERNAME"])
    needles |= {n.strip() for n in args.needles.split(",") if n.strip()}

    surviving = leaks(target, sorted(needles))
    target.close()

    for name, n in sorted(touched.items()):
        if n:
            print(f"  {name:34} {n:>9,} rows")
    if surviving:
        print(f"\nLEAK: {len(surviving)} column(s) still contain something real:")
        for table, column, needle, hits in surviving[:12]:
            print(f"  {table}.{column} contains {needle!r} in {hits:,} rows")
        print("\nthe copy was written but MUST NOT be photographed")
        return 1
    print(f"\nclean: no real project path, server, title or user name survives in {out}")
    print(f"  C4X_DB={out} python app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

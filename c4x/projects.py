"""Move a whole project in and out of the store: export, import, delete, and stop capturing it.

    python -m c4x.projects list
    python -m c4x.projects export "P:\\ClaudeExt\\QuestionExtension" --out tmp/qe.db
    python -m c4x.projects import tmp/qe.db
    python -m c4x.projects delete "P:\\ClaudeExt\\QuestionExtension" --confirm "P:\\..."
    python -m c4x.projects excluded
    python -m c4x.projects include "P:\\ClaudeExt\\QuestionExtension"
    python -m c4x.projects --self-test

A PROJECT IS A WORKING DIRECTORY, which is what `session_rows()` reports as `project` and what the
`project::<path>` cohort selects. It is not a stored entity: there is no projects table, only
sessions that share a `cwd`.

WHAT MAKES A PROJECT SEPARABLE. Every row that belongs to one is reachable from its session ids,
except `compaction_survivors`, which hangs off `compactions.uuid`, and the harvest offset in
`files`, which is keyed by `sessions.transcript_path`. There are no foreign keys in this schema, so
nothing cascades and every table is named explicitly below. That is more code and less to go wrong:
a table added later is absent from the list and is REPORTED as unhandled rather than silently left
behind.

WHAT IS NOT PART OF A PROJECT: `probes`, `probe_*`, `context_baselines`, `record_types` and
`harvest_runs`. Those describe the machine and the capture rather than any project, which is the
same reason the Diagnostics tab ignores the header selection.

DELETING IS NOT ENOUGH ON ITS OWN. `tools/harvest.mjs` scans `~/.claude/projects` and resumes each
transcript from `files.bytes_read`, so deleting the rows means the next harvest puts them back:
completely if the offset row went too, partially if it stayed and the transcript has since grown.
A delete that means it has to be recorded where harvest will see it, which is `excluded_projects`.
"""
import argparse
import hashlib
import json
import platform
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every table that belongs to a project, and HOW it is reached. Order matters for deletion: a row
# is removed before the row it points at, so a half-finished delete cannot orphan anything.
#
# `files` is here because the harvest offset is part of the project's footprint: leave it and the
# transcript is skipped forever; remove it and the next harvest re-reads from the beginning.
BY_SESSION = ("hook_events", "attachments", "tool_calls", "messages", "turns",
              "session_titles", "compactions", "sessions")

# Reached another way, and named so nothing depends on remembering it.
BY_COMPACTION = ("compaction_survivors",)
BY_TRANSCRIPT = ("files",)

# Deliberately NOT project data. Listed so that "is every table accounted for?" is a question this
# module can answer rather than a thing to check by eye.
STORE_WIDE = ("probes", "probe_categories", "probe_details", "probe_message_breakdown",
              "context_baselines", "record_types", "harvest_runs", "excluded_projects",
              "sqlite_sequence")

MANIFEST_TABLE = "c4x_export"


# ---------------------------------------------------------------------------
# Reading what a project is
# ---------------------------------------------------------------------------
def projects():
    """Every project in the store, with its session count. The cwd is the identity."""
    from c4x import store
    frame = store.q("""SELECT cwd AS project, COUNT(*) AS sessions
                         FROM sessions WHERE cwd IS NOT NULL
                        GROUP BY 1 ORDER BY 2 DESC""")
    return frame.to_dict("records")


def session_ids(con, project):
    """The sessions a project owns, as the PAGE resolves it, plus the ones the page cannot see.

    NOT `WHERE cwd = ?`, which is what this did first and what made it wrong. A project in this app
    is not a bare working directory: `store.session_rows()` appends `\\archived` to the label when
    the desktop app has archived the chat. Measured on this store, one session with cwd `P:\\Books`
    is shown, counted and selectable under `P:\\Books\\archived`, so keying on cwd exported four
    sessions for a cohort the header said held three, and would have deleted the archived one
    without ever naming it. In the other direction it was worse: NO cwd in this store literally
    ends in `\\archived`, so every archived cohort resolved to nothing and could not be exported at
    all.

    `ttl=0` because a 45-second-old answer would be a set from before the last import.

    THE SECOND HALF IS NOT REDUNDANT, and getting its condition wrong cost 79 sessions.

    `session_rows()` does not merely read FROM turns, it also drops any session with fewer than
    FIVE of them (`HAVING COUNT(*) >= 5`). Measured on this store that is 1,008 of 1,325 sessions,
    985 of which have between one and four turns. An earlier version of this asked for sessions
    with NO turns, so those 985 were in neither half: exporting one 137-session project carried 58
    of them and said nothing, and a delete would have left the other 79 behind with their project
    already marked excluded.

    So the test is "not in session_rows AT ALL", not "has no turns". A session the page cannot see
    under any label cannot have been attributed to another project by the archived rule, so its own
    cwd is the only evidence there is and it is safe to use.
    """
    from c4x import store
    # ONE uncached read, not two. `ttl=0` here refreshes the shared cache, so the `session_rows()`
    # below is served from what this call just put there. Asking for ttl=0 twice runs the GROUP BY
    # over every turn in the store a second time, which turned a sweep of 515 projects into
    # something that did not finish in ten minutes.
    ids = list(store.cohort_sessions(f"project::{project}", ttl=0))
    known = set(ids)
    seen = store.session_rows()
    visible = set(seen["session_id"]) if not seen.empty else set()
    unseen = con.execute("SELECT session_id FROM sessions WHERE cwd = ?", (project,)).fetchall()
    ids.extend(r[0] for r in unseen if r[0] not in known and r[0] not in visible)
    return ids


def footprint(con, project):
    """Row counts per table for one project, so a delete can be previewed before it happens."""
    ids = session_ids(con, project)
    out = {"sessions_selected": len(ids)}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for table in BY_SESSION:
        out[table] = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id IN ({marks})", ids).fetchone()[0]
    for table in BY_COMPACTION:
        out[table] = con.execute(
            f"""SELECT COUNT(*) FROM {table} WHERE compaction_uuid IN
                (SELECT uuid FROM compactions WHERE session_id IN ({marks}))""", ids).fetchone()[0]
    for table in BY_TRANSCRIPT:
        out[table] = con.execute(
            f"""SELECT COUNT(*) FROM {table} WHERE path IN
                (SELECT transcript_path FROM sessions WHERE session_id IN ({marks})
                  AND transcript_path IS NOT NULL)""", ids).fetchone()[0]
    return out


def unhandled_tables(con):
    """Tables this module knows nothing about.

    A schema gains a table and every function here keeps working while quietly ignoring it, which
    is how an export starts silently omitting something. Reported instead.
    """
    known = set(BY_SESSION) | set(BY_COMPACTION) | set(BY_TRANSCRIPT) | set(STORE_WIDE)
    known.add(MANIFEST_TABLE)
    live = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return sorted(live - known)


# ---------------------------------------------------------------------------
# The manifest, and the digest that makes it worth having
# ---------------------------------------------------------------------------
def digest(con, table):
    """A hash over a table's CONTENT, independent of how SQLite happened to store it.

    Not a sha256 of the file, for two reasons. The obvious one is that a hash of the file cannot
    live inside the file it describes, and the manifest is going in the export. The better one is
    that a file hash answers the wrong question: `VACUUM`, another SQLite build, or a differently
    ordered insert all change the bytes without changing a single value, so a file hash would report
    a corrupt export where there is none.

    Rows are sorted as text so the order they come back in cannot affect the result.
    """
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    if not cols:
        return None
    listed = ",".join(f'"{c}"' for c in cols)
    running = hashlib.sha256()
    rows = con.execute(f"SELECT {listed} FROM {table}").fetchall()
    for row in sorted(repr(tuple(r)) for r in rows):
        running.update(row.encode("utf-8"))
    return f"sha256:{running.hexdigest()}"


def read_manifest(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found = con.execute(
            f"SELECT value FROM {MANIFEST_TABLE} WHERE key = 'manifest'").fetchone()
        return json.loads(found[0]) if found else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def verify(path):
    """Recompute every digest in an export and compare it to what the export claims.

    Run on the file after writing it and again before importing it, so a truncated or edited export
    is caught rather than half-loaded.
    """
    manifest = read_manifest(path)
    if not manifest:
        return False, [f"{path} carries no {MANIFEST_TABLE} manifest, so it is not a c4x export"]
    problems = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, claimed in (manifest.get("digests") or {}).items():
            actual = digest(con, table)
            if actual != claimed:
                problems.append(f"{table}: manifest says {claimed}, file contains {actual}")
        for table, claimed in (manifest.get("counts") or {}).items():
            actual = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != claimed:
                problems.append(f"{table}: manifest says {claimed} rows, file has {actual}")
    except sqlite3.Error as exc:
        problems.append(f"{path} could not be read as a c4x export: {exc}")
    finally:
        con.close()
    return not problems, problems


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export(project, out_path):
    """Write one project to a standalone SQLite store, manifest inside it.

    A real database rather than a bundle of JSON: it round-trips exactly, opens in any SQLite tool,
    and imports with `ATTACH` instead of a parser. JSON would have to be trusted with NaN and with
    integer precision, which is where a numbers export usually goes wrong.
    """
    from c4x import store
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    source = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
    try:
        ids = session_ids(source, project)
        if not ids:
            raise ValueError(f"no sessions with cwd {project!r}")
        marks = ",".join("?" * len(ids))
        schema = source.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','view') AND sql IS NOT NULL"
        ).fetchall()

        out = sqlite3.connect(str(out_path))
        try:
            # The SAME schema, so the export is a store: every tool that reads one reads this.
            for (sql,) in schema:
                try:
                    out.execute(sql)
                except sqlite3.Error:
                    # A view over tables that are not carried is not worth failing an export for.
                    pass
            out.commit()

            source.execute("ATTACH DATABASE ? AS dest", (str(out_path),))
            counts = {}
            for table in BY_SESSION:
                source.execute(
                    f"INSERT INTO dest.{table} SELECT * FROM main.{table} "
                    f"WHERE session_id IN ({marks})", ids)
            for table in BY_COMPACTION:
                source.execute(
                    f"""INSERT INTO dest.{table} SELECT * FROM main.{table}
                        WHERE compaction_uuid IN (SELECT uuid FROM main.compactions
                        WHERE session_id IN ({marks}))""", ids)
            for table in BY_TRANSCRIPT:
                source.execute(
                    f"""INSERT INTO dest.{table} SELECT * FROM main.{table}
                        WHERE path IN (SELECT transcript_path FROM main.sessions
                        WHERE session_id IN ({marks}) AND transcript_path IS NOT NULL)""", ids)
            source.commit()
            source.execute("DETACH DATABASE dest")
        finally:
            out.close()

        # Counted and digested from the FILE, not from what was intended to be written. The point
        # of the manifest is to describe what is actually in there.
        out = sqlite3.connect(str(out_path))
        try:
            carried = BY_SESSION + BY_COMPACTION + BY_TRANSCRIPT
            counts = {t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in carried}
            digests = {t: digest(out, t) for t in carried}
            manifest = {
                "format": "c4x-project-export/1",
                "project": project,
                "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "source_machine": platform.node(),
                "source_store": str(store.DB_PATH),
                "sessions": len(ids),
                "counts": counts,
                "digests": digests,
                # Named so an import against a schema that has since grown can say what it dropped
                # rather than failing or, worse, shifting values into the wrong columns.
                "tables": list(carried),
                "unhandled_tables_at_export": unhandled_tables(out),
            }
            out.execute(f"CREATE TABLE IF NOT EXISTS {MANIFEST_TABLE} "
                        "(key TEXT PRIMARY KEY, value TEXT)")
            out.execute(
                f"INSERT OR REPLACE INTO {MANIFEST_TABLE} (key, value) VALUES ('manifest',?)",
                (json.dumps(manifest, indent=1),))
            out.commit()
        finally:
            out.close()
    finally:
        source.close()

    ok, problems = verify(out_path)
    if not ok:
        raise RuntimeError("the export could not be verified after writing: " + "; ".join(problems))
    return manifest


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_(path):
    """Load an export into this store, verifying it first and inserting only what is missing.

    `INSERT OR IGNORE` on the primary key, so importing the same file twice is a no-op rather than a
    pile of duplicates. Columns are matched BY NAME against the local schema: an export from a build
    with an extra column loads what it can and reports what it left behind, instead of failing
    outright or, far worse, shifting every value one column to the left.

    Imported sessions need no special handling to be labelled as imported. `store.classify()`
    already calls a session whose transcript is missing and outside this machine's home
    "Imported from another machine", which is exactly what these are.
    """
    from c4x import store
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no export at {path}")
    ok, problems = verify(path)
    if not ok:
        raise ValueError("refusing to import an export that does not verify: "
                         + "; ".join(problems))
    manifest = read_manifest(path)

    # STILL EXCLUDED? Say so rather than fixing it silently. Importing a project the store is
    # excluding puts every row back and leaves harvest skipping the directory, so the sessions the
    # user has since run are never captured and nothing on the page connects the two facts.
    #
    # Reported, not lifted. An export from another machine can name a working directory that this
    # machine has deliberately excluded, and quietly resuming capture of a local directory because
    # a file mentioned it is a decision this function is not entitled to make. The caller offers it.
    report = {"project": manifest.get("project"), "from": manifest.get("source_machine"),
              "still_excluded": any(e["cwd"] == manifest.get("project") for e in excluded()),
              "inserted": {}, "already_present": {}, "dropped_columns": {}}
    with store.write() as con:
        con.execute("ATTACH DATABASE ? AS src", (str(path),))
        try:
            for table in manifest.get("tables", []):
                # `PRAGMA main.table_info(t)`, not `PRAGMA table_info(main.t)`. The schema is a
                # prefix on the pragma itself; put it inside the parentheses and SQLite reports a
                # syntax error at the dot.
                mine = [r[1] for r in con.execute(f"PRAGMA main.table_info({table})").fetchall()]
                theirs = [r[1] for r in con.execute(f"PRAGMA src.table_info({table})").fetchall()]
                if not mine:
                    report["dropped_columns"][table] = ["(this store has no such table)"]
                    continue
                shared = [c for c in theirs if c in mine]
                missing = [c for c in theirs if c not in mine]
                if missing:
                    report["dropped_columns"][table] = missing
                before = con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
                listed = ",".join(f'"{c}"' for c in shared)
                con.execute(f"INSERT OR IGNORE INTO main.{table} ({listed}) "
                            f"SELECT {listed} FROM src.{table}")
                after = con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
                offered = con.execute(f"SELECT COUNT(*) FROM src.{table}").fetchone()[0]
                report["inserted"][table] = after - before
                report["already_present"][table] = offered - (after - before)
        finally:
            # COMMIT BEFORE DETACH. An open transaction holds the attached database, and SQLite
            # answers DETACH with "database src is locked" rather than anything naming the cause.
            con.commit()
            con.execute("DETACH DATABASE src")
    return report


# ---------------------------------------------------------------------------
# Exclusions, which are what make a delete stick
# ---------------------------------------------------------------------------
def ensure_exclusions(con):
    con.execute("""CREATE TABLE IF NOT EXISTS excluded_projects (
                     cwd TEXT PRIMARY KEY, excluded_at TEXT, note TEXT)""")


def excluded():
    from c4x import store
    try:
        return store.q("SELECT cwd, excluded_at, note FROM excluded_projects ORDER BY cwd") \
            .to_dict("records")
    except Exception:                              # noqa: BLE001 - absent table means none excluded
        return []


def exclude(project, note=""):
    from c4x import store
    with store.write() as con:
        ensure_exclusions(con)
        con.execute("INSERT OR REPLACE INTO excluded_projects (cwd, excluded_at, note) "
                    "VALUES (?,?,?)",
                    (project, datetime.now(UTC).isoformat(timespec="seconds"), note))
    return project


def include(project):
    """Stop excluding. The next harvest picks the project up again from the beginning."""
    from c4x import store
    with store.write() as con:
        ensure_exclusions(con)
        removed = con.execute("DELETE FROM excluded_projects WHERE cwd = ?", (project,)).rowcount
    return removed


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def file_name(project, stamped=False):
    """A filename for a project's export. One rule, so a download and a delete's backup agree.

    Every non-alphanumeric character goes, which flattens a Windows path to something a browser
    will accept as a download name. That is lossy on purpose and not a problem: the project path
    lives in the manifest INSIDE the file, so the name is a label and never the record.
    """
    safe = "".join(c if c.isalnum() else "-" for c in str(project)).strip("-")[:80] or "project"
    if not stamped:
        return f"{safe}.db"
    return f"{safe}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.db"


def delete(project, confirm, out_dir=None, keep_capturing=False):
    """Export, verify, then remove, then stop capturing. It stops at the first thing that fails.

    THE EXPORT COMES FIRST AND IS VERIFIED BEFORE ANYTHING IS REMOVED, so a delete is always
    undoable by importing the file it just wrote. If the export cannot be read back, nothing is
    deleted.

    `confirm` must be the project path exactly. Not a yes/no: the whole risk here is deleting the
    wrong project, and a boolean cannot tell those apart.
    """
    from c4x import store
    if confirm != project:
        raise ValueError("confirmation does not match the project path; nothing was deleted")

    out_dir = Path(out_dir or (ROOT / "tmp" / "exports"))
    backup = out_dir / file_name(project, stamped=True)
    manifest = export(project, backup)             # raises if it cannot be verified

    with store.write() as con:
        ids = session_ids(con, project)
        if not ids:
            raise ValueError(f"no sessions with cwd {project!r}")
        marks = ",".join("?" * len(ids))
        removed = {}
        # Survivors before compactions, and everything before sessions: no foreign keys means
        # nothing cleans up after a half-finished delete, so the order is the safety.
        for table in BY_COMPACTION:
            removed[table] = con.execute(
                f"""DELETE FROM {table} WHERE compaction_uuid IN
                    (SELECT uuid FROM compactions WHERE session_id IN ({marks}))""", ids).rowcount
        for table in BY_TRANSCRIPT:
            # The offset row goes too. Left behind, the transcript is skipped forever even after
            # the exclusion is lifted, which would make "include" quietly do nothing.
            removed[table] = con.execute(
                f"""DELETE FROM {table} WHERE path IN
                    (SELECT transcript_path FROM sessions WHERE session_id IN ({marks})
                      AND transcript_path IS NOT NULL)""", ids).rowcount
        for table in BY_SESSION:
            removed[table] = con.execute(
                f"DELETE FROM {table} WHERE session_id IN ({marks})", ids).rowcount
        if not keep_capturing:
            ensure_exclusions(con)
            con.execute("INSERT OR REPLACE INTO excluded_projects (cwd, excluded_at, note) "
                        "VALUES (?,?,?)",
                        (project, datetime.now(UTC).isoformat(timespec="seconds"),
                         f"deleted, exported to {backup.name}"))
    return {"project": project, "backup": str(backup), "removed": removed,
            "excluded": not keep_capturing, "exported_sessions": manifest["sessions"]}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def self_test():
    """Checks that need no store: the parts that decide whether data is lost."""
    import tempfile
    cases = []

    # A digest must depend on CONTENT and not on row order, or verify() would report a corrupt
    # export every time SQLite handed rows back differently.
    with tempfile.TemporaryDirectory() as folder:
        a, b = Path(folder) / "a.db", Path(folder) / "b.db"
        for path, order in ((a, [(1, "x"), (2, "y")]), (b, [(2, "y"), (1, "x")])):
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            con.executemany("INSERT INTO t VALUES (?,?)", order)
            con.commit()
            con.close()
        con_a, con_b = sqlite3.connect(str(a)), sqlite3.connect(str(b))
        same = digest(con_a, "t") == digest(con_b, "t")
        con_a.close(); con_b.close()                                            # noqa: E702
        cases.append(("row order does not change a digest", same))

        # And a changed VALUE must change it, or the check is decoration.
        con = sqlite3.connect(str(b))
        was = digest(con, "t")
        con.execute("UPDATE t SET v = 'z' WHERE id = 1")
        con.commit()
        cases.append(("a changed value DOES change a digest", digest(con, "t") != was))
        con.close()
        cases.append(("a file with no manifest does not verify", verify(a)[0] is False))

    cases.append(("every project table is named, none twice",
                  len(set(BY_SESSION) | set(BY_COMPACTION) | set(BY_TRANSCRIPT))
                  == len(BY_SESSION) + len(BY_COMPACTION) + len(BY_TRANSCRIPT)))
    cases.append(("sessions is deleted LAST, after everything keyed on it",
                  BY_SESSION[-1] == "sessions"))
    cases.append(("survivors are deleted before the compactions they point at",
                  "compaction_survivors" in BY_COMPACTION and
                  "compactions" in BY_SESSION))
    cases.append(("the store-wide tables are not treated as project data",
                  not (set(STORE_WIDE) & (set(BY_SESSION) | set(BY_COMPACTION)))))

    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("list", help="every project and its session count")
    sub.add_parser("excluded", help="projects the harvester has been told to skip")

    p_export = sub.add_parser("export", help="write one project to a standalone store")
    p_export.add_argument("project")
    p_export.add_argument("--out", required=True)

    p_import = sub.add_parser("import", help="load an exported project into this store")
    p_import.add_argument("path")

    p_delete = sub.add_parser("delete", help="export, then remove, then stop capturing")
    p_delete.add_argument("project")
    p_delete.add_argument("--confirm", default="", help="the project path, exactly")
    p_delete.add_argument("--keep-capturing", action="store_true",
                          help="do not exclude it, so the next harvest brings it back")
    p_delete.add_argument("--out-dir", default=None)

    p_include = sub.add_parser("include", help="stop excluding a project")
    p_include.add_argument("project")

    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.command:
        # Was `ap.print_help() or 0`, which reads as though print_help returned something.
        ap.print_help()
        return 0

    if args.command == "list":
        for row in projects():
            print(f"  {row['sessions']:>5}  {row['project']}")
        return 0
    if args.command == "excluded":
        rows = excluded()
        if not rows:
            print("  no projects are excluded; everything under ~/.claude/projects is captured")
        for row in rows:
            print(f"  {row['excluded_at']}  {row['cwd']}  {row['note'] or ''}")
        return 0
    if args.command == "export":
        manifest = export(args.project, args.out)
        print(f"  wrote {args.out}")
        print(f"  {manifest['sessions']} session(s), verified")
        for table, n in manifest["counts"].items():
            print(f"    {table:22} {n:>8,}")
        return 0
    if args.command == "import":
        report = import_(args.path)
        print(f"  {report['project']}  from {report['from']}")
        for table, n in report["inserted"].items():
            already = report["already_present"].get(table, 0)
            print(f"    {table:22} {n:>8,} inserted, {already:,} already present")
        for table, columns in report.get("dropped_columns", {}).items():
            print(f"    NOT LOADED  {table}: {', '.join(columns)}")
        return 0
    if args.command == "delete":
        result = delete(args.project, args.confirm, args.out_dir, args.keep_capturing)
        print(f"  exported to {result['backup']} before deleting")
        for table, n in result["removed"].items():
            print(f"    {table:22} {n:>8,} removed")
        print(f"  excluded from future harvests: {result['excluded']}")
        return 0
    if args.command == "include":
        print(f"  removed {include(args.project)} exclusion(s) for {args.project}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

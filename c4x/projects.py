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
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from c4x.frames import records

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Every table that belongs to a project, and HOW it is reached. Order matters for deletion: a row
# is removed before the row it points at, so a half-finished delete cannot orphan anything.
#
# `files` is here because the harvest offset is part of the project's footprint: leave it and the
# transcript is skipped forever; remove it and the next harvest re-reads from the beginning.
# cost_state and cost_state_models are BY SESSION, not store-wide, and the distinction decides
# whether an exported project carries what it actually cost. Both are keyed on session_id, both
# describe one session's work, and leaving them out would hand someone an export whose Cost tab
# shows the ESTIMATE and silently drops the measurement beside it: two figures on the source page,
# one on the copy, with nothing saying which went missing.
#
# `sessions` IS LAST, and the two cost tables were appended AFTER it when they were added, which
# put a delete back in the exact window this ordering exists to close: `sessions` gone while
# `cost_state` still points at it. The self-test has been reporting that as a FAIL; this is the
# reorder. Nothing but the deletion order reads this sequence, so the export and the footprint are
# unaffected.
BY_SESSION = ("hook_events", "attachments", "tool_calls", "messages", "turns",
              "session_titles", "compactions", "cost_state", "cost_state_models",
              "sessions")

# Reached another way, and named so nothing depends on remembering it.
BY_COMPACTION = ("compaction_survivors",)
BY_TRANSCRIPT = ("files",)

# Deliberately NOT project data. Listed so that "is every table accounted for?" is a question this
# module can answer rather than a thing to check by eye.
STORE_WIDE = ("probes", "probe_categories", "probe_details", "probe_message_breakdown",
              "context_baselines", "record_types", "harvest_runs", "excluded_projects",
              "sqlite_sequence")

MANIFEST_TABLE = "c4x_export"

# Claude Code's own state, carried as rows so the existing digest and verify machinery covers it.
# It is NOT a project table: `import_` applies it to the filesystem and must skip it in the
# row-copy loop, and `verify()` allows it by name rather than by membership in the three lists
# above. All three of those facts have to hold together, which is why they are asserted by
# `self_test()` rather than left to be remembered.
APP_STATE_TABLE = "c4x_app_state"

# A single value SQLite will not carry, and a file this big in a transcript directory is a sign
# something else is wrong. Reported by name rather than silently dropped.
MAX_CARRIED_BYTES = 500 * 1024 * 1024


# ---------------------------------------------------------------------------
# Reading what a project is
# ---------------------------------------------------------------------------
def projects():
    """Every project this tool can ACT on, named the way `export` and `delete` resolve them.

    THE LIST IS A MENU, so an entry it prints has to be one those two commands accept. This asked
    `GROUP BY cwd`, which is not how a project is identified here: `session_rows()` appends
    `\\archived` to the label when the desktop app has archived the chat, so an archived-only
    project was listed under its bare cwd and then refused by both commands with "no sessions with
    cwd", which reads as a broken store rather than a wrong name. Measured on this store, 3 of 25
    small projects sampled were listed and unusable.

    `session_ids` already had the archived case right and says so in its own docstring. It was this
    function that never learned, so the two halves below mirror ITS two halves rather than
    inventing a third answer: the labels the page uses, plus the sessions the page cannot see at
    all, which are keyed on their own cwd because that is the only evidence those have.
    """
    from c4x import store
    seen = store.session_rows()
    counts = {}
    if not seen.empty:
        for label, n in seen["project"].value_counts().items():
            counts[str(label)] = int(n)
    visible = set(seen["session_id"]) if not seen.empty else set()
    rest = store.q("SELECT session_id, cwd FROM sessions WHERE cwd IS NOT NULL AND cwd <> ''")
    for row in rest.itertuples(index=False):
        if row.session_id not in visible:
            counts[str(row.cwd)] = counts.get(str(row.cwd), 0) + 1
    return [{"project": p, "sessions": n}
            for p, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


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
    `SESSION_TURN_FLOOR` of them. Measured on this store that was 1,008 of 1,325 sessions,
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


def cwds_for(con, ids):
    """The WORKING DIRECTORIES these sessions actually have, resolved from the store.

    Never the project label. A label can be `P:\\Books\\archived`, which is a page label the
    desktop app's archive flag produced and not a directory anything can be written to. Callers run
    this BEFORE touching the filesystem, because the last attempt called it after the delete and
    got `[]` back, which left the fallback-to-the-label branch as the only one that ever ran.
    """
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    found = con.execute(
        f"SELECT DISTINCT cwd FROM sessions WHERE session_id IN ({marks}) "
        "AND cwd IS NOT NULL AND cwd <> ''", list(ids)).fetchall()
    return [r[0] for r in found]


def sessions_with_cwds(con, cwds):
    """Every session this store still has for these working directories.

    A SLUG DIRECTORY IS SHARED BY EVERY SESSION WITH THAT WORKING DIRECTORY, and two of the five
    layers are keyed by the directory rather than the session: `memory/` and the `~/.claude.json`
    entry. So a delete of one label has to ask whether anything else still lives there before it
    takes either.

    Measured on this machine on 2026-09-10, and it is not a corner. The label
    `P:\\ClaudeExt\\QuestionExtension\\archived` would carry 21 of the 2,430 files in its slug
    directory while `P:\\ClaudeExt\\QuestionExtension` carries 2,382. They are one directory under
    two labels, so deleting the archived one and taking the shared layers with it would strip the
    memory and the trust entry from a project the user did not delete.
    """
    if not cwds:
        return []
    marks = ",".join("?" * len(cwds))
    found = con.execute(
        f"SELECT DISTINCT session_id FROM sessions WHERE cwd IN ({marks})", list(cwds)).fetchall()
    return [r[0] for r in found]


def surviving_slugs(con):
    """Every slug directory that still has a session in it, casefolded.

    `sessions_sharing_slug` answers "does ANY carried directory still have a neighbour", which is
    one boolean over the whole delete. A project can carry more than one working directory, and
    then the boolean kept `memory/` for a slug directory with no survivor at all, leaving the file
    on disk and reporting it under `shared_with_surviving_sessions` as a deliberate exception. The
    decision belongs to the row, and the row carries the directory it came from.
    """
    from c4x import appstate
    out = set()
    for (cwd,) in con.execute("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"):
        try:
            out.add(appstate.slug_for(cwd).casefold())
        except (TypeError, ValueError):
            continue
    return out


def one_working_directory(con, project):
    """The sessions a project label owns, refusing a label that names two different projects.

    THE LABEL SPACE IS AMBIGUOUS AND THE CODEBASE ALREADY KNOWS IT. `store.session_rows` labels a
    desktop-archived session by appending the archived suffix to its working directory, and
    `project_label` returns the bare working directory otherwise, so an archived chat of a project
    and a REAL project whose working directory happens to end in that suffix produce the identical
    label. `cohort_sessions` then matches both, and the second half of `session_ids` adds the real
    project's below-floor sessions on top. `check_destination` refuses exactly this ambiguity on
    the import side; nothing consulted it on the delete side, and this branch is what made the
    consequence reach transcripts, tasks, the trust entry and a permanent exclusion rather than
    rows alone.

    A label that resolves to more than one working directory cannot be told apart from itself, so
    it is refused by name rather than guessed at. Every unambiguous label resolves to exactly one.
    """
    ids = session_ids(con, project)
    cwds = cwds_for(con, ids)
    if len(cwds) > 1:
        raise ValueError(
            f"{project!r} names more than one working directory on this machine: "
            + ", ".join(repr(c) for c in cwds)
            + ". An archived label and a real directory of that name are the same string here, so "
              "this delete cannot tell which one you mean; nothing was deleted.")
    return ids, cwds


def surviving_normalised_cwds(con):
    """Every working directory still in this store, in the form the config capture matches by.

    `_capture_config` (`c4x/appstate.py:489`) selects keys with `normalised(key) in wanted`, and
    `normalised` folds slash spelling and case. So deleting `P:` + `Alpha` spelled with a backslash
    carries the forward slash entry into the backup too. The survivor test that decided whether to
    keep them was `sessions_with_cwds`, a byte exact `WHERE cwd IN`, so a session filed under the
    other spelling was not a survivor and `_drop_config` removed BOTH keys by exact match. The live
    project then lost its trust entry and its per project settings, and the report said the
    settings had been kept.

    One import with `--into` using the other slash spelling is enough to produce both forms in one
    store, so this is reachable through the app's own commands. It is the same class as
    `sessions_sharing_slug`: the half that CAPTURES and the half that PROTECTS have to agree on
    what "the same working directory" means, and here they did not.
    """
    from c4x import appstate
    out = set()
    for (cwd,) in con.execute("SELECT DISTINCT cwd FROM sessions WHERE cwd IS NOT NULL"):
        try:
            out.add(appstate.normalised(cwd))
        except (TypeError, ValueError):
            continue
    return out


def sessions_sharing_slug(con, cwds):
    """Every session still filed in a slug directory these working directories map to.

    `sessions_with_cwds` compares cwd STRINGS, and that is right for the `~/.claude.json` entry,
    which is keyed by the exact string. It is wrong for `memory/`, which lives in the SLUG
    directory, because `slug_for` replaces every character that is not a letter or a digit with a
    hyphen. A hyphen, an underscore and a dot in the same position all become one hyphen, so
    three different projects, with three different config keys, share ONE directory. A survivor
    test by string reports that directory as free while
    another project's memory is sitting in it, and the delete then removes it and reports under
    `shared_with_surviving_sessions` that it kept it.

    A scan rather than SQL because the slug rule lives in Python and restating it as a SQL
    expression would be a second copy of the one rule this repo already warns is unvalidatable.
    """
    from c4x import appstate
    if not cwds:
        return []
    wanted = set()
    for cwd in cwds:
        try:
            wanted.add(appstate.slug_for(cwd).casefold())
        except ValueError:
            continue
    if not wanted:
        return []
    rows = con.execute(
        "SELECT DISTINCT session_id, cwd FROM sessions WHERE cwd IS NOT NULL").fetchall()
    out = []
    for sid, cwd in rows:
        try:
            if appstate.slug_for(cwd).casefold() in wanted:
                out.append(sid)
        except ValueError:
            continue
    return out


def snapshots_dir():
    """Where `hooks/compact-hook.mjs` keeps its verbatim pre-compaction copies.

    Beside the store rather than under the repo root, for the reason `delete` spells out about its
    backup directory: with the path following `C4X_DB`, a fixture run reads and writes beside the
    fixture and production beside production.
    """
    from c4x import store
    return store.DB_PATH.parent / "snapshots"


def snapshot_files(ids, shared_stems=()):
    """The pre-compaction snapshots these sessions own outright.

    A SNAPSHOT IS A COPY OF A TRANSCRIPT FILE, NOT OF A SESSION. `hooks/compact-hook.mjs:73` names
    it from `basename(transcript_path)` and line 74 copies the whole file, so the stem is the
    FILE's and matching it against a session id really asks "is this session the owner of that
    file". That is false whenever a transcript holds more than one session, which on this store is
    8 sessions across 7 files, 2 of those files spanning two working directories. The docstring
    here used to claim the hook named them per session; it does not.

    `shared_stems` are the stems of transcripts a surviving session is also in. They are skipped,
    because removing one takes another project's only copy of what a compaction dropped and the
    backup does not carry snapshots at all.

    Measured on this machine on 2026-09-10: 1,190 MB across 377 sessions, of which this repo's own
    project is 1,119.7 MB in 14 files, and 3 of 35 sampled projects had any at all.
    """
    base = snapshots_dir()
    if not base.is_dir():
        return []
    wanted = {str(s) for s in ids}
    shared = {str(s) for s in shared_stems}
    return sorted(p for p in base.iterdir()
                  if p.is_file() and p.name.split(".", 1)[0] in wanted
                  and p.name.split(".", 1)[0] not in shared)


def primary_cwd(con, ids):
    """The working directory most of these sessions have, which is what `--into` replaces."""
    if not ids:
        return None
    marks = ",".join("?" * len(ids))
    found = con.execute(
        f"SELECT cwd, COUNT(*) n FROM sessions WHERE session_id IN ({marks}) "
        "AND cwd IS NOT NULL AND cwd <> '' GROUP BY cwd ORDER BY n DESC, cwd", list(ids)).fetchone()
    return found[0] if found else None


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
    known.add(APP_STATE_TABLE)
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


def app_state_digest(con):
    """The same idea as `digest`, streamed, because these rows carry whole files.

    `digest` builds a list of every row's `repr` and sorts it. That is right for a table of numbers
    and would try to hold 694 MB of transcript in memory as text for this one, which is how a
    verification step turns into the thing that kills the machine it was protecting.

    The blob is not in this hash and does not need to be: every row carries its OWN sha256, this
    hash covers those, and `verify()` re-reads each blob and compares it to its row. So a
    truncated file is caught by the second check while the first stays cheap.
    """
    if not _has_table(con, APP_STATE_TABLE):
        return None
    running = hashlib.sha256()
    rows = con.execute(
        f"SELECT kind, path, cwd, mtime, sha256, rebased_sha256 FROM {APP_STATE_TABLE}")
    for row in sorted(repr(tuple(r)) for r in rows):
        running.update(row.encode("utf-8"))
    return f"sha256:{running.hexdigest()}"


def _has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone())


def app_state_rows(path, with_blobs=True):
    """Every carried file, as `appstate` wants them.

    `with_blobs=False` leaves the bytes in the file. `restore` needs them, because writing a file
    is what it does. `purge` does not: it compares the hash the row already carries against the
    bytes on the disk, so loading a backup of this repo's own project, 694.5 MB of blobs, to
    decide whether to delete the files those blobs were read from would spend the memory to learn
    nothing the row did not already say.
    """
    columns = "kind, path, cwd, mtime, sha256, rebased_sha256"
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if not _has_table(con, APP_STATE_TABLE):
            return []
        if not with_blobs:
            return [{"kind": k, "relpath": p, "cwd": c, "mtime": m,
                     "sha256": s, "rebased_sha256": r}
                    for k, p, c, m, s, r in con.execute(
                        f"SELECT {columns} FROM {APP_STATE_TABLE}")]
        return [{"kind": k, "relpath": p, "cwd": c, "mtime": m,
                 "sha256": s, "rebased_sha256": r, "blob": bytes(b)}
                for k, p, c, m, s, r, b in con.execute(
                    f"SELECT {columns}, blob FROM {APP_STATE_TABLE}")]
    finally:
        con.close()


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


def carried_tables(manifest):
    """The tables an import is allowed to touch: the ones this manifest DIGESTED.

    THE IMPORT USED TO READ A DIFFERENT FIELD FROM THE ONE VERIFY CHECKED. `verify` walks
    `digests` and `counts`; `import_` walked `tables`, and nothing compared the three. A file whose
    `tables` listed anything the digests did not cover verified clean and then had that name
    interpolated straight into `INSERT OR IGNORE INTO main.{table}` on the live store's read-write
    connection, under a docstring promising "Verified before a single row is written".

    So the verified set is the ONLY set, and it is returned from one place that both callers use.
    """
    return sorted((manifest.get("digests") or {}).keys())


def verify(path):
    """Recompute every digest in an export and compare it to what the export claims.

    Run on the file after writing it and again before importing it, so a truncated or edited export
    is caught rather than half-loaded.
    """
    manifest = read_manifest(path)
    if not manifest:
        return False, [f"{path} carries no {MANIFEST_TABLE} manifest, so it is not a c4x export"]
    problems = []

    # THE THREE FIELDS MUST NAME THE SAME TABLES. A genuine export writes all three from one
    # `carried` list (see export()), so disagreement is either corruption or a file built to be
    # verified on one set and applied to another. Either way it is refused before anything opens.
    digested = set((manifest.get("digests") or {}).keys())
    # A MANIFEST THAT CLAIMS NOTHING SATISFIES EVERY CHECK BELOW VACUOUSLY. With no digests there is
    # nothing to recompute, the set comparisons hold trivially, and `verify` returned True over a
    # file it had not examined at all - then the import wrote nothing and told the user it had
    # verified. Harmless in outcome and wrong in what it asserts, which is the failure this whole
    # function exists to prevent. Found while adversarially testing the fix that added those set
    # comparisons in the first place.
    if not digested:
        return False, [f"{path} carries a manifest with no digested tables, so there is nothing to "
                       "verify. A c4x export always digests every table it carries."]
    counted = set((manifest.get("counts") or {}).keys())
    listed = set(manifest.get("tables") or [])
    for name, seen in (("counts", counted), ("tables", listed)):
        if seen != digested:
            problems.append(
                f"manifest {name} names {sorted(seen - digested) or 'nothing extra'} that digests "
                f"do not, and omits {sorted(digested - seen) or 'nothing'}; an export writes all "
                "three from one list, so this file was edited")

    # And every one of them must be a table this module carries. The name reaches SQL as a bare
    # identifier, so an allow-list is the guard, not the quoting.
    #
    # APP_STATE_TABLE IS IN THE ALLOW-LIST AND IN NONE OF THE THREE LISTS. Getting that wrong is
    # not subtle: leave it out and every fresh export fails its own verification, put it in
    # BY_SESSION and `import_` inserts whole transcripts into a table that has no such columns.
    known = set(BY_SESSION) | set(BY_COMPACTION) | set(BY_TRANSCRIPT) | {APP_STATE_TABLE}
    unknown = sorted(digested - known)
    if unknown:
        problems.append(f"manifest names tables this store does not export: {unknown}")
    if problems:
        return False, problems

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, claimed in (manifest.get("digests") or {}).items():
            actual = app_state_digest(con) if table == APP_STATE_TABLE else digest(con, table)
            if actual != claimed:
                problems.append(f"{table}: manifest says {claimed}, file contains {actual}")
        for table, claimed in (manifest.get("counts") or {}).items():
            actual = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != claimed:
                problems.append(f"{table}: manifest says {claimed} rows, file has {actual}")
        # EVERY CARRIED FILE, RE-HASHED AGAINST ITS OWN ROW. The digest above covers the hashes;
        # this covers the bytes those hashes describe, which is the half a truncated export loses.
        # Streamed one row at a time: an export of this repo's own project is 694 MB.
        if _has_table(con, APP_STATE_TABLE):
            from c4x import appstate
            for kind, rel, claimed, blob in con.execute(
                    f"SELECT kind, path, sha256, blob FROM {APP_STATE_TABLE}"):
                if not isinstance(blob, (bytes, bytearray)):
                    problems.append(f"{kind} {rel}: blob is {type(blob).__name__}, not bytes")
                    continue
                actual = appstate.sha256_bytes(bytes(blob))
                if actual != claimed:
                    problems.append(f"{kind} {rel}: row says {claimed}, bytes hash to {actual}")
    except sqlite3.Error as exc:
        problems.append(f"{path} could not be read as a c4x export: {exc}")
    finally:
        con.close()
    return not problems, problems


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _write_app_state(out_path, cwds, ids):
    """Capture the four outside-the-store layers into the export, and report what was left.

    Returns the part of the manifest that describes them, INCLUDING what was not carried. An
    export that silently omits is the defect; an export that names what it omitted is honest, and
    the two are indistinguishable from a count alone.
    """
    from c4x import appstate

    # STRAIGHT INTO THE FILE, ONE ROW AT A TIME. This built the whole capture as a list of blobs
    # first: 694.5 MB resident for this repo's own project before a single byte reached the disk,
    # and every one of those bytes was already on the disk it was read from.
    counts = dict.fromkeys(appstate.KINDS, 0)
    written = {"files": 0, "bytes": 0}
    oversized = []
    con = sqlite3.connect(str(out_path))
    try:
        con.execute(f"""CREATE TABLE IF NOT EXISTS {APP_STATE_TABLE} (
                          kind TEXT NOT NULL, path TEXT NOT NULL, cwd TEXT NOT NULL,
                          mtime REAL NOT NULL, sha256 TEXT NOT NULL,
                          rebased_sha256 TEXT NOT NULL, blob BLOB NOT NULL,
                          PRIMARY KEY (kind, path, cwd))""")

        def keep(row):
            if len(row["blob"]) > MAX_CARRIED_BYTES:
                oversized.append({"path": row["relpath"], "bytes": len(row["blob"])})
                return
            con.execute(
                f"INSERT OR REPLACE INTO {APP_STATE_TABLE} "
                "(kind, path, cwd, mtime, sha256, rebased_sha256, blob) VALUES (?,?,?,?,?,?,?)",
                (row["kind"], row["relpath"], row["cwd"], row["mtime"], row["sha256"],
                 row["rebased_sha256"], sqlite3.Binary(row["blob"])))
            counts[row["kind"]] += 1
            written["files"] += 1
            written["bytes"] += len(row["blob"])

        _rows, report = appstate.capture(cwds, ids, sink=keep)
        con.commit()
    finally:
        con.close()

    return {
        "files": written["files"],
        "bytes": written["bytes"],
        "by_kind": counts,
        "cwds": cwds,
        "not_carried": report["not_carried"],
        "not_carried_files": report["not_carried_files"],
        "skipped": report["skipped"],
        "too_large": oversized,
        "source_desktop_pair": report["desktop_pair"],
    }


def _empty_app_state(out_path, cwds):
    """The table, with no rows, for a rows-only export.

    THE TABLE STILL HAS TO EXIST. `verify()` walks the manifest's digests, the manifest is built
    from the same `carried` list either way, and a digest of a table that is not there is None,
    which does not match. An export that cannot verify itself is refused by its own writer, so
    the empty case is created rather than special-cased in three places.
    """
    con = sqlite3.connect(str(out_path))
    try:
        con.execute(f"""CREATE TABLE IF NOT EXISTS {APP_STATE_TABLE} (
                          kind TEXT NOT NULL, path TEXT NOT NULL, cwd TEXT NOT NULL,
                          mtime REAL NOT NULL, sha256 TEXT NOT NULL,
                          rebased_sha256 TEXT NOT NULL, blob BLOB NOT NULL,
                          PRIMARY KEY (kind, path, cwd))""")
        con.commit()
    finally:
        con.close()
    return {"files": 0, "bytes": 0, "by_kind": {}, "cwds": cwds, "not_carried": [],
            "not_carried_files": 0, "skipped": [], "too_large": [],
            "source_desktop_pair": None,
            "why_empty": "this export was written with app_state off, so it carries rows only"}


def export(project, out_path, app_state=True):
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
            cwds = cwds_for(source, ids)
            primary = primary_cwd(source, ids)
        finally:
            out.close()

        # THE OTHER THREE LAYERS. Without them an imported project is a set of rows: present in
        # c4x, invisible in the desktop app, unopenable by `/resume`. Measured on a second machine,
        # an import used to touch exactly one file.
        carried_state = _write_app_state(out_path, cwds, ids) if app_state else \
            _empty_app_state(out_path, cwds)

        # Counted and digested from the FILE, not from what was intended to be written. The point
        # of the manifest is to describe what is actually in there.
        out = sqlite3.connect(str(out_path))
        try:
            carried = BY_SESSION + BY_COMPACTION + BY_TRANSCRIPT + (APP_STATE_TABLE,)
            counts = {t: out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in carried}
            digests = {t: (app_state_digest(out) if t == APP_STATE_TABLE else digest(out, t))
                       for t in carried}
            manifest = {
                "format": "c4x-project-export/2",
                "project": project,
                "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "source_machine": platform.node(),
                "source_store": str(store.DB_PATH),
                "sessions": len(ids),
                "session_ids": sorted(ids),
                # THE WORKING DIRECTORIES, WHICH ARE NOT THE PROJECT LABEL. `project` above can be
                # `P:\\Books\\archived`, a label the desktop app's archive flag produced. These are
                # what an import writes to, and what `--into` replaces.
                "cwds": cwds,
                "primary_cwd": primary,
                "app_state": carried_state,
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
def check_destination(into):
    """The destination a user typed, checked before anything is derived from it.

    Everything an import writes is derived from this one string: the slug directory, the config
    key, the `cwd` inside the desktop record, and the rows. So a value that is not a working
    directory does not fail loudly, it produces a project filed under a name nothing will ever look
    for. `..\\x` becomes the slug `---x`; a project LABEL ending in the archived suffix becomes a
    directory that does not exist; and an existing FILE becomes a slug directory beside it while
    the config claims the file is a project.

    Absolute, including UNC, because a relative path means nothing once it leaves the shell that
    typed it: the import runs in the API server's working directory, not the user's.
    """
    from c4x import store
    text = str(into).strip()
    if not text:
        raise ValueError("the destination working directory cannot be empty")
    if text.rstrip("\\/").casefold().endswith("\\" + store.ARCHIVED_SUFFIX.casefold()):
        raise ValueError(
            f"{text!r} is a project LABEL, not a directory: the desktop app's archive flag adds "
            f"\\{store.ARCHIVED_SUFFIX} to what the page shows. Import into the real directory.")
    windows_absolute = re.match(r"^[A-Za-z]:[\\/]", text) is not None
    unc = text.startswith("\\\\") or text.startswith("//")
    if not (windows_absolute or unc or text.startswith("/")):
        raise ValueError(
            f"{text!r} is a relative path. An import runs wherever the server runs, not where you "
            "typed it, so the destination has to be absolute.")
    if Path(text).is_file():
        raise ValueError(f"{text!r} is an existing FILE, so it cannot be a working directory")
    return text.rstrip("\\/") or text


def destination_mapping(manifest, into=None):
    """{source working directory: destination working directory} for this import.

    With no `--into`, every directory keeps its own name and the import lands where the export came
    from. With one, the PRIMARY directory becomes it and anything nested under the primary is moved
    by the same prefix, so `P:\\Skills` and `P:\\Skills\\sub` land as `D:\\Work\\Skills` and
    `D:\\Work\\Skills\\sub` rather than one of them being silently merged into the other.

    A directory that is neither the primary nor under it is left alone and REPORTED. Folding it
    into the destination would merge two projects, which is a decision this function is not
    entitled to make on the strength of one flag.
    """
    from c4x import appstate
    cwds = list(manifest.get("cwds") or [])
    primary = manifest.get("primary_cwd") or (cwds[0] if cwds else None)
    if into is not None:
        into = check_destination(into)
    if not into or not primary:
        return {c: c for c in cwds}, []
    mapping, unmoved = {}, []
    root = appstate.normalised(primary)
    for cwd in cwds:
        here = appstate.normalised(cwd)
        if here == root:
            mapping[cwd] = into
        elif here.startswith(root + "\\"):
            mapping[cwd] = str(into).rstrip("\\") + cwd[len(primary):]
        else:
            mapping[cwd] = cwd
            unmoved.append(cwd)
    return mapping, unmoved


def _rebase_store_rows(con, ids, mapping, app_rows):
    """Point the imported ROWS at this machine, so c4x and the desktop app agree on one path.

    Four things carry a working directory into the store, and leaving any of them holding the
    source machine's path shows the user a directory that does not exist here:

        sessions.cwd            what the project selector and every cohort resolve on
        hook_events.cwd         the same, for the hook tables
        sessions.transcript_path  where `store.classify()` looks for the conversation
        files.path              the harvest offset, keyed BY transcript_path

    The last two move together on purpose. Rewrite the session and leave the offset behind and the
    next harvest re-reads the whole transcript from zero against a path it has never seen.
    """
    from c4x import appstate
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    moved = {"sessions": 0, "hook_events": 0, "transcripts": 0, "files": 0}
    # WHICH SESSION CAME FROM WHICH DIRECTORY, read from the EXPORT rather than from this store.
    #
    # These matched on `AND cwd = <source>`, which is true only the first time. Import an export
    # into D:\First and the rows read D:\First; import the SAME export again into D:\Second and
    # that clause matches nothing, so the rows stayed at D:\First while the transcript and its
    # offset moved to D:\Second, and the page called it a success. The export knows what each
    # session's directory was, so the mapping is applied per session and the second import is as
    # correct as the first.
    origin = {}
    for sid, cwd in con.execute(
            f"SELECT session_id, cwd FROM src.sessions WHERE session_id IN ({marks})", ids):
        origin[sid] = cwd
    for sid, source_cwd in origin.items():
        dest_cwd = appstate.destination_cwd(source_cwd, mapping) if source_cwd else None
        if not dest_cwd or dest_cwd == source_cwd:
            continue
        moved["sessions"] += con.execute(
            "UPDATE sessions SET cwd = ? WHERE session_id = ?", (dest_cwd, sid)).rowcount
        moved["hook_events"] += con.execute(
            "UPDATE hook_events SET cwd = ? WHERE session_id = ?", (dest_cwd, sid)).rowcount

    # The transcript moves to whatever slug directory its bytes actually landed in, and only for
    # sessions this export carried a transcript FOR. A session with no carried transcript keeps the
    # source path, which is what makes `store.classify()` still call it imported.
    landed = {}
    for row in app_rows:
        if row["kind"] != appstate.TRANSCRIPT or "/" in row["relpath"]:
            continue
        if not row["relpath"].endswith(".jsonl"):
            continue
        sid = row["relpath"][: -len(".jsonl")]
        dest_cwd = appstate.destination_cwd(row["cwd"], mapping)
        landed[sid] = str(appstate.project_dir(dest_cwd) / row["relpath"])
    for sid, new_path in landed.items():
        old = con.execute("SELECT transcript_path FROM sessions WHERE session_id = ?",
                          (sid,)).fetchone()
        if not old or old[0] == new_path:
            continue
        # COPIED, NOT MOVED, and that is the whole of it. `tools/harvest.mjs` resumes each
        # transcript from `files.bytes_read` and treats a path with NO row as unread, so moving the
        # row left the ORIGINAL transcript, which an import never deletes, looking brand new. The
        # next harvest re-read it from zero and recreated every session row at the OLD working
        # directory, silently undoing the move the user asked for. Keeping both rows means neither
        # copy is re-read: the old path keeps its offset, the new path gets the same one.
        moved["files"] += con.execute(
            "INSERT OR IGNORE INTO files (path, size, mtime_ms, bytes_read, lines_read, rewrites, "
            "last_harvest_ts) SELECT ?, size, mtime_ms, bytes_read, lines_read, rewrites, "
            "last_harvest_ts FROM files WHERE path = ?", (new_path, old[0])).rowcount
        moved["transcripts"] += con.execute(
            "UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
            (new_path, sid)).rowcount
    return moved


def import_(path, into=None, dry_run=False):
    """Load an export into this store, verifying it first and inserting only what is missing.

    `INSERT OR IGNORE` on the primary key, so importing the same file twice is a no-op rather than a
    pile of duplicates. Columns are matched BY NAME against the local schema: an export from a build
    with an extra column loads what it can and reports what it left behind, instead of failing
    outright or, far worse, shifting every value one column to the left.

    THE DESTINATION IS THE CALLER'S CHOICE. `into` is a working directory on THIS machine, and
    everything is rebuilt from it: the slug directory, the `~/.claude.json` key, the desktop
    record, and the `cwd` in the rows. Nothing absolute from the source machine is used as a
    destination, so an import never reproduces the exporter's `C:\\Users\\them`. With no `into`,
    the export's own working directory is used, which is the same-machine case.

    What is NOT rewritten is the transcript's own bytes. A `.jsonl` records the working directory
    it ran in on every line, and editing that is surgery on the conversation which would also break
    the byte-identical guarantee the mirror rests on. The container is remapped around it instead,
    and `verify_mirror` compares those bytes unchanged.
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
    mapping, unmoved = destination_mapping(manifest, into)
    ids = list(manifest.get("session_ids") or [])
    app_rows = app_state_rows(path)

    # STILL EXCLUDED? Say so rather than fixing it silently. Importing a project the store is
    # excluding puts every row back and leaves harvest skipping the directory, so the sessions the
    # user has since run are never captured and nothing on the page connects the two facts.
    #
    # Reported, not lifted. An export from another machine can name a working directory that this
    # machine has deliberately excluded, and quietly resuming capture of a local directory because
    # a file mentioned it is a decision this function is not entitled to make. The caller offers it.
    destinations = sorted(set(mapping.values()))
    excluded_now = {e["cwd"] for e in excluded()}
    report = {"project": manifest.get("project"), "from": manifest.get("source_machine"),
              "into": destinations, "mapping": mapping, "not_moved": unmoved,
              "still_excluded": bool(excluded_now & (set(destinations)
                                                     | {manifest.get("project")})),
              "inserted": {}, "already_present": {}, "dropped_columns": {}}

    if dry_run:
        from c4x import appstate
        report["dry_run"] = True
        report["app_state"] = appstate.restore(app_rows, mapping, dry_run=True)
        return report

    with store.write() as con:
        con.execute("ATTACH DATABASE ? AS src", (str(path),))
        try:
            # The verified set, never manifest["tables"]. See carried_tables().
            #
            # APP_STATE_TABLE IS SKIPPED HERE ON PURPOSE. It is verified and digested with the
            # rest, and it is applied to the FILESYSTEM below rather than inserted: this store has
            # no such table, and copying it in would put whole transcripts in the database while
            # leaving `~/.claude/projects` empty, which is the failure the whole change exists to
            # end.
            for table in carried_tables(manifest):
                if table == APP_STATE_TABLE:
                    continue
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
            # INSIDE THE TRANSACTION, so a project half-moved between two working directories is
            # not a state this store can be left in.
            report["rebased_rows"] = _rebase_store_rows(con, ids, mapping, app_rows)
        except Exception:
            # ROLLBACK, NOT COMMIT. This was a bare `finally: con.commit()`, which ends the
            # transaction so DETACH can run and, in doing so, COMMITS a half-finished import.
            # `store.write()` documents that it rolls back on any exception and an unconditional
            # commit here overrode that promise. Demonstrated by an independent reviewer: a
            # failure mid-loop left 9 session rows behind with the rest of their tables missing.
            #
            # Predates the app-state work and outlives it: that feature was reverted, this was
            # not, because the defect and the fix are both independent of it.
            con.rollback()
            raise
        else:
            con.commit()
        finally:
            # THE TRANSACTION IS CLOSED EITHER WAY before this runs. An open one holds the
            # attached database and SQLite answers DETACH with "database src is locked" rather
            # than anything naming the cause.
            con.execute("DETACH DATABASE src")

    # THE FILESYSTEM, AFTER THE ROWS ARE COMMITTED. The two cannot be one transaction, so the
    # order is chosen for what a failure leaves behind: rows without files is a project c4x can
    # show and the desktop app cannot open, and re-running the import fixes it, because inserts
    # ignore what is already there and files are overwritten from the export either way.
    report["app_state"] = restore_app_state(path, mapping)
    report["mirror"] = verify_mirror(path, mapping=mapping)

    # THE SAME CACHES A DELETE CLEARS, for the same reason and in the other direction. An import
    # adds sessions and writes transcripts, and `store` holds the session frame and the transcript
    # scan for 45 seconds, so the imported project was absent from the page for up to that long
    # while the panel said it had landed. Only `delete` called this.
    from c4x import store as _s
    _s.invalidate()
    return report


def restore_app_state(path, mapping):
    """Write the four outside-the-store layers onto this machine.

    `mapping` is {source working directory: destination working directory}. A dict, never a name:
    `appstate._mapping` raises TypeError on anything else, which is the sixth and last guard added
    after a project LABEL reached a filesystem call for the sixth time.
    """
    from c4x import appstate
    return appstate.restore(app_state_rows(path), mapping)


def verify_mirror(path, into=None, mapping=None):
    """Does this machine hold what the export carries?

    THE ACCEPTANCE TEST FOR "COMPLETE MIRROR IMAGE", and it runs standalone against an export file
    so it can be run after the fact, by someone else, on the machine that received the import.

    `missing` is carried and absent. `differs` is carried and hashes differently. `extra` is in a
    slug directory this import wrote to and not in the export: REPORTED, never deleted, because
    that directory is shared with every other session that has the same working directory.

    A desktop record and a config entry are compared with their working-directory fields
    neutralised, because those two are what the import deliberately rewrites. Everything else in
    them is under the hash, so a renamed chat still fails.
    """
    manifest = read_manifest(path)
    if not manifest:
        return {"ok": False, "missing": [], "differs": [], "extra": [],
                "unresolved": [{"relpath": str(path), "why": "not a c4x export"}]}
    if mapping is None:
        mapping, _unmoved = destination_mapping(manifest, into)
    from c4x import appstate
    rows = app_state_rows(path)
    result = appstate.compare(rows, mapping)
    result["into"] = sorted(set(mapping.values()))
    result["not_carried"] = (manifest.get("app_state") or {}).get("not_carried") or []
    # AN EXPORT THAT CARRIES NO FILES IS NOT A FAILED MIRROR, it is a mirror question that does not
    # apply, and the difference is not academic. It was written when `delete` took its backup with
    # `app_state=False`, so every undo of a delete imported a rows-only export and answering with
    # ok=False made the documented recovery path report itself as a failure, exit non-zero, and
    # paint red on the page. `delete` now carries the files, because it now removes them, but a
    # rows-only export is still what `export(app_state=False)` writes and what every backup taken
    # before this change is, so the answer stays "not asked" rather than "no".
    #
    # The original complaint was still right: returning a bare ok=True over a file that carries
    # nothing reads as "this machine matches the export" when nothing was compared. So the answer
    # is neither yes nor no, it is "not asked", and `carries_no_files` says so to every caller.
    result["carries_no_files"] = not rows
    return result


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
            .pipe(records)
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


def delete(project, confirm, out_dir=None, keep_capturing=False, purge_snapshots=False):
    """Export, verify, then remove every layer, then stop capturing. First failure stops it.

    THE EXPORT COMES FIRST AND IS VERIFIED BEFORE ANYTHING IS REMOVED, so a delete is always
    undoable by importing the file it just wrote. If the export cannot be read back, nothing is
    deleted.

    THE ACCEPTANCE RULE IS ONE SENTENCE: a delete removes exactly what the backup contains, and
    nothing else. It is checkable in a way "we deleted the project" is not, and it is the inverse
    of the property the import already proves. `appstate.purge` enforces the first half by
    removing a file only when its bytes hash to what the backup holds; the two clauses below
    enforce the second.

    A SLUG DIRECTORY AND A CONFIG KEY BELONG TO A WORKING DIRECTORY, NOT TO A SESSION. When any
    session with this project's working directory survives the delete, which is what happens when
    an archived label is deleted and its base project stays, `memory/` and the `~/.claude.json`
    entry are LEFT and named. They are in the backup, so this is the one deliberate exception, and
    it exists because taking them would strip a project the user did not delete: measured on this
    machine, that case is 2,409 of 2,430 files.

    THE EXCLUSION FOLLOWS THE SAME RULE. Excluding a working directory stops the harvester
    capturing everything under it, so a cwd that still has sessions is not excluded, or deleting
    one archived label would silently stop capturing the live project sharing that directory.

    `confirm` must be the project path exactly. Not a yes/no: the whole risk here is deleting the
    wrong project, and a boolean cannot tell those apart.

    `purge_snapshots` reaches `data/snapshots`, which the backup does NOT carry, so it is the one
    thing here that importing the backup cannot put back. Off by default, counted and named either
    way: those files are the only copy of what a compaction dropped, and carrying them would turn
    this repo's own delete backup from about 2 MB into 1.1 GB.
    """
    from c4x import appstate, store
    if confirm != project:
        raise ValueError("confirmation does not match the project path; nothing was deleted")

    # THE CONFIG IS READ FIRST, BEFORE THE BACKUP IS EVEN WRITTEN. `read_config` raises on a file
    # it cannot parse, and one of the five layers this delete removes lives in that file. Finding
    # out afterwards would mean a backup that does not hold the project's trust and settings, and
    # a delete that cannot remove them either. It is also the cheapest possible refusal: no 694 MB
    # backup is written for a delete that is going to stop.
    #
    # The same fallback this replaces, `except ValueError: config = {}`, turned 26 project entries
    # into 1 on the test laptop over a byte order mark.
    appstate.read_config()

    # AND THE LABEL IS CHECKED BEFORE THE BACKUP IS WRITTEN, for the same reason: it is the
    # cheapest possible refusal, and a delete that cannot say which project it is about must not
    # start by writing a backup of both of them.
    _ro = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
    try:
        one_working_directory(_ro, project)
    finally:
        _ro.close()

    # NOT tmp/, AND NOT DERIVED FROM THE REPO ROOT. Two faults in one line.
    #
    # This wrote the only backup of a destructive operation into ROOT/tmp/exports, which .gitignore
    # calls "Scratch. Self-ignoring by convention" and which the download route sweeps: that route
    # now writes per-call uuid directories and removes them with a BackgroundTask, because one
    # shared directory had kept every export anyone ever asked for. So the one file that must
    # SURVIVE was living in the directory designed to be emptied, beside files that are deleted on
    # purpose, and the natural `rm -rf tmp/*` takes it.
    #
    # Deriving from the STORE rather than ROOT fixes a second fault at the same time. The API path
    # cannot pass out_dir, so every delete driven over HTTP wrote into the developer's real
    # directory, including the suite's: measured here, 181 files and 34 MB of P--Alpha backups from
    # tests/test_project_api.py, among which a user's real backup would be indistinguishable. With
    # the path following C4X_DB, a fixture run writes beside the fixture and production writes
    # beside production.
    from c4x import store as _store
    out_dir = Path(out_dir or (_store.DB_PATH.parent / "deleted-projects"))
    out_dir.mkdir(parents=True, exist_ok=True)
    backup = out_dir / file_name(project, stamped=True)
    # WITH THE FILES, because they are about to be removed and this is the only copy. A rows-only
    # backup was right while delete was store-only: nothing outside the store was going anywhere,
    # so carrying 694 MB protected against a removal that never happened. It is now the undo for
    # an actual removal, and a backup that does not hold what was deleted is not a backup.
    #
    # Nothing here calls `claude project purge`, which is what blocked this: that command matches
    # by slug PREFIX, so deleting `L:\\Books` also takes `L--Books-Courses`, and 21 of the 518 slug
    # directories on this machine are a prefix of another. `appstate.project_dir(cwd)` is an exact
    # lookup and takes none of them.
    manifest = export(project, backup, app_state=True)     # raises if it cannot be verified

    with store.write() as con:
        # THE SET THE BACKUP HOLDS, not a fresh resolution. This called `session_ids` again here,
        # so a harvest landing between the export and this transaction added a session that was
        # then deleted while absent from the only copy of it. For a large project the export runs
        # for minutes, which is plenty of window.
        ids = list(manifest.get("session_ids") or [])
        if not ids:
            raise ValueError(f"no sessions with cwd {project!r}")
        appeared = [s for s in session_ids(con, project) if s not in set(ids)]
        # WHICH TRANSCRIPT FILES THIS PROJECT SHARES WITH ANOTHER, asked BEFORE the rows go,
        # because afterwards there is nothing left to join on. `tools/harvest.mjs:1351` abandons a
        # whole file as soon as the first record carrying a cwd names an excluded directory, so the
        # exclusion's real unit is the FILE and the survivor test below asks about the directory.
        # Measured on this store: 7 transcript files hold two sessions each and 2 of them span two
        # working directories, so excluding one would stop capturing a project this delete did not
        # touch, silently and permanently.
        marks_all = ",".join("?" * len(ids))
        shared_transcripts = [
            {"cwd": a_cwd, "with_cwd": b_cwd, "transcript": path}
            for a_cwd, b_cwd, path in con.execute(
                f"""SELECT DISTINCT a.cwd, b.cwd, a.transcript_path
                      FROM sessions a JOIN sessions b ON a.transcript_path = b.transcript_path
                     WHERE a.session_id IN ({marks_all})
                       AND b.session_id NOT IN ({marks_all})
                       AND a.transcript_path IS NOT NULL AND b.cwd IS NOT NULL""",
                ids + ids).fetchall()]
        shares_a_file = {entry["cwd"] for entry in shared_transcripts}
        marks = ",".join("?" * len(ids))

        # THE ROW LAYER HAD NO ACCEPTANCE CHECK. `appeared` closes the export/delete race at
        # SESSION granularity, and the DELETEs below run at delete time against a backup taken at
        # export time, so anything a concurrent harvest wrote for a session ALREADY in `ids` was
        # removed and is in no copy. The window is the whole of the app-state capture plus
        # `verify`, which for a large project is minutes, and three processes write this store by
        # design. `hook_events` is the class with no recovery path either: its watermark row is not
        # deleted, so the log line ends up behind it and the row cannot be rebuilt from anything.
        #
        # The manifest already carries the number that catches it. Refusing is right rather than
        # deleting the extra rows: the backup is the undo, and a row the backup does not hold has
        # no undo.
        carried: dict = manifest.get("counts") or {}
        moved: dict = {}

        def _moved(table, live):
            if table in carried and live != carried[table]:
                moved[table] = {"backup": carried[table], "live": live}

        for table in BY_SESSION:
            _moved(table, con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id IN ({marks})", ids).fetchone()[0])
        for table in BY_COMPACTION:
            _moved(table, con.execute(
                f"""SELECT COUNT(*) FROM {table} WHERE compaction_uuid IN
                    (SELECT uuid FROM compactions WHERE session_id IN ({marks}))""",
                ids).fetchone()[0])
        for table in BY_TRANSCRIPT:
            _moved(table, con.execute(
                f"""SELECT COUNT(*) FROM {table} WHERE path IN
                    (SELECT transcript_path FROM sessions WHERE session_id IN ({marks})
                      AND transcript_path IS NOT NULL)""", ids).fetchone()[0])
        if moved:
            raise ValueError(
                "the store changed while the backup was being written, so the backup no longer "
                "holds what this delete would remove: "
                + "; ".join(f"{table} has {n['live']} rows and the backup carries {n['backup']}"
                            for table, n in sorted(moved.items()))
                + ". Nothing was deleted. Run it again and it will back up what is there now.")

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
        # WHAT STILL LIVES IN THIS DIRECTORY, asked AFTER the rows are gone, so the answer is
        # about survivors rather than about the set being deleted. It decides both of the
        # working-directory clauses in this function's docstring.
        # NO FALLBACK TO THE LABEL. This read `manifest.get("cwds") or [project]`, and `project`
        # is a page label: for sessions with no working directory recorded it is
        # `<slug> (no working directory recorded)`. That string was then handed to
        # `appstate.purge`, whose `_cwds` guard exists to refuse exactly a label and accepts it
        # because it is a non-empty string, and written into `excluded_projects`, where no `d.cwd`
        # can ever equal it, so the exclusion never fires and the sessions return on the next
        # harvest with their offset row already removed.
        cwds: list[str] = manifest.get("cwds") or []
        unlocated = not cwds
        survivors = sessions_with_cwds(con, cwds)
        # TWO SURVIVOR TESTS, BECAUSE THE TWO SHARED LAYERS ARE KEYED DIFFERENTLY. The config entry
        # is keyed by the exact working directory string; `memory/` is keyed by the slug directory,
        # and many strings map to one slug. Using the string test for both let a delete strip a
        # live project's memory and then report that it had kept it.
        slug_survivors = sessions_sharing_slug(con, cwds)
        # THE THIRD KEYING. Rows follow the session id, `memory/` follows the slug directory, and
        # the `~/.claude.json` entry follows the working directory string as `normalised` folds it.
        surviving_norm = surviving_normalised_cwds(con)
        surviving_slug_set = surviving_slugs(con)
        excluded_cwds: list[str] = []
        still_captured: list[str] = []
        if not keep_capturing:
            # THE WORKING DIRECTORIES, NOT THE LABEL. This wrote `project`, and a project's label
            # is `<cwd>\archived` whenever the desktop app archived its chats, which 764bc0b
            # deliberately made selectable. `tools/harvest.mjs` excludes by cwd and NO cwd ends in
            # that suffix, so deleting an archived project never stuck: the next harvest put every
            # row back while this function reported `excluded: True`. Measured on this store, 16 of
            # the labels the menu offers are of that shape.
            ensure_exclusions(con)
            for cwd in cwds:
                # NOT EVERY CARRIED cwd. An exclusion is by directory and the harvester skips
                # everything under it, so excluding one that still has sessions would stop
                # capturing a project this delete did not touch.
                if sessions_with_cwds(con, [cwd]):
                    still_captured.append(cwd)
                    continue
                if cwd in shares_a_file:
                    # No session of this directory is left, but a transcript FILE it wrote also
                    # holds another project's session, and the harvester drops the whole file.
                    still_captured.append(cwd)
                    continue
                excluded_cwds.append(cwd)
                con.execute("INSERT OR REPLACE INTO excluded_projects (cwd, excluded_at, note) "
                            "VALUES (?,?,?)",
                            (cwd, datetime.now(UTC).isoformat(timespec="seconds"),
                             f"deleted, exported to {backup.name}"))

    # THE MOMENT THE ROWS ARE COMMITTED, not at the end. Everything below this line can raise,
    # and when it did the caches still held the deleted project: the page went on drawing rows that
    # were already gone, for up to the full 45 second ttl, on exactly the runs where something had
    # gone wrong and the user most needed the page to be true.
    store.invalidate()

    # THE FILES, after the rows. A file removal that fails partway leaves rows already gone and
    # files still there, which one import of the backup puts back; the other order leaves rows
    # pointing at transcripts that are not there any more.
    rows = app_state_rows(backup, with_blobs=False)

    def _belongs_to_a_survivor(row):
        """Per ROW, and each kind by the key it is actually filed under.

        Deciding this per KIND was wrong twice over. `memory/` is one directory shared by every
        spelling that slugs to it, so it is kept when any session still slugs there. A config entry
        is one key per spelling, and the capture selects keys by `normalised`, so each carried key
        is kept only when a session still lives in the directory THAT key names.
        """
        if row["kind"] == appstate.MEMORY:
            # THIS ROW'S OWN DIRECTORY. `bool(slug_survivors)` was one answer for every carried
            # working directory at once, so a project spanning two slug directories kept the
            # memory of the one with no survivor and called it deliberate.
            try:
                return appstate.slug_for(row["cwd"]).casefold() in surviving_slug_set
            except (TypeError, ValueError):
                return False
        if row["kind"] == appstate.CONFIG:
            try:
                return appstate.normalised(row["cwd"]) in surviving_norm
            except (TypeError, ValueError):
                return False
        return False

    shared_kept = [{"relpath": row["relpath"], "kind": row["kind"]}
                   for row in rows if _belongs_to_a_survivor(row)]
    rows = [row for row in rows if not _belongs_to_a_survivor(row)]
    purged = appstate.purge(rows, cwds)

    # The stems of transcripts a surviving session is also in. A snapshot of one of those files
    # holds that session's history too, and the backup does not carry snapshots.
    shared_stems = {Path(entry["transcript"]).stem for entry in shared_transcripts}
    snapshots = snapshot_files(ids, shared_stems)
    snapshot_report = {"files": len(snapshots), "removed": 0,
                       "bytes": sum(p.stat().st_size for p in snapshots)}
    if purge_snapshots:
        for path in snapshots:
            path.unlink()
            if path.exists():
                raise RuntimeError(f"{path} was removed and is still there")
            snapshot_report["removed"] += 1

    return {"project": project, "backup": str(backup), "removed": removed,
            "excluded": bool(excluded_cwds), "excluded_cwds": excluded_cwds,
            # Directories this delete deliberately kept capturing, because sessions it did not
            # delete are still in them.
            "still_captured": still_captured,
            # No session under this label has a working directory recorded, so there is no
            # directory to purge and no exclusion that harvest could ever match.
            "unlocated": unlocated,
            # Transcript files this project shared with another working directory. The harvester
            # abandons a file, not a session, so these are why a directory can be left capturing.
            "shared_transcripts": shared_transcripts,
            "exported_sessions": manifest["sessions"],
            "removed_files": len(purged["removed"]), "removed_bytes": purged["bytes"],
            "kept_files": purged["kept"], "refused_files": purged["refused"],
            "config_keys_removed": purged["config_keys"],
            "config_keys_kept": purged["config_kept"],
            "pruned_dirs": purged["pruned"],
            # The one exception to "everything the backup holds": layers keyed by the working
            # directory, left because a session this delete did not take still lives there.
            "shared_with_surviving_sessions": shared_kept,
            "surviving_sessions": sorted(survivors),
            # WHAT THE EXPORT COULD NOT CARRY, carried through to the delete's own report. These
            # files are still on disk and the backup does not hold them, so they are the one thing
            # here that "removes exactly what the backup contains" does not account for. The import
            # already surfaces the same field; the delete did not, so a file the backup skipped
            # was left behind and named nowhere.
            "not_carried": (manifest.get("app_state") or {}).get("not_carried") or [],
            "too_large": (manifest.get("app_state") or {}).get("too_large") or [],
            # A file the export could not READ. Same class as the two above and it was dropped:
            # the backup does not hold it, the delete did not remove it, and nothing said so.
            "skipped": (manifest.get("app_state") or {}).get("skipped") or [],
            # Directory walks the prune refused because it could not prove where to stop.
            "prune_refused": purged["prune_refused"],
            # Sessions in the same SLUG directory under a different working directory string. They
            # are why `memory/` can be kept when `surviving_sessions` is empty.
            "sessions_sharing_slug": sorted(set(slug_survivors) - set(survivors)),
            "snapshots": snapshot_report,
            # THE ACCEPTANCE TEST, re-resolved from the backup rather than from the bookkeeping
            # above, so a purge that reported a removal it did not make is caught here. Empty is
            # the only good answer.
            "still_here": appstate.still_present(rows, cwds),
            # Sessions that arrived between the backup and the delete. They are NOT deleted, and
            # naming them is the difference between a race and a silent loss.
            "appeared_since_backup": appeared}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _refuses(call, argument):
    """True when `call(argument)` raises TypeError, which is what a guard is FOR."""
    try:
        call(argument)
    except TypeError:
        return True
    except Exception:
        return False
    return False


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

    # THE APP-STATE TABLE HAS TO SATISFY THREE THINGS AT ONCE, and each of the three has already
    # broken a build on its own: absent from `verify`'s allow-list, every fresh export fails its
    # own verification; present in one of the three project lists, `import_` inserts whole
    # transcripts into a table that has no such columns; missing from `unhandled_tables`' known
    # set, every export reports its own table as unhandled.
    cases.append(("the app-state table is not treated as a project table",
                  APP_STATE_TABLE not in
                  (set(BY_SESSION) | set(BY_COMPACTION) | set(BY_TRANSCRIPT) | set(STORE_WIDE))))
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / "known.db"
        con = sqlite3.connect(str(probe))
        con.execute(f"CREATE TABLE {APP_STATE_TABLE} (kind TEXT)")
        con.commit()
        cases.append(("an export does not report its own app-state table as unhandled",
                      unhandled_tables(con) == []))
        con.close()

    from c4x import appstate as _appstate
    cases.append(("a project label cannot reach a filesystem call",
                  _refuses(_appstate._cwds, r"P:\Books\archived")))
    cases.append(("a mapping cannot be a project label either",
                  _refuses(_appstate._mapping, r"P:\Books\archived")))

    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def _print_mirror(result):
    """The mirror verdict, and the exit code that goes with it.

    NON-ZERO ON A DIFFERENCE. "imported" printed above a non-empty `differs` is exactly the claim
    this whole change exists to stop, and an exit code is the half a script can act on.
    """
    if not result:
        print("  mirror  NOT CHECKED")
        return 0
    for entry in result.get("missing") or []:
        print(f"  MISSING  {entry['kind']}  {entry['relpath']}")
    for entry in result.get("differs") or []:
        print(f"  DIFFERS  {entry['kind']}  {entry['relpath']}")
    for entry in result.get("unresolved") or []:
        print(f"  UNRESOLVED  {entry['relpath']}: {entry['why']}")
    extra = result.get("extra") or []
    if extra:
        print(f"  {len(extra)} file(s) here that the export does not carry, left untouched")
    not_carried = result.get("not_carried") or []
    if not_carried:
        print(f"  {sum(n['files'] for n in not_carried)} file(s) the EXPORT did not carry, "
              "belonging to sessions it has no rows for")
    if result.get("carries_no_files"):
        # EXIT 0, like the other NOT CHECKED branch six lines above. A rows-only export is what
        # `delete` writes, so this is the undo path, and a non-zero exit told every script that
        # recovering a deleted project had failed.
        print("  mirror  NOT CHECKED: this export carries rows only, so there are no files to "
              "compare. The rows imported; the transcripts were never in this file.")
        return 0
    # NOT "byte for byte", WHICH WAS NOT TRUE OF ALL FIVE KINDS. Transcripts, memory and tasks land
    # byte-identical. A config entry and a desktop record are the two things an import deliberately
    # rewrites, so they are identical in content with the working directory replaced, and a real
    # record grew from 181,366 to 190,303 bytes on being re-serialised. Claiming the stronger
    # property of all of them was an overclaim an independent reviewer measured.
    print("  mirror  " + ("OK: files identical, config and desktop record rebased"
                          if result.get("ok") else "NOT A MIRROR"))
    return 0 if result.get("ok") else 1


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
    p_import.add_argument("--into", default=None,
                          help="the working directory to import INTO on this machine; defaults to "
                               "the one the export came from. Everything is rebuilt from it: the "
                               "slug directory, the config key, the desktop record, the rows.")
    p_import.add_argument("--dry-run", action="store_true",
                          help="name every destination and write nothing")

    p_mirror = sub.add_parser("verify-mirror",
                              help="does this machine hold what an export carries?")
    p_mirror.add_argument("path")
    p_mirror.add_argument("--into", default=None)

    p_delete = sub.add_parser("delete", help="export, then remove, then stop capturing")
    p_delete.add_argument("project")
    p_delete.add_argument("--confirm", default="", help="the project path, exactly")
    p_delete.add_argument("--keep-capturing", action="store_true",
                          help="do not exclude it, so the next harvest brings it back")
    p_delete.add_argument("--purge-snapshots", action="store_true",
                          help="also remove this project's pre-compaction snapshots. They are the "
                               "only copy of what a compaction dropped, the backup does not carry "
                               "them, and importing the backup cannot put them back.")
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
        for cwd in manifest["cwds"]:
            print(f"    working directory  {cwd}")
        for table, n in manifest["counts"].items():
            print(f"    {table:22} {n:>8,}")
        state = manifest["app_state"]
        print(f"    {'files carried':22} {state['files']:>8,}  "
              f"({state['bytes'] / 1048576:.1f} MB)  " + ", ".join(
                  f"{k} {n}" for k, n in sorted(state["by_kind"].items()) if n))
        if state["not_carried_files"]:
            print(f"    NOT CARRIED  {state['not_carried_files']:,} file(s) belonging to sessions "
                  "this export has no rows for")
        for skip in state["skipped"]:
            print(f"    SKIPPED  {skip['path']}: {skip['why']}")
        for big in state["too_large"]:
            print(f"    TOO LARGE  {big['path']}: {big['bytes']:,} bytes")
        return 0
    if args.command == "import":
        report = import_(args.path, into=args.into, dry_run=args.dry_run)
        print(f"  {report['project']}  from {report['from']}")
        for source_cwd, dest_cwd in sorted(report["mapping"].items()):
            arrow = "  stays at  " if source_cwd == dest_cwd else "  INTO  "
            print(f"    {source_cwd}{arrow}{dest_cwd}")
        for cwd in report.get("not_moved") or []:
            print(f"    LEFT WHERE IT WAS  {cwd}  (not under the project's own directory)")
        if report.get("dry_run"):
            for entry in report["app_state"]["written"]:
                # A CONFIG ROW MERGES ONE KEY AND NEVER REWRITES THE FILE. Printing "overwrites"
                # beside `~/.claude.json`, which is what this said, describes losing every other
                # project's settings and the machine's credentials. It does neither.
                if entry["kind"] == "config":
                    mark = "adds a key to"
                elif entry["exists"]:
                    mark = "overwrites   "
                else:
                    mark = "creates      "
                print(f"    {mark}  {entry['path']}")
            for refused in report["app_state"]["refused"]:
                print(f"    REFUSED  {refused['relpath']}: {refused['why']}")
            print(f"  {len(report['app_state']['written'])} file(s) would be written, "
                  "and nothing was")
            return 0
        for table, n in report["inserted"].items():
            already = report["already_present"].get(table, 0)
            print(f"    {table:22} {n:>8,} inserted, {already:,} already present")
        for table, columns in report.get("dropped_columns", {}).items():
            print(f"    NOT LOADED  {table}: {', '.join(columns)}")
        state = report.get("app_state") or {}
        print(f"    {'files restored':22} {len(state.get('written') or []):>8,}  "
              f"({(state.get('bytes') or 0) / 1048576:.1f} MB)")
        # NAMED SEPARATELY BECAUSE IT IS NOT A FILE. The config entry is a key merged into a shared
        # file, so it is not in `written`, and leaving it unprinted meant the one thing a user
        # actually notices, being asked to trust the directory again, had nothing reporting it.
        for key in state.get("config_keys") or []:
            print(f"    trust and settings carried for  {key}")
        for entry in state.get("replaced_shorter") or []:
            print(f"    SHORTER NOW  {entry['path']}  {entry['was']:,} -> {entry['now']:,} bytes")
        for refused in state.get("refused") or []:
            print(f"    REFUSED  {refused['relpath']}: {refused['why']}")
        for record in state.get("desktop") or []:
            print(f"    desktop record  {record['path']}")
        return _print_mirror(report.get("mirror") or {})
    if args.command == "verify-mirror":
        return _print_mirror(verify_mirror(args.path, into=args.into))
    if args.command == "delete":
        result = delete(args.project, args.confirm, args.out_dir, args.keep_capturing,
                        args.purge_snapshots)
        print(f"  exported to {result['backup']} before deleting")
        for table, n in result["removed"].items():
            print(f"    {table:22} {n:>8,} removed")
        print(f"    {'files removed':22} {result['removed_files']:>8,}  "
              f"({result['removed_bytes'] / 1048576:.1f} MB)")
        for key in result.get("config_keys_removed") or []:
            print(f"    trust and settings removed for  {key}")
        for entry in result.get("kept_files") or []:
            print(f"    KEPT  {entry['path']}: {entry['why']}")
        for entry in result.get("config_keys_kept") or []:
            print(f"    KEPT  {entry['key']}: {entry['why']}")
        for entry in result.get("refused_files") or []:
            print(f"    REFUSED  {entry['relpath']}: {entry['why']}")
        shared = result.get("shared_with_surviving_sessions") or []
        if shared:
            print(f"  {len(shared)} file(s) and config entries LEFT: "
                  f"{len(result['surviving_sessions'])} session(s) still have this working "
                  "directory, and memory and trust settings belong to the directory")
        snapshots = result.get("snapshots") or {}
        if snapshots.get("files"):
            if snapshots.get("removed"):
                print(f"  {snapshots['removed']} pre-compaction snapshot(s) removed, "
                      f"{snapshots['bytes'] / 1048576:.1f} MB, NOT in the backup")
            else:
                print(f"  {snapshots['files']} pre-compaction snapshot(s) kept, "
                      f"{snapshots['bytes'] / 1048576:.1f} MB. --purge-snapshots removes them")
        for cwd in result.get("excluded_cwds") or []:
            print(f"  excluded from future harvests: {cwd}")
        for cwd in result.get("still_captured") or []:
            print(f"  STILL CAPTURED: {cwd} has sessions this delete did not take")
        if not result["excluded"] and not result.get("still_captured"):
            print("  still being captured, so the next harvest brings it back")
        for sid in result.get("appeared_since_backup") or []:
            print(f"  KEPT, arrived after the backup was written and is not in it: {sid}")
        # NON-ZERO WHEN SOMETHING THE DELETE ASKED TO REMOVE IS STILL THERE. Printing the list
        # under a zero exit is the same defect as "imported" printed above a non-empty `differs`.
        for entry in result.get("not_carried") or []:
            print(f"  NOT CARRIED  {entry['files']:,} file(s)  {entry['path']}: {entry['why']}")
        for entry in result.get("too_large") or []:
            print(f"  TOO LARGE  {entry['bytes']:,} bytes  {entry['path']}")
        for entry in result.get("skipped") or []:
            print(f"  SKIPPED  {entry.get('path', entry)}: {entry.get('why', '')}")
        for entry in result.get("prune_refused") or []:
            print(f"  PRUNE REFUSED  {entry['path']}: {entry['why']}")
        for entry in result.get("still_here") or []:
            # A REFUSED ROW HAS NO PATH, so this printed the word None and nothing else useful.
            where = entry.get("path") or entry.get("relpath")
            why = entry.get("why")
            print(f"  STILL HERE  {entry['kind']}  {where}" + (f": {why}" if why else ""))
        return 1 if result.get("still_here") else 0
    if args.command == "include":
        print(f"  removed {include(args.project)} exclusion(s) for {args.project}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Reading the store, and the window math the reads depend on.

Read-only by construction: nothing in this module writes, because the dashboard never does. Only
harvest.mjs and the hooks write to the store.

It knows what a session is and nothing about how one is drawn, which makes it the opposite half of
theme.py and leaves neither importing the other.

The one thing to know before changing a query here: **sum api_calls, never turns.** A streamed
assistant message is written as several transcript rows sharing one request id, so summing turns
counts the same API call two to eight times.
"""
import contextlib
import glob
import json
import os
import re
import sqlite3
import subprocess
import time as _time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
# C4X_DB, the same override every node tool honours through paths.mjs. That module exists because
# some tools read the variable and others hardcoded the default, so `C4X_DB=copy.db` silently read
# one store and wrote another. The Python side never got the same treatment and ignored the
# variable outright, which meant the dashboard and the audit could only ever be run against the
# real store: pointing them at a fixture, the way CI does, was impossible.
DB_PATH = Path(os.environ.get("C4X_DB") or (ROOT / "data" / "context.db"))
HOME = str(Path.home())

# Window resolution spawns node, so it is cached per session for a short ttl to keep that
# spawn off the per-tick path. Same idiom as _rows_cache above it.
_window_cache: dict = {}


# ---------------------------------------------------------------------------
# Single source of truth for the math: read it out of the JS module.
# ---------------------------------------------------------------------------
def _node_json(script: str):
    """Run a node snippet that prints JSON, return the parsed value."""
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"node exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"node produced no stdout. stderr: {proc.stderr.strip()[:400]}")
    return json.loads(out)


def load_math():
    """Constants and thresholds, straight from tools/mirror-core.mjs.

    A Windows absolute path is not a legal import specifier, so the path is converted to a
    file:// URL before import.
    """
    core = (ROOT / "tools" / "mirror-core.mjs").as_uri()
    script = (
        f"import({json.dumps(core)}).then(m => {{"
        "  const ws = [200000, 500000, 967000, 1000000];"
        "  console.log(JSON.stringify({"
        "    K: m.K,"
        "    thresholds: ws.map(w => ({"
        "      window: w,"
        "      compact: m.reportedAutoCompactThreshold(w),"
        "      warn: m.reportedAutoCompactThreshold(w) - m.K.WARN_OFFSET,"
        "      blocked: w - m.K.COMPACT_BUFFER"
        "    }))"
        "  }));"
        "}).catch(e => { console.error(e.message); process.exit(1); });"
    )
    return _node_json(script)


def _node_json_argv(args, timeout=120):
    """Run a node script that prints JSON on stdout, return the parsed value."""
    proc = subprocess.run(["node", *args], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"node {args[0]} exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    if not proc.stdout.strip():
        raise RuntimeError(
            f"node {args[0]} produced no stdout. stderr: {proc.stderr.strip()[:400]}")
    return json.loads(proc.stdout)


def predict(tokens: int, window: int):
    """Ask tools/mirror.mjs, so the answer is the validated implementation's answer."""
    proc = subprocess.run(
        ["node", str(ROOT / "tools" / "mirror.mjs"),
         "--predict", str(int(tokens)), "--window", str(int(window))],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mirror.mjs exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Data access. Reads go through q(), which is read-only. Writes go through write(), which is not,
# and that is the entire list of ways this package can change the store.
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def write():
    """A read-write connection, for the few operations that are allowed to change the store.

    SEPARATE FROM `q` ON PURPOSE, and not merely because the mode string differs. Every read in this
    package opens `mode=ro`, so until now "does anything here write?" was answerable by reading one
    function. Keeping the write path its own named function keeps that property: `store.write` is
    greppable, and anything that does not call it cannot write.

    Used by `c4x/projects.py` and nothing else. Deleting a project and importing one are deliberate,
    user-initiated operations; the refresh tick is not one of them, which is what `C4X_READ_ONLY`
    governs and why that flag is about HARVESTING rather than about writing in general.

    Committed on a clean exit and rolled back on any exception, because the operations that use this
    span several tables with no foreign keys to tidy up after a half-finished one.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"No store at {DB_PATH}.")
    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute("PRAGMA foreign_keys = ON")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def q(sql: str, params=()) -> pd.DataFrame:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No store at {DB_PATH}. Run `node tools/harvest.mjs` first."
        )
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def overview_stats() -> dict:
    """Headline figures, each one tied to a stated population.

    These cards used to mix populations silently. `turns` counted every transcript row, which
    includes subagent work AND counts one streamed assistant message two to eight times, while
    `output tokens` came from the deduped `api_calls` view and the Breakdown tab charted only
    non-sidechain calls. Three different denominators on one dashboard, none of them labelled,
    and the raw count sat under the words "deduped by uuid" - true of harvest-time uuid dedup,
    and the exact misreading the README warns about under "Token numbers look about twice too
    high". So the API-call count leads now, and the transcript row count is shown beside it as
    what it is.

    The api_calls figures come from ONE pass. Each subquery against that view is a full GROUP BY
    over every turn, so asking it five separate questions would have cost five scans.
    """
    small = q("""
        SELECT (SELECT COUNT(*) FROM sessions)                     AS sessions,
               (SELECT COUNT(*) FROM turns)                        AS turn_rows,
               (SELECT COUNT(*) FROM compactions)                  AS compactions,
               (SELECT SUM(summary_uuid IS NULL) FROM compactions) AS unpaired,
               (SELECT COUNT(*) FROM files)                        AS files,
               (SELECT SUM(bytes_read) FROM files)                 AS bytes
    """).iloc[0].to_dict()
    calls = q("""
        SELECT COUNT(*)                                                     AS api_calls,
               SUM(CASE WHEN COALESCE(is_sidechain,0)=0 THEN 1 ELSE 0 END)   AS main_calls,
               SUM(COALESCE(output_tokens,0))                                AS out_tokens,
               SUM(COALESCE(cache_read_input_tokens,0))                      AS cache_read,
               SUM(COALESCE(input_tokens,0) + COALESCE(cache_creation_input_tokens,0)
                   + COALESCE(cache_read_input_tokens,0) + COALESCE(output_tokens,0)) AS billed,
               MAX(total_resident)                                           AS peak
        FROM api_calls
    """).iloc[0].to_dict()
    return {**small, **calls}


HOME_DIR = str(Path.home())


def project_label(cwd, slug) -> str:
    """What to CALL a project.

    `project_slug` is a filesystem-safe encoding of the working directory, not a name: `P--Books`,
    `C--Users-Administrator`. It was being printed straight into the picker. It is also lossy and
    ambiguous, so this is not a decoding problem: `subagents` is not a path at all, it is the folder
    subagent transcripts are written to, and in this store it maps to 30 different working
    directories. `cwd` holds the real path for 1,320 of 1,323 sessions, so the real path wins and
    the slug is only ever a last resort for the 3 that have none.
    """
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    if isinstance(slug, str) and slug.strip():
        return f"{slug.strip()} (no working directory recorded)"
    return "(unknown)"


_rows_cache = {"at": 0.0, "df": None}


def session_rows(ttl: float = 45.0) -> pd.DataFrame:
    """Cached wrapper. The uncached query is a GROUP BY over every turn in the store.

    scoped() calls this on every query once cohorts exist, so without a cache a single page render
    would run that aggregate a dozen times. The ttl is short enough that a live session appears
    within a tick or two and long enough that one render costs one query.
    """
    now = _time.time()
    if _rows_cache["df"] is not None and now - _rows_cache["at"] < ttl:
        return _rows_cache["df"]
    df = _session_rows_uncached()
    _rows_cache["at"] = now
    _rows_cache["df"] = df
    return df


ARCHIVED_SUFFIX = "archived"

# Enough of a session record to reach isArchived, which sits near the top of a file whose bulk is
# an enabledMcpTools map. Measured across the 188 records on this machine: the field is inside the
# first 8 KB of 59 of the 60 sampled, and the parser falls back to the whole file for the rest, so
# the bound is an optimisation and never a source of a wrong answer.
_HEAD_BYTES = 8192
_CLI_ID = re.compile(r'"cliSessionId"\s*:\s*"([0-9a-fA-F-]{36})"')
_ARCHIVED = re.compile(r'"isArchived"\s*:\s*(true|false)')

_archived_cache = {"map": None, "at": 0.0, "root": None}


def sessions_root():
    """Where the desktop app keeps its per-chat records."""
    return os.path.join(os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming"),
                        "Claude", "claude-code-sessions")


def read_archived_record(path):
    """(cli session id, archived) for one record file, or None when it is not a session record.

    Reads a bounded prefix first. If either field is missing from it the whole file is parsed, so a
    record that happens to order its keys differently is answered correctly rather than skipped.
    That directory also holds scheduled-tasks.json files, which carry neither field and are not
    session records; those return None rather than counting as a failed read.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    cli, arch = _CLI_ID.search(head), _ARCHIVED.search(head)
    if cli and arch:
        return cli.group(1), arch.group(1) == "true"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("cliSessionId"):
        return None
    return str(record["cliSessionId"]), bool(record.get("isArchived"))


def archived_sessions(root=None, ttl: float = 45.0) -> dict:
    """{session id: archived} for every chat the desktop app has a record of.

    Keyed by cliSessionId, which is what this store calls session_id. The desktop app's own
    sessionId is a different namespace entirely (`local_<uuid>`), and matching on it finds almost
    nothing, which is what made this look unreadable the first time.
    """
    root = root or sessions_root()
    now = _time.time()
    if (_archived_cache["map"] is not None and _archived_cache["root"] == root
            and now - _archived_cache["at"] < ttl):
        return _archived_cache["map"]
    found = {}
    for path in glob.glob(os.path.join(root, "*", "*", "*.json")):
        row = read_archived_record(path)
        if row is not None:
            found[row[0]] = row[1]
    _archived_cache.update({"map": found, "at": now, "root": root})
    return found


_transcript_cache = {"ids": None, "at": 0.0}


def transcript_ids(ttl: float = 45.0):
    """Every session id that has a transcript under ~/.claude/projects, as a set.

    Built with one scandir per project directory rather than a glob per session. The glob form
    expanded its star into all 510 project directories and stat'ed the candidate inside each, so
    checking 48 sessions cost 24,480 stat calls and 5.3 seconds, which was most of the time the
    sessions query took. This costs one pass and answers every session from memory.

    The ttl matches session_rows(), so a transcript written mid-render shows up a tick later rather
    than never.
    """
    now = _time.time()
    if _transcript_cache["ids"] is not None and now - _transcript_cache["at"] < ttl:
        return _transcript_cache["ids"]
    ids = set()
    root = os.path.join(HOME, ".claude", "projects")
    try:
        projects = list(os.scandir(root))
    except OSError:
        projects = []
    for project in projects:
        if not project.is_dir():
            continue
        try:
            for entry in os.scandir(project.path):
                if entry.name.endswith(".jsonl"):
                    ids.add(entry.name[: -len(".jsonl")])
        except OSError:
            continue          # a directory that vanished between the two scans is simply absent
    _transcript_cache["ids"] = ids
    _transcript_cache["at"] = now
    return ids



def _import_dates(session_ids) -> dict:
    """When this store ingested each session's transcript, keyed by session id.

    files.last_harvest_ts is the only ingest time recorded anywhere: `sessions` has first_ts and
    last_ts, which are when the CONVERSATION ran on its own machine, not when it arrived here. For
    a transcript that is no longer readable on this machine the last harvest of it is the import.
    """
    if not session_ids:
        return {}
    out = {}
    for chunk in range(0, len(session_ids), 500):     # SQLITE_MAX_VARIABLE_NUMBER is 999
        ids = list(session_ids)[chunk:chunk + 500]
        rows = q(f"""
            SELECT s.session_id, f.last_harvest_ts AS at
              FROM sessions s
              JOIN files f ON f.path = s.transcript_path
             WHERE s.session_id IN ({','.join('?' * len(ids))})
               AND f.last_harvest_ts IS NOT NULL
        """, ids)
        out.update(dict(zip(rows["session_id"], rows["at"], strict=True)))
    return out


def _title_or_name(title, section, imported_at) -> str:
    """The stored title, or a name for a session that can never have one.

    The name carries the ingest date only. It once carried the last-updated date too, which every
    caller already shows beside it, and the duplication is what made five identical-looking rows
    read as a naming collision when the labels differed by the minute all along.

    Only imported sessions get the generated name. A local session with no title is a different
    fact and must not be labelled "Imported", which would be a claim about where it came from
    rather than a note that it is nameless.
    """
    if isinstance(title, str) and title.strip():
        return title.strip()
    if section == "Imported from another machine" and imported_at:
        return f"Imported_{_ymd(imported_at)}"
    return "(untitled)"


def _ymd(ts) -> str:
    """YYYYMMDD off an ISO timestamp, or 'unknown' if there is nothing to read."""
    text = str(ts or "")[:10].replace("-", "")
    return text if len(text) == 8 and text.isdigit() else "unknown"


def _session_rows_uncached() -> pd.DataFrame:
    """Every session worth picking, with the name it goes by and the section it belongs to.

    Ordered the way the desktop sidebar orders: section, then project, then most recently active
    first. The old picker sorted 1,323 sessions by peak tokens, which interleaved every project and
    made the list unreadable.
    """
    df = q("""
        SELECT t.session_id,
               s.cwd, s.project_slug, s.entrypoint, s.transcript_path,
               COALESCE(s.last_ts, MAX(t.ts))                AS last_ts,
               COUNT(*)                                      AS turns,
               MAX(t.total_resident)                         AS peak,
               (SELECT x.total_resident FROM turns x
                 WHERE x.session_id = t.session_id
                 ORDER BY x.ts DESC LIMIT 1)                  AS current,
               (SELECT COUNT(*) FROM compactions c
                 WHERE c.session_id = t.session_id) AS compactions,
               (SELECT title FROM session_titles st WHERE st.session_id = t.session_id
                 ORDER BY CASE st.kind WHEN 'custom' THEN 0 WHEN 'ai' THEN 1
                          ELSE 2 END LIMIT 1) AS title,
               (SELECT kind FROM session_titles st WHERE st.session_id = t.session_id
                 ORDER BY CASE st.kind WHEN 'custom' THEN 0 WHEN 'ai' THEN 1
                          ELSE 2 END LIMIT 1) AS title_kind
        FROM turns t
        LEFT JOIN sessions s ON s.session_id = t.session_id
        -- Where the window sits NOW, which is what the header reports; peak is the high-water
        -- mark. Showing only peak made the two disagree for one session with nothing saying which
        -- was which, and the gap between them is what a compaction took out.
        --
        -- Read off `turns`, not off the api_calls view. That view groups per request id, so a
        -- window function over it materialises every row and costs 22 seconds where the indexed
        -- base table costs 0.1. The dedup that view exists for matters when SUMMING, because a
        -- streamed message writes several rows sharing one request id; it does not matter for one
        -- latest value, since those rows repeat total_resident.
        --
        -- That last sentence is checked rather than assumed. The first attempt compared the two
        -- forms against the LIVE store and three sessions disagreed, which turned out to be the
        -- three being written to while the comparison ran. On a frozen fixture they agree exactly,
        -- and the fixture now contains streamed messages, so the comparison had something to
        -- disagree about.
        GROUP BY t.session_id
        HAVING COUNT(*) >= 5
    """)
    if df.empty:
        return df

    def classify(r):
        """Which section a session belongs to. Every test is answerable from disk.

        No Archived section, but not for the reason this comment used to give. It claimed the flag
        lived in the desktop app's IndexedDB and "not in any readable file". It is a plain JSON
        file per chat under %APPDATA%/Claude/claude-code-sessions, carrying isArchived beside a
        cliSessionId that IS this store's session_id. Being wrong about that is what kept it off
        the page.

        It stays out of the SECTIONS because a section implies a partition, and this flag is a
        tri-state: archived, not archived, and no record. It is shown on the path instead, where
        the unmarked case is honest about meaning "not known".
        """
        path = r.transcript_path
        # The stored path is whatever file was harvested for this session LAST, and that can be a
        # subagent transcript, or one written under a project slug the working directory has since
        # moved away from. Either way it can be absent while the session itself is very much here,
        # which filed the live session under "Deleted from this machine". Its own transcript is the
        # thing that answers the question, so look for that first.
        if isinstance(path, str) and path and not os.path.exists(path):
            if r.session_id in transcript_ids():
                path = None
        if isinstance(path, str) and path and not os.path.exists(path):
            # Absent because it was written on another machine, or absent because it was deleted
            # here. Those are different facts and must not share a label.
            elsewhere = HOME_DIR.lower() not in path.replace("/", "\\").lower()
            return ("Imported from another machine" if elsewhere
                    else "Deleted from this machine")
        if isinstance(r.entrypoint, str) and r.entrypoint and r.entrypoint != "claude-desktop":
            return "CLI and SDK"
        return "Projects"

    df["section"] = df.apply(classify, axis=1)
    # Archived chats get a path of their own, which is what puts them together and away from the
    # rest under a sort by project. The flag is only knowable for the sessions the desktop app has
    # a record of, so `archived` is a tri-state: True, False, or None for "no record". None is NOT
    # folded into False, because the page says how many are unknown and that number would be a lie.
    flags = archived_sessions()
    df["archived"] = [flags.get(sid) for sid in df["session_id"]]
    df["project"] = [
        project_label(c, s) + ("\\" + ARCHIVED_SUFFIX if archived else "")
        for c, s, archived in zip(df["cwd"], df["project_slug"], df["archived"], strict=True)
    ]
    # A session with no title of any kind says so, rather than showing an empty cell that reads
    # like a rendering fault. An imported one gets a name instead, because it is not merely
    # untitled, it is untitleable: every title in this store was read out of a transcript record by
    # backfillTitles(), which walks the transcripts on THIS machine, and an imported session's
    # transcript is on the other one. The absence that classifies it as imported is the same
    # absence that puts it out of the titler's reach, so waiting for a later harvest to name it
    # would be waiting forever.
    imported_at = _import_dates(list(df["session_id"]))
    df["title"] = [
        _title_or_name(t, section, imported_at.get(sid))
        for t, section, sid
        in zip(df["title"], df["section"], df["session_id"], strict=True)
    ]
    order = {"Projects": 0, "CLI and SDK": 1, "Imported from another machine": 2,
             "Deleted from this machine": 3}
    df["_sec"] = df["section"].map(order).fillna(9)
    df = df.sort_values(["_sec", "project", "last_ts"],
                        ascending=[True, True, False], kind="mergesort")
    return df.drop(columns=["_sec"])


def session_turns(session_id: str, include_sidechain: bool = False) -> pd.DataFrame:
    """Turn records for one session.

    Sidechain is EXCLUDED by default, and that default is now stated on the page rather than
    applied silently. Subagent work is 70% of the API calls in this store, so a chart that
    quietly folded it in beside main-thread turns was answering a question nobody asked, while
    a chart that quietly dropped it looked like the whole session.
    """
    where = "" if include_sidechain else "AND COALESCE(is_sidechain,0) = 0"
    return q(f"""
        SELECT uuid, ts, model, input_tokens, cache_creation_input_tokens, cache_read_input_tokens,
               output_tokens, thinking_tokens, total_resident, is_sidechain
        FROM turns WHERE session_id = ? {where} ORDER BY ts
    """, (session_id,))


def session_survivors(session_id: str) -> pd.DataFrame:
    """Turns that lived through a compaction in this session.

    Only assistant turns can be matched, since those are the only uuids the store holds. The
    survivor set also names user and attachment records, which stay unmatched by design.
    """
    return q("""
        SELECT v.compaction_uuid, v.kind, v.uuid, t.ts, t.total_resident
        FROM compaction_survivors v
        JOIN compactions c ON c.uuid = v.compaction_uuid
        LEFT JOIN turns t ON t.uuid = v.uuid
        WHERE c.session_id = ?
    """, (session_id,))


def session_compactions(session_id: str) -> pd.DataFrame:
    return q("""
        SELECT ts, trigger, pre_tokens, post_tokens, cumulative_dropped_tokens,
               duration_ms, version, summary_chars
        FROM compactions WHERE session_id = ? ORDER BY ts
    """, (session_id,))


COHORT_ALL = "__all__"


def cohort_options() -> list:
    """The populations worth asking a question about.

    A single session answers "what happened here". A cohort answers "what is true of this kind of
    work", which is the question a research tool exists for: every session in a project, everything
    that ran outside the desktop app, everything imported from another machine.

    Built from the same frame the picker uses, so a cohort can never contain a session the picker
    does not list, and the counts shown here are the counts a tab will describe.
    """
    df = session_rows()
    opts = [{"label": f"All sessions ({len(df):,})", "value": COHORT_ALL}]
    if df.empty:
        return opts
    for sec, n in df["section"].value_counts().items():
        opts.append({"label": f"Section: {sec} ({n:,})", "value": f"section::{sec}"})
    for proj, n in df["project"].value_counts().head(40).items():
        opts.append({"label": f"Project: {proj} ({n:,})", "value": f"project::{proj}"})
    return opts


def cohort_parts(cohort) -> tuple:
    """Split a cohort string into (kind, value), or ("", "") if it names nothing.

    Pulled out of `cohort_sessions` so that anything needing to know WHICH project a cohort refers
    to reads the same rule rather than splitting the string again. That mattered once already: the
    frontend sent a bare path with no `project::` prefix, which resolves here to no restriction, and
    the difference between "this project" and "everything" showed up only as a wrong session count.
    A delete cannot afford the same mistake.
    """
    kind, sep, value = str(cohort or "").partition("::")
    if not sep or kind not in ("section", "project") or not value:
        return "", ""
    return kind, value


def cohort_sessions(cohort) -> list:
    """Resolve a cohort to the session ids it contains. Empty list means 'no restriction'."""
    if not cohort or cohort == COHORT_ALL:
        return []
    df = session_rows()
    if df.empty:
        return []
    kind, value = cohort_parts(cohort)
    col = {"section": "section", "project": "project"}.get(kind)
    if not col:
        return []
    return list(df.loc[df[col] == value, "session_id"])


def population_label(session_id, cohort, scope) -> str:
    """One sentence naming exactly what is being described, for the page to print.

    Every aggregate states its population. That is not decoration: the complaint that started this
    restructure was not knowing whether a number covered everything, the newest thing, or the thing
    selected, and a count with no denominator is how that happens.
    """
    side = "subagents included" if scope == "all" else "main thread only"
    if session_id:
        return f"1 session, {side}"
    ids = cohort_sessions(cohort)
    if ids:
        kind, _, value = str(cohort).partition("::")
        return f"{len(ids):,} sessions in {kind} {value}, {side}"
    return f"the whole store, every session, {side}"


def scoped(session_id, scope="main", alias="", cohort=None):
    """SQL fragment and params for the header selection.

    Returned as a pair rather than interpolated by each caller, so a tab cannot accidentally scope
    on a different column or forget the sidechain filter. `alias` is the table alias, empty for an
    unaliased FROM.

    A single session wins over a cohort: picking one session while a cohort is set means "this one",
    not "this one and everything like it". The cohort still narrows the picker, so the two stay
    consistent.
    """
    a = f"{alias}." if alias else ""
    bits, args = [], []
    if session_id:
        bits.append(f"AND {a}session_id = ?")
        args.append(session_id)
    else:
        ids = cohort_sessions(cohort)
        if ids:
            bits.append(f"AND {a}session_id IN ({','.join('?' * len(ids))})")
            args.extend(ids)
    if scope != "all":
        bits.append(f"AND COALESCE({a}is_sidechain,0) = 0")
    return " ".join(bits), tuple(args)


def all_compactions(session_id=None, cohort=None) -> pd.DataFrame:
    # scope="all": a compaction is a property of the session, not of one thread inside it, so the
    # sidechain filter does not apply to this table.
    where, args = scoped(session_id, "all", alias="c", cohort=cohort)
    return q(f"""
        SELECT c.uuid, c.ts, c.trigger, c.version,
               COALESCE(NULLIF(s.cwd,''), s.project_slug, '(unknown)') AS project,
               c.pre_tokens, c.post_tokens, c.cumulative_dropped_tokens AS dropped,
               c.duration_ms,
               (SELECT t.model FROM turns t
                 WHERE t.session_id = c.session_id AND t.ts <= c.ts
                 ORDER BY t.ts DESC LIMIT 1) AS model,
               (SELECT COUNT(*) FROM compaction_survivors v
                 WHERE v.compaction_uuid = c.uuid) AS survivors
        FROM compactions c LEFT JOIN sessions s ON s.session_id = c.session_id
        WHERE c.pre_tokens IS NOT NULL {where}
        ORDER BY c.pre_tokens DESC
    """, args)


def compaction_summary_text(compaction_uuid: str) -> pd.DataFrame:
    """The summary a compaction produced, as prose.

    Until the messages table existed this was only ever a character count, so the page could tell
    you 14,115 chars had replaced 981k tokens without showing you a word of it.
    """
    return q(
        """
        SELECT m.text, m.chars, m.ts
        FROM compactions c JOIN messages m ON m.uuid = c.summary_uuid
        WHERE c.uuid = ?
        """,
        (compaction_uuid,),
    )


def compaction_dropped(compaction_uuid: str, limit: int = 300) -> pd.DataFrame:
    """Messages from before a compaction that are absent from its survivor list.

    A LOWER BOUND in both directions, and labelled as one in the UI. Survivor uuids that the store
    holds no message for cannot be matched, and a message with no readable text was never stored,
    so this lists what can be shown to have gone rather than everything that went.
    """
    return q(
        """
        SELECT m.uuid, m.ts, m.role, m.type, m.chars,
               substr(replace(replace(m.text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM compactions c
        JOIN messages m ON m.session_id = c.session_id AND m.ts < c.ts
        WHERE c.uuid = ?
          AND m.uuid NOT IN (SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
          AND m.uuid <> COALESCE(c.summary_uuid, '')
        ORDER BY m.chars DESC
        LIMIT ?
        """,
        (compaction_uuid, limit),
    )


def compaction_dropped_count(compaction_uuid: str) -> int:
    """How many dropped messages EXIST, as opposed to how many the table shows.

    compaction_dropped() caps its result, and reporting the capped length as the count states the
    limit as though it were a finding.
    """
    df = q(
        """
        SELECT COUNT(*) AS n
        FROM compactions c
        JOIN messages m ON m.session_id = c.session_id AND m.ts < c.ts
        WHERE c.uuid = ?
          AND m.uuid NOT IN (SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
          AND m.uuid <> COALESCE(c.summary_uuid, '')
        """,
        (compaction_uuid,),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def session_messages(session_id: str, limit: int = 400) -> pd.DataFrame:
    return q(
        """
        SELECT uuid, ts, role, type, chars,
               substr(replace(replace(text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM messages WHERE session_id = ? ORDER BY ts LIMIT ?
        """,
        (session_id, limit),
    )


def message_text(uuid: str) -> pd.DataFrame:
    return q("SELECT text, chars, ts, role, type FROM messages WHERE uuid = ?", (uuid,))


def load_compaction_windows():
    """Window per compaction, resolved by model segment in one node pass.

    The window is a property of the model in use, not of the session, so a session that switched
    models has more than one. Resolved once at startup rather than per row.
    """
    return _node_json_argv([str(ROOT / "tools" / "segments.mjs"), "--windows-for-compactions"])


def segments_for(session_id: str):
    return _node_json_argv([str(ROOT / "tools" / "segments.mjs"), "--session", session_id])


MATH = load_math()


THRESHOLDS = {t["window"]: t for t in MATH["thresholds"]}


COMPACTION_WINDOWS = load_compaction_windows()


def fit_window(pre_tokens: int):
    """Pick the candidate window whose compact threshold is the largest at or below pre_tokens."""
    best = None
    for t in sorted(MATH["thresholds"], key=lambda x: -x["compact"]):
        if pre_tokens >= t["compact"]:
            best = t
            break
    if best is None:
        best = min(MATH["thresholds"], key=lambda x: x["compact"])
    return best["window"], pre_tokens - best["compact"]


def session_window(session_id: str, ttl: float = 60.0):
    """The window a session is actually running, resolved from evidence and cached.

    A model-name lookup is not sufficient: claude-opus-5 is listed in SMALL_WINDOW_MODELS, yet
    this build demonstrably runs it at 1M, and the proof is the session's own compaction and peak.
    tools/segments.mjs already performs that reasoning, so it is asked rather than reimplemented -
    once per session per ttl, to keep a node spawn off the per-tick path.
    """
    hit = _window_cache.get(session_id)
    now = _time.time()
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    window, confidence = None, "unresolved"
    try:
        info = segments_for(session_id)
        segs = [s for s in info.get("segments", []) if s.get("window")]
        if segs:
            window = segs[-1]["window"]
            confidence = segs[-1].get("confidence") or "segment"
    except Exception:                               # noqa: BLE001 - unresolved is a valid answer
        pass
    _window_cache[session_id] = (now, window, confidence)
    return window, confidence


# Pseudo-models that appear in the transcript but are not models anyone ran. Printing <synthetic>
# beside claude-opus-5 in a MODELS card invites the reader to think they used two. segments.mjs
# already treats it as not-a-model-switch; this is the display half of the same rule.
SYNTHETIC_MODELS = {"<synthetic>", "synthetic", "", None}


def real_models(values) -> list:
    seen = []
    for m in values:
        if m in SYNTHETIC_MODELS or (isinstance(m, float) and pd.isna(m)):
            continue
        if m not in seen:
            seen.append(m)
    return sorted(seen)

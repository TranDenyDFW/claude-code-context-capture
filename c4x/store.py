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
from typing import Any, TypedDict

import pandas as pd

from c4x.labels import distinct_short_paths, is_folderless, plural, titled_path

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
        # BEGIN IMMEDIATE, so a READ inside this block is inside the transaction too. Python's
        # sqlite3 defers the BEGIN until the first statement that changes something, so every
        # SELECT a writer makes first ran in autocommit: a caller that checked the store and then
        # deleted on the strength of that check had an open window between the two, and this store
        # has three writers by design. Taking the write lock up front closes it, at the cost of
        # making two concurrent writers serialise, which is what they should do.
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# FEWER THAN THIS MANY TRANSCRIPT ROWS AND A SESSION IS NOT LISTED. The rule's one home:
# session_rows() applies it, overview_stats() counts by it, the sessions tab prints it, and the
# tests import it. tests/test_floor.py reads the source and fails on any per-session HAVING clause
# that spells the number out instead, because that is how a rule drifts: one site moves and the
# others keep the old number, and the page shows two totals that no longer reconcile.
#
# Why it exists: most sessions in a store are one-shot runs from a benchmark harness or a hook
# probe, a few rows each, and listing them buries every real session. When this was measured
# (2026-08, one store of 1,325 sessions) 1,008 were below the line, 985 of them with between one
# and four rows.
#
# It is a PRESENTATION rule and nothing else may treat it as the population. Anything that has to
# cover every session, an export or a delete, must union the listed sessions with the ones this
# hides; c4x/projects.py does, and it was carrying 58 of one project's 137 sessions until it did.
SESSION_TURN_FLOOR = 5


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


def measured_cost(session_ids=None) -> dict:
    """What Claude Code itself says these sessions cost, or an empty answer.

    THE FIRST FIGURE IN THIS APP THAT IS A COST RATHER THAN A PRICING OF TOKENS. c4x/pricing.py
    derives money from tokens and a committed table; this reads the total Claude Code recorded in
    its own cost-state record. They are shown side by side and never merged, because the estimate
    is a stated LOWER BOUND and the measured figure can itself be incomplete.

    GUARDED, and the guard is the point. `cost_state` is created by harvest.mjs, and this package
    never writes, so a store harvested by an older build has no such table and a bare query would
    RAISE rather than return nothing. That exact defect shipped once already, on record_types.

    SUMMING ACROSS SESSIONS IS SAFE, and it was worth checking, because a cumulative total that
    survived a fork would double-count under exactly this SUM. Read out of the Claude Code 2.1.250
    binary: the writer is gated on `costLedger.belongsTo(currentSessionId)`, ownership is a single
    field compared by identity, and `restoreSnapshot` restores the owner FROM THE SNAPSHOT rather
    than re-owning it to the resuming session. So a forked or resumed session does not own the
    inherited ledger and emits no record at all. Scope of that evidence: a read of a minified
    binary, not an executed fork, so if a future build re-owns on restore this SUM is the thing
    that breaks, and the sessions count beside it is what would still be right.

    Returns sessions_with_a_record, total_usd, and incomplete, the count of sessions Claude Code
    flagged with hasUnknownModelCost. `total_usd` is None when nothing was found, never 0.0: zero
    is a claim that these sessions were free.
    """
    empty = {"sessions": 0, "total_usd": None, "incomplete": 0}
    if not tables_present("cost_state"):
        return empty
    sql = ("SELECT COUNT(*) AS sessions, SUM(total_cost_usd) AS total_usd, "
           "SUM(COALESCE(has_unknown_model_cost, 0)) AS incomplete FROM cost_state")
    params: tuple = ()
    if session_ids is not None:
        ids = list(session_ids)
        if not ids:
            # An optimisation, NOT the correctness guard. SQLite accepts `IN ()` and returns no
            # rows, so deleting this changes no answer; the guard that matters is `is not None`
            # above, which keeps an empty selection from meaning "no filter". Measured by mutating
            # both, see tests/test_cost_state.py.
            return empty
        sql += f" WHERE session_id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    df = q(sql, params)
    if df.empty:
        return empty
    row = df.iloc[0]
    n = int(row["sessions"] or 0)
    return {"sessions": n,
            "total_usd": float(row["total_usd"]) if n and row["total_usd"] is not None else None,
            "incomplete": int(row["incomplete"] or 0)}


def column_present(table: str, column: str) -> bool:
    """Whether a table has a column yet.

    SAME RULE AS tables_present, ONE LEVEL DOWN, and it exists for a reason worth stating: this
    package never writes, so it cannot run the migration that adds a column. `harvest.mjs` adds
    one when it next runs, which may be minutes or days after the code that reads it ships, and
    until then a query naming that column does not return an empty frame, it RAISES.

    Measured: adding `known` to record_types and reading it here turned the Sources tab into an
    exception panel and took two suite legs down with it, on a store that was perfectly healthy and
    had simply not been harvested since. The column existed on this machine within the hour only
    because a hook fired a harvest, which is exactly the kind of luck a gate must not depend on.
    """
    if not DB_PATH.exists():
        return False
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return any(r[1] == column for r in con.execute(f"PRAGMA table_info({table})").fetchall())
    except sqlite3.Error:
        return False
    finally:
        con.close()


# ---- What a tool call turned out to be -------------------------------------------------------
#
# `is_error` meant two opposite things at once: a tool that RAN AND FAILED, and a tool that NEVER
# RAN because something refused it. Measured on this store after the backfill: of 6,871 flagged
# calls, 1,848 are refusals (26.9%), 3,945 are genuine failures, and 1,078 predate the field that
# would settle it. Per tool it is far worse, because refusal is not evenly spread: of the 41 flagged
# ExitPlanMode calls, this store can PROVE none ran and failed: 13 carry a denial kind and the
# other 28 predate the field. Reading the result text of all 39 that could be matched: 23 say
# the user did not want to proceed and 2 are permission failures, both of which are calls that
# NEVER RAN, and exactly ONE is a genuine tool error. The plan said 3 and two rounds of commit
# messages repeated it without measuring. The claim here is about what is provable.
#
# harvest.mjs records the answer per call; this is the one place that reads it, so no surface can
# invent a second definition.

#: The vocabulary harvest.mjs writes. Mirrored here rather than imported, because that is a JS
#: module, and pinned by a test that reads the distinct values out of a real store.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_REFUSED = "refused"
OUTCOME_UNCLASSIFIED = "unclassified"


def outcome_available() -> bool:
    """Whether this store has been harvested since the outcome columns arrived."""
    return (column_present("tool_calls", "outcome")
            and column_present("tool_calls", "denial_kind"))


def outcome_sums(alias: str = "") -> str:
    """The three SELECT-list expressions every surface counts outcomes with.

    ONE FRAGMENT, SO SIX SITES CANNOT DISAGREE. They already had: one wrote
    `SUM(CASE WHEN is_error THEN 1 ELSE 0 END)` and the others `SUM(COALESCE(is_error, 0))`, and a
    seventh place retyped a paraphrase of the query for the reader that matched none of them.

    ON AN UNMIGRATED STORE THIS COUNTS EVERYTHING AS UNKNOWN, and that is deliberate rather than
    defensive. Returning zeros would render a blank column, which is exactly what a table where
    everything succeeded looks like, so the page would state the strongest possible claim on the
    weakest possible evidence. Saying "N unknown" is true, is visible, and names the command that
    fixes it. Same reasoning as the empty-frame guard on session_tool_calls, one level up.
    """
    where = f"{alias}." if alias else ""
    if not outcome_available():
        return "0 AS errors, 0 AS refused, COUNT(*) AS unknown"
    return (f"SUM(CASE WHEN {where}outcome = 'error' THEN 1 ELSE 0 END) AS errors, "
            f"SUM(CASE WHEN {where}outcome = 'refused' THEN 1 ELSE 0 END) AS refused, "
            f"SUM(CASE WHEN {where}outcome IS NULL OR {where}outcome = 'unclassified' "
            f"THEN 1 ELSE 0 END) AS unknown")


def outcome_text(errors=0, refused=0, unknown=0) -> str:
    """The merged cell, and an EMPTY STRING when there is nothing to say.

    A zero is noise. On a table of forty tools most rows have no failures at all, and forty cells
    reading "0 errors" is forty cells of nothing dressed as a measurement.

    THE WORDS ARE LONG ON PURPOSE, and this is not style. tools/table_audit.py fails any cell
    matching a number followed by up to four letters, so "3 err" would be reported as a number
    stored as text while "3 errors" is not; and the bare word "unknown" is one of that audit's
    placeholder strings, so the count always leads. Mirrors outcomeText in tools/outcomes.mjs.
    """
    parts = []
    for n, one, many in ((errors, "error", "errors"),
                         (refused, "refused", "refused"),
                         (unknown, "unknown", "unknown")):
        n = int(n or 0)
        if n > 0:
            parts.append(f"{n:,} {one if n == 1 else many}")
    return ", ".join(parts)


def fold_outcomes(df):
    """Replace the three counted columns with one merged `outcome`, keeping the numbers.

    APPLIED IMMEDIATELY AFTER THE QUERY, so no caller can render the three raw columns by
    forgetting to. The three survive as hidden columns so the numbers still reach a row click and
    the CSV, which `evidence_block` asks for with export_columns="all".

    WHAT THEY DO NOT SURVIVE AS IS SORTABLE. A hidden column has no header, so nothing in the
    browser can order by it, and the merged cell is text: it sorts lexicographically, putting
    "3 errors" above "36 refused" above "9 refused". The plan promised sorting as well; it was not
    deliverable behind a hidden column and saying so is cheaper than a reader discovering it.

    Inserted where `errors` sat, so column order is unchanged for every caller.
    """
    # NO getattr HERE. It was written defensively, and tools/table_audit.py reports any call whose
    # callee it cannot name from the source, because a dynamic call is exactly how a table-building
    # path evades that scan. The defensiveness bought nothing either: every caller passes a frame
    # straight from q(), which always returns one, so this asks the question directly.
    if df is None or df.empty or "errors" not in df.columns:
        return df
    out = df.copy()
    at = list(out.columns).index("errors")
    merged = [outcome_text(e, r, u) for e, r, u
              in zip(out["errors"], out["refused"], out["unknown"], strict=True)]
    for name in ("errors", "refused", "unknown"):
        if name in out.columns:
            out = out.drop(columns=[name])
    out.insert(at, "outcome", merged)
    # The numbers, kept and hidden. The table builder declares them in `hidden_columns`.
    out["errors"] = list(df["errors"])
    out["refused"] = list(df["refused"])
    out["unknown"] = list(df["unknown"])
    return out


#: What fold_outcomes leaves behind for a DataTable to hide.
OUTCOME_HIDDEN = ("errors", "refused", "unknown")


def tables_present(*names) -> bool:
    """Whether EVERY named table exists. One round trip, no query against the tables themselves.

    For a family of tables that is created together and is meaningless apart. `tools/probe.mjs`
    writes `probes` and its three child tables in one `ensureProbeSchema`, so a store either has
    the set or has none of it - but nothing enforces that, and guarding only the parent query left
    the children to raise. Proved by dropping one child from a fixture that had probe rows: the
    Window tab's Configuration panel went straight back to an exception panel.
    """
    if not DB_PATH.exists():
        return False
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return set(names) <= have


def q_optional(sql: str, params=(), columns=()) -> pd.DataFrame:
    """`q`, but a table that was never created reads as "nothing here yet".

    FIVE TABLES IN THIS SCHEMA ARE NOT CREATED BY THE INSTALL PATH. `probes`, `probe_categories`,
    `probe_details` and `probe_message_breakdown` come from `tools/probe.mjs`; `context_baselines`
    comes from `tools/breakdown.mjs --calibrate`. Nothing in `install.mjs`, the hooks, or the
    README runs either - the README does not contain the word "probe" - so on every fresh install
    those tables do not exist, and a bare `q` against one raises `no such table` and takes the tab
    down with it. Four call sites had a written empty-state branch sitting BELOW the query that
    raised before it could be reached.

    NOT FOLDED INTO `q` ITSELF, deliberately. `q`'s FileNotFoundError above is a contract every
    other tab depends on, and a missing HARVEST table means the store is broken and must stay
    loud. Only the caller knows which of its tables are optional, so only the caller opts in.

    Anything that is not a missing table re-raises: a typo in a column name must not read as an
    empty result, which is how a silent wrong answer gets shipped.
    """
    try:
        return q(sql, params)
    except FileNotFoundError:
        raise
    except Exception as exc:                       # noqa: BLE001 - narrowed by the message below
        if "no such table" not in str(exc).lower():
            raise
        return pd.DataFrame(columns=list(columns))


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
               -- How many of those the picker and All sessions actually LIST. Shown beside the
               -- total because the page shows both numbers and called them both "sessions",
               -- leaving a reader to reconcile 1,325 against 317 with nothing to go on. The
               -- threshold is SESSION_TURN_FLOOR, explained where it is defined.
               (SELECT COUNT(*) FROM (SELECT session_id FROM turns
                                       GROUP BY session_id HAVING COUNT(*) >= ?)) AS listed,
               (SELECT COUNT(*) FROM turns)                        AS turn_rows,
               (SELECT COUNT(*) FROM compactions)                  AS compactions,
               (SELECT SUM(summary_uuid IS NULL) FROM compactions) AS unpaired,
               (SELECT COUNT(*) FROM files)                        AS files,
               (SELECT SUM(bytes_read) FROM files)                 AS bytes
    """, (SESSION_TURN_FLOOR,)).iloc[0].to_dict()
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


class _RowsCache(TypedDict):
    """The session frame and when it was read.

    A TypedDict rather than a bare literal because `{"at": 0.0, "df": None}` infers as
    `dict[str, float | None]`, which makes `now - _rows_cache["at"]` a float minus an optional and
    the returned frame a float. Six of this module's type errors were that one inference.
    """

    at: float
    df: Any          # pandas.DataFrame, which has no usable stub here


_rows_cache: _RowsCache = {"at": 0.0, "df": None}

# BUMPED BY `invalidate`, AND CHECKED BY EVERY CACHED READER BEFORE IT INSTALLS WHAT IT READ. A
# clear alone is not enough: a reader that started before the delete finishes after it, and writes
# the answer it computed from the pre-delete store into the slot the delete just emptied. The
# window is the whole length of the uncached read, which for the session frame is an aggregate
# over every turn in the store. Three processes write this store by design, so the race is
# reachable rather than theoretical.
_generation = {"n": 0}


def session_rows(ttl: float = 45.0) -> pd.DataFrame:
    """Cached wrapper. The uncached query is a GROUP BY over every turn in the store.

    scoped() calls this on every query once cohorts exist, so without a cache a single page render
    would run that aggregate a dozen times. The ttl is short enough that a live session appears
    within a tick or two and long enough that one render costs one query.
    """
    now = _time.time()
    if _rows_cache["df"] is not None and now - _rows_cache["at"] < ttl:
        return _rows_cache["df"]
    seen = _generation["n"]
    df = _session_rows_uncached()
    if seen == _generation["n"]:
        _rows_cache["at"] = now
        _rows_cache["df"] = df
    return df


def invalidate():
    """Forget every cached read of the store and of this machine's Claude directory.

    A REMOVAL THAT DOES NOT CALL THIS LEAVES THE ROW ON THE PAGE. Four caches in this module hold a
    45 second answer and nothing cleared any of them, so a project deleted from the page stayed
    drawn until the ttl expired. That reads as "the delete did not work" and invites a second one.

    `_transcript_cache` is the one that is worse than cosmetic. It answers "does this session still
    have a transcript", and `classify` turns that into the label a session is filed under, so a set
    scanned before a removal keeps sessions in the wrong section of the page. It is also the
    predicate any future prune would delete on, which is why it is cleared rather than left to
    expire, but nothing on this branch deletes from it.

    Clearing all four rather than the one that changed, because working out which cache a given
    removal invalidated is exactly the reasoning that gets a cache wrong. The refills are not all
    equally cheap: `_rows_cache` costs one aggregate over `turns`, `_transcript_cache` and
    `_archived_cache` one directory scan each, and `_window_cache` spawns node per session, which
    is why it is cached at all. A removal is rare enough to pay for all four, and a page that draws
    a project the user just deleted costs more than a subprocess does.
    """
    _generation["n"] += 1
    _rows_cache.update({"at": 0.0, "df": None})
    _archived_cache.update({"map": None, "at": 0.0, "root": None, "sig": None})
    _transcript_cache.update({"ids": None, "at": 0.0})
    _window_cache.clear()
    # A fifth, and unlike the four above it is not a 45 second answer: whether the open store is a
    # redacted copy is a property of the file and changes only if the file is replaced. It is
    # cleared here anyway, because the cost is one `sqlite_master` read and the alternative is a
    # memo that can outlive the store it describes.
    _redacted_cache.clear()


ARCHIVED_SUFFIX = "archived"

# Enough of a session record to reach isArchived, which sits near the top of a file whose bulk is
# an enabledMcpTools map. Measured across the 188 records on this machine: the field is inside the
# first 8 KB of 59 of the 60 sampled, and the parser falls back to the whole file for the rest, so
# the bound is an optimisation and never a source of a wrong answer.
_HEAD_BYTES = 8192
_CLI_ID = re.compile(r'"cliSessionId"\s*:\s*"([0-9a-fA-F-]{36})"')
_ARCHIVED = re.compile(r'"isArchived"\s*:\s*(true|false)')


def _top_level_string(head, wanted):
    r"""The value of a TOP-LEVEL string key inside a bounded prefix, or None.

    A REGEX WOULD BE WRONG HERE, and that is the one way `title` differs from the two fields above.
    "cliSessionId" and "isArchived" occur once in a record and match a fixed shape, so a first match
    is the match. A title is neither unique nor shaped, and it fails in two distinct ways.

    THE ONE THAT BITES TODAY IS ESCAPING. Record 3c886370 is titled `Claude asking questions on
    "yours to call"`, which is stored with escaped quotes, and `"title"\s*:\s*"([^"]*)"` returns
    `Claude asking questions on \` for it. Measured across the 180 titled records here that is the
    single disagreement between a naive regex and the whole-file parse, and this scanner agrees
    with `json.load` on all 180.

    THE ONE THAT IS LATENT IS NESTING. 129 of the 194 files carry more than one `"title"` key; the
    extras are MCP tool schemas several levels down
    (`remoteMcpServersConfig[].tools[].inputSchema.properties.title`). The chat's own title happens
    to be written first in every record that has one, so on ordering alone a regex is right today
    and would start returning a tool's parameter name the first time a record is serialised in a
    different order, with nothing to say it had.

    So the prefix is walked once, string-aware and brace-aware, and only a key at depth 1 counts.
    A string the prefix cut in half returns None rather than a truncated answer, which is what
    sends the caller to the whole-file parse.
    """
    depth, i, n, pending = 0, 0, len(head), None
    while i < n:
        ch = head[i]
        if ch == '"':
            start = i
            i += 1
            while i < n:
                if head[i] == "\\":
                    i += 2
                    continue
                if head[i] == '"':
                    break
                i += 1
            if i >= n:
                # The bound landed inside a string. Nothing after it can be trusted either, since
                # this scanner no longer knows whether it is inside quotes.
                return None
            raw = head[start:i + 1]
            i += 1
            after = i
            while after < n and head[after] in " \t\r\n":
                after += 1
            if after < n and head[after] == ":":
                pending = raw if depth == 1 else None
                i = after + 1
                continue
            if pending is not None and depth == 1:
                try:
                    if json.loads(pending) == wanted:
                        return json.loads(raw)
                except ValueError:
                    return None
            pending = None
            continue
        if ch in "{[":
            depth += 1
            pending = None
        elif ch in "}]":
            depth -= 1
            pending = None
        i += 1
    return None

class _ArchivedCache(TypedDict):
    """What the desktop app records say about each chat, and where that was read from.

    One entry per chat: (archived, title). It holds BOTH fields rather than one because the scan
    that fills it is the expensive part, and the two callers that want those fields want them for
    the same rows on the same render. Reading the directory twice to answer two questions about the
    same 194 files is the mistake this shape exists to prevent.
    """

    map: dict[str, tuple[bool, str | None]] | None
    at: float
    root: str | None
    sig: tuple[int, int] | None


_archived_cache: _ArchivedCache = {"map": None, "at": 0.0, "root": None, "sig": None}


def _claude_appdata_candidates():
    """Every directory the desktop app might keep its state in on this machine.

    A Microsoft Store (MSIX) install redirects `%APPDATA%` into its own package container, so there
    are two names for what may or may not be one directory:

        %APPDATA%\\Claude
        %LOCALAPPDATA%\\Packages\\Claude_<publisher>\\LocalCache\\Roaming\\Claude

    MEASURED ON TWO MACHINES, AND THEY DISAGREE. On this one the two are the SAME directory: a
    record under each path returns identical device and inode numbers, and both list 184 records.
    On the test laptop they are two different stores, 19 records and a `config.json` under the
    package container against 1 record and no config under `%APPDATA%`, so reading `%APPDATA%`
    there saw one twentieth of the sessions and would have filed an imported record where the app
    never looks.
    """
    roaming = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    found = [os.path.join(roaming, "Claude")]
    found.extend(sorted(glob.glob(os.path.join(
        local, "Packages", "Claude*", "LocalCache", "Roaming", "Claude"))))
    return found


def _identity(path):
    """(device, inode) for a directory, or None. Two names for one directory share these."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def claude_appdata():
    """The directory the desktop app is ACTUALLY using, chosen from evidence rather than assumed.

    `C4X_SESSIONS_ROOT` overrides it outright, for a layout neither candidate covers.

    Otherwise the candidates are collapsed by identity, so a redirected install counts once, and
    then ranked: a `config.json` beside the records is the strongest signal that the app writes
    there, then how many records it holds, then how recent the newest one is. With no evidence at
    all the `%APPDATA%` name is returned, which is where a fresh install puts things.
    """
    override = os.environ.get("C4X_SESSIONS_ROOT")
    if override:
        return os.path.dirname(override.rstrip("\\/")) or override
    seen, candidates = set(), []
    for path in _claude_appdata_candidates():
        key = _identity(path)
        if key is not None and key in seen:
            continue                    # the same directory under its other name
        if key is not None:
            seen.add(key)
        candidates.append(path)
    if len(candidates) == 1:
        # NOTHING TO CHOOSE BETWEEN, so do not pay to choose. The scoring below globs every record
        # under every candidate, which is 11 ms on this machine, and it used to run even when the
        # identity collapse above had already left a single answer. That was affordable when this
        # was called once per page build and stopped being affordable when the API cache started
        # calling it to build a version stamp for EVERY request, including cache hits.
        #
        # Both names collapse to one directory here; the two-root case this scoring exists for is
        # a different machine (a packaged install where %APPDATA% holds 1 record and the package
        # container holds 16), and there it still scores, because there it genuinely has to.
        return candidates[0]
    best, best_score = candidates[0], None
    for path in candidates:
        records = glob.glob(os.path.join(path, "claude-code-sessions", "*", "*", "local_*.json"))
        newest = 0.0
        for record in records:
            try:
                newest = max(newest, os.path.getmtime(record))
            except OSError:
                continue
        score = (os.path.isfile(os.path.join(path, "config.json")), len(records), newest)
        if best_score is None or score > best_score:
            best, best_score = path, score
    return best


def sessions_root():
    """Where the desktop app keeps its per-chat records."""
    override = os.environ.get("C4X_SESSIONS_ROOT")
    if override:
        return override
    return os.path.join(claude_appdata(), "claude-code-sessions")


def records_fingerprint(root=None):
    """(file count, newest mtime) for the desktop records, or None when there are none.

    CHEAP ENOUGH TO ASK EVERY TIME, which is the whole point: reading the records costs about 47 ms
    and this costs about 1.6 ms, so it can answer "has anything changed" without doing the work.
    Two callers need exactly that. `desktop_records` uses it so a chat renamed in the app shows its
    new name on the next render instead of whenever a 45 second timer happens to lapse, and the API
    response cache uses it because a rename touches no database and so moved nothing it watched.

    A COUNT AS WELL AS AN MTIME. Deleting a record moves neither the newest mtime nor anything else
    that would be noticed.

    `os.scandir` rather than `glob`, measured on the 194 records here: the walk costs 1.6 ms and
    the equivalent glob costs 10.5 ms.
    """
    root = root or sessions_root()
    if not root:
        return None
    newest = count = 0
    try:
        with os.scandir(root) as accounts:
            for account in accounts:
                if not account.is_dir():
                    continue
                with os.scandir(account.path) as orgs:
                    for org in orgs:
                        if not org.is_dir():
                            continue
                        with os.scandir(org.path) as files:
                            for entry in files:
                                if not entry.name.endswith(".json"):
                                    continue
                                try:
                                    info = entry.stat()
                                except OSError:
                                    continue
                                count += 1
                                newest = max(newest, int(info.st_mtime_ns))
    except OSError:
        # No desktop app, no records directory, or it went away mid-walk. A machine with no
        # records has nothing to notice a change in.
        return None
    return (count, newest)


def read_archived_record(path):
    """(cli session id, archived, title) for one record, or None when it is not a session record.

    Reads a bounded prefix first. If any field is missing from it the whole file is parsed, so a
    record that happens to order its keys differently is answered correctly rather than skipped.
    That directory also holds scheduled-tasks.json files, which carry none of the fields and are
    not session records; those return None rather than counting as a failed read.

    THE TITLE IS THE NAME THE DESKTOP APP SHOWS, and it was being thrown away. This function has
    always opened and parsed the one file that carries it while returning only two of its fields,
    which is why a chat the app calls "Creating MCP server" was listed here under its first prompt.
    It can be an empty string or absent entirely, and that is returned as None rather than "": a
    record with no title and a record with a blank one are the same fact to every caller.

    A MISSING TITLE COSTS THE PREFIX OPTIMISATION, deliberately. A prefix that carries the id and
    the flag but no title is ambiguous between "this chat has no title" and "the title is past the
    bound", and only the whole file can tell those apart.

    COUNTED, not estimated, over the 194 files here: the whole-file parse ran on 24 of them before
    this change and runs on 29 now, so the title added FIVE. It is five and not fourteen because
    fourteen files lack a top-level title but nine of those are the scheduled-tasks.json files,
    which carry no cliSessionId and so took the slow path already. The five are session records
    with no title key at all.

    The end-to-end scan went from 36.8 ms to 47.4 ms, and the two halves of that 10.6 ms are worth
    naming because the obvious suspect is the smaller one: the five extra parses cost 6.3 ms, and
    running the scanner over all 194 prefixes costs 5.6 ms whether or not a fallback follows.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(_HEAD_BYTES)
    except OSError:
        return None
    cli, arch = _CLI_ID.search(head), _ARCHIVED.search(head)
    title = _top_level_string(head, "title")
    if cli and arch and title is not None:
        return cli.group(1), arch.group(1) == "true", (title.strip() or None)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not record.get("cliSessionId"):
        return None
    whole = record.get("title")
    whole = whole.strip() if isinstance(whole, str) else ""
    return str(record["cliSessionId"]), bool(record.get("isArchived")), (whole or None)


def desktop_records(root=None, ttl: float = 45.0) -> dict:
    """{session id: (archived, title)} for every chat the desktop app has a record of.

    Keyed by cliSessionId, which is what this store calls session_id. The desktop app's own
    sessionId is a different namespace entirely (`local_<uuid>`), and matching on it finds almost
    nothing, which is what made this look unreadable the first time.

    ONE SCAN, TWO ANSWERS. `archived_sessions` and `desktop_titles` are both views over this, so a
    render that wants the archived marker and the chat's name reads the directory once.
    """
    root = root or sessions_root()
    now = _time.time()
    # A FINGERPRINT, NOT A TIMER, and the difference is user-visible. A 45 second timer meant a
    # chat renamed in the desktop app kept its old name here until the timer happened to lapse,
    # which is the same staleness the API response cache was just taught to avoid: fixing it there
    # and leaving it here would move the delay rather than remove it. The fingerprint costs 1.6 ms
    # against a rescan that costs 47 ms, so asking every time is affordable and the answer is
    # always current. `ttl=0` still forces a rescan, which is what the tests use.
    sig = records_fingerprint(root) if ttl else None
    if (ttl and _archived_cache["map"] is not None and _archived_cache["root"] == root
            and _archived_cache["sig"] == sig):
        return _archived_cache["map"]
    seen = _generation["n"]
    found = {}
    for path in glob.glob(os.path.join(root, "*", "*", "*.json")):
        row = read_archived_record(path)
        if row is not None:
            found[row[0]] = (row[1], row[2])
    if seen == _generation["n"]:
        _archived_cache.update({"map": found, "at": now, "root": root, "sig": sig})
    return found


def archived_sessions(root=None, ttl: float = 45.0) -> dict:
    """{session id: archived} for every chat the desktop app has a record of."""
    return {sid: rec[0] for sid, rec in desktop_records(root, ttl).items()}


# The table `tools/redact.py` writes into every copy it produces. Its presence is the copy saying
# what it is, which is the only reason the check below can be automatic.
REDACTION_MARK = "redacted_store"
_redacted_cache: dict = {}


def store_is_redacted() -> bool:
    """Whether the open store is a redacted COPY rather than this machine's own store.

    THIS EXISTS BECAUSE A TITLE READ AT RENDER TIME IS NOT IN THE STORE, and so cannot be redacted
    by copying and rewriting one. `tools/redact.py` builds a scrubbed copy precisely so a README
    screenshot cannot leak session titles, and its gate greps that copy for surviving fragments.
    An overlay that reads the live records would paint the real names back over the redacted ones
    at render time, and the gate would still pass, because the leak is not in the file it checks.

    Its docstring argues, correctly, that the alternative to a copy is "a `--demo` switch applied
    wherever a path becomes display text ... and missing one leaks". So this is NOT a switch. The
    fact travels inside the artifact: redact.py stamps every copy it writes, and a stamped store
    gets no overlay, on any machine, whether or not anyone remembered anything.
    """
    key = str(DB_PATH)
    if key not in _redacted_cache:
        _redacted_cache[key] = tables_present(REDACTION_MARK)
    return _redacted_cache[key]


def overlay_desktop_titles(df, named):
    """Put the desktop app's name on every row it has one for. Mutates and returns `df`.

    A MODULE-LEVEL FUNCTION RATHER THAN SIX LINES INLINE, because inline it was the user-visible
    half of this feature and the only part with no test: reaching it needs a built session frame,
    which needs a store, so nothing exercised it and deleting it would have kept the suite green.

    Positional throughout. `list(df[col])` and iterating a Series both yield values in row order,
    and assigning a list back to a column aligns by position, so a frame whose index is not a
    clean range is handled correctly. That is worth stating because the obvious alternative,
    `df.loc[mask, "title"] = ...`, aligns by LABEL and would put titles on the wrong rows there.
    """
    if df is None or not len(df) or not named:
        return df
    titles, kinds = list(df["title"]), list(df["title_kind"])
    for i, sid in enumerate(df["session_id"]):
        text = named.get(sid)
        if text:
            titles[i], kinds[i] = text, "desktop"
    df["title"], df["title_kind"] = titles, kinds
    return df


def desktop_titles(root=None, ttl: float = 45.0) -> dict:
    """{session id: title} for every chat the desktop app has a NAME for.

    A chat with a record but no title is absent, not present with an empty value: the caller is
    choosing a title to display and "the app has no name for this either" is the same answer as
    "there is no record", not a different one.

    Empty on a redacted copy. This is the one choke point both overlays go through, which is why
    the check sits here rather than at each of them.
    """
    if store_is_redacted():
        return {}
    return {sid: rec[1] for sid, rec in desktop_records(root, ttl).items() if rec[1]}


class _TranscriptCache(TypedDict):
    """Which transcripts are still on this machine, and when that was last scanned."""

    ids: set[str] | None
    at: float


_transcript_cache: _TranscriptCache = {"ids": None, "at": 0.0}


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
    seen = _generation["n"]
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
    if seen == _generation["n"]:
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
        -- Fewer than SESSION_TURN_FLOOR transcript rows and a session is not listed. The rule,
        -- its measurement and its one warning are beside the constant.
        HAVING COUNT(*) >= ?
    """, (SESSION_TURN_FLOOR,))
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
    # `archived_sessions` and `desktop_titles` are both views over one cached scan, so calling both
    # costs one directory read, not two. They are called SEPARATELY rather than through
    # `desktop_records` on purpose: `archived_sessions` is the seam the delete tests patch to build
    # an archived label without writing records, and reaching past it broke three of them.
    flags = archived_sessions()
    df["archived"] = [flags.get(sid) for sid in df["session_id"]]
    df["project"] = [
        project_label(c, s) + ("\\" + ARCHIVED_SUFFIX if archived else "")
        for c, s, archived in zip(df["cwd"], df["project_slug"], df["archived"], strict=True)
    ]
    # THE NAME THE DESKTOP APP SHOWS WINS, and it wins over `custom` too, which is the part worth
    # justifying because `custom` is also a title a person chose.
    #
    # Measured against this machine's 194 records joined to the store: 406 sessions are listed,
    # 92 of them have a titled record. 73 already display a byte-identical title, 14 currently
    # display their opening prompt and gain a real name, and 6 disagree. Every one of the 6 favours
    # the record. Five are the record carrying a "(fork)" suffix the transcript lacks, which is the
    # only thing distinguishing a fork from its parent in a list where both otherwise read the
    # same. The sixth is decisive: record title "main", titleSource "user", and previousTitles
    # holding exactly the string the store still shows. The person renamed the chat and the
    # transcript kept the name they replaced.
    #
    # Note what does NOT decide this: file mtime. In that sixth case the transcript is the NEWER
    # file and holds the STALER title, because a transcript's mtime moves on any later turn and
    # says nothing about when its title record was written. A tiebreak on mtime would have got this
    # exactly backwards.
    overlay_desktop_titles(df, desktop_titles())
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
    # Says what the number counts. "All sessions (317)" beside a Summary card reading 1,325 is
    # two numbers for one word with nothing to reconcile them.
    opts = [{"label": f"All sessions ({len(df):,} listed)", "value": COHORT_ALL}]
    if df.empty:
        return opts
    for sec, n in df["section"].value_counts().items():
        opts.append({"label": f"Section: {sec} ({n:,})", "value": f"section::{sec}"})
    # RANKED BY WORK DONE, not by how many sessions a directory happens to hold.
    #
    # Session count put a benchmark harness in charge of this list. It spawned one short run per
    # scratch directory, so measured on this store 22 of the 40 projects offered sat under a tmp
    # folder and between them held 210 API calls of the 151,403 on the list. Meanwhile ranking by
    # calls surfaces 23 projects the old order left out, including one of 2,587 calls and another
    # of 1,006, hidden because each had only two sessions.
    #
    # No path heuristic. Nothing here knows or should know that "tmp" means scratch: a directory
    # earns its place by the work done in it, which is the same rule for every project.
    calls = q("SELECT session_id, COUNT(*) AS n FROM api_calls GROUP BY session_id")
    per_session = dict(zip(calls["session_id"], calls["n"], strict=True))
    work = (df.assign(_calls=[per_session.get(s, 0) for s in df["session_id"]])
              .groupby("project")
              .agg(sessions=("session_id", "count"), calls=("_calls", "sum"))
              .sort_values(["calls", "sessions"], ascending=False)
              .head(40))
    # SHORTENED, AND DISAMBIGUATED. These are working directories, about 150 characters here, and
    # the control that renders them clips from the right - so two projects under the same scratch
    # parent arrived as one identical string and the list offered the same choice twice. The chart
    # axis was given `short_path` for exactly this and the dropdown was not, which is why the
    # helper now lives in c4x/labels.py with a collision-aware variant beside it.
    #
    # The VALUE keeps the full path. The label is ambiguous by construction and nothing matches
    # on it; `cohort_parts` below splits the value, and a delete resolves through that.
    labels = distinct_short_paths(list(work.index))
    # A CHAT WITH NO FOLDER GETS ITS NAME. distinct_short_paths keeps the tail that tells two
    # projects apart, which is right when the tail is a directory somebody chose and useless when
    # it is "scratch-2026-09-05-d67fea" under two generated uuids. For those, the chat's own name is
    # the only thing that identifies it to a reader.
    #
    # The VALUE is untouched: cohort_parts splits it, and a delete resolves through that.
    folderless = [p for p in work.index if is_folderless(p)]
    if folderless:
        by_project: dict = {}
        for p in folderless:
            for sid in cohort_sessions(f"project::{p}"):
                by_project.setdefault(p, []).append(sid)
        every = titles_for([s for ids in by_project.values() for s in ids])
        for p in folderless:
            ids = by_project.get(p) or []
            labels[p] = titled_path(p, every.get(ids[0], {}) if ids else {})
    for proj, row in work.iterrows():
        # "listed", the same qualifier the All sessions option above carries. Without it the
        # number reads as "this project has N sessions", when it is the count the picker will
        # SHOW: a project whose sessions fall below SESSION_TURN_FLOOR offers fewer than it holds,
        # and a reader comparing it against the store has nothing to reconcile the two. Same
        # defect the first option was fixed for, on the option beside it.
        opts.append({"label": f"Project: {labels[proj]} ({int(row['sessions']):,} listed)",
                     "value": f"project::{proj}"})
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


def restrict_to_cohort(df, cohort, column: str = "session_id"):
    """Narrow a frame to a cohort, with an ASKED-FOR-BUT-EMPTY cohort meaning nothing, not all.

    ONE HOME FOR THE RULE, because the shape it replaces was copied to five places and every copy
    had the same hole: `ids = cohort_sessions(...)` then `if ids: df = df[isin]`, so a cohort that
    resolves to no sessions fell through and left the frame whole. A reader who deleted a project
    and stayed on the page then saw the entire store under a header naming the project they deleted.

    Five sites, found by sweeping rather than by reading the report, which named two.
    """
    ids = cohort_sessions(cohort)
    if ids:
        return df[df[column].isin(ids)]
    if cohort_named(cohort):
        return df.iloc[0:0]
    return df


def cohort_named(cohort) -> bool:
    """Whether a COHORT WAS ASKED FOR, regardless of whether anything answers to it.

    THE DISTINCTION `cohort_sessions` CANNOT MAKE. It returns an empty list for two situations that
    are opposites: nothing was selected, and a project was selected that no longer has any sessions.
    Every filtering site then reads empty as "no restriction", so deleting a project turns its
    filter into the whole store, silently, and the page shows more than was asked for rather than
    less. Proven by executing the functions: a live project emits `AND session_id IN (?,?)` and a
    deleted one emits byte-identical SQL to no filter at all.

    This repo has met the same conflation twice and both times guarded the INPUT SHAPE. cohort_parts
    above records one: "the frontend sent a bare path with no `project::` prefix, which resolves
    here to no restriction ... A delete cannot afford the same mistake." Guarding the shape does
    nothing for a well-formed cohort whose rows are gone, which is what a delete leaves behind.
    """
    return bool(cohort) and cohort != COHORT_ALL and bool(cohort_parts(cohort)[0])


def cohort_sessions(cohort, ttl: float = 45.0) -> list:
    """Resolve a cohort to the session ids it contains. Empty list means 'no restriction'.

    `ttl` is passed through to `session_rows`. A page render wants the cache; anything about to
    WRITE wants `ttl=0`, because a set that predates the last import would export or delete the
    wrong sessions while looking entirely ordinary.
    """
    if not cohort or cohort == COHORT_ALL:
        return []
    df = session_rows(ttl)
    if df.empty:
        return []
    kind, value = cohort_parts(cohort)
    col = {"section": "section", "project": "project"}.get(kind)
    if not col:
        return []
    return list(df.loc[df[col] == value, "session_id"])


def session_name(session_id) -> str:
    """One chat, said the way the pickers say it: project, title, and when it last ran.

    The same three parts and the same order as `selector_options`, so a name read off a comparison
    can be found again in the list it was chosen from without translating between two formats.

    Returns "" for an unknown id rather than raising. A caller that has a stale selection should
    still render, and the population sentence beside this one already says how many sessions there
    are, so an empty name degrades to exactly the wording that was there before.
    """
    if not session_id:
        return ""
    df = session_rows()
    if df.empty:
        return ""
    row = df[df["session_id"] == session_id]
    if row.empty:
        return ""
    r = row.iloc[0]
    when = str(r.get("last_ts") or "")[:16].replace("T", " ")
    return f"{r.get('project')}  ·  {str(r.get('title'))[:60]}  ·  {when}"


def titles_for(session_ids) -> dict:
    """Best-known title per session, as a mapping of session_id to {kind: text}.

    Guarded, because `session_titles` is written by harvest and this package never writes: a store
    from before that table existed has no such table and a bare query raises rather than returning
    nothing.
    """
    ids = [s for s in (session_ids or []) if s]
    if not ids:
        return {}
    out: dict = {}
    if tables_present("session_titles"):
        marks = ",".join("?" * len(ids))
        df = q(f"SELECT session_id, kind, title FROM session_titles WHERE session_id IN ({marks})",
               tuple(ids))
        for _, row in df.iterrows():
            out.setdefault(row["session_id"], {})[row["kind"]] = row["title"]
    # The desktop record is a title source like the others, and it is added HERE rather than only
    # in the session frame because this is a second, independent read path: `labels.py` names a
    # folder-less chat from whatever this returns, and leaving the overlay out of one of the two
    # would have the same chat called two different things on two panes. The `desktop` kind is
    # ranked by every consumer of this mapping, which is `labels.py:titled_path`.
    wanted = set(ids)
    for sid, text in desktop_titles().items():
        if sid in wanted:
            out.setdefault(sid, {})["desktop"] = text
    return out


def cohort_label(cohort, ids=None) -> str:
    """The display form of a cohort's value: shortened, and named when the path names nothing.

    ONE HOME, because this sentence is built in two places that must not drift: the population line
    on every scoped tab and the two arm labels on Compare. The VALUE is untouched; only what the
    reader sees changes.
    """
    kind, _, value = str(cohort or "").partition("::")
    if kind != "project" or not value:
        return value
    if not is_folderless(value):
        # UNCHANGED for a real project. Its path is its own name, and shortening it here would be a
        # different change from the one asked for: this sentence has always printed the full path
        # and nobody reported it as wrong.
        return value
    ids = list(ids if ids is not None else cohort_sessions(cohort))
    titles = titles_for(ids)
    best = titles.get(ids[0], {}) if ids else {}
    return titled_path(value, best)


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
        kind, _, _value = str(cohort).partition("::")
        # SHORTENED AND NAMED. This printed the raw working directory, and for a chat started
        # without a project that is a 152-character scratch path in the one sentence telling the
        # reader what they just selected. It reaches both frontends.
        return f"{plural(len(ids), 'session')} in {kind} {cohort_label(cohort, ids)}, {side}"
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
        elif cohort_named(cohort):
            # A COHORT WAS ASKED FOR AND NOTHING ANSWERS TO IT. Falling through here appended no
            # clause at all, so the query became the store-wide query and the page answered a
            # question nobody asked. Deleting a project is the ordinary way to reach this state,
            # and the app keeps the deleted cohort selected afterwards.
            #
            # An empty population is the truthful answer, so the filter matches no row rather than
            # every row. `1 = 0` rather than `IN ()`: SQLite accepts the latter, but this reads as
            # what it is to anyone opening the SQL accordion under the table.
            bits.append("AND 1 = 0")
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


def compaction_kept(compaction_uuid: str, limit: int = 300) -> pd.DataFrame:
    """Messages from before a compaction that its survivor list names, so they crossed the boundary.

    THE OTHER HALF OF `compaction_dropped`, and the half that says what a compaction was FOR. A
    boundary is not only a deletion: it keeps a chosen few messages verbatim alongside the summary
    it writes, and which ones it chose is the most legible thing about it.

    Same lower bound in the same direction. A survivor uuid the store holds no message for cannot
    be shown, so this lists what can be shown to have stayed rather than everything that stayed:
    on this store one boundary records 24 message survivors and 17 of them join to a harvested row.

    ONE ROW PER MESSAGE, VIA `IN` RATHER THAN A JOIN. `compaction_survivors` holds 208 duplicate
    (compaction, uuid) pairs across every boundary that has any, so joining it row for row returned
    the same message twice: 270 rows carrying 268 distinct uuids on the worst boundary here. That
    inflated the count the page printed AND collided the React key it is drawn under. Its opposite
    number, `compaction_dropped`, uses `NOT IN` and was never exposed to this, which is how the two
    halves of one feature came to disagree.
    """
    return q(
        """
        SELECT m.uuid, m.ts, m.role, m.type, m.chars,
               substr(replace(replace(m.text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM compactions c
        JOIN messages m ON m.uuid IN (
                 SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
        WHERE c.uuid = ?
          AND m.uuid <> COALESCE(c.summary_uuid, '')
        ORDER BY m.chars DESC
        LIMIT ?
        """,
        (compaction_uuid, limit),
    )


def compaction_kept_count(compaction_uuid: str) -> int:
    """How many survivors can be SHOWN: distinct messages this store actually holds.

    NOT how many were recorded, and the page must not print it under that word. On the worst
    boundary here the store recorded 697 survivors, 270 of them join to a harvested message, and
    268 of those are distinct. Three different numbers, and only the last is what a reader sees.
    """
    df = q(
        """
        SELECT COUNT(*) AS n FROM (
            SELECT DISTINCT m.uuid
            FROM compactions c
            JOIN messages m ON m.uuid IN (
                     SELECT uuid FROM compaction_survivors WHERE compaction_uuid = c.uuid)
            WHERE c.uuid = ? AND m.uuid <> COALESCE(c.summary_uuid, ''))
        """,
        (compaction_uuid,),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


def compaction_exists(compaction_uuid: str) -> bool:
    """Whether this store holds a boundary with that uuid.

    THE TEST IS EXISTENCE, NOT WHETHER A SUMMARY WAS FOUND, and the difference is a real state
    rather than a nicety. `compactions.summary_uuid` is NULL for a boundary whose summary message
    was never harvested, `overview_stats` counts those as `unpaired` and puts the number on the
    page, and the detail route deliberately answers `"summary": null` for one. Gating the 404 on an
    empty summary would delete the only surface that reports it.

    This store happens to hold zero unpaired boundaries today, so that wrong version would look
    entirely correct on the machine it was written on.
    """
    df = q("SELECT 1 FROM compactions WHERE uuid = ? LIMIT 1", (compaction_uuid,))
    return not df.empty


def compaction_survivors_recorded(compaction_uuid: str) -> int:
    """How many survivors the BOUNDARY recorded, which is what its own row reports.

    The number the reader just clicked on. Kept apart from what can be shown, because they differ
    by a factor of two and a half on this store and printing either under the other's name is a
    wrong number rather than a rounded one.
    """
    df = q(
        "SELECT COUNT(DISTINCT uuid) AS n FROM compaction_survivors WHERE compaction_uuid = ?",
        (compaction_uuid,),
    )
    return int(df.iloc[0]["n"]) if not df.empty else 0


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


def session_messages(session_id: str, limit: int = 2000) -> pd.DataFrame:
    """The messages of one session, capped, with each one cut to a preview.

    THE CAP IS 2,000, RAISED FROM 400. Measured on this store: 62 of 1,352 sessions hold more than
    400 messages and 27 hold more than 2,000, so the raise halves the number of sessions whose
    table, and whose search box, cannot see the whole session. It costs nothing on a typical one:
    the mean session holds 244.7 messages and never reached the old cap either. At 180 bytes of
    preview per row the worst case is about 360 KB, and only for the sessions that need it.

    It is still a cap, and the table says so in its own note rather than presenting the first
    2,000 as the whole. The largest session here holds 53,124 messages.

    THE PREVIEW IS 220 CHARACTERS and that is a display cut, never a search one. 205,775 of the
    330,857 messages in this store are longer than that, so a browser searching the preview was
    searching about a tenth of the average message; the page fetches the whole of the column when
    somebody actually searches.
    """
    return q(
        """
        SELECT uuid, ts, role, type, chars,
               substr(replace(replace(text, char(10), ' '), char(13), ' '), 1, 220) AS preview
        FROM messages WHERE session_id = ? ORDER BY ts LIMIT ?
        """,
        (session_id, limit),
    )


def session_tool_calls(session_id: str, limit: int = 2000) -> pd.DataFrame:
    """The tool calls of one session, shaped like messages so they can share a timeline.

    WHAT WAS PROPOSED HAS NO MESSAGE. `messages` holds no tool_use type at all, store-wide, so a
    rejected call read as "plan written" then "the user does not want to proceed with this tool
    use" with nothing between them naming what was refused. The call itself lived in `tool_calls`,
    a different table on a different tab, and until harvest kept a preview it recorded only a hash
    and a byte count.

    Columns are named for the message ones deliberately: the two frames are concatenated and sorted
    by ts, so the reader meets the proposal and its refusal in the order they happened.

    EMPTY WHEN THE STORE HAS NOT MIGRATED, rather than raising. This package never writes, so it
    cannot add the column itself; harvest adds it on its next run, which may be days after this
    code ships. A timeline missing its proposals is the state that existed before this function,
    and it is a great deal better than a tab that raises.
    """
    if not (column_present("tool_calls", "input_preview")
            and column_present("tool_calls", "description")):
        return pd.DataFrame(columns=["uuid", "ts", "role", "type", "chars", "preview"])
    # THE SAME VOCABULARY THE MESSAGES TABLE USES, which theme.COLUMN_HELP defines: `role` is the
    # transport record's own type, which is why it reads `user` on a tool result, and `type` is
    # what actually produced the record. A tool_use block sits on an assistant record, so those are
    # 'assistant' and 'tool_use', the exact mirror of the 'tool_result' row that answers it.
    #
    # THE NAME, THEN THE AGENT'S NOTE, THEN THE INPUT. The note is the short line the agent wrote
    # about the call ("Located chunk files"), and it is the thing a reader is scanning for. It is
    # NOT assistant prose: Claude Code never puts text and a tool_use in one record, so it lives
    # in the tool input and reached no table until it had a column. Ahead of the input because a
    # 220-character preview of a long command would otherwise push it off the end, which is the
    # same truncation that loses it inside input_preview 40% of the time.
    # Without the name the row says a call was proposed and not which,
    # and the name is the first thing a reader needs to make sense of the input that follows.
    return q(
        """
        SELECT tool_use_id AS uuid, ts,
               'assistant' AS role,
               'tool_use' AS type,
               input_bytes AS chars,
               substr(COALESCE(tool_name, 'tool') || ': ' ||
                      COALESCE(description || ' - ', '') ||
                      replace(replace(COALESCE(input_preview, ''), char(10), ' '), char(13), ' '),
                      1, 220) AS preview
        FROM tool_calls WHERE session_id = ? AND input_preview IS NOT NULL
        ORDER BY ts LIMIT ?
        """,
        (session_id, limit),
    )

def messages_text(uuids: list[str]) -> dict[str, str]:
    """{uuid: full text} for the uuids given, in one query. Absent uuids are absent, not empty."""
    if not uuids:
        return {}
    marks = ",".join("?" for _ in uuids)
    df = q(f"SELECT uuid, text FROM messages WHERE uuid IN ({marks})", tuple(uuids))
    return {str(u): ("" if t is None else str(t))
            for u, t in zip(df["uuid"], df["text"], strict=True)}


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
    seen = _generation["n"]
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
    # THE FOURTH CACHE, AND THE ONE THAT WAS MISSED. `invalidate` clears it, but a resolution
    # already in flight re-installed its answer afterwards, exactly as the other three did before
    # they were guarded. This one spawns node to refill, so the window is the longest of the four.
    if seen == _generation["n"]:
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

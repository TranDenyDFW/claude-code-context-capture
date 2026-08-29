"""Reading the store, and the window math the reads depend on.

Read-only by construction: nothing in this module writes, because the dashboard never does. Only
harvest.mjs and the hooks write to the store.

It knows what a session is and nothing about how one is drawn, which makes it the opposite half of
theme.py and leaves neither importing the other.

The one thing to know before changing a query here: **sum api_calls, never turns.** A streamed
assistant message is written as several transcript rows sharing one request id, so summing turns
counts the same API call two to eight times.
"""
import json
import os
import sqlite3
import subprocess
import time as _time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "context.db"
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
# Data access. Read-only; the app never writes to the store.
# ---------------------------------------------------------------------------
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
               (SELECT COUNT(*) FROM compactions c
                 WHERE c.session_id = t.session_id) AS compactions,
               (SELECT title FROM session_titles st WHERE st.session_id = t.session_id
                 ORDER BY CASE st.kind WHEN 'custom' THEN 0 WHEN 'ai' THEN 1
                          ELSE 2 END LIMIT 1) AS title,
               (SELECT kind FROM session_titles st WHERE st.session_id = t.session_id
                 ORDER BY CASE st.kind WHEN 'custom' THEN 0 WHEN 'ai' THEN 1
                          ELSE 2 END LIMIT 1) AS title_kind
        FROM turns t LEFT JOIN sessions s ON s.session_id = t.session_id
        GROUP BY t.session_id
        HAVING COUNT(*) >= 5
    """)
    if df.empty:
        return df

    def classify(r):
        """Which section a session belongs to. Every test is answerable from disk.

        No Archived section: that flag lives in the desktop app's IndexedDB, not in the transcripts
        and not in any readable file, so it cannot be shown without a snapshot that would go stale
        silently. A stale flag presented as live is worse than an absent one.
        """
        path = r.transcript_path
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
    df["project"] = [project_label(c, s)
                     for c, s in zip(df["cwd"], df["project_slug"], strict=True)]
    # A session with no title of any kind says so, rather than showing an empty cell that reads
    # like a rendering fault.
    df["title"] = [
        (t.strip() if isinstance(t, str) and t.strip() else "(untitled)")
        for t in df["title"]
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


def cohort_sessions(cohort) -> list:
    """Resolve a cohort to the session ids it contains. Empty list means 'no restriction'."""
    if not cohort or cohort == COHORT_ALL:
        return []
    df = session_rows()
    if df.empty:
        return []
    kind, _, value = str(cohort).partition("::")
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

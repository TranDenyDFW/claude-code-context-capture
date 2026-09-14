"""A review run is a chat that reads another chat, and it is named after the one it read.

WHAT THIS IS FOR. A Stop hook on the test laptop ran `claude -p` with a fixed prompt whose second
half was the last 250 records of the transcript being reviewed, pasted in as `CLAUDE SAID:` /
`OUTPUT WAS:` / `USER:` blocks. Each run wrote a transcript of its own into the reviewed session's
project folder, with the app's own entrypoint inherited from its environment, so the store holds
58 of them as ordinary sessions (one prompt, one reply: APPROVED or PROBLEMS) and every one of
them was offered and adopted as a chat named after its opening line, "You are reviewing another
Claude instance's work before it...". The prompt carries no session id and no transcript path.

WHAT TIES A RUN TO THE CHAT IT READ IS QUOTATION. The excerpt is verbatim, so long lines of the
run's prompt occur in the reviewed session's messages and in no other session's. Measured on
that laptop before this was written: ASCII lines from the excerpt's tail name exactly one session
for 54 of the 58 runs; timing alone does not (a run approving a session starts after that
session's last turn, and five long sessions overlap in one folder), and the verdict fed back into
the reviewed session covers only the runs that blocked.

THE RULE, all of it:
  - Only ONE-SHOTS are ever tested: a session with exactly one typed prompt and at most three
    messages. Nothing else is read for this.
  - The pool is the sessions in the run's own folder (the hook's child inherited its cwd) that
    were alive when the run started: begun no later than the run and last active no more than
    SLACK (an hour) before it. A reviewer reads a session that was just running, and a session
    that stopped long before is never the one, however much it shares the folder. Not one-shots
    themselves, so a run is never tied to a run.
  - Up to eight snippets from the prompt, newest first: lines of 80 or more characters that are
    pure ASCII (the excerpt crossed a shell pipe and the store holds U+FFFD where the transcript
    had anything else, so such a line can never match), an all-caps label such as `CLAUDE SAID: `
    dropped, and the LAST 120 characters kept (a substring of a line the hook truncated is still a
    substring of the original).
  - ONE query per run: how many of the snippets each pool session says at least once. The run is
    tied to the session with the most when that session is unique and its count reaches
    `min(2, snippets)`. A tie, no snippet or no hit ties nothing, and the run keeps whatever name
    it would have had.

WHAT IT COSTS, measured, because this runs behind every load of the page. With every session in
the folder as the pool and a query per snippet, the author's store (908 candidates, 705 one-shots)
took 26.5 s cold. The pool above is a handful of sessions, the prompts are read in one query and
the hits in one per run. Answers are cached per store and run: a tie, once found, is kept for good
(quotation does not go away as the store grows); a miss is kept while the run's pool is the same
set of sessions, and asked again only when a session joins it.
"""
import datetime as _dt
import re
from collections import Counter
from typing import Any

PREFIX = "Reviewer - "
WANT = 8
MIN_LINE = 80
TAIL = 120
ONE_SHOT_MESSAGES = 3
SLACK = 3600.0   # seconds a session may have been quiet before a run that read it started
# `CLAUDE SAID: `, `OUTPUT WAS: `, `USER: `: an all-caps label the excerpt's writer put in front
# of a line it copied. Dropped, so that the line matches what the reviewed session actually said.
LABEL = re.compile(r"^[A-Z][A-Z ]{1,20}: ")
_CHUNK = 900   # under SQLite's variable limit, with room for the other parameters

_cache: dict = {}


def forget() -> None:
    """Drop every cached answer. Tests that rebuild a store call this."""
    _cache.clear()


def snippets(prompt: Any, want: int = WANT, min_len: int = MIN_LINE, tail: int = TAIL) -> list:
    """The lines of `prompt` worth searching for, newest first: see the module docstring."""
    out: list = []
    for raw in reversed(str(prompt or "").splitlines()):
        line = LABEL.sub("", raw.strip(), count=1)
        if len(line) >= min_len and line.isascii():
            out.append(line[-tail:])
            if len(out) >= want:
                break
    return out


def _chunks(items, size: int = _CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _seconds(ts: Any):
    """An ISO timestamp as seconds, or None when it is not one."""
    text = str(ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.timestamp()


def one_shots(session_ids) -> set:
    """The sessions among `session_ids` with exactly one typed prompt and at most three messages."""
    from c4x import store
    out: set = set()
    for chunk in _chunks(dict.fromkeys(str(s) for s in session_ids if s)):
        marks = ",".join("?" * len(chunk))
        df = store.q(f"""
            SELECT session_id FROM messages WHERE session_id IN ({marks})
             GROUP BY session_id
            HAVING SUM(CASE WHEN type = 'typed' AND role = 'user' THEN 1 ELSE 0 END) = 1
               AND COUNT(*) <= ?""", (*chunk, ONE_SHOT_MESSAGES))
        out.update(str(s) for s in df["session_id"])
    return out


def _prompts(runs) -> dict:
    """run id -> its typed prompt, read in one query per chunk rather than one per run."""
    from c4x import store
    out: dict = {}
    for chunk in _chunks(runs):
        marks = ",".join("?" * len(chunk))
        df = store.q(f"""SELECT session_id, text FROM messages
                          WHERE session_id IN ({marks}) AND type = 'typed' AND role = 'user'
                          ORDER BY ts""", tuple(chunk))
        for sid, text in zip(df["session_id"], df["text"], strict=True):
            out.setdefault(str(sid), str(text or ""))
    return out


def _hits(snips: list, pool: list) -> Counter:
    """session id -> how many of the snippets it says at least once. One query per pool chunk."""
    from c4x import store
    hits: Counter = Counter()
    said = " + ".join("MAX(CASE WHEN instr(text, ?) > 0 THEN 1 ELSE 0 END)" for _s in snips)
    for chunk in _chunks(pool):
        marks = ",".join("?" * len(chunk))
        df = store.q(f"""SELECT session_id, {said} AS n FROM messages
                          WHERE session_id IN ({marks}) GROUP BY session_id""",
                     (*snips, *chunk))
        for sid, n in zip(df["session_id"], df["n"], strict=True):
            if int(n or 0) > 0:
                hits[str(sid)] += int(n)
    return hits


def _tie(prompt: str, pool: list):
    """The one session in `pool` the prompt quotes, or None."""
    snips = snippets(prompt)
    if not snips or not pool:
        return None
    hits = _hits(snips, pool)
    if not hits:
        return None
    top = max(hits.values())
    best = [sid for sid, n in hits.items() if n == top]
    if len(best) != 1 or top < min(2, len(snips)):
        return None
    return best[0]


def reviewed_by(sessions: list, session_ids) -> dict:
    """run id -> the id of the session it quotes, for the review runs among `session_ids`.

    `sessions` are `adopt._sessions()` rows (session_id, cwd, first_ts, last_ts); `session_ids`
    are the ones worth asking about, the candidates and the ledger's. Everything else is only ever
    a pool entry.
    """
    from c4x import store
    by_id = {str(r["session_id"]): r for r in sessions}
    wanted = [sid for sid in dict.fromkeys(str(s) for s in session_ids) if sid in by_id]
    runs = one_shots(wanted)
    if not runs:
        return {}
    db = str(store.DB_PATH)

    def pool_for(run: str) -> list:
        r = by_id[run]
        start = _seconds(r.get("first_ts"))
        if start is None:
            return []
        out = []
        for s in sessions:
            sid = str(s["session_id"])
            if sid == run or s.get("cwd") != r.get("cwd"):
                continue
            begun, last = _seconds(s.get("first_ts")), _seconds(s.get("last_ts"))
            if begun is None or begun > start:
                continue
            if (last if last is not None else begun) + SLACK < start:
                continue
            out.append(sid)
        return out

    alive = {run: pool_for(run) for run in runs}
    # ONE query says which pool entries are one-shots themselves, so a run is never tied to a run.
    pool_runs = one_shots({sid for pool in alive.values() for sid in pool})
    pools: dict = {run: tuple(sorted(sid for sid in pool if sid not in pool_runs))
                   for run, pool in alive.items()}
    fresh = []
    for run in runs:
        known = _cache.get((db, run))
        if known is None or (not known[1] and known[0] != pools[run]):
            fresh.append(run)
    prompts = _prompts(fresh) if fresh else {}
    for run in fresh:
        _cache[(db, run)] = (pools[run], _tie(prompts.get(run, ""), list(pools[run])))
    return {run: _cache[(db, run)][1] for run in runs if _cache[(db, run)][1]}

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
for 54 of the 58 runs; timing does not (a run approving a session starts after that session's last
turn, and five long sessions overlap in one folder), and the verdict fed back into the reviewed
session covers only the runs that blocked.

THE RULE, all of it:
  - Only ONE-SHOTS are ever tested: a session with exactly one typed prompt and at most three
    messages. Nothing else is read for this.
  - The pool is the sessions in the run's own folder (the hook's child inherited its cwd) that
    started no later than the run and are not one-shots themselves.
  - Up to eight snippets from the prompt, newest first: lines of 80 or more characters that are
    pure ASCII (the excerpt crossed a shell pipe and the store holds U+FFFD where the transcript
    had anything else, so such a line can never match), an all-caps label such as `CLAUDE SAID: `
    dropped, and the LAST 120 characters kept (a substring of a line the hook truncated is still a
    substring of the original).
  - One `instr` query per snippet over the pool; hits are summed per session; the run is tied to
    the session with the most hits when that session is unique and its hits reach
    `min(2, snippets)`. A tie, no snippet or no hit ties nothing, and the run keeps whatever name
    it would have had.

Answers are cached per store and run. A tie, once found, is kept for good: quotation does not
go away when the store grows. A run that tied to nothing is asked again only after the store
changed, since a new session in its folder could be the one it quotes. Measured on the test
laptop: 60 one-shots cost 4.0 s cold and 0.02 s from the cache, and every hook run changes the
store, which is why a found tie must not depend on its mtime.
"""
import os
import re
from collections import Counter
from typing import Any

PREFIX = "Reviewer - "
WANT = 8
MIN_LINE = 80
TAIL = 120
ONE_SHOT_MESSAGES = 3
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


def _prompt(run: str) -> str:
    from c4x import store
    df = store.q("""SELECT text FROM messages WHERE session_id = ? AND type = 'typed'
                     AND role = 'user' ORDER BY ts LIMIT 1""", (run,))
    return str(df["text"].iloc[0] or "") if len(df) else ""


def _hits(snips: list, pool: list) -> Counter:
    from c4x import store
    hits: Counter = Counter()
    for snip in snips:
        for chunk in _chunks(pool):
            marks = ",".join("?" * len(chunk))
            df = store.q(f"""SELECT DISTINCT session_id FROM messages
                              WHERE session_id IN ({marks}) AND instr(text, ?) > 0""",
                         (*chunk, snip))
            for sid in df["session_id"]:
                hits[str(sid)] += 1
    return hits


def _tie(run: str, pool: list):
    """The one session in `pool` the run quotes, or None."""
    snips = snippets(_prompt(run))
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


def _stamp() -> tuple:
    from c4x import store
    try:
        return (str(store.DB_PATH), os.stat(store.DB_PATH).st_mtime_ns)
    except OSError:
        return (str(store.DB_PATH), 0)


def reviewed_by(sessions: list, session_ids) -> dict:
    """run id -> the id of the session it quotes, for the review runs among `session_ids`.

    `sessions` are `adopt._sessions()` rows (session_id, cwd, first_ts); `session_ids` are the ones
    worth asking about, the candidates and the ledger's. Everything else is only ever a pool entry.
    """
    by_id = {str(r["session_id"]): r for r in sessions}
    wanted = [sid for sid in dict.fromkeys(str(s) for s in session_ids) if sid in by_id]
    runs = one_shots(wanted)
    if not runs:
        return {}
    stamp = _stamp()

    def pool_for(run: str) -> list:
        r = by_id[run]
        start = str(r.get("first_ts") or "")
        return [str(s["session_id"]) for s in sessions
                if str(s["session_id"]) != run and s.get("cwd") == r.get("cwd")
                and s.get("first_ts") and str(s["first_ts"]) <= start]

    # ONE query says which pool entries are one-shots themselves, so a run is never tied to a run.
    pool_runs = one_shots({sid for run in runs for sid in pool_for(run)})
    out: dict = {}
    for run in runs:
        key = (stamp[0], run)
        known = _cache.get(key)
        # A found tie is kept whatever the store's mtime; a miss is retried once the store changed.
        if known is None or (not known[1] and known[0] != stamp[1]):
            known = (stamp[1], _tie(run, [sid for sid in pool_for(run) if sid not in pool_runs]))
            _cache[key] = known
        if known[1]:
            out[run] = known[1]
    return out

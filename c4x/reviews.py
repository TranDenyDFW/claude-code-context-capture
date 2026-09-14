"""Review runs, as the store records them.

A Stop hook on the test laptop ran a headless `claude -p` after each turn with a prompt quoting
the last 250 records of the transcript under review. Each run left a one-prompt session in the
reviewed session's folder, carrying the app's own entrypoint and no session id, and the store
held 58 of them as chats: adopted into the desktop app, listed in the pickers, their tokens
counted as their own.

THE RULE LIVES IN `tools/harvest.mjs` (`deriveReviews`; the `review_links` schema comment records
the finding and the measurement): quotation is the only evidence and it is exact, so a one-shot
whose prompt's long ASCII lines occur in one session's assistant text and tool results, in the
same folder, alive when the run started, is that session's review. Harvest writes the table after
every pass and on `--backfill-reviews`; this module only reads it, through `store.review_links`.

`snippets` is the Python twin of `reviewSnippets`, pinned by tests on both sides to the same
inputs, kept so a change to either shows up as a failing test rather than as a quiet drift.
"""
import re
from typing import Any

WANT = 8
MIN_LINE = 80
TAIL = 120
# `CLAUDE SAID: `, `OUTPUT WAS: `, `USER: `: an all-caps label the excerpt's writer put in front
# of a line it copied. Dropped, so that the line matches what the reviewed session actually said.
LABEL = re.compile(r"^[A-Z][A-Z ]{1,20}: ")


def snippets(prompt: Any, want: int = WANT, min_len: int = MIN_LINE, tail: int = TAIL) -> list:
    """The lines of `prompt` worth searching for, newest first: 80 or more characters, pure ASCII,
    the label dropped, the last 120 kept. Split on newlines the way the node twin splits."""
    out: list = []
    for raw in reversed(str(prompt or "").split("\n")):
        line = LABEL.sub("", raw.strip(), count=1)
        if len(line) >= min_len and line.isascii():
            out.append(line[-tail:])
            if len(out) >= want:
                break
    return out


def reviewed_by(session_ids=None) -> dict:
    """run id -> the id of the session it reviewed, for the runs among `session_ids`, or for every
    run the store knows when none are named."""
    from c4x import store
    parent_of, _runs = store.review_links()
    if session_ids is None:
        return dict(parent_of)
    wanted = {str(s) for s in session_ids}
    return {run: parent for run, parent in parent_of.items() if run in wanted}


def runs_of(session_id: Any) -> list:
    """The runs that reviewed this one session, newest first. Empty when there are none."""
    from c4x import store
    _parent_of, runs = store.review_links()
    return list(runs.get(str(session_id), []))

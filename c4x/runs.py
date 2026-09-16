"""Headless child runs, as the store records them.

A chat's shell command ran `claude -p` in a folder (a benchmark harness, a script, a hook), and
each run left a one-prompt session of its own, under its own working directory, carrying the
app's entrypoint it inherited from its environment. The store held 888 of them under one
project's tmp folder on the author's machine, and the Adopt page offered 870 as one-chat folders.

THE RULE LIVES IN `tools/harvest.mjs` (`deriveRuns`; the `run_links` schema comment records the
finding and the measurements): a one-shot begun inside the span of another chat's shell call is
that chat's child (the call's input carries its prompt or names its cwd, or the parent's tool
result quotes its reply, or its cwd sits under the parent's); a one-shot with no such call and at
least three sibling one-shots begun within ten minutes is a batch, folded under the nearest
ancestor directory that holds a real chat. Harvest writes the table after every pass and on
`--backfill-runs`; this module only reads it, through `store.run_links`.
"""
from typing import Any


def spawned_by(session_ids=None) -> dict:
    """run id -> the id of the chat that spawned it, for the runs among `session_ids`, or for
    every run the store knows when none are named. The value is None for a batch run (no chat
    behind it; `project_of` says which project it folds under). A batch is still a run: never
    offered, its record taken back with the rest."""
    from c4x import store
    parent_of, _runs, _project_of = store.run_links()
    if session_ids is None:
        return dict(parent_of)
    wanted = {str(s) for s in session_ids}
    return {run: parent for run, parent in parent_of.items() if run in wanted}


def runs_of(session_id: Any) -> list:
    """The runs this one session spawned, newest first. Empty when there are none."""
    from c4x import store
    _parent_of, runs, _project_of = store.run_links()
    return list(runs.get(str(session_id), []))


def project_of(session_id: Any) -> str | None:
    """The working directory a run folds under: its parent chat's, or the nearest ancestor with a
    real chat for a batch. None for a run the store could not place, and for a session that is
    not a run."""
    from c4x import store
    _parent_of, _runs, project_of_ = store.run_links()
    return project_of_.get(str(session_id))

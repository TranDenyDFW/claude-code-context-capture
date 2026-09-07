"""You could see that something was rejected, never what.

Raised from the Messages table: "after the second item in the red box I rejected the request, why
doesn't the request show?" The rejection was there. The REQUEST was not, and it was missing
store-wide: `messages` holds no `tool_use` type at all, so the timeline read

    "Target confirmed present at 75 bytes. Plan written..."
    "The user doesn't want to proceed with this tool use."

with nothing between them saying what was proposed. The call itself sat in `tool_calls`, a different
table on a different tab, and that table kept only a hash and a byte count: 2,655 bytes, is_error 1,
and not one word of the plan. The plan was in the transcript on disk the whole time, and harvest
read it to derive the hash and then threw it away.

Two halves, tested separately because they fail separately: harvest has to KEEP the content, and the
timeline has to SHOW it beside the refusal it belongs to.
"""
import pandas as pd
import pytest

from c4x import store
from c4x.cli import extract
from c4x.tabs import session as session_tab

COLUMNS = ["uuid", "ts", "role", "type", "chars", "preview"]

PLAN = 'ExitPlanMode: {"plan": "# Delete results/t16-keep.txt (C4X test T16-A)'


def _messages():
    return pd.DataFrame([
        {"uuid": "m1", "ts": "2026-09-07T05:01:47", "role": "assistant", "type": "assistant",
         "chars": 177, "preview": "Target confirmed present at 75 bytes. Plan written"},
        {"uuid": "m2", "ts": "2026-09-07T05:01:51", "role": "user", "type": "tool_result",
         "chars": 225, "preview": "The user doesn't want to proceed with this tool use."},
    ], columns=COLUMNS)


def _calls():
    return pd.DataFrame([
        {"uuid": "toolu_1", "ts": "2026-09-07T05:01:49", "role": "assistant", "type": "tool_use",
         "chars": 2655, "preview": PLAN},
    ], columns=COLUMNS)


def test_the_store_declines_rather_than_raising_before_the_migration():
    """This package never writes, so it cannot add the column; harvest does, on its next run. Until
    then the query must not raise, because a tab that raises is worse than a timeline missing its
    proposals, which is the state that existed before any of this."""
    out = store.session_tool_calls("nobody")
    assert list(out.columns) == COLUMNS
    assert len(out) == 0


def test_a_proposal_lands_between_what_was_said_and_the_refusal(monkeypatch, has_store):
    """THE DEFECT, in the order the reader met it."""
    monkeypatch.setattr(session_tab, "session_messages", lambda *a, **k: _messages())
    monkeypatch.setattr(session_tab, "session_tool_calls", lambda *a, **k: _calls())
    rows = _messages_table(monkeypatch)
    assert [r["uuid"] for r in rows] == ["m1", "toolu_1", "m2"], (
        "the proposal must sit between the message that led to it and the refusal that answered it")


def test_the_proposal_says_what_was_proposed(monkeypatch, has_store):
    monkeypatch.setattr(session_tab, "session_messages", lambda *a, **k: _messages())
    monkeypatch.setattr(session_tab, "session_tool_calls", lambda *a, **k: _calls())
    rows = _messages_table(monkeypatch)
    proposal = next(r for r in rows if r["uuid"] == "toolu_1")
    assert "t16-keep.txt" in proposal["preview"], "a row that cannot name the file names nothing"
    assert proposal["preview"].startswith("ExitPlanMode"), (
        "the tool name leads, or the row says a call was proposed and not which")


def test_the_count_says_both_kinds(monkeypatch, has_store):
    """A mixed table reported as a message count calls tool calls messages. Stated separately
    rather than summed, since one total hides which half the reader is short of."""
    monkeypatch.setattr(session_tab, "session_messages", lambda *a, **k: _messages())
    monkeypatch.setattr(session_tab, "session_tool_calls", lambda *a, **k: _calls())
    note = _note(monkeypatch)
    assert "tool call" in note, note


def test_the_note_is_unchanged_when_there_are_no_calls(monkeypatch, has_store):
    """THE NEGATIVE CONTROL. Without it, a note that always said "and 0 tool calls" would pass the
    test above, and every session on an unmigrated store would carry a meaningless clause."""
    monkeypatch.setattr(session_tab, "session_messages", lambda *a, **k: _messages())
    monkeypatch.setattr(session_tab, "session_tool_calls",
                        lambda *a, **k: pd.DataFrame(columns=COLUMNS))
    note = _note(monkeypatch)
    assert "tool call" not in note, note
    assert "messages in this session" in note, note


def _rendered(monkeypatch):
    from c4x.store import q
    got = q("SELECT session_id FROM messages GROUP BY 1 LIMIT 1")
    if got.empty:
        pytest.skip("this store holds no messages")
    return session_tab.session_layout(got.iloc[0]["session_id"], "main", None)


def _messages_table(monkeypatch):
    for table in extract.tables(_rendered(monkeypatch)):
        if table.get("id") == "tbl-messages":
            return table["rows"]
    pytest.fail("the session tab rendered no messages table")


def _note(monkeypatch):
    lines = [t for t in extract.texts(_rendered(monkeypatch)) if "in this session" in t]
    return next((line for line in lines if "message" in line), "")

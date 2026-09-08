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


# --------------------------------------------------------------------------- the agent's note

LONG_COMMAND = "grep -rn " + ("x" * 900) + " ."


def _store_with_a_call(tmp_path, description, preview=None):
    """A temp store holding one tool call, so the timeline can be checked without a migration.

    The live store gains a column only when harvest next runs, and this package never writes, so a
    test that waited for that would be a test that runs on some machines.
    """
    import sqlite3

    path = tmp_path / "calls.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE tool_calls (
        tool_use_id TEXT PRIMARY KEY, session_id TEXT, turn_uuid TEXT, ts TEXT,
        tool_name TEXT, input_bytes INTEGER, input_preview TEXT, description TEXT)""")
    con.execute("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?,?)",
                ("toolu_1", "s1", "u1", "2026-09-07T08:03:09", "Bash", len(LONG_COMMAND),
                 preview if preview is not None else LONG_COMMAND[:500], description))
    con.commit()
    con.close()
    return path


def test_the_note_the_agent_wrote_is_on_the_row(tmp_path, monkeypatch):
    """THE DEFECT. "Located chunk files" is the line a reader scans for, and it reached no table:
    Claude Code never puts assistant text and a tool_use in the same record, so it is not assistant
    prose, and the record it does live in has no readable text and was dropped."""
    monkeypatch.setattr(store, "DB_PATH", _store_with_a_call(tmp_path, "Located chunk files"))
    rows = store.session_tool_calls("s1")
    assert len(rows) == 1
    assert "Located chunk files" in rows.iloc[0]["preview"], rows.iloc[0]["preview"]


def test_the_note_comes_before_the_command(tmp_path, monkeypatch):
    """Ordering is the whole fix. A 220-character preview of a 900-character command would push
    the note off the end, which is the same truncation that loses it inside input_preview."""
    monkeypatch.setattr(store, "DB_PATH", _store_with_a_call(tmp_path, "Located chunk files"))
    preview = store.session_tool_calls("s1").iloc[0]["preview"]
    assert preview.index("Located chunk files") < preview.index("grep -rn"), preview


def test_the_tool_name_still_leads(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", _store_with_a_call(tmp_path, "Located chunk files"))
    assert store.session_tool_calls("s1").iloc[0]["preview"].startswith("Bash: ")


def test_a_call_with_no_note_reads_normally(tmp_path, monkeypatch):
    """THE NEGATIVE CONTROL. A naive concatenation writes "Bash:  - grep..." for a call with no
    description, and every Read and Edit in the store has none."""
    monkeypatch.setattr(store, "DB_PATH", _store_with_a_call(tmp_path, None))
    preview = store.session_tool_calls("s1").iloc[0]["preview"]
    assert preview.startswith("Bash: grep -rn"), preview
    assert " - " not in preview[:20], preview


def test_the_timeline_declines_until_the_column_exists(tmp_path, monkeypatch):
    """Harvest adds the column; this package cannot. Until then the timeline is what it was, which
    is a great deal better than a tab that raises."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tool_calls (tool_use_id TEXT, session_id TEXT, input_preview TEXT)")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    out = store.session_tool_calls("s1")
    assert list(out.columns) == COLUMNS
    assert len(out) == 0

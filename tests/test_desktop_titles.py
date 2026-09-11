"""The chat name the desktop app shows, read from its own records.

Every test here points the reader at a directory this file writes, so it runs on a machine that has
never opened Claude Code Desktop. The reason is the one stated in test_archived.py: in CI there are
no records at all, and a check that cannot run is a failure rather than a pass.

What this covers that test_archived.py does not: the same files carry a `title`, it was being
discarded, and it outranks every title recovered from a transcript.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def records(tmp_path):
    """Records covering each state the reader has to tell apart.

    titled      an ordinary record with a name
    untitled    a record with no title key at all
    blank       a record whose title is whitespace
    past        a titled record whose fields sit beyond the 8 KB prefix
    nested      a record carrying a DECOY title, nested, written BEFORE the real one
    """
    folder = tmp_path / "acct" / "org"
    folder.mkdir(parents=True)

    def write(name, record):
        (folder / name).write_text(json.dumps(record), encoding="utf-8")

    write("local_titled.json", {
        "sessionId": "local_11111111-1111-4111-8111-111111111111",
        "cliSessionId": "11111111-1111-4111-8111-111111111111",
        "isArchived": False,
        "title": "Remove hooks",
        "titleSource": "auto",
    })
    write("local_untitled.json", {
        "cliSessionId": "22222222-2222-4222-8222-222222222222",
        "isArchived": False,
    })
    write("local_blank.json", {
        "cliSessionId": "33333333-3333-4333-8333-333333333333",
        "isArchived": True,
        "title": "   ",
    })
    write("local_past.json", {
        "enabledMcpTools": {f"server__tool_{i}": True for i in range(900)},
        "cliSessionId": "44444444-4444-4444-8444-444444444444",
        "isArchived": False,
        "title": "Named past the prefix",
    })
    # The decoy is FIRST, which is the whole point. A first-match regex for "title" reads this file
    # and returns "Decoy from a tool schema". The real records on this machine carry exactly this
    # shape, an MCP tool's inputSchema with a `title` property, and they only avoid the bug because
    # the chat's own title happens to be written earlier.
    # Not hypothetical. Record 3c886370 on the machine this was written on is titled exactly this
    # shape, and it is the ONE record of 180 where a naive regex disagrees with a whole-file parse.
    write("local_escaped.json", {
        "cliSessionId": "66666666-6666-4666-8666-666666666666",
        "isArchived": True,
        "title": 'Claude asking questions on "yours to call"',
    })
    write("local_nested.json", {
        "remoteMcpServersConfig": [
            {"tools": [{"inputSchema": {"properties": {"title": "Decoy from a tool schema"}}}]},
        ],
        "cliSessionId": "55555555-5555-4555-8555-555555555555",
        "isArchived": False,
        "title": "The chat's own name",
    })
    (folder / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": []}),
                                                 encoding="utf-8")
    return tmp_path


TITLED = "11111111-1111-4111-8111-111111111111"
UNTITLED = "22222222-2222-4222-8222-222222222222"
BLANK = "33333333-3333-4333-8333-333333333333"
PAST = "44444444-4444-4444-8444-444444444444"
NESTED = "55555555-5555-4555-8555-555555555555"
ESCAPED = "66666666-6666-4666-8666-666666666666"


class TestReadingTheTitle:
    def test_a_title_in_the_prefix_is_returned(self, store, records):
        found = store.read_archived_record(str(records / "acct" / "org" / "local_titled.json"))
        assert found == (TITLED, False, "Remove hooks")

    def test_a_title_past_the_prefix_is_found_by_the_fallback(self, store, records):
        path = records / "acct" / "org" / "local_past.json"
        assert path.stat().st_size > store._HEAD_BYTES, (
            "the padded record is not actually larger than the prefix, so this proves nothing")
        assert store.read_archived_record(str(path)) == (PAST, False, "Named past the prefix")

    def test_a_nested_title_written_first_does_not_win(self, store, records):
        """The regex-killer. This is the test a first-match implementation fails."""
        path = records / "acct" / "org" / "local_nested.json"
        raw = path.read_text(encoding="utf-8")
        assert raw.index('"Decoy from a tool schema"') < raw.index('"The chat\'s own name"'), (
            "the decoy is not actually written first, so this proves nothing")
        assert store.read_archived_record(str(path))[2] == "The chat's own name"

    def test_a_title_containing_quotes_survives_intact(self, store, records):
        """The failure that occurs on the REAL records, not a constructed one.

        A `"title"\\s*:\\s*"([^"]*)"` regex stops at the first escaped quote and returns
        `Claude asking questions on \\`. Of 180 titled records on the machine this was written on,
        this is the one where a naive regex and a whole-file parse disagree.
        """
        path = records / "acct" / "org" / "local_escaped.json"
        expected = 'Claude asking questions on "yours to call"'
        assert json.loads(path.read_text(encoding="utf-8"))["title"] == expected, (
            "the fixture does not hold the escaped title, so this proves nothing")
        assert store.read_archived_record(str(path)) == (ESCAPED, True, expected)

    def test_no_title_and_a_blank_title_are_the_same_answer(self, store, records):
        folder = records / "acct" / "org"
        assert store.read_archived_record(str(folder / "local_untitled.json")) == (
            UNTITLED, False, None)
        assert store.read_archived_record(str(folder / "local_blank.json")) == (BLANK, True, None)

    def test_a_non_record_is_still_not_a_record(self, store, records):
        path = records / "acct" / "org" / "scheduled-tasks.json"
        assert store.read_archived_record(str(path)) is None


class TestTheViewsOverOneScan:
    def test_only_named_chats_appear(self, store, records):
        found = store.desktop_titles(root=str(records), ttl=0)
        assert found == {
            TITLED: "Remove hooks",
            PAST: "Named past the prefix",
            NESTED: "The chat's own name",
            ESCAPED: 'Claude asking questions on "yours to call"',
        }, "a record with no title, or a blank one, is not a name"

    def test_the_archived_flag_still_covers_every_record(self, store, records):
        flags = store.archived_sessions(root=str(records), ttl=0)
        assert set(flags) == {TITLED, UNTITLED, BLANK, PAST, NESTED, ESCAPED}
        assert flags[BLANK] is True, "an untitled chat still has an archived flag"

    def test_both_views_cost_one_directory_read(self, store, records, monkeypatch):
        """The reason the cache holds both fields rather than one."""
        store.invalidate()
        reads = []
        real = store.read_archived_record

        def counted(path):
            reads.append(path)
            return real(path)

        monkeypatch.setattr(store, "read_archived_record", counted)
        store.archived_sessions(root=str(records), ttl=45.0)
        after_first = len(reads)
        store.desktop_titles(root=str(records), ttl=45.0)
        assert after_first > 0, "the first call read nothing, so the count below proves nothing"
        assert len(reads) == after_first, "the second view re-scanned the directory"
        store.invalidate()


class TestWhichNameWins:
    def test_the_desktop_title_outranks_a_transcript_title(self, store, records, monkeypatch):
        """Ranked above `custom`, which is the contested half of the decision.

        Measured on the real records when this was written: of 6 disagreements between a stored
        `custom` row and the desktop record, all 6 favoured the record. Five were a "(fork)" suffix
        the transcript lacked. The sixth carried titleSource "user" and listed the stored value in
        previousTitles, so the person had renamed the chat and the transcript kept the old name.
        """
        monkeypatch.setattr(store, "sessions_root", lambda: str(records))
        # SELECTIVE, and it has to be. A blanket `lambda: True` also answers the redaction probe,
        # which correctly suppresses the overlay and makes this test fail for the wrong reason.
        monkeypatch.setattr(store, "tables_present", lambda *names: "session_titles" in names)
        monkeypatch.setattr(store, "q", lambda *a, **k: _frame([
            {"session_id": TITLED, "kind": "custom", "title": "A name from the transcript"},
            {"session_id": TITLED, "kind": "last-prompt", "title": "whatever was typed first"},
        ]))
        store.invalidate()
        out = store.titles_for([TITLED])
        assert out[TITLED]["custom"] == "A name from the transcript", (
            "the stored row is still there")
        assert out[TITLED]["desktop"] == "Remove hooks"
        from c4x.labels import titled_path
        scratch = "C:/x/scratch-workspaces/u/scratch-2026-09-07-433162"
        assert titled_path(scratch, out[TITLED]).endswith(" - Remove hooks"), (
            "titled_path picked a transcript title over the name the app is showing")
        store.invalidate()

    def test_a_session_with_no_record_keeps_its_stored_title(self, store, records, monkeypatch):
        """The negative control: the overlay must not invent or blank a name."""
        monkeypatch.setattr(store, "sessions_root", lambda: str(records))
        monkeypatch.setattr(store, "tables_present", lambda *names: "session_titles" in names)
        monkeypatch.setattr(store, "q", lambda *a, **k: _frame([
            {"session_id": "no-record-here", "kind": "custom", "title": "Only in the store"},
        ]))
        store.invalidate()
        out = store.titles_for(["no-record-here"])
        assert out["no-record-here"] == {"custom": "Only in the store"}
        store.invalidate()


def _frame(rows):
    import pandas as pd
    return pd.DataFrame(rows, columns=["session_id", "kind", "title"])


class TestTheSessionFrameOverlay:
    """The user-visible half: what the Sessions list actually draws.

    The third row is a session with NO title of any kind, which is a real state: the SQL returns
    NULL for it and pandas stores that as NaN, not None. `_vals` normalises the two so an assertion
    can say "nothing here" without pinning which of them a given pandas version produced.
    """

    @staticmethod
    def _vals(series):
        import pandas as pd
        return [None if pd.isna(v) else v for v in series]

    def _frame(self, index=None):
        import pandas as pd
        df = pd.DataFrame({
            "session_id": [TITLED, "not-in-any-record", NESTED],
            "title": ["whatever was typed first", "Only in the store", None],
            "title_kind": ["last-prompt", "custom", None],
        })
        if index is not None:
            df.index = index
        return df

    def test_a_named_session_takes_the_desktop_title(self, store):
        df = store.overlay_desktop_titles(self._frame(), {TITLED: "Remove hooks"})
        assert self._vals(df["title"]) == ["Remove hooks", "Only in the store", None]
        assert self._vals(df["title_kind"]) == ["desktop", "custom", None]

    def test_a_session_with_no_record_is_untouched(self, store):
        df = store.overlay_desktop_titles(self._frame(), {TITLED: "Remove hooks"})
        assert df["title"][1] == "Only in the store"
        assert df["title_kind"][1] == "custom"

    def test_a_record_for_a_session_not_in_the_frame_adds_no_row(self, store):
        before = len(self._frame())
        df = store.overlay_desktop_titles(self._frame(), {"a-session-far-away": "Ghost"})
        assert len(df) == before
        assert "Ghost" not in list(df["title"])

    def test_a_non_default_index_still_lands_on_the_right_rows(self, store):
        """`df.loc[...]` would align by LABEL here and title the wrong session."""
        df = self._frame(index=[7, 0, 3])
        store.overlay_desktop_titles(df, {TITLED: "Remove hooks"})
        assert self._vals(df["title"]) == ["Remove hooks", "Only in the store", None], (
            "the overlay aligned by index label rather than by position")

    def test_an_empty_frame_does_not_raise(self, store):
        import pandas as pd
        empty = pd.DataFrame({"session_id": [], "title": [], "title_kind": []})
        assert len(store.overlay_desktop_titles(empty, {TITLED: "Remove hooks"})) == 0

    def test_no_records_changes_nothing(self, store):
        df = store.overlay_desktop_titles(self._frame(), {})
        assert self._vals(df["title_kind"]) == ["last-prompt", "custom", None]


class TestARedactedCopyGetsNoOverlay:
    """A title read at render time is not in the store, so copying and scrubbing cannot remove it.

    `tools/redact.py` exists so a public README screenshot cannot leak session titles, and its gate
    greps the COPY. Without the mark this overlay would repaint the real names at render time and
    that gate would still pass, because the leak is not in the file it checks.
    """

    def _db(self, path, marked):
        import sqlite3
        con = sqlite3.connect(str(path))
        if marked:
            con.execute("CREATE TABLE redacted_store (tool TEXT NOT NULL, note TEXT NOT NULL)")
            con.execute("INSERT INTO redacted_store VALUES ('tools/redact.py', 'a copy')")
        else:
            con.execute("CREATE TABLE sessions (session_id TEXT)")
        con.commit()
        con.close()

    def test_a_marked_store_shows_no_desktop_titles(self, store, records, tmp_path, monkeypatch):
        demo = tmp_path / "demo.db"
        self._db(demo, marked=True)
        monkeypatch.setattr(store, "DB_PATH", demo)
        store.invalidate()
        assert store.store_is_redacted() is True
        assert store.desktop_titles(root=str(records), ttl=0) == {}
        store.invalidate()

    def test_an_unmarked_store_still_shows_them(self, store, records, tmp_path, monkeypatch):
        """The control. Without it the test above passes on any empty answer for any reason."""
        ordinary = tmp_path / "ordinary.db"
        self._db(ordinary, marked=False)
        monkeypatch.setattr(store, "DB_PATH", ordinary)
        store.invalidate()
        assert store.store_is_redacted() is False
        assert store.desktop_titles(root=str(records), ttl=0), (
            "the records are unreadable, so the empty answer above proved nothing")
        store.invalidate()

    def test_redact_writes_the_mark_the_store_looks_for(self, store, tmp_path):
        """The two files agree on the table name, checked rather than assumed.

        They are in different packages and nothing imports one from the other, so the only thing
        keeping them in step is this.
        """
        import sqlite3
        sys.path.insert(0, str(ROOT / "tools"))
        import redact

        assert redact.REDACTION_MARK == store.REDACTION_MARK
        demo = tmp_path / "stamped.db"
        con = sqlite3.connect(str(demo))
        redact.stamp_as_redacted(con)
        con.commit()
        con.close()
        assert sqlite3.connect(str(demo)).execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (store.REDACTION_MARK,)).fetchone() is not None


class TestTheCacheNoticesARename:
    def test_a_rewritten_record_moves_the_stamp(self, store, records):
        """Renaming a chat rewrites one JSON file and touches no database.

        Before the records joined the stamp, `cache.get` served an entry whose version matched
        REGARDLESS of age, so on a quiet store the old name could be served indefinitely.
        """
        from c4x.api import cache
        db = records / "not-a-real.db"
        db.write_bytes(b"x")
        before = cache.stamp(str(db), store.records_fingerprint(str(records)))
        path = records / "acct" / "org" / "local_titled.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["title"] = "Renamed in the app"
        path.write_text(json.dumps(record), encoding="utf-8")
        assert cache.stamp(str(db), store.records_fingerprint(str(records))) != before

    def test_a_deleted_record_moves_the_stamp(self, store, records):
        """A count as well as an mtime: deleting the newest record moves neither on its own."""
        from c4x.api import cache
        db = records / "not-a-real.db"
        db.write_bytes(b"x")
        before = cache.stamp(str(db), store.records_fingerprint(str(records)))
        (records / "acct" / "org" / "local_untitled.json").unlink()
        assert cache.stamp(str(db), store.records_fingerprint(str(records))) != before

    def test_the_extra_part_is_opt_in(self, store, records):
        """Every existing caller stamps a bare database and must keep the value it had."""
        from c4x.api import cache
        db = records / "not-a-real.db"
        db.write_bytes(b"x")
        bare = cache.stamp(str(db))
        assert cache.stamp(str(db)) == bare
        assert cache.stamp(str(db), store.records_fingerprint(str(records))) != bare

    def test_a_missing_records_directory_does_not_raise(self, store, tmp_path):
        from c4x.api import cache
        db = tmp_path / "not-a-real.db"
        db.write_bytes(b"x")
        assert store.records_fingerprint(str(tmp_path / "nope")) is None
        # A missing directory contributes nothing, so the stamp is the bare one. It is the CALLER
        # that decides to include the part, and `store.records_fingerprint` returning None is the
        # honest answer for a machine with no desktop app.
        assert cache.stamp(str(db), None) == cache.stamp(str(db))

    def test_a_renamed_chat_is_seen_without_waiting_for_a_timer(self, store, records):
        """The record cache is validated by fingerprint, not by a 45 second clock.

        Fixing only the API cache would have moved the delay rather than removed it: the frame is
        built from `desktop_records`, and that used to serve a cached map for up to 45 seconds no
        matter what the files said.
        """
        store.invalidate()
        first = store.desktop_titles(root=str(records), ttl=45.0)
        assert first[TITLED] == "Remove hooks"
        path = records / "acct" / "org" / "local_titled.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["title"] = "Renamed in the app"
        path.write_text(json.dumps(record), encoding="utf-8")
        again = store.desktop_titles(root=str(records), ttl=45.0)
        assert again[TITLED] == "Renamed in the app", (
            "the cached map was served even though the file on disk had changed")
        store.invalidate()

    def test_an_unchanged_directory_is_not_rescanned(self, store, records, monkeypatch):
        """The control for the test above: the fingerprint must still prevent the rescan."""
        store.invalidate()
        store.desktop_titles(root=str(records), ttl=45.0)
        reads = []
        real = store.read_archived_record
        monkeypatch.setattr(store, "read_archived_record",
                            lambda p: (reads.append(p), real(p))[1])
        store.desktop_titles(root=str(records), ttl=45.0)
        assert reads == [], "the directory was rescanned even though nothing changed"
        store.invalidate()

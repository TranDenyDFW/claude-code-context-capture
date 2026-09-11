"""One row per CHAT: a resumed chat's sessions fold into its newest one.

The desktop app resumes a chat by starting a new CLI session whose transcript copies the old one,
so one chat is several sessions and the app shows one entry. `session_links`, written by harvest,
says which sessions are prefixes of which; everything here checks what the store DOES with that
table: the frame, the totals, the name, the section, the floor, cohorts, selection, and the
negative control that with no links every session is its own chat exactly as before.

Built on `build_store` from test_projects, which writes the real schema with awkward values, plus
two link rows making `s0-0` the head of a three-session chat in P:\\Alpha.
"""
import json
import sqlite3

import pytest

from tests.test_projects import build_store, forget_cached_rows

ALPHA = r"P:\Alpha"
HEAD, MID, OLD = "s0-0", "s0-1", "s0-2"      # newest to oldest, all one chat
CHAIN = [HEAD, MID, OLD]


def link(con, session_id, head_id, next_id, prefix_uuids):
    con.execute(
        """INSERT INTO session_links (session_id, head_id, next_id, head_kind, overlap,
             prefix_uuids, next_uuids, shared_uuids, method, linked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (session_id, head_id, next_id, "record" if next_id == head_id else "none",
         0.95, prefix_uuids, prefix_uuids + 20, prefix_uuids - 1, "test", "2026-09-11T00:00:00Z"))


@pytest.fixture
def chain_store(tmp_path, monkeypatch):
    """A store where s0-2 was resumed into s0-1, which was resumed into s0-0.

    The members' turns are shifted an hour apart so "the newest turn across the chain" has one
    answer: build_store stamps every session's turns with the same clock, and a tie there would
    let this suite pass on whichever row pandas happened to keep.
    """
    from c4x import store
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    link(con, MID, HEAD, HEAD, 34)
    link(con, OLD, HEAD, MID, 17)
    for sid, hour in ((MID, "T01:"), (OLD, "T02:")):
        con.execute("UPDATE turns SET ts = replace(ts, 'T00:', ?) WHERE session_id = ?", (hour, sid))
        con.execute("UPDATE messages SET ts = replace(ts, 'T00:', ?) WHERE session_id = ?", (hour, sid))
    con.commit()
    con.close()
    records = tmp_path / "records"
    (records / "acct" / "org").mkdir(parents=True)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "sessions_root", lambda: str(records))
    forget_cached_rows()
    yield path
    forget_cached_rows()


def frame(store):
    forget_cached_rows()
    return store.session_rows(ttl=0)


def sql(path, query, params=()):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


class TestOneRowPerChat:
    def test_prefixes_are_folded_into_the_head(self, chain_store, store):
        df = frame(store)
        ids = set(df["session_id"])
        assert HEAD in ids
        assert MID not in ids and OLD not in ids, "a superseded session is still its own row"
        assert {"s1-0", "s1-1", "s1-2"} <= ids, "an unlinked session is its own chat, as before"
        assert len(df) == 4

    def test_every_total_is_recomputed_over_the_members(self, chain_store, store):
        row = frame(store).set_index("session_id").loc[HEAD]
        marks = ",".join("?" * len(CHAIN))
        turns, peak = sql(chain_store, f"""SELECT COUNT(*), MAX(total_resident) FROM turns
                                            WHERE session_id IN ({marks})""", tuple(CHAIN))[0]
        comps = sql(chain_store, f"SELECT COUNT(*) FROM compactions WHERE session_id IN ({marks})",
                    tuple(CHAIN))[0][0]
        current = sql(chain_store, f"""SELECT total_resident FROM turns WHERE session_id IN ({marks})
                                        ORDER BY ts DESC LIMIT 1""", tuple(CHAIN))[0][0]
        assert int(row["turns"]) == turns == 51, "turns is the sum over the chain"
        assert int(row["peak"]) == peak, "peak is the max over the chain"
        assert int(row["compactions"]) == comps == 3
        assert int(row["current"]) == current, "current is the newest turn across the chain"
        assert int(row["cli_sessions"]) == 3

    def test_the_head_alone_does_not_account_for_the_row(self, chain_store, store):
        """The control: the numbers above are not what the head's own rows would give."""
        row = frame(store).set_index("session_id").loc[HEAD]
        own = sql(chain_store, "SELECT COUNT(*) FROM turns WHERE session_id = ?", (HEAD,))[0][0]
        assert own == 17 and int(row["turns"]) == 51


class TestTheName:
    def test_the_desktop_record_of_the_head_names_the_chat(self, chain_store, store, tmp_path):
        record = tmp_path / "records" / "acct" / "org" / "local_head.json"
        record.write_text(json.dumps({"cliSessionId": HEAD, "isArchived": False,
                                      "title": "Alpha chat"}), encoding="utf-8")
        row = frame(store).set_index("session_id").loc[HEAD]
        assert row["title"] == "Alpha chat" and row["title_kind"] == "desktop"

    def test_without_a_record_the_head_keeps_its_own_stored_title(self, chain_store, store):
        stored = sql(chain_store, "SELECT title FROM session_titles WHERE session_id = ?", (HEAD,))[0][0]
        assert frame(store).set_index("session_id").loc[HEAD]["title"] == stored

    def test_a_head_with_no_title_takes_the_newest_members(self, chain_store, store):
        con = sqlite3.connect(str(chain_store))
        con.execute("DELETE FROM session_titles WHERE session_id = ?", (HEAD,))
        con.commit()
        con.close()
        newest = sql(chain_store, "SELECT session_id FROM turns ORDER BY ts DESC LIMIT 1")[0][0]
        assert newest != HEAD, "the fixture's newest member must not be the head for this to test anything"
        expected = sql(chain_store, "SELECT title FROM session_titles WHERE session_id = ?", (newest,))[0][0]
        assert frame(store).set_index("session_id").loc[HEAD]["title"] == expected

    def test_a_prefix_id_is_named_by_its_chat(self, chain_store, store):
        assert store.session_name(OLD) == store.session_name(HEAD) != ""


class TestSectionAndFloor:
    def test_the_section_follows_the_head(self, chain_store, store):
        """The head is the transcript the app resumes: gone means the chat is gone."""
        con = sqlite3.connect(str(chain_store))
        con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
                    (store.HOME_DIR + r"\.claude\projects\nope\s0-0.jsonl", HEAD))
        con.commit()
        con.close()
        df = frame(store).set_index("session_id")
        assert df.loc[HEAD]["section"] == "Deleted from this machine"
        # The members' paths are not under HOME, so per session they would read as imported.
        assert df.loc["s1-0"]["section"] == "Imported from another machine"

    def test_the_floor_applies_to_the_chat_not_to_its_members(self, chain_store, store):
        con = sqlite3.connect(str(chain_store))
        link(con, "s1-1", "s1-0", "s1-0", 3)
        con.execute("DELETE FROM turns WHERE session_id IN ('s1-0', 's1-1') AND line_no >= 3")
        con.commit()
        con.close()
        for sid in ("s1-0", "s1-1"):
            assert sql(chain_store, "SELECT COUNT(*) FROM turns WHERE session_id = ?", (sid,))[0][0] == 3
        df = frame(store).set_index("session_id")
        assert "s1-0" in df.index and int(df.loc["s1-0"]["turns"]) == 6, (
            "two members below the floor make one chat above it")
        assert "s1-1" not in df.index

    def test_two_members_below_the_floor_without_a_link_are_both_dropped(self, chain_store, store):
        """The control for the test above."""
        con = sqlite3.connect(str(chain_store))
        con.execute("DELETE FROM turns WHERE session_id IN ('s1-0', 's1-1') AND line_no >= 3")
        con.commit()
        con.close()
        ids = set(frame(store)["session_id"])
        assert "s1-0" not in ids and "s1-1" not in ids


class TestCohortsAndSelection:
    def test_a_cohort_names_every_member_head_first(self, chain_store, store):
        assert store.cohort_sessions(f"project::{ALPHA}", ttl=0) == CHAIN

    def test_the_population_sentence_counts_chats(self, chain_store, store):
        assert store.population_label(None, f"project::{ALPHA}", "main").startswith("1 session in")

    def test_scoped_on_a_prefix_covers_the_chain(self, chain_store, store):
        where, args = store.scoped(OLD, "all")
        assert "session_id IN (?,?,?)" in where and set(args) == set(CHAIN)

    def test_scoped_on_an_unlinked_session_is_unchanged(self, chain_store, store):
        assert store.scoped("s1-0", "all") == ("AND session_id = ?", ("s1-0",))

    def test_chat_head_and_members(self, chain_store, store):
        assert store.chat_head(OLD) == HEAD and store.chat_head(HEAD) == HEAD
        assert store.chat_head("never-seen") == "never-seen" and store.chat_head(None) is None
        assert store.chat_members(MID) == CHAIN and store.chat_members("s1-2") == ["s1-2"]

    def test_direct_readers_cover_the_chain(self, chain_store, store):
        assert len(store.session_turns(OLD, include_sidechain=True)) == 51
        assert len(store.session_compactions(OLD)) == 3
        assert len(store.session_messages(OLD)) == 51

    def test_the_default_other_arm_is_never_a_member(self, chain_store, store):
        from c4x.tabs.compare import default_arm_b
        assert default_arm_b(HEAD) not in CHAIN
        assert default_arm_b(OLD, f"project::{ALPHA}") is None, (
            "inside the chat's own cohort there is nothing that is not the chat")

    def test_the_project_option_counts_one_chat(self, chain_store, store):
        option = next(o for o in store.cohort_options() if o["value"] == f"project::{ALPHA}")
        assert "(1 listed)" in option["label"]

    def test_the_browse_table_selects_the_head(self, chain_store, store):
        from c4x.ui.callbacks.selection import _pick_from_table
        assert _pick_from_table([0], [{"session_id": OLD}]) == HEAD


class TestTheApi:
    def test_a_prefix_selection_is_resolved_and_reported(self, chain_store, store):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient
        from c4x.api import cache
        from c4x.api.main import api
        client = TestClient(api, base_url="http://127.0.0.1:8059")
        cache.clear()
        first = client.get("/api/tab/tab-session/render", params={"session": OLD})
        assert first.json()["session"] == HEAD, "the payload names the chat, not the id asked with"
        assert first.headers.get("x-c4x-session-requested") == OLD
        assert "session_requested" not in first.json(), (
            "which id a caller asked with is per request and must never enter the shared cache")
        again = client.get("/api/tab/tab-session/render", params={"session": HEAD})
        assert again.headers.get("x-c4x-cache") == "hit", (
            "the prefix and the head must share one cache entry")
        assert "x-c4x-session-requested" not in again.headers
        assert again.json()["session"] == HEAD
        # no_cache keeps the header too, so a caller bypassing the cache is told the same thing.
        fresh = client.get("/api/tab/tab-session/render", params={"session": OLD, "no_cache": 1})
        assert fresh.headers.get("x-c4x-session-requested") == OLD and fresh.json()["session"] == HEAD


class TestExportAndDelete:
    """The acceptance rule, with chains: a delete removes exactly what the backup contains."""

    def test_the_export_carries_every_member_and_the_links(self, chain_store, store, tmp_path):
        from c4x import projects
        out = tmp_path / "alpha.db"
        manifest = projects.export(ALPHA, out)
        assert sorted(manifest["session_ids"]) == sorted(CHAIN), (
            "a backup of the chat must name its superseded sessions, not only the head")
        assert len(sql(out, "SELECT * FROM session_links")) == 2

    def test_a_delete_removes_the_chain_and_leaves_another_chats_links(self, chain_store, store, tmp_path):
        from c4x import projects
        con = sqlite3.connect(str(chain_store))
        link(con, "s1-1", "s1-0", "s1-0", 17)
        con.commit()
        con.close()
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        assert sorted(result["session_ids"]) == sorted(CHAIN) if "session_ids" in result else True
        marks = ",".join("?" * len(CHAIN))
        for table in ("sessions", "turns", "messages", "compactions", "session_links"):
            left = sql(chain_store, f"SELECT COUNT(*) FROM {table} WHERE session_id IN ({marks})",
                       tuple(CHAIN))[0][0]
            assert left == 0, f"{table} still holds rows of the deleted chat"
        assert sql(chain_store, "SELECT session_id, head_id FROM session_links") == [("s1-1", "s1-0")]
        assert HEAD not in set(frame(store)["session_id"])


class TestNoLinks:
    def test_dropping_the_table_restores_one_row_per_session(self, chain_store, store):
        con = sqlite3.connect(str(chain_store))
        con.execute("DROP TABLE session_links")
        con.commit()
        con.close()
        df = frame(store).set_index("session_id")
        assert len(df) == 6 and MID in df.index and OLD in df.index
        assert int(df.loc[HEAD]["turns"]) == 17 and int(df.loc[HEAD]["cli_sessions"]) == 1
        assert store.chat_members(OLD) == [OLD]

r"""A review run folds into the chat it reviewed, and only where the user said it should.

Harvest writes `review_links` (the rule and its measurement sit in tools/harvest.mjs); everything
here checks what the store DOES with that table: a run resolves to its chat's head, it is listed
nowhere on its own, it is counted on the chat, it reaches the chat's numbers under the subagent
scope and never under the main one, the transcript readers never show it, the chat's page lists it
with its verdict and the prompt it was answering, and a store without the table behaves exactly as
before.

Built on `build_store` from test_projects (the real schema, twelve turns per session, so every
session clears the listing floor on its own), plus one chain link, two review links, two runs
with turns and messages of their own, and cost ledgers for a chat and its run.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

ALPHA, BETA = r"P:\Alpha", r"P:\Beta"
HEAD, OLD = "s0-0", "s0-1"           # one chat in Alpha: OLD resumed into HEAD
LONE = "s1-0"                        # an unchained chat in Beta
RUN, RUN2 = "r-1", "r-2"             # a review of OLD (so of the chat), a review of LONE
ORPHAN = "r-4"                       # a review the store cannot place: a NULL head
L1 = ("The parser now rejects a trailing comma and the three tests that covered it pass again "
      "after the rewrite")
L4 = "Both Delta chats said this exact sentence once, so a run quoting only it ties to neither"
PROMPT = ("You are reviewing another Claude instance's work before it is allowed to finish its "
          "turn.\n\n--- THE WORK ---\nCLAUDE SAID: " + L1 + "\n")


def _run(con, sid, cwd, at_minute, verdict, parent, reply, turns=6):
    """A one-shot session: six turns (well over the floor), one typed prompt, one reply."""
    first = f"2026-08-01T00:{at_minute:02d}:00Z"
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, "slug-r", cwd, "main", "2.1.263", "claude-desktop", first, first,
                 rf"C:\t\{sid}.jsonl"))
    for i in range(turns):
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, input_tokens,
                         cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
                         thinking_tokens, total_resident, is_sidechain, file_path, line_no)
                       VALUES (?, ?, ?, 'claude-opus-5', ?, 100, 0, 50, 10, 0, 20000, 0, 'f', ?)""",
                    (f"{sid}-t{i}", sid, f"2026-08-01T00:{at_minute:02d}:{i:02d}Z",
                     f"req-{sid}-{i}", i))
    con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                     is_sidechain, file_path, line_no)
                   VALUES (?, ?, ?, 'user', 'typed', ?, ?, 0, 'f', 1)""",
                (f"{sid}-p", sid, first, PROMPT, len(PROMPT)))
    con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                     is_sidechain, file_path, line_no)
                   VALUES (?, ?, ?, 'assistant', 'assistant', ?, ?, 0, 'f', 2)""",
                (f"{sid}-a", sid, f"2026-08-01T00:{at_minute + 1:02d}:00Z", reply, len(reply)))
    con.execute("""INSERT INTO review_links (session_id, head_id, hits, snippets, verdict, method,
                     linked_at) VALUES (?, ?, 2, 3, ?, 'test', '2026-08-02T00:00:00Z')""",
                (sid, parent, verdict))


@pytest.fixture
def review_store(tmp_path, monkeypatch):
    from c4x import store
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    con.execute("""INSERT INTO session_links (session_id, head_id, next_id, head_kind, overlap,
                     prefix_uuids, next_uuids, shared_uuids, method, linked_at)
                   VALUES (?, ?, ?, 'none', 0.95, 10, 20, 9, 'test', '2026-09-12T00:00:00Z')""",
                (OLD, HEAD, HEAD))
    # The prompts the chats were answering when their reviews started, and one after.
    for uuid_, sid, ts, text in (("old-p1", OLD, "2026-08-01T00:10:00Z", "please fix the parser"),
                                 ("old-p2", OLD, "2026-08-01T00:40:00Z", "and now the tests"),
                                 ("lone-p1", LONE, "2026-08-01T00:30:00Z", "start over")):
        con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                         is_sidechain, file_path, line_no)
                       VALUES (?, ?, ?, 'user', 'typed', ?, ?, 0, 'f', 9)""",
                    (uuid_, sid, ts, text, len(text)))
    _run(con, RUN, ALPHA, 20, "APPROVED", OLD, "APPROVED\nfine")
    _run(con, RUN2, BETA, 50, "PROBLEMS", LONE, "PROBLEMS\n- one thing")
    # A run whose transcript carries no usage: messages and a session row, no turns (an sdk-py
    # review here is shaped like this). Its time is the session row's own start.
    _run(con, "r-3", BETA, 55, None, LONE, "Looking at this", turns=0)
    con.execute("""INSERT INTO review_links (session_id, head_id, hits, snippets, verdict, method,
                     linked_at) VALUES ('r-3', ?, 3, 8, NULL, 'test', '2026-08-02T00:00:00Z')
                   ON CONFLICT(session_id) DO NOTHING""", (LONE,))
    # An ORPHAN: harvest knows it is a review (its lines are said somewhere, its reply a verdict)
    # and cannot say of what. A NULL head: it folds into nothing and is listed nowhere.
    _run(con, ORPHAN, BETA, 58, "APPROVED", None, "APPROVED\nof a chat this store cannot name")
    for sid, usd in ((HEAD, 1.0), (RUN, 0.25)):
        con.execute("INSERT INTO cost_state (session_id, total_cost_usd) VALUES (?, ?)", (sid, usd))
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


@pytest.fixture
def store(review_store):
    from c4x import store
    return store


class TestTheMap:
    def test_a_run_knows_its_chat_and_a_chat_its_runs(self, store):
        parent_of, runs_of = store.review_links(ttl=0)
        assert parent_of == {RUN: OLD, RUN2: LONE, "r-3": LONE, ORPHAN: None}
        assert runs_of == {OLD: [RUN], LONE: ["r-3", RUN2]}, "newest first, and no list for None"

    def test_an_orphan_folds_into_nothing_and_is_listed_nowhere(self, store):
        assert store.chat_head(ORPHAN) == ORPHAN
        assert store.chat_members(ORPHAN) == [ORPHAN]
        assert store.chat_members(ORPHAN, reviews=True) == [ORPHAN], "it is nobody's run"
        rows = store.session_rows(ttl=0)
        assert ORPHAN not in set(rows["session_id"]), "still a run, so still no row of its own"
        assert int(rows["reviews"].sum()) == 3, "and it counts toward no chat"
        assert list(store.chat_reviews(LONE)["session_id"]) == ["r-3", RUN2]
        from c4x.ui.header import selector_options
        assert ORPHAN not in {o["value"] for o in selector_options()}

    def test_a_run_resolves_to_the_head_of_the_chat_it_reviewed(self, store):
        assert store.chat_head(RUN) == HEAD, "through the chain: OLD folds into HEAD"
        assert store.chat_head(RUN2) == LONE
        assert store.chat_head("nobody") == "nobody" and store.chat_head(None) is None

    def test_members_leave_the_runs_out_unless_asked(self, store):
        assert store.chat_members(HEAD) == [HEAD, OLD]
        assert store.chat_members(HEAD, reviews=True) == [HEAD, OLD, RUN]
        assert store.chat_members(RUN) == [HEAD, OLD], "a run names its chat, not itself"
        assert store.chat_members(LONE, reviews=True) == [LONE, "r-3", RUN2]
        clause, params = store.chain_where(HEAD)
        assert RUN not in params and clause.startswith("session_id IN")
        assert RUN in store.chain_where(HEAD, reviews=True)[1]

    def test_a_store_without_the_table_is_the_store_it_was(self, store, review_store):
        con = sqlite3.connect(str(review_store))
        con.execute("DROP TABLE review_links")
        con.commit()
        con.close()
        forget_cached_rows()
        assert store.review_links(ttl=0) == ({}, {})
        assert store.chat_members(HEAD, reviews=True) == [HEAD, OLD]
        rows = store.session_rows(ttl=0)
        assert RUN in set(rows["session_id"]), "with no link a run is an ordinary session"
        assert set(rows["reviews"]) == {0}


class TestTheLists:
    def test_a_run_is_no_row_and_its_chat_counts_it(self, store):
        rows = store.session_rows(ttl=0)
        listed = set(rows["session_id"])
        assert RUN not in listed and RUN2 not in listed, "six turns, and still not a chat"
        assert "r-3" not in listed, "no turns at all, and still not a chat"
        by_id = dict(zip(rows["session_id"], rows["reviews"], strict=True))
        assert by_id[HEAD] == 1 and by_id[LONE] == 2
        assert sum(by_id.values()) == 3
        assert int(rows.loc[rows["session_id"] == HEAD, "cli_sessions"].iloc[0]) == 2, (
            "the run is not a CLI session of the chat")

    def test_the_summary_counts_chats_not_runs(self, store):
        stats = store.overview_stats()
        total = store.q("SELECT COUNT(*) n FROM sessions")["n"].iloc[0]
        assert stats["sessions"] == int(total) - 4, "three placed runs and the orphan"

    def test_the_picker_says_how_many_reviews_a_chat_received(self, store):
        from c4x.ui.header import selector_options
        labels = {o["value"]: o["label"] for o in selector_options()}
        assert labels[HEAD].endswith("  ·  1 review"), labels[HEAD]
        assert "review" not in labels["s0-2"]
        assert RUN not in labels

    def test_the_work_column_and_totals_carry_reviews(self, store):
        from c4x.tabs.sessions import work_summary
        assert store.chat_work_counts(HEAD)["reviews"] == 1
        assert store.chat_work_counts(HEAD)["harvested"]["reviews"] is True
        assert store.chat_work_counts("s0-2")["reviews"] == 0
        totals = store.chat_work_totals(ttl=0)
        assert totals[HEAD]["reviews"] == 1 and totals[LONE]["reviews"] == 2
        assert work_summary({"reviews": 2}) == "2 reviews"
        assert work_summary({"plans": 1, "reviews": 1}) == "1 plan, 1 review"


class TestTheNumbers:
    def test_scoped_folds_the_run_under_the_subagent_scope_only(self, store):
        _w, main = store.scoped(HEAD, "main")
        _w, everything = store.scoped(HEAD, "all")
        assert RUN not in main and set(main) == {HEAD, OLD}
        assert RUN in everything and set(everything) == {HEAD, OLD, RUN}
        _w, cohort_main = store.scoped(None, "main", cohort=f"project::{ALPHA}")
        _w, cohort_all = store.scoped(None, "all", cohort=f"project::{ALPHA}")
        assert RUN not in cohort_main and RUN in cohort_all

    def test_the_measured_cost_of_the_chat_includes_its_run_under_that_scope(self, store):
        assert store.measured_cost(store.chat_members(HEAD))["total_usd"] == 1.0
        assert store.measured_cost(store.chat_members(HEAD, reviews=True))["total_usd"] == 1.25

    def test_the_transcript_readers_never_show_the_run(self, store):
        uuids = set(store.session_messages(HEAD)["uuid"])
        assert "old-p1" in uuids
        assert f"{RUN}-p" not in uuids, "the reviewer's prompt is not something the chat typed"
        turns = set(store.session_turns(HEAD, include_sidechain=True)["uuid"])
        assert f"{RUN}-t0" not in turns


class TestTheChatPage:
    def test_each_review_says_when_what_and_after_which_prompt(self, store):
        df = store.chat_reviews(HEAD)
        assert list(df["session_id"]) == [RUN]
        row = df.iloc[0]
        assert row["verdict"] == "APPROVED" and int(row["round"]) == 1
        assert row["after_prompt"] == "please fix the parser", "the newest prompt before the run"
        assert row["after_prompt_ts"] == "2026-08-01T00:10:00Z"
        assert int(row["calls"]) == 6 and int(row["input_tokens"]) == 600
        assert int(row["cache_read"]) == 300 and int(row["output_tokens"]) == 60
        assert row["cost_usd"] > 0
        assert int(row["hits"]) == 2 and int(row["snippets"]) == 3
        lone = store.chat_reviews(LONE)
        assert list(lone["session_id"]) == ["r-3", RUN2], "newest first"
        # pandas reads a missing verdict and a missing cost as NaN, which is what the route then
        # sends as null; `is None` would be the wrong question of a frame.
        assert pd.isna(lone.iloc[0]["verdict"]) and lone.iloc[1]["verdict"] == "PROBLEMS"
        assert list(lone["round"]) == [2, 1]
        assert lone.iloc[1]["after_prompt"] == "start over"
        turnless = lone.iloc[0]
        assert turnless["ts"] == "2026-08-01T00:55:00Z", "the session row's own start"
        assert int(turnless["calls"]) == 0 and pd.isna(turnless["cost_usd"])
        assert turnless["after_prompt"] == "start over"
        assert store.chat_reviews("s0-2").empty

    def test_the_route_carries_the_reviews_beside_the_other_lists(self, store):
        from fastapi.testclient import TestClient

        from c4x.api.main import api
        body = TestClient(api).get(f"/api/chat/{HEAD}").json()
        assert body["reviews_total"] == 1
        assert [r["session_id"] for r in body["reviews"]] == [RUN]
        assert body["reviews"][0]["verdict"] == "APPROVED"
        assert body["harvested"]["reviews"] is True
        assert body["chat"] == [HEAD, OLD], "the chat's own sessions, the run beside them"


class TestTheTwin:
    def test_snippets_pin_the_same_inputs_as_the_node_self_test(self):
        """`reviewSnippets` in tools/harvest.mjs pins these three; a drift shows on either side."""
        from c4x import reviews
        mixed = ("short\nCLAUDE SAID: " + L1 + "\nOUTPUT WAS: caf\u00e9 " + L1
                 + "\nUSER: " + L4 + "\n")
        assert reviews.snippets(mixed) == [L4[-120:], L1[-120:]]
        assert reviews.snippets("") == []
        assert reviews.snippets("A LABEL: " + "x" * 300) == ["x" * 120]

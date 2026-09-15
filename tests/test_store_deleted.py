"""A chat deleted in the desktop app is hidden from every list, with its whole chain.

Harvest writes `desktop_records` (the rule and the measured marker are in tools/harvest.mjs and
docs/desktop-records.md section 7); this package only reads it. What is tested here is what the
readers do with a row whose `deleted_at` is set: the chat leaves the session frame, the pickers,
the cohorts and the Summary's count, while its id still opens its page. A row with `gone_at`
only (c4x took the record back, or a reinstall) hides nothing.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

ALPHA, BETA = r"P:\Alpha", r"P:\Beta"
HEAD, PREFIX, OTHER = "s0-0", "s0-1", "s0-2"    # one chat in Alpha: PREFIX resumed into HEAD
GONE_ONLY = "s1-0"                               # Beta: its record went without a marker


def _record(con, uuid, sid, deleted_at=None, gone_at=None):
    con.execute("""INSERT INTO desktop_records (record_uuid, session_id, dir, title, archived,
                     first_seen, last_seen, gone_at, deleted_at, source)
                   VALUES (?, ?, 'X:/records/a/o', 'A chat', 0, '2026-09-01T00:00:00Z',
                           '2026-09-01T00:00:00Z', ?, ?, 'disk')""",
                (uuid, sid, gone_at, deleted_at))


@pytest.fixture
def deleted_store(tmp_path, monkeypatch):
    from c4x import store
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    con.execute("""INSERT INTO session_links (session_id, head_id, next_id, head_kind, overlap,
                     prefix_uuids, next_uuids, shared_uuids, method, linked_at)
                   VALUES (?, ?, ?, 'none', 0.95, 10, 20, 9, 'test', '2026-09-12T00:00:00Z')""",
                (PREFIX, HEAD, HEAD))
    # THE DELETED RECORD NAMES THE PREFIX, not the head: the app keeps the id a chat started
    # with while the CLI resumes into new ones, so hiding only the named session would leave the
    # chat's newest transcript listed under its own name.
    _record(con, "11111111-1111-4111-8111-111111111111", PREFIX,
            deleted_at="2026-09-15T01:01:21.365Z", gone_at="2026-09-15T01:05:00Z")
    _record(con, "22222222-2222-4222-8222-222222222222", GONE_ONLY, gone_at="2026-09-15T01:05:00Z")
    _record(con, "33333333-3333-4333-8333-333333333333", OTHER)
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


@pytest.fixture
def store(deleted_store):
    from c4x import store
    return store


class TestWhatIsHidden:
    def test_the_deleted_chat_leaves_the_frame_with_its_whole_chain(self, store):
        assert store.deleted_in_app(ttl=0) == frozenset({PREFIX})
        listed = set(store.session_rows(ttl=0)["session_id"])
        assert HEAD not in listed and PREFIX not in listed, "the head goes with the prefix"
        assert {OTHER, GONE_ONLY, "s1-1", "s1-2"} <= listed

    def test_a_record_that_merely_went_hides_nothing(self, store):
        assert GONE_ONLY in set(store.session_rows(ttl=0)["session_id"])

    def test_the_summary_count_and_the_pickers_agree_with_the_frame(self, store):
        from c4x.ui.header import selector_options
        total = store.q("SELECT COUNT(*) n FROM sessions")["n"].iloc[0]
        assert store.overview_stats()["sessions"] == int(total) - 2
        values = {o["value"] for o in selector_options()}
        assert HEAD not in values and PREFIX not in values and OTHER in values
        alpha = next(o for o in store.cohort_options() if o["value"] == f"project::{ALPHA}")
        assert alpha["label"].endswith("(1 listed)"), alpha["label"]
        assert store.cohort_sessions(f"project::{ALPHA}", ttl=0) == [OTHER]

    def test_a_raw_id_still_opens_its_chat(self, store):
        assert store.chat_head(PREFIX) == HEAD
        assert store.chat_members(HEAD) == [HEAD, PREFIX]
        assert not store.session_turns(HEAD).empty

    def test_a_store_without_the_table_is_the_store_it_was(self, store, deleted_store):
        con = sqlite3.connect(str(deleted_store))
        con.execute("DROP TABLE desktop_records")
        con.commit()
        con.close()
        forget_cached_rows()
        assert store.deleted_in_app(ttl=0) == frozenset()
        assert store.hidden_sessions_sql().startswith("SELECT session_id FROM review_links") or (
            "desktop_records" not in store.hidden_sessions_sql())
        assert HEAD in set(store.session_rows(ttl=0)["session_id"])

    def test_the_subquery_names_the_deleted_the_head_and_the_members(self, store):
        rows = store.q(f"SELECT DISTINCT session_id FROM ({store.hidden_sessions_sql()})")
        assert set(rows["session_id"]) == {HEAD, PREFIX}


def test_the_reader_is_cleared_with_the_other_maps(deleted_store):
    from c4x import store
    assert store.deleted_in_app() == frozenset({PREFIX})
    con = sqlite3.connect(str(deleted_store))
    con.execute("UPDATE desktop_records SET deleted_at = NULL")
    con.commit()
    con.close()
    assert store.deleted_in_app() == frozenset({PREFIX}), "cached, like the chain map"
    store.invalidate()
    assert store.deleted_in_app() == frozenset()

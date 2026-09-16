"""Adopt leaves child runs out, counts them under the folder they fold into, and takes back the
records an earlier build wrote for them, the way it does for review runs."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import adopt, store  # noqa: E402
from tests.test_adopt import DELTA, machine  # noqa: E402, F401
from tests.test_projects import forget_cached_rows  # noqa: E402

CHILD, BATCH, PLAIN = "x-child", "x-batch", "x-plain"
NEST = DELTA + r"\tmp\c1"
OTHER = DELTA + r"\other"
LINKS = [(CHILD, "s3-0", DELTA, "prompt", "toolu_c", 4, "test", "2026-08-05T13:00:00Z"),
         (BATCH, None, DELTA, "batch", None, 3, "test", "2026-08-05T13:00:00Z")]


def _session(con, home, sid, cwd):
    """A desktop session with its transcript on disk and two turns: a candidate by every clause."""
    path = home / f"{sid}.jsonl"
    path.write_text('{"type":"summary"}\n', encoding="utf-8")
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, "slug-x", cwd, "main", "2.1.229", "claude-desktop",
                 "2026-08-05T00:00:00Z", "2026-08-05T00:00:00Z", str(path)))
    for i in range(2):
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id,
                         output_tokens, file_path, line_no)
                       VALUES (?,?,?,'claude-sonnet-5',?,1,'f',?)""",
                    (f"{sid}-t{i}", sid, f"2026-08-05T12:0{i}:00Z", f"req-{sid}-{i}", i))


@pytest.fixture
def with_runs(machine, tmp_path):
    """The adopt fixture plus three one-shots under Delta: a child of s3-0 and a batch run in a
    tmp folder, and a plain chat in another folder. Nothing marks the first two as runs yet."""
    home = tmp_path / "home" / ".claude" / "projects" / "slug"
    con = sqlite3.connect(str(store.DB_PATH))
    _session(con, home, CHILD, NEST)
    _session(con, home, BATCH, NEST)
    _session(con, home, PLAIN, OTHER)
    con.commit()
    con.close()
    forget_cached_rows()
    return machine


def link(rows=LINKS):
    """What harvest's deriveRuns would have written."""
    con = sqlite3.connect(str(store.DB_PATH))
    con.executemany("INSERT INTO run_links VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    forget_cached_rows()


def offered(state):
    return sorted(s["session_id"] for g in state["groups"] for s in g["sessions"])


class TestWhatIsOffered:
    def test_runs_are_never_offered_and_are_counted_under_the_folder_they_fold_into(self, with_runs):
        link()
        state = adopt.state()
        assert PLAIN in offered(state)
        assert CHILD not in offered(state) and BATCH not in offered(state)
        by_cwd = {g["cwd"]: g for g in state["groups"]}
        assert NEST not in by_cwd, "a folder holding only runs is no row at all"
        assert by_cwd[DELTA]["runs"] == 2 and by_cwd[OTHER]["runs"] == 0
        assert (state["runs"], state["runs_placed"], state["runs_batched"],
                state["runs_unplaced"], state["run_records"]) == (2, 1, 1, 0, 0)

    def test_without_links_the_same_sessions_are_ordinary_chats(self, with_runs):
        state = adopt.state()
        assert CHILD in offered(state) and BATCH in offered(state)
        by_cwd = {g["cwd"]: g for g in state["groups"]}
        assert by_cwd[NEST]["runs"] == 0 and state["runs"] == 0

    def test_adopting_the_folder_writes_no_record_for_a_run(self, with_runs):
        link()
        report = adopt.adopt([DELTA, OTHER])
        assert sorted(w["session_id"] for w in report["written"]) == ["s3-0", "s3-1", PLAIN]


class TestTakingRecordsBack:
    def test_records_an_earlier_build_wrote_for_runs_are_taken_back(self, with_runs):
        # Adopted while nothing marked them as runs, the way the first build adopted 58 reviews.
        report = adopt.adopt([NEST])
        assert sorted(w["session_id"] for w in report["written"]) == [BATCH, CHILD]
        paths = {w["session_id"]: Path(w["path"]) for w in report["written"]}
        link()
        assert adopt.state()["run_records"] == 2
        result = adopt.unadopt_reviews()
        removed = {r["session_id"]: r for r in result["removed"]}
        assert set(removed) == {CHILD, BATCH} and result["kept"] == 0
        assert removed[CHILD]["kind"] == "run" and removed[CHILD]["parent"] == "s3-0"
        assert removed[CHILD]["reviewed"] is None
        assert removed[BATCH]["kind"] == "run" and removed[BATCH]["parent"] is None
        assert result["restart_required"] is True
        assert not paths[CHILD].exists() and not paths[BATCH].exists()
        assert adopt.state()["run_records"] == 0
        assert adopt.unadopt_reviews()["removed"] == [], "nothing left to take back"

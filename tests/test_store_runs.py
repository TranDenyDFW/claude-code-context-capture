"""What the store does with `run_links`: a child run folds into the chat that spawned it, a batch
folds under the project above it, and neither is a chat of its own anywhere on the page.

The rule that writes the table lives in `tools/harvest.mjs` (`deriveRuns`, proven by its own
self-test); these rows are written by hand, the way a harvested store would carry them."""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

ALPHA, BETA = r"P:\Alpha", r"P:\Beta"
CHILD, BATCH, LONE = "run-child", "run-batch", "run-lone"
CHILD_CWD, BATCH_CWD, LONE_CWD = r"P:\Alpha\tmp\c1", r"P:\Alpha\tmp\c2", r"Q:\x\p0"
# Above SESSION_TURN_FLOOR, so a run is hidden by the fold and not by the floor.
TURNS = 6
RUN_BYTES = 5000


def _run(con, sid, cwd, ts="2026-08-05T10:00:00Z"):
    """One headless run: a session with its own folder, six turns, one typed prompt and one tool
    call that returned RUN_BYTES."""
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, f"slug-{sid}", cwd, "main", "2.1.229", "claude-desktop", ts, ts,
                 rf"C:\t\{sid}.jsonl"))
    for i in range(TURNS):
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, input_tokens,
                         cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
                         total_resident, is_sidechain, file_path, line_no)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"{sid}-t{i}", sid, f"2026-08-05T10:0{i}:00Z", "claude-opus-5",
                     f"req-{sid}-{i}", 100, 0, 0, 50, 1000, 0, rf"C:\t\{sid}.jsonl", i))
    con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                     is_sidechain, file_path, line_no)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f"{sid}-m", sid, ts, "user", "typed", f"Probe {sid}: create the file and stop",
                 30, 0, rf"C:\t\{sid}.jsonl", 0))
    con.execute("""INSERT INTO tool_calls (tool_use_id, session_id, turn_uuid, ts, tool_name,
                     server_name, target, input_sha1, input_bytes, result_bytes, is_error,
                     is_sidechain, file_path, line_no)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{sid}-tc", sid, f"{sid}-t0", ts, "Bash", None, None, "sha", 10, RUN_BYTES, 0,
                 0, rf"C:\t\{sid}.jsonl", 1))


@pytest.fixture
def runs_store(tmp_path, monkeypatch):
    """build_store's two projects (three chats each), plus a child of Alpha's first chat, a batch
    run under Alpha, and a batch run the store could place under nothing."""
    from c4x import store
    path = build_store(tmp_path / "runs.db")
    con = sqlite3.connect(str(path))
    _run(con, CHILD, CHILD_CWD)
    _run(con, BATCH, BATCH_CWD)
    _run(con, LONE, LONE_CWD)
    con.executemany("INSERT INTO run_links VALUES (?,?,?,?,?,?,?,?)", [
        (CHILD, "s0-0", ALPHA, "prompt", "s0-0-tc", 4, "test", "2026-08-05T11:00:00Z"),
        (BATCH, None, ALPHA, "batch", None, 3, "test", "2026-08-05T11:00:00Z"),
        (LONE, None, None, "batch", None, 3, "test", "2026-08-05T11:00:00Z"),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


class TestTheMap:
    def test_the_three_maps_and_their_readers(self, runs_store):
        from c4x import runs, store
        parent_of, children_of, project_of = store.run_links(ttl=0)
        assert parent_of == {CHILD: "s0-0", BATCH: None, LONE: None}
        assert children_of == {"s0-0": [CHILD]}
        assert project_of == {CHILD: ALPHA, BATCH: ALPHA, LONE: None}
        assert runs.spawned_by([CHILD, "s0-0"]) == {CHILD: "s0-0"}
        assert runs.spawned_by() == parent_of
        assert runs.runs_of("s0-0") == [CHILD] and runs.runs_of(CHILD) == []
        assert runs.project_of(BATCH) == ALPHA
        assert runs.project_of(LONE) is None and runs.project_of("s0-0") is None
        assert store.runs_by_project(ttl=0) == {ALPHA: 2}


class TestTheFold:
    def test_a_child_resolves_to_the_chat_that_spawned_it_and_a_batch_to_itself(self, runs_store):
        from c4x import store
        assert store.chat_head(CHILD) == "s0-0"
        assert store.chat_head(BATCH) == BATCH
        assert store.chat_head("s0-0") == "s0-0"

    def test_members_carry_the_child_only_under_the_subagent_scope(self, runs_store):
        from c4x import store
        assert store.chat_members("s0-0") == ["s0-0"]
        assert store.chat_members("s0-0", reviews=True) == ["s0-0", CHILD]
        assert store.chat_members(CHILD) == ["s0-0"], "a child names its chat"
        cohort = store.cohort_sessions(f"project::{ALPHA}", ttl=0, reviews=True)
        assert CHILD in cohort and BATCH not in cohort and LONE not in cohort
        assert CHILD not in store.cohort_sessions(f"project::{ALPHA}", ttl=0)

    def test_no_run_is_listed_anywhere(self, runs_store):
        from c4x import store
        ids = set(store.session_rows(ttl=0)["session_id"])
        assert ids == {"s0-0", "s0-1", "s0-2", "s1-0", "s1-1", "s1-2"}
        assert store.overview_stats()["sessions"] == 6

    def test_the_work_column_counts_the_children(self, runs_store):
        from c4x import store
        counts = store.chat_work_counts("s0-0")
        assert counts["runs"] == 1 and counts["harvested"]["runs"] is True
        assert store.chat_work_counts("s1-0")["runs"] == 0
        assert store.chat_work_totals(ttl=0)["s0-0"]["runs"] == 1

    def test_the_chat_page_lists_the_child_with_how_where_and_what_it_cost(self, runs_store):
        from c4x import store
        df = store.chat_runs("s0-0")
        assert list(df["session_id"]) == [CHILD]
        row = df.iloc[0]
        assert row["how"] == "prompt" and row["hits"] == 4 and row["call_id"] == "s0-0-tc"
        assert row["leaf"] == "c1" and row["cwd"] == CHILD_CWD
        assert str(row["prompt"]).startswith("Probe run-child")
        assert int(row["output_tokens"]) == TURNS * 50 and int(row["calls"]) == TURNS
        assert store.chat_runs(BATCH).empty and store.chat_runs("s1-0").empty


class TestTheProjectSurfaces:
    def test_the_population_list_says_how_many_runs_a_project_carries(self, runs_store):
        from c4x import store
        labels = {o["value"]: o["label"] for o in store.cohort_options()}
        assert labels[f"project::{ALPHA}"].endswith("(3 listed, 2 runs)")
        assert labels[f"project::{BETA}"].endswith("(3 listed)")

    def test_the_summary_chart_folds_a_run_s_bytes_into_its_project(self, runs_store):
        from c4x.tabs.summary import project_totals_fig
        fig = project_totals_fig()
        cats = list(fig.layout.yaxis.tickvals)
        assert ALPHA in cats and CHILD_CWD not in cats and BATCH_CWD not in cats
        assert LONE_CWD in cats, "a run placed under nothing keeps its own bar, as before"
        con = sqlite3.connect(str(runs_store))
        own = dict(con.execute(
            """SELECT s.cwd, SUM(t.result_bytes) FROM tool_calls t
               JOIN sessions s ON s.session_id = t.session_id
               WHERE s.session_id NOT IN (SELECT session_id FROM run_links) GROUP BY s.cwd"""))
        con.close()
        bar = {p: sum(int(trace.x[i]) for trace in fig.data) for i, p in enumerate(cats)}
        # The project's own calls, plus the two runs folded in; Beta's bar is its own calls only.
        assert bar[ALPHA] == own[ALPHA] + 2 * RUN_BYTES
        assert bar[BETA] == own[BETA] and bar[LONE_CWD] == RUN_BYTES
        assert list(fig.data[0].customdata)[cats.index(ALPHA)] == 2
        assert list(fig.data[0].customdata)[cats.index(BETA)] == 0


class TestTheSamePromptAgain:
    def test_a_same_prompt_run_reads_as_a_run_with_no_chat_behind_it(self, runs_store):
        """`how` is the harvester's business. A row with no head folds under its project whatever
        tier wrote it, so the tier that places a plugin's SDK one-shots (begun in the chat's OWN
        folder, which no batch ever is) needed no reader to change. This pins that."""
        from c4x import runs, store
        same = "run-same-prompt"
        con = sqlite3.connect(str(runs_store))
        _run(con, same, ALPHA)
        con.execute("INSERT INTO run_links VALUES (?,?,?,?,?,?,?,?)",
                    (same, None, ALPHA, "same-prompt", None, 182, "test", "2026-08-05T11:00:00Z"))
        con.commit()
        con.close()
        forget_cached_rows()
        assert runs.spawned_by([same]) == {same: None} and runs.project_of(same) == ALPHA
        assert store.chat_head(same) == same and store.chat_runs(same).empty
        assert same not in set(store.session_rows(ttl=0)["session_id"])
        assert store.runs_by_project(ttl=0) == {ALPHA: 3}
        labels = {o["value"]: o["label"] for o in store.cohort_options()}
        assert labels[f"project::{ALPHA}"].endswith("(3 listed, 3 runs)")

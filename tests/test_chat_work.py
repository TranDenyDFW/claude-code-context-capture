r"""What a chat planned and what it ran, as the panel beside the Sessions list asks for it.

Four kinds, one chat. The cases that matter are the ones where a naive query answers confidently
and wrongly: a plan written before the last resume, a subagent run this chat holds the files for but
never called, a workflow whose agent count and whose agents on disk are different numbers, and a
task id that resolves to nothing at all.

Built on `build_store` from test_projects, so the schema is harvest's own and a column added there
appears here without this file knowing.
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

HEAD, OLD = "s0-0", "s0-1"
AGENT_FILE = r"C:\t\s0-0\subagents\agent-a1.jsonl"


def link(con, session_id, head_id):
    con.execute(
        """INSERT INTO session_links (session_id, head_id, next_id, head_kind, overlap,
             prefix_uuids, next_uuids, shared_uuids, method, linked_at)
           VALUES (?,?,?,'none',0.95,10,20,9,'test','2026-09-12T00:00:00Z')""",
        (session_id, head_id, head_id))


@pytest.fixture
def work_store(tmp_path, monkeypatch):
    """A chat of two sessions that wrote two plans and ran three agents and two workflows."""
    from c4x import store
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    link(con, OLD, HEAD)
    con.execute("""INSERT INTO plans (tool_use_id, session_id, turn_uuid, ts, plan_text,
                     plan_chars, plan_file_path, is_sidechain, file_path, line_no)
                   VALUES ('tp-1', ?, 's0-0-t0', '2026-08-02T10:00:00Z', 'the newest plan', 15,
                           'C:\\plans\\one.md', 0, 'f', 1)""", (HEAD,))
    # ON A SUPERSEDED SESSION. The chat wrote it before its last resume, and a reader scoped to the
    # newest session alone would report that this chat never planned anything.
    con.execute("""INSERT INTO plans (tool_use_id, session_id, turn_uuid, ts, plan_text,
                     plan_chars, plan_file_path, is_sidechain, file_path, line_no)
                   VALUES ('tp-0', ?, 's0-1-t0', '2026-08-01T10:00:00Z', 'an older plan', 13,
                           NULL, 0, 'f', 2)""", (OLD,))
    con.execute("""UPDATE tool_calls SET tool_name = 'ExitPlanMode', outcome = 'refused',
                     denial_kind = 'permission-rule' WHERE tool_use_id = 's0-1-tc'""")
    con.execute("UPDATE tool_calls SET tool_use_id = 'tp-0' WHERE tool_use_id = 's0-1-tc'")
    con.execute("""UPDATE tool_calls SET tool_name = 'Agent', subagent_type = 'general-purpose'
                   WHERE tool_use_id = 's0-0-tc'""")
    con.execute("UPDATE tool_calls SET tool_use_id = 'tc-agent' WHERE tool_use_id = 's0-0-tc'")
    # Three runs: one reached through the call that spawned it, one only through the directory it
    # sits in, and one belonging to a workflow.
    runs = [
        ("a1", HEAD, "tc-agent", None, "general-purpose", AGENT_FILE),
        ("a2", HEAD, None, None, "Explore", r"C:\t\s0-0\subagents\agent-a2.jsonl"),
        ("a3", HEAD, None, "wf_1", "workflow-subagent", r"C:\t\s0-0\subagents\agent-a3.jsonl"),
    ]
    for agent_id, where, call, wf, kind, transcript in runs:
        con.execute("""INSERT INTO agent_runs (agent_id, dir_session_id, tool_use_id,
                         workflow_run_id, agent_type, description, spawn_depth, meta_json,
                         transcript_path, meta_path, meta_size, meta_mtime_ms)
                       VALUES (?,?,?,?,?,'did a thing',1,'{}',?,'m',10,0)""",
                    (agent_id, where, call, wf, kind, transcript))
    # The first run's own transcript, so its records and tokens can be derived rather than stored.
    for n in range(3):
        con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                         file_path, line_no) VALUES (?,?,?,'assistant','assistant','x',1,?,?)""",
                    (f"am{n}", HEAD, f"2026-08-02T11:0{n}:00Z", AGENT_FILE, n))
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, output_tokens,
                         file_path, line_no) VALUES (?,?,?,'claude-opus-5',?,?,?,?)""",
                    (f"at{n}", HEAD, f"2026-08-02T11:0{n}:00Z", f"req-{n}", 100, AGENT_FILE, n))
    # A RETRY, which repeats its request id and its counts. Summed over rows the total would be 400.
    con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, output_tokens,
                     file_path, line_no) VALUES ('at-retry',?, '2026-08-02T11:03:00Z',
                     'claude-opus-5', 'req-2', 100, ?, 9)""", (HEAD, AGENT_FILE))
    con.execute("""INSERT INTO workflow_runs (run_id, task_id, dir_session_id, tool_use_id,
                     turn_uuid, session_id, workflow_name, status, started_at, ts, duration_ms,
                     agent_count, total_tokens, total_tool_calls, file_path, file_size,
                     file_mtime_ms)
                   VALUES ('wf_1','wtask1',?, 'tc-wf', 's0-0-t1', ?, 'review-changes','completed',
                           '2026-08-02T12:00:00Z','2026-08-02T12:00:00Z', 90000, 4, 5000, 20,
                           'f', 10, 0)""", (HEAD, HEAD))
    con.execute("""INSERT INTO task_events (uuid, session_id, ts, parent_uuid, task_id, task_type,
                     status, description, file_path, line_no)
                   VALUES ('te-1',?, '2026-08-02T13:00:00Z','s0-0-t0','a1','local_agent',
                           'completed','the run this chat can resolve','f',1)""", (HEAD,))
    con.execute("""INSERT INTO task_events (uuid, session_id, ts, parent_uuid, task_id, task_type,
                     status, description, file_path, line_no)
                   VALUES ('te-2',?, '2026-08-02T13:01:00Z','s0-0-t0','wtask1','local_agent',
                           'running','a workflow by its task id','f',2)""", (HEAD,))
    con.execute("""INSERT INTO task_events (uuid, session_id, ts, parent_uuid, task_id, task_type,
                     status, description, file_path, line_no)
                   VALUES ('te-3',?, '2026-08-02T13:02:00Z','s0-0-t0','a9999','local_agent',
                           'running','a task whose run is nowhere','f',3)""", (HEAD,))
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


class TestThePlans:
    def test_a_plan_from_before_the_last_resume_belongs_to_the_chat(self, work_store, store):
        rows = store.chat_plans(HEAD)
        assert list(rows["tool_use_id"]) == ["tp-1", "tp-0"], "newest first, both sessions"

    def test_the_verdict_is_joined_from_the_call_not_stored_on_the_plan(self, work_store, store):
        rows = store.chat_plans(HEAD).set_index("tool_use_id")
        assert rows.loc["tp-0"]["outcome"] == "refused"
        assert rows.loc["tp-0"]["denial_kind"] == "permission-rule"

    def test_the_preview_is_one_line_and_the_whole_text_is_elsewhere(self, work_store, store):
        rows = store.chat_plans(HEAD).set_index("tool_use_id")
        assert rows.loc["tp-1"]["preview"] == "the newest plan"
        whole = store.plan_text("tp-1")
        assert whole.iloc[0]["plan_text"] == "the newest plan"
        assert int(whole.iloc[0]["plan_chars"]) == 15

    def test_an_unknown_plan_is_an_empty_frame_rather_than_a_raise(self, work_store, store):
        assert store.plan_text("nope").empty


class TestTheAgentRuns:
    def test_a_run_is_reachable_through_the_call_and_through_the_directory(self, work_store, store):
        rows = store.chat_agent_runs(HEAD).set_index("agent_id")
        assert set(rows.index) == {"a1", "a2", "a3"}
        assert rows.loc["a1"]["called_from"] == HEAD, "this one names the call that spawned it"
        assert pd.isna(rows.loc["a2"]["called_from"]), "and this one is only in the directory"

    def test_its_size_is_derived_from_its_own_transcript(self, work_store, store):
        row = store.chat_agent_runs(HEAD).set_index("agent_id").loc["a1"]
        assert int(row["records"]) == 3
        assert row["first_ts"] == "2026-08-02T11:00:00Z"

    def test_a_retried_request_is_counted_once(self, work_store, store):
        """Summed over turn rows the answer is 400, and three of those tokens were never spent."""
        row = store.chat_agent_runs(HEAD).set_index("agent_id").loc["a1"]
        assert int(row["output_tokens"]) == 300

    def test_a_run_of_another_chat_is_not_listed(self, work_store, store):
        assert store.chat_agent_runs("s1-0").empty


class TestTheWorkflowRuns:
    def test_what_it_reported_and_what_this_store_holds_are_two_numbers(self, work_store, store):
        row = store.chat_workflow_runs(HEAD).iloc[0]
        assert int(row["agent_count"]) == 4, "the run said four"
        assert int(row["agents_on_disk"]) == 1, "and one of their transcripts is here"

    def test_it_names_the_call_that_launched_it(self, work_store, store):
        row = store.chat_workflow_runs(HEAD).iloc[0]
        assert row["tool_use_id"] == "tc-wf" and row["turn_uuid"] == "s0-0-t1"


class TestTheTaskNotifications:
    def test_each_one_says_what_it_resolves_to(self, work_store, store):
        rows = store.chat_task_events(HEAD).set_index("uuid")
        assert rows.loc["te-1"]["resolved_to"] == "agent"
        assert rows.loc["te-2"]["resolved_to"] == "workflow"
        assert pd.isna(rows.loc["te-3"]["resolved_to"]), (
            "a task with no run is a state, not a row to hide")

    def test_a_resolved_run_says_whose_directory_it_ran_under(self, work_store, store):
        rows = store.chat_task_events(HEAD).set_index("uuid")
        assert rows.loc["te-1"]["ran_under"] == HEAD


class TestTheCounts:
    def test_they_cover_the_whole_chat(self, work_store, store):
        counts = store.chat_work_counts(HEAD)
        assert counts["plans"] == 2, "including the one written before the last resume"
        assert counts["agent_runs"] == 3
        assert counts["workflow_runs"] == 1
        assert counts["task_events"] == 3
        assert all(counts["harvested"].values())

    def test_a_session_this_store_does_not_have_is_not_a_chat(self, work_store, store):
        assert store.chat_exists(HEAD) and not store.chat_exists("never-seen")


class TestAStoreHarvestedBeforeThisExisted:
    def test_every_reader_answers_empty_rather_than_raising(self, work_store, store):
        """The Python package never creates a table, so an older store simply has none of these."""
        con = sqlite3.connect(str(work_store))
        for table in ("plans", "agent_runs", "workflow_runs", "task_events"):
            con.execute(f"DROP TABLE {table}")
        con.commit()
        con.close()
        forget_cached_rows()
        assert store.chat_plans(HEAD).empty
        assert store.chat_agent_runs(HEAD).empty
        assert store.chat_workflow_runs(HEAD).empty
        assert store.chat_task_events(HEAD).empty
        counts = store.chat_work_counts(HEAD)
        assert counts["plans"] == 0 and not any(counts["harvested"].values())
        assert store.chat_exists(HEAD), "the chat is still a chat"


class TestTheCensusThatMeansTranscripts:
    """`files` holds two kinds since the sidecar pass, and one card counts only one of them."""

    def test_the_transcripts_count_leaves_the_sidecars_out(self, work_store, store):
        con = sqlite3.connect(str(work_store))
        before = con.execute("SELECT COUNT(*) n, SUM(bytes_read) b FROM files").fetchone()
        con.execute("""INSERT INTO files (path, size, mtime_ms, bytes_read, lines_read, rewrites,
                                          last_harvest_ts, kind)
                       VALUES (?, 120, 0, 120, 1, 0, 't', 'sidecar')""",
                    (AGENT_FILE.replace(".jsonl", ".meta.json"),))
        con.commit()
        con.close()
        forget_cached_rows()
        stats = store.overview_stats()
        assert stats["files"] == before[0], (
            "the Summary card headed 'transcripts' is counting the JSON files beside them")
        assert stats["bytes"] == before[1], "and its GB caption is counting their bytes"

    def test_a_store_without_the_column_still_answers(self, work_store, store, monkeypatch):
        """`harvest.mjs` adds `kind` and this package cannot, so a store awaiting it must work."""
        monkeypatch.setattr(store, "column_present", lambda table, column: False)
        forget_cached_rows()
        assert store.overview_stats()["files"] >= 0

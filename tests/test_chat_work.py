r"""What a chat planned and what it ran, as the panel beside the Sessions list asks for it.

Four kinds, one chat. The cases that matter are the ones where a naive query answers confidently
and wrongly: a plan written before the last resume, a subagent run this chat holds the files for but
never called, a workflow whose agent count and whose agents on disk are different numbers, and a
task id that resolves to nothing at all.

Built on `build_store` from test_projects, so the schema is harvest's own and a column added there
appears here without this file knowing.
"""
import json
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

# LONGER THAN THE PREVIEW, AND ON MORE THAN ONE LINE, because both of those are rules and a short
# single-line plan gates neither. A reviewer made `/api/plan` truncate its answer to 400 characters
# and the test named for that rule still passed, since the fixture plan was 15 characters and a cut
# of it is the same string. Measured on the author's store: all 280 plans exceed 400 characters,
# the shortest is 1,493 and the longest 80,428, so a short one is not the shape being modelled.
NEWEST = "the newest plan\nand a second line so the flattening is visible " + "x" * 600


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
                   VALUES ('tp-1', ?, 's0-0-t0', '2026-08-02T10:00:00Z', ?, ?,
                           'C:\\plans\\one.md', 0, 'f', 1)""", (HEAD, NEWEST, len(NEWEST)))
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
    # A CALL IN THIS CHAT, A DIRECTORY IN ANOTHER. This is the ONLY row reachable through the
    # calling session alone, and without it the OR in `chat_agent_runs` is ungated: an independent
    # reviewer deleted the caller-side clause and the whole file stayed green, because every other
    # run here sits under this chat's own directory.
    #
    # NOT YET SEEN ON THIS MACHINE, and the honest version of that sentence matters. Of 7,433 agent
    # runs, 761 join a tool call that carries a session id and NONE of those differ from
    # `dir_session_id`; the same holds for all 208 workflow runs. The schema keeps both columns
    # because they answer different questions, and the reader is written for the day they differ:
    # history is bridged between sessions, so a run a chat asked for can end up stored under
    # another chat's directory. This row is that day, written down.
    con.execute("""INSERT INTO tool_calls (tool_use_id, session_id, turn_uuid, ts, tool_name,
                     subagent_type, file_path, line_no)
                   VALUES ('tc-elsewhere', ?, 's0-0-t0', '2026-08-02T11:30:00Z', 'Agent',
                           'general-purpose', 'f', 20)""", (HEAD,))
    # Four runs: one reached through the call that spawned it, one only through the directory it
    # sits in, one belonging to a workflow, and one reachable ONLY through the call.
    runs = [
        ("a1", HEAD, "tc-agent", None, "general-purpose", AGENT_FILE),
        ("a2", HEAD, None, None, "Explore", r"C:\t\s0-0\subagents\agent-a2.jsonl"),
        ("a3", HEAD, None, "wf_1", "workflow-subagent", r"C:\t\s0-0\subagents\agent-a3.jsonl"),
        ("a4", "s1-0", "tc-elsewhere", None, "Explore",
         r"C:\t\s1-0\subagents\agent-a4.jsonl"),
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
    # THE FILE CHANGES, in the shapes a reader must tell apart: an edit whose result carried a
    # patch, a created file, a subagent edit whose result was never recorded, and a refused edit on
    # the superseded session. The hunk removes two lines and adds three while the old text is one
    # line, so counting texts instead of prefixes is visible. See TestTheChanges.
    put_change = """INSERT INTO changes (tool_use_id, session_id, turn_uuid, ts, tool_name, file,
                      kind, old_text, new_text, replace_all, old_lines, new_lines, patch_json,
                      additions, deletions, original_chars, user_modified, is_sidechain, file_path,
                      line_no) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,0,?,'f',?)"""
    hunk = json.dumps([{"oldStart": 4, "oldLines": 3, "newStart": 4, "newLines": 4,
                        "lines": [" keep", "-gone", "-also gone", "+one", "+two", "+three"]}])
    three = "one" + chr(10) + "two" + chr(10) + "three"
    con.execute(put_change, ("ch-1", HEAD, "s0-0-t0", "2026-08-02T10:10:00Z", "Edit",
                             "C:/p/app.py", "edit", "gone", three, 1, 3, hunk, 3, 2, 120, 0, 12))
    con.execute(put_change, ("ch-2", HEAD, "s0-0-t0", "2026-08-02T10:20:00Z", "Write",
                             "C:/p/new.txt", "create", None, "a" + chr(10) + "b", None, 2, "[]",
                             0, 0, None, 0, 13))
    con.execute(put_change, ("ch-3", HEAD, "s0-0-t0", "2026-08-02T10:30:00Z", "Edit",
                             "C:/p/app.py", None, "two", "deux", 1, 1, None, None, None, None,
                             1, 14))
    con.execute(put_change, ("ch-4", OLD, "s0-1-t0", "2026-08-01T09:00:00Z", "Edit",
                             "C:/p/app.py", None, "x", "y", 1, 1, None, None, None, None, 0, 15))
    put_call = """INSERT INTO tool_calls (tool_use_id, session_id, turn_uuid, ts, tool_name,
                    file_path, line_no, outcome, denial_kind) VALUES (?,?,?,?,?,'f',?,?,?)"""
    con.execute(put_call, ("ch-1", HEAD, "s0-0-t0", "2026-08-02T10:10:00Z", "Edit", 12, "ok", None))
    con.execute(put_call, ("ch-2", HEAD, "s0-0-t0", "2026-08-02T10:20:00Z", "Write", 13, "ok",
                           None))
    con.execute(put_call, ("ch-3", HEAD, "s0-0-t0", "2026-08-02T10:30:00Z", "Edit", 14, "ok", None))
    con.execute(put_call, ("ch-4", OLD, "s0-1-t0", "2026-08-01T09:00:00Z", "Edit", 15, "refused",
                           "permission-rule"))
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
        """Two rules, and the plan is long enough for either to break.

        The list carries 400 characters with the newlines flattened, so it fits a table cell; the
        whole document is a second fetch. A short plan gates neither, because a cut of it and a
        flattening of it are both the original string.
        """
        preview = store.chat_plans(HEAD).set_index("tool_use_id").loc["tp-1"]["preview"]
        assert len(preview) == 400, "the list carries 400 characters, not the document"
        assert preview == NEWEST[:400].replace(chr(10), " "), "flattened for a table cell"
        assert chr(10) not in preview
        whole = store.plan_text("tp-1")
        assert whole.iloc[0]["plan_text"] == NEWEST
        assert int(whole.iloc[0]["plan_chars"]) == len(NEWEST) > 400

    def test_an_unknown_plan_is_an_empty_frame_rather_than_a_raise(self, work_store, store):
        assert store.plan_text("nope").empty


class TestTheAgentRuns:
    def test_a_run_is_reachable_through_the_call_and_through_the_directory(self, work_store, store):
        rows = store.chat_agent_runs(HEAD).set_index("agent_id")
        assert set(rows.index) == {"a1", "a2", "a3", "a4"}
        assert rows.loc["a1"]["called_from"] == HEAD, "this one names the call that spawned it"
        assert pd.isna(rows.loc["a2"]["called_from"]), "and this one is only in the directory"

    def test_a_run_this_chat_called_but_does_not_hold_is_still_its_run(self, work_store, store):
        """The gate on the OR. Without the caller-side reach this row disappears, and the header
        count above it does not, so the panel shows a number over a list that lacks the row."""
        rows = store.chat_agent_runs(HEAD).set_index("agent_id")
        assert "a4" in rows.index
        assert rows.loc["a4"]["dir_session_id"] == "s1-0", "stored under another chat"
        assert rows.loc["a4"]["called_from"] == HEAD, "and reached only through the call"

    def test_its_size_is_derived_from_its_own_transcript(self, work_store, store):
        row = store.chat_agent_runs(HEAD).set_index("agent_id").loc["a1"]
        assert int(row["records"]) == 3
        assert row["first_ts"] == "2026-08-02T11:00:00Z"

    def test_a_retried_request_is_counted_once(self, work_store, store):
        """Summed over turn rows the answer is 400, and three of those tokens were never spent."""
        row = store.chat_agent_runs(HEAD).set_index("agent_id").loc["a1"]
        assert int(row["output_tokens"]) == 300

    def test_a_run_of_another_chat_is_not_listed(self, work_store, store):
        """`s1-1` neither called a run nor holds one. `s1-0` is not the example any more: it holds
        the directory of the run this chat called, so it can see that one, which is the point."""
        assert store.chat_agent_runs("s1-1").empty
        assert set(store.chat_agent_runs("s1-0")["agent_id"]) == {"a4"}


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
        assert counts["agent_runs"] == 4, "including the one stored under another chat"
        assert counts["workflow_runs"] == 1
        assert counts["task_events"] == 3
        assert all(counts["harvested"].values())

    def test_a_session_this_store_does_not_have_is_not_a_chat(self, work_store, store):
        assert store.chat_exists(HEAD) and not store.chat_exists("never-seen")


class TestAStoreHarvestedBeforeThisExisted:
    def test_every_reader_answers_empty_rather_than_raising(self, work_store, store):
        """The Python package never creates a table, so an older store simply has none of these."""
        con = sqlite3.connect(str(work_store))
        for table in ("plans", "agent_runs", "workflow_runs", "task_events", "changes"):
            con.execute(f"DROP TABLE {table}")
        con.commit()
        con.close()
        forget_cached_rows()
        assert store.chat_plans(HEAD).empty
        assert store.chat_agent_runs(HEAD).empty
        assert store.chat_workflow_runs(HEAD).empty
        assert store.chat_task_events(HEAD).empty
        assert store.chat_changed_files(HEAD).empty
        assert store.chat_changes(HEAD).empty
        assert store.change_detail("ch-1").empty
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

    def test_a_store_without_the_column_still_answers(self, work_store, store):
        """`harvest.mjs` adds `kind` and this package cannot, so a store awaiting it must work.

        THE COLUMN IS REALLY DROPPED, not monkeypatched away. This stubbed `column_present` to
        False while the column was still there, so both branches built SQL that ran, and removing
        the guard entirely left the test green. A reviewer proved it. What the guard prevents is
        a query naming a column the store does not have, and only a store without it can show that.
        """
        # REBUILT WITHOUT IT, because ALTER TABLE DROP COLUMN cannot be used here: SQLite reparses
        # the stored CREATE TABLE to rewrite it, and harvest writes that statement with SQL
        # comments in it, so the drop fails with "error in table files after drop column:
        # incomplete input". The table is recreated from its live column list instead.
        con = sqlite3.connect(str(work_store))
        cols = [r[1] for r in con.execute("PRAGMA table_info(files)") if r[1] != "kind"]
        listed = ",".join(f'"{c}"' for c in cols)
        con.execute(f"CREATE TABLE files_old AS SELECT {listed} FROM files")
        con.execute("DROP TABLE files")
        con.execute("ALTER TABLE files_old RENAME TO files")
        con.commit()
        con.close()
        forget_cached_rows()
        assert not store.column_present("files", "kind"), "gone, as it is on an older store"
        stats = store.overview_stats()
        assert stats["files"] >= 0, "and the census answers rather than raising"


class TestTheRoutes:
    """The two routes the panel fetches. Neither had a test until a reviewer said so."""

    @pytest.fixture
    def api_client(self, work_store, store):
        from fastapi.testclient import TestClient

        from c4x.api.main import api
        return TestClient(api, base_url="http://127.0.0.1:8059")

    def test_the_chat_route_answers_all_four_kinds_with_their_totals(self, api_client):
        body = api_client.get(f"/api/chat/{HEAD}").json()
        assert body["session"] == HEAD
        assert sorted(body["chat"]) == [HEAD, OLD], "the whole chat, not the newest session"
        assert body["plans_total"] == 2
        assert body["agent_runs_total"] == 4
        assert body["workflow_runs_total"] == 1
        assert body["task_events_total"] == 3
        assert body["task_events_unresolved"] == 1
        assert all(body["harvested"].values())
        assert {r["agent_id"] for r in body["agent_runs"]} == {"a1", "a2", "a3", "a4"}

    def test_a_session_this_store_never_saw_is_a_404_that_names_it(self, api_client):
        answer = api_client.get("/api/chat/never-seen")
        assert answer.status_code == 404
        assert "never-seen" in str(answer.json()["detail"])

    def test_the_plan_route_answers_the_whole_text_not_a_preview(self, api_client):
        body = api_client.get("/api/plan/tp-1").json()
        assert body["text"] == NEWEST
        assert len(body["text"]) > 400, "a route that truncated would pass on a short plan"
        assert body["chars"] == len(NEWEST)
        # A PATH IS NOT A FILE. The row names one that was never created here.
        assert body["plan_file_path"].endswith("one.md")
        assert body["file_exists"] is False

    def test_the_route_carries_the_verdict_from_the_call(self, api_client):
        """ASSERTED ON THE PLAN THAT HAS A CALL. This asked `tp-1`, which has none, under
        `outcome is None or isinstance(outcome, str)`, which is true of every value the field can
        hold: a route returning a literal None for both fields passed. A reviewer proved it."""
        refused = api_client.get("/api/plan/tp-0").json()
        assert refused["outcome"] == "refused"
        assert refused["denial_kind"] == "permission-rule"
        assert api_client.get("/api/plan/tp-1").json()["outcome"] is None, (
            "and a plan with no call has no verdict, which is a state and not a default")

    def test_it_says_whether_the_file_the_plan_names_is_still_there(self, api_client, tmp_path):
        """BOTH ANSWERS. Every fixture plan named a missing file, so a route hard-coding
        `file_exists = False` passed: it would have said "no longer on disk" about every plan
        ever written. Measured on the author's store: 291 plans against 27 surviving files, so
        the false case is the common one and the true case is the one worth proving."""
        assert api_client.get("/api/plan/tp-1").json()["file_exists"] is False
        real = tmp_path / "kept.md"
        real.write_text("a plan file that still exists", encoding="utf-8")
        from c4x import store
        con = sqlite3.connect(str(store.DB_PATH))
        con.execute("UPDATE plans SET plan_file_path = ? WHERE tool_use_id = 'tp-0'", (str(real),))
        con.commit()
        con.close()
        forget_cached_rows()
        assert api_client.get("/api/plan/tp-0").json()["file_exists"] is True

    def test_an_unknown_plan_is_a_404(self, api_client):
        assert api_client.get("/api/plan/nope").status_code == 404

    def test_the_chat_route_answers_the_files_changed_with_two_totals(self, api_client):
        body = api_client.get(f"/api/chat/{HEAD}").json()
        assert body["changes_total"] == 4 and body["changed_files_total"] == 2
        assert {f["file"] for f in body["changed_files"]} == {"C:/p/app.py", "C:/p/new.txt"}
        assert body["harvested"]["changes"] is True

    def test_the_edits_route_filters_by_file_and_404s_on_an_unknown_chat(self, api_client):
        rows = api_client.get(f"/api/chat/{HEAD}/changes",
                              params={"file": "C:/p/app.py"}).json()["changes"]
        assert [r["tool_use_id"] for r in rows] == ["ch-3", "ch-1", "ch-4"], "newest first"
        assert api_client.get("/api/chat/never-seen/changes").status_code == 404

    def test_the_change_route_answers_the_hunks_whole_or_says_there_are_none(self, api_client):
        one = api_client.get("/api/change/ch-1").json()
        assert one["hunks"][0]["lines"] == [" keep", "-gone", "-also gone", "+one", "+two",
                                            "+three"]
        assert one["additions"] == 3 and one["deletions"] == 2 and one["outcome"] == "ok"
        # THREE STATES, NOT TWO. None is an edit whose result was never recorded; an empty list is
        # a created file whose whole content is the new text.
        agent = api_client.get("/api/change/ch-3").json()
        assert agent["hunks"] is None and agent["old_text"] == "two"
        assert agent["is_sidechain"] is True and agent["additions"] is None
        created = api_client.get("/api/change/ch-2").json()
        assert created["hunks"] == [] and created["kind"] == "create"
        assert api_client.get("/api/change/nope").status_code == 404


class TestTheChanges:
    """The file changes a chat made, grouped by file, the counts covering only what was recorded."""

    def test_the_files_are_grouped_and_the_counts_cover_only_what_was_recorded(self, work_store,
                                                                               store):
        files = store.chat_changed_files(HEAD).set_index("file")
        assert set(files.index) == {"C:/p/app.py", "C:/p/new.txt"}
        app = files.loc["C:/p/app.py"]
        assert int(app["edits"]) == 3, "the patched edit, the subagent edit and the refused one"
        assert int(app["ok_edits"]) == 2
        assert int(app["additions"]) == 3 and int(app["deletions"]) == 2, (
            "from the one edit that carried a patch, and from its hunk prefixes, not its texts")
        assert int(app["patched"]) == 1 and int(app["by_subagents"]) == 1
        new = files.loc["C:/p/new.txt"]
        assert int(new["additions"]) == 0 and int(new["deletions"]) == 0
        assert int(new["patched"]) == 1, "a created file has an empty patch, which is still a patch"

    def test_the_edits_behind_one_file_say_whether_they_carry_a_patch(self, work_store, store):
        rows = store.chat_changes(HEAD, file="C:/p/app.py")
        assert list(rows["tool_use_id"]) == ["ch-3", "ch-1", "ch-4"], "newest first, whole chat"
        by = rows.set_index("tool_use_id")
        assert int(by.loc["ch-1"]["has_patch"]) == 1 and int(by.loc["ch-3"]["has_patch"]) == 0
        assert by.loc["ch-4"]["outcome"] == "refused"
        assert pd.isna(by.loc["ch-3"]["additions"]), "unknown is unknown, not zero"
        assert len(store.chat_changes(HEAD)) == 4, "and without a file, every edit"

    def test_a_change_whole_carries_the_hunks_or_says_there_are_none(self, work_store, store):
        whole = store.change_detail("ch-1").iloc[0]
        assert json.loads(whole["patch_json"])[0]["lines"][1] == "-gone"
        assert whole["new_text"].count(chr(10)) == 2, "the text is the whole text"
        assert store.change_detail("ch-3").iloc[0]["patch_json"] is None
        assert store.change_detail("nope").empty

    def test_the_counts_cover_the_whole_chat_and_reach_the_column(self, work_store, store):
        counts = store.chat_work_counts(HEAD)
        assert counts["changes"] == 4 and counts["changed_files"] == 2
        assert store.chat_work_totals()[HEAD]["changes"] == 4
        from c4x.tabs.sessions import work_summary
        assert work_summary({"changes": 4}) == "4 changes"
        assert work_summary({"changes": 1}) == "1 change"

    def test_the_table_is_classified_for_delete_and_export(self, work_store, store):
        """`unhandled_tables` caught four unclassified tables last time, against the LIVE store.
        The live store may not hold this table yet; this one does, so the check is real here."""
        from c4x import projects
        con = sqlite3.connect(str(work_store))
        try:
            assert "changes" not in projects.unhandled_tables(con)
        finally:
            con.close()
        assert "changes" in projects.BY_SESSION
        assert projects.where_for("changes", ["s0-0"])[0] == "WHERE session_id IN (?)"


class TestTheColumnForTheSessionsList:
    """`chat_work_totals` answers for every chat at once, and its keys must all be chats."""

    def test_a_run_with_no_calling_session_adds_no_key_of_its_own(self, work_store, store):
        """pandas reads a SQL NULL as nan, and bool(nan) is True.

        Found by an independent reviewer: the caller column is NULL for every run with no matching
        tool call, and a falsiness guard let those through, so the dict grew a nan key holding
        6,671 agent runs on the author's store. Every one was also counted under its real owner, so
        no number was wrong; the key was simply not a session.
        """
        totals = store.chat_work_totals()
        assert all(isinstance(key, str) and key for key in totals), (
            f"these keys are not session ids: {[k for k in totals if not isinstance(k, str)]}")

    def test_it_counts_each_chat_the_way_the_panel_does(self, work_store, store):
        totals = store.chat_work_totals()
        counts = store.chat_work_counts(HEAD)
        assert totals[HEAD]["agent_runs"] == counts["agent_runs"] == 4
        assert totals[HEAD]["plans"] == counts["plans"] == 2
        # THE OTHER CHAT SEES THE RUN IT HOLDS, which is the same statement the panel makes.
        assert totals["s1-0"]["agent_runs"] == 1

    def test_the_column_says_which_kinds_rather_than_four_numbers(self, work_store, store):
        from c4x.tabs.sessions import work_summary
        assert work_summary({"plans": 2, "agent_runs": 1}) == "2 plans, 1 agent"
        assert work_summary({"plans": 1}) == "1 plan"
        assert work_summary({"plans": 0}) == "", "a zero is nothing to say, not a zero to print"
        assert work_summary(None) == ""

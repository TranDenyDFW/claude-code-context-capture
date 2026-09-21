"""The Update data button's server half: `c4x/harvest.py` and `/api/store/harvest`.

NOTHING HERE EVER RUNS THE HARVESTER. Its transcripts root is this machine's own and cannot be
redirected (`tools/harvest.mjs`, `PROJECTS`), so every job in this file is driven by a fake runner
that returns what the real one would have printed, and `tests/conftest.py` makes the default
runner raise for the test that forgets. What keeps those fakes honest is the last class: the keys
this module reads are held to the list `tools/harvest.mjs` exports, and that file's own self-test
holds the real report to the same list.

No threads and no sleeps: `spawn=lambda work: work()` finishes a job before `start` returns, and
`spawn=held.append` holds one so "running" can be looked at.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from c4x import harvest, proc, store
from c4x.api import cache
from c4x.api.main import api
from tests.test_projects import build_store, forget_cached_rows

ROOT = Path(__file__).resolve().parents[1]
EVIL = "http://evil.example"


def report(**over):
    """What `tools/harvest.mjs` prints for an incremental run, trimmed to what matters here."""
    base = {
        "mode": "incremental",
        "chains": {"directories": 1, "links": 0, "reviews": 0, "runs": 0, "failed": []},
        "sidecars": {"directories": 1, "seen": 0, "read": 0, "failed": []},
        "desktop_records": {"seen": 4, "deleted": 0, "gone": 0, "returned": 0, "failed": None},
        "files_seen": 9599, "files_read": 3, "rewritten_files": 0, "excluded_files": 0,
        "lines": 412, "mb": 0.4, "turn_records_seen": 57, "compaction_records_seen": 0,
        "unknown_record_types": [], "seconds": 1.2,
    }
    return {**base, **over}


def ran(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def runner(*answers, calls=None):
    """A fake `proc.run` that answers in order and records how it was called."""
    left = list(answers)

    def run(args, **kw):
        if calls is not None:
            calls.append((list(args), kw))
        answer = left.pop(0) if len(left) > 1 else left[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return run


def once(answer, **kw):
    return harvest.run_once("incremental", run=runner(answer), stamp=lambda: False,
                            sleep=lambda s: None, root=kw.pop("root", "X:/install"), **kw)


class TestWhetherItWill:
    def test_the_installs_own_store_is_enabled_even_before_it_exists(self, tmp_path):
        found = harvest.capability(db=tmp_path / "data" / "context.db", root=tmp_path, env={},
                                   redacted=lambda: False)
        assert found["enabled"] is True and found["reason"] is None

    def test_a_served_copy_is_refused_and_both_paths_are_named(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "context.db").write_bytes(b"real")
        (tmp_path / "copy.db").write_bytes(b"real")
        found = harvest.capability(db=tmp_path / "copy.db", root=tmp_path, env={},
                                   redacted=lambda: False)
        assert found["enabled"] is False and found["reason"] == "not-own-store"
        assert "copy.db" in found["why_not"] and "context.db" in found["why_not"]
        assert found["fix"].startswith("Start the server without --db")

    def test_case_and_slashes_do_not_make_two_stores(self, tmp_path):
        (tmp_path / "data").mkdir()
        real = tmp_path / "data" / "context.db"
        real.write_bytes(b"real")
        spelled = str(real).replace("\\", "/")
        assert harvest.same_file(spelled, real)
        if re.match(r"^[A-Za-z]:", spelled):
            assert harvest.same_file(spelled.upper(), real)

    def test_a_redacted_copy_at_the_own_path_is_refused(self, tmp_path):
        found = harvest.capability(db=tmp_path / "data" / "context.db", root=tmp_path, env={},
                                   redacted=lambda: True)
        assert found["enabled"] is False and found["reason"] == "redacted-copy"
        assert "real transcripts" in found["why_not"]

    def test_the_mark_is_read_from_the_file_itself(self, tmp_path):
        """No injected callable here: the production wiring. A store at the install's own path that
        carries the table redact.py stamps is a copy, and is refused."""
        import sqlite3
        (tmp_path / "data").mkdir()
        own = tmp_path / "data" / "context.db"
        con = sqlite3.connect(own)
        con.execute(f"CREATE TABLE {store.REDACTION_MARK} (at TEXT)")
        con.commit()
        con.close()
        found = harvest.capability(db=own, root=tmp_path, env={})
        assert found["enabled"] is False and found["reason"] == "redacted-copy"

    def test_a_copy_swapped_in_under_a_running_server_is_seen_at_once(self, tmp_path):
        """The gate is asked at startup and on every status read. A memo would go on saying "the
        real store" about a copy swapped into place afterwards, and the click would write real
        transcripts into it; this reads the file each time."""
        import sqlite3
        (tmp_path / "data").mkdir()
        own = tmp_path / "data" / "context.db"
        sqlite3.connect(own).close()
        assert harvest.capability(db=own, root=tmp_path, env={})["enabled"] is True
        con = sqlite3.connect(own)
        con.execute(f"CREATE TABLE {store.REDACTION_MARK} (at TEXT)")
        con.commit()
        con.close()
        assert harvest.capability(db=own, root=tmp_path, env={})["reason"] == "redacted-copy"

    def test_a_file_that_is_not_a_database_is_unreadable_not_fine(self, tmp_path):
        """FAILS CLOSED. The helper the title overlay uses reads this as "not a copy"."""
        (tmp_path / "data").mkdir()
        own = tmp_path / "data" / "context.db"
        own.write_bytes(b"this is not a sqlite file, it is a half finished copy" * 40)
        found = harvest.capability(db=own, root=tmp_path, env={})
        assert found["enabled"] is False and found["reason"] == "unreadable-store"
        assert harvest.carries_mark(tmp_path / "data" / "absent.db") is False

    def test_no_writes_disables_it(self, tmp_path):
        found = harvest.capability(db=tmp_path / "data" / "context.db", root=tmp_path,
                                   env={"C4X_NO_WRITES": "1"}, redacted=lambda: False)
        assert found["enabled"] is False and found["reason"] == "no-writes"

    def test_a_store_that_cannot_be_asked_is_not_offered(self, tmp_path):
        def broken():
            raise OSError("locked by another process")
        found = harvest.capability(db=tmp_path / "data" / "context.db", root=tmp_path, env={},
                                   redacted=broken)
        assert found["enabled"] is False and found["reason"] == "unreadable-store"

    def test_a_comparison_the_system_refuses_is_not_the_same_file(self, tmp_path, monkeypatch):
        """Fails CLOSED. Two paths that both exist and cannot be compared are different stores as
        far as an update is concerned, because the other answer is the one that writes."""
        one = tmp_path / "a.db"
        one.write_bytes(b"x")

        def refuse(a, b):
            raise OSError("access is denied")
        monkeypatch.setattr(harvest.os.path, "samefile", refuse)
        assert harvest.same_file(one, one) is False


class TestTheCommandLine:
    def test_the_child_never_sees_the_served_path(self):
        calls = []
        harvest.run_once("incremental", run=runner(ran(json.dumps(report())), calls=calls),
                         stamp=lambda: False, root="X:/install",
                         env={"PATH": "p", "C4X_DB": "X:/served/copy.db", "c4x_db": "lower",
                              "C4X_NO_TEXT": "1"})
        argv, kw = calls[0]
        assert "--db" not in argv and not any("copy.db" in part for part in argv)
        assert "C4X_DB" not in kw["env"] and "c4x_db" not in kw["env"]
        assert kw["env"]["C4X_NO_TEXT"] == "1" and kw["env"]["PATH"] == "p"

    def test_only_known_flags_exist_and_never_full(self):
        every = [flag for flags in harvest.FLAGS.values() for flag in flags]
        assert not {"--full", "--yes", "--db"} & set(every)
        assert all(kind in harvest.KINDS for kind, _dry in harvest.FLAGS)
        argv = harvest.argv_for("incremental", node="node", root="X:/install")
        assert argv[0] == "node" and argv[1].replace("\\", "/").endswith("tools/harvest.mjs")
        assert argv[2:] == []

    def test_a_job_that_does_not_exist_has_no_command_line(self):
        with pytest.raises(ValueError):
            harvest.argv_for("full")
        with pytest.raises(ValueError):
            harvest.argv_for("incremental", True)

    def test_the_report_is_decoded_as_utf8_and_the_run_has_a_ceiling(self):
        calls = []
        harvest.run_once("incremental", run=runner(ran(json.dumps(report())), calls=calls),
                         stamp=lambda: False, root="X:/install")
        kw = calls[0][1]
        assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"
        assert kw["timeout"] == harvest.TIMEOUT_S["incremental"] >= 3600
        assert kw["cwd"].replace("\\", "/") == "X:/install"


class TestTheRequest:
    def test_an_empty_body_is_the_everyday_update(self):
        assert harvest.parse_request({}) == ("incremental", False)

    @pytest.mark.parametrize("body", [{"kind": "full"}, {"kind": 7}, {"kind": "--full"}, [1]])
    def test_an_unknown_kind_is_a_value_error(self, body):
        with pytest.raises(ValueError):
            harvest.parse_request(body)

    def test_a_dry_run_of_a_kind_that_has_none_is_refused(self):
        with pytest.raises(ValueError, match="no dry run"):
            harvest.parse_request({"kind": "incremental", "dry_run": True})

    @pytest.mark.parametrize("given", ["yes", "true", 1, 0, None, [True]])
    def test_dry_run_is_a_boolean_or_the_request_is_refused(self, given):
        """A caller who sends "yes" MEANT a dry run. Reading it as false would run the job that
        writes, which is the one way to get this wrong that costs something."""
        with pytest.raises(ValueError, match="true or false"):
            harvest.parse_request({"kind": "incremental", "dry_run": given})
        assert harvest.parse_request({"kind": "incremental", "dry_run": False}) \
            == ("incremental", False)


class TestWhatItSays:
    def test_nothing_new_says_so(self):
        got = once(ran(json.dumps(report(files_read=0))))
        assert got["ok"] and not got["partial"] and got["short"] == "Nothing new."
        assert got["sentence"].startswith("Nothing new: all 9,599 transcripts")

    def test_a_clean_read_names_what_was_read(self):
        got = once(ran(json.dumps(report())))
        assert got["ok"] and not got["partial"] and got["error"] is None
        assert got["short"] == "Updated: 3 transcripts read."
        assert "Read 3 of 9,599 transcripts" in got["sentence"]
        assert got["summary"]["files_read"] == 3 and got["exit_code"] == 0

    def test_one_transcript_is_singular(self):
        assert once(ran(json.dumps(report(files_read=1))))["short"] \
            == "Updated: 1 transcript read."

    @pytest.mark.parametrize("field,value,phrase", [
        ("chains", {"directories": 1, "failed": [{"dir": "P--x", "error": "SQLITE_BUSY"}]},
         "the chains pass failed for 1 folder (SQLITE_BUSY)"),
        ("sidecars", {"read": 0, "failed": ["bad json in agent-1.meta"]},
         "the sidecar pass failed (bad json in agent-1.meta)"),
        ("desktop_records", {"deleted": 0, "failed": "EPERM: records root"},
         "the desktop records pass failed (EPERM: records root)"),
    ])
    def test_a_failed_subpass_is_partial_not_clean(self, field, value, phrase):
        got = once(ran(json.dumps(report(**{field: value}))))
        assert got["ok"] is True and got["partial"] is True
        assert phrase in got["sentence"] and got["summary"]["failures"] == [phrase]
        assert got["short"].startswith("Updated in part:")

    def test_exit_zero_without_a_report_is_a_failure(self):
        got = once(ran("harvest: 3 transcripts\n"))
        assert got["ok"] is False and "printed no report" in got["sentence"]
        assert got["short"].startswith("Update failed:")

    def test_a_json_value_that_is_not_a_report_is_not_one(self):
        assert harvest.read_report('"fine"') is None
        assert harvest.read_report('{"mode": "incremental"}') is None

    def test_a_stray_stdout_line_does_not_lose_the_report(self):
        got = once(ran("(node:1) ExperimentalWarning\n" + json.dumps(report(), indent=2) + "\nbye"))
        assert got["ok"] is True and got["summary"]["files_read"] == 3

    def test_a_missing_node_is_a_failure_that_changed_nothing(self):
        got = once(FileNotFoundError("[WinError 2] node"))
        assert got["ok"] is False and "could not start" in got["sentence"]
        assert "Nothing was changed" in got["sentence"] and got["exit_code"] is None

    def test_a_run_past_its_ceiling_says_what_is_kept(self):
        got = once(proc.TimeoutExpired(cmd="node", timeout=7200))
        assert got["ok"] is False and "already stored is kept" in got["sentence"]

    def test_a_crash_is_said_with_its_exit_code_and_the_tail_of_stderr(self):
        got = once(ran("", code=1, stderr="TypeError: x is not a function\n    at run"))
        assert got["ok"] is False and "exited 1" in got["sentence"]
        assert "TypeError" in got["sentence"]

    def test_what_else_happened_is_said_too(self):
        got = once(ran(json.dumps(report(rewritten_files=2, excluded_files=1,
                                         unknown_record_types=["zzz-new"]))))
        assert "2 transcripts had shrunk" in got["sentence"]
        assert "1 transcript skipped" in got["sentence"] and "zzz-new" in got["sentence"]


class TestTheJob:
    def jobs(self):
        return harvest.Jobs(boot="t")

    def start(self, jobs, run, **kw):
        kw.setdefault("spawn", lambda work: work())
        kw.setdefault("invalidate", lambda: None)
        kw.setdefault("capability_of", lambda: {"enabled": True, "reason": None, "why_not": None,
                                                "fix": None, "db": "a", "own": "a"})
        kw.setdefault("stamp", lambda: False)
        kw.setdefault("sleep", lambda s: None)
        return jobs.start("incremental", run=run, **kw)

    def test_a_finished_job_is_the_last_one_and_carries_its_report(self):
        jobs = self.jobs()
        began = self.start(jobs, runner(ran(json.dumps(report()))))
        assert began["state"] == "running" and began["id"] == "t-1"
        assert jobs.in_progress() is False
        done = jobs.finished()
        assert done["id"] == "t-1" and done["ok"] is True and done["finished_at"]
        assert done["short"] == "Updated: 3 transcripts read."

    def test_a_second_start_while_one_runs_is_busy(self):
        jobs, held = self.jobs(), []
        self.start(jobs, runner(ran(json.dumps(report()))), spawn=held.append)
        assert jobs.in_progress() is True
        with pytest.raises(harvest.Busy) as refusal:
            self.start(jobs, runner(ran(json.dumps(report()))), spawn=held.append)
        assert refusal.value.job["id"] == "t-1" and refusal.value.job["kind"] == "incremental"
        assert len(held) == 1
        held[0]()
        assert jobs.in_progress() is False
        assert self.start(jobs, runner(ran(json.dumps(report()))))["id"] == "t-2"

    def test_the_lock_is_free_after_a_failure_and_after_a_timeout(self):
        jobs = self.jobs()
        self.start(jobs, runner(ran("", code=1, stderr="boom")))
        self.start(jobs, runner(proc.TimeoutExpired(cmd="node", timeout=1)))
        assert jobs.finished()["id"] == "t-2" and jobs.finished()["ok"] is False
        assert self.start(jobs, runner(ran(json.dumps(report()))))["id"] == "t-3"

    def test_ids_name_the_process_so_a_restarted_server_cannot_reuse_one(self):
        """The page calls a run a success only when the server names its id back. A bare counter
        starts at 1 again with every server, so a run Restart C4X interrupted and the first run
        of the new server would both be job 1."""
        one, two = harvest.Jobs(), harvest.Jobs()
        first = self.start(one, runner(ran(json.dumps(report()))))["id"]
        second = self.start(two, runner(ran(json.dumps(report()))))["id"]
        assert first != second and first.endswith("-1") and second.endswith("-1")

    def test_a_job_that_has_just_ended_is_not_busy(self):
        """The worker clears `current` a moment before it frees the lock. A click in that moment
        finds the lock held and nobody to name; it waits for the lock instead of answering 409
        about a job that is over, which the page would have shown as a failed update."""
        import threading
        jobs = self.jobs()
        jobs._lock.acquire()
        threading.Timer(0.05, jobs._lock.release).start()
        began = self.start(jobs, runner(ran(json.dumps(report()))))
        assert began["id"] == "t-1" and jobs.finished()["ok"] is True

    def test_the_default_runner_in_a_test_is_a_tripwire(self, never_the_real_harvester):
        """The job swallows what its runner raises into a finished report (it must: a bug there
        cannot be allowed to wedge the lock), so the tripwire cannot fail a test by raising. It
        RECORDS, and the fixture fails the test when it is torn down. This test is the one
        place allowed to reach it, and it empties the record to say so."""
        jobs = self.jobs()
        jobs.start("incremental", spawn=lambda work: work(), invalidate=lambda: None,
                   stamp=lambda: False,
                   capability_of=lambda: {"enabled": True, "reason": None, "why_not": None,
                                          "fix": None, "db": "a", "own": "a"})
        assert len(never_the_real_harvester) == 1
        assert "harvest.mjs" in " ".join(never_the_real_harvester[0])
        assert jobs.finished()["ok"] is False
        never_the_real_harvester.clear()

    def test_a_spawn_that_cannot_start_frees_the_lock(self):
        jobs = self.jobs()

        def no_threads(work):
            raise RuntimeError("can't start new thread")
        with pytest.raises(RuntimeError):
            self.start(jobs, runner(ran(json.dumps(report()))), spawn=no_threads)
        assert jobs.in_progress() is False
        assert self.start(jobs, runner(ran(json.dumps(report()))))["id"] == "t-2"

    def test_every_finished_job_invalidates_after_the_run(self):
        jobs, order = self.jobs(), []

        def run(args, **kw):
            order.append("run")
            return ran(json.dumps(report()))
        self.start(jobs, run, invalidate=lambda: order.append("invalidate"),
                   after=lambda: order.append("after"))
        assert order == ["run", "invalidate", "after"]

    def test_a_failed_job_still_invalidates(self):
        jobs, order = self.jobs(), []
        self.start(jobs, runner(ran("", code=1, stderr="boom")),
                   invalidate=lambda: order.append("invalidate"),
                   after=lambda: order.append("after"))
        assert order == ["invalidate", "after"] and jobs.finished()["ok"] is False

    def test_a_cache_that_will_not_clear_does_not_lose_the_report(self):
        jobs = self.jobs()

        def broken():
            raise RuntimeError("cache gone")
        self.start(jobs, runner(ran(json.dumps(report()))), invalidate=broken)
        done = jobs.finished()
        assert done["ok"] is True and "caches were not cleared" in done["error"]
        assert jobs.in_progress() is False

    def test_a_fault_in_the_server_is_a_finished_failure_not_a_held_lock(self):
        jobs = self.jobs()

        def stamp():
            raise ZeroDivisionError("a bug")
        self.start(jobs, runner(ran(json.dumps(report()))), stamp=stamp)
        done = jobs.finished()
        assert done["ok"] is False and "inside the server" in done["sentence"]
        assert jobs.in_progress() is False

    def test_a_server_that_will_not_harvest_starts_nothing(self):
        jobs, calls = self.jobs(), []
        off = {"enabled": False, "reason": "not-own-store", "why_not": "a copy", "fix": "f",
               "db": "a", "own": "b"}
        with pytest.raises(harvest.Disabled) as refusal:
            self.start(jobs, runner(ran(json.dumps(report())), calls=calls),
                       capability_of=lambda: off)
        assert refusal.value.capability["reason"] == "not-own-store" and calls == []

    def test_a_busy_crash_is_retried_once_and_only_once(self):
        calls, naps = [], []
        locked = ran("", code=1, stderr="Error: database is locked")
        got = harvest.run_once("incremental", stamp=lambda: False, sleep=naps.append,
                               run=runner(locked, ran(json.dumps(report())), calls=calls),
                               root="X:/install")
        assert got["ok"] is True and got["attempts"] == 2 and len(calls) == 2
        assert naps == [harvest.RETRY_AFTER_S]
        calls.clear()
        got = harvest.run_once("incremental", stamp=lambda: False, sleep=naps.append,
                               run=runner(locked, calls=calls), root="X:/install")
        assert got["ok"] is False and got["attempts"] == 2 and len(calls) == 2
        assert "another harvest held the store" in got["sentence"] and "twice" in got["sentence"]

    def test_a_crash_that_is_not_busy_is_not_retried(self):
        calls = []
        harvest.run_once("incremental", stamp=lambda: False, sleep=lambda s: None,
                         run=runner(ran("", code=1, stderr="TypeError"), calls=calls),
                         root="X:/install")
        assert len(calls) == 1

    def test_the_stamp_is_written_first_and_never_creates_the_directory(self, tmp_path):
        assert harvest.write_stamp(tmp_path, now=lambda: "T") is False
        assert not (tmp_path / "data").exists()
        (tmp_path / "data" / "raw").mkdir(parents=True)
        order = []

        def run(args, **kw):
            order.append(("run", (tmp_path / "data" / "raw" / ".last-harvest").exists()))
            return ran(json.dumps(report()))
        harvest.run_once("incremental", run=run, root=tmp_path)
        assert order == [("run", True)]


class TestHowFresh:
    def test_last_harvest_is_plain_json_and_the_newest_row(self, tmp_path, monkeypatch):
        import sqlite3
        path = build_store(tmp_path / "store.db")
        con = sqlite3.connect(path)
        con.execute("INSERT INTO harvest_runs VALUES "
                    "('2026-09-20T17:53:37.000Z','incremental',9599,12,0,400,0.3,50,0,0,1412)")
        con.commit()
        con.close()
        monkeypatch.setattr(store, "DB_PATH", path)
        forget_cached_rows()
        got = store.last_harvest()
        assert got == {"ts": "2026-09-20T17:53:37.000Z", "mode": "incremental",
                       "files_seen": 9599, "files_read": 12, "ms": 1412}
        assert json.loads(json.dumps(got)) == got
        assert all(type(got[k]) is int for k in ("files_seen", "files_read", "ms"))

    def test_last_harvest_is_none_without_a_store_or_a_table(self, tmp_path, monkeypatch):
        import sqlite3
        monkeypatch.setattr(store, "DB_PATH", tmp_path / "absent.db")
        assert store.last_harvest() is None
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        monkeypatch.setattr(store, "DB_PATH", empty)
        assert store.last_harvest() is None


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A server whose store IS its install's own, a fresh job book, a job that finishes inside the
    POST, and a recording fake in place of the harvester."""
    (tmp_path / "data").mkdir()
    path = build_store(tmp_path / "data" / "context.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.delenv("C4X_NO_WRITES", raising=False)
    monkeypatch.setattr(harvest, "JOBS", harvest.Jobs(boot="t"))
    monkeypatch.setattr(harvest, "_default_spawn", lambda work: work())
    calls = []
    monkeypatch.setattr(harvest, "_default_run",
                        runner(ran(json.dumps(report())), calls=calls))
    forget_cached_rows()
    client = TestClient(api, base_url="http://127.0.0.1:8059")
    yield SimpleNamespace(client=client, calls=calls, root=tmp_path)
    forget_cached_rows()


class TestTheRoutes:
    def test_post_starts_a_job_and_get_reads_its_report(self, served):
        answer = served.client.post("/api/store/harvest", json={"kind": "incremental"})
        assert answer.status_code == 202
        body = answer.json()
        assert body["accepted"] is True and body["id"] == "t-1" and len(served.calls) == 1
        state = served.client.get("/api/store/harvest").json()
        assert state["enabled"] is True and state["running"] is False and state["job"] is None
        assert state["last"]["id"] == "t-1" and state["last"]["ok"] is True
        assert state["last"]["short"] == "Updated: 3 transcripts read."
        assert state["last_harvest"]["mode"] == "incremental" and state["kinds"] == ["incremental"]

    def test_busy_is_409_and_names_the_running_job(self, served, monkeypatch):
        held = []
        monkeypatch.setattr(harvest, "_default_spawn", held.append)
        assert served.client.post("/api/store/harvest", json={}).status_code == 202
        state = served.client.get("/api/store/harvest").json()
        assert state["running"] is True and state["job"]["id"] == "t-1"
        assert state["job"]["elapsed_s"] >= 0
        again = served.client.post("/api/store/harvest", json={})
        assert again.status_code == 409
        detail = again.json()["detail"]
        assert detail["reason"] == "busy" and detail["job"]["id"] == "t-1" and detail["error"]
        held[0]()
        assert served.client.get("/api/store/harvest").json()["running"] is False

    def test_a_served_copy_is_never_harvested_over_http(self, served, monkeypatch):
        copy = build_store(served.root / "copy.db")
        monkeypatch.setattr(store, "DB_PATH", copy)
        forget_cached_rows()
        state = served.client.get("/api/store/harvest").json()
        assert state["enabled"] is False and state["reason"] == "not-own-store"
        answer = served.client.post("/api/store/harvest", json={})
        assert answer.status_code == 403 and served.calls == []
        detail = answer.json()["detail"]
        assert detail["reason"] == "not-own-store" and "copy.db" in detail["error"]
        assert detail["fix"]

    def test_no_writes_is_403(self, served, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        answer = served.client.post("/api/store/harvest", json={})
        assert answer.status_code == 403 and served.calls == []
        assert served.client.get("/api/store/harvest").json()["reason"] == "no-writes"

    @pytest.mark.parametrize("body", [{"kind": "full"}, {"kind": "incremental", "dry_run": True},
                                      {"kind": "incremental", "dry_run": "yes"}])
    def test_a_job_that_does_not_exist_is_400(self, served, body):
        answer = served.client.post("/api/store/harvest", json=body)
        assert answer.status_code == 400 and served.calls == []
        assert answer.json()["detail"]["kinds"] == ["incremental"]

    def test_the_route_empties_the_response_cache_when_the_job_ends(self, served):
        cache.put("pane", ("v",), b"{}")
        hits_before = cache.stats()["hits"]
        assert cache.get("pane", ("v",)) == b"{}"
        served.client.post("/api/store/harvest", json={})
        assert cache.stats()["entries"] == 0
        assert cache.stats()["hits"] == hits_before + 1, "the counters health reports are kept"

    def test_asking_for_status_runs_nothing(self, served):
        for _ in range(3):
            assert served.client.get("/api/store/harvest").status_code == 200
        assert served.calls == []

    @pytest.mark.parametrize("headers", [{"Origin": EVIL}, {"Sec-Fetch-Site": "cross-site"},
                                         {"Host": "rebound.evil.example"}])
    def test_a_foreign_page_cannot_start_an_update(self, served, headers):
        answer = served.client.post("/api/store/harvest", json={}, headers=headers)
        assert answer.status_code == 403 and served.calls == []
        assert served.client.get("/api/store/harvest").json()["last"] is None

    def test_a_running_update_keeps_the_watchdog_waiting(self, served, monkeypatch):
        held = []
        monkeypatch.setattr(harvest, "_default_spawn", held.append)
        assert harvest.in_progress() is False
        served.client.post("/api/store/harvest", json={})
        assert harvest.in_progress() is True
        held[0]()
        assert harvest.in_progress() is False


class TestTheContractWithTheHarvester:
    def test_the_parser_reads_only_keys_the_harvester_prints(self):
        source = (ROOT / "tools" / "harvest.mjs").read_text(encoding="utf-8")
        block = re.search(r"export const RUN_REPORT_KEYS = Object\.freeze\(\[(.*?)\]\);",
                          source, re.DOTALL)
        assert block, "tools/harvest.mjs no longer exports RUN_REPORT_KEYS"
        printed = set(re.findall(r"'([a-z_]+)'", block.group(1)))
        assert len(printed) >= 20
        assert harvest.READS <= printed, f"read but never printed: {harvest.READS - printed}"

    def test_the_fake_report_in_this_file_has_only_real_keys(self):
        source = (ROOT / "tools" / "harvest.mjs").read_text(encoding="utf-8")
        block = re.search(r"export const RUN_REPORT_KEYS = Object\.freeze\(\[(.*?)\]\);",
                          source, re.DOTALL)
        printed = set(re.findall(r"'([a-z_]+)'", block.group(1)))
        assert set(report()) <= printed

    def test_every_key_the_summary_touches_is_declared(self):
        """EVERY SPELLING of a top-level read: `raw.get("x")`, `raw.get("x", 0)`, `raw["x"]`, either
        quote, and the `"x" in found` of `read_report`. A scan that knew one spelling let a key
        the harvester never prints be read through another."""
        source = (ROOT / "c4x" / "harvest.py").read_text(encoding="utf-8")
        touched = set(re.findall(r"""raw(?:\.get\(|\[)\s*["']([a-z_]+)["']""", source))
        touched |= set(re.findall(r"""["']([a-z_]+)["'] in found""", source))
        assert len(touched) >= 10
        assert touched <= harvest.READS, f"undeclared: {touched - harvest.READS}"

    def test_the_nested_keys_are_held_the_same_two_ways(self):
        source = (ROOT / "tools" / "harvest.mjs").read_text(encoding="utf-8")
        block = re.search(r"export const RUN_REPORT_NESTED = Object\.freeze\(\{(.*?)\}\);",
                          source, re.DOTALL)
        assert block, "tools/harvest.mjs no longer exports RUN_REPORT_NESTED"
        printed = {group: set(re.findall(r"'([a-z_]+)'", keys))
                   for group, keys in re.findall(r"(\w+): \[(.*?)\]", block.group(1))}
        assert set(printed) == set(harvest.READS_NESTED)
        for group, keys in harvest.READS_NESTED.items():
            assert keys <= printed[group], \
                f"{group}: read but never printed: {keys - printed[group]}"
        # And the other side: every nested read in c4x/harvest.py, through the local name the
        # summary uses for the group and through the inline `(raw.get("group") or {}).get("key")`.
        here = (ROOT / "c4x" / "harvest.py").read_text(encoding="utf-8")
        quoted = r"""["']([a-z_]+)["']"""
        for name, group in (("chains", "chains"), ("sidecars", "sidecars"),
                            ("records", "desktop_records")):
            read = set(re.findall(name + r"\.get\(\s*" + quoted, here))
            inline = r"raw\.get\(\s*[\"']" + group + r"[\"']\s*\) or \{\}\)\.get\(\s*" + quoted
            read |= set(re.findall(inline, here))
            assert read, f"{group}: the scan found no read at all, so it proves nothing"
            assert read <= harvest.READS_NESTED[group], \
                f"{group}: undeclared: {read - harvest.READS_NESTED[group]}"

"""The three write routes, over HTTP, against a store built for the test.

Separate from `test_projects.py` because these check a different thing: not whether the logic is
right, but whether the route hands it the right arguments. That is where the last cohort bug lived
- the logic was correct and the caller passed a bare path - and it showed up only as a wrong
session count, never as an error.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from c4x import projects, store
from c4x.api.main import api
from tests.test_projects import build_store, forget_cached_rows

ALPHA = r"P:\Alpha"
BETA = r"P:\Beta"


@pytest.fixture
def client(tmp_path, monkeypatch):
    build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "store.db")
    monkeypatch.delenv("C4X_NO_WRITES", raising=False)
    forget_cached_rows()
    # A LOOPBACK Host, because the server requires one. `/api/project/*` and the other
    # mutating routes are refused outright unless the Host header names loopback, which is what
    # closes DNS rebinding: an Origin check alone cannot see it, since a rebound name makes the
    # attacker's page same-origin. TestClient defaults to `Host: testserver` and would be refused
    # exactly as a rebound host is - correctly, but it would test the guard instead of the API.
    yield TestClient(api, base_url="http://127.0.0.1:8059")
    forget_cached_rows()


def count(project):
    con = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?", (project,)).fetchone()[0]
    finally:
        con.close()


class TestCohortHandling:
    def test_a_bare_path_is_refused_rather_than_treated_as_the_whole_store(self, client, tmp_path):
        """The bug this project has already paid for once.

        `cohort_sessions` resolves an unprefixed string to an EMPTY list, which every read path
        reads as "no restriction". A delete that inherited the same rule would take the store.
        """
        r = client.post("/api/project/delete", json={"cohort": ALPHA, "confirm": ALPHA})
        assert r.status_code == 400, r.text
        assert "does not name a project" in r.json()["detail"]["error"]
        assert count(ALPHA) == 3 and count(BETA) == 3

    def test_a_section_cohort_is_refused_too(self, client):
        r = client.get("/api/project/export", params={"cohort": "section::whatever"})
        assert r.status_code == 400

    def test_an_empty_cohort_is_refused(self, client):
        assert client.post("/api/project/delete",
                           json={"cohort": "", "confirm": ""}).status_code == 400


class TestExport:
    def test_the_file_it_built_is_gone_once_the_response_is_sent(self, client, tmp_path,
                                                                monkeypatch):
        """An export WRITES the file it serves, and nothing used to delete it.

        Every call wrote into one shared `tmp/exports`, named after the project, and the largest
        project in this store exports to 180 MB. Nothing in the repo removed them, so a route
        documented as a read grew the checkout without bound. The fix is a directory per call
        removed by a BackgroundTask, which runs after the body is sent, so this asserts on the
        state AFTER the response rather than during it.

        Two exports, because a per-call directory is also what stops two concurrent exports of the
        same project from deleting each other's file, and one call cannot show that.
        """
        import c4x.api.main as main
        monkeypatch.setattr(main, "ROOT", tmp_path)
        exports = tmp_path / "tmp" / "exports"

        for _ in range(2):
            r = client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"})
            assert r.status_code == 200, r.text
            assert len(r.content) > 0, "the response carried no file"

        left = sorted(p for p in exports.rglob("*") if p.is_file()) if exports.exists() else []
        assert left == [], f"the export files outlived their responses: {left}"

    def test_it_serves_a_file_that_verifies(self, client, tmp_path):
        r = client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"})
        assert r.status_code == 200, r.text
        assert r.headers["x-c4x-sessions"] == "3"
        assert r.headers["x-c4x-project"] == ALPHA
        got = tmp_path / "downloaded.db"
        got.write_bytes(r.content)
        ok, problems = projects.verify(got)
        assert ok, problems

    def test_the_bytes_on_the_wire_carry_the_values_not_just_the_counts(self, client, tmp_path):
        """A truncated or re-encoded download would still have the right row count."""
        r = client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"})
        got = tmp_path / "downloaded.db"
        got.write_bytes(r.content)
        theirs = sqlite3.connect(f"file:{got}?mode=ro", uri=True)
        mine = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
        a = theirs.execute("SELECT * FROM turns ORDER BY uuid").fetchall()
        b = mine.execute("""SELECT * FROM turns WHERE session_id IN
                            (SELECT session_id FROM sessions WHERE cwd = ?)
                            ORDER BY uuid""", (ALPHA,)).fetchall()
        theirs.close()
        mine.close()
        assert a == b

    def test_an_unknown_project_is_a_404(self, client):
        r = client.get("/api/project/export", params={"cohort": r"project::P:\Nope"})
        assert r.status_code == 404


class TestDelete:
    def test_a_wrong_confirmation_is_refused_and_removes_nothing(self, client):
        r = client.post("/api/project/delete",
                        json={"cohort": f"project::{ALPHA}", "confirm": r"P:\alpha"})
        # 409, not 400: the request was well formed and the guard refused it.
        assert r.status_code == 409, r.text
        assert count(ALPHA) == 3

    def test_a_missing_confirmation_is_refused(self, client):
        assert client.post("/api/project/delete",
                           json={"cohort": f"project::{ALPHA}"}).status_code == 409
        assert count(ALPHA) == 3

    def test_the_right_confirmation_removes_it_and_leaves_the_other_project(self, client):
        r = client.post("/api/project/delete",
                        json={"cohort": f"project::{ALPHA}", "confirm": ALPHA})
        assert r.status_code == 200, r.text
        assert count(ALPHA) == 0
        assert count(BETA) == 3, "deleting one project took another"
        assert r.json()["excluded"] is True

    def test_the_round_trip_works_over_HTTP(self, client, tmp_path):
        exported = client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"}).content
        client.post("/api/project/delete",
                    json={"cohort": f"project::{ALPHA}", "confirm": ALPHA})
        assert count(ALPHA) == 0
        r = client.post("/api/project/import",
                        files={"file": ("alpha.db", exported, "application/vnd.sqlite3")})
        assert r.status_code == 200, r.text
        assert count(ALPHA) == 3
        assert sum(r.json()["inserted"].values()) > 0

    def test_the_destination_reaches_the_import_and_the_rows_follow_it(self, client, tmp_path):
        """THE CLI IS NOT THE PRODUCT. A destination that exists only as a flag is unreachable
        from the page, which is where this is actually used.

        Checked on the STORE rather than on the response, so a route that accepted the field and
        dropped it would still fail."""
        from c4x import store
        exported = client.get("/api/project/export",
                              params={"cohort": f"project::{ALPHA}"}).content
        client.post("/api/project/delete", json={"cohort": f"project::{ALPHA}", "confirm": ALPHA})
        client.post("/api/project/include", json={"project": ALPHA})
        r = client.post("/api/project/import",
                        files={"file": ("alpha.db", exported, "application/vnd.sqlite3")},
                        data={"into": r"D:\Work\Alpha"})
        assert r.status_code == 200, r.text
        assert r.json()["into"] == [r"D:\Work\Alpha"]
        landed = store.q("SELECT DISTINCT cwd FROM sessions WHERE cwd LIKE 'D:%'")
        assert list(landed["cwd"]) == [r"D:\Work\Alpha"]

    def test_a_dry_run_over_http_writes_nothing(self, client):
        exported = client.get("/api/project/export",
                              params={"cohort": f"project::{ALPHA}"}).content
        client.post("/api/project/delete", json={"cohort": f"project::{ALPHA}", "confirm": ALPHA})
        r = client.post("/api/project/import",
                        files={"file": ("alpha.db", exported, "application/vnd.sqlite3")},
                        data={"into": r"D:\Work\Alpha", "dry_run": "true"})
        assert r.status_code == 200, r.text
        assert r.json()["dry_run"] is True
        assert count(ALPHA) == 0, "a dry run put the rows back"

    def test_importing_something_that_is_not_an_export_is_a_400(self, client):
        r = client.post("/api/project/import",
                        files={"file": ("junk.db", b"this is not a database", "application/x")})
        assert r.status_code == 400

    def test_a_tampered_upload_is_refused(self, client, tmp_path):
        exported = client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"}).content
        path = tmp_path / "tampered.db"
        path.write_bytes(exported)
        con = sqlite3.connect(str(path))
        con.execute("UPDATE messages SET text = text || 'x'")
        con.commit()
        con.close()
        r = client.post("/api/project/import",
                        files={"file": ("t.db", path.read_bytes(), "application/vnd.sqlite3")})
        assert r.status_code == 400, r.text


class TestExclusions:
    def test_a_deleted_project_is_listed_and_can_be_let_back_in(self, client):
        client.post("/api/project/delete",
                    json={"cohort": f"project::{ALPHA}", "confirm": ALPHA})
        listed = client.get("/api/project/excluded").json()["excluded"]
        assert [e["cwd"] for e in listed] == [ALPHA]
        assert client.post("/api/project/include", json={"project": ALPHA}).status_code == 200
        assert client.get("/api/project/excluded").json()["excluded"] == []

    def test_keep_capturing_deletes_without_excluding(self, client):
        r = client.post("/api/project/delete", json={
            "cohort": f"project::{ALPHA}", "confirm": ALPHA, "keep_capturing": True})
        assert r.status_code == 200
        assert r.json()["excluded"] is False
        assert client.get("/api/project/excluded").json()["excluded"] == []


class TestTheOneIrreversibleFlag:
    """`purge_snapshots` reaches what the backup does not carry, so it takes literal true only.

    `bool(body.get(...))` accepted the STRING "false", 0.1, [0] and any non-empty string, which is
    exactly the shape a hand-written or mis-serialised request arrives in. Every other flag here is
    undoable by importing the backup; this one is not.
    """

    def test_the_string_false_does_not_purge_snapshots(self, client, monkeypatch):
        from c4x import projects
        seen = {}

        def record(project, confirm, out_dir=None, keep_capturing=False, purge_snapshots=False):
            seen["purge_snapshots"] = purge_snapshots
            return {"project": project, "backup": "x", "removed": {}, "still_here": []}

        monkeypatch.setattr(projects, "delete", record)

        client.post("/api/project/delete", json={
            "cohort": f"project::{ALPHA}", "confirm": ALPHA, "purge_snapshots": "false"})

        assert seen["purge_snapshots"] is False

    def test_literal_true_still_purges(self, client, monkeypatch):
        from c4x import projects
        seen = {}

        def record(project, confirm, out_dir=None, keep_capturing=False, purge_snapshots=False):
            seen["purge_snapshots"] = purge_snapshots
            return {"project": project, "backup": "x", "removed": {}, "still_here": []}

        monkeypatch.setattr(projects, "delete", record)

        client.post("/api/project/delete", json={
            "cohort": f"project::{ALPHA}", "confirm": ALPHA, "purge_snapshots": True})

        assert seen["purge_snapshots"] is True


class TestAHalfFinishedDeleteOverHttp:
    """A failure after the rows are committed must not answer like a refusal.

    409 says "the request was well formed and the server refused it", which is what a wrong
    confirmation string gets. A delete that removed every row and then failed is the opposite of a
    refusal, and answering it the same way told the user nothing had happened.
    """

    def test_a_post_commit_failure_is_500_and_names_the_backup(self, client, monkeypatch):
        from c4x import projects

        def half_finished(*_args, **_kwargs):
            raise projects.AfterTheRowsWereRemoved(
                "the file half failed. THE ROWS ARE ALREADY GONE: the backup at tmp/x.db is the "
                "only copy of it and importing that file puts it back.")

        monkeypatch.setattr(projects, "delete", half_finished)

        response = client.post("/api/project/delete", json={
            "cohort": f"project::{ALPHA}", "confirm": ALPHA})

        assert response.status_code == 500
        body = response.json()["detail"]
        assert "THE ROWS ARE ALREADY GONE" in body["error"]
        assert "tmp/x.db" in body["error"], "the backup path is the one thing the user needs"

    def test_a_refusal_is_still_409(self, client, monkeypatch):
        from c4x import projects

        def refuse(*_args, **_kwargs):
            raise ValueError("confirmation does not match the project path; nothing was deleted")

        monkeypatch.setattr(projects, "delete", refuse)

        response = client.post("/api/project/delete", json={
            "cohort": f"project::{ALPHA}", "confirm": "wrong"})

        assert response.status_code == 409


class TestTheWriteSwitch:
    """`--no-writes` must turn these off WITHOUT claiming the server harvests.

    Two separate facts on /api/health. Collapsed into one flag, the UI could disable the controls
    but not say why, so it would fail on click instead.
    """

    def test_health_reports_the_two_facts_separately(self, client):
        body = client.get("/api/health").json()
        assert body["read_only"] is True
        assert body["writes_enabled"] is True

    def test_every_write_route_refuses(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        health = client.get("/api/health").json()
        # Both facts, still separate. Turning writes off must not start claiming the server
        # harvests, which is what one collapsed flag would have done.
        assert health["writes_enabled"] is False
        assert health["read_only"] is True
        for r in (client.get("/api/project/export", params={"cohort": f"project::{ALPHA}"}),
                  client.post("/api/project/delete",
                              json={"cohort": f"project::{ALPHA}", "confirm": ALPHA}),
                  client.post("/api/project/import",
                              files={"file": ("x.db", b"x", "application/x")}),
                  client.post("/api/project/include", json={"project": ALPHA})):
            assert r.status_code == 403, r.url
        assert count(ALPHA) == 3, "a refused delete still removed rows"

    def test_reading_the_exclusions_still_works_with_writes_off(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        r = client.get("/api/project/excluded")
        assert r.status_code == 200
        assert r.json()["writes_enabled"] is False


class TestTheAccountSwitch:
    """Sharing one chat list between the accounts on this machine, over HTTP.

    The logic lives in `c4x.accounts` and is tested there. What these check is the route: that it
    refuses a mode it does not understand, that it refuses at all on a server started with
    `--no-writes`, and that the one refusal a user can act on arrives as a 409 with the sentence
    that says what to do, rather than as a 500 with an empty body.
    """

    def test_the_state_is_readable_without_writes(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        answer = client.get("/api/accounts")
        assert answer.status_code == 200
        body = answer.json()
        assert set(body) >= {"supported", "mode", "intended", "pairs", "app_running"}

    def test_an_unknown_mode_is_refused_before_anything_moves(self, client):
        answer = client.post("/api/accounts/sharing", json={"mode": "both"})
        assert answer.status_code == 400
        assert "mode" in str(answer.json()["detail"])

    def test_a_server_with_no_writes_refuses_to_move_directories(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        answer = client.post("/api/accounts/sharing", json={"mode": "all"})
        assert answer.status_code in (403, 409, 503), answer.status_code

    def test_the_app_being_open_is_a_409_that_says_what_to_do(self, client, monkeypatch):
        from c4x import accounts
        monkeypatch.setattr(accounts, "supported", lambda: (True, ""))
        monkeypatch.setattr(accounts, "app_running", lambda: True)
        answer = client.post("/api/accounts/sharing", json={"mode": "all"})
        assert answer.status_code == 409
        assert "Quit Claude" in str(answer.json()["detail"]["error"])

    def test_verify_answers_on_a_machine_that_never_shared(self, client, tmp_path, monkeypatch):
        # A ROOT OF ITS OWN, empty. Intent is read from the disk when the marker is absent, and
        # the machine this runs on may well have shared: this test's premise is a machine that
        # never did, so it gets one.
        monkeypatch.setattr(store, "sessions_roots", lambda: [str(tmp_path / "roots")])
        answer = client.get("/api/accounts/verify")
        assert answer.status_code == 200
        assert answer.json()["intended"] == "current"

    def test_the_state_says_where_the_intent_came_from_and_what_is_uncovered(self, client):
        body = client.get("/api/accounts").json()
        assert body["intended_source"] in ("marker", "disk", "none")
        assert isinstance(body["uncovered"], list)

    def test_cover_now_refuses_without_writes(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        answer = client.post("/api/accounts/reconcile")
        assert answer.status_code in (403, 409, 503), answer.status_code

    def test_cover_now_is_a_409_with_the_pending_pairs_while_the_app_runs(self, client,
                                                                          monkeypatch):
        from c4x import accounts
        pending = [{"root": "R", "account": "a", "org": "o", "path": "R/a/o", "records": 2}]
        monkeypatch.setattr(accounts, "reconcile", lambda: {
            "ran": False, "app_running": True, "pending": pending,
            "why": "Claude is running, and a directory it has open cannot be moved. Quit Claude "
                   "and try again; nothing has been changed."})
        answer = client.post("/api/accounts/reconcile")
        assert answer.status_code == 409
        detail = answer.json()["detail"]
        assert "Quit Claude" in detail["error"] and detail["pending"] == pending

    def test_cover_now_returns_the_report(self, client, monkeypatch):
        from c4x import accounts
        report = {"ran": True, "app_running": False, "pending": [], "why": "covered 1 pair(s)",
                  "roots": [], "backup": "B", "marker_written": True, "restart_required": True}
        monkeypatch.setattr(accounts, "reconcile", lambda: report)
        answer = client.post("/api/accounts/reconcile")
        assert answer.status_code == 200 and answer.json() == report

    def test_cover_now_with_nothing_to_do_is_a_200_that_says_so(self, client, monkeypatch):
        from c4x import accounts
        monkeypatch.setattr(accounts, "reconcile", lambda: {
            "ran": False, "app_running": True, "pending": [],
            "why": "Claude is running; nothing to cover"})
        answer = client.post("/api/accounts/reconcile")
        assert answer.status_code == 200 and answer.json()["ran"] is False

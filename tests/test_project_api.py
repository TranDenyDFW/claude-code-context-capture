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

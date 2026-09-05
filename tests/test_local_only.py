"""The two guards that decide what a WEB PAGE can do to this store, and what an import may write.

Both exist because of the same wrong assumption: that "only local processes can reach 127.0.0.1"
is the whole trust model. It is not. A page you visit in a browser can SEND a request to loopback -
CORS governs whether it may read the RESPONSE, never whether the request is dispatched - and a
multipart form POST is a CORS *simple* request, so it goes without a preflight to stop it.

`c4x/server.py` writes that threat model out in full for `/__shutdown__`, which was the one route
that carried a guard. The other five did not.

Each test here is paired with its positive control. A guard that refuses everything passes every
refusal assertion ever written, so every refusal below is followed by the same request succeeding
from a caller that should be allowed.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from c4x import projects, store
from c4x.api.main import api
from tests.test_projects import build_store, forget_cached_rows

EVIL = "http://evil.example"


@pytest.fixture
def client(tmp_path, monkeypatch):
    build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "store.db")
    monkeypatch.delenv("C4X_NO_WRITES", raising=False)
    forget_cached_rows()
    yield TestClient(api, base_url="http://127.0.0.1:8059")
    forget_cached_rows()


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Where an upload would land, isolated so this test can assert the directory is untouched."""
    import c4x.api.main as main
    monkeypatch.setattr(main, "ROOT", tmp_path)
    return tmp_path / "tmp" / "imports"


# ---------------------------------------------------------------------------
# The origin guard
# ---------------------------------------------------------------------------
def test_a_cross_origin_multipart_upload_is_refused_before_it_reaches_disk(client, staging):
    """The exact request an external reviewer sent, which reached the handler and wrote a file.

    Multipart is the shape that matters. A JSON body cannot be sent cross-origin without a
    preflight, so those routes were already covered by the CORS allow-list by accident; this one
    never was.

    THE DIRECTORY ASSERTION IS THE POINT, not the status code. A route dependency would also
    return 403 here, and would return it AFTER FastAPI had already awaited `request.form()` and
    spooled the whole body onto disk. Only a check that runs before routing can leave the
    directory empty, so that is what this asserts.
    """
    before = sorted(p.name for p in staging.glob("*")) if staging.exists() else []
    response = client.post("/api/project/import",
                           files={"file": ("payload.db", b"x" * 8192)},
                           headers={"Origin": EVIL})
    assert response.status_code == 403, response.text
    after = sorted(p.name for p in staging.glob("*")) if staging.exists() else []
    assert after == before, f"the refused upload still reached disk: {set(after) - set(before)}"


@pytest.mark.parametrize("path,body", [
    ("/api/project/delete", {"cohort": "project::P:\\Alpha", "confirm": "P:\\Alpha"}),
    ("/api/project/include", {"project": "P:\\Alpha"}),
    ("/api/messages/text", {"uuids": []}),
    ("/api/mirror/predict", {"tokens": 1}),
])
def test_every_mutating_route_refuses_a_cross_origin_caller(client, path, body):
    assert client.post(path, json=body, headers={"Origin": EVIL}).status_code == 403


def test_the_guard_also_refuses_a_browser_that_sends_no_origin(client):
    """Sec-Fetch-Site, for the hop a browser labels even when it omits Origin."""
    assert client.post("/api/project/include", json={"project": "x"},
                       headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_a_rebound_host_is_refused(client):
    """DNS rebinding is the hole an Origin check alone cannot see.

    A name the attacker controls resolves to 127.0.0.1, so their page becomes same-origin with this
    server: Origin passes, Sec-Fetch-Site says same-origin, and both checks above are satisfied.
    What does not change is the Host header, which still names the attacker's domain.
    """
    assert client.post("/api/project/include", json={"project": "x"},
                       headers={"Host": "rebound.evil.example"}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8059", "localhost:8059", "app.localhost:3000",
                                  "[::1]:8059"])
def test_a_loopback_host_is_allowed_through(client, host):
    """The positive control for the Host rule, including the names a dev setup really uses.

    `*.localhost` is reserved for loopback by RFC 6761, so refusing it would break a legitimate
    local caller to stop nothing. Asserting only that the request is not refused BY THE GUARD: what
    the handler then decides is a different question, tested elsewhere.
    """
    response = client.post("/api/project/include", json={"project": "x"}, headers={"Host": host})
    assert response.status_code != 403 or "loopback Host" not in response.text


def test_a_local_caller_with_no_origin_still_works(client):
    """The control that matters most: curl, the CLI and every script send no Origin at all.

    A guard that refused those would stop the only way anyone actually uses this tool while
    stopping none of the attack.
    """
    assert client.get("/api/tabs").status_code == 200
    assert client.post("/api/mirror/predict", json={"tokens": 850_000}).status_code == 200
    # Reaches the handler and is refused on its own terms - a 400 about the cohort, not a 403.
    refused = client.post("/api/project/delete", json={"cohort": "nonsense", "confirm": "x"})
    assert refused.status_code == 400, refused.text


# ---------------------------------------------------------------------------
# The manifest, which decides what an import is allowed to write
# ---------------------------------------------------------------------------
def make_export(path, digests, counts, tables, schema):
    con = sqlite3.connect(str(path))
    for table, cols in schema.items():
        con.execute(f"CREATE TABLE {table} ({cols})")
    con.execute("CREATE TABLE c4x_export (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO c4x_export VALUES ('manifest', ?)",
                (json.dumps({"format": "c4x-project-export/1", "project": "P:\\Alpha",
                             "digests": digests, "counts": counts, "tables": tables}),))
    con.commit()
    con.close()
    return path


def test_an_export_whose_tables_exceed_its_digests_is_refused(tmp_path):
    """VERIFIED ON ONE SET, APPLIED TO ANOTHER. The manifest carries three lists of table names.

    `verify()` walked `digests` and `counts`. `import_()` walked `tables`. Nothing compared them,
    and the name from `tables` went straight into `INSERT OR IGNORE INTO main.{table} ... FROM
    src.{table}` on the live store's read-write connection - under a docstring reading "Verified
    before a single row is written". A file listing a table the digests did not cover verified
    clean and then wrote undigested rows into the user's store.
    """
    good = make_export(tmp_path / "honest.db", {}, {}, [], {"sessions": "session_id TEXT"})
    con = sqlite3.connect(str(good))
    digest = projects.digest(con, "sessions")
    con.close()

    honest = make_export(tmp_path / "ok.db", {"sessions": digest}, {"sessions": 0}, ["sessions"],
                         {"sessions": "session_id TEXT"})
    assert projects.verify(honest) == (True, []), "a genuine export must still import"

    # The attack: one digested table, three named for import.
    forged = make_export(tmp_path / "forged.db", {"sessions": digest}, {"sessions": 0},
                         ["sessions", "messages", "tool_calls"],
                         {"sessions": "session_id TEXT", "messages": "uuid TEXT",
                          "tool_calls": "uuid TEXT"})
    ok, problems = projects.verify(forged)
    assert not ok, "a manifest naming undigested tables was accepted"
    assert any("messages" in p for p in problems), problems
    # And the set the import would actually touch is the verified one, whatever the file says.
    assert projects.carried_tables(projects.read_manifest(forged)) == ["sessions"]


def test_a_manifest_naming_a_table_this_store_does_not_export_is_refused(tmp_path):
    """The second, independent guard: an allow-list, because the name reaches SQL as an identifier.

    Set agreement alone would accept a self-consistent manifest that named anything at all, and
    quoting is not what makes an identifier safe here - knowing it is one of ours is.
    """
    forged = make_export(tmp_path / "foreign.db", {"sqlite_master": "x"}, {"sqlite_master": 0},
                         ["sqlite_master"], {"sessions": "session_id TEXT"})
    ok, problems = projects.verify(forged)
    assert not ok and any("does not export" in p for p in problems), problems

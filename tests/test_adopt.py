r"""Adopting a session the desktop app has no record for.

The fixture is a machine: one records root with two account pairs, a config naming the signed-in
account, a store with seven sessions shaped one clause of the candidate rule each, and real
transcript files under a scratch home. Every root the module reads is pointed at it, in both the
module that writes (`appstate`) and the one that reads (`store`), because a fixture that patches
one and not the other is how a write lands in a directory no test looks at.
"""
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import accounts, adopt, appstate, reviews, store  # noqa: E402
from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

A, B = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
ORG_A, ORG_B = "11111111-3333-4333-8333-111111111111", "22222222-4444-4444-8444-222222222222"
ALPHA, BETA, GAMMA, DELTA = r"P:\Alpha", r"P:\Beta", r"P:\Gamma", r"P:\Delta"


def record(folder, name, title="a chat"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"local_{name}.json"
    path.write_text(json.dumps({"cliSessionId": name, "title": title}), encoding="utf-8")
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Sessions, one per clause of the rule:

        s0-0  desktop, transcript present, no record               CANDIDATE (Alpha, custom title)
        s0-1  desktop, transcript present, record under A          has a record
        s0-2  desktop, transcript present, record under B          other_account
        s1-0  desktop, transcript MISSING                          excluded
        s1-1  cli, transcript present                              cli candidate only
        s1-2  desktop, transcript under subagents (backslashes)    excluded
        s2-0  NULL entrypoint, only a last-prompt title            CANDIDATE (Gamma, no title)
        s2-1  desktop, no turns at all                             excluded
        s2-2  desktop, transcript under subagents (slashes)        excluded
    """
    root = tmp_path / "appdata" / "Claude" / "claude-code-sessions"
    (root / A / ORG_A).mkdir(parents=True)
    (root / B / ORG_B).mkdir(parents=True)
    (root.parent / "config.json").write_text(json.dumps({"lastKnownAccountUuid": A}),
                                             encoding="utf-8")
    (root.parent / "plan-usage-history.json").write_text(
        json.dumps({"samples": [{"org": ORG_A, "t": 1}]}), encoding="utf-8")
    monkeypatch.setattr(appstate, "sessions_root", lambda: str(root))
    monkeypatch.setattr(store, "sessions_roots", lambda: [str(root)])
    monkeypatch.setattr(store, "sessions_root", lambda: str(root))
    monkeypatch.setattr(accounts, "app_running", lambda: False)

    db = tmp_path / "data" / "context.db"
    db.parent.mkdir(parents=True)
    build_store(db)
    home = tmp_path / "home" / ".claude" / "projects" / "slug"
    home.mkdir(parents=True)

    def transcript(name, sub=None):
        if sub is None:
            path = home / f"{name}.jsonl"
        else:
            path = home / name / "subagents" / f"agent-{name}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type":"summary"}\n', encoding="utf-8")
        text = str(path)
        return text.replace("\\", "/") if sub == "slash" else text

    con = sqlite3.connect(str(db))
    con.execute("UPDATE sessions SET entrypoint = 'claude-desktop'")
    for sid in ("s0-0", "s0-1", "s0-2"):
        con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
                    (transcript(sid), sid))
    con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = 's1-0'",
                (str(home / "s1-0-missing.jsonl"),))
    con.execute("UPDATE sessions SET entrypoint = 'cli', transcript_path = ? "
                "WHERE session_id = 's1-1'", (transcript("s1-1"),))
    con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = 's1-2'",
                (transcript("s1-2", sub="back"),))
    for sid, cwd, entrypoint, path in (
        ("s2-0", GAMMA, None, transcript("s2-0")),
        ("s2-1", GAMMA, "claude-desktop", transcript("s2-1")),
        ("s2-2", GAMMA, "claude-desktop", transcript("s2-2", sub="slash")),
        ("s3-0", DELTA, "claude-desktop", transcript("s3-0")),
        ("s3-1", DELTA, "claude-desktop", transcript("s3-1")),
    ):
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, "slug-2", cwd, "main", "2.1.229", entrypoint,
                     "2026-08-03T00:00:00Z", "2026-08-03T00:00:00Z", path))
    # Delta's two sessions are a day newer than Gamma's, so "newest first" has an order to show.
    for sid in ("s2-0", "s2-2", "s3-0", "s3-1"):
        day = "04" if sid.startswith("s3") else "03"
        for i in range(2):
            con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id,
                             output_tokens, file_path, line_no)
                           VALUES (?,?,?,'claude-sonnet-5',?,1,'f',?)""",
                        (f"{sid}-t{i}", sid, f"2026-08-{day}T12:0{i}:00Z", f"req-{sid}-{i}", i))
    con.execute("UPDATE session_titles SET kind = 'last-prompt', title = 'the whole prompt text' "
                "WHERE session_id = 's0-0' AND kind = 'custom'")
    con.execute("INSERT INTO session_titles VALUES ('s0-0', 'custom', 'Alpha work', 'f', 1)")
    con.execute("INSERT INTO session_titles VALUES ('s2-0', 'last-prompt', 'raw prompt', 'f', 1)")
    # s3-0 has no title of any kind, only what was typed: the name comes from messages. A tool
    # result and a sidechain prompt sit earlier and must not win.
    typed = "  Please   refactor the\n parser and the tests around it, then run them all twice more"
    for uuid_, ts, kind, text, side in (
        ("s3-0-r", "2026-08-04T11:59:00Z", "tool_result", "ignored", 0),
        ("s3-0-s", "2026-08-04T11:59:30Z", "typed", "a subagent prompt", 1),
        ("s3-0-m", "2026-08-04T12:00:00Z", "typed", typed, 0),
        ("s3-0-n", "2026-08-04T12:01:00Z", "typed", "later", 0),
    ):
        con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                         is_sidechain, file_path, line_no)
                       VALUES (?, 's3-0', ?, 'user', ?, ?, ?, ?, 'f', 1)""",
                    (uuid_, ts, kind, text, len(text), side))
    con.commit()
    con.close()

    record(root / A / ORG_A, "s0-1")
    record(root / B / ORG_B, "s0-2")
    (root / A / ORG_A / "deleted_1111").write_text("1", encoding="utf-8")
    (root / A / ORG_A / "deleted_2222").write_text("1", encoding="utf-8")

    monkeypatch.setattr(store, "DB_PATH", db)
    forget_cached_rows()
    yield root
    forget_cached_rows()


def candidate_ids(state):
    return sorted(s["session_id"] for g in state["groups"] for s in g["sessions"])


class TestWhatIsOffered:
    def test_the_candidate_rule_one_clause_at_a_time(self, machine):
        state = adopt.state()
        assert state["supported"] is True
        assert candidate_ids(state) == ["s0-0", "s2-0", "s3-0", "s3-1"]
        assert state["candidates"] == 4
        assert state["cli_candidates"] == 1, "s1-1 is counted, not offered"
        assert state["other_account"] == 1, "s0-2 belongs to the other account"
        assert state["pair"]["account"] == A and state["pair"]["org"] == ORG_A

    def test_cli_sessions_are_offered_only_when_asked(self, machine):
        state = adopt.state(include_cli=True)
        assert candidate_ids(state) == ["s0-0", "s1-1", "s2-0", "s3-0", "s3-1"]
        flagged = {s["session_id"]: s["cli"] for g in state["groups"] for s in g["sessions"]}
        assert flagged == {"s0-0": False, "s1-1": True, "s2-0": False, "s3-0": False,
                           "s3-1": False}

    def test_a_subagent_path_is_excluded_in_both_spellings(self, machine):
        assert adopt.is_subagent_path(r"C:\t\s\subagents\agent-x.jsonl")
        assert adopt.is_subagent_path("C:/t/s/subagents/agent-x.jsonl")
        assert not adopt.is_subagent_path(r"C:\t\s.jsonl")
        offered = candidate_ids(adopt.state())
        assert "s1-2" not in offered and "s2-2" not in offered

    def test_groups_are_by_folder_newest_first(self, machine):
        state = adopt.state()
        assert [g["cwd"] for g in state["groups"]] == [DELTA, GAMMA, ALPHA]
        assert [g["project"] for g in state["groups"]] == ["Delta", "Gamma", "Alpha"]
        assert [g["count"] for g in state["groups"]] == [2, 1, 1]

    def test_deleted_markers_are_counted_for_the_signed_in_pair(self, machine):
        assert adopt.state()["deleted_markers"] == 2

    def test_no_pair_evidence_is_reported_not_written(self, tmp_path, monkeypatch, machine):
        bare = tmp_path / "bare" / "Claude" / "claude-code-sessions"
        bare.mkdir(parents=True)
        monkeypatch.setattr(appstate, "sessions_root", lambda: str(bare))
        monkeypatch.setattr(store, "sessions_roots", lambda: [str(bare)])
        state = adopt.state()
        assert state["supported"] is False and "no desktop account" in state["why_not"]
        report = adopt.adopt([ALPHA])
        assert report["supported"] is False and report["written"] == []
        assert not list(bare.rglob("local_*.json"))


class TestTheWrite:
    def test_dry_run_names_the_files_and_writes_nothing(self, machine):
        report = adopt.adopt([ALPHA], dry_run=True)
        assert report["selected"] == 1 and len(report["written"]) == 1
        assert report["written"][0]["path"].startswith(str(machine / A / ORG_A))
        assert not Path(report["written"][0]["path"]).exists()
        assert report["restart_required"] is False
        assert not adopt.ledger_path().exists()

    def test_the_record_carries_the_nine_fields_the_link_and_the_custom_title(self, machine):
        report = adopt.adopt([ALPHA])
        assert [w["session_id"] for w in report["written"]] == ["s0-0"]
        path = Path(report["written"][0]["path"])
        assert path.parent == machine / A / ORG_A
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) == set(adopt.FIELDS) | {"cliSessionId", "title", "titleSource"}
        assert data["cliSessionId"] == "s0-0"
        assert data["sessionId"].startswith("local_") and path.name == f"{data['sessionId']}.json"
        assert data["cwd"] == ALPHA and data["originCwd"] == ALPHA
        assert data["title"] == "Alpha work", "the custom title, never the last prompt"
        assert data["titleSource"] == "user", "a person typed it"
        assert data["model"] == "claude-opus-5"
        assert data["isArchived"] is False and data["permissionMode"] == "default"
        assert data["remoteMcpServersConfig"] == []
        first = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
        assert data["createdAt"] == int(first.timestamp() * 1000)
        assert isinstance(data["lastActivityAt"], int)
        assert data["lastActivityAt"] > data["createdAt"]
        assert report["restart_required"] is True and report["note"] is None

    def test_a_last_prompt_becomes_the_name(self, machine):
        report = adopt.adopt([GAMMA])
        data = json.loads(Path(report["written"][0]["path"]).read_text(encoding="utf-8"))
        assert data["title"] == "raw prompt" and data["titleSource"] == "auto"
        assert data["model"] == "claude-sonnet-5"

    def test_the_opening_typed_prompt_names_a_session_the_titles_table_does_not(self, machine):
        report = adopt.adopt([DELTA])
        by_id = {w["session_id"]: json.loads(Path(w["path"]).read_text(encoding="utf-8"))
                 for w in report["written"]}
        assert by_id["s3-0"]["title"] == ("Please refactor the parser and the tests around it, "
                                          "then...")
        assert by_id["s3-0"]["titleSource"] == "auto"
        assert by_id["s3-1"]["title"] == "Chat from 2026-08-04", "nothing typed, so the date"
        assert all(w["title"] for w in report["written"]), "the report carries the names too"

    def test_adopting_twice_finds_nothing_the_second_time(self, machine):
        adopt.adopt([ALPHA])
        assert "s0-0" not in candidate_ids(adopt.state())
        with pytest.raises(ValueError, match="nothing to adopt"):
            adopt.adopt([ALPHA])
        assert len(list((machine / A / ORG_A).glob("local_*.json"))) == 2, "s0-1's and s0-0's"

    def test_the_store_now_sees_the_record(self, machine):
        adopt.adopt([ALPHA])
        seen = store.desktop_records(str(machine), ttl=0)
        assert seen["s0-0"] == (False, "Alpha work")

    def test_the_ledger_names_what_was_written(self, machine):
        report = adopt.adopt([ALPHA, GAMMA])
        ledger = json.loads(adopt.ledger_path().read_text(encoding="utf-8"))
        assert sorted(e["session_id"] for e in ledger) == ["s0-0", "s2-0"]
        assert {e["path"] for e in ledger} == {w["path"] for w in report["written"]}
        assert all(e["record"].startswith("local_") and e["at"].endswith("Z") for e in ledger)

    def test_a_folder_that_was_not_offered_is_refused(self, machine):
        with pytest.raises(ValueError):
            adopt.adopt([r"P:\Nowhere"])

    def test_the_milliseconds_are_the_apps(self):
        assert adopt.to_ms("2026-08-01T00:00:00Z") == 1785542400000
        assert adopt.to_ms("2026-08-01T00:00:00+00:00") == adopt.to_ms("2026-08-01T00:00:00Z")


class TestTheName:
    def test_the_order_is_custom_ai_opening_request_typed_prompt_date(self):
        kinds = {"custom": "Mine", "ai": "Theirs", "last-prompt": "the opening request"}
        assert adopt.title_for(kinds, "typed", "2026-08-03T12:00:00Z") == ("Mine", "user")
        del kinds["custom"]
        assert adopt.title_for(kinds, "typed", None) == ("Theirs", "auto")
        del kinds["ai"]
        assert adopt.title_for(kinds, "typed", None) == ("the opening request", "auto")
        assert adopt.title_for({}, "typed", None) == ("typed", "auto")
        assert adopt.title_for({}, None, "2026-08-03T12:00:00Z") == ("Chat from 2026-08-03", "auto")
        assert adopt.title_for({"custom": "   "}, "  ", None)[0].startswith("Chat from")

    def test_a_prompt_is_cut_on_a_word_boundary_and_marked(self):
        long = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
        cut = adopt.cut(long)
        assert len(cut) <= adopt.TITLE_MAX + 3 and cut.endswith("...")
        assert not cut[:-3].endswith(" ") and " " in cut, cut
        assert cut == "one two three four five six seven eight nine ten eleven..."
        assert adopt.cut("short  and   spaced\n out") == "short and spaced out", "collapsed"
        assert adopt.cut("x" * 70) == "x" * 60 + "...", "no space to break on: a hard cut"

    def test_a_custom_title_is_never_cut(self):
        long = "a title a person typed that runs on past sixty characters without stopping at all"
        assert adopt.title_for({"custom": long}, None, None) == (long, "user")


def nameless(path):
    """A record the first build wrote: the nine fields and the link, no title at all."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("title", None)
    data.pop("titleSource", None)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestRetitle:
    def test_only_the_nameless_ledger_records_are_named(self, machine):
        report = adopt.adopt([ALPHA, GAMMA, DELTA])
        paths = {w["session_id"]: Path(w["path"]) for w in report["written"]}
        nameless(paths["s2-0"])
        nameless(paths["s3-1"])
        mine = json.loads(paths["s0-0"].read_text(encoding="utf-8"))
        mine["title"], mine["titleSource"] = "My own name", "user"
        paths["s0-0"].write_text(json.dumps(mine), encoding="utf-8")
        assert adopt.state()["untitled_adopted"] == 2
        result = adopt.retitle()
        assert sorted(r["session_id"] for r in result["renamed"]) == ["s2-0", "s3-1"]
        assert result["kept"] == 2 and result["missing"] == 0 and result["restart_required"] is True
        assert json.loads(paths["s2-0"].read_text(encoding="utf-8"))["title"] == "raw prompt"
        dated = json.loads(paths["s3-1"].read_text(encoding="utf-8"))["title"]
        assert dated == "Chat from 2026-08-04"
        after = json.loads(paths["s0-0"].read_text(encoding="utf-8"))
        assert (after["title"], after["titleSource"]) == ("My own name", "user"), "a person's"
        assert adopt.state()["untitled_adopted"] == 0
        assert adopt.retitle()["renamed"] == [], "nothing left to name"

    def test_a_missing_record_is_counted_not_invented(self, machine):
        report = adopt.adopt([ALPHA])
        Path(report["written"][0]["path"]).unlink()
        result = adopt.retitle()
        assert result["missing"] == 1 and result["renamed"] == []
        assert not list((machine / A / ORG_A).glob("local_*.json"))[1:], "nothing recreated"

    def test_a_ledger_path_is_resolved_under_another_root(self, tmp_path, monkeypatch, machine):
        """The writer saw `%APPDATA%`; the reader sees the package container. Same pair, same
        file name, a different root."""
        report = adopt.adopt([GAMMA])
        written = Path(report["written"][0]["path"])
        nameless(written)
        other = tmp_path / "container" / "Claude" / "claude-code-sessions"
        target = other / A / ORG_A / written.name
        target.parent.mkdir(parents=True)
        written.replace(target)
        monkeypatch.setattr(appstate, "sessions_roots", lambda: [str(machine), str(other)])
        result = adopt.retitle()
        assert [r["path"] for r in result["renamed"]] == [str(target)]
        assert json.loads(target.read_text(encoding="utf-8"))["title"] == "raw prompt"


class TestTheToggleIsUntouched:
    """The account switch keeps working, and in its off position it moves nothing."""

    def test_current_mode_adds_to_the_signed_in_pair_and_nothing_else(self, machine):
        before = accounts.state()
        adopt.adopt([ALPHA, GAMMA])
        after = accounts.state()
        assert (before["pairs"], before["linked"]) == (after["pairs"], after["linked"])
        assert before["mode"] == after["mode"] == accounts.CURRENT
        counts = lambda st: {p["account"]: p["records"] for r in st["roots"] for p in r["pairs"]}  # noqa: E731
        assert counts(after)[A] == counts(before)[A] + 2
        assert counts(after)[B] == counts(before)[B]
        assert after["chats_visible"] == before["chats_visible"] + 2

    def test_all_mode_writes_through_the_link_and_the_shared_list_grows(self, machine):
        # A holds the most records, so it is the canonical pair and B becomes a link to it.
        record(machine / A / ORG_A, "extra")
        accounts.share_all()
        before = accounts.state()
        assert before["linked"] == 1 and before["intended"] == accounts.ALL
        report = adopt.adopt([ALPHA])
        after = accounts.state()
        assert (before["pairs"], before["linked"]) == (after["pairs"], after["linked"])
        physical = Path(report["physical"])
        assert physical == machine / A / ORG_A
        assert Path(report["written"][0]["path"]).parent == physical
        by_account = {p["account"]: p for r in after["roots"] for p in r["pairs"]}
        assert by_account[A]["records"] == 4, "3 real plus the adopted one"
        assert by_account[B]["records"] == 4, "the link reports the same list"
        assert by_account[B]["link_to"] and Path(by_account[B]["link_to"]) == physical
        assert after["chats_visible"] == before["chats_visible"] + 1
        assert report["note"] is None, "the signed-in pair IS the shared directory"

    def test_all_mode_from_the_linked_side_says_where_the_bytes_live(self, machine):
        # B holds the most records, so the signed-in pair A becomes a link to B's directory.
        record(machine / B / ORG_B, "b1")
        record(machine / B / ORG_B, "b2")
        accounts.share_all()
        report = adopt.adopt([ALPHA])
        assert Path(report["physical"]) == machine / B / ORG_B
        written = Path(report["written"][0]["path"])
        assert written.parent == machine / A / ORG_A, "written through the link"
        assert (machine / B / ORG_B / Path(report["written"][0]["path"]).name).exists()
        assert report["note"] and "sharing is on" in report["note"]

    def test_sharing_on_with_an_unshared_pair_is_refused(self, machine):
        record(machine / B / ORG_B, "b1")
        record(machine / B / ORG_B, "b2")
        accounts._write_marker([{"link": str(machine / B / ORG_B), "to": "elsewhere"}])
        assert accounts.intended_mode() == accounts.ALL
        with pytest.raises(adopt.SharingMismatch):
            adopt.adopt([ALPHA])
        landed = [p for p in (machine / A / ORG_A).glob("local_*.json") if "s0-0" in p.read_text()]
        assert not landed


class TestTheRoutes:
    @pytest.fixture
    def client(self, machine, monkeypatch):
        from fastapi.testclient import TestClient

        from c4x.api.main import api
        monkeypatch.delenv("C4X_NO_WRITES", raising=False)
        return TestClient(api, base_url="http://127.0.0.1:8059")

    def test_get_lists_the_groups(self, client):
        body = client.get("/api/adopt").json()
        assert body["supported"] is True
        assert [g["cwd"] for g in body["groups"]] == [DELTA, GAMMA, ALPHA]
        assert client.get("/api/adopt", params={"include_cli": "true"}).json()["candidates"] == 4

    def test_post_writes_and_asks_for_a_restart(self, client, machine):
        answer = client.post("/api/adopt", json={"cwds": [ALPHA]})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert [w["session_id"] for w in body["written"]] == ["s0-0"] and body["restart_required"]
        assert len(list((machine / A / ORG_A).glob("local_*.json"))) == 2

    def test_nothing_selected_is_400(self, client):
        assert client.post("/api/adopt", json={"cwds": []}).status_code == 400
        assert client.post("/api/adopt", json={}).status_code == 400
        assert client.post("/api/adopt", json={"cwds": [r"P:\Nowhere"]}).status_code == 400

    def test_no_writes_is_403(self, client, monkeypatch):
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        assert client.post("/api/adopt", json={"cwds": [ALPHA]}).status_code == 403

    def test_retitle_names_the_nameless_and_needs_writes(self, client, machine, monkeypatch):
        report = client.post("/api/adopt", json={"cwds": [GAMMA]}).json()
        nameless(Path(report["written"][0]["path"]))
        assert client.get("/api/adopt").json()["untitled_adopted"] == 1
        answer = client.post("/api/adopt/retitle")
        assert answer.status_code == 200, answer.text
        assert [r["session_id"] for r in answer.json()["renamed"]] == ["s2-0"]
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        assert client.post("/api/adopt/retitle").status_code == 403

    def test_review_runs_are_counted_and_named_through_the_routes(self, client, reviewed):
        written = client.post("/api/adopt", json={"cwds": [DELTA]}).json()["written"]
        titles = {w["session_id"]: w["title"] for w in written}
        assert titles["r-1"].startswith("Reviewer - "), "named at adopt time"
        assert client.get("/api/adopt").json()["review_runs_to_name"] == 0
        path = Path(next(w["path"] for w in written if w["session_id"] == "r-1"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["title"] = "You are reviewing another Claude instance's work before it..."
        data["titleSource"] = "auto"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert client.get("/api/adopt").json()["review_runs_to_name"] == 1
        answer = client.post("/api/adopt/retitle").json()
        assert answer["reviews"] == 1 and [r["session_id"] for r in answer["renamed"]] == ["r-1"]

    def test_an_unshared_pair_under_sharing_is_409(self, client, machine):
        record(machine / B / ORG_B, "b1")
        record(machine / B / ORG_B, "b2")
        accounts._write_marker([{"link": str(machine / B / ORG_B), "to": "elsewhere"}])
        answer = client.post("/api/adopt", json={"cwds": [ALPHA]})
        assert answer.status_code == 409 and "sharing is on" in answer.json()["detail"]["error"]


def test_the_module_never_imports_accounts_at_module_level():
    """`c4x/accounts.py` is not touched: not edited, and not imported until a function needs it."""
    source = (ROOT / "c4x" / "adopt.py").read_text(encoding="utf-8")
    top = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("accounts" in line for line in top)
    assert os.path.exists(ROOT / "c4x" / "accounts.py")


L1 = ("The parser now rejects a trailing comma and the three tests that covered it pass again "
      "after the rewrite")
L2 = "12 passed in 0.41s, nothing skipped, and the fixture directory was removed on the way out"
L3 = "A line that only the second Delta chat ever said, long enough to be a snippet all by itself"
L4 = "Both Delta chats said this exact sentence once, so a run quoting only it ties to neither"
PREAMBLE = ("You are reviewing another Claude instance's work before it is allowed to finish its "
            "turn.\n\nLook for a claim wider than its evidence.\n\n--- THE WORK ---\n")


@pytest.fixture
def reviewed(machine, tmp_path):
    """Four one-shot chats under Delta, beside s3-0 (which has typed prompts) and s3-1:

        r-1  quotes two lines of s3-0                        tied to s3-0
        r-2  quotes nothing                                  not a review
        r-3  quotes the one line both s3-0 and s3-1 said     a tie, so tied to neither
        r-4  quotes s3-0 too, past a line with an accent     tied to s3-0
    """
    db = tmp_path / "data" / "context.db"
    home = tmp_path / "home" / ".claude" / "projects" / "slug"
    con = sqlite3.connect(str(db))

    def message(uuid_, sid, ts, role, kind, text):
        con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                         is_sidechain, file_path, line_no)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'f', 1)""",
                    (uuid_, sid, ts, role, kind, text, len(text)))
    message("s3-0-a1", "s3-0", "2026-08-04T12:00:30Z", "assistant", "assistant", L1)
    message("s3-0-o1", "s3-0", "2026-08-04T12:00:40Z", "user", "tool_result", L2)
    message("s3-0-a2", "s3-0", "2026-08-04T12:00:50Z", "assistant", "assistant", L4)
    message("s3-1-a1", "s3-1", "2026-08-04T12:00:30Z", "assistant", "assistant", L3)
    message("s3-1-a2", "s3-1", "2026-08-04T12:00:50Z", "assistant", "assistant", L4)
    prompts = {
        "r-1": PREAMBLE + "CLAUDE SAID: " + L1 + "\n\nOUTPUT WAS: " + L2 + "\n",
        "r-2": "hello, one short line",
        "r-3": PREAMBLE + "CLAUDE SAID: " + L4 + "\n",
        "r-4": (PREAMBLE + "CLAUDE SAID: café " + L1 + "\n\nCLAUDE SAID: " + L1
                + "\n\nOUTPUT WAS: " + L2 + "\n"),
    }
    for i, (sid, prompt) in enumerate(prompts.items()):
        path = home / f"{sid}.jsonl"
        path.write_text('{"type":"summary"}\n', encoding="utf-8")
        ts = f"2026-08-04T12:1{i}:00Z"
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, "slug-2", DELTA, "main", "2.1.263", "claude-desktop", ts, ts, str(path)))
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, output_tokens,
                         file_path, line_no)
                       VALUES (?, ?, ?, 'claude-sonnet-5', ?, 1, 'f', 0)""",
                    (f"{sid}-t0", sid, ts, f"req-{sid}"))
        message(f"{sid}-p", sid, ts, "user", "typed", prompt)
        message(f"{sid}-a", sid, ts.replace(":00Z", ":30Z"), "assistant", "assistant",
                "APPROVED\nchecked the claim against the output")
    con.commit()
    con.close()
    forget_cached_rows()
    reviews.forget()
    yield machine
    reviews.forget()


class TestReviewRuns:
    def test_snippets_are_ascii_line_tails_with_the_label_dropped_newest_first(self):
        prompt = ("short\nCLAUDE SAID: " + L1 + "\nOUTPUT WAS: café " + L2 + "\nUSER: " + L3
                  + "\n")
        assert reviews.snippets(prompt) == [L3[-120:], L1[-120:]], "accented line skipped"
        assert reviews.snippets("") == [] and reviews.snippets(None) == []
        long = "x" * 300
        assert reviews.snippets("A LABEL: " + long) == [long[-120:]]
        assert reviews.snippets("Not a label: " + long) == [("Not a label: " + long)[-120:]]
        assert len(reviews.snippets("\n".join([L1] * 20))) == reviews.WANT

    def test_a_run_that_quotes_a_chat_is_tied_to_it_and_nothing_else_is(self, reviewed):
        rows = adopt._sessions()
        ids = [r["session_id"] for r in rows]
        assert reviews.one_shots(ids) == {"r-1", "r-2", "r-3", "r-4"}, "s3-0 typed three times"
        assert reviews.reviewed_by(rows, ids) == {"r-1": "s3-0", "r-4": "s3-0"}
        assert reviews.reviewed_by(rows, ["s3-0", "r-2", "r-3"]) == {}, "only what was asked"

    def test_a_found_tie_is_kept_and_a_miss_is_retried_only_when_its_pool_changes(
            self, reviewed, monkeypatch):
        """Every hook run changes the store; a tie, once found, must not be bought again, and a
        miss is worth asking again only when a session joins the run's pool."""
        rows = adopt._sessions()
        ids = [r["session_id"] for r in rows]
        assert reviews.reviewed_by(rows, ids) == {"r-1": "s3-0", "r-4": "s3-0"}
        asked: list = []

        def counting(prompt, pool):
            asked.append(len(pool))
            return None
        monkeypatch.setattr(reviews, "_tie", counting)
        assert reviews.reviewed_by(rows, ids) == {"r-1": "s3-0", "r-4": "s3-0"}
        assert asked == [], "the pools did not change: nothing is asked again"
        joined = rows + [{"session_id": "s3-9", "cwd": DELTA, "first_ts": "2026-08-04T12:00:00Z",
                          "last_ts": "2026-08-04T13:00:00Z"}]
        assert reviews.reviewed_by(joined, ids) == {"r-1": "s3-0", "r-4": "s3-0"}
        assert asked == [3, 3], "the two misses, each against a pool of three now"

    def test_the_pool_is_the_folder_s_sessions_alive_when_the_run_started(self, reviewed):
        rows = adopt._sessions()
        r1 = next(r for r in rows if r["session_id"] == "r-1")
        # A copy of s3-0 that went quiet two hours before the run is not a candidate; one quiet
        # for half an hour is; one begun after the run is not.
        stale = dict(next(r for r in rows if r["session_id"] == "s3-0"))
        stale.update(session_id="s3-stale", first_ts="2026-08-04T08:00:00Z",
                     last_ts="2026-08-04T10:00:00Z")
        recent = dict(stale, session_id="s3-recent", last_ts="2026-08-04T11:45:00Z")
        later = dict(stale, session_id="s3-later", first_ts="2026-08-04T12:30:00Z",
                     last_ts="2026-08-04T12:40:00Z")
        reviews.forget()
        seen: dict = {}
        real = reviews._tie

        def spy(prompt, pool):
            seen["pool"] = sorted(pool)
            return real(prompt, pool)
        try:
            reviews._tie = spy
            assert reviews.reviewed_by(rows + [stale, recent, later], [r1["session_id"]]) == {
                "r-1": "s3-0"}
        finally:
            reviews._tie = real
        assert seen["pool"] == ["s3-0", "s3-1", "s3-recent"], seen

    def test_the_run_is_offered_under_the_name_of_the_chat_it_read(self, reviewed):
        delta = next(g for g in adopt.state()["groups"] if g["cwd"] == DELTA)
        titles = {s["session_id"]: s["title"] for s in delta["sessions"]}
        assert titles["r-1"].startswith("Reviewer - Please refactor the parser"), titles["r-1"]
        assert titles["r-1"] == titles["r-4"]
        assert len(titles["r-1"]) <= adopt.TITLE_MAX and titles["r-1"].endswith("...")
        assert titles["r-2"] == "hello, one short line"
        assert titles["r-3"].startswith("You are reviewing another Claude"), titles["r-3"]

    def test_the_form_and_who_wins(self):
        assert adopt.review_title("T01") == "Reviewer - T01"
        long = adopt.review_title("a name " * 20)
        assert len(long) <= adopt.TITLE_MAX and long.startswith("Reviewer - a name")
        assert long.endswith("...")
        assert adopt.title_for({"custom": "My run"}, "p", None, reviewed="T01") == (
            "My run", "user")
        assert adopt.title_for({"ai": "Its own"}, "p", None, reviewed="T01") == (
            "Its own", "auto")
        assert adopt.title_for({"last-prompt": "the prompt"}, "p", None, reviewed="T01") == (
            "Reviewer - T01", "auto")
        assert adopt.title_for({"last-prompt": "the prompt"}, "p", None, reviewed=" ") == (
            "the prompt", "auto")

    def test_retitle_renames_an_auto_named_run_and_leaves_a_persons_name(self, reviewed):
        report = adopt.adopt([DELTA])
        paths = {w["session_id"]: Path(w["path"]) for w in report["written"]}

        def rename(sid, title, source):
            data = json.loads(paths[sid].read_text(encoding="utf-8"))
            data["title"], data["titleSource"] = title, source
            paths[sid].write_text(json.dumps(data), encoding="utf-8")
        # What a first build wrote for a run: its own opening line, as an automatic name.
        rename("r-1", "You are reviewing another Claude instance's work before it...", "auto")
        rename("r-4", "Mine", "user")
        assert adopt.state()["review_runs_to_name"] == 1
        result = adopt.retitle()
        assert [r["session_id"] for r in result["renamed"]] == ["r-1"] and result["reviews"] == 1
        assert result["renamed"][0]["title"].startswith("Reviewer - Please refactor")
        assert result["restart_required"] is True
        assert json.loads(paths["r-1"].read_text(encoding="utf-8"))["titleSource"] == "auto"
        assert json.loads(paths["r-4"].read_text(encoding="utf-8"))["title"] == "Mine"
        assert adopt.state()["review_runs_to_name"] == 0
        again = adopt.retitle()
        assert again["renamed"] == [] and again["reviews"] == 0

    def test_a_nameless_run_record_is_named_after_the_chat_too(self, reviewed):
        report = adopt.adopt([DELTA])
        paths = {w["session_id"]: Path(w["path"]) for w in report["written"]}
        nameless(paths["r-1"])
        assert adopt.state()["untitled_adopted"] == 1
        result = adopt.retitle()
        assert result["renamed"][0]["title"].startswith("Reviewer - ") and result["reviews"] == 0
        assert result["renamed"][0]["session_id"] == "r-1"

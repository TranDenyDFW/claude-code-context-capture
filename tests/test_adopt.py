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

    def test_review_run_records_are_taken_back_through_the_route(self, client, reviewed,
                                                                 monkeypatch):
        assert client.get("/api/adopt").json()["review_runs"] == 3
        written_by_hand(reviewed, "r-1")
        first = client.get("/api/adopt").json()
        # r-1 has a record now, so it is out before the run filter ever sees it: two runs left.
        assert first["review_records"] == 1 and first["review_runs"] == 2
        answer = client.post("/api/adopt/unadopt-reviews")
        assert answer.status_code == 200, answer.text
        assert [r["session_id"] for r in answer.json()["removed"]] == ["r-1"]
        assert client.get("/api/adopt").json()["review_records"] == 0
        monkeypatch.setenv("C4X_NO_WRITES", "1")
        assert client.post("/api/adopt/unadopt-reviews").status_code == 403

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


@pytest.fixture
def reviewed(machine, tmp_path):
    """Four more desktop sessions under Delta. Harvest has tied two of them, r-1 and r-4, to s3-0
    as its review runs (`review_links`); r-3 is a run it could not place (a NULL head, an orphan);
    r-2 is an ordinary one-shot. The rule itself is harvest's and is tested there; what is tested
    here is what Adopt does with the table."""
    db = tmp_path / "data" / "context.db"
    home = tmp_path / "home" / ".claude" / "projects" / "slug"
    con = sqlite3.connect(str(db))
    for i, sid in enumerate(("r-1", "r-2", "r-3", "r-4")):
        path = home / f"{sid}.jsonl"
        path.write_text('{"type":"summary"}\n', encoding="utf-8")
        ts = f"2026-08-04T12:1{i}:00Z"
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, "slug-2", DELTA, "main", "2.1.263", "claude-desktop", ts, ts, str(path)))
        con.execute("""INSERT INTO turns (uuid, session_id, ts, model, request_id, output_tokens,
                         file_path, line_no)
                       VALUES (?, ?, ?, 'claude-sonnet-5', ?, 1, 'f', 0)""",
                    (f"{sid}-t0", sid, ts, f"req-{sid}"))
        con.execute("""INSERT INTO messages (uuid, session_id, ts, role, type, text, chars,
                         is_sidechain, file_path, line_no)
                       VALUES (?, ?, ?, 'user', 'typed', ?, ?, 0, 'f', 1)""",
                    (f"{sid}-p", sid, ts, "You are reviewing another Claude instance's work", 48))
    for run in ("r-1", "r-4"):
        con.execute("""INSERT INTO review_links (session_id, head_id, hits, snippets, verdict,
                         method, linked_at)
                       VALUES (?, 's3-0', 2, 3, 'APPROVED', 'test', '2026-08-05T00:00:00Z')""",
                    (run,))
    con.execute("""INSERT INTO review_links (session_id, head_id, hits, snippets, verdict,
                     method, linked_at)
                   VALUES ('r-3', NULL, 2, 3, 'PROBLEMS', 'test', '2026-08-05T00:00:00Z')""")
    con.commit()
    con.close()
    forget_cached_rows()
    yield machine
    forget_cached_rows()


def written_by_hand(root, sid, title="You are reviewing another Claude instance's work..."):
    """A record a first build wrote for a run, and its ledger entry: the laptop's state."""
    path = record(root / A / ORG_A, sid, title=title)
    adopt._append_ledger([{"session_id": sid, "record": f"local_{sid}", "path": str(path),
                           "at": "2026-08-05T00:00:00Z"}])
    return path


class TestReviewRuns:
    def test_a_run_is_never_offered_and_is_counted(self, reviewed):
        state = adopt.state()
        delta = next(g for g in state["groups"] if g["cwd"] == DELTA)
        offered = {s["session_id"] for s in delta["sessions"]}
        assert "r-2" in offered, "an ordinary one-shot is still a chat"
        assert not ({"r-1", "r-4"} & offered), "a review run is not"
        assert "r-3" not in offered, "nor is a run the store cannot place"
        assert state["review_runs"] == 3 and state["review_records"] == 0
        written = {w["session_id"] for w in adopt.adopt([DELTA])["written"]}
        assert written == {"s3-0", "s3-1", "r-2"}

    def test_records_a_first_build_wrote_for_runs_are_counted_and_taken_back(self, reviewed):
        p1 = written_by_hand(reviewed, "r-1")
        p3 = written_by_hand(reviewed, "r-3")
        p4 = written_by_hand(reviewed, "r-4", title="Mine")
        assert adopt.state()["review_records"] == 3
        result = adopt.unadopt_reviews()
        assert sorted(r["session_id"] for r in result["removed"]) == ["r-1", "r-3", "r-4"]
        reviewed_of = {r["session_id"]: r["reviewed"] for r in result["removed"]}
        assert reviewed_of == {"r-1": "s3-0", "r-3": None, "r-4": "s3-0"}, (
            "an orphan's record goes too, and it names no chat")
        assert result["restart_required"] is True and result["missing"] == 0
        assert not p1.exists() and not p3.exists() and not p4.exists(), (
            "a person's title does not keep a run's record")
        stamped = {e["session_id"]: e.get("removed_at") for e in adopt._load_ledger()}
        assert stamped["r-1"] and stamped["r-3"] and stamped["r-4"]
        assert adopt.state()["review_records"] == 0
        assert adopt._ledger_records() == [], "a taken-back entry is out of every later count"
        again = adopt.unadopt_reviews()
        assert again["removed"] == [] and again["restart_required"] is False

    def test_a_real_chat_s_record_is_kept_and_a_missing_file_is_counted(self, reviewed):
        report = adopt.adopt([GAMMA])
        written_by_hand(reviewed, "r-1").unlink()
        result = adopt.unadopt_reviews()
        assert result["removed"] == [] and result["missing"] == 1 and result["kept"] == 1
        assert Path(report["written"][0]["path"]).exists()
        assert adopt.state()["untitled_adopted"] == 0
        assert adopt.retitle()["renamed"] == [], "the nameless rule is untouched by any of this"

    def test_the_reader_answers_from_the_table(self, reviewed):
        assert reviews.reviewed_by() == {"r-1": "s3-0", "r-3": None, "r-4": "s3-0"}
        assert reviews.reviewed_by(["r-1", "r-2", "r-3"]) == {"r-1": "s3-0", "r-3": None}
        assert reviews.runs_of("s3-0") == ["r-4", "r-1"], "newest first"
        assert reviews.runs_of("s3-1") == []


class TestTheSweep:
    """`adopt.sweep_reviews`: the drawer's "Remove them" run by the server at startup, plus the
    restart, each guard fed the input that trips it."""

    @pytest.fixture
    def swept(self, reviewed, monkeypatch):
        monkeypatch.delenv("C4X_NO_WRITES", raising=False)
        monkeypatch.delenv("C4X_NO_REVIEW_SWEEP", raising=False)
        written_by_hand(reviewed, "r-1")
        written_by_hand(reviewed, "r-3")
        return reviewed

    def test_records_are_taken_back_and_claude_is_restarted_once(self, swept):
        restarts = []

        def restart():
            restarts.append(1)
            return {"restarted": True, "killed": 2, "launch": ["explorer.exe", "shell:x"],
                    "why": "relaunched"}
        report = adopt.sweep_reviews(restart=restart, running=lambda: True, now=1000.0,
                                     log=lambda m: None)
        assert report["removed"] == 2 and report["restarted"] is True and restarts == [1]
        assert report["restart"]["killed"] == 2 and "Claude restarted" in report["why"]
        assert adopt.last_sweep() == report, "the stamp is the report"
        assert adopt.sweep_stamp_path() == Path(store.DB_PATH).parent / "raw" / ".review-sweep"
        assert adopt.state()["review_records"] == 0

    def test_nothing_to_remove_means_no_restart(self, reviewed, monkeypatch):
        monkeypatch.delenv("C4X_NO_WRITES", raising=False)

        def never():
            raise AssertionError("restarted with nothing removed")
        report = adopt.sweep_reviews(restart=never, running=lambda: True, now=1000.0,
                                     log=lambda m: None)
        assert report["removed"] == 0 and report["restarted"] is False
        assert report["why"] == "nothing to remove; no restart"
        assert adopt.last_sweep()["restarted_epoch"] is None

    def test_a_second_restart_within_the_cooldown_is_refused(self, swept, reviewed):
        ok = lambda: {"restarted": True, "killed": 1, "launch": ["x"], "why": "relaunched"}  # noqa: E731
        first = adopt.sweep_reviews(restart=ok, running=lambda: True, now=1000.0,
                                    log=lambda m: None)
        assert first["restarted"] is True
        written_by_hand(reviewed, "r-4")
        again = []
        second = adopt.sweep_reviews(restart=lambda: again.append(1) or ok(), running=lambda: True,
                                     now=1000.0 + adopt.SWEEP_COOLDOWN - 1, log=lambda m: None)
        assert again == [] and second["restarted"] is False and "not again" in second["why"]
        assert adopt.state()["review_records"] == 1, "the record waits for the next sweep"
        assert adopt.last_sweep()["epoch"] == 1000.0, "a refused sweep writes no stamp"
        third = adopt.sweep_reviews(restart=lambda: again.append(1) or ok(), running=lambda: True,
                                    now=1000.0 + adopt.SWEEP_COOLDOWN + 1, log=lambda m: None)
        assert again == [1] and third["removed"] == 1 and third["restarted"] is True

    def test_a_restart_that_did_not_bring_the_app_back_is_said_and_starts_no_cooldown(self, swept):
        stayed_down = {"restarted": False, "killed": 1, "launch": ["x"],
                       "why": "the app did not come back within 20 s"}
        report = adopt.sweep_reviews(restart=lambda: stayed_down, running=lambda: True,
                                     now=1000.0, log=lambda m: None)
        assert report["removed"] == 2 and report["restarted"] is False
        assert "was not restarted: the app did not come back" in report["why"]
        assert report["restarted_epoch"] is None

    @pytest.mark.parametrize("env,running,expect", [
        ({"C4X_NO_WRITES": "1"}, True, "writes are off"),
        ({"C4X_NO_REVIEW_SWEEP": "1"}, True, "off (--no-review-sweep)"),
        ({}, False, "Claude is not running"),
    ])
    def test_each_refusal_removes_nothing_and_writes_no_stamp(self, swept, monkeypatch, env,
                                                              running, expect):
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        def never():
            raise AssertionError("restarted under a refusal")
        report = adopt.sweep_reviews(restart=never, running=lambda: running, now=1000.0,
                                     log=lambda m: None)
        assert expect in report["why"] and report["removed"] == 0
        assert adopt.last_sweep() is None
        assert adopt.state()["review_records"] == 2, "nothing was removed"

    def test_enabled_reads_the_two_switches(self):
        assert adopt.sweep_enabled(env={}) is True
        assert adopt.sweep_enabled(env={"C4X_NO_WRITES": "1"}) is False
        assert adopt.sweep_enabled(env={"C4X_NO_REVIEW_SWEEP": "1"}) is False


class TestTheServerRoutes:
    @pytest.fixture
    def client(self, machine, monkeypatch):
        from fastapi.testclient import TestClient

        from c4x.api.main import api
        monkeypatch.delenv("C4X_NO_WRITES", raising=False)
        monkeypatch.delenv("C4X_NO_REVIEW_SWEEP", raising=False)
        return TestClient(api, base_url="http://127.0.0.1:8059")

    def test_the_sweep_route_reports_the_switch_and_the_last_stamp(self, client, monkeypatch):
        assert client.get("/api/adopt/sweep").json() == {"enabled": True, "last": None}
        adopt.sweep_stamp_path().parent.mkdir(parents=True, exist_ok=True)
        adopt.sweep_stamp_path().write_text(json.dumps({"at": "2026-09-14T00:00:00Z", "removed": 4,
                                                        "restarted": True, "why": "x"}),
                                            encoding="utf-8")
        body = client.get("/api/adopt/sweep").json()
        assert body["last"]["removed"] == 4 and body["last"]["restarted"] is True
        monkeypatch.setenv("C4X_NO_REVIEW_SWEEP", "1")
        assert client.get("/api/adopt/sweep").json()["enabled"] is False

    def test_stop_and_restart_reach_the_server_module_and_nothing_else(self, client, monkeypatch):
        from c4x import server
        calls = []
        answer = {"restarting": True, "pid": 1, "argv": ["x"]}
        monkeypatch.setattr(server, "hardened_shutdown",
                            lambda reason, spare=(): calls.append(("stop", reason)))
        monkeypatch.setattr(server, "restart_server",
                            lambda reason: calls.append(("restart", reason)) or answer)
        assert client.post("/api/server/stop", json={}).json() == {"stopped": True}
        assert client.post("/api/server/restart", json={}).json() == answer
        assert calls == [("stop", "Stop C4X button"), ("restart", "Restart C4X button")]

    def test_a_foreign_page_cannot_stop_or_restart_this_server(self, client, monkeypatch):
        from c4x import server

        def never(*a, **k):
            raise AssertionError("a cross-origin request reached the shutdown")
        monkeypatch.setattr(server, "hardened_shutdown", never)
        monkeypatch.setattr(server, "restart_server", never)
        for path in ("/api/server/stop", "/api/server/restart"):
            answer = client.post(path, json={}, headers={"Origin": "https://evil.example"})
            assert answer.status_code == 403, answer.text
            rebound = client.post(path, json={}, headers={"Host": "attacker.example:8059"})
            assert rebound.status_code == 403, rebound.text

    def test_the_health_answer_names_the_process(self, client):
        assert client.get("/api/health").json()["pid"] == os.getpid()

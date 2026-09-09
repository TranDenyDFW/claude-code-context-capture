"""Export and import as a byte-for-byte mirror, landed on the CURRENT user.

`tests/test_appstate.py` gates the four layers in isolation. This gates them as the user meets
them: a whole project exported from one machine and imported onto another, where the working
directory is different, the account is different, and nothing about the source machine's paths may
survive.

The two properties, and they pull against each other, which is why both are here:

    the CONTENT is identical, byte for byte, and provably so
    the ADDRESSING is this machine's, and provably not the exporter's

A test that only checks the first passes on an import that recreated `C:\\Users\\them`. A test that
only checks the second passes on an import that wrote nothing.

Every root is monkeypatched onto a temporary directory, layered over the autouse isolation in
`conftest.py`, so nothing here can read or write the real `~/.claude`, `~/.claude.json` or
`%APPDATA%\\Claude`.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import projects  # noqa: E402
from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

SOURCE = r"P:\Alpha"
DEST = r"D:\Work\Alpha"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
FOREIGN_ACCOUNT = "99999999-9999-4999-8999-999999999999"
DESKTOP_FILE = "local_aaaa1111-2222-4333-8444-555566667777.json"


@pytest.fixture
def store_at(tmp_path, monkeypatch):
    """A fresh store per test, with `c4x.store` pointed at it. Three sessions have cwd P:\\Alpha."""
    from c4x import store
    path = build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A whole fake machine, holding what a real one holds for this project.

    The desktop record is filed under a DIFFERENT account from the one this machine is signed in
    to, which is the case that decides whether an import is visible in the app: the two directory
    levels are the machine's account and organisation, not the project's, so an import that
    reproduced them would file the record where the destination app never looks.
    """
    from c4x import appstate
    home = tmp_path / "fakehome"
    claude = home / ".claude"
    config = home / ".claude.json"
    sessions = tmp_path / "fakeappdata" / "Claude" / "claude-code-sessions"
    (claude / "tasks").mkdir(parents=True)
    sessions.mkdir(parents=True)
    monkeypatch.setattr(appstate, "CLAUDE_DIR", claude)
    monkeypatch.setattr(appstate, "CONFIG_PATH", config)
    monkeypatch.setattr(appstate, "sessions_root", lambda: str(sessions))

    base = claude / "projects" / appstate.slug_for(SOURCE)
    (base / "memory").mkdir(parents=True)
    for n in range(3):
        sid = f"s0-{n}"
        (base / f"{sid}.jsonl").write_bytes(f"line for {sid}\n".encode() * 3)
        (base / sid / "subagents").mkdir(parents=True)
        (base / sid / "subagents" / "one.jsonl").write_bytes(f"sub {sid}".encode())
    (base / "memory" / "notes.md").write_bytes(b"# memory\n")
    (claude / "tasks" / "s0-0").mkdir(parents=True)
    (claude / "tasks" / "s0-0" / "task.json").write_bytes(b'{"t":1}')
    config.write_text(json.dumps({
        "oauthAccount": {"leave": "alone"},
        "projects": {SOURCE: {"hasTrustDialogAccepted": True},
                     r"P:\Beta": {"hasTrustDialogAccepted": False}}}), encoding="utf-8")

    (sessions.parent / "config.json").write_text(
        json.dumps({"lastKnownAccountUuid": ACCOUNT}), encoding="utf-8")
    (sessions.parent / "plan-usage-history.json").write_text(
        json.dumps({"samples": [{"t": 1, "org": ORG}]}), encoding="utf-8")
    foreign = sessions / FOREIGN_ACCOUNT / ORG
    foreign.mkdir(parents=True)
    (foreign / DESKTOP_FILE).write_text(json.dumps({
        "cliSessionId": "s0-0", "sessionId": DESKTOP_FILE[: -len(".json")],
        "cwd": SOURCE, "originCwd": SOURCE, "isArchived": False, "title": "the chat"}),
        encoding="utf-8")
    return SimpleNamespace(claude=claude, config=config, sessions=sessions, base=base)


def wipe(machine):
    """Remove everything the import is supposed to put back.

    Without this a passing test proves the FIXTURE built the files, which is the shape of a check
    that cannot fail.
    """
    shutil.rmtree(machine.claude / "projects")
    shutil.rmtree(machine.claude / "tasks")
    (machine.claude / "tasks").mkdir(parents=True)
    machine.config.write_text(json.dumps({"oauthAccount": {"leave": "alone"}, "projects": {}}),
                              encoding="utf-8")
    shutil.rmtree(machine.sessions / FOREIGN_ACCOUNT)


def read_store(path, sql):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(sql)]
    finally:
        con.close()


class TestTheExportCarriesEverything:
    def test_the_files_and_not_only_the_rows(self, store_at, machine, tmp_path):
        """The defect the whole change exists to end.

        Measured on a second machine before this: an import touched exactly ONE file,
        `context.db`, leaving `~/.claude/projects`, `~/.claude.json` and the desktop records
        byte-identical, so the project appeared in c4x and nowhere else."""
        manifest = projects.export(SOURCE, tmp_path / "e.db")
        state = manifest["app_state"]
        assert state["by_kind"]["transcript"] == 6, "three transcripts and three subagent files"
        assert state["by_kind"]["memory"] == 1
        assert state["by_kind"]["tasks"] == 1
        assert state["by_kind"]["config"] == 1
        assert state["by_kind"]["desktop"] == 1
        assert manifest["cwds"] == [SOURCE], "the export carries directories, not the label"

    def test_the_per_session_directory_is_in_there(self, store_at, machine, tmp_path):
        """A capture narrowed to `<sid>.jsonl` left 326 MB of these unbacked on the real store."""
        projects.export(SOURCE, tmp_path / "e.db")
        carried = {r["relpath"] for r in projects.app_state_rows(tmp_path / "e.db")}
        assert "s0-0/subagents/one.jsonl" in carried

    def test_a_rows_only_export_still_verifies(self, store_at, machine, tmp_path):
        """`delete` takes its backup this way. An export that cannot verify itself is refused by
        its own writer, so the empty table has to exist rather than be special-cased."""
        manifest = projects.export(SOURCE, tmp_path / "rows.db", app_state=False)
        assert manifest["app_state"]["files"] == 0
        ok, problems = projects.verify(tmp_path / "rows.db")
        assert ok, problems


class TestTheRoundTrip:
    def test_onto_a_wiped_machine_it_is_a_mirror(self, store_at, machine, tmp_path):
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        report = projects.import_(export_path)
        assert report["mirror"]["ok"], report["mirror"]
        assert (machine.base / "s0-0.jsonl").exists()
        assert (machine.base / "s0-0" / "subagents" / "one.jsonl").exists()
        assert (machine.base / "memory" / "notes.md").exists()
        assert (machine.claude / "tasks" / "s0-0" / "task.json").exists()

    def test_the_carried_files_are_not_loaded_into_the_store_as_rows(self, store_at, machine,
                                                                     tmp_path):
        """FOUND BY MUTATION TESTING. Removing the skip in the row-copy loop left every test green.

        It is not harmless: the loop's own fallback then reports the table as columns this build
        does not have, which puts a line on the page saying data was not loaded when nothing was
        missing, and it is the branch that would insert whole transcripts as BLOBs into any store
        that did have such a table."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        report = projects.import_(export_path)
        assert projects.APP_STATE_TABLE not in report["dropped_columns"]
        assert projects.APP_STATE_TABLE not in report["inserted"]

    def test_the_bytes_are_identical_and_not_merely_present(self, store_at, machine, tmp_path):
        """Presence is what a count proves. This is what "mirror" means."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        before = {p.relative_to(machine.claude).as_posix(): p.read_bytes()
                  for p in machine.claude.rglob("*") if p.is_file()}
        wipe(machine)
        projects.import_(export_path)
        after = {p.relative_to(machine.claude).as_posix(): p.read_bytes()
                 for p in machine.claude.rglob("*") if p.is_file()}
        assert after == before

    def test_importing_twice_changes_nothing_the_second_time(self, store_at, machine, tmp_path):
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path)
        first = {p.relative_to(machine.claude).as_posix(): p.read_bytes()
                 for p in machine.claude.rglob("*") if p.is_file()}
        second = projects.import_(export_path)
        again = {p.relative_to(machine.claude).as_posix(): p.read_bytes()
                 for p in machine.claude.rglob("*") if p.is_file()}
        assert again == first
        assert second["mirror"]["ok"]


class TestTheRebase:
    """The requirement: the destination is the user's choice, and the roots are the CURRENT
    user's."""

    def test_the_files_land_under_the_destination_slug(self, store_at, machine, tmp_path):
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        landed = appstate.project_dir(DEST)
        assert landed.name == "D--Work-Alpha"
        assert (landed / "s0-0.jsonl").exists()
        assert (landed / "s0-0" / "subagents" / "one.jsonl").exists()
        assert not (machine.claude / "projects" / appstate.slug_for(SOURCE)).exists(), \
            "the import recreated the SOURCE machine's directory"

    def test_nothing_created_carries_the_source_addressing(self, store_at, machine, tmp_path):
        """The one assertion the requirement reduces to.

        Transcript CONTENT still records the directory it ran in, on every line. That is stated
        rather than hidden: rewriting it would be surgery on the conversation and would break the
        byte-identical guarantee the mirror rests on. This is about where things LAND."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        created = [str(p) for root in (machine.claude, machine.sessions)
                   for p in root.rglob("*") if p.is_file()]
        assert created
        wrong = appstate.slug_for(SOURCE)
        assert not [p for p in created if wrong in p or FOREIGN_ACCOUNT in p]

    def test_the_config_key_moves_and_nothing_else_is_touched(self, store_at, machine, tmp_path):
        """`hasTrustDialogAccepted` is why an imported project otherwise asks to be trusted again.

        The file also holds this machine's credentials and every other project's settings, so the
        second half of this is not decoration."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        config = json.loads(machine.config.read_text(encoding="utf-8"))
        assert config["projects"][DEST]["hasTrustDialogAccepted"] is True
        assert SOURCE not in config["projects"]
        assert config["oauthAccount"] == {"leave": "alone"}

    def test_the_desktop_record_lands_under_THIS_account_and_names_the_destination(
            self, store_at, machine, tmp_path):
        """What makes the import VISIBLE IN THE DESKTOP APP.

        The account and organisation directories are the machine's: one pair on the real machine
        holds 171 records across 57 working directories, so nothing about a project produces them.
        Copying the source pair files the record where the destination app never looks."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        landed = machine.sessions / ACCOUNT / ORG / DESKTOP_FILE
        assert landed.exists(), "the record is not where this machine's app reads records"
        record = json.loads(landed.read_text(encoding="utf-8"))
        assert record["cwd"] == DEST
        assert record["originCwd"] == DEST
        assert record["cliSessionId"] == "s0-0", "the record no longer points at its session"
        assert record["title"] == "the chat", "the rest of the record was not carried unchanged"
        assert not (machine.sessions / FOREIGN_ACCOUNT).exists()

    def test_a_second_account_on_the_destination_does_not_capture_the_record(
            self, store_at, machine, tmp_path):
        """FOUND BY MUTATION TESTING, and the realistic case rather than the tidy one.

        `wipe` removes the source machine's account directory, so at import time there were no
        records on disk at all and the account filter in `desktop_pair` never ran: removing it left
        every test green. A real destination has records already, sometimes under a second account
        that is signed out. Here one is left in place, and newer than anything else, so choosing by
        recency alone files the import where the app never looks.
        """
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        shutil.rmtree(machine.claude / "projects")
        # The source machine's record STAYS, and is the newest thing on this disk.
        stale = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        stale.touch()
        projects.import_(export_path, into=DEST)
        assert (machine.sessions / ACCOUNT / ORG / DESKTOP_FILE).exists(), \
            "the record went to the newest account on disk instead of the signed-in one"

    def test_the_rows_move_with_the_files(self, store_at, machine, tmp_path):
        """c4x and the desktop app have to agree on one path, or the page names a directory that
        does not exist on this machine."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        expected = str(appstate.project_dir(DEST) / "s0-0.jsonl")
        cwd_sql = "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'"
        assert read_store(store_at, cwd_sql) == [DEST]
        assert read_store(
            store_at,
            "SELECT DISTINCT cwd FROM hook_events WHERE session_id LIKE 's0-%'") == [DEST]
        assert read_store(
            store_at,
            "SELECT transcript_path FROM sessions WHERE session_id = 's0-0'") == [expected]
        offset_sql = "SELECT path FROM files WHERE path LIKE '%s0-0%'"
        assert read_store(store_at, offset_sql) == [expected], \
            "the harvest offset stayed on the old path, so the next harvest re-reads from zero"

    def test_another_project_in_the_store_is_left_alone(self, store_at, machine, tmp_path):
        """The rebase is scoped to the sessions the export carries, and nothing else."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        assert read_store(store_at,
                          "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's1-%'") == \
            [r"P:\Beta"]

    def test_with_no_destination_it_lands_where_it_came_from(self, store_at, machine, tmp_path):
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path)
        assert (machine.base / "s0-0.jsonl").exists()
        config = json.loads(machine.config.read_text(encoding="utf-8"))
        assert SOURCE in config["projects"]
        assert read_store(store_at,
                          "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'") == \
            [SOURCE]

    def test_a_dry_run_names_every_destination_and_writes_nothing(self, store_at, machine,
                                                                  tmp_path):
        """The only cheap way to catch a wrong destination BEFORE it lands."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        report = projects.import_(export_path, into=DEST, dry_run=True)
        assert report["dry_run"] is True
        # Six transcripts, one memory file, one task, one config entry, one desktop record.
        assert len(report["app_state"]["written"]) == 10
        assert all("D--Work-Alpha" in e["path"] or ".claude.json" in e["path"]
                   or "claude-code-sessions" in e["path"] or "tasks" in e["path"]
                   for e in report["app_state"]["written"])
        assert not (machine.claude / "projects").exists()
        assert read_store(store_at,
                          "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'") == \
            [SOURCE], "a dry run changed the store"


class TestTheProofCanSayNo:
    """A check that cannot fail is worse than no check: it certifies whatever it is pointed at."""

    def test_a_tampered_file_is_reported(self, store_at, machine, tmp_path):
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        (appstate.project_dir(DEST) / "s0-0.jsonl").write_bytes(b"tampered")
        result = projects.verify_mirror(export_path, into=DEST)
        assert not result["ok"]
        assert [d["relpath"] for d in result["differs"]] == ["s0-0.jsonl"]

    def test_a_deleted_file_is_reported(self, store_at, machine, tmp_path):
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        (appstate.project_dir(DEST) / "memory" / "notes.md").unlink()
        result = projects.verify_mirror(export_path, into=DEST)
        assert not result["ok"]
        assert [m["relpath"] for m in result["missing"]] == ["memory/notes.md"]

    def test_a_truncated_export_does_not_verify(self, store_at, machine, tmp_path):
        """The digest covers the hashes. This covers the bytes those hashes describe."""
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        con = sqlite3.connect(str(export_path))
        con.execute(f"UPDATE {projects.APP_STATE_TABLE} SET blob = ? WHERE kind = 'transcript'",
                    (sqlite3.Binary(b"short"),))
        con.commit()
        con.close()
        ok, problems = projects.verify(export_path)
        assert not ok
        assert any("bytes hash to" in p for p in problems), problems

    def test_an_export_carrying_an_absolute_path_writes_nothing_outside_its_root(
            self, store_at, machine, tmp_path):
        """A fabricated export is untrusted input. `verify()` proves the file is the file that was
        written, and nothing whatsoever about where its rows would land."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        evil = str(tmp_path / "escaped.txt")
        con = sqlite3.connect(str(export_path))
        con.execute(f"UPDATE {projects.APP_STATE_TABLE} SET path = ? "
                    "WHERE rowid = (SELECT MIN(rowid) FROM "
                    f"{projects.APP_STATE_TABLE} WHERE kind = 'transcript')", (evil,))
        con.commit()
        con.close()
        report = appstate.restore(projects.app_state_rows(export_path), {SOURCE: DEST})
        assert any("absolute" in r["why"] for r in report["refused"]), report["refused"]
        assert not Path(evil).exists(), "an export wrote outside every root it was given"

    def test_a_non_bytes_blob_is_refused_before_anything_lands(self, store_at, machine, tmp_path):
        """This one used to VERIFY CLEAN and then raise after the rows were committed."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        rows = projects.app_state_rows(export_path)
        rows[0]["blob"] = "text, not bytes"
        with pytest.raises(TypeError):
            appstate.restore(rows, {SOURCE: DEST})
        assert not appstate.project_dir(DEST).exists()

    def test_a_file_this_machine_has_and_the_export_does_not_is_reported_never_deleted(
            self, store_at, machine, tmp_path):
        """The slug directory is shared by every session with that working directory."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        wipe(machine)
        projects.import_(export_path, into=DEST)
        intruder = appstate.project_dir(DEST) / "someone-elses.jsonl"
        intruder.write_bytes(b"another session")
        result = projects.verify_mirror(export_path, into=DEST)
        assert any("someone-elses" in e for e in result["extra"])
        assert intruder.read_bytes() == b"another session"

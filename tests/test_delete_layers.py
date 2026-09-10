"""Delete reaching every layer, and stopping exactly where the backup stops.

`tests/test_delete_sticks.py` gates the store half: the exclusion is written by working directory,
and the set the backup holds is the set that is deleted. This gates the other four layers, and the
one rule that governs all of them:

    DELETE REMOVES EXACTLY WHAT THE BACKUP CONTAINS, AND NOTHING ELSE.

Both halves are load bearing and they fail in opposite directions. A delete that removes less than
the backup leaves a chat in the desktop app for a project the user deleted. A delete that removes
more than the backup takes another project's files: a slug directory is shared by every session
with that working directory, and measured on this machine the extreme case is
`P:\\ClaudeExt\\QuestionExtension\\archived`, which would carry 21 of the 2,430 files in its slug
directory while the base project carries 2,382 of them.

Every root is monkeypatched onto a temporary directory, layered over the autouse isolation in
`conftest.py`, so nothing here can read or write the real `~/.claude`, `~/.claude.json` or
`%APPDATA%\\Claude`.
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import projects  # noqa: E402
from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

ALPHA = r"P:\Alpha"
ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
FOREIGN_ACCOUNT = "99999999-9999-4999-8999-999999999999"
DESKTOP_FILE = "local_aaaa1111-2222-4333-8444-555566667777.json"


@pytest.fixture
def store_at(tmp_path, monkeypatch):
    from c4x import store
    path = build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A whole fake machine, holding what a real one holds for this project.

    The same shape `tests/test_mirror.py` builds, and deliberately a copy rather than an import:
    the fixtures in this suite are defined per file, and a delete needs to move parts of it that
    an import never touches.

    The desktop record is filed under a DIFFERENT account from the one this machine is signed in
    to. For an import that is the case that decides whether the chat is visible at all; for a
    delete it is the case that decides whether the chat is found.
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

    base = claude / "projects" / appstate.slug_for(ALPHA)
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
        "projects": {ALPHA: {"hasTrustDialogAccepted": True},
                     r"P:\Beta": {"hasTrustDialogAccepted": False}}}), encoding="utf-8")

    (sessions.parent / "config.json").write_text(
        json.dumps({"lastKnownAccountUuid": ACCOUNT}), encoding="utf-8")
    (sessions.parent / "plan-usage-history.json").write_text(
        json.dumps({"samples": [{"t": 1, "org": ORG}]}), encoding="utf-8")
    foreign = sessions / FOREIGN_ACCOUNT / ORG
    foreign.mkdir(parents=True)
    (foreign / DESKTOP_FILE).write_text(json.dumps({
        "cliSessionId": "s0-0", "sessionId": DESKTOP_FILE[: -len(".json")],
        "cwd": ALPHA, "originCwd": ALPHA, "isArchived": False, "title": "the chat"}),
        encoding="utf-8")
    return SimpleNamespace(claude=claude, config=config, sessions=sessions, base=base,
                           stray=None)


# ---------------------------------------------------------------------------
# Gate 10, as a fixture rather than a test, so it covers every case in this file
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_claude_project_purge(monkeypatch):
    """NOTHING HERE SHELLS OUT, and `claude project purge` least of all.

    That command is what blocked this work: it matches by slug PREFIX, so deleting `L:\\Books` also
    takes `L--Books-Courses`, and measured on this machine 21 of the 518 slug directories are a
    prefix of another. The design decision is that delete never delegates to it, and a decision
    that is only written down is not a gate, so every subprocess entry point raises here.
    """
    def guard(real):
        def checked(command, *args, **kwargs):
            words = [str(part) for part in
                     (command if isinstance(command, (list, tuple)) else [command])]
            # THE EXECUTABLE'S NAME AND WHOLE TOKENS, not a substring: this repo lives under
            # `P:\\ClaudeExt`, so `"claude" in part` matches the path of every node script it runs
            # and would fail on the store's own import.
            program = Path(words[0]).stem.casefold() if words else ""
            tokens = {word.casefold() for word in words[1:]}
            if program.startswith("claude") or {"project", "purge"} <= tokens:
                raise AssertionError(
                    f"a delete shelled out to the command this design exists to avoid: {command!r}")
            return real(command, *args, **kwargs)
        return checked

    # `store` shells out to node for the compaction constants, so a blanket ban would fail at
    # import and prove nothing about deleting. The ban is on the one command, by name.
    for name in ("run", "call", "check_call", "check_output", "Popen"):
        monkeypatch.setattr(subprocess, name, guard(getattr(subprocess, name)))


@pytest.fixture
def machine_with_extras(machine, store_at):
    """The fake machine, plus a file belonging to a session this store has no row for.

    That file is the `not_carried` case, and it is the normal case rather than a corner: measured
    on this machine, 7 of 35 sampled slug directories hold files the selected project would not
    carry.
    """
    stray = machine.base / "zz-unknown-session.jsonl"
    stray.write_bytes(b"a session this store has never seen\n")
    machine.stray = stray
    return machine


def tree(*roots):
    """{path: sha256 or DIRECTORY} for everything under these roots. The before and after.

    DIRECTORIES ARE IN HERE, AND THE ROOTS THEMSELVES ARE TOO, because leaving them out made this
    helper blind to the one class of damage the delete can actually do outside the project. It
    hashed `path.is_file()` only, so a delete that walked its prune past its floor and removed the
    machine wide `claude-code-sessions` directory changed nothing this returned, and every
    "nothing outside this project changed" assertion in the file passed over it. That is exactly
    what happened: an unbounded prune deleted the sessions root and `TestTheDesktopRecord` stayed
    green.

    An empty directory is not nothing. It is where the desktop app puts the next record.
    """
    out = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            out[str(root)] = "ABSENT"
            continue
        out[str(root)] = "DIRECTORY" if root.is_dir() else "FILE"
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                out[str(path)] = "DIRECTORY"
            elif path.is_file():
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def archive_only(monkeypatch, ids):
    """Make `store.session_rows()` label exactly these sessions as archived.

    Faked at the seam the label comes from, as `tests/test_delete_sticks.py` does, because no real
    store holds a cwd ending in the suffix: measured on the live store, 16 labels of that shape
    exist and zero cwds do.
    """
    from c4x import store
    monkeypatch.setattr(store, "archived_sessions", lambda *a, **k: dict.fromkeys(ids, True))
    forget_cached_rows()
    return ALPHA + "\\" + store.ARCHIVED_SUFFIX


def snapshots_for(store_path, session_ids):
    """Pre-compaction snapshots, named the way `hooks/compact-hook.mjs` names them."""
    base = Path(store_path).parent / "snapshots"
    base.mkdir(parents=True, exist_ok=True)
    made = []
    for sid in session_ids:
        path = base / f"{sid}.2026-01-01T00-00-00.pre-compact.jsonl"
        path.write_bytes(b'{"dropped":"by a compaction"}\n')
        made.append(path)
    return made


def config_of(machine):
    return json.loads(machine.config.read_text(encoding="utf-8-sig"))


class TestAnArchivedLabelTakesOnlyItsOwnSession:
    """The 2,409 file case, which is the reason this plan existed at all.

    A label and its base working directory share ONE slug directory. Deleting the label must take
    that session's transcripts and leave every other session's, and it must leave the two layers
    that are keyed by the DIRECTORY rather than the session, `memory/` and the `~/.claude.json`
    entry, because a project the user did not delete still lives there.
    """

    def test_the_other_sessions_files_are_all_still_there(
            self, store_at, machine, tmp_path, monkeypatch):
        label = archive_only(monkeypatch, ["s0-0"])
        before = tree(machine.claude, machine.sessions)

        result = projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        assert not (machine.base / "s0-0.jsonl").exists()
        assert not (machine.base / "s0-0").exists()
        for survivor in ("s0-1", "s0-2"):
            assert (machine.base / f"{survivor}.jsonl").exists(), (
                f"{survivor} belongs to the base project, which was not deleted")
            assert (machine.base / survivor / "subagents" / "one.jsonl").exists()
        after = tree(machine.claude, machine.sessions)
        gone = set(before) - set(after)
        assert all("s0-0" in path or DESKTOP_FILE in path for path in gone), sorted(gone)
        assert result["surviving_sessions"] == ["s0-1", "s0-2"]

    def test_memory_is_kept_for_a_project_that_only_shares_the_slug_directory(
            self, store_at, machine, tmp_path):
        """The survivor test compared cwd STRINGS while `memory/` is keyed by the SLUG directory.

        `slug_for` turns every character that is not a letter or a digit into a hyphen, so a
        forward slash and a backslash in the same place produce one directory from two different
        working directories, with two different `~/.claude.json` keys. Asking "does any session
        still have this exact cwd" answered no and the delete took a live project's memory with it,
        then listed it under `shared_with_surviving_sessions` as kept.
        """
        from c4x import appstate
        assert appstate.slug_for("P:/Alpha") == appstate.slug_for(ALPHA), (
            "the fixture depends on these two strings sharing one slug directory")
        con = sqlite3.connect(str(store_at))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("other-0", "slug-0", "P:/Alpha", None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", r"C:	\other-0.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert (machine.base / "memory" / "notes.md").exists(), (
            "another project is still filed in this slug directory and its memory is in it")
        assert result["surviving_sessions"] == [], "no session has that exact working directory"
        assert result["sessions_sharing_slug"] == ["other-0"]
        assert {e["kind"] for e in result["shared_with_surviving_sessions"]} == {"memory"}, (
            "the config entry is keyed by the exact string, so it goes; memory is not, so it stays")
        assert ALPHA not in config_of(machine)["projects"]

    def test_memory_and_the_trust_entry_belong_to_the_directory_and_stay(
            self, store_at, machine, tmp_path, monkeypatch):
        label = archive_only(monkeypatch, ["s0-0"])

        result = projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        assert (machine.base / "memory" / "notes.md").exists(), (
            "memory is keyed by the working directory, and two sessions of the base project are "
            "still in it")
        assert ALPHA in config_of(machine)["projects"]
        kinds = {entry["kind"] for entry in result["shared_with_surviving_sessions"]}
        assert kinds == {"memory", "config"}
        assert result["config_keys_removed"] == []

    def test_the_working_directory_keeps_being_captured(
            self, store_at, machine, tmp_path, monkeypatch):
        """An exclusion is by DIRECTORY, and the harvester skips everything under it."""
        label = archive_only(monkeypatch, ["s0-0"])

        result = projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        assert result["excluded_cwds"] == []
        assert result["still_captured"] == [ALPHA]
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert [r[0] for r in con.execute("SELECT cwd FROM excluded_projects")] == []
        finally:
            con.close()

    def test_deleting_the_whole_directory_does_take_memory_and_the_entry(
            self, store_at, machine, tmp_path):
        """The other side of the same rule, so the test above cannot pass by never removing."""
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert not (machine.base / "memory" / "notes.md").exists()
        assert ALPHA not in config_of(machine)["projects"]
        assert result["config_keys_removed"] == [ALPHA]
        assert result["shared_with_surviving_sessions"] == []


class TestNothingTheBackupDoesNotHold:
    def test_a_file_belonging_to_an_unknown_session_survives_and_is_named(
            self, store_at, machine_with_extras, tmp_path):
        machine = machine_with_extras
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert machine.stray.exists(), (
            "that file belongs to a session this store has no row for, so it is not ours to take")
        not_carried = projects.read_manifest(result["backup"])["app_state"]["not_carried"]
        assert any(str(machine.stray) == entry["path"] for entry in not_carried)

    def test_nothing_outside_this_project_changes(self, store_at, machine, tmp_path):
        """Hash the whole fake machine before and after, and account for every difference."""
        before = tree(machine.claude, machine.sessions, machine.config.parent)
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")
        after = tree(machine.claude, machine.sessions, machine.config.parent)

        removed = {path for path in before if path not in after}
        added = {path for path in after if path not in before}
        changed = {path for path in before if path in after and before[path] != after[path]}

        assert result["removed_files"] > 0
        assert changed == {str(machine.config)}, (
            "the only file whose CONTENT changes is the config, and only by losing one key")
        assert added == {str(machine.config) + ".c4x-before"}, (
            "a copy is kept beside the config, as the import does")
        assert removed, "nothing was removed at all"
        for path in removed:
            assert str(machine.base) in path or "tasks" in path or DESKTOP_FILE in path, path
        assert r"P:\Beta" in config_of(machine)["projects"], (
            "the other project's trust entry is in the same file and is not this delete's")

    def test_a_file_changed_since_the_backup_is_kept_and_named(
            self, store_at, machine, tmp_path, monkeypatch):
        """The backup is the undo, so anything it cannot restore is not this function's to take."""
        real_export = projects.export

        def export_then_touch(project, out_path, app_state=True):
            manifest = real_export(project, out_path, app_state=app_state)
            (machine.base / "s0-1.jsonl").write_bytes(b"appended after the backup was written\n")
            return manifest

        monkeypatch.setattr(projects, "export", export_then_touch)
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert (machine.base / "s0-1.jsonl").exists()
        kept = {Path(entry["path"]).name: entry["why"] for entry in result["kept_files"]}
        assert "s0-1.jsonl" in kept
        assert "changed since the backup" in kept["s0-1.jsonl"]
        assert [entry["path"] for entry in result["still_here"]] == [
            str(machine.base / "s0-1.jsonl")]


class TestTheDesktopRecord:
    def test_the_chat_is_removed_even_under_an_account_this_machine_no_longer_uses(
            self, store_at, machine, tmp_path):
        """Where the record IS, not where an import would PUT it.

        The fixture files it under a different account from the one this machine is signed in to,
        which is the case that decides whether an import is visible in the app at all. A delete
        that resolved the destination the way an import does would miss it and leave the chat
        listed.
        """
        record = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        assert record.exists()

        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert not record.exists()

    def test_the_prune_stops_at_the_record_and_leaves_the_machines_directories(
            self, store_at, machine, tmp_path):
        """An account and an organisation directory belong to the MACHINE, not to this project.

        The prune walked straight past them and removed `claude-code-sessions` itself. The floor it
        was handed was `desktop_dir(sessions_root)`, the pair the app writes to NOW, while
        `purge_paths` finds the record where it actually IS, and this fixture makes those two
        differ on purpose. A floor that is not an ancestor is never reached by a loop that stops on
        equality, so nothing bounded the walk.

        It stayed invisible because `tree()` hashed files and ignored directories, so no assertion
        in this file could see a directory disappear.
        """
        record = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        before = tree(machine.sessions)

        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert not record.exists()
        assert (machine.sessions / FOREIGN_ACCOUNT / ORG).is_dir(), (
            "the organisation directory is the machine's and holds every other chat under it")
        assert (machine.sessions / FOREIGN_ACCOUNT).is_dir()
        assert machine.sessions.is_dir(), (
            "claude-code-sessions is where the desktop app writes the NEXT record")
        gone = set(before) - set(tree(machine.sessions))
        assert gone == {str(record)}, (
            f"the delete removed more than the one record it carried: {sorted(gone)}")

    def test_one_row_naming_two_files_is_refused_rather_than_removing_both(self, machine):
        """The glob spans every account and organisation pair, the backup holds ONE row.

        That row cannot say which of two files it describes, and removing a file the backup does
        not separately hold is the one thing this delete promises never to do.
        """
        from c4x import appstate
        source = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        twin = machine.sessions / ACCOUNT / ORG
        twin.mkdir(parents=True, exist_ok=True)
        (twin / DESKTOP_FILE).write_bytes(source.read_bytes())
        row = {"kind": appstate.DESKTOP, "relpath": DESKTOP_FILE, "cwd": ALPHA,
               "sha256": "x", "rebased_sha256": "x"}

        paths, refusal = appstate.purge_paths(row, ALPHA, str(machine.sessions))

        assert paths == []
        assert "more than one record under that name" in refusal
        assert str(source) in refusal and str(twin / DESKTOP_FILE) in refusal
        assert source.exists() and (twin / DESKTOP_FILE).exists()


class TestTheInverseOfTheMirror:
    def test_every_carried_file_is_missing_afterwards(self, store_at, machine, tmp_path):
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")
        backup = result["backup"]

        mirror = projects.verify_mirror(backup)
        carried = {row["relpath"] for row in projects.app_state_rows(backup, with_blobs=False)
                   if row["kind"] != "config"}
        # NAMED, NOT COUNTED. A backup that carries nothing satisfies "everything it carries is
        # missing" perfectly, so the set is pinned against the fixture before it is compared.
        assert {"s0-0.jsonl", "s0-1.jsonl", "s0-2.jsonl", "memory/notes.md",
                "s0-0/task.json", DESKTOP_FILE} <= carried, sorted(carried)
        assert {entry["relpath"] for entry in mirror["missing"]} == carried
        assert result["still_here"] == []
        assert result["removed_files"] == len(carried)

        # ASSERTING `differs` IS EMPTY HERE PROVED NOTHING. A file is either missing or different,
        # and the line above just asserted every carried file is missing, so `differs` was empty by
        # construction and the assertion could not fail. Put one back with other bytes instead, so
        # the channel is exercised rather than assumed.
        assert [entry for entry in mirror["differs"] if entry["kind"] != "config"] == []
        # The slug directory itself was pruned, correctly, because the delete emptied it.
        machine.base.mkdir(parents=True, exist_ok=True)
        (machine.base / "s0-1.jsonl").write_bytes(b"not what the backup holds")
        again = projects.verify_mirror(backup)
        assert {entry["relpath"] for entry in again["differs"]
                if entry["kind"] != "config"} == {"s0-1.jsonl"}
        assert "s0-1.jsonl" not in {entry["relpath"] for entry in again["missing"]}

    def test_importing_the_backup_puts_the_whole_project_back(self, store_at, machine, tmp_path):
        """The round trip, which is the claim the Delete panel makes on the page."""
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")
        backup = result["backup"]
        forget_cached_rows()

        projects.import_(backup)

        mirror = projects.verify_mirror(backup)
        assert mirror["ok"] is True, mirror
        assert mirror["carries_no_files"] is False
        assert (machine.base / "s0-0.jsonl").exists()
        assert (machine.base / "memory" / "notes.md").exists()
        assert ALPHA in config_of(machine)["projects"]


class TestNothingIsRemovedOnTheStrengthOfABackupThatDoesNotRead:
    def test_an_export_that_cannot_verify_deletes_nothing(
            self, store_at, machine, tmp_path, monkeypatch):
        before = tree(machine.claude, machine.sessions)
        monkeypatch.setattr(projects, "verify",
                            lambda path: (False, ["deliberately refused by the test"]))

        with pytest.raises(RuntimeError):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert tree(machine.claude, machine.sessions) == before

    def test_a_config_that_does_not_parse_stops_the_delete_before_the_backup(
            self, store_at, machine, tmp_path):
        """The defect that cost 26 project entries on the test laptop, as a refusal."""
        before = tree(machine.claude, machine.sessions)
        machine.config.write_text("{not json at all", encoding="utf-8")
        out_dir = tmp_path / "backups"

        with pytest.raises(ValueError, match="does not parse as JSON"):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=out_dir)

        assert (machine.base / "s0-0.jsonl").exists()
        assert set(tree(machine.claude, machine.sessions)) == set(before)
        assert not out_dir.exists() or list(out_dir.iterdir()) == [], (
            "no backup is written for a delete that is going to refuse")


class TestTheSnapshotLayer:
    def test_snapshots_are_kept_and_counted_by_default(self, store_at, machine, tmp_path):
        made = snapshots_for(store_at, ["s0-0", "s0-1"])

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert all(path.exists() for path in made)
        assert result["snapshots"]["files"] == 2
        assert result["snapshots"]["removed"] == 0
        assert result["snapshots"]["bytes"] == sum(p.stat().st_size for p in made)

    def test_purge_snapshots_removes_exactly_this_projects_own(
            self, store_at, machine, tmp_path):
        mine = snapshots_for(store_at, ["s0-0", "s0-1"])
        someone_else = snapshots_for(store_at, ["s1-0"])

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 purge_snapshots=True)

        assert not any(path.exists() for path in mine)
        assert all(path.exists() for path in someone_else), (
            "those belong to P:\\Beta, which was not deleted")
        assert result["snapshots"]["removed"] == 2


class TestTheRaceBetweenTheBackupAndTheDelete:
    def test_a_session_that_arrives_after_the_backup_keeps_its_files(
            self, store_at, machine, tmp_path, monkeypatch):
        real_export = projects.export

        def export_then_harvest(project, out_path, app_state=True):
            manifest = real_export(project, out_path, app_state=app_state)
            (machine.base / "s0-9.jsonl").write_bytes(b"harvested while the backup was written\n")
            con = sqlite3.connect(str(store_at))
            try:
                con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                            ("s0-9", "slug-0", ALPHA, "late", "2.1.229", "cli",
                             "2026-08-03T00:00:00Z", "2026-08-03T00:00:00Z",
                             r"C:\t\s0-9.jsonl"))
                con.commit()
            finally:
                con.close()
            forget_cached_rows()
            return manifest

        monkeypatch.setattr(projects, "export", export_then_harvest)
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert (machine.base / "s0-9.jsonl").exists(), (
            "it is not in the only copy of this project, so it is not deletable")
        assert result["appeared_since_backup"] == ["s0-9"]


class TestDirectoriesAreOnlyRemovedWhenEmpty:
    def test_the_slug_directory_goes_when_the_last_file_in_it_does(
            self, store_at, machine, tmp_path):
        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert not machine.base.exists()
        assert machine.base.parent.exists(), "~/.claude/projects holds every other project"

    def test_a_directory_with_anything_left_in_it_stays(
            self, store_at, machine_with_extras, tmp_path):
        machine = machine_with_extras
        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert machine.base.is_dir()
        assert machine.stray.exists()


class TestPurgeRefusesWhatRestoreRefuses:
    def test_a_bare_string_where_directories_belong_is_refused(self):
        from c4x import appstate
        with pytest.raises(TypeError):
            appstate.purge([], ALPHA + "\\archived")

    def test_a_row_carrying_no_hash_is_refused_rather_than_kept_in_silence(self):
        from c4x import appstate
        row = {"kind": "transcript", "cwd": ALPHA, "relpath": "s0-0.jsonl",
               "sha256": None, "rebased_sha256": None}
        with pytest.raises(TypeError, match="sha256"):
            appstate.purge([row], [ALPHA])

    def test_an_absolute_path_is_refused_and_never_followed(self, machine):
        from c4x import appstate
        row = {"kind": "transcript", "cwd": ALPHA, "relpath": r"C:\evil.txt",
               "sha256": "sha256:x", "rebased_sha256": "sha256:x"}
        report = appstate.purge([row], [ALPHA])
        assert report["removed"] == []
        assert "absolute" in report["refused"][0]["why"]

    def test_a_trust_entry_changed_since_the_backup_is_kept_and_named(
            self, store_at, machine, tmp_path):
        """`_drop_config` drops a key ONLY when the entry under it is the one the backup holds.

        Nothing exercised that branch. The config is the one layer a purge edits in place rather
        than unlinking, and this guard is what stops a delete discarding a setting the user changed
        after the backup was written, which the backup cannot then put back.
        """
        from c4x import appstate
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        projects.import_(result["backup"])
        rows = projects.app_state_rows(result["backup"], with_blobs=False)
        config = config_of(machine)
        assert ALPHA in config["projects"], "the import put the entry back, or this proves nothing"
        config["projects"][ALPHA] = {"hasTrustDialogAccepted": True, "changedSince": True}
        machine.config.write_text(json.dumps(config), encoding="utf-8")

        report = appstate.purge(rows, [ALPHA])

        assert report["config_keys"] == [], "the entry is not the one the backup holds"
        assert [entry["key"] for entry in report["config_kept"]] == [ALPHA]
        assert "not the one the backup holds" in report["config_kept"][0]["why"]
        assert config_of(machine)["projects"][ALPHA] == {
            "hasTrustDialogAccepted": True, "changedSince": True}
        assert r"P:\Beta" in config_of(machine)["projects"], "every other project is untouched"

    def test_a_dry_run_names_every_path_and_removes_nothing(self, store_at, machine, tmp_path):
        from c4x import appstate
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        projects.import_(result["backup"])
        rows = projects.app_state_rows(result["backup"], with_blobs=False)
        before = tree(machine.claude, machine.sessions)

        report = appstate.purge(rows, [ALPHA], dry_run=True)

        assert report["dry_run"] is True
        # NAMED, NOT TRUTHY. `assert report["removed"]` passed on a dry run that listed one row of
        # six, which is the whole failure this test exists to catch.
        assert {entry["relpath"] for entry in report["removed"]} == {
            row["relpath"] for row in rows if row["kind"] != "config"}
        assert report["config_keys"] == [ALPHA]
        assert tree(machine.claude, machine.sessions) == before
# ---------------------------------------------------------------------------
# The caches that outlive a removal
# ---------------------------------------------------------------------------
class TestARemovalDoesNotLeaveTheRowOnThePage:
    """`store` holds four module level caches for 45 seconds and nothing cleared any of them.

    So a delete driven from the panel removed the rows and then went on drawing them for up to the
    full ttl, which is indistinguishable from a delete that silently did nothing and invites a
    second one.

    The cache that matters most is not the visible one. `transcript_ids()` answers "does this
    session still have a transcript", which is the predicate a session prune deletes on, so a set
    scanned before a removal is the wrong basis for the next decision.

    EVERY CACHE HERE IS WARMED BEFORE THE DELETE, AND THE WARMTH IS ASSERTED. Checking a cold cache
    would pass whether or not anything ever cleared it, which is a gate that cannot fail.
    """

    def test_the_deleted_sessions_are_gone_from_the_frame_at_the_default_ttl(
            self, store_at, machine, tmp_path):
        from c4x import store
        warm = set(store.session_rows()["session_id"])
        assert {"s0-0", "s0-1", "s0-2"} <= warm, (
            "the frame cache must be warm and hold this project, or this proves nothing")

        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        after = set(store.session_rows()["session_id"])
        assert not ({"s0-0", "s0-1", "s0-2"} & after), (
            "session_rows() served a 45 second old frame, so the panel would still draw the "
            "project that was just deleted")
        assert "s1-0" in after, "the other project was not deleted and must still be listed"

    def test_the_caches_are_cleared_even_when_the_delete_raises_after_the_rows_are_gone(
            self, store_at, machine, tmp_path, monkeypatch):
        """`invalidate()` sat at the end, after everything that can raise.

        The rows are committed when the write transaction closes, and the file removal, the
        snapshot pass and the acceptance check all run after it and all raise. On exactly those
        runs the caches still held the deleted project, so the page went on drawing rows that were
        already gone, for up to the full ttl, at the moment the user most needed it to be true.
        """
        from c4x import appstate, store

        def explode(*_args, **_kwargs):
            raise RuntimeError("the file half failed")

        monkeypatch.setattr(appstate, "purge", explode)
        store.session_rows()
        assert store._rows_cache["df"] is not None, "warm, or this proves nothing"

        with pytest.raises(RuntimeError, match="the file half failed"):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert store._rows_cache["df"] is None, (
            "the rows were committed, so the frame the page serves is wrong until this is cleared")
        assert store._transcript_cache["ids"] is None

    def test_every_cache_the_removal_invalidates_is_actually_cleared(
            self, store_at, machine, tmp_path):
        """One assertion per line of `store.invalidate`, so reverting any one of them fails here."""
        import time

        from c4x import store
        # Stamped NOW rather than in the far future. A far future stamp also defeats the `ttl=0`
        # that `session_ids` uses to force a fresh read before a write, so the delete under test
        # would have resolved its own id set from the sentinel.
        now = time.time()
        store.session_rows()
        store._archived_cache.update({"map": {"s0-0": True}, "at": now, "root": "sentinel root"})
        store._transcript_cache.update({"ids": {"sentinel"}, "at": now})
        store._window_cache["s0-0"] = "sentinel window"
        assert store._rows_cache["df"] is not None, "warm, or this proves nothing"
        assert store.transcript_ids() == {"sentinel"}, "warm, or this proves nothing"

        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert store._rows_cache["df"] is None
        assert store._archived_cache["map"] is None
        assert store._transcript_cache["ids"] is None
        assert store._window_cache == {}

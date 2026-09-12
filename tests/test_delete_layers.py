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

Every root these tests WRITE through is monkeypatched onto a temporary directory, layered over the
autouse isolation in `conftest.py`, so nothing here can write the real `~/.claude`,
`~/.claude.json` or the desktop app's application data directory.

READS ARE NOT FULLY ISOLATED, and claiming they were was wider than the truth. `conftest.py`
patches `appstate.CLAUDE_DIR`, `appstate.CONFIG_PATH` and `appstate.sessions_root`. It
deliberately does NOT patch `store.sessions_root`, because that is what puts the archived marker
on every session row and pointing it at an empty directory silently removed the marker from the
whole suite. Neither is `store.HOME` (`store.py:35`), which is resolved at import. So `delete` to
`session_ids` to `session_rows` to `classify` scans the real projects directory, and
`archived_sessions` globs the real application data directory. Both are reads, both are harmless,
and no test here may depend on what they return.
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
        # BOTH SHARED LAYERS STAY, for two different reasons, which is why they are decided per row
        # rather than per kind. `memory/` stays because a session still slugs into that directory.
        # The trust entry stays because `normalised` folds the slash spelling, so the surviving
        # session at the forward slash spelling IS a session in the directory this key names, and
        # `_capture_config` carried the key on exactly that basis.
        assert {e["kind"] for e in result["shared_with_surviving_sessions"]} == {"memory", "config"}
        assert ALPHA in config_of(machine)["projects"], (
            "a live project is in this directory, and without its trust entry Claude Code asks to "
            "trust the directory again")
        assert result["config_keys_removed"] == []

    def test_a_surviving_project_under_the_other_slash_spelling_keeps_its_trust_entry(
            self, store_at, machine, tmp_path):
        """`_capture_config` selects keys with `normalised(key)`, so it carries BOTH spellings.

        The survivor test that decided whether to keep them was a byte exact `WHERE cwd IN`, so a
        session filed under the other spelling was not a survivor and `_drop_config` deleted both
        keys by exact match. The live project lost its trust entry and its per project settings,
        Claude Code would ask to trust that directory again, and the report said the settings had
        been kept while `config_keys_removed` named both.

        One `import --into` with the other slash spelling puts both forms in one store, so this is
        reachable through the app's own commands.
        """
        config = config_of(machine)
        config["projects"]["P:/Alpha"] = {"hasTrustDialogAccepted": True, "other": "spelling"}
        machine.config.write_text(json.dumps(config), encoding="utf-8")
        con = sqlite3.connect(str(store_at))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("other-0", "slug-0", "P:/Alpha", None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", "C:/t/other-0.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        after = config_of(machine)["projects"]
        assert "P:/Alpha" in after, "the surviving project's own key"
        assert ALPHA in after, "the same directory under the spelling the backup carried"
        assert result["config_keys_removed"] == []
        assert {e["relpath"] for e in result["shared_with_surviving_sessions"]} >= {
            ALPHA, "P:/Alpha"}
        assert r"P:\Beta" in after, "every unrelated project is untouched"

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
        # THE DELETE'S OWN REPORT, not the manifest. Reading the manifest here tested
        # `export`, which already behaved this way, so `"not_carried": []` in the delete report
        # passed. These files are on disk and not in the backup, which is the one thing the
        # acceptance rule does not cover, so the delete has to say so itself.
        assert any(str(machine.stray) == entry["path"] for entry in result["not_carried"])
        carried = projects.read_manifest(result["backup"])["app_state"]["not_carried"]
        assert result["not_carried"] == carried

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
        # NOT the bare substring "tasks", which is satisfied by `~/.claude/tasks` ITSELF. That
        # directory belongs to the machine and holds every other project's task directories, so
        # raising the TASKS prune floor by one level removed it and this assertion still passed.
        own_tasks = str(machine.claude / "tasks" / "s0-0")
        for path in removed:
            assert (str(machine.base) in path or path.startswith(own_tasks)
                    or DESKTOP_FILE in path), path
        assert str(machine.claude / "tasks") in after, (
            "the tasks directory is the machine's, and every other project has one under it")
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

    def test_one_row_naming_two_files_removes_the_copies_whose_bytes_it_holds(self, machine):
        """Two files, one name, and the hash decides each of them on its own.

        The glob spans every account and organisation pair, and now every records root, so two
        matches are two paths to the SAME record: a packaged install keeping a second root, or a
        pair the app has stopped filing under.

        This refused outright, on the argument that the backup's single row cannot say which of two
        files it describes. It does not have to. A purge removes a file only when the bytes on the
        disk hash to that row and KEEPS and names any that do not, so the refusal protected nothing
        and left the real record in place, which on a page that reads every root keeps the deleted
        chat listed under its old name.
        """
        from c4x import appstate
        source = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        twin = machine.sessions / ACCOUNT / ORG
        twin.mkdir(parents=True, exist_ok=True)
        (twin / DESKTOP_FILE).write_bytes(source.read_bytes())
        blob = source.read_bytes()
        marked = appstate.rebase_marked(blob, appstate.DESKTOP_CWD_FIELDS) or blob
        row = {"kind": appstate.DESKTOP, "relpath": DESKTOP_FILE, "cwd": ALPHA,
               "sha256": appstate.sha256_bytes(blob),
               "rebased_sha256": appstate.sha256_bytes(marked)}

        paths, refusal = appstate.purge_paths(row, ALPHA, str(machine.sessions))

        assert refusal is None
        assert sorted(str(p) for p in paths) == sorted(
            [str(source), str(twin / DESKTOP_FILE)])
        appstate.purge([row], [ALPHA], str(machine.sessions))
        assert not source.exists() and not (twin / DESKTOP_FILE).exists()

    def test_a_second_copy_the_backup_cannot_restore_is_kept_and_named(self, machine):
        """The other half, and the reason the refusal was never what protected anything."""
        from c4x import appstate
        source = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE
        twin = machine.sessions / ACCOUNT / ORG
        twin.mkdir(parents=True, exist_ok=True)
        (twin / DESKTOP_FILE).write_bytes(b'{"cliSessionId": "s0-0", "title": "something else"}')
        blob = source.read_bytes()
        marked = appstate.rebase_marked(blob, appstate.DESKTOP_CWD_FIELDS) or blob
        row = {"kind": appstate.DESKTOP, "relpath": DESKTOP_FILE, "cwd": ALPHA,
               "sha256": appstate.sha256_bytes(blob),
               "rebased_sha256": appstate.sha256_bytes(marked)}

        report = appstate.purge([row], [ALPHA], str(machine.sessions))

        assert not source.exists(), "the copy the backup holds was not removed"
        assert (twin / DESKTOP_FILE).exists(), "a copy the backup cannot put back was removed"
        assert any(str(twin / DESKTOP_FILE) in kept["path"] for kept in report["kept"]), (
            report["kept"])


class TestAStoredPathIsNotThisPlatformsPath:
    """`transcript_path` is whatever the CAPTURING machine wrote, which need not be this one.

    This store holds 193 sessions captured on another host, and CI runs on Linux while the app runs
    on Windows. `Path(...).stem` answers with the RUNNING platform's rules, so on POSIX a Windows
    path is one component and its stem is the whole path: the shared-snapshot guard matched nothing
    and silently purged a snapshot it had just promised to keep. The assertions below hold on every
    platform, which is the point of them.
    """

    def test_the_key_is_the_same_whichever_separator_the_path_carries(self):
        assert projects.transcript_key(r"C:\\t\\s0-0.jsonl") == "s0-0"
        assert projects.transcript_key("/home/u/.claude/projects/x/s0-0.jsonl") == "s0-0"
        assert projects.transcript_key("s0-0.20260101-000000.pre-compact.jsonl") == "s0-0"
        assert projects.transcript_key(r"C:\\t\\s0-0.20260101.pre-compact.jsonl") == "s0-0"

    def test_pathlib_would_have_answered_differently_on_linux(self):
        """The measurement this exists for, pinned so the reason cannot be lost.

        `PurePosixPath` is how the same call behaves on the machine CI uses.
        """
        from pathlib import PurePosixPath, PureWindowsPath
        stored = r"C:\\t\\s0-0.jsonl"

        assert PureWindowsPath(stored).stem == "s0-0"
        assert PurePosixPath(stored).stem != "s0-0", (
            "this is the difference that made the guard a no-op on Linux")
        assert projects.transcript_key(stored) == "s0-0", "and this is what it answers instead"


class TestTheBackupHoldsEveryRowTheDeleteRemoves:
    def test_rows_written_while_the_backup_was_being_taken_stop_the_delete(
            self, store_at, machine, tmp_path, monkeypatch):
        """`appeared_since_backup` closes this race at SESSION granularity and no finer.

        The row copy happens at the top of `export`; the DELETEs run minutes later against `ids`.
        Anything a concurrent harvest writes for a session ALREADY in `ids` was removed and was in
        no copy, and `still_here` only re-resolves the app-state FILES, so the row half of "a
        delete removes exactly what the backup contains" was unchecked. `hook_events` is the class
        with no recovery path at all, because its watermark row is not deleted.
        """
        real = projects.export

        def a_harvest_lands_after_the_backup(project, out_path, app_state=True):
            manifest = real(project, out_path, app_state=app_state)
            con = sqlite3.connect(str(store_at))
            con.execute(
                """INSERT INTO turns (uuid,session_id,ts,model,request_id,input_tokens,
                     cache_creation_input_tokens,cache_read_input_tokens,output_tokens,
                     thinking_tokens,eph_1h,eph_5m,service_tier,total_resident,is_sidechain,
                     file_path,line_no,parent_uuid)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("s0-0-LATE", "s0-0", "2026-08-01T01:00:00Z", "claude-opus-5", "req-late",
                 1, 2, 3, 4, 0, 0, 0, "x", 5, 0, "C:/t/s0-0.jsonl", 99, None))
            con.commit()
            con.close()
            return manifest

        monkeypatch.setattr(projects, "export", a_harvest_lands_after_the_backup)

        with pytest.raises(ValueError, match="the store changed while the backup"):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM turns WHERE uuid = ?",
                               ("s0-0-LATE",)).fetchone()[0] == 1, "the late row is still there"
            assert con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                               (ALPHA,)).fetchone()[0] == 3, "and so is every session"
        finally:
            con.close()
        assert (machine.base / "s0-0.jsonl").exists(), "and every file"

    def test_a_snapshot_of_a_transcript_a_survivor_is_also_in_is_kept(
            self, store_at, machine, tmp_path):
        """A snapshot is a byte copy of a transcript FILE, and a file can hold two sessions.

        `hooks/compact-hook.mjs` names it from the transcript's basename, so matching that stem
        against a session id asks "does this session own that file", which is false for 8 sessions
        across 7 files on the live store. The backup does not carry snapshots, so removing one
        takes another project's only copy of what a compaction dropped.
        """
        con = sqlite3.connect(str(store_at))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("lodger-0", "slug-l", "P:/Gamma", None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", r"C:\t\s0-0.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()
        snaps = Path(store_at).parent / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        shared = snaps / "s0-0.20260101-000000.pre-compact.jsonl"
        alone = snaps / "s0-1.20260101-000000.pre-compact.jsonl"
        shared.write_bytes(b"two sessions are in this file")
        alone.write_bytes(b"only one session is in this file")

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 purge_snapshots=True)

        assert shared.exists(), (
            "lodger-0 is in that same transcript, and this is the only copy of what its "
            "compaction dropped")
        assert not alone.exists(), "the one this project owns outright still goes"
        assert result["snapshots"]["removed"] == 1

    def test_a_config_that_cannot_be_written_is_reported_rather_than_raised(
            self, store_at, machine, tmp_path, monkeypatch):
        """It is the last write, after every other layer is already gone.

        Raising here aborted the delete once the transcripts, the tasks and the desktop record had
        been removed, so the caller never got the report naming them. A config that could not be
        edited is a config that was KEPT, which this function already knows how to say.
        """
        from c4x import appstate

        def refuse(*_args, **_kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(appstate.os, "replace", refuse)

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert result["config_keys_removed"] == []
        assert [entry["key"] for entry in result["config_keys_kept"]] == [ALPHA]
        assert "could not be written" in result["config_keys_kept"][0]["why"]
        assert not (machine.base / "s0-0.jsonl").exists(), "the rest of the delete still happened"

    def test_a_project_with_no_working_directory_writes_no_exclusion(
            self, store_at, machine, tmp_path):
        """`manifest["cwds"] or [project]` fed a PAGE LABEL to everything below it.

        For sessions with no working directory recorded the label is
        `<slug> (no working directory recorded)`, and the `excluded_projects` row written for that
        string can never equal any `d.cwd`, so harvest never skips them and they return on the next
        pass with their offset row already gone.
        """
        con = sqlite3.connect(str(store_at))
        con.execute("UPDATE sessions SET cwd = NULL WHERE cwd = ?", (ALPHA,))
        con.commit()
        con.close()
        forget_cached_rows()
        from c4x import store
        label = store.project_label(None, "slug-0")
        assert "no working directory" in label, label
        assert label in {entry["project"] for entry in projects.projects()}, (
            "the picker offers this label, which is what makes it deletable")

        result = projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        assert result["unlocated"] is True
        assert result["excluded_cwds"] == [], "no directory means no exclusion harvest could match"
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM excluded_projects").fetchone()[0] == 0
        finally:
            con.close()


class TestWhatTheReportReachesAUserAs:
    def test_a_late_write_inside_a_carried_directory_is_named(
            self, store_at, machine, tmp_path, monkeypatch):
        """`appeared_files` compared a top-level entry NAME against a flat set of relpaths.

        Three mistakes in one line: a tasks or desktop relpath could shadow a slug entry of the
        same name; a directory that carried ONE file was treated as fully carried, so a write
        inside it was invisible; and an entry name never equals a nested relpath anyway. The file
        this field exists to name was exactly the one it missed.
        """
        real = projects.export
        late = machine.base / "s0-0" / "subagents" / "arrived-late.jsonl"

        def a_write_lands_after_the_capture(project, out_path, app_state=True):
            manifest = real(project, out_path, app_state=app_state)
            late.parent.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"written after the backup was taken")
            return manifest

        monkeypatch.setattr(projects, "export", a_write_lands_after_the_capture)

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert late.exists(), "the backup does not hold it, so the purge must not take it"
        assert str(late) in {entry["path"] for entry in result["appeared_files"]}, (
            "it is inside a directory that DID carry files, which is what hid it")

    def test_the_cli_does_not_print_the_two_sentences_that_were_false(
            self, store_at, machine, tmp_path, capsys):
        """Nothing read CLI output, so two sentences this branch proved false stayed in it.

        One named "0 session(s) still have this working directory" as the REASON it kept files,
        whenever memory was kept for a session sharing the slug directory. The other told the user
        a still-captured directory "has sessions this delete did not take", which is exactly untrue
        for the shared-transcript case, and suppressed the it-comes-back warning while doing it.
        """
        con = sqlite3.connect(str(store_at))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("lodger-0", "slug-l", "P:/Gamma", None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", r"C:\t\s0-0.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()

        code = projects.main(["delete", ALPHA, "--confirm", ALPHA,
                              "--out-dir", str(tmp_path / "backups")])
        printed = capsys.readouterr().out

        assert code == 0, printed
        assert "has sessions this delete did not take" not in printed, (
            "that directory has no sessions left at all; it is kept capturing because it shares a "
            "transcript FILE, and the harvester abandons files")
        assert "STILL CAPTURED" in printed
        assert "shares" in printed and "skips whole files" in printed
        assert "the next harvest brings it back" in printed, (
            "it is not excluded, so it does come back, and that is the case the old condition "
            "suppressed the warning for")


class TestTheDryRunAndTheRunAgree:
    def test_a_writer_holds_the_lock_before_it_reads(self, store_at):
        """Python's sqlite3 defers the BEGIN until the first statement that changes something.

        So every SELECT a writer made first ran in autocommit, and a caller that checks the store
        and then deletes on the strength of that check had an open window between the two. This
        store has three writers by design, and the delete's own row guard is exactly such a
        check-then-delete.
        """
        from c4x import store
        with store.write() as con:
            assert con.in_transaction, (
                "the write lock has to be held before the first read, or the guard is not atomic "
                "with what it guards")

    def test_a_file_that_is_not_there_is_absent_in_both(self, store_at, machine, tmp_path):
        """The dry run called it `removed`; the run calls it `absent`.

        Same drift the hash test was added to close, one branch further up: a dry run that
        disagrees with the run about what will happen is worse than no dry run.
        """
        from c4x import appstate
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        projects.import_(result["backup"])
        rows = projects.app_state_rows(result["backup"], with_blobs=False)
        (machine.base / "s0-1.jsonl").unlink()

        dry = appstate.purge(rows, [ALPHA], dry_run=True)

        assert "s0-1.jsonl" in {entry["relpath"] for entry in dry["absent"]}
        assert "s0-1.jsonl" not in {entry["relpath"] for entry in dry["removed"]}

    def test_the_window_cache_has_the_guard_the_other_three_got(self, store_at, monkeypatch):
        """The fourth cached reader. It spawns node to refill, so its window is the longest."""
        from c4x import store
        store.invalidate()

        def a_change_lands_mid_resolution(session_id):
            store.invalidate()
            return {"segments": [{"window": 200000, "confidence": "segment"}]}

        monkeypatch.setattr(store, "segments_for", a_change_lands_mid_resolution)

        window, confidence = store.session_window("s0-0")

        assert window == 200000, "the caller still gets what was resolved"
        assert "s0-0" not in store._window_cache, (
            "that answer was resolved before the change, so it must not be installed after it")


class TestTheRowHalfIsEnforcedByContent:
    def test_an_in_place_update_during_the_backup_stops_the_delete(
            self, store_at, machine, tmp_path, monkeypatch):
        """The first version of this guard compared row COUNTS, which this write does not change.

        It is also the write the harvester makes most: `setToolResult` filling in an outcome, the
        three `--backfill-*` sweeps whose own success condition is that the count does NOT change,
        and the upserts on a re-read. Those rows were deleted while the backup held the stale copy
        and the report said nothing.
        """
        real = projects.export

        def a_backfill_lands_after_the_backup(project, out_path, app_state=True):
            manifest = real(project, out_path, app_state=app_state)
            con = sqlite3.connect(str(store_at))
            con.execute("UPDATE turns SET parent_uuid = ? WHERE uuid = ?",
                        ("s0-0-tPARENT", "s0-0-t0"))
            con.commit()
            con.close()
            return manifest

        monkeypatch.setattr(projects, "export", a_backfill_lands_after_the_backup)

        with pytest.raises(ValueError, match="the store changed while the backup"):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT parent_uuid FROM turns WHERE uuid = ?",
                               ("s0-0-t0",)).fetchone()[0] == "s0-0-tPARENT"
        finally:
            con.close()

    def test_a_survivor_with_no_working_directory_still_blocks_the_exclusion(
            self, store_at, machine, tmp_path):
        """The shared-transcript join dropped any session whose cwd was never recorded.

        That session is still in the file, and the harvester abandons files, so excluding the
        directory stops capturing it. Its cwd being NULL is exactly why nothing else notices.
        """
        con = sqlite3.connect(str(store_at))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("nocwd-0", "slug-n", None, None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", r"C:\t\s0-0.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert result["excluded_cwds"] == []
        assert result["still_captured"] == [ALPHA]
        assert [entry["with_cwd"] for entry in result["shared_transcripts"]] == [
            "(no working directory recorded)"]

    def test_a_file_written_after_the_capture_is_left_and_named(
            self, store_at, machine, tmp_path, monkeypatch):
        """`appeared_since_backup` names SESSIONS. This names files.

        A transcript written for one of these sessions between the app-state capture and the purge
        is not in the backup, so the purge leaves it, correctly. Nothing said so, and the slug
        directory kept files for a session the store no longer has.
        """
        real = projects.export
        late = machine.base / "s0-0.late-subagent.jsonl"

        def a_write_lands_after_the_capture(project, out_path, app_state=True):
            manifest = real(project, out_path, app_state=app_state)
            late.write_bytes(b"written after the backup was taken")
            return manifest

        monkeypatch.setattr(projects, "export", a_write_lands_after_the_capture)

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert late.exists(), "the backup does not hold it, so the purge must not take it"
        assert [entry["path"] for entry in result["appeared_files"]] == [str(late)]

    def test_a_failure_after_the_rows_are_gone_says_so_and_names_the_backup(
            self, store_at, machine, tmp_path, monkeypatch):
        """The backup exists by then and is the ONLY copy, because the rows are already committed.

        A traceback that does not name it leaves a half-finished delete and nowhere to look. Over
        HTTP it was a 500 with an empty body.
        """
        from c4x import appstate

        def explode(*_args, **_kwargs):
            raise RuntimeError("the file half failed")

        monkeypatch.setattr(appstate, "purge", explode)

        with pytest.raises(projects.AfterTheRowsWereRemoved) as caught:
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert "the file half failed" in str(caught.value)
        assert "THE ROWS ARE ALREADY GONE" in str(caught.value)
        assert str(tmp_path / "backups") in str(caught.value)

    def test_a_value_error_after_the_commit_is_not_reported_as_a_refusal(
            self, store_at, machine, tmp_path, monkeypatch):
        """`except ValueError: raise` said "nothing has been removed" and that was false.

        `appstate.purge` re-reads `~/.claude.json` AFTER the write transaction commits, and
        `read_config` raises ValueError on a file it cannot parse, which Claude Code rewrites
        continuously. Reproduced on this fixture before the fix: every session row and turn gone, a
        permanent exclusion written, all five files still on disk, and the caller told it was a
        refusal, over HTTP a 409 that named no backup. That is worse than either outcome.
        """
        real = projects.export

        def export_then_the_config_tears(project, out_path, app_state=True):
            manifest = real(project, out_path, app_state=app_state)
            machine.config.write_text('{"projects": {', encoding="utf-8")
            return manifest

        monkeypatch.setattr(projects, "export", export_then_the_config_tears)

        with pytest.raises(projects.AfterTheRowsWereRemoved) as caught:
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert "does not parse as JSON" in str(caught.value)
        assert "THE ROWS ARE ALREADY GONE" in str(caught.value)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                               (ALPHA,)).fetchone()[0] == 0, (
                "the rows really are gone, which is the whole point of not calling this a refusal")
        finally:
            con.close()

    def test_a_failure_before_the_rows_go_says_nothing_was_removed(
            self, store_at, machine, tmp_path, monkeypatch):
        """The other half. Telling someone to import the backup here restores what never left."""
        from c4x import store

        def explode():
            raise OSError("the store could not be opened")

        monkeypatch.setattr(store, "write", explode)

        with pytest.raises(RuntimeError) as caught:
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert not isinstance(caught.value, projects.AfterTheRowsWereRemoved)
        assert "Nothing was removed" in str(caught.value)
        assert "can be discarded" in str(caught.value)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                               (ALPHA,)).fetchone()[0] == 3
        finally:
            con.close()

    def test_a_config_that_stops_parsing_is_reported_rather_than_raised(self, monkeypatch):
        """`_drop_config` read the file OUTSIDE its guard, after every other layer was gone.

        `purge` checks the config at the top precisely so a delete refuses before removing
        anything, and that check is what makes this the narrow case: the file has to stop parsing
        BETWEEN that check and this write. Uncaught, it killed the delete and the caller never saw
        the report naming what had already been removed, which is the only record of it.
        """
        from c4x import appstate

        def no_longer_parses():
            raise ValueError("does not parse as JSON any more")

        monkeypatch.setattr(appstate, "read_config", no_longer_parses)
        row = {"kind": appstate.CONFIG, "cwd": ALPHA, "relpath": ALPHA,
               "sha256": "sha256:x", "rebased_sha256": "sha256:x"}

        dropped, kept = appstate._drop_config([(row, ALPHA)])

        assert dropped == []
        assert [entry["key"] for entry in kept] == [ALPHA]
        assert "could not be read" in kept[0]["why"]


class TestALabelThatNamesTwoProjects:
    """The archived label and a real directory of that name are the same string.

    `store.session_rows` builds the archived label by appending the suffix to the working
    directory, and `project_label` returns the bare working directory otherwise, so the two live in
    one string space. `cohort_sessions` matches both halves and the second half of `session_ids`
    adds the real project's below-floor sessions on top, so one confirmed delete took a project the
    user never named: its rows, its transcripts, its tasks, its trust entry, and a permanent
    exclusion. `check_destination` already refuses this on the import side. Nothing consulted it
    here.

    The separator is a LITERAL backslash, matching `archive_only`, and not a `Path` join.
    `Path` renders the running platform's separator, so a join here made the collision exist
    on Windows and not on Linux: the suite was green locally and red on CI.
    """

    @staticmethod
    def nested_cwd():
        from c4x import store
        # A LITERAL SEPARATOR, NOT `Path`. Building it with `Path` renders the platform's own
        # separator, so this produced `P:\Alpha/archived` on Linux while `archive_only`
        # builds the label with a backslash. The two then did not collide, the label was
        # unambiguous, and the guard correctly did not fire: green on Windows, red on CI.
        return ALPHA + "\\" + store.ARCHIVED_SUFFIX

    def a_real_project_named_archived(self, store_path):
        con = sqlite3.connect(str(store_path))
        for n in range(2):
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"nested-{n}", "slug-n", self.nested_cwd(), None, "2.1.229", "cli",
                         "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", f"C:/t/nested-{n}.jsonl"))
        con.commit()
        con.close()
        forget_cached_rows()

    def test_it_is_refused_by_name_rather_than_guessed_at(
            self, store_at, machine, tmp_path, monkeypatch):
        self.a_real_project_named_archived(store_at)
        label = archive_only(monkeypatch, ["s0-0"])
        assert label == self.nested_cwd(), "the collision is the whole point of this fixture"

        with pytest.raises(ValueError, match="more than one working directory"):
            projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        assert (machine.base / "s0-0.jsonl").exists(), "nothing is removed by a refusal"
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            left = con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                               (self.nested_cwd(),)).fetchone()[0]
        finally:
            con.close()
        assert left == 2, "the project the user did not name is untouched"

    def test_the_refusal_happens_before_a_backup_is_written(
            self, store_at, machine, tmp_path, monkeypatch):
        """The rule the config read follows: refuse before writing a copy of both projects."""
        self.a_real_project_named_archived(store_at)
        label = archive_only(monkeypatch, ["s0-0"])
        out_dir = tmp_path / "backups"

        with pytest.raises(ValueError, match="more than one working directory"):
            projects.delete(label, confirm=label, out_dir=out_dir)

        assert not out_dir.exists() or list(out_dir.iterdir()) == []

    def test_the_guard_runs_again_inside_the_transaction(
            self, store_at, machine, tmp_path, monkeypatch):
        """The first check is on a read-only connection BEFORE the export, and cannot be the last.

        The export takes minutes on a large project and a session landing in that window can make
        the label ambiguous after the check has already passed. Bypassing the early check is how
        that window is reproduced deterministically: the manifest still carries two working
        directories, and the delete must refuse on those rather than on what it saw first.
        """
        self.a_real_project_named_archived(store_at)
        label = archive_only(monkeypatch, ["s0-0"])
        monkeypatch.setattr(projects, "one_working_directory", lambda con, project: (None, None))

        with pytest.raises(ValueError, match="more than one working directory"):
            projects.delete(label, confirm=label, out_dir=tmp_path / "backups")

        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            assert con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                               (self.nested_cwd(),)).fetchone()[0] == 2, (
                "the project the user did not name is untouched, by the second guard this time")
        finally:
            con.close()

    # The per-row memory decision under these guards is now UNREACHABLE through `delete`:
    # both guards refuse a label that resolves to two working directories, so a delete never
    # carries more than one. The per-row form is kept in `_belongs_to_a_survivor` as defence
    # rather than as live behaviour, and the test that reached it by defeating the guards was
    # removed with them, because a test that can only pass by disabling two refusals is
    # asserting something the code no longer does.

class TestATranscriptASurvivingSessionIsAlsoIn:
    """One file can hold two sessions' records, and the second one can be another project's.

    The delete already computed this. `shared_transcripts` is read before the rows are removed and
    spent on two decisions, the exclusion and the snapshots, and the file itself was purged anyway,
    so deleting one project took a project the user had not asked about with it. Measured on the
    author's store: 7 transcript files are claimed by more than one session row, 1 of those pairs
    across two working directories.

    The backup still held the file, so the acceptance rule was never broken. The survivor's
    conversation was gone all the same, and only an import of the other project's backup would have
    brought it back.
    """

    @staticmethod
    def share(store_at):
        """Give a session of another project the same transcript file as one of ALPHA's."""
        con = sqlite3.connect(str(store_at))
        con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = 's1-0'",
                    (r"C:\t\s0-0.jsonl",))
        con.commit()
        con.close()
        forget_cached_rows()

    def test_the_shared_file_is_kept_and_the_rest_go(self, store_at, machine, tmp_path):
        self.share(store_at)
        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        assert (machine.base / "s0-0.jsonl").exists(), (
            "a surviving project's conversation was deleted with this one")
        assert not (machine.base / "s0-1.jsonl").exists(), (
            "keeping the shared file kept everything else too")

    def test_the_kept_file_is_named_in_the_report(self, store_at, machine, tmp_path):
        self.share(store_at)
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        kept = result["shared_with_surviving_sessions"]
        assert any(entry["relpath"] == "s0-0.jsonl" for entry in kept), kept
        assert not result["still_here"], (
            "a file this delete decided to keep is not a delete that did not finish")

    def test_nothing_is_kept_when_the_file_is_this_projects_alone(self, store_at, machine,
                                                                  tmp_path):
        """The gate, with the sharing removed: otherwise it keeps every transcript for free."""
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        assert not (machine.base / "s0-0.jsonl").exists()
        assert [entry for entry in result["shared_with_surviving_sessions"]
                if entry["kind"] == "transcript"] == []


class TestTheExclusionsUnitIsTheFile:
    def test_a_directory_sharing_a_transcript_keeps_being_captured(
            self, store_at, machine, tmp_path):
        """`tools/harvest.mjs` abandons a FILE as soon as its first cwd-bearing record is excluded.

        The survivor test asked whether any session still carries that working directory, which is
        a different unit. Measured on the live store, 7 transcript files hold two sessions each and
        2 of them span two working directories, so an exclusion written on the directory test
        stops capturing a project this delete did not touch, silently and permanently.
        """
        shared = "C:/t/shared.jsonl"
        con = sqlite3.connect(str(store_at))
        con.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = ?",
                    (shared, "s0-0"))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                    ("lodger-0", "slug-l", r"P:\Gamma", None, "2.1.229", "cli",
                     "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z", shared))
        con.commit()
        con.close()
        forget_cached_rows()

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert result["excluded_cwds"] == [], "excluding it would drop the lodger's file too"
        assert result["still_captured"] == [ALPHA]
        assert [entry["with_cwd"] for entry in result["shared_transcripts"]] == [r"P:\Gamma"]
        assert result["shared_transcripts"][0]["transcript"] == shared


class TestARefusalIsNotARemoval:
    def test_a_row_the_purge_refused_reaches_the_acceptance_check(
            self, store_at, machine, tmp_path, monkeypatch):
        """A refusal means "I cannot tell", which is not "it is gone".

        `still_present` skipped every refusal, so a carried file the purge would not touch left
        `still_here` empty: the CLI exited 0 and the panel painted green over a delete that had
        not removed it. The refusal is forced here rather than staged on disk, because the natural
        cause, two records under one name, is already gated one layer down and staging it would
        make the backup carry two rows for one relpath.
        """
        from c4x import appstate
        real = appstate.purge_paths

        def refuse_the_record(row, dest_cwd, root=None):
            if row["kind"] == appstate.DESKTOP:
                return [], "forced by the test: this machine cannot resolve that record"
            return real(row, dest_cwd, root)

        monkeypatch.setattr(appstate, "purge_paths", refuse_the_record)

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert (machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE).exists(), (
            "the purge would not touch it, so it is still there")
        assert [entry["relpath"] for entry in result["refused_files"]] == [DESKTOP_FILE]
        assert [entry["relpath"] for entry in result["still_here"]] == [DESKTOP_FILE], (
            "the acceptance check has to see what the purge would not touch, or a delete that "
            "removed less than the backup holds reports success")


    def test_a_machine_with_no_desktop_records_reports_absence_not_a_refusal(self, tmp_path):
        """The other end of the same fix, and the reason the skip existed at all.

        Calling "there is no such record anywhere on this machine" a refusal meant every caller had
        to skip refusals or raise a false alarm on any machine without the desktop app. That skip
        is what hid a genuinely unresolved row from the acceptance check. It is an absence, so it
        is reported as one and `purge` files it under `absent`.
        """
        from c4x import appstate
        row = {"kind": appstate.DESKTOP, "relpath": DESKTOP_FILE, "cwd": ALPHA,
               "sha256": "x", "rebased_sha256": "x"}

        paths, refusal = appstate.purge_paths(row, ALPHA, str(tmp_path / "no-such-root"))

        assert paths == []
        assert refusal is None


class TestTheCachesTellTheTruthAfterAChange:
    def test_a_reader_in_flight_does_not_reinstall_what_a_change_cleared(
            self, store_at, monkeypatch):
        """Clearing is not enough on its own.

        A reader that started before the delete finishes after it and writes the answer it computed
        from the pre delete store into the slot the delete just emptied. The window is the whole
        length of the uncached read, which here is an aggregate over every turn in the store, and
        three processes write this store by design.
        """
        from c4x import store
        real = store._session_rows_uncached

        def a_delete_lands_mid_read():
            df = real()
            store.invalidate()
            return df

        monkeypatch.setattr(store, "_session_rows_uncached", a_delete_lands_mid_read)
        store.invalidate()

        store.session_rows()

        assert store._rows_cache["df"] is None, (
            "that frame describes the store as it was before the change, so installing it puts "
            "the stale answer back behind a fresh timestamp")

    def test_an_import_clears_them_too(self, store_at, machine, tmp_path):
        """Only `delete` did. An import adds sessions and writes transcripts."""
        from c4x import store
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        store.session_rows()
        assert store._rows_cache["df"] is not None, "warm, or this proves nothing"

        projects.import_(result["backup"])

        assert store._rows_cache["df"] is None
        assert store._transcript_cache["ids"] is None


class TestTheGuardCoversEveryReaderThatGotIt:
    """Three cached readers were given the generation guard and one of them was gated.

    One line of three is not a gate for three lines: the other two could be reverted with the suite
    green, and they are the two a session prune reads to decide what to delete.
    """

    def test_the_transcript_scan_does_not_install_what_a_change_cleared(
            self, tmp_path, monkeypatch):
        from c4x import store
        root = tmp_path / ".claude" / "projects" / "P--X"
        root.mkdir(parents=True)
        (root / "abc.jsonl").write_bytes(b"x")
        monkeypatch.setattr(store, "HOME", str(tmp_path))
        store.invalidate()
        real = store.os.scandir
        calls = {"n": 0}

        def a_change_lands_mid_scan(path):
            calls["n"] += 1
            if calls["n"] == 1:
                store.invalidate()
            return real(path)

        monkeypatch.setattr(store.os, "scandir", a_change_lands_mid_scan)

        found = store.transcript_ids()

        assert found == {"abc"}, "the caller still gets what the scan found"
        assert store._transcript_cache["ids"] is None, (
            "that set was scanned before the change, so it must not be installed after it")

    def test_the_desktop_record_scan_does_not_either(self, tmp_path, monkeypatch):
        from c4x import store
        pair = tmp_path / "acct" / "org"
        pair.mkdir(parents=True)
        (pair / "local_x.json").write_text('{"cliSessionId": "s0-0", "isArchived": true}',
                                           encoding="utf-8")
        store.invalidate()
        real = store.read_archived_record

        def a_change_lands_mid_glob(path):
            store.invalidate()
            return real(path)

        monkeypatch.setattr(store, "read_archived_record", a_change_lands_mid_glob)

        found = store.archived_sessions(root=str(tmp_path), ttl=45.0)

        assert found == {"s0-0": True}
        assert store._archived_cache["map"] is None


class TestThePruneKnowsWhereToStop:
    """Each of the two guards, on its own.

    THE END TO END TEST CANNOT CATCH EITHER OF THEM ALONE, and that is a property of the fix rather
    than a flaw in the test: the corrected floor and the ancestry check are independently
    sufficient, so reverting one leaves the other protecting the tree. That is good defence and a
    bad gate, because either line could rot with the suite still green and nothing would be left
    holding the other end. These two assert each line directly.
    """

    def test_a_desktop_records_floor_is_where_it_was_found(self, machine):
        """Not `desktop_dir(sessions_root)`, which is the pair the app writes to NOW.

        `purge_paths` finds the record wherever it actually is. When the two disagree the floor is
        not an ancestor of the directory being walked, the loop that stops on equality never stops,
        and the walk removes the org directory, the account directory and the sessions root.
        """
        from c4x import appstate
        record = machine.sessions / FOREIGN_ACCOUNT / ORG / DESKTOP_FILE

        floor = appstate._prune_floor(appstate.DESKTOP, ALPHA, str(machine.sessions), record)

        assert floor == record.parent
        assert floor != appstate.desktop_dir(str(machine.sessions)), (
            "the fixture files the record under an account this machine is not signed in to, "
            "which is the case that tells the two answers apart")

    def test_a_floor_that_is_not_a_parent_is_not_a_floor(self):
        """The guard, as its own claim, because the loop it protects stops only on equality."""
        from c4x import appstate
        here = Path("C:/a/b/c")

        assert appstate._is_within(here, Path("C:/a/b"))
        assert appstate._is_within(here, here), "a directory is within itself"
        assert appstate._is_within(here, Path("C:/A/B")), "NTFS does not care about case"
        assert not appstate._is_within(here, Path("C:/a/x")), (
            "a sibling is not a parent, and walking up from c would never reach it")
        assert not appstate._is_within(Path("C:/a"), here), "a parent is not within its child"


    def test_a_prune_whose_floor_is_not_a_parent_refuses_instead_of_walking(
            self, store_at, machine, tmp_path, monkeypatch):
        """The guard AT ITS CALL SITE, forced, because a correct floor never reaches it.

        Testing `_is_within` alone was not enough: reverting the call site to `if False:` left the
        helper correct and its unit test green, so the branch that actually protects the tree could
        have been deleted with the suite still passing. Forcing a wrong floor is the only way to
        reach it once the floor beside it is right.
        """
        from c4x import appstate
        real = appstate._prune_floor

        def wrong(kind, dest_cwd, sessions_root=None, path=None):
            if kind == appstate.DESKTOP:
                return machine.sessions / "no-such-account" / "no-such-org"
            return real(kind, dest_cwd, sessions_root, path)

        monkeypatch.setattr(appstate, "_prune_floor", wrong)

        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert machine.sessions.is_dir(), "the sessions root belongs to the machine"
        assert (machine.sessions / FOREIGN_ACCOUNT).is_dir(), "so does the account directory"
        assert [entry["path"] for entry in result["prune_refused"]] == [
            str(machine.sessions / FOREIGN_ACCOUNT / ORG)]
        assert "is not a parent of" in result["prune_refused"][0]["why"]


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
        # THE CONFIG FILE IS IN THE COMPARISON. `machine.config` lives beside `.claude`, not
        # under it, so hashing only those two roots left the one layer a delete EDITS rather than
        # unlinks outside the check: dropping the project key before the export refused was
        # invisible here.
        roots = (machine.claude, machine.sessions, machine.config.parent)
        before = tree(*roots)
        monkeypatch.setattr(projects, "verify",
                            lambda path: (False, ["deliberately refused by the test"]))

        with pytest.raises(RuntimeError):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups")

        assert tree(*roots) == before
        assert ALPHA in config_of(machine)["projects"]

    def test_a_config_that_does_not_parse_stops_the_delete_before_the_backup(
            self, store_at, machine, tmp_path):
        """The defect that cost 26 project entries on the test laptop, as a refusal."""
        before = tree(machine.claude, machine.sessions, machine.config.parent)
        machine.config.write_text("{not json at all", encoding="utf-8")
        out_dir = tmp_path / "backups"

        with pytest.raises(ValueError, match="does not parse as JSON"):
            projects.delete(ALPHA, confirm=ALPHA, out_dir=out_dir)

        assert (machine.base / "s0-0.jsonl").exists()
        # HASHES, NOT A SET OF PATHS. `set(...)` compares the keys and drops every value, so a file
        # rewritten in place during the refusal changed nothing this could see. Only the config is
        # excluded, because this test is the thing that rewrote it.
        after = tree(machine.claude, machine.sessions, machine.config.parent)
        config_path = str(machine.config)
        assert ({k: v for k, v in after.items() if k != config_path}
                == {k: v for k, v in before.items() if k != config_path})
        assert machine.config.read_text(encoding="utf-8") == "{not json at all", (
            "the refusal must not have rewritten the file it refused to parse")
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

    def test_two_rows_landing_on_one_file_are_refused_before_anything_is_removed(self, machine):
        """The fourth refusal, which `restore` has a test for and `purge` did not.

        `tests/test_appstate.py` gates it for restore. purge grew the identical guard on this
        branch and nothing exercised it: mutating `if key in seen:` to `if False:` left the whole
        delete area green. Without it the first row removes the file and the second finds it
        absent, so a delete that took one file reports two removals and calls the second an
        absence, which is the acceptance rule this branch is built on saying the wrong thing.
        """
        from c4x import appstate
        # THE FILE THE ROWS ACTUALLY NAME. Asserting on `s0-0.jsonl` proved nothing here: no row
        # in this test names it, so it could not have been removed either way.
        landed = machine.base / "A.jsonl"
        landed.write_bytes(b"one file, two rows point at it")
        rows = [{"kind": "transcript", "cwd": ALPHA, "relpath": name,
                 "sha256": "sha256:x", "rebased_sha256": "sha256:x"}
                for name in ("A.jsonl", "a.jsonl")]

        with pytest.raises(ValueError, match="two rows resolve to one file"):
            appstate.purge(rows, [ALPHA])

        assert landed.exists(), "it refuses before anything is removed"

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

    def test_a_dry_run_does_not_promise_a_config_key_the_run_keeps(
            self, store_at, machine, tmp_path):
        """The dry run listed every carried key with no entry test at all.

        `_drop_config` drops a key only when the entry under it is the one the backup holds, so a
        dry run that appends every key promised removals the run refuses, in the one layer that is
        edited in place rather than unlinked.
        """
        from c4x import appstate
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        projects.import_(result["backup"])
        rows = projects.app_state_rows(result["backup"], with_blobs=False)
        config = config_of(machine)
        config["projects"][ALPHA] = {"hasTrustDialogAccepted": True, "changedSince": True}
        machine.config.write_text(json.dumps(config), encoding="utf-8")

        report = appstate.purge(rows, [ALPHA], dry_run=True)

        assert report["config_keys"] == [], "it would be kept, so it must not be promised"
        assert [entry["key"] for entry in report["config_kept"]] == [ALPHA]
        assert "not the one the backup holds" in report["config_kept"][0]["why"]

    def test_a_dry_run_does_not_promise_a_removal_the_run_would_refuse(
            self, store_at, machine, tmp_path):
        """It listed every resolved path as `removed` without making the hash test the run makes.

        So a file changed since the backup was written was announced as "this will go" and then
        kept. A dry run whose answer differs from the run is worse than no dry run.
        """
        from c4x import appstate
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path / "backups",
                                 keep_capturing=True)
        forget_cached_rows()
        projects.import_(result["backup"])
        rows = projects.app_state_rows(result["backup"], with_blobs=False)
        (machine.base / "s0-1.jsonl").write_bytes(b"edited since the backup was written")

        report = appstate.purge(rows, [ALPHA], dry_run=True)

        assert "s0-1.jsonl" not in {entry["relpath"] for entry in report["removed"]}
        kept = {entry["relpath"]: entry["why"] for entry in report["kept"]}
        assert "s0-1.jsonl" in kept
        assert "does not hold what is here now" in kept["s0-1.jsonl"]
        assert "s0-0.jsonl" in {entry["relpath"] for entry in report["removed"]}, (
            "the untouched files are still promised")

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

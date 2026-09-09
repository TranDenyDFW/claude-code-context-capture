"""The four layers outside the store, tested where a single character decides whether data is lost.

Every gate here exists because something went wrong, and most of them were written after a
reverted attempt whose three independent reviews all returned FAIL. The named ones:

    a project LABEL used where a working directory belonged, six times, three silently
    the per-session directories missed entirely, leaving 326 MB unbacked
    a write to the Windows device name `nul`, which succeeds and lands nothing
    `../../../../evil.txt`, which resolved to C:\\Users\\evil.txt
    a TEXT mtime, which verified clean and raised after the rows were committed

NOTHING HERE TOUCHES THE REAL MACHINE. `CLAUDE_DIR`, `CONFIG_PATH` and `store.sessions_root` are
monkeypatched onto a temporary directory in the one fixture every test takes, so a gate that
regressed into writing the real `~/.claude` would fail rather than damage anything.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import appstate  # noqa: E402

ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
SOURCE_ACCOUNT = "99999999-9999-4999-8999-999999999999"
SOURCE_ORG = "88888888-8888-4888-8888-888888888888"

SOURCE_CWD = r"P:\Alpha"
DEST_CWD = r"D:\Work\Alpha"
SID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER_SID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
DESKTOP_UUID = "cccccccc-3333-4333-8333-cccccccccccc"


class Machine:
    """One fake machine: a home, a config, and a desktop record directory."""

    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.claude = self.home / ".claude"
        self.config = self.home / ".claude.json"
        self.appdata = tmp_path / "appdata" / "Claude"
        self.sessions = self.appdata / "claude-code-sessions"
        (self.claude / "projects").mkdir(parents=True)
        (self.claude / "tasks").mkdir(parents=True)
        self.sessions.mkdir(parents=True)

    def project_dir(self, cwd):
        return self.claude / "projects" / appstate.slug_for(cwd)

    def write_config(self, payload):
        self.config.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def write_desktop_config(self, account=ACCOUNT, org=ORG):
        (self.appdata / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": account}), encoding="utf-8")
        (self.appdata / "plan-usage-history.json").write_text(
            json.dumps({"version": 2, "samples": [{"t": 1, "org": org}]}), encoding="utf-8")

    def desktop_record(self, account, org, cli_session_id, cwd, uuid=DESKTOP_UUID, **extra):
        folder = self.sessions / account / org
        folder.mkdir(parents=True, exist_ok=True)
        record = {"cliSessionId": cli_session_id, "sessionId": f"local_{uuid}",
                  "cwd": cwd, "originCwd": cwd, "isArchived": False,
                  "title": "a chat", "completedTurns": 3, **extra}
        path = folder / f"local_{uuid}.json"
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
        return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A whole fake machine, with every root in `appstate` pointed at it."""
    box = Machine(tmp_path)
    monkeypatch.setattr(appstate, "CLAUDE_DIR", box.claude)
    monkeypatch.setattr(appstate, "CONFIG_PATH", box.config)
    monkeypatch.setattr(appstate, "sessions_root", lambda: str(box.sessions))
    box.write_config({"oauthAccount": {"do": "not touch"}, "projects": {}})
    box.write_desktop_config()
    return box


@pytest.fixture
def populated(machine):
    """The four shapes a real slug directory holds, plus tasks, config and a desktop record.

    Measured over three real slug directories: 106 `<session id>.jsonl`, 55 `<session id>/`
    directories, 2 `memory/`, 1 `<session id>.desktop-released.json`. All four are here, because a
    capture rule that reaches three of them is the one that left 326 MB behind.
    """
    base = machine.project_dir(SOURCE_CWD)
    (base / SID / "subagents").mkdir(parents=True)
    (base / SID / "tool-results").mkdir(parents=True)
    (base / "memory").mkdir(parents=True)
    (base / f"{SID}.jsonl").write_bytes(b'{"cwd":"P:\\\\Alpha"}\n' * 4)
    (base / f"{SID}.desktop-released.json").write_bytes(b"{}")
    (base / SID / "subagents" / "one.jsonl").write_bytes(b"subagent transcript\n")
    (base / SID / "tool-results" / "big.txt").write_bytes(b"x" * 5000)
    (base / "memory" / "notes.md").write_bytes(b"# project memory\n")
    # A file belonging to a session this export does NOT carry. The slug directory is shared, so
    # this is real and is not ours to move: it must be REPORTED, never captured, never deleted.
    (base / f"{OTHER_SID}.jsonl").write_bytes(b"someone else\n")

    (machine.claude / "tasks" / SID).mkdir(parents=True)
    (machine.claude / "tasks" / SID / "task.json").write_bytes(b'{"task":1}')

    # BOTH SLASH SPELLINGS. 4 of the 91 projects on this machine have two, and an export that
    # carried only the exact string leaves the imported project asking to be trusted again.
    machine.write_config({
        "oauthAccount": {"do": "not touch"},
        "projects": {
            SOURCE_CWD: {"hasTrustDialogAccepted": True, "allowedTools": []},
            SOURCE_CWD.replace("\\", "/"): {"hasTrustDialogAccepted": True},
            r"P:\Unrelated": {"hasTrustDialogAccepted": False},
        }})
    machine.desktop_record(SOURCE_ACCOUNT, SOURCE_ORG, SID, SOURCE_CWD)
    return machine


def capture(machine):
    return appstate.capture([SOURCE_CWD], [SID], sessions_root=str(machine.sessions))


def kinds(rows, kind):
    return sorted(r["relpath"] for r in rows if r["kind"] == kind)


# ---------------------------------------------------------------------------
# The boundary. Six failures of one substitution, and the last one got past `list[str]`.
# ---------------------------------------------------------------------------
class TestTheLabelBoundary:
    def test_a_bare_string_is_refused_where_directories_belong(self):
        """`restore("P:\\Skills\\archived", ...)` wrote to `~/.claude/projects/P` and said ok.

        A `str` is an iterable of `str`, so `list[str]` describes it perfectly and stops nothing.
        This is the check that does, and it is the reason every public function calls `_cwds`.
        """
        with pytest.raises(TypeError, match="LIST of working directories"):
            appstate._cwds(r"P:\Skills\archived")

    def test_a_path_object_is_refused_too(self):
        """A Path is not iterable as directories either, and it is the likelier honest mistake."""
        with pytest.raises(TypeError):
            appstate._cwds(Path(r"P:\Skills"))

    def test_a_list_is_accepted(self):
        assert appstate._cwds([r"P:\Skills", Path(r"P:\Other")]) == [r"P:\Skills", r"P:\Other"]

    def test_capture_and_restore_and_compare_all_guard_the_boundary(self, machine):
        """The guard has to be on EVERY door. Last time one function still took a name."""
        with pytest.raises(TypeError):
            appstate.capture(SOURCE_CWD, [SID])
        with pytest.raises(TypeError):
            appstate.restore([], SOURCE_CWD)
        with pytest.raises(TypeError):
            appstate.compare([], SOURCE_CWD)


class TestTheSlug:
    def test_every_non_alphanumeric_character_becomes_a_hyphen(self):
        assert appstate.slug_for(r"P:\Skills") == "P--Skills"
        assert appstate.slug_for(r"S:\www.sec.gov\Archives") == "S--www-sec-gov-Archives"
        assert appstate.slug_for(r"P:\cSrc\dual_skill_package") == "P--cSrc-dual-skill-package"
        assert appstate.slug_for(r"P:\VSA Agent GP") == "P--VSA-Agent-GP"

    def test_a_different_destination_is_a_different_slug(self):
        """The whole point of the rebase: the destination decides the directory."""
        assert appstate.slug_for(SOURCE_CWD) != appstate.slug_for(DEST_CWD)
        assert appstate.slug_for(DEST_CWD) == "D--Work-Alpha"


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
class TestCapture:
    def test_the_per_session_directory_is_carried_and_not_just_the_jsonl(self, populated):
        """THE 326 MB DEFECT. A capture narrowed to `<sid>.jsonl` fixed a leak and left the
        per-session directories, which hold 22 to 137 MB each, out of the backup entirely."""
        rows, _report = capture(populated)
        carried = kinds(rows, appstate.TRANSCRIPT)
        assert f"{SID}.jsonl" in carried
        assert f"{SID}/subagents/one.jsonl" in carried
        assert f"{SID}/tool-results/big.txt" in carried
        assert f"{SID}.desktop-released.json" in carried

    def test_project_memory_is_carried(self, populated):
        rows, _report = capture(populated)
        assert kinds(rows, appstate.MEMORY) == ["memory/notes.md"]

    def test_a_foreign_session_is_reported_and_never_carried(self, populated):
        """A shared directory means "not mine" is a real category. Silence is the defect."""
        rows, report = capture(populated)
        assert all(OTHER_SID not in r["relpath"] for r in rows)
        assert report["not_carried_files"] == 1
        assert any(OTHER_SID in n["path"] for n in report["not_carried"])

    def test_tasks_are_carried(self, populated):
        rows, _report = capture(populated)
        assert kinds(rows, appstate.TASKS) == [f"{SID}/task.json"]

    def test_both_config_spellings_are_carried_and_nothing_else(self, populated):
        rows, _report = capture(populated)
        carried = kinds(rows, appstate.CONFIG)
        assert carried == sorted([SOURCE_CWD, SOURCE_CWD.replace("\\", "/")])
        assert all("Unrelated" not in c for c in carried)

    def test_the_desktop_record_is_carried_by_cli_session_id(self, populated):
        rows, _report = capture(populated)
        assert kinds(rows, appstate.DESKTOP) == [f"local_{DESKTOP_UUID}.json"]

    def test_a_desktop_record_for_another_session_is_not_carried(self, populated):
        populated.desktop_record(SOURCE_ACCOUNT, SOURCE_ORG, OTHER_SID, SOURCE_CWD,
                                 uuid="dddddddd-4444-4444-8444-dddddddddddd")
        rows, _report = capture(populated)
        assert kinds(rows, appstate.DESKTOP) == [f"local_{DESKTOP_UUID}.json"]

    def test_every_row_carries_a_hash_of_its_own_bytes(self, populated):
        """`written: N` is a coverage number and coverage numbers reward fabrication."""
        rows, _report = capture(populated)
        assert rows
        for row in rows:
            assert row["sha256"] == appstate.sha256_bytes(row["blob"])

    def test_a_symlink_is_named_and_never_followed(self, populated):
        base = populated.project_dir(SOURCE_CWD)
        try:
            os.symlink(str(base / SID), str(base / f"{SID}-link"), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            pytest.skip("this machine cannot create a symlink without elevation")
        rows, report = capture(populated)
        assert all("-link" not in r["relpath"] for r in rows)
        assert any("symlink" in s["why"] for s in report["skipped"])


# ---------------------------------------------------------------------------
# Where a row is allowed to land
# ---------------------------------------------------------------------------
class TestContainment:
    @pytest.mark.parametrize("relpath,why", [
        ("../../../../evil.txt", "a parent reference, which resolved to C:/Users/evil.txt"),
        (r"..\..\evil.txt", "the same escape written the Windows way"),
        ("C:/evil.txt", "an absolute path"),
        (r"C:\evil.txt", "an absolute Windows path"),
        ("/etc/passwd", "an absolute POSIX path"),
        ("nul", "a device name: the write SUCCEEDS and lands nothing"),
        ("NUL.jsonl", "the same device with an extension"),
        ("sub/con", "a device name in a subdirectory, which is still a device"),
        ("a.jsonl.", "a trailing dot, which Windows strips, making it collide"),
        ("a.jsonl ", "a trailing space, same"),
        ("", "empty"),
        (".", "the current directory"),
    ])
    def test_a_path_that_could_escape_or_vanish_is_refused(self, machine, relpath, why):
        row = {"kind": appstate.TRANSCRIPT, "cwd": SOURCE_CWD, "relpath": relpath,
               "mtime": 1.0, "sha256": "x", "rebased_sha256": "x", "blob": b"x"}
        path, refusal = appstate.destination(row, DEST_CWD, str(machine.sessions))
        assert path is None and refusal, f"{relpath!r} was allowed, and it is {why}"

    def test_an_ordinary_path_is_allowed(self, machine):
        row = {"kind": appstate.TRANSCRIPT, "cwd": SOURCE_CWD, "relpath": f"{SID}/subagents/a.json",
               "mtime": 1.0, "sha256": "x", "rebased_sha256": "x", "blob": b"x"}
        path, refusal = appstate.destination(row, DEST_CWD, str(machine.sessions))
        assert refusal is None
        assert path == machine.project_dir(DEST_CWD) / SID / "subagents" / "a.json"


# ---------------------------------------------------------------------------
# Restore, in the order the checks have to happen
# ---------------------------------------------------------------------------
def one_row(**over):
    row = {"kind": appstate.TRANSCRIPT, "cwd": SOURCE_CWD, "relpath": "a.jsonl",
           "mtime": 1_700_000_000.0, "blob": b"hello"}
    row.update(over)
    row.setdefault("sha256", appstate.sha256_bytes(row["blob"])
                   if isinstance(row["blob"], (bytes, bytearray)) else "x")
    row.setdefault("rebased_sha256", row["sha256"])
    return row


class TestRestoreOrder:
    def test_a_non_bytes_blob_is_refused_before_anything_is_written(self, machine):
        """A type check AFTER the write is a half-finished restore that raises."""
        rows = [one_row(relpath="first.jsonl"), one_row(relpath="second.jsonl", blob="text")]
        with pytest.raises(TypeError, match="not bytes"):
            appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(machine.sessions))
        assert not (machine.project_dir(DEST_CWD) / "first.jsonl").exists()

    def test_a_text_mtime_is_refused_before_anything_is_written(self, machine):
        """This one VERIFIED CLEAN and raised after committing rows: SQLite stores what it
        is given."""
        rows = [one_row(relpath="first.jsonl"),
                one_row(relpath="second.jsonl", mtime="2026-01-01")]
        with pytest.raises(TypeError, match="not a number"):
            appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(machine.sessions))
        assert not (machine.project_dir(DEST_CWD) / "first.jsonl").exists()

    def test_two_rows_landing_on_one_file_are_refused_before_the_exists_branch(self, machine):
        """Windows is case-insensitive. Decided after the exists branch, the second row exits as
        "kept existing" and is never named, so an export that carries two versions of a file
        silently keeps whichever the loop reached first."""
        rows = [one_row(relpath="A.jsonl", blob=b"one"), one_row(relpath="a.jsonl", blob=b"two")]
        with pytest.raises(ValueError, match="two rows resolve to one file"):
            appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(machine.sessions))

    def test_a_file_that_does_not_read_back_as_written_is_a_failure(self, machine):
        """The re-hash. Without it `written: 1` is satisfied by a write that landed nothing."""
        rows = [one_row(sha256="sha256:" + "0" * 64)]
        with pytest.raises(RuntimeError, match="does not read back"):
            appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(machine.sessions))


class TestRestore:
    def test_the_bytes_and_the_mtime_both_arrive(self, machine):
        rows = [one_row()]
        report = appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(machine.sessions))
        landed = machine.project_dir(DEST_CWD) / "a.jsonl"
        assert landed.read_bytes() == b"hello"
        assert landed.stat().st_mtime == pytest.approx(1_700_000_000.0, abs=2)
        assert len(report["written"]) == 1

    def test_source_always_wins_and_a_shrinking_replacement_is_named(self, machine):
        """The mirror policy, and the one case it gets wrong, reported rather than prevented.

        A compacted transcript is NEWER and SHORTER, so a true mirror can replace a longer local
        record with a shorter one. That is what the user chose; what is not acceptable is doing it
        quietly."""
        landed = machine.project_dir(DEST_CWD) / "a.jsonl"
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_bytes(b"a much longer existing local transcript")
        report = appstate.restore([one_row()], {SOURCE_CWD: DEST_CWD}, str(machine.sessions))
        assert landed.read_bytes() == b"hello"
        assert [r["relpath"] for r in report["replaced_shorter"]] == ["a.jsonl"]

    def test_nothing_is_ever_deleted(self, machine):
        """The slug directory belongs to every session with that working directory."""
        base = machine.project_dir(DEST_CWD)
        base.mkdir(parents=True, exist_ok=True)
        (base / "someone-elses.jsonl").write_bytes(b"not mine")
        appstate.restore([one_row()], {SOURCE_CWD: DEST_CWD}, str(machine.sessions))
        assert (base / "someone-elses.jsonl").read_bytes() == b"not mine"

    def test_every_working_directory_is_restored_and_not_just_the_first(self, machine):
        """`cwds[0]` wrote the second directory's state under the first one's path and reported
        "merged"."""
        rows = [one_row(cwd=r"P:\One", relpath="one.jsonl", blob=b"1"),
                one_row(cwd=r"P:\Two", relpath="two.jsonl", blob=b"2")]
        appstate.restore(rows, {r"P:\One": r"D:\One", r"P:\Two": r"D:\Two"},
                         str(machine.sessions))
        assert (machine.project_dir(r"D:\One") / "one.jsonl").read_bytes() == b"1"
        assert (machine.project_dir(r"D:\Two") / "two.jsonl").read_bytes() == b"2"

    def test_a_dry_run_writes_nothing_and_names_every_destination(self, machine):
        report = appstate.restore([one_row()], {SOURCE_CWD: DEST_CWD}, str(machine.sessions),
                                  dry_run=True)
        assert report["dry_run"] is True
        assert not (machine.project_dir(DEST_CWD) / "a.jsonl").exists()
        assert report["written"][0]["path"].endswith("a.jsonl")
        assert report["written"][0]["into"] == DEST_CWD


# ---------------------------------------------------------------------------
# The rebase, which is what makes an import land on THIS user rather than the exporter's
# ---------------------------------------------------------------------------
class TestRebase:
    def test_files_land_under_the_destination_slug_and_the_source_one_is_untouched(self, populated):
        """The source slug directory exists here because the FIXTURE built it, so "it is empty"
        would be a false check. What is asserted is that the restore added nothing to it: the
        listing before and the listing after are the same set."""
        rows, _report = capture(populated)
        before = sorted(p.relative_to(populated.claude).as_posix()
                        for p in populated.project_dir(SOURCE_CWD).rglob("*") if p.is_file())
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        after = sorted(p.relative_to(populated.claude).as_posix()
                       for p in populated.project_dir(SOURCE_CWD).rglob("*") if p.is_file())
        assert before == after, "the restore wrote into the SOURCE slug directory"
        assert (populated.project_dir(DEST_CWD) / f"{SID}.jsonl").exists()
        assert (populated.project_dir(DEST_CWD) / SID / "subagents" / "one.jsonl").exists()
        assert (populated.project_dir(DEST_CWD) / "memory" / "notes.md").exists()

    def test_no_file_the_restore_creates_carries_the_source_working_directory(self, populated):
        """The requirement in one assertion: an import does not reproduce the exporter's paths.

        Measured as new-files-only, by differencing the whole fake home before and after, so the
        fixture's own source-side files cannot make this pass or fail by accident.

        Transcript CONTENT still records the source machine's cwd on every line, which is stated
        rather than hidden: rewriting it would be surgery on the conversation and would break the
        byte-identical guarantee the mirror rests on. This is about where things LAND."""
        rows, _report = capture(populated)

        def every_file():
            roots = [populated.claude, populated.sessions]
            return {str(p) for root in roots for p in root.rglob("*") if p.is_file()}

        before = every_file()
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        created = every_file() - before
        assert created, "the restore created nothing at all"
        wrong = appstate.slug_for(SOURCE_CWD)
        offenders = [p for p in created
                     if wrong in p or SOURCE_ACCOUNT in p or SOURCE_ORG in p]
        assert not offenders, f"created under the source machine's own addressing: {offenders}"

    def test_the_config_key_is_the_destination(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        config = json.loads(populated.config.read_text(encoding="utf-8"))
        assert DEST_CWD in config["projects"]
        assert config["projects"][DEST_CWD]["hasTrustDialogAccepted"] is True

    def test_the_config_merge_leaves_every_other_key_alone(self, populated):
        """This file holds the machine's credentials and every other project's settings."""
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        config = json.loads(populated.config.read_text(encoding="utf-8"))
        assert config["oauthAccount"] == {"do": "not touch"}
        assert r"P:\Unrelated" in config["projects"]

    def test_the_desktop_record_names_the_destination(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        landed = populated.sessions / ACCOUNT / ORG / f"local_{DESKTOP_UUID}.json"
        record = json.loads(landed.read_text(encoding="utf-8"))
        assert record["cwd"] == DEST_CWD
        assert record["originCwd"] == DEST_CWD

    def test_the_desktop_record_lands_under_THIS_machine_account_and_org(self, populated):
        """The finding that decides whether the import is visible at all.

        The two directory levels are the machine's account and organisation, not the project's:
        one pair on the real machine holds 171 records across 57 working directories. Reproducing
        the source pair files the record where the destination app never looks."""
        rows, _report = capture(populated)
        # The source pair exists here only because the fixture wrote the ORIGINAL record there, so
        # the check is that the restore CHANGED nothing under it.
        source_pair = populated.sessions / SOURCE_ACCOUNT / SOURCE_ORG
        before = {p.name: p.read_bytes() for p in source_pair.glob("*.json")}
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert (populated.sessions / ACCOUNT / ORG / f"local_{DESKTOP_UUID}.json").exists()
        after = {p.name: p.read_bytes() for p in source_pair.glob("*.json")}
        assert after == before, "the restore wrote under the SOURCE machine's account and org"

    def test_everything_else_in_the_record_survives_byte_for_byte(self, populated):
        rows, _report = capture(populated)
        source = json.loads((populated.sessions / SOURCE_ACCOUNT / SOURCE_ORG /
                             f"local_{DESKTOP_UUID}.json").read_text(encoding="utf-8"))
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        landed = json.loads((populated.sessions / ACCOUNT / ORG /
                             f"local_{DESKTOP_UUID}.json").read_text(encoding="utf-8"))
        for field in source:
            if field in appstate.DESKTOP_CWD_FIELDS:
                continue
            assert landed[field] == source[field], f"{field} was not carried unchanged"
        assert landed["cliSessionId"] == SID

    def test_with_no_destination_given_everything_lands_where_it_came_from(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {}, str(populated.sessions))
        assert (populated.project_dir(SOURCE_CWD) / f"{SID}.jsonl").exists()
        config = json.loads(populated.config.read_text(encoding="utf-8"))
        assert SOURCE_CWD in config["projects"]


class TestWhichAppDataTheAppActuallyUses:
    """A Store install redirects `%APPDATA%`, and the two names are not always one directory.

    FOUND ON THE TEST LAPTOP, and it would have made the whole feature untrue there: reading
    `%APPDATA%\\Claude` saw 1 record where the app's own container held 19, so an imported record
    would have been filed where the app never looks and `archived_sessions` was already reading one
    twentieth of the sessions.
    """

    def roots(self, tmp_path, monkeypatch, packaged_records=0, packaged_config=False,
              roaming_records=0, roaming_config=False):
        from c4x import store
        roaming = tmp_path / "Roaming" / "Claude"
        packaged = (tmp_path / "Local" / "Packages" / "Claude_abc" / "LocalCache" / "Roaming"
                    / "Claude")
        for base, records, config in ((roaming, roaming_records, roaming_config),
                                      (packaged, packaged_records, packaged_config)):
            folder = base / "claude-code-sessions" / "acct" / "org"
            folder.mkdir(parents=True)
            for n in range(records):
                (folder / f"local_{n}.json").write_text("{}", encoding="utf-8")
            if config:
                (base / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.delenv("C4X_SESSIONS_ROOT", raising=False)
        return store, roaming, packaged

    def test_the_packaged_container_wins_when_it_is_the_one_holding_the_state(self, tmp_path,
                                                                             monkeypatch):
        """The laptop's arrangement, in numbers taken from it: 19 records and a config against 1."""
        store, _roaming, packaged = self.roots(
            tmp_path, monkeypatch, packaged_records=19, packaged_config=True, roaming_records=1)
        assert store.sessions_root() == str(packaged / "claude-code-sessions")

    def test_roaming_wins_when_it_is_the_one_holding_the_state(self, tmp_path, monkeypatch):
        store, roaming, _packaged = self.roots(
            tmp_path, monkeypatch, roaming_records=19, roaming_config=True, packaged_records=1)
        assert store.sessions_root() == str(roaming / "claude-code-sessions")

    def test_a_config_beside_the_records_outranks_a_bigger_pile_without_one(self, tmp_path,
                                                                           monkeypatch):
        """Record count alone is the wrong signal: an abandoned install can hold more of them."""
        store, _roaming, packaged = self.roots(
            tmp_path, monkeypatch, packaged_records=2, packaged_config=True, roaming_records=40)
        assert store.sessions_root() == str(packaged / "claude-code-sessions")

    def test_with_no_evidence_anywhere_it_is_the_roaming_name(self, tmp_path, monkeypatch):
        store, roaming, _packaged = self.roots(tmp_path, monkeypatch)
        assert store.sessions_root() == str(roaming / "claude-code-sessions")

    def test_one_directory_under_two_names_is_counted_once(self, tmp_path, monkeypatch):
        """THIS MACHINE's arrangement: both paths return the same device and inode.

        The candidate list is patched to name it twice rather than building a junction, which
        needs privileges the suite does not have. What is under test is the collapse, and the
        answer must be a real single root rather than a crash or a duplicate.
        """
        from c4x import store
        base = tmp_path / "Roaming" / "Claude"
        (base / "claude-code-sessions").mkdir(parents=True)
        (base / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.delenv("C4X_SESSIONS_ROOT", raising=False)
        monkeypatch.setattr(store, "_claude_appdata_candidates",
                            lambda: [str(base), str(base)])
        assert store.sessions_root() == str(base / "claude-code-sessions")

    def test_an_explicit_override_wins_over_both(self, tmp_path, monkeypatch):
        from c4x import store
        monkeypatch.setenv("C4X_SESSIONS_ROOT", str(tmp_path / "elsewhere"))
        assert store.sessions_root() == str(tmp_path / "elsewhere")


class TestTheDesktopPair:
    def test_the_newest_record_on_disk_decides(self, machine):
        old = machine.desktop_record(ACCOUNT, "33333333-3333-4333-8333-333333333333", SID,
                                     SOURCE_CWD, uuid="eeeeeeee-5555-4555-8555-eeeeeeeeeeee")
        new = machine.desktop_record(ACCOUNT, ORG, OTHER_SID, SOURCE_CWD,
                                     uuid="ffffffff-6666-4666-8666-ffffffffffff")
        os.utime(old, (1_600_000_000, 1_600_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        pair = appstate.desktop_pair(str(machine.sessions))
        assert (pair["account"], pair["org"]) == (ACCOUNT, ORG)

    def test_a_stale_record_under_ANOTHER_account_does_not_win(self, machine):
        """The case the first version of this file could not see, found by mutation testing.

        Every other test here puts its records under the signed-in account, so removing the
        account filter from `desktop_pair` changed no answer and the deliberate defect SURVIVED.
        This is the arrangement the real machine is actually in: a second account whose newest
        record is 12 days old, sitting beside the one in use. Filing an import under it would be
        invisible to the signed-in user while every count said it had been written.
        """
        stale = machine.desktop_record(SOURCE_ACCOUNT, SOURCE_ORG, SID, SOURCE_CWD,
                                       uuid="eeeeeeee-5555-4555-8555-eeeeeeeeeeee")
        os.utime(stale, (1_900_000_000, 1_900_000_000))       # NEWER than anything of ours
        pair = appstate.desktop_pair(str(machine.sessions))
        assert pair["account"] == ACCOUNT, (
            "a record under another account decided where this machine files its sessions")
        assert pair["org"] == ORG

    def test_with_no_records_the_config_answers(self, machine):
        pair = appstate.desktop_pair(str(machine.sessions))
        assert (pair["account"], pair["org"]) == (ACCOUNT, ORG)
        assert "no records yet" in pair["source"]

    def test_with_no_evidence_at_all_it_refuses_rather_than_guessing(self, machine):
        (machine.appdata / "config.json").unlink()
        (machine.appdata / "plan-usage-history.json").unlink()
        assert appstate.desktop_pair(str(machine.sessions)) is None
        row = {"kind": appstate.DESKTOP, "cwd": SOURCE_CWD, "relpath": "local_x.json",
               "mtime": 1.0, "sha256": "x", "rebased_sha256": "x", "blob": b"{}"}
        path, refusal = appstate.destination(row, DEST_CWD, str(machine.sessions))
        assert path is None and "would not appear in the app" in refusal

    def test_a_desktop_record_carrying_a_directory_is_refused(self, machine):
        """The relative path is a FILENAME. Anything else is an export built by hand."""
        row = {"kind": appstate.DESKTOP, "cwd": SOURCE_CWD, "relpath": "acct/org/local_x.json",
               "mtime": 1.0, "sha256": "x", "rebased_sha256": "x", "blob": b"{}"}
        path, refusal = appstate.destination(row, DEST_CWD, str(machine.sessions))
        assert path is None and "filename alone" in refusal


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------
class TestCompare:
    def test_a_clean_round_trip_is_ok(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert result["ok"], result

    def test_a_deleted_file_is_reported_missing(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        (populated.project_dir(DEST_CWD) / f"{SID}.jsonl").unlink()
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert not result["ok"]
        assert [m["relpath"] for m in result["missing"]] == [f"{SID}.jsonl"]

    def test_an_edited_file_is_reported_as_differing(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        (populated.project_dir(DEST_CWD) / f"{SID}.jsonl").write_bytes(b"tampered")
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert not result["ok"]
        assert [d["relpath"] for d in result["differs"]] == [f"{SID}.jsonl"]

    def test_an_edited_desktop_record_is_reported_even_though_it_was_rebased(self, populated):
        """A rebased record still has to be provable: everything but the two cwd fields is
        hashed."""
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        landed = populated.sessions / ACCOUNT / ORG / f"local_{DESKTOP_UUID}.json"
        record = json.loads(landed.read_text(encoding="utf-8"))
        record["title"] = "someone renamed this"
        landed.write_text(json.dumps(record), encoding="utf-8")
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert not result["ok"]
        assert any(d["kind"] == appstate.DESKTOP for d in result["differs"])

    def test_changing_only_the_cwd_of_a_record_is_NOT_a_difference(self, populated):
        """The other direction: the rebase itself must not read as corruption."""
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert not result["differs"], result["differs"]

    def test_a_file_the_export_does_not_carry_is_reported_as_extra_and_left_alone(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        intruder = populated.project_dir(DEST_CWD) / "not-in-the-export.jsonl"
        intruder.write_bytes(b"someone else's session")
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert any("not-in-the-export" in e for e in result["extra"])
        assert intruder.exists()

    def test_a_missing_config_entry_is_reported(self, populated):
        rows, _report = capture(populated)
        appstate.restore(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        config = json.loads(populated.config.read_text(encoding="utf-8"))
        del config["projects"][DEST_CWD]
        populated.write_config(config)
        result = appstate.compare(rows, {SOURCE_CWD: DEST_CWD}, str(populated.sessions))
        assert not result["ok"]
        assert any(d["kind"] == appstate.CONFIG for d in result["differs"])


def test_the_module_self_test_passes():
    assert appstate.self_test() == 0

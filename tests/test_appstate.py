"""Claude Code's own state: capturing it, restoring it, and refusing what an export should not do.

THE DEFECT THIS WHOLE MODULE EXISTS FOR. `export` carried a complete copy of what c4x had PARSED
and nothing of what Claude Code itself holds, so an imported project appeared in c4x and was
invisible to the desktop app. Measured on a second machine before the change: an import touched
exactly one file, `context.db`.

THE DEFECT THAT KEPT RECURRING WHILE FIXING IT. `session_rows()` appends `\\archived` to a project
label once the desktop app has archived the chat, and that LABEL was threaded into three functions
that key on the WORKING DIRECTORY. It went wrong four times, three of them silently: an export that
carried no transcript, an mtime of None that quietly turned newer-wins into never-overwrite, and a
restore that wrote into `P--Skills-archived/`, a directory Claude Code will never read. Only the
last was visible, and only by diffing the target machine's filesystem. Most of the gates below
exist for that one substitution.
"""
import json
import os
from pathlib import Path

import pytest

from c4x import appstate


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A whole fake Claude Code state, so nothing here can touch the real one."""
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    desktop = tmp_path / "desktop-sessions"
    (desktop / "ws" / "inner").mkdir(parents=True)
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({
        "oauthAccount": {"do": "not touch"},
        "projects": {r"P:\Other": {"hasTrustDialogAccepted": True}},
    }), encoding="utf-8")

    monkeypatch.setattr(appstate, "CLAUDE_DIR", claude)
    monkeypatch.setattr(appstate, "CONFIG_PATH", config)
    monkeypatch.setattr("c4x.store.sessions_root", lambda: str(desktop))
    return {"claude": claude, "desktop": desktop, "config": config}


def test_the_slug_matches_what_claude_code_actually_uses():
    """Verified against a real directory, not inferred from the rule.

    `claude project purge --dry-run "P:\\Skills"` names `~/.claude/projects/P--Skills`.
    """
    assert appstate.slug_for(r"P:\Skills") == "P--Skills"
    assert appstate.slug_for(r"P:\ClaudeExt\ccxe\c4x") == "P--ClaudeExt-ccxe-c4x"


class TestTheLabelThatIsNotAPath:
    """The substitution that went wrong four times, gated at each place it went wrong."""

    def test_capture_uses_the_sessions_cwd_not_the_name_it_was_given(self, sandbox, monkeypatch):
        """THE FIRST FAILURE: an export keyed on a page label carried no transcript at all.

        The caller passes `P:\\Skills\\archived` because that is what the page calls the project.
        Claude Code keys its state on `P:\\Skills`. Asking for the label's transcript folder gets
        `P--Skills-archived`, which does not exist, so the capture came back with the desktop
        record alone and said nothing was wrong.
        """
        (sandbox["claude"] / "projects" / "P--Skills").mkdir()
        (sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl").write_bytes(b"real transcript")
        items = appstate.capture([r"P:\Skills"], session_ids=["s"])
        got = appstate.summarise(items)
        assert got[appstate.TRANSCRIPT]["files"] == 1, "the transcript was lost"
        assert got[appstate.TRANSCRIPT]["bytes"] == len(b"real transcript")

    def test_every_captured_file_carries_the_mtime_it_was_read_with(self, sandbox, monkeypatch):
        """THE SECOND FAILURE, and the quietest.

        The mtime was looked up afterwards by a second function that took the LABEL again, so it
        resolved a directory that does not exist and recorded None. Nothing raised. The caller's
        "take the newer file" policy then had nothing to compare and silently became
        "never overwrite", which is a different policy that looks like success.
        """
        (sandbox["claude"] / "projects" / "P--Skills").mkdir()
        (sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl").write_bytes(b"x")
        items = appstate.capture([r"P:\Skills"], session_ids=["s"])
        readable = [(k, p, m, b) for k, p, m, b in items if b is not None]
        assert readable, "nothing was captured, so this gate proved nothing"
        missing = [p for _k, p, m, _b in readable if m is None]
        assert not missing, f"captured with no mtime, so newer-wins cannot work: {missing}"

    def test_restore_puts_the_transcript_where_claude_code_reads_it(self, sandbox):
        """THE THIRD FAILURE, the only one that was visible, and only by diffing a filesystem.

        A restore given the label wrote into `P--Skills-archived/`. The file was on disk, the
        report said written, and Claude Code will never look in that directory.
        """
        items = [(appstate.TRANSCRIPT, "s.jsonl", 1.0, b"hello")]
        appstate.restore([r"P:\Skills"], items)
        assert (sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl").read_bytes() == b"hello"
        assert not (sandbox["claude"] / "projects" / "P--Skills-archived").exists()


class TestAnExportIsUntrustedInput:
    """It arrives from another machine and this code writes the paths it names."""

    @pytest.mark.parametrize("hostile", ["../../../evil.txt", "a/../../../evil.txt",
                                         "C:/Windows/evil.txt", ".."])
    def test_a_path_that_escapes_its_root_is_refused(self, sandbox, hostile):
        """Arbitrary file write out of a data file.

        Measured before the guard: `../../../../evil.txt` under the transcript root resolved to
        `C:\\Users\\evil.txt`. Adding the restore capability is what made a stored string dangerous;
        the string was inert while nothing wrote it.
        """
        report = appstate.restore([r"P:\Skills"],
                                 [(appstate.TRANSCRIPT, hostile, 1.0, b"pwned")])
        assert report["rejected_paths"], f"{hostile} was not refused"
        assert report["written"] == 0
        assert not (Path(sandbox["claude"]).parent / "evil.txt").exists()

    def test_an_ordinary_nested_path_is_still_allowed(self, sandbox):
        """The guard must not be a blanket refusal: `memory/` is a real subdirectory."""
        report = appstate.restore([r"P:\Skills"],
                                 [(appstate.TRANSCRIPT, "memory/notes.md", 1.0, b"keep")])
        assert report["written"] == 1 and not report["rejected_paths"]
        assert (sandbox["claude"] / "projects" / "P--Skills"
                / "memory" / "notes.md").read_bytes() == b"keep"


class TestWhenTheFileIsAlreadyThere:
    """The policy is newer-wins, chosen deliberately, including the case it gets wrong."""

    def _existing(self, sandbox, body, mtime):
        d = sandbox["claude"] / "projects" / "P--Skills"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "s.jsonl"
        f.write_bytes(body)
        os.utime(f, (mtime, mtime))
        return f

    def test_the_newer_incoming_file_wins(self, sandbox):
        f = self._existing(sandbox, b"old", 1000.0)
        report = appstate.restore([r"P:\Skills"],
                                 [(appstate.TRANSCRIPT, "s.jsonl", 2000.0, b"newer")])
        assert f.read_bytes() == b"newer"
        assert report["replaced"] == ["s.jsonl"]

    def test_an_older_incoming_file_is_kept_out(self, sandbox):
        f = self._existing(sandbox, b"current", 2000.0)
        report = appstate.restore([r"P:\Skills"],
                                 [(appstate.TRANSCRIPT, "s.jsonl", 1000.0, b"stale")])
        assert f.read_bytes() == b"current"
        assert report["kept_existing"] == ["s.jsonl"]

    def test_a_newer_but_shorter_replacement_is_reported(self, sandbox):
        """THE CASE THE CHOSEN POLICY GETS WRONG, surfaced rather than prevented.

        A transcript can be rewritten IN PLACE and come out smaller: compaction writes a sibling
        temp file and renames it over the original, so a compacted transcript is newer AND shorter
        and newer-wins replaces a complete record with a compacted one. That is a real way to lose
        conversation and it is not detectable from the bytes, so every shrinking replacement is
        named in the report.
        """
        self._existing(sandbox, b"a much longer original transcript", 1000.0)
        report = appstate.restore([r"P:\Skills"],
                                 [(appstate.TRANSCRIPT, "s.jsonl", 2000.0, b"short")])
        assert report["newer_but_shorter"], "a shrinking replacement was not reported"
        assert "s.jsonl" in report["newer_but_shorter"][0]


class TestTheConfigEntry:
    """~/.claude.json holds the OAuth session and every other project, not just this one."""

    def test_the_trust_entry_comes_back(self, sandbox):
        entry = json.dumps({"hasTrustDialogAccepted": True}).encode("utf-8")
        appstate.restore([r"P:\Skills"], [(appstate.CONFIG, "claude.json", 1.0, entry)])
        data = json.loads(sandbox["config"].read_text(encoding="utf-8"))
        assert data["projects"][r"P:\Skills"]["hasTrustDialogAccepted"] is True

    def test_nothing_else_in_that_file_is_disturbed(self, sandbox):
        """The gate that stops a restore costing the login.

        Written whole rather than merged, this file loses the OAuth session and 90 other projects'
        trust state. The blast radius of getting it wrong is the entire install, not this project.
        """
        entry = json.dumps({"hasTrustDialogAccepted": True}).encode("utf-8")
        appstate.restore([r"P:\Skills"], [(appstate.CONFIG, "claude.json", 1.0, entry)])
        data = json.loads(sandbox["config"].read_text(encoding="utf-8"))
        assert data["oauthAccount"] == {"do": "not touch"}
        assert data["projects"][r"P:\Other"]["hasTrustDialogAccepted"] is True


class TestPurgeIsDelegated:
    """The footprint is not stable, so it is not reimplemented here."""

    def test_it_calls_claude_project_purge_rather_than_sweeping_by_hand(self, monkeypatch):
        """This build deletes TWO items where the documentation describes five.

        A sweep written from the docs would delete nothing extra today and would be wrong silently
        after any release that moves the footprint. The command ships with the build, so it is the
        only thing that knows its own answer.
        """
        seen = {}

        class Done:
            returncode = 0
            stdout = "purged"
            stderr = ""

        monkeypatch.setattr(appstate, "has_state", lambda cwd: True)
        monkeypatch.setattr(appstate.shutil, "which", lambda name: "/fake/claude")
        monkeypatch.setattr(appstate.subprocess, "run",
                            lambda argv, **kw: (seen.update(argv=argv), Done())[1])
        out = appstate.purge(r"P:\Skills")
        assert out["ran"] and out["code"] == 0
        assert seen["argv"][1:4] == ["project", "purge", r"P:\Skills"]
        assert "--yes" in seen["argv"]

    def test_a_dry_run_never_passes_yes(self, monkeypatch):
        seen = {}

        class Done:
            returncode = 0
            stdout = "plan"
            stderr = ""

        monkeypatch.setattr(appstate, "has_state", lambda cwd: True)
        monkeypatch.setattr(appstate.shutil, "which", lambda name: "/fake/claude")
        monkeypatch.setattr(appstate.subprocess, "run",
                            lambda argv, **kw: (seen.update(argv=argv), Done())[1])
        appstate.purge(r"P:\Skills", dry_run=True)
        assert "--dry-run" in seen["argv"] and "--yes" not in seen["argv"]

    def test_a_missing_cli_is_reported_not_swallowed(self, monkeypatch):
        """"Could not run" is a different answer from "nothing to delete" and must say so."""
        monkeypatch.setattr(appstate, "has_state", lambda cwd: True)
        monkeypatch.setattr(appstate.shutil, "which", lambda name: None)
        out = appstate.purge(r"P:\Skills")
        assert out["ran"] is False and "not on PATH" in out["reason"]


class TestWhatAnIndependentReviewFound:
    """Every FAIL from the review, gated where it broke. None of these was author-found."""

    def test_the_tasks_directory_is_captured_because_the_purge_deletes_it(self, sandbox):
        """DELETE DESTROYED STATE ITS OWN VERIFIED BACKUP DID NOT CONTAIN.

        The footprint was asserted to be two items after measuring exactly ONE project.
        `P:\\ClaudeExt\\QuestionExtension` prints FOUR, the extra two being
        `~/.claude/tasks/<session id>` directories. Delegating to `claude project purge` removed
        those; `capture` never collected them. 15 files for one project, measured.
        """
        t = sandbox["claude"] / "tasks" / "s"
        t.mkdir(parents=True)
        (t / "task.json").write_bytes(b"work in progress")
        items = appstate.capture([r"P:\Skills"], session_ids=["s"])
        got = appstate.summarise(items)
        assert got[appstate.TASKS]["files"] == 1, (
            "the purge deletes ~/.claude/tasks and the backup does not carry it")

    def test_an_empty_path_cannot_be_written_over_the_project_directory(self, sandbox):
        """It passed containment, and `restore` wrote a FILE over the transcript directory.

        The report said `written: 1` with no rejection. Every later restore into that project then
        raised FileExistsError forever, so one malformed row bricked the destination.
        """
        d = sandbox["claude"] / "projects" / "P--Skills"
        d.mkdir(parents=True)
        for bad in ("", ".", "   "):
            report = appstate.restore([r"P:\Skills"],
                                      [(appstate.TRANSCRIPT, bad, 1.0, b"x")])
            assert report["rejected_paths"], f"{bad!r} was accepted"
        assert d.is_dir(), "the project directory was replaced by a file"

    def test_a_blob_that_is_not_bytes_is_reported_not_raised(self, sandbox):
        """A corrupt or hostile export must not abandon the restore halfway through."""
        report = appstate.restore([r"P:\Skills"], [
            (appstate.TRANSCRIPT, "a.jsonl", 1.0, "not bytes"),
            (appstate.TRANSCRIPT, "b.jsonl", 1.0, b"good"),
        ])
        assert report["failed"] and "not bytes" in report["failed"][0]
        assert report["written"] == 1, "one bad row stopped the rest of the restore"

    def test_a_directory_where_a_file_should_go_is_reported_not_raised(self, sandbox):
        (sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl").mkdir(parents=True)
        report = appstate.restore([r"P:\Skills"],
                                  [(appstate.TRANSCRIPT, "s.jsonl", 1.0, b"x")])
        assert report["failed"] and "directory" in report["failed"][0]

    def test_the_restored_file_keeps_the_mtime_it_was_exported_with(self, sandbox):
        """Otherwise newer-wins is one-shot.

        A restore that stamps every file with `now` makes the SECOND import of a corrected export
        compare against a file newer than itself, so it silently does nothing and reports the
        collision as kept.
        """
        appstate.restore([r"P:\Skills"], [(appstate.TRANSCRIPT, "s.jsonl", 4242.0, b"x")])
        f = sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl"
        assert abs(f.stat().st_mtime - 4242.0) < 2, (
            "the exported mtime was not preserved, so a re-import cannot compare")

    def test_capture_takes_only_the_sessions_asked_for(self, sandbox):
        """THE EXPORT LEAKED ACROSS PAGE PROJECTS AND HELD THE FOLDER IN RAM.

        The transcript folder is shared by every session with that working directory. Walking it
        meant exporting one archived session carried all 1,445 files and 725 MB of its 67-session
        sibling, accumulated as blobs in memory inside the web app's export route. A transcript is
        named `<session id>.jsonl`, so the right ones can be named exactly.
        """
        d = sandbox["claude"] / "projects" / "P--Skills"
        d.mkdir(parents=True)
        (d / "mine.jsonl").write_bytes(b"asked for")
        (d / "someone-elses.jsonl").write_bytes(b"NOT asked for")
        items = appstate.capture([r"P:\Skills"], session_ids=["mine"])
        paths = [p for k, p, _m, _b in items if k == appstate.TRANSCRIPT]
        assert paths == ["mine.jsonl"], f"carried a session nobody asked for: {paths}"

    def test_purge_launches_nothing_for_a_project_with_no_state(self, sandbox, monkeypatch):
        """THE SUITE RAN A REAL DESTRUCTIVE COMMAND 17 TIMES PER RUN.

        `tests/test_projects.py` calls `projects.delete()` against the fixture name `P:\\Alpha`,
        and every one of those fired a real `claude project purge P:\\Alpha --yes` on the
        developer's own machine. Harmless only because no project of that name existed. Checking
        for state first is also the right answer for a real caller, since the command exits 1 on a
        path it holds nothing for.
        """
        fired = []
        monkeypatch.setattr(appstate.shutil, "which", lambda name: "/fake/claude")
        monkeypatch.setattr(appstate.subprocess, "run", lambda argv, **kw: fired.append(argv))
        out = appstate.purge(r"P:\NothingHere")
        assert out["ran"] is False and "no state" in out["reason"]
        assert not fired, "a subprocess was launched for a project with no state"

    def test_a_write_that_did_not_land_is_not_counted_as_written(self, sandbox):
        """A COVERAGE NUMBER THAT REWARDS FABRICATION.

        `write_bytes` to a Windows device name such as `nul` or `con` SUCCEEDS and swallows the
        content. Measured: size 0 on disk, reads back empty, absent from the directory listing,
        while the report said written with the full byte count. Reporting what LANDED rather than
        what was attempted is the only version of that number worth printing.
        """
        report = appstate.restore([r"P:\Skills"],
                                  [(appstate.TRANSCRIPT, "nul", 1.0, b"swallowed")])
        assert report["written"] == 0, "a write that landed nothing was counted"
        assert report["failed"], "and it was not reported either"

    def test_two_rows_landing_on_one_file_are_reported(self, sandbox):
        """Windows compares paths case-insensitively.

        `a.jsonl` and `A.JSONL` are two rows in an export and one file on disk, so the second
        silently overwrote the first and the report counted two successes.
        """
        report = appstate.restore([r"P:\Skills"], [
            (appstate.TRANSCRIPT, "a.jsonl", 1.0, b"first"),
            (appstate.TRANSCRIPT, "A.JSONL", 1.0, b"second"),
        ])
        assert report["written"] == 1
        assert any("collides" in f for f in report["failed"]), report["failed"]

    def test_a_symlink_is_not_carried_into_the_export(self, sandbox):
        """Following one puts whatever it points at into a file that travels to another machine."""
        d = sandbox["claude"] / "projects" / "P--Skills" / "memory"
        d.mkdir(parents=True)
        outside = sandbox["claude"].parent / "secret.txt"
        outside.write_bytes(b"not this project's content")
        try:
            (d / "link.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("this machine does not allow creating symlinks")
        items = appstate.capture([r"P:\Skills"], session_ids=[])
        carried = [b for _k, _p, _m, b in items if b is not None]
        assert b"not this project's content" not in carried, "a symlink's target was exported"

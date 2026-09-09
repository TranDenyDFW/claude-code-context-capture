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
        monkeypatch.setattr(appstate, "cwds_for", lambda ids: [r"P:\Skills"])

        items = appstate.capture(r"P:\Skills\archived", session_ids=["s"])
        got = appstate.summarise(items)
        assert got["transcripts"] == 1, "the label was used as a path and the transcript was lost"
        assert got["transcript_bytes"] == len(b"real transcript")

    def test_every_captured_file_carries_the_mtime_it_was_read_with(self, sandbox, monkeypatch):
        """THE SECOND FAILURE, and the quietest.

        The mtime was looked up afterwards by a second function that took the LABEL again, so it
        resolved a directory that does not exist and recorded None. Nothing raised. The caller's
        "take the newer file" policy then had nothing to compare and silently became
        "never overwrite", which is a different policy that looks like success.
        """
        (sandbox["claude"] / "projects" / "P--Skills").mkdir()
        (sandbox["claude"] / "projects" / "P--Skills" / "s.jsonl").write_bytes(b"x")
        monkeypatch.setattr(appstate, "cwds_for", lambda ids: [r"P:\Skills"])

        items = appstate.capture(r"P:\Skills\archived", session_ids=["s"])
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
        appstate.restore(r"P:\Skills", items)
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
        report = appstate.restore(r"P:\Skills",
                                 [(appstate.TRANSCRIPT, hostile, 1.0, b"pwned")])
        assert report["rejected_paths"], f"{hostile} was not refused"
        assert report["written"] == 0
        assert not (Path(sandbox["claude"]).parent / "evil.txt").exists()

    def test_an_ordinary_nested_path_is_still_allowed(self, sandbox):
        """The guard must not be a blanket refusal: `memory/` is a real subdirectory."""
        report = appstate.restore(r"P:\Skills",
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
        report = appstate.restore(r"P:\Skills",
                                 [(appstate.TRANSCRIPT, "s.jsonl", 2000.0, b"newer")])
        assert f.read_bytes() == b"newer"
        assert report["replaced"] == ["s.jsonl"]

    def test_an_older_incoming_file_is_kept_out(self, sandbox):
        f = self._existing(sandbox, b"current", 2000.0)
        report = appstate.restore(r"P:\Skills",
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
        report = appstate.restore(r"P:\Skills",
                                 [(appstate.TRANSCRIPT, "s.jsonl", 2000.0, b"short")])
        assert report["newer_but_shorter"], "a shrinking replacement was not reported"
        assert "s.jsonl" in report["newer_but_shorter"][0]


class TestTheConfigEntry:
    """~/.claude.json holds the OAuth session and every other project, not just this one."""

    def test_the_trust_entry_comes_back(self, sandbox):
        entry = json.dumps({"hasTrustDialogAccepted": True}).encode("utf-8")
        appstate.restore(r"P:\Skills", [(appstate.CONFIG, "claude.json", 1.0, entry)])
        data = json.loads(sandbox["config"].read_text(encoding="utf-8"))
        assert data["projects"][r"P:\Skills"]["hasTrustDialogAccepted"] is True

    def test_nothing_else_in_that_file_is_disturbed(self, sandbox):
        """The gate that stops a restore costing the login.

        Written whole rather than merged, this file loses the OAuth session and 90 other projects'
        trust state. The blast radius of getting it wrong is the entire install, not this project.
        """
        entry = json.dumps({"hasTrustDialogAccepted": True}).encode("utf-8")
        appstate.restore(r"P:\Skills", [(appstate.CONFIG, "claude.json", 1.0, entry)])
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

        monkeypatch.setattr(appstate.shutil, "which", lambda name: "/fake/claude")
        monkeypatch.setattr(appstate.subprocess, "run",
                            lambda argv, **kw: (seen.update(argv=argv), Done())[1])
        appstate.purge(r"P:\Skills", dry_run=True)
        assert "--dry-run" in seen["argv"] and "--yes" not in seen["argv"]

    def test_a_missing_cli_is_reported_not_swallowed(self, monkeypatch):
        """"Could not run" is a different answer from "nothing to delete" and must say so."""
        monkeypatch.setattr(appstate.shutil, "which", lambda name: None)
        out = appstate.purge(r"P:\Skills")
        assert out["ran"] is False and "not on PATH" in out["reason"]

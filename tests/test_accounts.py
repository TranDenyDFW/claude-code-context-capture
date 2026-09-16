r"""Sharing one chat list between the accounts signed into a machine, and putting it back.

The desktop app separates accounts with a directory: `<account>/<org>/local_<uuid>.json`, and the
listing is the list. `c4x.accounts` makes every pair on a root resolve to one directory with a
Windows junction, so whichever account is signed in reads the same records.

NOTHING HERE SKIPS. The first version marked the mutating half Windows only, and the runner counts
a skip against the deterministic fixture as a fixture gap: the Linux leg went red on the guards it
could not reach, and the Windows leg went red on the one test that skipped there. The module makes
the link the platform's own instead, a junction on Windows and a directory symlink elsewhere, so
every test runs on every leg.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import accounts  # noqa: E402

A, B = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
ORG_A, ORG_B = "11111111-3333-4333-8333-111111111111", "22222222-4444-4444-8444-222222222222"


def record(folder, name, title="a chat"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"local_{name}.json"
    path.write_text(json.dumps({"cliSessionId": name, "title": title}), encoding="utf-8")
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """One records root with two account pairs: three chats under A, one under B."""
    from c4x import store
    root = tmp_path / "appdata" / "Claude" / "claude-code-sessions"
    for n in range(3):
        record(root / A / ORG_A, f"a{n}")
    record(root / B / ORG_B, "b0")
    (root / A / ORG_A / "scheduled-tasks.json").write_text('{"scheduledTasks": []}',
                                                           encoding="utf-8")
    (root / B / ORG_B / "scheduled-tasks.json").write_text('{"scheduledTasks": ["theirs"]}',
                                                           encoding="utf-8")
    monkeypatch.setattr(store, "sessions_roots", lambda: [str(root)])
    # THE FIXTURE'S OWN CANDIDATES, so `_spellings` re-roots under them and never stats the
    # developer's profile.
    monkeypatch.setattr(store, "_claude_appdata_candidates",
                        lambda: [str(tmp_path / "appdata" / "Claude")])
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "context.db")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(accounts, "app_running", lambda: False)
    return root


class TestWhatItSeesBeforeAnythingMoves:
    def test_every_pair_is_listed_with_its_record_count(self, machine):
        pairs = accounts.pairs_on(machine)
        assert sorted((p["account"][:8], p["records"]) for p in pairs) == [
            (A[:8], 3), (B[:8], 1)]
        assert all(p["link_to"] is None for p in pairs), "nothing is linked yet"

    def test_the_state_reads_as_separate(self, machine):
        state = accounts.state()
        assert state["mode"] == accounts.CURRENT
        assert state["linked"] == 0 and state["pairs"] == 2
        assert state["chats_visible"] == 4, "every pair's records, since none is shared yet"

    def test_the_fullest_pair_is_the_one_others_point_at(self, machine):
        head = accounts.canonical_pair(accounts.pairs_on(machine))
        assert head["account"] == A and head["records"] == 3

    def test_an_ordinary_directory_is_not_a_link(self, machine):
        assert accounts.link_target(machine / A / ORG_A) is None


class TestTheGuards:
    def test_it_refuses_while_the_app_is_open(self, machine, monkeypatch):
        """A directory the app holds cannot be moved, and half a move is the state to avoid."""
        monkeypatch.setattr(accounts, "app_running", lambda: True)
        with pytest.raises(RuntimeError, match="Claude is running"):
            accounts.share_all()
        with pytest.raises(RuntimeError, match="Claude is running"):
            accounts.share_current()
        assert accounts.state()["mode"] == accounts.CURRENT, "and nothing moved"

    def test_it_refuses_to_restore_with_no_backup(self, machine):
        with pytest.raises(RuntimeError, match="no backup"):
            accounts.share_current()

    def test_every_platform_can_point_one_directory_at_another(self):
        """A junction on Windows, a directory symlink elsewhere, and the module says so.

        This refused off Windows, which left every guard below untestable there: the runner counts
        a skip against the deterministic fixture as a fixture gap, so a module that skips itself is
        a module CI never checks. The Linux leg went red on exactly that.
        """
        ok, why = accounts.supported()
        assert ok and why == ""

    def test_a_dry_run_writes_nothing(self, machine):
        before = sorted(p.name for p in (machine / B / ORG_B).iterdir())
        report = accounts.share_all(dry_run=True)
        assert report["dry_run"] and report["backup"] is None
        assert sorted(p.name for p in (machine / B / ORG_B).iterdir()) == before
        assert accounts.state()["mode"] == accounts.CURRENT


class TestSharingAndPuttingItBack:
    def test_every_account_reads_the_same_records(self, machine):
        report = accounts.share_all()
        assert report["restart_required"] is True
        pairs = {p["account"]: p for p in accounts.pairs_on(machine)}
        assert pairs[B]["link_to"], "the smaller pair is a link now"
        assert pairs[A]["link_to"] is None, "the fullest pair stays a real directory"
        through = sorted(p.name for p in (machine / B / ORG_B).glob("local_*.json"))
        direct = sorted(p.name for p in (machine / A / ORG_A).glob("local_*.json"))
        assert through == direct and len(through) == 4, (
            "both accounts list every chat, including the one that was B's")
        assert accounts.state()["mode"] == accounts.ALL

    def test_a_file_that_cannot_be_merged_is_set_aside_and_named(self, machine):
        """One directory holds one `scheduled-tasks.json`, and it is not this code's to merge."""
        report = accounts.share_all()
        aside = [a for r in report["roots"] for a in r["set_aside"]]
        assert [Path(a["path"]).name for a in aside] == ["scheduled-tasks.json"]
        kept = Path(report["backup"]) / "set-aside" / B[:8] / "scheduled-tasks.json"
        assert kept.is_file(), "the copy that did not travel is in the backup"
        assert "theirs" in kept.read_text(encoding="utf-8"), "and it is B's own, not a stand-in"
        shared = (machine / A / ORG_A / "scheduled-tasks.json").read_text(encoding="utf-8")
        assert "theirs" not in shared, "A's schedule was not overwritten by B's"

    def test_the_backup_holds_every_record_before_the_move(self, machine):
        report = accounts.share_all()
        manifest = json.loads(
            (Path(report["backup"]) / "manifest.json").read_text(encoding="utf-8"))
        names = sorted(Path(f["rel"]).name for f in manifest["files"]
                       if f["rel"].endswith(".json") and "local_" in f["rel"])
        assert names == ["local_a0.json", "local_a1.json", "local_a2.json", "local_b0.json"]

    def test_putting_it_back_restores_the_layout(self, machine):
        accounts.share_all()
        report = accounts.share_current()
        assert report["restart_required"] is True
        pairs = {p["account"]: p for p in accounts.pairs_on(machine)}
        assert all(p["link_to"] is None for p in pairs.values()), "no link survives"
        assert pairs[A]["records"] == 3 and pairs[B]["records"] == 1, (
            "each account has its own chats again")
        assert (machine / B / ORG_B / "scheduled-tasks.json").is_file(), (
            "and the file set aside came back")
        assert accounts.state()["mode"] == accounts.CURRENT

    def test_a_chat_written_while_shared_goes_back_to_the_account_that_owns_the_name(self, machine):
        """The records move by NAME from the backup, so a record rewritten since moves as it is."""
        accounts.share_all()
        live = machine / B / ORG_B / "local_b0.json"
        live.write_text(json.dumps({"cliSessionId": "b0", "title": "renamed while shared"}),
                        encoding="utf-8")
        accounts.share_current()
        back = machine / B / ORG_B / "local_b0.json"
        assert "renamed while shared" in back.read_text(encoding="utf-8"), (
            "the restore put back the backup's stale copy instead of the current one")

    def test_verify_notices_when_a_link_becomes_a_directory_again(self, machine):
        """What an app update's migration would leave behind, and nothing else reports it."""
        accounts.share_all()
        assert accounts.verify()["ok"]
        link = machine / B / ORG_B
        # THE MODULE'S OWN REMOVER, because the two platforms disagree about what a link is:
        # `rmdir` takes a junction on Windows and raises NotADirectoryError on a POSIX symlink,
        # which is how the Linux leg failed while Windows was green.
        accounts._remove_link(link)
        record(link, "b0", title="a migration recreated this")
        answer = accounts.verify()
        assert not answer["ok"]
        assert any("migration" in p for p in answer["problems"]), answer["problems"]


class TestAppRunning:
    """No child process: the answer comes from the process table, by image name, in any case.

    It asked `tasklist`, on every page load, and once the server ran without a console each of
    those calls flashed a console window.
    """
    class _Process:
        def __init__(self, name):
            self.info = {"name": name}

    def _table(self, monkeypatch, names):
        seen = {"attrs": None}

        def process_iter(attrs=None):
            seen["attrs"] = attrs
            return iter(self._Process(n) for n in names)
        monkeypatch.setattr(accounts.psutil, "process_iter", process_iter)
        return seen

    def test_the_app_is_found_by_image_name_in_any_case(self, monkeypatch):
        monkeypatch.setattr(accounts.platform, "system", lambda: "Windows")
        seen = self._table(monkeypatch, ["svchost.exe", "CLAUDE.EXE"])
        assert accounts.app_running() is True
        assert seen["attrs"] == ["name"], "only the name is asked for, which is what stays cheap"

    def test_a_process_that_refuses_its_name_is_skipped(self, monkeypatch):
        monkeypatch.setattr(accounts.platform, "system", lambda: "Windows")
        self._table(monkeypatch, [None, "node.exe", "claude-helper.exe"])
        assert accounts.app_running() is False, "a helper is not the app, and None is not a name"

    def test_off_windows_the_table_is_never_read(self, monkeypatch):
        monkeypatch.setattr(accounts.platform, "system", lambda: "Linux")

        def never(attrs=None):
            raise AssertionError("the process table was read")
        monkeypatch.setattr(accounts.psutil, "process_iter", never)
        assert accounts.app_running() is False


def signed_in_as(root, monkeypatch, account, org):
    """Make `account`/`org` the pair the desktop app is writing, the way the app records it:
    `config.json` names the account and `plan-usage-history.json` the organisation. Without
    this `desktop_pair` falls back to the newest record on disk, which is whichever the
    fixture wrote last, and a test built on that passes or fails by mtime."""
    from c4x import appstate
    (root.parent / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}),
                                             encoding="utf-8")
    (root.parent / "plan-usage-history.json").write_text(
        json.dumps({"samples": [{"org": org, "t": 1}]}), encoding="utf-8")
    monkeypatch.setattr(appstate, "sessions_root", lambda: str(root))


class TestWhatTheSignedInAccountSees:
    """`own` per pair and `current_chats`: what Current would show, beside what All shows."""

    def test_in_current_mode_the_count_is_the_pair_s_own_records(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        state = accounts.state()
        own = {p["account"]: p["own"] for p in state["roots"][0]["pairs"]}
        assert own == {A: 3, B: 1}
        assert state["signed_in"] == {"account": A, "org": ORG_A}
        assert state["current_chats"] == 3 and state["chats_visible"] == 4

    def test_under_all_each_account_still_knows_its_own(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        accounts.share_all()
        state = accounts.state()
        assert state["chats_visible"] == 4, "All shows every record in the one directory"
        own = {p["account"]: p["own"] for p in state["roots"][0]["pairs"]}
        assert own == {A: 3, B: 1}, "the manifest says whose each record was"
        assert state["current_chats"] == 3
        # A chat written while shared lands in the shared directory and counts for the pair the
        # others point at, which is where share_current leaves it.
        record(machine / A / ORG_A, "new-since")
        state = accounts.state()
        own = {p["account"]: p["own"] for p in state["roots"][0]["pairs"]}
        assert own == {A: 4, B: 1} and state["current_chats"] == 4
        signed_in_as(machine, monkeypatch, B, ORG_B)
        assert accounts.state()["current_chats"] == 1

    def test_putting_it_back_returns_the_plain_count(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, B, ORG_B)
        accounts.share_all()
        assert accounts.state()["current_chats"] == 1
        accounts.share_current()
        state = accounts.state()
        assert state["current_chats"] == 1
        assert {p["account"]: p["own"] for p in state["roots"][0]["pairs"]} == {A: 3, B: 1}

    def test_a_link_with_no_manifest_answers_none_not_a_guess(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        accounts.share_all()
        import shutil
        shutil.rmtree(accounts.backups_dir())
        state = accounts.state()
        assert state["current_chats"] is None
        assert all(p["own"] is None for p in state["roots"][0]["pairs"])
        assert state["chats_visible"] == 4, "All's number does not need the manifest"

    def test_no_signed_in_pair_answers_none(self, machine, monkeypatch):
        from c4x import appstate
        monkeypatch.setattr(appstate, "sessions_root", lambda: str(machine))
        monkeypatch.setattr(appstate, "desktop_pair", lambda root=None: None)
        state = accounts.state()
        assert state["signed_in"] is None and state["current_chats"] is None
        assert {p["account"]: p["own"] for p in state["roots"][0]["pairs"]} == {A: 3, B: 1}


ORG_C = "33333333-5555-4555-8555-333333333333"


class TestCoveringAPairThatAppearsLater:
    """The app creates `<account>/<org>` at sign-in with an organisation the junctions never
    named; `reconcile` folds it into the shared directory the moment the app is closed."""

    def _later_pair(self, machine, account=A, org=ORG_C, names=("c0",)):
        for name in names:
            record(machine / account / org, name)
        return machine / account / org

    def test_a_pair_the_app_creates_later_is_covered(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        accounts.share_all()
        later = self._later_pair(machine)
        report = accounts.reconcile()
        assert report["ran"] is True and report["restart_required"] is True
        assert accounts.link_target(later), "the new pair is a link now"
        assert (machine / A / ORG_A / "local_c0.json").is_file(), "its record is shared now"
        assert any(Path(link["link"]) == later for link in accounts._recorded_links()), (
            "the marker names the new link")
        owners = accounts._record_owners(accounts._newest_manifest())
        assert owners["local_c0.json"] == (str(machine), A, ORG_C), (
            "the newest manifest files the record under the pair it came from")
        assert accounts.state()["uncovered"] == []
        assert accounts.verify()["ok"]

    def test_reconcile_refuses_while_the_app_runs_and_names_the_pending_pair(self, machine,
                                                                            monkeypatch):
        accounts.share_all()
        later = self._later_pair(machine)
        before = sorted(p.name for p in accounts.backups_dir().iterdir())
        monkeypatch.setattr(accounts, "app_running", lambda: True)
        report = accounts.reconcile()
        assert report["ran"] is False and report["app_running"] is True
        assert [p["org"] for p in report["pending"]] == [ORG_C]
        assert "Quit Claude" in report["why"]
        assert (later / "local_c0.json").is_file() and accounts.link_target(later) is None
        assert sorted(p.name for p in accounts.backups_dir().iterdir()) == before, (
            "a refusal takes no backup")
        assert [p["org"] for p in accounts.state()["uncovered"]] == [ORG_C]

    def test_an_unshared_machine_is_left_alone(self, machine):
        report = accounts.reconcile()
        assert report["ran"] is False and "off" in report["why"] and report["pending"] == []
        assert not accounts.marker_path().exists() and not accounts.backups_dir().exists()
        assert accounts.state()["intended_source"] == "none"

    def test_intent_is_read_from_the_disk_when_the_marker_is_gone(self, machine):
        accounts.share_all()
        accounts.marker_path().unlink()
        assert accounts.intended_mode() == accounts.ALL, "links on disk are not made by accident"
        assert accounts.state()["intended_source"] == "disk"
        report = accounts.reconcile()
        assert report["ran"] is True and report["marker_written"] is True
        marker = json.loads(accounts.marker_path().read_text(encoding="utf-8"))
        assert marker["mode"] == accounts.ALL and marker["by"] == "reconcile"
        assert [Path(link["link"]) for link in marker["links"]] == [machine / B / ORG_B]
        assert accounts.state()["intended_source"] == "marker"

    def test_a_machine_with_links_and_no_manifest_gets_one(self, machine, monkeypatch):
        import shutil
        signed_in_as(machine, monkeypatch, A, ORG_A)
        accounts.share_all()
        shutil.rmtree(accounts.backups_dir())
        assert accounts.state()["current_chats"] is None, "links and no manifest: not known"
        report = accounts.reconcile()
        assert report["ran"] is True and report["backup"], "a manifest with nothing to fold"
        assert accounts.state()["current_chats"] == 4, (
            "every record sits under A now, and the new manifest says so")

    def test_nothing_pending_with_a_manifest_takes_no_second_backup(self, machine):
        accounts.share_all()
        before = sorted(p.name for p in accounts.backups_dir().iterdir())
        report = accounts.reconcile()
        assert report["ran"] is True and report["backup"] is None
        assert "nothing to cover" in report["why"]
        assert sorted(p.name for p in accounts.backups_dir().iterdir()) == before

    def test_a_backup_on_a_linked_root_lists_each_record_once(self, machine):
        """On Windows a junction is a directory to rglob, and the shared directory was copied
        once per link; on Linux the symlink was never followed and this passed either way."""
        accounts.share_all()
        _dest, manifest = accounts._backup([str(machine)], "again")
        names = [Path(f["rel"]).name for f in manifest["files"]
                 if Path(f["rel"]).name.startswith("local_")]
        assert sorted(names) == ["local_a0.json", "local_a1.json", "local_a2.json", "local_b0.json"]
        assert {Path(f["rel"]).parts[0] for f in manifest["files"]} == {A}, "under the holder"

    def test_two_backups_in_one_second_do_not_collide(self, machine):
        first, _ = accounts._backup([str(machine)], "one")
        second, _ = accounts._backup([str(machine)], "two")
        assert first != second and first.is_dir() and second.is_dir()

    def test_a_fuller_new_pair_does_not_displace_the_link_target(self, machine):
        accounts.share_all()
        later = self._later_pair(machine, B, ORG_C, names=("c0", "c1", "c2", "c3", "c4"))
        head = accounts.canonical_pair(accounts.pairs_on(machine))
        assert head["account"] == A and head["org"] == ORG_A, "the pair the links point at wins"
        accounts.reconcile()
        assert accounts.link_target(later) and accounts.link_target(machine / A / ORG_A) is None
        assert len(list((machine / A / ORG_A).glob("local_*.json"))) == 9

    def test_an_empty_new_pair_is_reported_by_verify_and_in_state(self, machine):
        accounts.share_all()
        (machine / A / ORG_C).mkdir(parents=True)
        answer = accounts.verify()
        assert not answer["ok"] and any("not linked" in p for p in answer["problems"])
        assert [p["org"] for p in answer["uncovered"]] == [ORG_C]
        assert [p["org"] for p in accounts.state()["uncovered"]] == [ORG_C]
        accounts.reconcile()
        assert accounts.link_target(machine / A / ORG_C) and accounts.verify()["ok"]

    def test_the_cli_covers_and_reports(self, machine, capsys):
        accounts.share_all()
        self._later_pair(machine)
        assert accounts.main(["--reconcile", "--dry-run"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["dry_run"] is True and printed["roots"][0]["linked"], "names the fold"
        assert accounts.link_target(machine / A / ORG_C) is None, "and moves nothing"
        assert accounts.main(["--reconcile"]) == 0
        assert accounts.link_target(machine / A / ORG_C)


class TestLinksTheKernelResolvesElsewhere:
    """A junction's substitute name is a string the kernel resolves physically. Made from inside the
    Store build's virtualised process tree with the virtual spelling, it lands somewhere else (a
    leftover directory holding one record on the author's machine, nothing on the laptop), while
    `link_target` reads the right string back. Identity is the only comparison that can see it."""

    def _alias(self, tmp_path):
        """Another spelling of the fixture's `Claude` directory, as the package spelling is."""
        alias = tmp_path / "alias"
        accounts._make_link(alias, tmp_path / "appdata" / "Claude")
        return alias

    def test_same_path_is_by_identity_not_by_string(self, machine, tmp_path):
        alias = self._alias(tmp_path)
        assert accounts._same_path(alias / "claude-code-sessions" / A / ORG_A, machine / A / ORG_A)
        assert not accounts._same_path(machine / A / ORG_A, machine / B / ORG_B)
        gone = tmp_path / "gone"
        gone.mkdir()
        accounts._make_link(tmp_path / "dangling", gone)
        gone.rmdir()
        assert not accounts._same_path(tmp_path / "dangling", machine / A / ORG_A), "no raise"
        assert accounts._same_path(tmp_path / "nowhere", tmp_path / "nowhere"), (
            "strings when neither is there")

    def test_resolves_to_follows_the_link(self, machine, tmp_path):
        accounts._make_link(tmp_path / "healthy", machine / A / ORG_A)
        assert accounts.resolves_to(tmp_path / "healthy", machine / A / ORG_A)
        decoy = tmp_path / "decoy"
        record(decoy, "d0")
        accounts._make_link(tmp_path / "elsewhere", decoy)
        assert not accounts.resolves_to(tmp_path / "elsewhere", machine / A / ORG_A)
        assert accounts.resolves_to(tmp_path / "elsewhere", decoy)
        gone = tmp_path / "gone"
        gone.mkdir()
        accounts._make_link(tmp_path / "dangling", gone)
        gone.rmdir()
        assert not accounts.resolves_to(tmp_path / "dangling", machine / A / ORG_A)

    def test_spellings_lists_every_identical_spelling_once(self, machine, tmp_path, monkeypatch):
        from c4x import store
        alias = self._alias(tmp_path)
        monkeypatch.setattr(store, "_claude_appdata_candidates",
                            lambda: [str(alias), str(tmp_path / "appdata" / "Claude")])
        given = str(machine / A / ORG_A)
        spellings = accounts._spellings(given)
        assert len(spellings) == 2 and spellings[-1] == given
        other = spellings[0]
        assert other.startswith(str(alias)) and accounts._same_path(other, given)
        assert len({os.path.normcase(s) for s in spellings}) == 2, "each spelling once"

    def test_spellings_never_re_roots_by_a_partial_component(self, machine, tmp_path,
                                                             monkeypatch):
        from c4x import store
        beta = tmp_path / "appdata" / "ClaudeBeta" / "claude-code-sessions"
        beta.mkdir(parents=True)
        monkeypatch.setattr(store, "_claude_appdata_candidates",
                            lambda: [str(tmp_path / "appdata" / "ClaudeBeta"),
                                     str(tmp_path / "appdata" / "Claude")])
        assert accounts._spellings(machine / A / ORG_A) == [str(machine / A / ORG_A)]

    def test_make_link_keeps_the_first_spelling_that_resolves(self, machine, tmp_path,
                                                              monkeypatch):
        real = str(machine / A / ORG_A)
        bogus = str(tmp_path / "bogus" / "x")
        monkeypatch.setattr(accounts, "_spellings", lambda path: [bogus, real])
        written = accounts._make_link(tmp_path / "L", real)
        assert written == real
        assert accounts.resolves_to(tmp_path / "L", real)
        assert os.path.normcase(accounts.link_target(tmp_path / "L")) == os.path.normcase(real)
        monkeypatch.setattr(accounts, "_spellings", lambda path: [bogus, bogus + "2"])
        with pytest.raises(RuntimeError, match="bogus"):
            accounts._make_link(tmp_path / "M", real)
        assert not os.path.lexists(tmp_path / "M"), "nothing left behind"

    def test_remove_link_removes_the_link_and_only_the_link(self, machine, tmp_path):
        accounts._make_link(tmp_path / "L", machine / A / ORG_A)
        accounts._remove_link(tmp_path / "L")
        assert not os.path.lexists(tmp_path / "L")
        assert len(list((machine / A / ORG_A).glob("local_*.json"))) == 3, "the target is intact"
        with pytest.raises(RuntimeError, match="not a link"):
            accounts._remove_link(machine / A / ORG_A)
        assert (machine / A / ORG_A).is_dir()
        with pytest.raises(RuntimeError, match="not a link"):
            accounts._remove_link(tmp_path / "nowhere")

    def _repoint(self, machine, target):
        accounts._remove_link(machine / B / ORG_B)
        accounts._make_link(machine / B / ORG_B, target)

    def test_a_link_that_points_elsewhere_is_reported_and_re_pointed(self, machine, tmp_path,
                                                                    monkeypatch):
        accounts.share_all()
        decoy = tmp_path / "decoy"
        record(decoy, "d0")
        self._repoint(machine, decoy)
        answer = accounts.verify()
        assert not answer["ok"] and any("points elsewhere" in p for p in answer["problems"])
        assert [p["why"] for p in answer["uncovered"]] == ["points elsewhere"]
        state = accounts.state()
        assert state["mode"] == accounts.MIXED and state["linked"] == 0
        assert [(p["account"], p["why"]) for p in state["uncovered"]] == [(B, "points elsewhere")]
        monkeypatch.setattr(accounts, "app_running", lambda: True)
        refused = accounts.reconcile()
        assert refused["ran"] is False
        assert [p["why"] for p in refused["pending"]] == ["points elsewhere"]
        assert accounts.resolves_to(machine / B / ORG_B, decoy), "nothing moved while the app runs"
        monkeypatch.setattr(accounts, "app_running", lambda: False)
        report = accounts.reconcile()
        assert report["ran"] is True and report["restart_required"] is True
        assert "re-pointed 1 link(s)" in report["why"]
        done = report["roots"][0]["relinked"]
        assert len(done) == 1 and done[0]["why"] == "points elsewhere" and not done[0]["restored"]
        assert accounts.resolves_to(machine / B / ORG_B, machine / A / ORG_A)
        assert sorted(p.name for p in (machine / B / ORG_B).glob("local_*.json")) == [
            "local_a0.json", "local_a1.json", "local_a2.json", "local_b0.json", "local_d0.json"]
        assert (Path(report["backup"]) / "through-link" / B[:8] / "local_d0.json").is_file()
        assert [Path(c["to"]).name for c in done[0]["copied"]] == ["local_d0.json"]
        assert (decoy / "local_d0.json").is_file(), "copied, never moved"
        assert accounts.verify()["ok"]
        after = accounts.state()
        assert after["mode"] == accounts.ALL and after["uncovered"] == []
        marker = json.loads(accounts.marker_path().read_text(encoding="utf-8"))
        assert marker["by"] == "reconcile"
        assert [Path(link["link"]) for link in marker["links"]] == [machine / B / ORG_B]

    def test_a_colliding_name_seen_through_the_link_is_kept_in_the_backup_only(self, machine,
                                                                              tmp_path):
        accounts.share_all()
        decoy = tmp_path / "decoy"
        record(decoy, "a0", title="the stale one")
        self._repoint(machine, decoy)
        report = accounts.reconcile()
        done = report["roots"][0]["relinked"][0]
        assert done["copied"] == []
        assert [Path(s["path"]).name for s in done["set_aside"]] == ["local_a0.json"]
        assert "stale" not in (machine / A / ORG_A / "local_a0.json").read_text(encoding="utf-8")
        kept = Path(report["backup"]) / "through-link" / B[:8] / "local_a0.json"
        assert "stale" in kept.read_text(encoding="utf-8")

    def test_a_dangling_link_is_re_pointed(self, machine, tmp_path):
        accounts.share_all()
        gone = tmp_path / "gone"
        gone.mkdir()
        self._repoint(machine, gone)
        gone.rmdir()
        assert [p["why"] for p in accounts.state()["uncovered"]] == ["dangling"]
        assert any("dangling" in p for p in accounts.verify()["problems"])
        report = accounts.reconcile()
        assert report["roots"][0]["relinked"][0]["why"] == "dangling"
        assert accounts.resolves_to(machine / B / ORG_B, machine / A / ORG_A)
        assert accounts.verify()["ok"]

    def test_a_relink_that_fails_puts_the_old_link_back(self, machine, tmp_path, monkeypatch):
        accounts.share_all()
        decoy = tmp_path / "decoy"
        record(decoy, "d0")
        self._repoint(machine, decoy)

        def refuse(link, target):
            raise RuntimeError("no spelling resolves")
        monkeypatch.setattr(accounts, "_make_link", refuse)
        with pytest.raises(RuntimeError, match="put back"):
            accounts.reconcile()
        assert accounts.resolves_to(machine / B / ORG_B, decoy), "the old link is back"
        assert os.path.normcase(accounts.link_target(machine / B / ORG_B)) == os.path.normcase(
            str(decoy))

    def test_a_dry_run_names_the_relink_and_moves_nothing(self, machine, tmp_path):
        accounts.share_all()
        decoy = tmp_path / "decoy"
        record(decoy, "d0")
        self._repoint(machine, decoy)
        before = sorted(p.name for p in accounts.backups_dir().iterdir())
        report = accounts.reconcile(dry_run=True)
        assert report["ran"] is True and report["backup"] is None
        assert report["roots"][0]["relinked"][0]["to"] == str(machine / A / ORG_A)
        assert accounts.resolves_to(machine / B / ORG_B, decoy)
        assert not (machine / A / ORG_A / "local_d0.json").exists()
        assert sorted(p.name for p in accounts.backups_dir().iterdir()) == before


class TestTheTags:
    """`current_chats` and each pair's `own` from harvest's owner tags, once any exist."""

    @pytest.fixture(autouse=True)
    def _fresh_frame(self):
        # THE 45 SECOND FRAME CACHE outlives a test: a frame built on one test's store answered
        # the next test's "no store" as six rows. Forgotten before and after each test here.
        from tests.test_projects import forget_cached_rows
        forget_cached_rows()
        yield
        forget_cached_rows()

    def _tagged_store(self, tmp_path, rows):
        from tests.test_projects import build_store, forget_cached_rows
        path = build_store(tmp_path / "data" / "context.db")
        con = sqlite3.connect(str(path))
        for n, (owner, gone) in enumerate(rows):
            con.execute("""INSERT INTO desktop_records (record_uuid, session_id, dir, first_seen,
                             last_seen, gone_at, source, owner_account, owner_org, owner_source)
                           VALUES (?, ?, 'X:/r/a/o', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z',
                                   ?, 'disk', ?, ?, ?)""",
                        (f"{n:08d}-0000-4000-8000-000000000000", f"s0-{n}", gone, owner,
                         None if owner is None else "org", None if owner is None else "signed-in"))
        con.commit()
        con.close()
        forget_cached_rows()
        return path

    def test_the_numbers_come_from_the_tags_when_any_exist(self, machine, tmp_path, monkeypatch):
        self._tagged_store(tmp_path, [(A, None), (A, None), (B, None), (None, None),
                                      (A, "2026-09-02T00:00:00Z")])
        signed_in_as(machine, monkeypatch, A, ORG_A)
        state = accounts.state()
        assert state["current_source"] == "tags" and state["untagged"] == 1
        assert state["current_chats"] == 2, "live records tagged with the signed-in account"
        assert {p["account"]: p["own"] for p in state["roots"][0]["pairs"]} == {A: 2, B: 1}
        signed_in_as(machine, monkeypatch, B, ORG_B)
        assert accounts.state()["current_chats"] == 1

    def test_without_tags_the_manifest_and_the_directories_answer_and_say_so(self, machine,
                                                                            monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        assert accounts.state()["current_source"] == "directory"
        accounts.share_all()
        assert accounts.state()["current_source"] == "manifest"
        import shutil
        shutil.rmtree(accounts.backups_dir())
        state = accounts.state()
        assert state["current_source"] is None and state["current_chats"] is None

    def test_a_store_with_the_table_and_no_tags_is_no_tags(self, machine, tmp_path, monkeypatch):
        self._tagged_store(tmp_path, [(None, None), (None, None)])
        signed_in_as(machine, monkeypatch, A, ORG_A)
        state = accounts.state()
        assert state["current_source"] == "directory" and state["untagged"] == 2
        assert state["current_chats"] == 3

    def test_the_header_carries_what_the_page_lists_beside_what_the_app_lists(self, machine,
                                                                            tmp_path, monkeypatch):
        """Three live records tagged A, two of them chats this page lists: the app shows 3, the
        page 2, and the header carries both, the page's from the same function the population
        list reads."""
        from c4x import store
        self._tagged_store(tmp_path, [(A, None), (A, None), (B, None), (None, None),
                                      (A, "2026-09-02T00:00:00Z"), (A, None)])
        signed_in_as(machine, monkeypatch, A, ORG_A)
        state = accounts.state()
        assert state["current_chats"] == 3, "records the app lists for A"
        assert state["current_listed"] == 2, "chats this page lists for A"
        assert state["current_listed"] == store.listed_by_account()["mine"]
        assert state["listed"] == len(store.session_rows(ttl=0))

    def test_without_a_store_the_page_numbers_are_none(self, machine, monkeypatch):
        signed_in_as(machine, monkeypatch, A, ORG_A)
        state = accounts.state()
        assert state["listed"] is None and state["current_listed"] is None

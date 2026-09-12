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
        os.rmdir(link)
        record(link, "b0", title="a migration recreated this")
        answer = accounts.verify()
        assert not answer["ok"]
        assert any("migration" in p for p in answer["problems"]), answer["problems"]

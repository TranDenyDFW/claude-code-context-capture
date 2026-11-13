r"""Pointing a chat's record at the working folder that exists now.

Every path here is a real directory under tmp, so the resolver is exercised against a filesystem
rather than a stub: the whole question it answers is "is this really there", and a stubbed answer
would not ask it.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import repair_paths  # noqa: E402

ACCOUNT, ORG = "aaaaaaaa-1111", "11111111-9999"


@pytest.fixture
def machine(tmp_path):
    """An old tree that is gone, two places it might have moved to, and a records root.

    `kept` is the one that holds the real contents; `decoy` exists under the same name and is
    empty, which is the shape that was really on disk: `R:\\WorkNotes` beside a populated
    `R:\\Projects\\WorkNotes`.
    """
    gone = tmp_path / "gone"
    kept = tmp_path / "kept"
    decoy = tmp_path / "decoy"
    (kept / "Alpha").mkdir(parents=True)
    (decoy / "Alpha").mkdir(parents=True)
    (kept / "Beta").mkdir(parents=True)
    moves = ((str(gone) + os.sep, (str(kept) + os.sep, str(decoy) + os.sep)),)
    root = tmp_path / "claude-code-sessions"
    (root / ACCOUNT / ORG).mkdir(parents=True)
    return {"gone": gone, "kept": kept, "decoy": decoy, "moves": moves, "root": root,
            "pair": root / ACCOUNT / ORG}


def record(machine, name, cwd, **extra):
    body = {"sessionId": f"local_{name}", "cliSessionId": name, "title": "a chat",
            "cwd": cwd, "originCwd": cwd, "isArchived": False, "model": "a-model"}
    body.update(extra)
    path = machine["pair"] / f"local_{name}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class TestWhereAFolderWent:
    def test_a_destination_that_has_already_been_written_needs_no_repair(self, machine):
        """A path outside the old prefix resolves to None, so a second run rewrites nothing."""
        assert repair_paths.resolve(str(machine["kept"] / "Alpha"), machine["moves"]) is None

    def test_a_path_under_the_old_prefix_that_still_exists_is_left_alone(self, machine):
        """KNOWN BAD INPUT, and the first version of this file did not have it.

        The check that matters is "is the folder actually missing", and the test above cannot fail
        without it: a rewritten path no longer starts with the old prefix, so it resolves to None
        whatever the code does. Removing the existence check from `resolve` left all 13 tests
        green. A folder still sitting under the old prefix is the case that tells them apart, and
        rewriting one would move a working chat to a different directory.
        """
        here = machine["gone"] / "Still"
        here.mkdir(parents=True)
        (machine["kept"] / "Still").mkdir()
        assert repair_paths.resolve(str(here), machine["moves"]) is None

    def test_the_populated_candidate_is_preferred_over_the_empty_one(self, machine):
        """KNOWN BAD INPUT: both candidates exist, so "it exists" cannot be the whole rule.

        `R:\\WorkNotes` and `R:\\Projects\\WorkNotes` both existed on the machine this was written
        for, and the first was empty. Order decides, and the order is the one the move really used.
        """
        found = repair_paths.resolve(str(machine["gone"] / "Alpha"), machine["moves"])
        assert found == str(machine["kept"] / "Alpha")

    def test_a_folder_that_is_nowhere_resolves_to_nothing(self, machine):
        """No basename search, no nearest parent: a guess here renames someone's chat to a
        different project. `P:\\WorkNotes\\Armko-CA` had a destination holding `Armko`, and a fuzzy
        matcher would have called that a hit."""
        assert repair_paths.resolve(str(machine["gone"] / "Nowhere"), machine["moves"]) is None

    def test_a_path_under_another_prefix_is_not_touched(self, machine):
        assert repair_paths.resolve(r"Q:\Somewhere", machine["moves"]) is None


class TestWhatARunWouldDo:
    def test_it_plans_both_path_fields_and_leaves_the_rest(self, machine):
        record(machine, "one", str(machine["gone"] / "Alpha"))
        said = repair_paths.plan(machine["root"], moves=machine["moves"])
        assert said["chats"] == 1
        item = said["rewrites"][0]
        assert set(item["fields"]) == {"cwd", "originCwd"}
        assert item["fields"]["cwd"] == str(machine["kept"] / "Alpha")

    def test_a_grant_and_the_users_own_words_are_counted_not_rewritten(self, machine):
        """The two fields this module refuses. Rewriting a grant extends a permission nobody gave,
        and rewriting a prompt edits what someone said."""
        record(machine, "two", str(machine["gone"] / "Alpha"),
               sessionPermissionUpdates=[{"directories": [str(machine["gone"] / "Alpha")]}],
               scratchPromptRecents=[f"look in {machine['gone']}"])
        said = repair_paths.plan(machine["root"], moves=machine["moves"])
        assert said["untouched"] == {"sessionPermissionUpdates[].directories": 1,
                                     "scratchPromptRecents": 1}
        assert set(said["rewrites"][0]["fields"]) == {"cwd", "originCwd"}

    def test_a_folder_with_no_destination_is_named_and_left(self, machine):
        record(machine, "three", str(machine["gone"] / "Nowhere"))
        said = repair_paths.plan(machine["root"], moves=machine["moves"])
        assert said["rewrites"] == []
        assert said["unresolved"] == {str(machine["gone"] / "Nowhere"): 1}

    def test_a_healthy_record_is_not_reported_as_a_problem(self, machine):
        record(machine, "four", str(machine["kept"] / "Beta"))
        said = repair_paths.plan(machine["root"], moves=machine["moves"])
        assert said["rewrites"] == [] and said["unresolved"] == {}


class TestTheRewrite:
    def test_only_the_two_fields_change_and_nothing_else_moves(self, machine):
        grant = [{"directories": [str(machine["gone"] / "Alpha")]}]
        words = [f"look in {machine['gone']}"]
        path = record(machine, "five", str(machine["gone"] / "Alpha"),
                      sessionPermissionUpdates=grant, scratchPromptRecents=words)
        before = json.loads(path.read_text(encoding="utf-8"))
        said = repair_paths.repair(root=machine["root"], dry_run=False, moves=machine["moves"])
        assert said["rewritten"] == 1 and said["failed"] == []
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["cwd"] == str(machine["kept"] / "Alpha")
        assert after["originCwd"] == str(machine["kept"] / "Alpha")
        assert after["sessionPermissionUpdates"] == grant, "a permission grant was rewritten"
        assert after["scratchPromptRecents"] == words, "the user's own words were rewritten"
        for key in before:
            if key not in ("cwd", "originCwd"):
                assert after[key] == before[key], f"{key} changed and had no business changing"

    def test_the_modification_time_survives(self, machine):
        """`claude_appdata()` ranks roots by mtime, so a bulk rewrite that restamped every record
        would make a repaired root look freshly written."""
        path = record(machine, "six", str(machine["gone"] / "Alpha"))
        old = (path.stat().st_atime - 86400, path.stat().st_mtime - 86400)
        os.utime(path, old)
        repair_paths.repair(root=machine["root"], dry_run=False, moves=machine["moves"])
        assert abs(path.stat().st_mtime - old[1]) < 2

    def test_a_second_run_finds_nothing_to_do(self, machine):
        record(machine, "seven", str(machine["gone"] / "Alpha"))
        first = repair_paths.repair(root=machine["root"], dry_run=False, moves=machine["moves"])
        assert first["rewritten"] == 1
        again = repair_paths.repair(root=machine["root"], dry_run=False, moves=machine["moves"])
        assert again["rewritten"] == 0 and again["rewrites"] == []

    def test_a_dry_run_writes_nothing(self, machine):
        path = record(machine, "eight", str(machine["gone"] / "Alpha"))
        before = path.read_bytes()
        said = repair_paths.repair(root=machine["root"], dry_run=True, moves=machine["moves"])
        assert said["rewrites"] and said["rewritten"] == 0
        assert path.read_bytes() == before
        assert "would be pointed at" in repair_paths.sentence(said)

    def test_the_history_folders_are_left_alone_unless_asked(self, machine, tmp_path):
        """The app finds a transcript by scanning for the session id, and the missing folder
        banner reads `cwd` alone, so moving 1.6 GB of history is not part of clearing it."""
        projects = tmp_path / "projects"
        (projects / repair_paths.slug(str(machine["gone"] / "Alpha"))).mkdir(parents=True)
        (projects / repair_paths.slug(str(machine["gone"] / "Alpha")) / "x.jsonl").write_text(
            "{}", encoding="utf-8")
        record(machine, "nine", str(machine["gone"] / "Alpha"))
        said = repair_paths.repair(root=machine["root"], projects=projects, dry_run=False,
                                   moves=machine["moves"])
        assert said["moved"] == []
        assert (projects / repair_paths.slug(str(machine["gone"] / "Alpha")) / "x.jsonl").exists()
        assert said["folders"], "the move was still planned and reported, just not performed"


class TestTheMachinesOwnMoves:
    """`moves_for` adds the relocation the app itself performed.

    Derived from the resolved roots rather than written down, because the new name carries the
    install's publisher suffix.
    """

    def test_the_scratch_workspaces_follow_the_data_directory(self, tmp_path):
        was, here = tmp_path / "Roaming" / "Claude", tmp_path / "Claude-Data" / "claude_abc"
        moves = repair_paths.moves_for(appdata=str(here), roaming=str(was))
        (here / "scratch-workspaces" / "a" / "b").mkdir(parents=True)
        gone = str(was / "scratch-workspaces" / "a" / "b")
        assert repair_paths.resolve(gone, moves) == str(here / "scratch-workspaces" / "a" / "b")

    def test_one_directory_under_two_names_adds_nothing(self, tmp_path):
        """KNOWN BAD INPUT: the app never moved, so there is no relocation to add and inventing one
        would map a live path onto itself."""
        same = str(tmp_path / "Claude")
        assert repair_paths.moves_for(appdata=same, roaming=same) == repair_paths.DEFAULT_MOVES

    def test_a_rule_exists_for_the_second_gathering_under_projects(self):
        r"""`R:\Local\PDrive` became `R:\Projects\Local\PDrive`, so chats that already named an
        `R:` path went missing in their turn.

        This pins the rule's PRESENCE and says so: the behaviour it guards is covered by the
        resolver tests above, which run against real directories, and a drive letter cannot be
        exercised under tmp without pretending.
        """
        assert "R:\\" in [old for old, _ in repair_paths.DEFAULT_MOVES]

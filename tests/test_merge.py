r"""The union merge: every account's pair ends up holding every chat's record.

The app lists only the records in the pair signed in, so a chat made under another account is
invisible rather than lost. Junctions used to hide that and this build refuses to write through
one, so the remedy is a copy. The rules worth testing are the refusals: never overwrite a record a
pair already has, never resurrect a chat deleted in the app, never write into a pair that is a
link, and never copy a record whose own id disagrees with its filename.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import merge  # noqa: E402

A, B, C = "aaaaaaaa-1111", "bbbbbbbb-2222", "cccccccc-3333"
ORG = "11111111-9999"


def pair_at(root, account, org=ORG, link=None) -> dict:
    folder = Path(root) / account / org
    folder.mkdir(parents=True, exist_ok=True)
    return {"root": str(root), "account": account, "org": org, "path": str(folder),
            "link_to": link, "records": 0}


def record(pair, name, title="a chat", archived=False, says=None, pad=0):
    """One record file. `says` writes a different `sessionId` than the filename."""
    body = {"sessionId": says or name, "cliSessionId": name[len(merge.RECORD):],
            "title": title, "isArchived": archived}
    if pad:
        body["filler"] = "x" * pad
    path = Path(pair["path"]) / f"{name}.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def idx(pair, ids):
    (Path(pair["path"]) / merge.IDX).write_bytes(merge.idx_bytes(ids))


def marker(pair, name):
    (Path(pair["path"]) / (merge.DELETED + name[len(merge.RECORD):])).write_text(
        "1760000000000", encoding="utf-8")


@pytest.fixture
def machine(tmp_path):
    """Two real pairs that have drifted apart, plus one that is a link.

    A holds a chat B has never seen (the case that started this), B holds one A lacks, both hold a
    shared one, A has an archived chat, and a chat deleted in B must not come back from A.
    """
    root = tmp_path / "claude-code-sessions"
    a, b, c = pair_at(root, A), pair_at(root, B), pair_at(root, C, link=str(root / A / ORG))
    record(a, "local_only_in_a", title="Armko CA review and device audit")
    record(a, "local_shared")
    record(b, "local_shared")
    record(b, "local_only_in_b")
    record(a, "local_archived_one", archived=True)
    idx(a, ["local_archived_one"])
    record(a, "local_deleted_one")
    marker(b, "local_deleted_one")
    return {"root": root, "a": a, "b": b, "c": c,
            "indexes": [merge.index_pair(p) for p in (a, b, c)]}


class TestWhatEachPairHolds:
    def test_the_records_the_markers_and_the_archived_set(self, machine):
        a = machine["indexes"][0]
        assert set(a["records"]) == {"local_only_in_a", "local_shared", "local_archived_one",
                                     "local_deleted_one"}
        assert a["archived"] == {"local_archived_one"}
        assert machine["indexes"][1]["deleted"] == {"local_deleted_one"}, (
            "a marker names the record id, with the deleted_ prefix swapped for local_")

    def test_a_missing_or_broken_index_is_an_empty_set_rather_than_an_error(self, tmp_path):
        assert merge.read_idx(tmp_path) == set()
        (tmp_path / merge.IDX).write_text("{not json", encoding="utf-8")
        assert merge.read_idx(tmp_path) == set()

    def test_the_index_is_written_the_way_the_app_writes_it(self):
        said = merge.idx_bytes({"local_b", "local_a"})
        assert said == b'{"v":1,"archived":["local_a","local_b"]}'
        assert b"\n" not in said and not said.startswith(b"\xef\xbb\xbf"), "one line, no BOM"


class TestThePlan:
    """Pure, so every refusal can be stated without touching a disk."""

    def test_each_real_pair_is_offered_what_it_lacks(self, machine):
        said = merge.plan_merge(machine["indexes"])
        into_b = {c["id"] for c in said["copies"] if c["pair"].startswith(B[:8])}
        assert "local_only_in_a" in into_b, "the chat that was invisible under the other account"
        assert "local_archived_one" in into_b
        assert "local_shared" not in into_b, "add only: a record already there is never rewritten"

    def test_a_pair_that_is_a_link_is_never_written_to(self, machine):
        said = merge.plan_merge(machine["indexes"])
        assert not [c for c in said["copies"] if c["pair"].startswith(C[:8])]
        assert said["skipped"]["link"] == [f"{C[:8]}\\{ORG[:8]}"]

    def test_a_chat_deleted_in_any_pair_stays_deleted_everywhere(self, machine):
        said = merge.plan_merge(machine["indexes"])
        assert "local_deleted_one" not in {c["id"] for c in said["copies"]}
        assert {s["id"] for s in said["skipped"]["deleted"]} == {"local_deleted_one"}

    def test_a_record_too_large_for_the_app_is_not_copied(self, machine, monkeypatch):
        monkeypatch.setattr(merge, "MAX_RECORD_BYTES", 10)
        said = merge.plan_merge(machine["indexes"])
        assert not said["copies"], "every fixture record is larger than ten bytes"
        assert said["skipped"]["too_large"]

    def test_the_archived_set_travels_with_the_record(self, machine):
        said = merge.plan_merge(machine["indexes"])
        assert said["idx_adds"][f"{B[:8]}\\{ORG[:8]}"] == ["local_archived_one"]
        assert all("local_only_in_a" not in v for v in said["idx_adds"].values()), (
            "a live chat must not arrive archived")

    def test_two_copies_that_differ_are_reported_and_left_alone(self, machine):
        record(machine["b"], "local_shared", title="renamed under the other account", pad=500)
        indexes = [merge.index_pair(p) for p in (machine["a"], machine["b"], machine["c"])]
        said = merge.plan_merge(indexes)
        assert [d["id"] for d in said["divergent"]] == ["local_shared"]
        assert "local_shared" not in {c["id"] for c in said["copies"]}

    def test_one_id_can_be_picked_by_a_prefix(self, machine):
        said = merge.plan_merge(machine["indexes"], only="local_only_in_a")
        assert {c["id"] for c in said["copies"]} == {"local_only_in_a"}
        # A prefix only one id starts with. `local_only_in_` would match the chat in the other
        # pair too, and the module is right to refuse that: see the ambiguity case below.
        assert merge.plan_merge(machine["indexes"], only="local_arch")["resolved"] == (
            "local_archived_one")

    def test_an_ambiguous_or_unknown_prefix_resolves_to_nothing(self, machine):
        assert merge.plan_merge(machine["indexes"], only="local_only")["resolved"] is None
        said = merge.plan_merge(machine["indexes"], only="local_nothing")
        assert said["resolved"] is None and said["copies"] == []


class TestTheMerge:
    def test_the_records_land_and_the_part_file_does_not_survive(self, machine):
        said = merge.merge(indexes=machine["indexes"], running=False)
        here = Path(machine["b"]["path"])
        assert (here / "local_only_in_a.json").is_file()
        assert json.loads((here / "local_only_in_a.json").read_text(encoding="utf-8"))["title"] == (
            "Armko CA review and device audit"), "copied byte for byte, title included"
        assert not list(here.glob(f"*{merge.PART}"))
        assert said["restart_required"] is True

    def test_a_dry_run_writes_nothing(self, machine):
        said = merge.merge(indexes=machine["indexes"], running=False, dry_run=True)
        assert said["written"] and all(not w["done"] for w in said["written"])
        assert not (Path(machine["b"]["path"]) / "local_only_in_a.json").exists()
        assert said["restart_required"] is False

    def test_a_record_whose_id_disagrees_with_its_name_is_refused(self, machine):
        """THE ONE CHECK THAT CANNOT BE DONE EARLIER. The index keys on the filename for speed, so
        this is where a file that calls itself something else is caught; the app lists by the
        field, so copying it would file the chat under a name nothing resolves."""
        record(machine["a"], "local_lying", says="local_someone_else")
        indexes = [merge.index_pair(p) for p in (machine["a"], machine["b"], machine["c"])]
        said = merge.merge(indexes=indexes, running=False)
        assert [f["id"] for f in said["failed"]] == ["local_lying"]
        assert "calls itself" in said["failed"][0]["why"]
        assert not (Path(machine["b"]["path"]) / "local_lying.json").exists()

    def test_a_copy_that_fails_is_reported_rather_than_raised(self, machine):
        def broken(src, dst):
            raise OSError("no space left on device")

        said = merge.merge(indexes=machine["indexes"], running=False, copy=broken)
        assert said["written"] == [] and len(said["failed"]) >= 1
        assert "no space" in said["failed"][0]["why"]
        assert not list(Path(machine["b"]["path"]).glob(f"*{merge.PART}"))

    def test_the_index_is_merged_only_while_the_app_is_closed(self, machine):
        """The app rewrites that file wholesale from its own set, so a merge into it while the app
        runs is a write one side loses. The records still land; the entries wait."""
        said = merge.merge(indexes=machine["indexes"], running=True)
        assert said["idx_pending"][f"{B[:8]}\\{ORG[:8]}"] == ["local_archived_one"]
        assert said["idx_written"] == []
        assert merge.read_idx(machine["b"]["path"]) == set(), "nothing written while it is open"
        assert (Path(machine["b"]["path"]) / "local_archived_one.json").is_file(), (
            "the record itself still arrives, the way adopt writes one with the app open")

    def test_with_the_app_closed_the_index_becomes_the_union(self, machine):
        idx(machine["b"], ["local_only_in_b"])
        indexes = [merge.index_pair(p) for p in (machine["a"], machine["b"], machine["c"])]
        merge.merge(indexes=indexes, running=False)
        assert merge.read_idx(machine["b"]["path"]) == {"local_only_in_b", "local_archived_one"}, (
            "what it had, plus what arrived, never one replacing the other")

    def test_running_it_twice_changes_nothing_the_second_time(self, machine):
        merge.merge(indexes=machine["indexes"], running=False)
        again = merge.merge(indexes=[merge.index_pair(p) for p in (machine["a"], machine["b"],
                                                                   machine["c"])], running=False)
        assert again["written"] == [] and again["failed"] == []
        assert merge.sentence(again) == "every pair already holds every chat"

    def test_only_copies_the_one_record_when_asked(self, machine):
        said = merge.merge(indexes=machine["indexes"], running=False, only="local_only_in_a")
        assert [w["id"] for w in said["written"]] == ["local_only_in_a"]
        assert not (Path(machine["b"]["path"]) / "local_archived_one.json").exists()

    def test_an_unknown_id_says_so_and_writes_nothing(self, machine):
        said = merge.merge(indexes=machine["indexes"], running=False, only="local_nothing")
        assert said["written"] == [] and "no record id starts with" in said["why"]
        assert merge.sentence(said) == said["why"]


class TestWhatItReports:
    def test_the_survey_counts_what_each_pair_lacks(self, machine):
        said = merge.survey(indexes=machine["indexes"])
        by_pair = {p["pair"]: p for p in said["pairs"]}
        assert by_pair[f"{B[:8]}\\{ORG[:8]}"]["missing"] == 2, "the live one and the archived one"
        assert by_pair[f"{C[:8]}\\{ORG[:8]}"]["link"] is True
        assert said["ids"] == 5 and said["to_copy"] >= 2

    def test_a_dry_run_says_what_it_would_do_rather_than_that_there_is_nothing_to_do(self, machine):
        """THE DEFECT THIS RECORDS. The sentence counted what was WRITTEN, and a dry run writes
        nothing, so it answered "every pair already holds every chat" over a plan of 112 copies on
        the live machine, which is the one answer that stops a person running it."""
        said = merge.sentence(merge.merge(indexes=machine["indexes"], running=False, dry_run=True))
        assert said.startswith("dry run: ") and "would be copied" in said
        assert "already holds every chat" not in said

    def test_the_sentence_names_the_work_and_what_is_waiting(self, machine):
        said = merge.merge(indexes=machine["indexes"], running=True)
        line = merge.sentence(said)
        assert "record(s) copied into" in line
        assert "pending until Claude closes" in line

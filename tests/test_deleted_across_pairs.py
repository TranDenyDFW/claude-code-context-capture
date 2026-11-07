r"""A chat the app deleted stays deleted, even while copies of its record sit in other accounts.

THE CASE THIS EXISTS FOR. The union merge puts a copy of every chat in every account's pair.
Deleting a chat in the app removes the record from the SIGNED-IN pair only and leaves a
`deleted_<uuid>` marker there, so the eight other copies still read as "on disk" and the harvester
cleared the deleted stamp on every pass. Measured on this machine 2026-11-07: 85 ids marked
deleted, 27 of them still holding a record in 8 pairs each, which is a deleted chat back on the
page and back in the import list.

Driven through the REAL node functions rather than a reimplementation of them, because the thing
under test is what `tools/harvest.mjs` does with the marker, not what this file thinks it does.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


def record(folder: Path, uuid: str, title="a chat"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"local_{uuid}.json").write_text(json.dumps({
        "sessionId": f"local_{uuid}", "cliSessionId": uuid, "title": title,
        "isArchived": False}), encoding="utf-8")


def marker(folder: Path, uuid: str, at="1793000000000"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"deleted_{uuid}").write_text(at, encoding="utf-8")


def run_node(script: str, env_extra: dict) -> dict:
    harvest = (ROOT / "tools" / "harvest.mjs").as_uri()
    code = f"const H = {json.dumps(harvest)};\n" + script
    done = subprocess.run(
        ["node", "--input-type=module", "-e", code], capture_output=True, text=True,
        timeout=180, cwd=str(ROOT), env={**os.environ, **env_extra})
    if done.returncode != 0:
        pytest.fail(f"node failed: {done.stderr[-1500:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture
def machine(tmp_path):
    """Two account pairs. The chat lives in both; only the second pair says it was deleted."""
    root = tmp_path / "claude-code-sessions"
    first = root / "aaaaaaaa-0000" / "org-0000"
    second = root / "bbbbbbbb-1111" / "org-1111"
    record(first, UUID_A, "the deleted one")
    record(second, UUID_A, "the deleted one")
    record(first, UUID_B, "an untouched one")
    record(second, UUID_B, "an untouched one")
    marker(second, UUID_A)
    return {"root": root, "first": first, "second": second, "tmp": tmp_path}


class TestTheMarkerIsFoundWhereverItIs:
    def test_a_marker_in_another_pair_is_found(self, machine):
        """KNOWN BAD INPUT: the marker is NOT in the pair the record is read from.

        `markerTime` only ever looked under the same `<account>/<org>`, which is why a delete in
        one account did nothing for the copies in the others.
        """
        found = run_node(
            "const m = await import(H);"
            "const got = m.deletedMarkerPaths([process.env.T_ROOT]);"
            "console.log(JSON.stringify({ids: [...got.keys()]}));",
            {"T_ROOT": str(machine["root"])})
        assert UUID_A in found["ids"]
        assert UUID_B not in found["ids"], "a chat with no marker was reported deleted"

    def test_no_markers_anywhere_finds_nothing(self, tmp_path):
        """The gate must be able to answer no, or it says nothing when it answers yes."""
        root = tmp_path / "claude-code-sessions"
        record(root / "acct" / "org", UUID_B)
        found = run_node(
            "const m = await import(H);"
            "console.log(JSON.stringify({ids: [...m.deletedMarkerPaths([process.env.T_ROOT]).keys()]}));",
            {"T_ROOT": str(root)})
        assert found["ids"] == []


class TestWhatTheHarvesterRecords:
    def test_a_deleted_chat_stays_deleted_while_copies_remain(self, machine):
        """The whole point: the record is still on disk in another pair, and it is still deleted."""
        answer = run_node(
            "const m = await import(H);"
            "const db = m.openDb(process.env.T_DB);"
            "const out = m.reconcileDesktopRecords(db, [process.env.T_ROOT], "
            "  {ledgerPath: process.env.T_LEDGER, backupsDir: process.env.T_BACKUPS, "
            "   now: '2026-11-07T00:00:00.000Z'});"
            "const rows = db.prepare('SELECT record_uuid, deleted_at FROM desktop_records').all();"
            "console.log(JSON.stringify({rows, deleted: out.deleted}));",
            {"T_ROOT": str(machine["root"]), "T_DB": str(machine["tmp"] / "store.db"),
             "T_LEDGER": str(machine["tmp"] / "no-ledger.json"),
             "T_BACKUPS": str(machine["tmp"] / "no-backups")})
        stamps = {r["record_uuid"].lower(): r["deleted_at"] for r in answer["rows"]}
        assert stamps.get(UUID_A), (
            "the deleted chat was recorded as live because a copy sits in another pair")
        assert stamps.get(UUID_B) is None, "a chat nobody deleted was stamped deleted"

    def test_the_stamp_survives_a_second_pass(self, machine):
        """Every pass used to clear it again, so the chat came back on the next Update data."""
        env = {"T_ROOT": str(machine["root"]), "T_DB": str(machine["tmp"] / "twice.db"),
               "T_LEDGER": str(machine["tmp"] / "no-ledger.json"),
               "T_BACKUPS": str(machine["tmp"] / "no-backups")}
        script = (
            "const m = await import(H);"
            "const db = m.openDb(process.env.T_DB);"
            "const opts = {ledgerPath: process.env.T_LEDGER, backupsDir: process.env.T_BACKUPS,"
            "  now: '2026-11-07T00:00:00.000Z'};"
            "m.reconcileDesktopRecords(db, [process.env.T_ROOT], opts);"
            "m.reconcileDesktopRecords(db, [process.env.T_ROOT], opts);"
            "const row = db.prepare('SELECT deleted_at FROM desktop_records WHERE record_uuid = ?')"
            "  .get(process.env.T_UUID);"
            "console.log(JSON.stringify({deleted_at: row ? row.deleted_at : null}));")
        answer = run_node(script, {**env, "T_UUID": UUID_A})
        assert answer["deleted_at"], "the second pass cleared the deleted stamp again"

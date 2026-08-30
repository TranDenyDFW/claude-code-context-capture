"""The archived reader, against records this test writes.

Pointed at a directory built here, so it runs on a machine that has never opened Claude Code
Desktop. In CI there are no records at all, and a check that cannot run is a failure rather than a
pass, so the fixture is the only honest way to exercise it.
"""
import json

import pytest


@pytest.fixture
def records(tmp_path):
    """Four files: two ordinary records, one whose fields sit past the prefix, and a non-record.

    The third exists because the reader reads a bounded 8 KB prefix for speed and parses the whole
    file only when that prefix does not carry the fields. Without it the fallback is never taken
    and could rot untested.
    """
    folder = tmp_path / "a" / "b"
    folder.mkdir(parents=True)

    def write(name, cli, archived, padding=0):
        record = {}
        if padding:
            record["enabledMcpTools"] = {f"server__tool_{i}": True for i in range(padding)}
        record.update({"sessionId": f"local_{cli}", "cliSessionId": cli, "isArchived": archived})
        (folder / name).write_text(json.dumps(record), encoding="utf-8")

    write("local_a.json", "11111111-1111-4111-8111-111111111111", True)
    write("local_b.json", "22222222-2222-4222-8222-222222222222", False)
    write("local_c.json", "33333333-3333-4333-8333-333333333333", True, padding=900)
    (folder / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": []}),
                                                 encoding="utf-8")
    return tmp_path


def test_finds_every_session_record_and_nothing_else(store, records):
    found = store.archived_sessions(root=str(records), ttl=0)
    assert len(found) == 3, "a scheduled-tasks file is not a session record"


def test_archived_and_not_archived_are_distinguished(store, records):
    found = store.archived_sessions(root=str(records), ttl=0)
    assert found["11111111-1111-4111-8111-111111111111"] is True
    assert found["22222222-2222-4222-8222-222222222222"] is False


def test_a_record_past_the_prefix_is_read_by_the_fallback(store, records):
    """The whole reason the fallback exists, exercised rather than assumed."""
    padded = records / "a" / "b" / "local_c.json"
    assert padded.stat().st_size > store._HEAD_BYTES, (
        "the padded record is not actually larger than the prefix, so this proves nothing")
    found = store.archived_sessions(root=str(records), ttl=0)
    assert found["33333333-3333-4333-8333-333333333333"] is True


def test_flipping_a_record_flips_the_answer(store, records):
    """Negative control: the reader must be reading, not returning something canned."""
    path = records / "a" / "b" / "local_a.json"
    before = store.archived_sessions(root=str(records), ttl=0)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["isArchived"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    after = store.archived_sessions(root=str(records), ttl=0)
    assert before["11111111-1111-4111-8111-111111111111"] is True
    assert after["11111111-1111-4111-8111-111111111111"] is False


def test_a_missing_directory_yields_nothing_rather_than_raising(store, tmp_path):
    assert store.archived_sessions(root=str(tmp_path / "nope"), ttl=0) == {}


def test_a_record_with_no_cli_session_id_is_skipped(store, tmp_path):
    """The desktop app's own sessionId is a different namespace and must not be used as a key."""
    folder = tmp_path / "x" / "y"
    folder.mkdir(parents=True)
    (folder / "local_z.json").write_text(
        json.dumps({"sessionId": "local_zzz", "isArchived": True}), encoding="utf-8")
    assert store.archived_sessions(root=str(tmp_path), ttl=0) == {}

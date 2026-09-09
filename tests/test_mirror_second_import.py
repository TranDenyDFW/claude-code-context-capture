"""Moving a project twice, and moving one on the machine it already lives on.

Two defects an independent sweep of the branch found, both invisible on a first import and both
leaving the store and the disk disagreeing afterwards.

  THE SECOND MOVE. `_rebase_store_rows` matched `AND cwd = <source>`, which is true exactly once.
  Import into `D:\\First` and the rows read `D:\\First`; import the SAME export again into
  `D:\\Second` and that clause matches nothing, so the rows stayed at the first destination while
  the transcript and its offset moved to the second. The page called it a success.

  THE HARVEST UNDO. An import never deletes the original transcript, and `tools/harvest.mjs`
  resumes each file from `files.bytes_read`, treating a path with NO row as unread. Moving the
  offset row therefore left the original looking brand new, so the next harvest re-read it from
  zero and recreated every session row at the OLD working directory.
"""
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import projects  # noqa: E402
from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

SOURCE = r"P:\Alpha"
FIRST = r"D:\First\Alpha"
SECOND = r"D:\Second\Alpha"


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
    from c4x import appstate
    home = tmp_path / "home"
    claude = home / ".claude"
    config = home / ".claude.json"
    sessions = tmp_path / "appdata" / "Claude" / "claude-code-sessions"
    (claude / "tasks").mkdir(parents=True)
    (sessions / "acct" / "org").mkdir(parents=True)
    monkeypatch.setattr(appstate, "CLAUDE_DIR", claude)
    monkeypatch.setattr(appstate, "CONFIG_PATH", config)
    monkeypatch.setattr(appstate, "sessions_root", lambda: str(sessions))
    (sessions.parent / "config.json").write_text(
        json.dumps({"lastKnownAccountUuid": "acct"}), encoding="utf-8")
    (sessions.parent / "plan-usage-history.json").write_text(
        json.dumps({"samples": [{"t": 1, "org": "org"}]}), encoding="utf-8")
    slug = claude / "projects" / appstate.slug_for(SOURCE)
    slug.mkdir(parents=True)
    for n in range(3):
        (slug / f"s0-{n}.jsonl").write_bytes(f"line {n}\n".encode())
    config.write_text(json.dumps({"projects": {SOURCE: {"hasTrustDialogAccepted": True}}}),
                      encoding="utf-8")
    return SimpleNamespace(claude=claude, config=config, sessions=sessions)


def rows(path, sql):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(sql)]
    finally:
        con.close()


class TestImportingTheSameExportTwice:
    def test_the_second_destination_wins_everywhere_or_nowhere(self, store_at, machine, tmp_path):
        """The store and the disk must not end up naming different directories."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        projects.import_(export_path, into=FIRST)
        assert rows(store_at, "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'") \
            == [FIRST]

        projects.import_(export_path, into=SECOND)
        assert rows(store_at, "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'") \
            == [SECOND], "the rows stayed at the first destination while the files moved"
        assert rows(store_at,
                    "SELECT transcript_path FROM sessions WHERE session_id = 's0-0'") \
            == [str(appstate.project_dir(SECOND) / "s0-0.jsonl")]
        assert rows(store_at,
                    "SELECT DISTINCT cwd FROM hook_events WHERE session_id LIKE 's0-%'") \
            == [SECOND]

    def test_the_second_import_still_reports_a_clean_mirror(self, store_at, machine, tmp_path):
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        projects.import_(export_path, into=FIRST)
        report = projects.import_(export_path, into=SECOND)
        assert report["mirror"]["ok"], report["mirror"]


class TestTheOriginalKeepsItsHarvestOffset:
    def test_both_paths_carry_an_offset_after_a_move(self, store_at, machine, tmp_path):
        """Neither copy may look unread, or a harvest recreates the project where it used to be."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        original = rows(store_at, "SELECT path FROM files WHERE path LIKE '%s0-0%'")
        assert original, "the fixture has no offset row to move"

        projects.import_(export_path, into=FIRST)
        after = rows(store_at, "SELECT path FROM files WHERE path LIKE '%s0-0%'")
        assert original[0] in after, (
            "the ORIGINAL transcript lost its offset row, so harvest reads it from zero and "
            "recreates the project at the old working directory")
        assert str(appstate.project_dir(FIRST) / "s0-0.jsonl") in after, (
            "the destination transcript has no offset row, so harvest would re-read that too")

    def test_the_offset_value_is_carried_over_and_not_reset(self, store_at, machine, tmp_path):
        """A copied row with bytes_read of 0 would be the same defect with extra steps."""
        from c4x import appstate
        export_path = tmp_path / "e.db"
        projects.export(SOURCE, export_path)
        projects.import_(export_path, into=FIRST)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            got = con.execute("SELECT bytes_read FROM files WHERE path = ?",
                              (str(appstate.project_dir(FIRST) / "s0-0.jsonl"),)).fetchone()
        finally:
            con.close()
        assert got and got[0] == 1234, f"the copied offset reads {got}"

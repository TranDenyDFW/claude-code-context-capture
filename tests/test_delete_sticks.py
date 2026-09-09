"""Delete's two defects, found by an independent sweep of the whole branch.

Both are the same shape as things this branch already fixed elsewhere, which is why they are worth
their own file rather than a line in another one.

  THE LABEL AGAIN. `delete` wrote the project LABEL into `excluded_projects.cwd`, and a label is
  `<cwd>\\archived` whenever the desktop app archived the chats. Harvest excludes by cwd and no cwd
  ends in that suffix, so deleting an archived project never stuck: the next harvest put every row
  back while `delete` reported `excluded: True`. 764bc0b is what made those labels selectable in the
  first place, so the fix that opened the menu opened this with it.

  THE SET RESOLVED TWICE. The backup was written from one resolution of the session set and the
  delete ran from another, taken minutes later inside the write transaction, so a session harvested
  in between was deleted while absent from the only copy of it.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import projects  # noqa: E402
from tests.test_projects import build_store, forget_cached_rows  # noqa: E402

ALPHA = r"P:\Alpha"


@pytest.fixture
def store_at(tmp_path, monkeypatch):
    from c4x import store
    path = build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


def excluded_cwds(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT cwd FROM excluded_projects")]
    finally:
        con.close()


def archive_them(path, session_ids):
    """Make the desktop app's records say these sessions are archived.

    That is what puts `\\archived` on the label, and the label is what `delete` used to write.
    """
    con = sqlite3.connect(str(path))
    try:
        con.execute("UPDATE sessions SET cwd = ? WHERE session_id IN "
                    "(SELECT session_id FROM sessions WHERE session_id LIKE 's0-%')", (ALPHA,))
        con.commit()
    finally:
        con.close()


class TestTheExclusionSticks:
    def test_a_plain_project_is_excluded_by_its_working_directory(self, store_at, tmp_path):
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        assert result["excluded"] is True
        assert excluded_cwds(store_at) == [ALPHA]
        assert result["excluded_cwds"] == [ALPHA]

    def test_an_archived_project_excludes_the_DIRECTORY_and_not_the_label(
            self, store_at, tmp_path, monkeypatch):
        """The defect. A label is not a directory, and harvest only knows directories.

        The label is produced by `store.session_rows()` from the desktop app's archive flag, so it
        is faked here at that seam rather than by writing a `\\archived` cwd into the store, which
        no real store contains: measured on the live store, 16 labels of that shape exist and ZERO
        cwds end in the suffix.
        """
        from c4x import store
        label = ALPHA + "\\" + store.ARCHIVED_SUFFIX
        ids = [f"s0-{n}" for n in range(3)]
        monkeypatch.setattr(store, "archived_sessions", lambda *a, **k: dict.fromkeys(ids, True))
        forget_cached_rows()

        result = projects.delete(label, confirm=label, out_dir=tmp_path)
        assert result["excluded"] is True
        assert excluded_cwds(store_at) == [ALPHA], (
            "the exclusion was written under the page LABEL, which harvest never matches, so the "
            "next harvest would put every row back")
        assert label not in excluded_cwds(store_at)

    def test_keeping_capture_writes_no_exclusion_at_all(self, store_at, tmp_path):
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path, keep_capturing=True)
        assert result["excluded"] is False
        assert excluded_cwds(store_at) == []


class TestTheBackupAndTheDeleteAgree:
    def test_only_what_the_backup_holds_is_deleted(self, store_at, tmp_path, monkeypatch):
        """A session that arrives between the two is KEPT and named, not deleted unbacked.

        Simulated at the seam that makes it possible: the export resolves the set, and this inserts
        a fourth session before the delete transaction opens, which is exactly what a harvest
        landing mid-export does.
        """
        real_export = projects.export

        def export_then_a_harvest_lands(project, out_path, app_state=True):
            manifest = real_export(project, out_path, app_state=app_state)
            con = sqlite3.connect(str(store_at))
            try:
                con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)",
                            ("s0-late", "slug-0", ALPHA, "arrived mid-export", "2.1.229", "cli",
                             "2026-08-03T00:00:00Z", "2026-08-03T00:00:00Z",
                             r"C:\t\s0-late.jsonl"))
                con.commit()
            finally:
                con.close()
            forget_cached_rows()
            return manifest

        monkeypatch.setattr(projects, "export", export_then_a_harvest_lands)
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)

        assert result["appeared_since_backup"] == ["s0-late"], result["appeared_since_backup"]
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            survivors = [r[0] for r in con.execute("SELECT session_id FROM sessions")]
        finally:
            con.close()
        assert "s0-late" in survivors, (
            "a session that was never in the backup was deleted anyway")
        assert not [s for s in survivors if s.startswith("s0-") and s != "s0-late"]

    def test_the_backup_carries_the_session_ids_the_delete_uses(self, store_at, tmp_path):
        """The two halves are the same list, which is the property the fix rests on."""
        result = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        manifest = projects.read_manifest(Path(result["backup"]))
        assert sorted(manifest["session_ids"]) == [f"s0-{n}" for n in range(3)]
        assert result["appeared_since_backup"] == []

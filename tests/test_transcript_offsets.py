r"""The `files` table is the offset of EVERY transcript a session wrote, not just its own file.

`appstate.capture` carries every entry in the slug directory whose name begins with a session id,
which is the session's own `<session id>.jsonl` AND the `<session id>/` directory holding its
subagent transcripts and tool output. The purge removes all of them. The row half was scoped to
`sessions.transcript_path` alone, so on the author's store 7,634 of 9,068 offset rows were neither
carried by an export nor removed by a delete.

An offset says a file has been read to its end. Left behind after a delete, it makes harvest skip
those bytes forever if the file ever comes back, which is the same defect the delete's own comment
already names for the top-level transcript and fixes only there.
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
OWN = r"C:\t\s0-0.jsonl"
SUB = r"C:\t\s0-0\subagents\one.jsonl"
TOOLS = r"C:\t\s0-0\tool-results\big.txt"
OTHER = r"C:\t\s1-0\subagents\one.jsonl"


def offsets(con, path, first_ts="2026-08-01T00:00:00Z"):
    con.execute("INSERT INTO files (path,size,mtime_ms,bytes_read,lines_read,rewrites,"
                "last_harvest_ts,first_ts) VALUES (?,?,?,?,?,?,?,?)",
                (path, 99, 0, 99, 9, 0, "2026-08-02T00:00:00Z", first_ts))


@pytest.fixture
def store_at(tmp_path, monkeypatch):
    """A store whose first session also wrote a subagent transcript and a tool result."""
    from c4x import store
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    for extra in (SUB, TOOLS, OTHER):
        offsets(con, extra)
    con.execute("UPDATE files SET first_ts = ? WHERE path = ?", ("2026-07-31T00:00:00Z", OWN))
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


def paths_in(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT path FROM files")}
    finally:
        con.close()


class TestTheExportCarriesThem:
    def test_every_offset_of_a_carried_session_travels(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(ALPHA, out)
        carried = paths_in(out)
        assert OWN in carried, "the session's own transcript"
        assert SUB in carried and TOOLS in carried, (
            "the export carries these files and left their offsets behind")

    def test_another_projects_offsets_do_not_travel(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(ALPHA, out)
        assert OTHER not in paths_in(out), "the export leaked another project's offset"

    def test_the_preview_counts_what_the_export_carries(self, store_at):
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            preview = projects.footprint(con, ALPHA)
            carried = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'C:\\t\\s0-%'"
                                  ).fetchone()[0]
        finally:
            con.close()
        assert preview["files"] == carried == 5, (
            "the preview and the export disagree about how many offsets a delete would remove")


CROOKED = r"C:\t\s0-2\subagents\agent-x.jsonl"


class TestASessionWhoseOwnPathNamesASubagentFile:
    """The one row on the author's store that breaks `slug_of(cwd) = project_slug`.

    Its `transcript_path` names a file under `<session id>/subagents/`, because a subagent file was
    harvested before the session's own and the identity rules that stop that are newer than the row.
    A scope derived from `transcript_path` reaches that one file and misses everything else the
    session wrote: an independent reviewer measured the export carrying 19 files whose offsets it
    did not carry, and the delete removing those 19 files while leaving their 19 offsets behind.
    The rule is the session ID in the path, which is what `appstate.capture` matches on.
    """

    @staticmethod
    def crooked(store_at):
        con = sqlite3.connect(str(store_at))
        con.execute("UPDATE sessions SET transcript_path = ?, project_slug = 'subagents' "
                    "WHERE session_id = 's0-2'", (CROOKED,))
        offsets(con, CROOKED)
        con.commit()
        con.close()
        forget_cached_rows()

    def test_the_export_carries_the_rest_of_what_that_session_wrote(self, store_at, tmp_path):
        self.crooked(store_at)
        out = tmp_path / "out.db"
        projects.export(ALPHA, out)
        carried = paths_in(out)
        assert CROOKED in carried
        assert r"C:\t\s0-2.jsonl" in carried, (
            "the session's own transcript is not reachable from a path that names a subagent file")

    def test_the_delete_leaves_none_of_them_behind(self, store_at, tmp_path):
        self.crooked(store_at)
        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        left = paths_in(store_at)
        assert not [path for path in left if "s0-" in path], sorted(left)
        assert OTHER in left, "the delete took another project's offset"


class TestTheDeleteRemovesThem:
    def test_no_offset_survives_for_a_file_the_delete_removed(self, store_at, tmp_path):
        report = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        left = paths_in(store_at)
        assert SUB not in left and TOOLS not in left and OWN not in left, (
            "an offset for a removed transcript says it has been read to its end")
        assert OTHER in left, "the delete took another project's offset"
        assert report["removed"]["files"] == 5

    def test_the_backup_holds_exactly_what_the_delete_removed(self, store_at, tmp_path):
        """The acceptance rule, applied to the table this was wrong in.

        Symmetric by construction, since the backup and the delete share `where_for`, so it cannot
        tell one scoping rule from another. It is here as an acceptance check and not as a gate.
        """
        before = paths_in(store_at)
        report = projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        after = paths_in(store_at)
        assert before - after == paths_in(Path(report["backup"])) - set(report["kept_offsets"])


class TestAnImportPutsThemBack:
    def test_every_offset_comes_back_with_its_first_timestamp(self, store_at, tmp_path):
        """Round trip only: the export carries the column and the import loads it.

        NOT A GATE FOR THE REBASE, and it used to say it was. With no `into` the destination cwd is
        the source cwd, `_rebase_store_rows` hits its `continue`, and the hand-typed column list
        this docstring blamed never runs at all: an independent reviewer reverted that fix and
        watched this file stay green. The gate for it is
        `tests/test_mirror.py::TestTheRebase::test_the_offset_keeps_the_timestamp_that_orders_ingest`,
        which moves the project and does go red.
        """
        out = tmp_path / "out.db"
        projects.export(ALPHA, out)
        projects.delete(ALPHA, confirm=ALPHA, out_dir=tmp_path)
        projects.import_(out)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        try:
            stamps = dict(con.execute("SELECT path, first_ts FROM files"))
        finally:
            con.close()
        assert stamps[OWN] == "2026-07-31T00:00:00Z"
        assert stamps[SUB] == "2026-08-01T00:00:00Z"

"""A store is opened read-only through a `file:` URI, and a URI gives meaning to characters a
folder name is free to hold.

Measured with SQLite 3.50 before `store.ro_uri` existed (`f"file:{path}?mode=ro"`, the path as it
came): under a folder called `a#b` everything from the `#` on is a fragment, `?mode=ro` included,
so SQLite opened `.../a` READ-WRITE, CREATED it empty, and answered "no such table"; under `p%41q`
the escape was decoded and the store opened was the one under `pAq`, when there was one, and
otherwise none. A UNC path survived only because it was spelled with backslashes: the same path
with forward slashes reads as a host name.

So nothing in `c4x/` or `tools/` builds that URI by hand any more, which the last test holds.
"""
import os
import re
import sqlite3
from pathlib import Path

import pytest

from c4x import store
from tests.test_projects import forget_cached_rows

ROOT = Path(__file__).resolve().parents[1]
AWKWARD = ["plain", "a#b", "p%41q", "per%cent", "sp ace", "amp&eq=x", "uni\u00e9"]
if os.name != "nt":
    AWKWARD.append("q?mark")   # not a legal file name on Windows


def make(path, name):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE here (name TEXT)")
    con.execute("INSERT INTO here VALUES (?)", (name,))
    con.commit()
    con.close()
    return path


def said(path):
    con = sqlite3.connect(store.ro_uri(path), uri=True)
    try:
        return con.execute("SELECT name FROM here").fetchone()[0]
    finally:
        con.close()


class TestTheUri:
    @pytest.mark.parametrize("folder", AWKWARD)
    def test_it_opens_the_file_it_was_given(self, tmp_path, folder):
        target = make(tmp_path / folder / "context.db", folder)
        before = sorted(p.name for p in tmp_path.iterdir())
        assert said(target) == folder
        assert said(str(target)) == folder, "a string is a path too"
        assert sorted(p.name for p in tmp_path.iterdir()) == before, "nothing was created beside it"

    def test_an_escape_in_a_name_is_not_decoded_into_another_store(self, tmp_path):
        """`p%41q` decodes to `pAq`. With both on disk the old URI read the WRONG store and said
        nothing."""
        make(tmp_path / "pAq" / "context.db", "the decoy")
        target = make(tmp_path / "p%41q" / "context.db", "the one that was asked for")
        assert said(target) == "the one that was asked for"

    def test_it_is_read_only(self, tmp_path):
        target = make(tmp_path / "a#b" / "context.db", "x")
        con = sqlite3.connect(store.ro_uri(target), uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                con.execute("INSERT INTO here VALUES ('written')")
        finally:
            con.close()

    def test_a_store_that_is_not_there_is_an_error_and_is_not_created(self, tmp_path):
        absent = tmp_path / "a#b" / "absent.db"
        absent.parent.mkdir()
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(store.ro_uri(absent), uri=True).execute("SELECT 1")
        assert not absent.exists() and [p.name for p in tmp_path.iterdir()] == ["a#b"]

    @pytest.mark.skipif(os.name != "nt", reason="a UNC path is a Windows spelling")
    def test_a_share_is_spelled_with_an_empty_host(self):
        """`//server/share/x` after `file:` names the HOST `server`, which SQLite refuses. Four
        slashes: an empty host, then the path with both of its own."""
        assert store.ro_uri(r"\\server\share\my store\context.db") == \
            "file:////server/share/my%20store/context.db?mode=ro"
        assert store.ro_uri(r"P:\x\a#b\context.db") == "file:P:/x/a%23b/context.db?mode=ro"

    @pytest.mark.skipif(os.name != "nt", reason="a UNC path is a Windows spelling")
    def test_a_store_on_a_share_opens(self, tmp_path):
        target = make(tmp_path / "a#b" / "context.db", "over the share")
        drive = target.drive.rstrip(":")
        share = Path(rf"\\localhost\{drive}$") / target.relative_to(target.anchor)
        if len(drive) != 1 or not share.exists():
            pytest.skip("this machine does not serve its own drive as an admin share")
        assert said(share) == "over the share"


class TestTheReaders:
    @pytest.mark.parametrize("folder", ["a#b", "p%41q"])
    def test_the_store_reads_from_a_folder_with_such_a_name(self, tmp_path, monkeypatch, folder):
        """`q`, `column_present` and `tables_present` are every read the page makes."""
        target = make(tmp_path / folder / "context.db", folder)
        monkeypatch.setattr(store, "DB_PATH", target)
        forget_cached_rows()
        try:
            assert list(store.q("SELECT name FROM here")["name"]) == [folder]
            assert store.column_present("here", "name") is True
            assert store.column_present("here", "absent") is False
            assert store.tables_present("here") is True
            assert [p.name for p in tmp_path.iterdir()] == [folder], "no stray file beside it"
        finally:
            forget_cached_rows()

    @pytest.mark.parametrize("folder", ["C#", "p%41q"])
    def test_a_project_is_exported_verified_and_deleted_from_such_a_folder(
            self, tmp_path, monkeypatch, folder):
        """The five opens in `c4x/projects.py`: the export reads the store, `verify`,
        `read_manifest` and `app_state_rows` read the export, and a delete checks its label against
        the store before it writes a backup. A folder called `C#` is nobody's edge case."""
        from c4x import projects
        from tests.test_projects import build_store
        home = tmp_path / folder
        home.mkdir()
        path = build_store(home / "context.db")
        monkeypatch.setattr(store, "DB_PATH", path)
        forget_cached_rows()
        try:
            out = home / "exports" / "alpha.db"
            manifest = projects.export(r"P:\Alpha", out)
            assert manifest["sessions"] == 3
            assert projects.verify(out) == (True, [])
            assert projects.read_manifest(out)["project"] == r"P:\Alpha"
            assert isinstance(projects.app_state_rows(out, with_blobs=False), list)
            projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=home / "backups")
            assert list(store.q("SELECT DISTINCT cwd FROM sessions")["cwd"]) == [r"P:\Beta"]
            assert sorted(p.name for p in tmp_path.iterdir()) == [folder], "no stray file beside it"
        finally:
            forget_cached_rows()

    def test_the_redact_tool_builds_the_same_uri(self, tmp_path):
        """`tools/redact.py` imports nothing from the package, so it carries a copy; nothing but
        this keeps the two in step. It matters there more than anywhere: under `a#b` the old URI
        made the tool back up an EMPTY file it had just created in place of the store."""
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import redact
        paths = [tmp_path / folder / "context.db" for folder in AWKWARD]
        if os.name == "nt":
            paths.append(Path(r"\\server\share\a#b\context.db"))
        for path in paths:
            assert redact.ro_uri(path) == store.ro_uri(path), path
            assert redact.ro_uri(str(path)) == store.ro_uri(path), path
        target = make(tmp_path / "a#b" / "context.db", "read by the tool")
        con = sqlite3.connect(redact.ro_uri(target), uri=True)
        try:
            assert con.execute("SELECT name FROM here").fetchone()[0] == "read by the tool"
        finally:
            con.close()

    def test_no_module_builds_a_sqlite_uri_by_hand(self):
        """THE CLASS, not the instance: eleven places built `f"file:{path}?mode=ro"` when the first
        of them was found. A new one would bring the defect back for whatever it opens."""
        by_hand = re.compile(r"""["']file:\{""")
        found = []
        for folder, pattern in (("c4x", "**/*.py"), ("tools", "*.py")):
            for source in sorted((ROOT / folder).glob(pattern)):
                for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                    if by_hand.search(line) and "def ro_uri" not in line:
                        found.append(f"{source.relative_to(ROOT).as_posix()}:{number}")
        allowed = {"c4x/store.py", "tools/redact.py"}   # where the URI is built, once each
        extra = [f for f in found if f.rsplit(":", 1)[0] not in allowed]
        assert extra == [], f"open these through store.ro_uri: {extra}"
        per_file = {name: sum(1 for f in found if f.startswith(name + ":")) for name in allowed}
        assert per_file == {"c4x/store.py": 1, "tools/redact.py": 1}, per_file

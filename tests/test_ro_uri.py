"""A store is opened read-only through a `file:` URI, and a URI gives meaning to characters a
folder name is free to hold.

Measured with SQLite 3.50 before `c4x.paths.ro_uri` existed, the URI being an f-string of `file:`,
the path as it came, and `?mode=ro`: under a folder called `a#b` everything from the `#` on is a
fragment, `?mode=ro` included, so SQLite opened `.../a` READ-WRITE, CREATED it empty, and answered
"no such table"; under `p%41q` the escape was decoded and the store opened was the one under
`pAq`, when there was one, and otherwise none. A share survived only because it was spelled with
backslashes: the same path with forward slashes reads as a host name.

Eleven places in `c4x/` and `tools/*.py` built it that way, and so did the suite's own snapshot of
the real store (`tests/conftest.py`), 38 opens in eight test files (one of them the suite's store
under the checkout, not a tmp path) and three node tools, one of which `c4x.store` runs when it is
imported. The Python ones now call `ro_uri`; the node ones pass the path itself, which is never
URI-parsed. The last test looks for a hand-built one in every Python file under `c4x/`, `tools/`
and `tests/` (this file excepted) and every `.mjs` under `tools/` and `hooks/`; the test before
it feeds the rule known-bad lines and ASSERTS the ones it is known to miss. NOT covered here: how
the node tools find the install they belong to (`rootFrom` in `tools/paths.mjs`). Measured on
this branch, it misreads a space, `#`, `%`, square and curly brackets, a tilde, a caret, a
backtick and any non-ASCII character in the folder's name (parentheses, `&`, `'`, `;`, `+` are
read right). That is a separate defect with its own branch (`fix/root-from-decoding`).

NO TEST HERE SKIPS AGAINST THE FIXTURE. CI's runner fails a skipped test on the fixture leg
(`tools/run_tests.mjs`, `judgePytest`), so a platform difference is a branch inside the test, the
way `tests/test_proc.py` does it. The one skip is for a LIVE store too large to copy, which that
runner allows and reports by name.
"""
import os
import re
import sqlite3
from pathlib import Path

import pytest

from c4x import paths, proc, store
from tests.test_projects import forget_cached_rows

ROOT = Path(__file__).resolve().parents[1]
AWKWARD = ["plain", "a#b", "p%41q", "per%cent", "sp ace", "amp&eq=x", "uni\u00e9"]
if os.name != "nt":
    AWKWARD.append("q?mark")   # not a legal file name on Windows


# WHO MAY TURN A STRING INTO A SQLITE URI. Python has one door: `sqlite3.connect(..., uri=True)`.
# So the rule is an allow-list on that flag: a line that sets it must be a line that calls `ro_uri`,
# however the string beside it was put together (an f-string, a constant, four slashes, a template).
# An earlier sweep tried to recognise the spellings instead, and two reviews in a row found more.
URI_FLAG = re.compile(r"\buri\s*=\s*True\b")
# Node has no such flag: a string that starts `file:` is simply parsed as a URI. What gives a
# hand-built one away is why anyone builds one: a query parameter, or `file:` on the line that
# opens.
NODE_PARAM = re.compile(r"\b(mode=(ro|rw|rwc|memory)|immutable=|nolock=|cache=(shared|private))")
NODE_OPEN = re.compile(r"DatabaseSync\s*\(")
TELEMETRY = re.compile(r"OTEL_LOG_RAW_API_BODIES")


def py_by_hand(line):
    text = line.strip()
    return not text.startswith("#") and bool(URI_FLAG.search(text)) and "ro_uri(" not in text


def node_by_hand(line):
    text = line.strip()
    if text.startswith(("//", "*", "/*")) or TELEMETRY.search(text):
        return False
    return bool(NODE_PARAM.search(text)) or ("file:" in text and bool(NODE_OPEN.search(text)))


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


def copy_of(source, target):
    """A consistent copy through SQLite's own backup, the way the suite snapshots a store."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(store.ro_uri(source), uri=True)
    dst = sqlite3.connect(target)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return target


class TestTheUri:
    def test_the_store_and_the_paths_module_share_one_function(self):
        assert store.ro_uri is paths.ro_uri

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

    def test_a_share_is_spelled_with_an_empty_host(self):
        """`//server/share/x` after `file:` names the HOST `server`, which SQLite refuses. Four
        slashes: an empty host, then the path with both of its own. Both flavours of pathlib keep
        exactly two leading slashes, so the first line holds everywhere."""
        assert store.ro_uri("//server/share/my store/context.db") == \
            "file:////server/share/my%20store/context.db?mode=ro"
        if os.name == "nt":
            assert store.ro_uri(r"\\server\share\my store\context.db") == \
                "file:////server/share/my%20store/context.db?mode=ro"
            assert store.ro_uri(r"P:\x\a#b\context.db") == "file:P:/x/a%23b/context.db?mode=ro"
        else:
            assert store.ro_uri("/x/a#b/context.db") == "file:/x/a%23b/context.db?mode=ro"

    def test_a_path_that_begins_with_two_slashes_opens(self, tmp_path):
        """The four slashes, LIVE, without needing a share: on Windows the long-path spelling
        `\\\\?\\P:\\...` has the same two leading slashes once it is posix, and the old URI could
        not open it; on POSIX `//tmp/x` is `/tmp/x`."""
        target = make(tmp_path / "a#b" / "context.db", "two slashes")
        doubled = "\\\\?\\" + str(target) if os.name == "nt" else "/" + str(target)
        assert Path(doubled).as_posix().startswith("//")
        assert said(doubled) == "two slashes"

    def test_a_name_that_is_not_utf8_is_encoded_from_its_bytes(self, tmp_path):
        """Linux hands Python the byte E9 in a name as a lone surrogate. Quoting the STR raises on
        it; `sqlite3.connect` never did, because it encodes the way `os.fsencode` does."""
        name = "caf\udce9"
        uri = store.ro_uri(f"x/{name}/context.db")
        assert uri.startswith("file:x/caf%") and uri.endswith("/context.db?mode=ro")
        if os.name != "nt":
            assert uri == "file:x/caf%E9/context.db?mode=ro"
            folder = os.fsencode(tmp_path) + b"/caf\xe9"
            os.mkdir(folder)
            target = Path(os.fsdecode(folder)) / "context.db"
            make(target, "bytes, not text")
            assert said(target) == "bytes, not text"


class TestTheReaders:
    @pytest.mark.parametrize("folder", ["a#b", "p%41q"])
    def test_the_store_reads_from_a_folder_with_such_a_name(self, tmp_path, monkeypatch, folder):
        """`q`, `column_present` and `tables_present`: the three opens in `c4x/store.py`."""
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
        this keeps the two in step. Measured on the old tool: under `a#b` it backed up the EMPTY
        file it had just created and then died in `build_maps` on "no such table: sessions", with
        nothing stamped or reported (loud); under `p%41q` with a `pAq` beside it, it redacted the
        WRONG store, stamped it and reported it clean (silent)."""
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import redact
        finally:
            sys.path.remove(str(ROOT / "tools"))
        shapes = [tmp_path / folder / "context.db" for folder in AWKWARD]
        shapes += ["//server/share/a#b/context.db", "rel/a#b/context.db", "x/caf\udce9/context.db"]
        if os.name == "nt":
            shapes.append(r"\\server\share\a#b\context.db")
        for shape in shapes:
            assert redact.ro_uri(shape) == store.ro_uri(shape), shape
            assert redact.ro_uri(str(shape)) == store.ro_uri(shape), shape
        target = make(tmp_path / "a#b" / "context.db", "read by the tool")
        con = sqlite3.connect(redact.ro_uri(target), uri=True)
        try:
            assert con.execute("SELECT name FROM here").fetchone()[0] == "read by the tool"
        finally:
            con.close()

    def test_the_node_tools_read_the_store_they_were_pointed_at(self, tmp_path):
        """`c4x.store` runs `tools/segments.mjs` when it is imported, so until that tool stopped
        building the URI by hand the package could not even be imported with its store under
        `a#b`, whatever Python did. Asked the same question of one store under three folders, the
        third with a decoy beside it that the old URI would have read instead."""
        source = Path(store.DB_PATH)
        if source.stat().st_size > 64 * 1024 * 1024:
            pytest.skip("the live store is too large to copy three times; the fixture leg has it")
        make(tmp_path / "pAq" / "context.db", "the decoy")
        asked = (("segments.mjs", "--windows-for-compactions"), ("waste.mjs", "--servers"))
        answers = {}
        for folder in ("plain", "a#b", "p%41q"):
            target = copy_of(source, tmp_path / folder / "context.db")
            env = {**os.environ, "C4X_DB": str(target)}
            for tool, flag in asked:
                done = proc.run([store.NODE, str(ROOT / "tools" / tool), flag], cwd=str(ROOT),
                                env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
                assert done.returncode == 0, f"{tool} under {folder}: {done.stderr[-400:]}"
                answers[(folder, tool)] = done.stdout.replace(str(target), "<store>") \
                                                     .replace(target.as_posix(), "<store>")
        for tool in ("segments.mjs", "waste.mjs"):
            assert answers[("plain", tool)].strip(), f"{tool} said nothing about the plain store"
            assert answers[("a#b", tool)] == answers[("plain", tool)], tool
            assert answers[("p%41q", tool)] == answers[("plain", tool)], tool
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b", "p%41q", "pAq", "plain"]

    def test_the_sweep_sees_what_it_claims_to_and_says_what_it_does_not(self):
        """Known-bad lines, fed to the rule before it is trusted with the tree, and the lines it
        is KNOWN to miss, asserted to be missed, so that this docstring cannot outrun the code.
        The first of each list was in the tree; the rest were shown slipping past an earlier
        sweep, or are kept so that a narrower rule cannot come back unnoticed."""
        bad_python = [
            'con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)',
            "con = sqlite3.connect(f'file:{target.as_posix()}?mode=ro', uri=True)",
            'con = sqlite3.connect(f"file:///{posix}?mode=ro", uri=True)',
            'con = sqlite3.connect(f"file:////{share}?mode=ro", uri=True)',
            'con = sqlite3.connect(f"file:data/{name}.db?mode=ro", uri=True)',
            'con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)',
            'con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)',
            'con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)',
            'con = sqlite3.connect(SCHEME + str(path) + "?mode=ro", uri=True)',
            'con = sqlite3.connect(Template("file:$p?mode=ro").substitute(p=path), uri=True)',
            'con = sqlite3.connect(uri, uri=True)',
            '    uri=True,',
            'con = sqlite3.connect(path.as_uri() + "?mode=ro", uri = True)',
        ]
        fine_python = [
            'con = sqlite3.connect(ro_uri(path), uri=True)',
            'src = sqlite3.connect(store.ro_uri(source), uri=True)',
            '# sqlite3.connect(f"file:{path}?mode=ro", uri=True) is how it used to be written',
            'con = sqlite3.connect(str(path))',
        ]
        slips_python = [
            'con = sqlite3.connect(text, uri=as_uri)',       # the flag is not the literal True
            'con = sqlite3.connect(text, **options)',        # nor spelled at all
        ]
        bad_node = [
            "const d = new DatabaseSync(`file:${DB_PATH}?mode=ro`, { readOnly: true });",
            "const d = new DatabaseSync(`file:///${posix(db)}?mode=ro`, { readOnly: true });",
            "const d = new DatabaseSync('file:' + dbPath, { readOnly: true });",
            "const d = new DatabaseSync(SCHEME + posix(p) + '?mode=ro', { readOnly: true });",
            "const uri = `file:data/${name}?mode=ro`;",
            "const uri = 'file://' + posix(db) + '?mode=ro';",
            "const uri = `file:${p}?nolock=1`;",
            "const uri = ['file:', p, '?immutable=1'].join('');",
        ]
        fine_node = [
            "const d = new DatabaseSync(DB_PATH, { readOnly: true });",
            "// never a `file:` URI built from it: new DatabaseSync(`file:${p}?mode=ro`)",
            " * `?mode=ro` adds nothing once readOnly is set",
            "    OTEL_LOG_RAW_API_BODIES: `file:${BODY_DIR}`,",
        ]
        slips_node = [
            "const SCHEME = 'file:';",                        # no parameter and no open on the line
            "const uri = `file:${posix(db)}`;",
        ]
        assert [line for line in bad_python if not py_by_hand(line)] == []
        assert [line for line in fine_python if py_by_hand(line)] == []
        assert [line for line in bad_node if not node_by_hand(line)] == []
        assert [line for line in fine_node if node_by_hand(line)] == []
        assert [line for line in slips_python if py_by_hand(line)] == [], "a limit closed: say so"
        assert [line for line in slips_node if node_by_hand(line)] == [], "a limit closed: say so"

    def test_nothing_builds_a_sqlite_uri_by_hand(self):
        """THE TREE. Every Python file under `c4x/`, `tools/` and `tests/` (this one excepted: it
        spells the bad forms out in order to look for them) and every `.mjs` under `tools/` and
        `hooks/`. In Python the only lines allowed to set `uri=True` are the ones that call
        `ro_uri`, so there is nothing to allow-list; in node nothing opens through a URI at all.
        The test above says, and asserts, what this cannot see: a flag that is not the literal
        `True`, and a node URI with no query parameter that is built away from the line that opens
        it. `Path.as_uri()` would be flagged too, though it escapes correctly: use `ro_uri`."""
        found = []
        for folder, pattern, by_hand in (
                ("c4x", "**/*.py", py_by_hand), ("tools", "**/*.py", py_by_hand),
                ("tests", "**/*.py", py_by_hand),
                ("tools", "**/*.mjs", node_by_hand), ("hooks", "**/*.mjs", node_by_hand)):
            for source in sorted((ROOT / folder).glob(pattern)):
                if source == Path(__file__).resolve():
                    continue
                for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                    if by_hand(line):
                        found.append(f"{source.relative_to(ROOT).as_posix()}:{number}")
        assert found == [], f"open these through ro_uri, or by the path itself in node: {found}"
        built = [f"{rel}:{n}" for rel in ("c4x/paths.py", "tools/redact.py")
                 for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1)
                 if line.lstrip().startswith("return f\"file:{quote(")]
        assert len(built) == 2, f"built once in the package and once in the tool: {built}"

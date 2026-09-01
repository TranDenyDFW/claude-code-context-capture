"""Export, import and delete, tested where a single character decides whether data survives.

Two of the three bugs found writing this module were exactly that: `PRAGMA table_info(main.t)`
instead of `PRAGMA main.table_info(t)`, and a missing `commit()` before `DETACH` that reported
"database src is locked" and named nothing useful. Neither would have been caught by a test that
only counted rows.

So these build a real SQLite store per test, put deliberately awkward VALUES in it, and compare
what comes back out. Row counts are the weakest possible check here: a shifted column, a NULL that
became an empty string, or an integer that went through a float all preserve the count exactly.
"""
import sqlite3

import pytest

from c4x import projects

# The awkward values, and why each one is here. Every one of these has broken a data export
# somewhere: they are not decoration.
AWKWARD = [
    ("plain", "ordinary text"),
    ("empty string", ""),
    ("null", None),
    ("zero", 0),
    ("windows path", r"P:\ClaudeExt\ccx-engineering-work\tmp\fidpool\F2-p6"),
    ("single quote", "it's a 'quoted' value"),
    ("double quote", 'say "hello"'),
    ("newline", "line one\nline two\r\nline three"),
    ("tab", "a\tb\tc"),
    ("unicode", "café . 日本語 . emoji 🙂 . zero-width\u200bspace"),
    ("comma", "a,b,c"),
    # Beyond 2^53: a value that survives SQLite and JSON but NOT a float round trip.
    ("big integer", 9007199254740993),
    ("negative", -42),
    ("float", 0.1 + 0.2),
    ("sql-ish", "'); DROP TABLE sessions; --"),
    ("percent and underscore", "100% _wild_card_"),
    ("very long", "x" * 5000),
]


def build_store(path, project=r"P:\Alpha", rows=3, extra_project=r"P:\Beta"):
    """A store with the columns this module touches, two projects, and awkward values.

    Its own schema rather than a copy of the real one, so these tests run anywhere and say what
    they mean. The integration test at the bottom uses the real store.
    """
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_slug TEXT, cwd TEXT,
                               transcript_path TEXT, note TEXT, big INTEGER, ratio REAL);
        CREATE TABLE turns (uuid TEXT PRIMARY KEY, session_id TEXT, total_resident INTEGER,
                            text TEXT);
        CREATE TABLE messages (uuid TEXT PRIMARY KEY, session_id TEXT, text TEXT);
        CREATE TABLE tool_calls (uuid TEXT PRIMARY KEY, session_id TEXT);
        CREATE TABLE attachments (uuid TEXT PRIMARY KEY, session_id TEXT);
        CREATE TABLE hook_events (uuid TEXT PRIMARY KEY, session_id TEXT);
        CREATE TABLE session_titles (session_id TEXT, kind TEXT, title TEXT,
                                     PRIMARY KEY (session_id, kind));
        CREATE TABLE compactions (uuid TEXT PRIMARY KEY, session_id TEXT);
        CREATE TABLE compaction_survivors (compaction_uuid TEXT, kind TEXT, uuid TEXT,
                                           PRIMARY KEY (compaction_uuid, uuid));
        CREATE TABLE files (path TEXT PRIMARY KEY, bytes_read INTEGER);
        -- Store-wide tables, with the real columns rather than a stub. They must survive a delete
        -- untouched, and the Diagnostics tab reads them, so a two-column stand-in would make the
        -- one test that renders that tab fail for a reason having nothing to do with exclusions.
        CREATE TABLE probes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ok INTEGER, error TEXT,
          model TEXT, max_tokens INTEGER, total_tokens INTEGER, percentage INTEGER,
          autocompact_source TEXT, auto_compact_threshold INTEGER, is_auto_compact_enabled INTEGER,
          raw_json TEXT);
        CREATE TABLE probe_categories (probe_id INTEGER, name TEXT, tokens INTEGER, color TEXT,
                                       is_deferred INTEGER);
        CREATE TABLE probe_details (probe_id INTEGER, kind TEXT, name TEXT, extra TEXT,
                                    tokens INTEGER, loaded INTEGER);
        CREATE TABLE probe_message_breakdown (probe_id INTEGER, name TEXT, tokens INTEGER);
        CREATE TABLE harvest_runs (
          ts TEXT, mode TEXT, files_seen INTEGER, files_read INTEGER, rewrites INTEGER,
          lines INTEGER, mb REAL, turns INTEGER, compactions INTEGER, unpaired INTEGER,
          ms INTEGER);
    """)
    for which, cwd in ((0, project), (1, extra_project)):
        for n in range(rows):
            sid = f"s{which}-{n}"
            label, value = AWKWARD[(which * rows + n) % len(AWKWARD)]
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                        (sid, f"slug-{which}", cwd, rf"C:\t\{sid}.jsonl", value,
                         9007199254740993, 0.1 + 0.2))
            con.execute("INSERT INTO files VALUES (?,?)", (rf"C:\t\{sid}.jsonl", 1234))
            for i, (_, awkward) in enumerate(AWKWARD):
                con.execute("INSERT INTO turns VALUES (?,?,?,?)",
                            (f"{sid}-t{i}", sid, i * 1000, awkward))
                con.execute("INSERT INTO messages VALUES (?,?,?)", (f"{sid}-m{i}", sid, awkward))
            con.execute("INSERT INTO tool_calls VALUES (?,?)", (f"{sid}-tc", sid))
            con.execute("INSERT INTO attachments VALUES (?,?)", (f"{sid}-a", sid))
            con.execute("INSERT INTO hook_events VALUES (?,?)", (f"{sid}-h", sid))
            con.execute("INSERT INTO session_titles VALUES (?,?,?)", (sid, "custom", label))
            con.execute("INSERT INTO compactions VALUES (?,?)", (f"{sid}-c", sid))
            con.execute("INSERT INTO compaction_survivors VALUES (?,?,?)",
                        (f"{sid}-c", "turn", f"{sid}-t0"))
    con.execute("""INSERT INTO probes (id, ts, ok, model, total_tokens, percentage,
                     auto_compact_threshold, is_auto_compact_enabled)
                   VALUES (1, '2026-01-01T00:00:00Z', 1, 'claude-opus-5', 12345, 3, 800000, 1)""")
    con.execute("""INSERT INTO harvest_runs VALUES
                   ('2026-01-01T00:00:00Z','incremental',2,2,0,10,0.1,5,0,0,7)""")
    con.commit()
    con.close()
    return path


@pytest.fixture
def store_at(tmp_path, monkeypatch):
    """A fresh store per test, with `c4x.store` pointed at it."""
    from c4x import store
    path = build_store(tmp_path / "store.db")
    monkeypatch.setattr(store, "DB_PATH", path)
    return path


def rows_of(path, table):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# The digest, which every other guarantee rests on
# ---------------------------------------------------------------------------
class TestDigest:
    def test_null_and_empty_string_are_not_the_same(self, tmp_path):
        """The single most common way a data export lies.

        `NULL` means unknown and `''` means known-to-be-empty, and this whole app turns on that
        distinction: an unpriced model has an unknown cost, not a zero one. A digest that treated
        them alike would report a clean round trip across exactly that corruption.
        """
        digests = []
        for value in (None, ""):
            path = tmp_path / f"{value!r}.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            con.execute("INSERT INTO t VALUES (1, ?)", (value,))
            con.commit()
            digests.append(projects.digest(con, "t"))
            con.close()
        assert digests[0] != digests[1]

    def test_a_number_and_its_string_are_not_the_same(self, tmp_path):
        digests = []
        for value in (1, "1"):
            path = tmp_path / f"{type(value).__name__}.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
            con.execute("INSERT INTO t VALUES (1, ?)", (value,))
            con.commit()
            digests.append(projects.digest(con, "t"))
            con.close()
        assert digests[0] != digests[1]

    def test_row_order_does_not_change_it(self, tmp_path):
        digests = []
        for order in ([(1, "a"), (2, "b")], [(2, "b"), (1, "a")]):
            path = tmp_path / f"{order[0][0]}.db"
            con = sqlite3.connect(str(path))
            con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            con.executemany("INSERT INTO t VALUES (?,?)", order)
            con.commit()
            digests.append(projects.digest(con, "t"))
            con.close()
        assert digests[0] == digests[1]

    def test_one_changed_character_changes_it(self, tmp_path):
        path = tmp_path / "t.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO t VALUES (1, 'hello')")
        con.commit()
        was = projects.digest(con, "t")
        con.execute("UPDATE t SET v = 'hellp' WHERE id = 1")
        con.commit()
        assert projects.digest(con, "t") != was
        con.close()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
class TestExport:
    def test_carries_every_row_of_the_project_and_none_of_the_other(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        manifest = projects.export(r"P:\Alpha", out)
        assert manifest["sessions"] == 3
        con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        cwds = {r[0] for r in con.execute("SELECT DISTINCT cwd FROM sessions").fetchall()}
        assert cwds == {r"P:\Alpha"}, f"the export leaked another project: {cwds}"
        con.close()

    def test_values_survive_exactly(self, store_at, tmp_path):
        """Row for row, value for value, against the source. This is the real check."""
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        source = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        got = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        for table in ("turns", "messages", "sessions", "session_titles"):
            mine = source.execute(
                f"""SELECT * FROM {table} WHERE session_id IN
                    (SELECT session_id FROM sessions WHERE cwd = ?) ORDER BY 1""",
                (r"P:\Alpha",)).fetchall()
            theirs = got.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            assert mine == theirs, f"{table} did not survive the export byte for byte"
        source.close()
        got.close()

    def test_the_manifest_lives_inside_the_file(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        assert list(tmp_path.glob("out.db*")) == [out], "the export wrote more than one artefact"
        manifest = projects.read_manifest(out)
        assert manifest["project"] == r"P:\Alpha"
        assert manifest["counts"]["turns"] == 3 * len(AWKWARD)

    def test_it_verifies_itself_after_writing(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        ok, problems = projects.verify(out)
        assert ok, problems

    def test_a_tampered_export_stops_verifying(self, store_at, tmp_path):
        """One byte, and the file must stop passing. Otherwise verify is decoration."""
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        con = sqlite3.connect(str(out))
        con.execute("UPDATE turns SET total_resident = total_resident + 1")
        con.commit()
        con.close()
        ok, problems = projects.verify(out)
        assert not ok
        assert any("turns" in p for p in problems)

    def test_an_unknown_project_is_refused_rather_than_exported_empty(self, store_at, tmp_path):
        with pytest.raises(ValueError):
            projects.export(r"P:\NotHere", tmp_path / "out.db")

    def test_a_file_that_is_not_an_export_does_not_verify(self, tmp_path):
        path = tmp_path / "random.db"
        con = sqlite3.connect(str(path))
        con.execute("CREATE TABLE whatever (x)")
        con.commit()
        con.close()
        assert projects.verify(path)[0] is False


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
class TestImport:
    def test_round_trip_restores_every_value(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        before = {t: rows_of(store_at, t) for t in projects.BY_SESSION}
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        projects.import_(out)
        after = {t: rows_of(store_at, t) for t in projects.BY_SESSION}
        for table in projects.BY_SESSION:
            assert before[table] == after[table], f"{table} changed across the round trip"

    def test_importing_twice_inserts_nothing_the_second_time(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        first = projects.import_(out)
        second = projects.import_(out)
        assert sum(first["inserted"].values()) > 0
        assert sum(second["inserted"].values()) == 0, "a second import duplicated rows"

    def test_a_re_import_does_not_overwrite_what_is_already_here(self, store_at, tmp_path):
        """Row counts cannot see this one.

        `INSERT OR REPLACE` would also report "0 inserted" on a second import, because replacing a
        row does not change the count. The difference only shows on a row that has since changed
        locally: IGNORE leaves it, REPLACE silently reverts it to whatever the file says.
        """
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        con = sqlite3.connect(str(store_at))
        con.execute("UPDATE turns SET total_resident = 999999 WHERE uuid = 's0-0-t0'")
        con.commit()
        con.close()
        projects.import_(out)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        got = con.execute("SELECT total_resident FROM turns WHERE uuid = 's0-0-t0'").fetchone()[0]
        con.close()
        assert got == 999999, "the re-import overwrote a row that was already present"

    def test_it_refuses_an_export_that_does_not_verify(self, store_at, tmp_path):
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        con = sqlite3.connect(str(out))
        con.execute("UPDATE messages SET text = 'tampered'")
        con.commit()
        con.close()
        with pytest.raises(ValueError):
            projects.import_(out)

    def test_it_refuses_a_missing_file(self, store_at, tmp_path):
        with pytest.raises(FileNotFoundError):
            projects.import_(tmp_path / "nope.db")

    def test_a_column_this_store_lacks_is_REPORTED_not_shifted(self, store_at, tmp_path):
        """The disaster this design exists to avoid.

        `INSERT INTO t SELECT * FROM src.t` is positional: give it an export with one extra column
        and every value lands one place to the left, silently, with the row count unchanged. This
        import names its columns, so the extra one is dropped and SAID, and the rest stay put.
        """
        out = tmp_path / "out.db"
        projects.export(r"P:\Alpha", out)
        con = sqlite3.connect(str(out))
        con.execute("ALTER TABLE sessions ADD COLUMN from_the_future TEXT")
        con.execute("UPDATE sessions SET from_the_future = 'surprise'")
        # The manifest must be re-stamped, or verify correctly rejects the file before we get here.
        digests = dict(projects.read_manifest(out)["digests"])
        digests["sessions"] = projects.digest(con, "sessions")
        manifest = projects.read_manifest(out)
        manifest["digests"] = digests
        manifest["counts"]["sessions"] = con.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0]
        import json as _json
        con.execute(f"UPDATE {projects.MANIFEST_TABLE} SET value = ? WHERE key = 'manifest'",
                    (_json.dumps(manifest),))
        con.commit()
        con.close()

        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        report = projects.import_(out)
        assert report["dropped_columns"]["sessions"] == ["from_the_future"]
        # And the values that DID load are in their own columns, not shifted along by one.
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        cwds = {r[0] for r in con.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE session_id LIKE 's0-%'").fetchall()}
        con.close()
        assert cwds == {r"P:\Alpha"}, f"columns shifted on import: cwd now holds {cwds}"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
class TestDelete:
    def test_it_refuses_a_confirmation_that_does_not_match(self, store_at, tmp_path):
        with pytest.raises(ValueError):
            projects.delete(r"P:\Alpha", confirm=r"P:\alpha", out_dir=tmp_path)
        # Case differs by ONE character and nothing was removed.
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        assert con.execute("SELECT COUNT(*) FROM sessions WHERE cwd = ?",
                           (r"P:\Alpha",)).fetchone()[0] == 3
        con.close()

    def test_it_exports_before_removing_anything(self, store_at, tmp_path):
        result = projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        backup = tmp_path / result["backup"].split("\\")[-1].split("/")[-1]
        assert backup.exists(), "no backup was written"
        assert projects.verify(backup)[0], "the backup does not verify, so the delete was unsafe"

    def test_it_leaves_every_other_project_alone(self, store_at, tmp_path):
        before = rows_of(store_at, "turns")
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        after = rows_of(store_at, "turns")
        beta_before = [r for r in before if r[1].startswith("s1-")]
        beta_after = [r for r in after if r[1].startswith("s1-")]
        assert beta_before == beta_after, "deleting one project changed another"

    def test_it_removes_survivors_and_the_harvest_offset_too(self, store_at, tmp_path):
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        # Orphaned survivors would make "what the compaction kept" wrong rather than missing.
        orphans = con.execute("""SELECT COUNT(*) FROM compaction_survivors
                                 WHERE compaction_uuid NOT IN
                                 (SELECT uuid FROM compactions)""").fetchone()[0]
        offsets = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?",
                              (r"C:\t\s0-%",)).fetchone()[0]
        con.close()
        assert orphans == 0, "compaction_survivors were left pointing at nothing"
        assert offsets == 0, "the harvest offset survived, so the transcript is skipped forever"

    def test_it_does_not_touch_the_store_wide_tables(self, store_at, tmp_path):
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        con = sqlite3.connect(f"file:{store_at}?mode=ro", uri=True)
        assert con.execute("SELECT COUNT(*) FROM probes").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM harvest_runs").fetchone()[0] == 1
        con.close()

    def test_it_records_the_exclusion_so_the_delete_sticks(self, store_at, tmp_path):
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        assert [r["cwd"] for r in projects.excluded()] == [r"P:\Alpha"]

    def test_keep_capturing_skips_the_exclusion_and_says_so(self, store_at, tmp_path):
        result = projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path,
                                 keep_capturing=True)
        assert result["excluded"] is False
        assert projects.excluded() == []

    def test_include_lifts_it_again(self, store_at, tmp_path):
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        assert projects.include(r"P:\Alpha") == 1
        assert projects.excluded() == []


# ---------------------------------------------------------------------------
# The one place a user can find out this happened
# ---------------------------------------------------------------------------
class TestDiagnosticsShowsIt:
    """An excluded project looks exactly like a project nobody has touched lately.

    The session count simply stops rising. If Diagnostics does not name it, there is no way to
    tell the two apart, and the delete becomes a silent change to what this store means.
    """

    @staticmethod
    def table_rows(node):
        """Every DataTable's data under a node.

        Read off the component, not off `str(node)`. Dash truncates a long repr, so a check
        against the printed form silently stopped seeing the rows once the table grew, and a
        substring match would also pass on a project name that appeared only in a tooltip.
        """
        found = []
        stack = [node]
        while stack:
            item = stack.pop()
            if isinstance(item, (list, tuple)):
                stack.extend(item)
                continue
            data = getattr(item, "data", None)
            if data is not None and type(item).__name__ == "DataTable":
                found.extend(data)
            kids = getattr(item, "children", None)
            if kids is not None:
                stack.append(kids)
        return found

    def test_it_says_so_when_nothing_is_excluded(self, store_at):
        from c4x.tabs.diagnostics import exclusions_layout
        assert self.table_rows(exclusions_layout()) == []
        assert "None." in str(exclusions_layout()[1].children)

    def test_the_excluded_project_is_named(self, store_at, tmp_path):
        from c4x.tabs.diagnostics import exclusions_layout
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        rows = self.table_rows(exclusions_layout())
        assert [r["cwd"] for r in rows] == [r"P:\Alpha"], \
            "the tab does not name the project it stopped capturing"

    def test_the_whole_tab_carries_it_through(self, store_at, tmp_path):
        from c4x.tabs.diagnostics import diagnostics_layout
        projects.delete(r"P:\Alpha", confirm=r"P:\Alpha", out_dir=tmp_path)
        rows = self.table_rows(diagnostics_layout())
        assert any(r.get("cwd") == r"P:\Alpha" for r in rows), \
            "the section renders alone but is missing once composed into the tab"


# ---------------------------------------------------------------------------
# Against the real schema
# ---------------------------------------------------------------------------
def test_every_table_in_the_real_store_is_accounted_for(has_store):
    """A table added later must be REPORTED, not silently dropped from every export.

    This is the check that ages well: it fails the day someone adds a table and does not decide
    whether it belongs to a project.
    """
    from c4x import store
    con = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True)
    unhandled = projects.unhandled_tables(con)
    con.close()
    assert unhandled == [], (
        f"these tables are in the store and in none of the lists in c4x/projects.py: {unhandled}. "
        "Decide whether each belongs to a project before the next export silently omits it.")

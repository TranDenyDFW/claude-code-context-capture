"""The presentation floor has one home.

`session_rows()` hides any session with fewer than SESSION_TURN_FLOOR transcript rows. The number
used to be a literal 5 in two store queries, one tab sentence and two tests, which is how a rule
drifts: one site moves, the others keep the old number, and the page shows totals that no longer
reconcile. The constant is now the only spelling, and this test reads the source to keep it so.

The scan keys on the per-session clause, `GROUP BY session_id HAVING COUNT(*) >= <digit>`. A
clause grouped by session AND target is the repeated-read rule, a different threshold with its own
parameter, and is left alone on purpose.
"""
import re
from pathlib import Path

import pytest

from c4x.store import SESSION_TURN_FLOOR

ROOT = Path(__file__).resolve().parents[1]
LITERAL_FLOOR = re.compile(r"GROUP BY\s+(?:\w+\.)?session_id\s+HAVING\s+COUNT\(\*\)\s*>=\s*\d")


def spelled_out(root=ROOT):
    """Every source line under c4x/ and tests/ that writes the session floor as a number."""
    hits = []
    files = sorted(list((root / "c4x").rglob("*.py")) + list((root / "tests").rglob("*.py")))
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LITERAL_FLOOR.search(line):
                hits.append(f"{path.relative_to(root).as_posix()}:{n}: {line.strip()}")
    return hits


def test_no_query_spells_the_session_floor_as_a_literal():
    hits = spelled_out()
    assert not hits, (
        "the session floor is spelled out instead of using SESSION_TURN_FLOOR:\n" + "\n".join(hits))


def test_the_scan_catches_a_planted_literal_and_leaves_the_parameter_form_alone(tmp_path):
    """The negative control: a scan that cannot fail proves nothing. The planted line is built by
    concatenation so this file's own source does not trip the scan above."""
    (tmp_path / "c4x").mkdir()
    (tmp_path / "tests").mkdir()
    planted = "SELECT 1 FROM turns GROUP BY session_id HAVING COUNT(*) >= " + "5"
    (tmp_path / "c4x" / "planted.py").write_text(f'q("{planted}")\n', encoding="utf-8")
    clean = "\n".join([
        'q("FROM turns GROUP BY t.session_id HAVING COUNT(*) >= ?", (SESSION_TURN_FLOOR,))',
        # The repeated-read rule: grouped by session and target, its own threshold, not this one.
        'q("SELECT 1 FROM tool_calls GROUP BY session_id, target HAVING COUNT(*) >= " + "3")',
    ])
    (tmp_path / "tests" / "clean.py").write_text(clean + "\n", encoding="utf-8")
    hits = spelled_out(tmp_path)
    assert hits == [f"c4x/planted.py:1: q(\"{planted}\")"], hits


def test_the_floor_is_a_small_positive_integer():
    """Not a tautology on the value: a floor of 0 would list every probe and a float would break
    the sentence the tab prints, and either is a change that should fail loudly here first."""
    assert isinstance(SESSION_TURN_FLOOR, int) and 1 <= SESSION_TURN_FLOOR <= 50


# --- the one exemption: a chat the desktop app lists is listed here ------------------------

@pytest.fixture
def tiny_store(tmp_path, monkeypatch):
    """The real schema; `s0-0` cut to ONE transcript row, well under the floor.

    CLEARED AFTERWARDS. The frame is cached for 45 s, and a test file that leaves its store's
    frame in the cache hands it to the next file: on CI this leaked `s0-0` into
    tests/test_front_door.py, whose cohort then had no sessions with turns in the real fixture,
    and the runner read that skip as a fixture gap.
    """
    import sqlite3

    from c4x import store
    from tests.test_projects import build_store, forget_cached_rows
    path = build_store(tmp_path / "store.db")
    con = sqlite3.connect(str(path))
    con.execute("DELETE FROM turns WHERE session_id = 's0-0' AND line_no > 0")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    forget_cached_rows()
    yield path
    forget_cached_rows()


def _record_row(path, sid, gone_at=None, deleted_at=None):
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("""INSERT INTO desktop_records (record_uuid, session_id, dir, title, archived,
                     first_seen, last_seen, gone_at, deleted_at, source)
                   VALUES (?, ?, 'X:/r/a/o', 'T02', 0, '2026-09-01T00:00:00Z',
                           '2026-09-01T00:00:00Z', ?, ?, 'disk')""",
                (f"u-{sid}", sid, gone_at, deleted_at))
    con.commit()
    con.close()


def test_a_chat_below_the_floor_is_not_listed_without_a_record(tiny_store):
    from c4x import store
    assert "s0-0" not in set(store.session_rows(ttl=0)["session_id"])


def test_a_chat_the_desktop_app_holds_a_live_record_for_is_listed_whatever_its_size(tiny_store):
    """Measured on the laptop: T02 (one row), T09 (three) and T14-A (four) sat in the sidebar and
    nowhere on this page. The app lists them, so this page does too."""
    from c4x import store
    from tests.test_projects import forget_cached_rows
    _record_row(tiny_store, "s0-0")
    forget_cached_rows()
    rows = store.session_rows(ttl=0)
    assert "s0-0" in set(rows["session_id"])
    assert int(rows.loc[rows["session_id"] == "s0-0", "turns"].iloc[0]) == 1
    assert store.overview_stats()["listed"] == len(rows), "the card counts what the list draws"


def test_a_record_that_went_or_was_deleted_exempts_nothing(tiny_store):
    from c4x import store
    from tests.test_projects import forget_cached_rows
    _record_row(tiny_store, "s0-0", gone_at="2026-09-15T01:05:00Z")
    forget_cached_rows()
    assert "s0-0" not in set(store.session_rows(ttl=0)["session_id"])

"""The measured cost, beside the estimate and never merged with it.

Claude Code writes a `cost-state` record carrying its own `totalCostUSD`. c4x has always DERIVED
money from tokens and a committed price table, and says on the page that the figure is a lower
bound. Both numbers now appear, and these pin the three properties that make that honest rather
than confusing: the measured figure is blank when absent (never zero), the two cover the same
population, and a store that has never been harvested since the table arrived does not raise.

Built on their own sqlite files rather than the session store, because the interesting cases are
store SHAPES: one with the table and rows, one with the table and no rows, and one without the
table at all. That last one is the shape that shipped a defect once already.
"""
import sqlite3

import pytest

from c4x import store
from c4x.pricing import measured_note

COST_SCHEMA = """
CREATE TABLE cost_state (
  session_id TEXT PRIMARY KEY, started_at TEXT, total_cost_usd REAL,
  total_api_ms INTEGER, total_api_ms_no_retries INTEGER, total_tool_ms INTEGER,
  total_duration_ms INTEGER, lines_added INTEGER, lines_removed INTEGER,
  has_unknown_model_cost INTEGER, file_path TEXT, line_no INTEGER
);
"""


def _store_at(path, rows=None, with_table=True):
    """A store file with cost_state present or absent, and whatever rows are asked for."""
    con = sqlite3.connect(str(path))
    if with_table:
        con.executescript(COST_SCHEMA)
        for sid, cost, incomplete in rows or []:
            con.execute("INSERT INTO cost_state (session_id, total_cost_usd, "
                        "has_unknown_model_cost) VALUES (?,?,?)", (sid, cost, incomplete))
    else:
        con.execute("CREATE TABLE turns (session_id TEXT)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def pointed(monkeypatch):
    """Point store at a given file for the duration of one test."""
    def _use(path):
        monkeypatch.setattr(store, "DB_PATH", path)
    return _use


def test_a_store_without_the_table_does_not_raise(tmp_path, pointed):
    """THE DEFECT THAT SHIPPED ONCE. The dashboard never writes, so it cannot create the table it
    wants to read; a store harvested by an older build has no cost_state and a bare query raises
    rather than returning nothing."""
    pointed(_store_at(tmp_path / "old.db", with_table=False))
    assert store.measured_cost() == {"sessions": 0, "total_usd": None, "incomplete": 0}


def test_a_store_with_the_table_and_no_rows_reports_nothing_not_zero(tmp_path, pointed):
    """Zero is a claim that the sessions were free. None says this app does not know."""
    pointed(_store_at(tmp_path / "empty.db", rows=[]))
    out = store.measured_cost()
    assert out["sessions"] == 0
    assert out["total_usd"] is None


def test_it_totals_only_the_sessions_asked_for(tmp_path, pointed):
    """The population match is the whole point: a measured total over a different session set than
    the estimate reads as a pricing divergence when it is a population difference."""
    pointed(_store_at(tmp_path / "some.db",
                      rows=[("a", 1.0, 0), ("b", 2.0, 0), ("c", 4.0, 0)]))
    assert store.measured_cost(["a", "b"])["total_usd"] == pytest.approx(3.0)
    assert store.measured_cost(["a", "b"])["sessions"] == 2
    assert store.measured_cost()["total_usd"] == pytest.approx(7.0)


def test_an_empty_selection_is_not_the_whole_store(tmp_path, pointed):
    """An empty id list must mean "nothing", not "no filter". Getting this backwards would show a
    store-wide total on a page describing one project.

    WHAT THIS DOES AND DOES NOT PIN, measured by mutation rather than assumed. It FAILS against the
    defect it exists for, `if session_ids:` in place of `if session_ids is not None:`, which is the
    plausible simplification. It does NOT fail if the early return alone is deleted, because SQLite
    accepts `IN ()` as an empty set and returns the same answer; that early return is an
    optimisation, not the correctness guard, and should not be read as one.
    """
    pointed(_store_at(tmp_path / "some.db", rows=[("a", 1.0, 0)]))
    assert store.measured_cost([])["total_usd"] is None


def test_an_incomplete_total_is_counted_as_such(tmp_path, pointed):
    """hasUnknownModelCost is Claude Code flagging its OWN total as incomplete. Dropping that flag
    would present a partial number as a whole one."""
    pointed(_store_at(tmp_path / "flag.db", rows=[("a", 1.0, 1), ("b", 2.0, 0)]))
    assert store.measured_cost()["incomplete"] == 1


# --- the note ---------------------------------------------------------------------------------

def test_the_note_is_silent_when_there_is_nothing_measured():
    """Every store predating cost-state, which is most of them. A sentence explaining an absent
    figure is noise on a page that has no such figure."""
    assert measured_note({"sessions": 0, "total_usd": None, "incomplete": 0}, 5.0) == ""


def test_the_note_names_the_direction_of_the_divergence():
    """The estimate is a stated LOWER BOUND, so measured coming in HIGHER is expected and measured
    coming in LOWER is the surprise. An unsigned percentage would hide which happened."""
    above = measured_note({"sessions": 3, "total_usd": 12.0, "incomplete": 0}, 10.0)
    below = measured_note({"sessions": 3, "total_usd": 8.0, "incomplete": 0}, 10.0)
    assert "ABOVE the estimate" in above and "expected direction" in above
    assert "BELOW the estimate" in below and "does not explain" in below


def test_the_note_reports_a_flagged_incomplete_total():
    note = measured_note({"sessions": 3, "total_usd": 12.0, "incomplete": 2}, 10.0)
    assert "hasUnknownModelCost" in note and "incomplete" in note


def test_the_note_never_claims_the_two_were_reconciled():
    """They cover different populations and neither corrects the other. If this sentence ever goes
    missing, the page is showing two totals with nothing saying why they differ."""
    note = measured_note({"sessions": 3, "total_usd": 12.0, "incomplete": 0}, 10.0)
    assert "not merged" in note


def test_the_note_survives_an_estimate_of_zero():
    """A population with no priced model estimates 0.0, and dividing by it would raise. The
    measured half must still report."""
    note = measured_note({"sessions": 1, "total_usd": 4.0, "incomplete": 0}, 0.0)
    assert "MEASURED" in note and "4.00" in note

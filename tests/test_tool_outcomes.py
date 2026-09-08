"""What a tool call turned out to be, and the ways this app used to get that wrong.

ONE FLAG MEANT TWO OPPOSITE THINGS. Claude Code sets `is_error` on a tool that RAN AND FAILED and
on a tool that NEVER RAN because something refused it. Every "errors" number this app printed was
the two added together. Measured on this store after the backfill: of 6,871 flagged calls, 1,848
are refusals and 1,078 more predate the field that would prove it either way, so 26.9% of what
was called an error never ran and only 57.4% of it is a provable failure. Per tool it is far
worse, because refusal is not spread evenly: of the 41 flagged ExitPlanMode calls, NOT ONE is a
tool that ran and failed.

Every check here was watched to FAIL against the defect it names before it was kept. The ones
marked "gate can fail" are the ones a plausible-looking wrong implementation still passes without.
"""
import ast
import re
import sqlite3
from pathlib import Path

import pytest

from c4x.cli import extract
from c4x.store import (
    OUTCOME_HIDDEN,
    fold_outcomes,
    outcome_sums,
    outcome_text,
)

ROOT = Path(__file__).resolve().parents[1]
NL = chr(10)


# ---------------------------------------------------------------------------
# D1 to D3: the merged cell
# ---------------------------------------------------------------------------
def test_nothing_to_report_renders_nothing_not_a_zero():
    """THE ONE THE MERGED COLUMN EXISTS FOR.

    On a table of forty tools most rows have no failures at all, and forty cells reading "0 errors"
    is forty cells of nothing dressed as a measurement. An implementation that formats the numbers
    it was given passes every other check in this file and fails this one.
    """
    assert outcome_text(0, 0, 0) == ""
    assert outcome_text() == ""


def test_one_is_singular_and_only_the_one_that_is_one():
    assert outcome_text(1, 0, 0) == "1 error"
    assert outcome_text(1, 1, 0) == "1 error, 1 refused"
    assert outcome_text(3, 36, 0) == "3 errors, 36 refused"


def test_no_rendered_value_reads_as_a_number_stored_as_text():
    """The audit's own patterns, PARSED FROM IT rather than restated here.

    tools/table_audit.py fails a cell matching a number followed by up to four letters, so "3 err"
    would be reported as a number stored as text while "3 errors" is not; and the bare word
    "unknown" is one of its placeholder strings, which is why a count always leads. Restating
    either rule in this file would make it a copy that can quietly stop matching its subject, which
    is the failure mode the audit itself was written about.
    """
    source = (ROOT / "tools" / "table_audit.py").read_text(encoding="utf-8")
    pattern = re.search(r'NUMERIC_LOOKING\s*=\s*re\.compile\(\s*r"([^"]+)"', source)
    placeholders = re.search(r"PLACEHOLDER\s*=\s*\{([^}]*)\}", source)
    assert pattern and placeholders, "table_audit.py no longer declares the rules this checks"
    numeric = re.compile(pattern.group(1), re.IGNORECASE)
    bare = {v.strip().strip("\"'").lower() for v in placeholders.group(1).split(",") if v.strip()}

    rendered = [outcome_text(3, 36, 0), outcome_text(1, 0, 0),
                outcome_text(0, 9, 3), outcome_text(0, 0, 2)]
    for text in rendered:
        assert not numeric.match(text), f"{text!r} reads as a number stored as text"
        assert text.strip().lower() not in bare, f"{text!r} is a bare placeholder word"


def test_a_count_always_leads_the_word_unknown():
    assert outcome_text(0, 0, 2) == "2 unknown"


def test_folding_keeps_the_numbers_and_the_column_order(q, has_store):
    """Decision 7, checked rather than asserted in a docstring.

    The merged cell is TEXT and sorts lexicographically, which would put "3 errors" above
    "36 refused" above "9 refused". A reader ordering a table by failures needs the numbers, and so
    does a CSV export, so the three survive as hidden columns.
    """
    df = q(f"SELECT tool_name AS tool, COUNT(*) AS calls, {outcome_sums()} "
           "FROM tool_calls GROUP BY tool_name")
    folded = fold_outcomes(df)
    assert list(folded.columns)[:3] == ["tool", "calls", "outcome"], list(folded.columns)
    for name in OUTCOME_HIDDEN:
        assert name in folded.columns, f"{name} was dropped, so nothing can sort or export by it"
        assert list(folded[name]) == list(df[name]), f"{name} was altered on the way through"


# ---------------------------------------------------------------------------
# D4: a store that predates the column
# ---------------------------------------------------------------------------
@pytest.fixture
def unmigrated(tmp_path, monkeypatch):
    """A store with tool_calls and NO outcome column, which is every store until it is harvested."""
    from c4x import store

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE tool_calls (tool_use_id TEXT PRIMARY KEY, session_id TEXT,
                   ts TEXT, tool_name TEXT, result_bytes INTEGER, is_error INTEGER)""")
    con.executemany("INSERT INTO tool_calls VALUES (?,?,?,?,?,?)", [
        ("a", "s1", "2026-08-01T00:00:00Z", "Bash", 10, 1),
        ("b", "s1", "2026-08-02T00:00:00Z", "Bash", 10, 0),
        ("c", "s1", "2026-08-03T00:00:00Z", "Read", 10, 0),
    ])
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    if hasattr(store.q, "cache_clear"):
        store.q.cache_clear()
    yield path
    if hasattr(store.q, "cache_clear"):
        store.q.cache_clear()


def test_a_store_without_the_column_reports_unknown_not_zero(unmigrated):
    """A BLANK COLUMN THERE WOULD BE INDISTINGUISHABLE FROM "EVERYTHING SUCCEEDED".

    Which is exactly the defect this whole change removes, so the honest answer is to count every
    row as unknown and say so. This is the check that stops a future guard being written the
    defensive way, returning zeros so nothing raises.
    """
    from c4x.store import q

    df = q(f"SELECT tool_name AS tool, COUNT(*) AS calls, {outcome_sums()} "
           "FROM tool_calls GROUP BY tool_name")
    assert not df.empty
    for _, row in df.iterrows():
        assert int(row["unknown"]) == int(row["calls"]), (
            f"{row['tool']} claims to know the outcome of calls this store cannot classify")
        assert int(row["errors"]) == 0 and int(row["refused"]) == 0

    folded = fold_outcomes(df)
    assert all(text.endswith("unknown") for text in folded["outcome"]), list(folded["outcome"])


# ---------------------------------------------------------------------------
# D5: the defect itself, on the real store
# ---------------------------------------------------------------------------
def test_a_tool_whose_failures_are_all_refusals_never_says_error(pane, q, has_store):
    """THE DEFECT, ON WHATEVER STORE THIS SUITE IS POINTED AT.

    Reverting the Tool Calls site makes this read "N errors" for a tool where every flagged call
    was stopped before it ran. Skipped rather than passed where the store holds no refusals at all,
    because a check that silently passes on data that cannot exercise it is not a check.
    """
    refusers = q("""SELECT tool_name, SUM(CASE WHEN outcome = 'refused' THEN 1 ELSE 0 END) refused,
                           SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) errors
                      FROM tool_calls GROUP BY tool_name
                     HAVING refused > 0 AND errors = 0 ORDER BY refused DESC""")
    if refusers.empty:
        pytest.skip("this store records no tool whose flagged calls are all refusals")
    tool = refusers.iloc[0]["tool_name"]
    n = int(refusers.iloc[0]["refused"])

    for table in extract.tables(pane("tab-cost")):
        if "outcome" not in (table["columns"] or []):
            continue
        for row in table["rows"]:
            if row.get("tool") == tool:
                assert row["outcome"] == f"{n:,} refused", row["outcome"]
                assert "error" not in row["outcome"], (
                    f"{tool} never failed; every flagged call was stopped before it ran")
                return
    pytest.fail(f"the Cost tab drew no outcome cell for {tool}")


def test_the_denial_vocabulary_reaches_a_table_not_only_a_note(pane, q, has_store):
    """Decision 8. A note carries the same words and cannot be sorted, filtered or exported."""
    kinds = q("SELECT DISTINCT denial_kind FROM tool_calls WHERE denial_kind IS NOT NULL")
    if kinds.empty:
        pytest.skip("this store records no refusals")
    drawn = set()
    for table in extract.tables(pane("tab-cost")):
        if "denial_kind" in (table["columns"] or []):
            drawn |= {row["denial_kind"] for row in table["rows"]}
    assert drawn == set(kinds["denial_kind"]), (
        "the Refusals table does not report Claude Code's vocabulary unchanged")


# ---------------------------------------------------------------------------
# D6: the two producers of the same three counts
# ---------------------------------------------------------------------------
def _node_fragments():
    """The two strings tools/waste.mjs builds, sliced out of outcomeSums().

    SLICED, NOT GREPPED. A scan of the whole file matched the prose that DESCRIBES the old
    conflated query, in the docstrings that exist to explain why this change happened, and reported
    the two producers as disagreeing with each other. A gate that fires on its own explanation is
    not a gate.
    """
    text = (ROOT / "tools" / "waste.mjs").read_text(encoding="utf-8")
    start = text.index("function outcomeSums(db) {")
    body = text[start:text.index(NL + "}", start)]
    fallback = re.search(r"return '([^']+)';", body)
    built = re.search(r"return `(.+?)`;", body, re.S)
    assert fallback and built, "outcomeSums no longer returns two distinct answers"
    return fallback.group(1), built.group(1)


def _same_sql(a, b):
    """Whitespace and the optional AS keyword are not the property under test."""
    def flat(text):
        return re.sub(r"\s+", " ", text.replace(" AS ", " ")).strip()
    return flat(a) == flat(b)


def test_the_python_and_node_readers_classify_identically(has_store):
    """`c4x/store.py` and `tools/waste.mjs` count the same rows for the same store.

    THE THIRD SIBLING SET THIS REPO HAS HAD, and the previous two both drifted before anything
    noticed. Neither producer is wrong on its own, which is why the property is checked here rather
    than in either of them: what matters is that they AGREE.

    Compares the SQL each one actually PRODUCES, not the source that produces it. The Python side
    is called; the Node side is read out of its file, because a Node process cannot be asked for
    that string from inside pytest without pretending its store exists.
    """
    from c4x.store import outcome_available, outcome_sums

    if not outcome_available():
        pytest.skip("this store predates the outcome column, so the built branch is unreachable")
    _fallback, built = _node_fragments()
    assert _same_sql(outcome_sums(), built), f"python={outcome_sums()!r} node={built!r}"


def test_both_readers_answer_an_unmigrated_store_the_same_way(unmigrated):
    """And about the store they CANNOT classify, which is the easier half to get wrong.

    Zeros here would render a blank column, which is exactly what a store where everything
    succeeded looks like, so both readers would make the strongest possible claim on the weakest
    possible evidence. Both must count every row as unknown, and say it the same way.
    """
    from c4x.store import outcome_sums

    fallback, _built = _node_fragments()
    assert "COUNT(*)" in fallback and "unknown" in fallback, fallback
    assert _same_sql(outcome_sums(), fallback), f"python={outcome_sums()!r} node={fallback!r}"


# ---------------------------------------------------------------------------
# D7: the query shown is the query that ran
# ---------------------------------------------------------------------------
def test_every_diff_table_shows_the_query_that_actually_ran(q, has_store, session_id):
    """THIS FAILED BEFORE THE CHANGE, against a hand-typed paraphrase that had already drifted.

    Three tables on the turn-diff panel were handed a retyping of their own query. One said
    SUM(is_error) where the real one said a CASE, and all three dropped the scope clause AND its
    bound arguments, so a reader who copied what was shown got the whole store back instead of the
    session in front of them.

    Checked as a PROPERTY of every table on the panel, not of the one that was named: the gate that
    catches a retyped query only catches a query someone retyped.
    """
    from c4x import panels

    turns = q("SELECT ts FROM turns WHERE session_id = ? ORDER BY ts", (session_id,))
    if len(turns) < 2:
        pytest.skip("need two turns to diff")
    span = (str(turns["ts"].iloc[0]), str(turns["ts"].iloc[-1]))
    _spend, *queried = panels.turn_diff(session_id, "main", *span)

    for block in queried:
        assert block.sql.count("?") == len(block.params), (
            f"{block.sql.splitlines()[0]} binds {len(block.params)} values into "
            f"{block.sql.count('?')} placeholders")
        # The scope clause is the half that was dropped, and it is what makes the shown query
        # describe this session rather than the whole store.
        assert "session_id" in block.sql, block.sql


# ---------------------------------------------------------------------------
# D8: nothing reads the conflated flag any more
# ---------------------------------------------------------------------------
def test_no_python_surface_counts_is_error():
    """The flag STAYS in the store: it is the raw transcript fact, and dropping it would lose data.

    It simply stops being read by anything that reports a number, because on its own it cannot say
    which of two opposite things happened. Comments and test fixtures may still name it; a query
    may not.
    """
    offenders = []
    for path in sorted((ROOT / "c4x").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # DOCSTRINGS ARE EXCLUDED BY IDENTITY, not by looking like prose. Two of them quote
        # the old conflated query verbatim, because explaining what was wrong is the reason
        # they exist, and a line-based scan reported those explanations as the offence.
        docs = {id(ast.get_docstring(n, clean=False)) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node.value) in docs or "is_error" not in node.value:
                continue
            if re.search(r"(SUM|COUNT|CASE|WHERE|SELECT)[^\n]*is_error",
                         node.value, re.IGNORECASE):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: "
                                 f"{node.value.strip()[:90]}")
    assert not offenders, "surfaces still counting the conflated flag:\n" + "\n".join(offenders)


def test_the_registry_describes_the_columns_that_replaced_it():
    """`errors` had to LEAVE the help registry, not be reworded, and its replacements to land."""
    from c4x.theme import COLUMN_HELP

    assert "errors" not in COLUMN_HELP, "a column nothing renders still carries help"
    assert "permission-rule" in COLUMN_HELP["denial_kind"], (
        "the help does not say that permission-rule covers both a settings rule and a hook, so the "
        "vocabulary misleads while being technically exact")
    assert "2.1.202" in COLUMN_HELP["outcome"], (
        "the help does not say which builds could not record a reason")

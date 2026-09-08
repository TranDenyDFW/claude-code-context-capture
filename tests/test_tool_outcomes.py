"""What a tool call turned out to be, and the ways this app used to get that wrong.

ONE FLAG MEANT TWO OPPOSITE THINGS. Claude Code sets `is_error` on a tool that RAN AND FAILED and
on a tool that NEVER RAN because something refused it. Every "errors" number this app printed was
the two added together. Measured on this store after the backfill: of 6,871 flagged calls, 1,848
are refusals and 1,078 more predate the field that would prove it either way, so 26.9% of what
was called an error never ran and only 57.4% of it is a provable failure. Per tool it is far
worse, because refusal is not spread evenly: of the 41 flagged ExitPlanMode calls, NONE can be
proven to have run and failed. Reading their result text suggests about 3 were real, and this
app reports the provable answer rather than the suggested one.

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

    The merged cell is TEXT, so the numbers have to survive somewhere: a row click and the CSV
    both need them. This is the DataFrame half of that property. The table half, that a hidden
    column was first a declared one, is asserted against the rendered app further down, because
    checking it here is exactly the mistake that let the argument be a no-op at three sites.
    """
    df = q(f"SELECT tool_name AS tool, COUNT(*) AS calls, {outcome_sums()} "
           "FROM tool_calls GROUP BY tool_name")
    folded = fold_outcomes(df)
    assert list(folded.columns)[:3] == ["tool", "calls", "outcome"], list(folded.columns)
    for name in OUTCOME_HIDDEN:
        assert name in folded.columns, f"{name} was dropped, so nothing can read the number"
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
def test_no_table_calls_a_refused_tool_an_error(pane, q, has_store):
    """THE DEFECT, ON WHATEVER STORE THIS SUITE IS POINTED AT.

    Reverting any site makes the tool whose flagged calls were all stopped before they ran read
    "N errors" again.

    ASSERTED OF EVERY TABLE, not of the first one that looked right. The first version matched by
    shape, took the first table with a `tool` and an `outcome` column, and compared its cell to
    the store-wide total. Adding a site broke it, because the table it then matched groups by
    input hash and honestly reports a smaller number. A gate that can be broken by adding a
    correct site was testing the site, not the property.
    """
    refusers = q("""SELECT tool_name, SUM(CASE WHEN outcome = 'refused' THEN 1 ELSE 0 END) refused,
                           SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) errors
                      FROM tool_calls GROUP BY tool_name
                     HAVING refused > 0 AND errors = 0 ORDER BY refused DESC""")
    if refusers.empty:
        pytest.skip("this store records no tool whose flagged calls are all refusals")
    tool = refusers.iloc[0]["tool_name"]

    checked = 0
    for table in extract.tables(pane("tab-cost")):
        if "outcome" not in (table["columns"] or []):
            continue
        for row in table["rows"]:
            if row.get("tool") != tool:
                continue
            checked += 1
            assert "error" not in row["outcome"], (
                f"{tool} never failed: every flagged call was stopped before it ran, but a "
                f"table reports {row['outcome']!r}")
            assert "refused" in row["outcome"], row["outcome"]
            # The cell must agree with the row it was folded from, whatever population that row
            # covers. Comparing to a store-wide total is what coupled this to one table.
            assert row["outcome"] == outcome_text(row["errors"], row["refused"], row["unknown"])
    assert checked, f"no table on the Cost tab drew an outcome cell for {tool}"

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


# ---------------------------------------------------------------------------
# The two properties, asked of every table the app renders
# ---------------------------------------------------------------------------
def _cols_of(table):
    """A DataTable's declared column specs. Dash omits an unset prop rather than defaulting"""
    return [c for c in (getattr(table, "columns", None) or []) if isinstance(c, dict)]


def _every_table(app, pane, session_id, other_session_id):
    """Every table on every tab, with the query that produced it.

    The query travels on the wrapper `panels.with_query` puts around each block, so it reaches
    its table by CONTAINMENT. Paired by position it could mis-attribute, and a query shown
    against a table that did not produce it is worse than no query at all. Same walk as
    `c4x/api/main.py`, for the same reason.
    """
    from c4x.panels import QUERY_MARK

    def walk(node, query, found):
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, query, found)
            return found
        if not hasattr(node, "_prop_names"):
            return found
        if QUERY_MARK in str(getattr(node, "className", "") or "").split():
            props = (node.to_plotly_json().get("props", {}) or {})
            query = props.get("data-query") or query
        if type(node).__name__ == "DataTable":
            found.append((node, query))
        for name in getattr(node, "_prop_names", []):
            if name != "id":
                walk(getattr(node, name, None), query, found)
        return found

    # THE APP'S OWN TAB LIST, never a copy of it here. A copy is a second place to forget, and
    # a tab this file did not know about is exactly where an unchecked table would sit. Naming the
    # population's SOURCE rather than its members is the whole point of both gates below.
    out = []
    for tab in [t[0] for t in app.TABS]:
        for table, query in walk(pane(tab, session_id), None, []):
            out.append((tab, table, query or ""))
        if other_session_id:
            for table, query in walk(pane(tab, None), None, []):
                out.append((tab, table, query or ""))
    assert out, "no tables rendered, so neither gate below could have failed"
    return out


def test_a_hidden_column_was_first_a_declared_one(app, pane, session_id, other_session_id,
                                                  has_store):
    """`hidden_columns` HIDES A COLUMN THAT EXISTS. Naming an undeclared one hides nothing.

    And it costs that column everything a column has: no header, so no native sort; no filter
    cell; and no place in the CSV, which is built from `columns` under either export_columns
    setting. Decision 7 exists to keep the three counts EXPORTABLE behind the
    merged text cell, and naming them without declaring them delivered none of it.

    THIS REPO PAID FOR IT ONCE ALREADY, in c4x/tabs/summary.py, where an undeclared hidden
    column kept a row out of derived_viewport_data and every click on the findings table was a
    silent no-op. It was reintroduced at three sites in one commit because the gate written for
    the property asserted on the DataFrame instead of on the table. This one asks the table.
    """
    undeclared = []
    for tab, table, _query in _every_table(app, pane, session_id, other_session_id):
        declared = {c.get("id") for c in _cols_of(table)}
        for name in (getattr(table, "hidden_columns", None) or []):
            if name not in declared:
                undeclared.append(f"{tab}/{getattr(table, 'id', None) or '(anonymous)'}: {name}")
    assert not undeclared, ("hidden columns that were never declared, so they hide nothing and "
                            "cannot be exported: " + ", ".join(sorted(set(undeclared))))


#: Tables that read tool_calls and deliberately say nothing about how the calls turned out.
#: Each carries its reason, and the reason is checked, not trusted.
OUTCOME_EXEMPT = {
    "denial_kind": "every row IS a refusal, so an outcome column would say `refused` N times",
    "reads": ("a refused read is not a read, and it would be a real defect in this count, but "
              "the store holds 4 refused reads against 47,117 and none of them falls in a "
              "group this table shows. A column blank on every row is the noise the merged "
              "cell exists to avoid. Revisit if the refused count ever reaches this table."),
}


def test_every_table_that_reports_tool_calls_says_how_they_turned_out(
        app, pane, session_id, other_session_id, has_store):
    """THE POPULATION IS EVERY TABLE THAT READS tool_calls, not the six the plan listed.

    Taking that list as the population is the error this gate removes. Two tables were missed,
    MCP Calls and Multi-Session Input, both on the tab the change edited, and on the live store
    one MCP row hid 176 errors and 7 unknown behind a bare invocation count. A gate that knew
    the list would have walked past both.

    An exemption must be DECLARED and must still describe its table, in the manner of
    tools/table_audit.py: a stale exemption silently grants coverage to a table that has since
    started needing it.
    """
    missing, unused = [], set(OUTCOME_EXEMPT)
    for tab, table, query in _every_table(app, pane, session_id, other_session_id):
        if "tool_calls" not in query:
            continue
        ids = [c.get("id") for c in _cols_of(table)]
        if "outcome" in ids:
            continue
        excuse = next((k for k in OUTCOME_EXEMPT if k in ids), None)
        if excuse:
            unused.discard(excuse)
            continue
        missing.append(f"{tab}: {ids}")
    assert not missing, ("tables reporting tool calls that say nothing about how they turned "
                         "out: " + " | ".join(sorted(set(missing))))
    assert not unused, (f"exemptions describing no rendered table, so they are stale: {unused}")

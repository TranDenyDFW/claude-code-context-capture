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

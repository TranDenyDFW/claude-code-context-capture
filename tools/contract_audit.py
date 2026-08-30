"""The data-integrity audit, done against the API payload instead of a Dash component tree.

    python tools/contract_audit.py            # audit every tab, every selection state
    python tools/contract_audit.py --tab tab-cost
    python tools/contract_audit.py --self-test

WHY THIS EXISTS. `tools/table_audit.py` is 988 lines and roughly half of it cannot outlive Dash: it
reads Dash's `GLOBAL_CALLBACK_LIST`, walks `dash_table.DataTable` objects, and reconstructs the call
graph of `app.py` to prove every table-building path was exercised. None of that has a meaning once
the frontend is React and the server returns JSON.

The other half does. "A numeric column holding the STRING '1,234'" and "a measured zero rendered as
'-'" are defects in the DATA, and the data is exactly what an API returns. Those checks are carried
over here, and they are IMPORTED from `table_audit` rather than restated, because two copies of a
rule like `PLACEHOLDER` drift and the drift is silent: the old audit keeps passing on a rule the new
one no longer has.

WHAT IS LOST, said plainly rather than left for someone to discover. This audit sees what the API
returns for the selections it asks for. It cannot prove that every code path that can build a table
was reached, which is what the coverage half of `table_audit.py` did by instrumenting construction.
A tab that only builds a particular table under a selection nobody thought to list here is not
covered, and this file will not say so. That is a real reduction and it is the price of the
frontend no longer being a Python object graph. Until Dash is retired, BOTH audits run.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

# ONE definition of each rule, imported. See the module docstring: a second copy drifts silently.
from table_audit import (  # noqa: E402
    NUMERIC_LOOKING,
    PLACEHOLDER,
    PROSE_MIN_LENGTH,
    TEXT_BY_NATURE,
)

STATES = [
    ("nothing selected", {}),
    ("one session", {"session": "{session}"}),
    ("one session, subagents", {"session": "{session}", "scope": "all"}),
    ("a cohort", {"cohort": "{cohort}"}),
]


def table_faults(where, table):
    """Every way one table's DATA is wrong, as readable lines.

    The same two rules `table_audit.findings` applies, against the payload rather than the
    component: a numeric-looking string where a number belongs, and a placeholder standing in for a
    number that was actually measured.
    """
    out = []
    tid = table.get("id") or "(anonymous)"
    rows = table.get("rows") or []
    columns = table.get("columns") or []

    for column in columns:
        if not isinstance(column, str):
            out.append(f"{where}/{tid}: column name {column!r} is not a string")

    # Numeric BY CONTENT, because a declared type is not available over the wire and was never
    # enough anyway: five of this app's tables declare nothing at all.
    numeric = {column for row in rows for column, value in row.items()
               if isinstance(value, (int, float)) and not isinstance(value, bool)}
    # Prose is exempted by MEASUREMENT, not by name. The numeric rule once fired on a message whose
    # entire text was the character "1".
    prose = {column for row in rows for column, value in row.items()
             if isinstance(value, str) and len(value) > PROSE_MIN_LENGTH}

    for index, row in enumerate(rows):
        for column, value in row.items():
            if column in TEXT_BY_NATURE or column in prose or not isinstance(value, str):
                continue
            text = value.strip()
            if text and NUMERIC_LOOKING.match(text):
                out.append(f"{where}/{tid}: row {index} column {column!r} is the STRING {value!r}")
            elif column in numeric and (text in PLACEHOLDER or not text):
                out.append(f"{where}/{tid}: row {index} column {column!r} is the placeholder "
                           f"{value!r} in a numeric column")
    return out


def contract_faults(where, payload):
    """Every way a payload breaks the shape a frontend is entitled to rely on."""
    out = []
    for key in ("tables", "figures", "text"):
        if key not in payload:
            out.append(f"{where}: the payload has no {key!r}")
    for table in payload.get("tables", []):
        tid = table.get("id") or "(anonymous)"
        rows = table.get("rows") or []
        columns = set(table.get("columns") or [])
        # A DECLARED column with no value in any row renders as an empty stripe down the table.
        if rows:
            present = {key for row in rows for key in row}
            for column in columns - present:
                out.append(f"{where}/{tid}: column {column!r} is declared "
                           "and absent from every row")
        # A tooltip on a column that is not shown is help nobody can reach. Hidden columns are the
        # normal case for extra ROW keys, not for tooltips: a tooltip is attached to a header.
        for column in (table.get("tooltips") or {}):
            if columns and column not in columns:
                out.append(f"{where}/{tid}: tooltip on {column!r}, which is not a shown column")
    return out


def help_faults(payloads):
    """Every COLUMN_HELP entry that never reaches a header a reader can hover.

    37 hand-written explanations live in `c4x/theme.py` and nowhere else. One attached to a column
    that no longer exists is invisible: nothing fails, the help simply never appears.

    NOT A LIST OF DEAD ENTRIES, and this is why the check is opt-in rather than part of the audit.
    It reports every entry that did not appear IN THE SELECTIONS THIS AUDIT ASKED FOR, and several
    of this app's tables live in sub-panels that a reader opens by clicking a row. Run against the
    live store, nine entries came back unreached and five of them belong to `c4x/probe_detail.py`,
    a panel this audit never opens. They are reachable in the app and would be reported here
    forever.

    So the finding is worded as what it is: a column this run did not reach. Acting on it means
    opening the panel it belongs to and checking, not deleting the help.
    """
    from c4x.theme import COLUMN_HELP
    reached = set()
    for payload in payloads:
        for table in payload.get("tables", []):
            reached.update(table.get("tooltips") or {})
    missing = sorted(set(COLUMN_HELP) - reached)
    return [f"COLUMN_HELP entry {column!r} was not reached by any selection this audit asked for. "
            "It may still be live in a sub-panel; check before deleting it."
            for column in missing]


def run(only_tab=None, strict_help=False):
    from fastapi.testclient import TestClient

    import app as module
    from c4x import store
    from c4x.api.main import api

    client = TestClient(api)
    found = store.q("""
        SELECT t.session_id FROM turns t
          JOIN compactions c ON c.session_id = t.session_id
         GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""")
    if found.empty:
        found = store.q("SELECT session_id FROM turns GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1")
    session = None if found.empty else found.iloc[0]["session_id"]
    cohorts = store.cohort_options()
    cohort = cohorts[0]["value"] if cohorts else None

    tabs = [t[0] for t in module.TABS]
    if only_tab:
        if only_tab not in tabs:
            print(f"unknown tab {only_tab!r}. Known: {', '.join(tabs)}")
            return 2
        tabs = [only_tab]

    problems, payloads, checked = [], [], 0
    for tab in tabs:
        for label, template in STATES:
            params, skip = {}, False
            for key, value in template.items():
                resolved = (session if value == "{session}"
                            else cohort if value == "{cohort}" else value)
                if resolved is None:
                    skip = True
                    break
                params[key] = resolved
            if skip:
                # Reported, never silently dropped: an audit that skips is an audit that passes.
                problems.append(f"SKIPPED {tab} / {label}: this store cannot build that selection")
                continue
            response = client.get(f"/api/tab/{tab}", params={**params, "no_cache": "1"})
            if response.status_code != 200:
                problems.append(f"{tab} / {label}: HTTP {response.status_code}")
                continue
            payload = response.json()
            payloads.append(payload)
            checked += 1
            where = f"{tab} / {label}"
            problems.extend(contract_faults(where, payload))
            for table in payload.get("tables", []):
                problems.extend(table_faults(where, table))

    # Only meaningful over a FULL run: auditing one tab would report every other tab's help as
    # unreached, which is a wrong answer that looks like 30 findings.
    if strict_help and not only_tab:
        problems.extend(help_faults(payloads))

    real = [p for p in problems if not p.startswith("SKIPPED")]
    for line in problems:
        print(f"  {'' if line.startswith('SKIPPED') else 'FAULT  '}{line}")
    print(f"\n  {checked} payload(s) across {len(tabs)} tab(s)")
    if real:
        print(f"  AUDIT FAIL  {len(real)} fault(s)")
        return 1
    print("  AUDIT PASS  every table's data is the type it claims to be")
    return 0


def self_test():
    """The rules, fed data they MUST reject. An audit that only ever passes is furniture."""
    ok = {"id": "t", "columns": ["n", "name"],
          "rows": [{"n": 1, "name": "fine"}], "tooltips": {}}
    cases = [
        ("clean data produces no faults", table_faults("w", ok) == []),
        # The defect this rule exists for: a count arriving as text, which sorts and sums wrongly
        # everywhere downstream and looks identical on screen.
        ("a numeric-looking STRING is a fault",
         len(table_faults("w", {"id": "t", "columns": ["n"], "rows": [{"n": "1,234"}]})) == 1),
        ("a currency string is a fault",
         len(table_faults("w", {"id": "t", "columns": ["n"], "rows": [{"n": "$12.50"}]})) == 1),
        # `survivors` rendered "-" for a measured zero, so 44 rows claimed a count was unknown when
        # it had been counted.
        ("a placeholder in a numeric column is a fault",
         len(table_faults("w", {"id": "t", "columns": ["n"],
                                "rows": [{"n": 5}, {"n": "-"}]})) == 1),
        ("an EMPTY string in a numeric column is a fault too",
         len(table_faults("w", {"id": "t", "columns": ["n"],
                                "rows": [{"n": 5}, {"n": ""}]})) == 1),
        # None is how the API says "unknown", and it is CORRECT: it is what keeps an unpriced model
        # blank instead of free. Flagging it would push someone to render a zero.
        ("a null is NOT a fault, because unknown is a real answer",
         table_faults("w", {"id": "t", "columns": ["n"], "rows": [{"n": 5}, {"n": None}]}) == []),
        ("a version string is not a number", table_faults(
            "w", {"id": "t", "columns": ["version"], "rows": [{"version": "2.0.14"}]}) == []),
        ("prose that happens to be one digit is not a number",
         table_faults("w", {"id": "t", "columns": ["preview"],
                            "rows": [{"preview": "1"},
                                     {"preview": "x" * (PROSE_MIN_LENGTH + 1)}]}) == []),
        ("a non-string column name is a fault",
         len(table_faults("w", {"id": "t", "columns": [7], "rows": []})) == 1),
        ("a declared column absent from every row is a fault",
         len(contract_faults("w", {"tables": [{"id": "t", "columns": ["a", "ghost"],
                                               "rows": [{"a": 1}]}],
                                   "figures": [], "text": []})) == 1),
        ("a tooltip on a column nobody can see is a fault",
         len(contract_faults("w", {"tables": [{"id": "t", "columns": ["a"], "rows": [{"a": 1}],
                                               "tooltips": {"gone": "help"}}],
                                   "figures": [], "text": []})) == 1),
        ("a hidden ROW key is not a fault, because that is how ids travel",
         contract_faults("w", {"tables": [{"id": "t", "columns": ["a"],
                                           "rows": [{"a": 1, "session_id": "x"}]}],
                               "figures": [], "text": []}) == []),
        ("a payload missing a whole section is a fault",
         len(contract_faults("w", {"tables": []})) == 2),
        ("the rules come from table_audit, not a second copy",
         "-" in PLACEHOLDER and "version" in TEXT_BY_NATURE),
    ]
    bad = 0
    for what, passed in cases:
        if not passed:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tab", help="audit one tab only")
    ap.add_argument("--strict-help", action="store_true",
                    help="also report COLUMN_HELP entries this run did not reach. Opt-in: some "
                         "live in sub-panels this audit never opens, so it is a lead not a verdict")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    return run(args.tab, args.strict_help)


if __name__ == "__main__":
    sys.exit(main())

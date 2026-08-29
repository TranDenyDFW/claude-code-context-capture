"""Fail if any table in the dashboard holds a number as a string.

Formatting a number for display used to mean replacing it: fmt_tokens(997800) produced the string
"997.8k" and that string went into the table. Two things break. Native sort becomes lexicographic,
so 9 sorts after 80,000 and "1000k" sorts before "99.2k". And every table in this app inherits
export_format="csv" from TABLE_STYLE, so the CSV receives display text rather than values, which is
the whole point of offering an export.

The fix is to leave the value numeric and let Dash format the display. This audit exists because
the fix was applied twice to the columns someone happened to look at, and both times the sibling
columns in other tables were missed. It renders every tab and every table-bearing callback and
reports the whole class at once.

Run: python tools/table_audit.py     (exit 0 clean, 1 if anything is stringified or the audit
                                      cannot prove itself able to fail)
"""
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m  # noqa: E402
from dash import dash_table, html  # noqa: E402

# Deliberately loose: it matches anything a reader would call a number, including the "997.8k" and
# "24.7%" shapes this app produced. Columns that hold a genuine string with digits in it, such as a
# version, are excluded by name below rather than by making this pattern clever.
NUMERIC_LOOKING = re.compile(r"^-?[\d,]+(\.\d+)?\s*(k|M|G|B|KB|MB|GB|%)?$", re.I)

# A version is "2.0.14" and a timestamp is "10:32:07". Both hold digits and neither is a quantity.
TEXT_BY_NATURE = {"version", "ts", "last active", "session_id", "uuid"}


def tables_in(node, out):
    if isinstance(node, dash_table.DataTable):
        out.append(node)
        return
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            tables_in(child, out)
    elif children is not None:
        tables_in(children, out)


def findings(label, node):
    found = []
    tables = []
    tables_in(node, tables)
    for table in tables:
        tid = getattr(table, "id", None) or "(anonymous)"
        for row in (getattr(table, "data", None) or []):
            for col, val in row.items():
                if col in TEXT_BY_NATURE or not isinstance(val, str):
                    continue
                if val.strip() and NUMERIC_LOOKING.match(val.strip()):
                    found.append((label, tid, col, val))
    return found


def main():
    hits, errors = [], []

    session_id = m.q("""SELECT session_id FROM compactions GROUP BY 1
                        ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0]["session_id"]
    cohorts = [o["value"] for o in m.cohort_options()
               if str(o["value"]).startswith("section::")]
    cases = [("no selection", (None, "main", None)),
             ("one session", (session_id, "main", None)),
             ("a cohort", (None, "main", cohorts[0] if cohorts else None))]

    for tab_id, _label, layout in m.TABS:
        for case, args in cases:
            try:
                hits += findings(f"{tab_id} / {case}", layout(*args))
            except Exception as exc:
                errors.append(f"{tab_id} / {case}: {type(exc).__name__}: {exc}")

    # A table built inside a callback is unreachable from the tab walk, and the compaction detail
    # builds one. Pick a compaction that HAS dropped rows: the first version of this audit took
    # whichever came first, got an empty set, never built the table, and reported one defect fewer
    # than existed.
    uuid = None
    for _, row in m.q("SELECT uuid FROM compactions ORDER BY rowid DESC LIMIT 40").iterrows():
        if not m.compaction_dropped(row["uuid"]).empty:
            uuid = row["uuid"]
            break
    if uuid is None:
        errors.append("no compaction with dropped rows, so the detail table was never audited")
    else:
        hits += findings("callback / compaction detail",
                         m._compaction_clicked({"row": 0}, [{"uuid": uuid}]))

    # The audit must be able to fail, or a clean run means nothing.
    fixture = findings("fixture", html.Div(dash_table.DataTable(
        columns=[{"name": "n", "id": "n"}], data=[{"n": "1,234"}])))

    seen = set()
    for label, tid, col, val in hits:
        key = (tid, col)
        if key not in seen:
            seen.add(key)
            print(f"  STRINGIFIED  {tid}.{col} = {val!r}   (first seen in {label})")
    for err in errors:
        print(f"  ERROR  {err}")
    print(f"  tables audited across {len(m.TABS)} tabs x {len(cases)} selections plus one callback")
    print(f"  columns holding numbers as strings: {len(seen)}")
    print(f"  known-bad fixture was detected: {bool(fixture)}")

    ok = not seen and not errors and bool(fixture)
    print("AUDIT PASS" if ok else "AUDIT FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Fail if any table in the dashboard holds a number as a string, or if a table went unaudited.

Formatting a number for display used to mean replacing it: fmt_tokens(997800) produced the string
"997.8k" and that string went into the table. Two things break. Native sort becomes lexicographic,
so 9 sorts after 80,000 and "1000k" sorts before "99.2k". And every table here inherits
export_format="csv" from TABLE_STYLE, so the CSV receives display text rather than values, which is
the whole point of offering an export.

This audit exists because the fix kept being applied to whichever columns someone happened to look
at. Three rounds: two columns, then eleven, then a thirteenth site in a callback that the second
round's own sweep could not reach and still called itself a sweep of every table.

So reachability is checked mechanically rather than asserted. The audit parses app.py for every
DataTable construction site, then records the line of every DataTable actually constructed while it
runs, and FAILS on any site it did not reach. A table this audit cannot reach is a failure, not a
silent omission, which is the only way a clean run means anything.

Run: python tools/table_audit.py    (exit 0 clean, 1 on any stringified value, unreached table, or
                                     failure of the audit to detect its own known-bad fixture)
"""
import ast
import contextlib
import os
import re
import sys
import traceback
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as m  # noqa: E402
from dash import dash_table, html  # noqa: E402

# Deliberately loose: it matches anything a reader would call a number, including the "997.8k",
# "51.5 MB" and "24.7%" shapes this app produced. Columns holding a genuine string with digits in
# it, such as a version, are excluded by name below rather than by making this pattern clever.
NUMERIC_LOOKING = re.compile(r"^-?[\d,]+(\.\d+)?\s*(x|k|M|G|B|KB|MB|GB|%)?$", re.I)

# A placeholder is the other way a numeric column becomes text. `survivors` rendered "-" for a
# measured zero, so 44 rows claimed the count was unknown when it had been counted.
PLACEHOLDER = {"-", "--", "n/a", "N/A", "none", "unknown"}

# A version is "2.0.14" and a timestamp is "10:32:07". Both hold digits, neither is a quantity.
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
    out = []
    tables = []
    tables_in(node, tables)
    for table in tables:
        tid = getattr(table, "id", None) or "(anonymous)"
        numeric = {c["id"] for c in (getattr(table, "columns", None) or [])
                   if c.get("type") == "numeric"}
        for row in (getattr(table, "data", None) or []):
            for col, val in row.items():
                if col in TEXT_BY_NATURE or not isinstance(val, str):
                    continue
                text = val.strip()
                if text and NUMERIC_LOOKING.match(text):
                    out.append(("stringified", label, tid, col, val))
                elif text in PLACEHOLDER and col in numeric:
                    out.append(("placeholder", label, tid, col, val))
    return out


@contextlib.contextmanager
def recording_construction(built):
    """Record the app.py line of every DataTable actually constructed while this is open.

    Reachability by observation, not by name. The first attempt tracked which functions the audit
    called, which reported evidence_block() and breakdown_body() as unreached when both are helpers
    a tab layout calls: the audit had rendered their tables and still failed itself.
    """
    original = dash_table.DataTable.__init__

    def spy(self, *args, **kwargs):
        for frame in reversed(traceback.extract_stack()):
            if os.path.basename(frame.filename) == "app.py":
                built.add(frame.lineno)
                break
        return original(self, *args, **kwargs)

    dash_table.DataTable.__init__ = spy
    try:
        yield
    finally:
        dash_table.DataTable.__init__ = original


def table_sites():
    """Every DataTable construction site in app.py, by enclosing top-level function.

    Static, so it sees the sites no rendering path happens to take. That is the point: the gap this
    catches is a table built somewhere the audit never calls.
    """
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    sites = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            here = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.col_offset == 0 else fn)
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == "DataTable":
                    sites.append((here, child.lineno))
            walk(child, here)

    walk(tree, None)
    return sites


def main():
    hits, errors, built = [], [], set()

    session_id = m.q("""SELECT session_id FROM compactions GROUP BY 1
                        ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0]["session_id"]
    cohorts = [o["value"] for o in m.cohort_options()
               if str(o["value"]).startswith("section::")]
    cases = [("no selection", (None, "main", None)),
             ("one session", (session_id, "main", None)),
             ("a cohort", (None, "main", cohorts[0] if cohorts else None))]

    recorder = recording_construction(built)
    recorder.__enter__()

    for tab_id, _label, layout in m.TABS:
        for case, args in cases:
            try:
                hits += findings(f"{tab_id} / {case}", layout(*args))
            except Exception as exc:
                errors.append(f"{tab_id} / {case}: {type(exc).__name__}: {exc}")

    # Tables built inside a callback are unreachable from the tab walk, so each is called directly.
    #
    # Pick a compaction that HAS dropped rows. The first version of this audit took whichever came
    # first, got an empty set, never built the table, and reported one defect fewer than existed.
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

    # The compare table is built by a callback too, and comparing one session against a whole
    # cohort is the case that exercises the unequal-arm rows.
    a = m.selection_metrics(session_id, "main", None)
    if cohorts:
        b = m.selection_metrics(None, "main", cohorts[0])
        hits += findings("callback / compare", m.compare_table(
            m.population_label(session_id, "main", None), a,
            m.population_label(None, "main", cohorts[0]), b))
    else:
        errors.append("no cohort available, so the compare table was never audited")

    recorder.__exit__(None, None, None)

    # Reachability, observed rather than claimed.
    sites = table_sites()
    for fn, lineno in sites:
        if lineno not in built:
            errors.append(f"app.py:{lineno} builds a DataTable inside {fn}(), "
                          f"which this audit never reached")

    # The audit must be able to fail, or a clean run means nothing. Both shapes are fed to it.
    fixture = findings("fixture", html.Div(dash_table.DataTable(
        columns=[{"name": "n", "id": "n", "type": "numeric"}],
        data=[{"n": "1,234"}, {"n": "-"}])))
    fixture_kinds = {kind for kind, *_ in fixture}

    seen = set()
    for kind, label, tid, col, val in hits:
        key = (tid, col, kind)
        if key not in seen:
            seen.add(key)
            print(f"  {kind.upper():<12} {tid}.{col} = {val!r}   (first seen in {label})")
    for err in errors:
        print(f"  ERROR  {err}")

    print(f"  {len(sites)} DataTable sites in app.py across "
          f"{len({f for f, _ in sites})} functions, {len(built & {ln for _, ln in sites})} "
          f"reached")
    print(f"  columns holding a number as text: {len(seen)}")
    print(f"  known-bad fixture detected: {sorted(fixture_kinds)}")

    ok = not seen and not errors and fixture_kinds == {"stringified", "placeholder"}
    print("AUDIT PASS" if ok else "AUDIT FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

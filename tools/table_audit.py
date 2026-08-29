"""Fail if any table in the dashboard holds a number as a string, or if a table went unaudited.

Formatting a number for display used to mean replacing it: fmt_tokens(997800) produced the string
"997.8k" and that string went into the table. Two things break. Native sort becomes lexicographic,
so 9 sorts after 80,000 and "1000k" sorts before "99.2k". And every table here inherits
export_format="csv" from TABLE_STYLE, so the CSV receives display text rather than values, which is
the whole point of offering an export.

This audit exists because the fix kept being applied to whichever columns someone happened to look
at. Three rounds: two columns, then eleven, then a thirteenth site in a callback that the second
round's own sweep could not reach and still called itself a sweep of every table.

So coverage is measured rather than asserted, in four ways that each caught something:

  every construction site is reached      a site the run never builds is a failure, not an omission
  every call that can reach one is taken  evidence_block builds one table from eight callers, so a
                                          reached LINE proves one caller ran, not all of them
  nothing is built that was not predicted  an alias or a wrapper defeats the static scan, and this
                                          is what says so
  everything built was also walked        a table built and never inspected is not audited, however
                                          green the coverage number looks

What it does NOT check. A reviewer produced EIGHT wrong passes against an earlier version of this
file, six of them with a real stringified table rendering. TWO of those eight are closed. Six are
not, and they share one root: a builder invoked by a name the static scan cannot resolve.

  a callee the static scan cannot resolve. Edges come from BARE NAMES, so `_EB = evidence_block`
  then `_EB(...)`, a dict of handlers, `getattr`, or a call from inside a class body is not an edge,
  and its callers are never demanded. A real table DOES hide here: build one in a callback this
  audit never invokes, and the construction line is still satisfied by the other callers that do
  run, while the table itself is never constructed during the audit and so never walked. That is
  the shape to expect a fourth escape from.

  two calls, or two constructions, that share one physical line. Coverage is line-granular, so a
  taken call covers an untaken one beside it.

  a branch inside a table-building function that this run's session, cohort and compaction never
  take. The inputs are picked to be awkward rather than typical, which is a choice, not a proof.

Closed of the eight: a table in a prop that is not `children` (it renders, and was read as nothing),
and a numeric shape outside the unit list the regex used to carry.

An unreached call site is reported as an error even when the path is genuinely defensive and this
store cannot trigger it. Give the audit an input that takes the branch. Do not delete the gate.

Run: python tools/table_audit.py              audits the app
     python tools/table_audit.py --self-test  shows every gate above firing on a case built to
                                              defeat it, because a gate never seen to fire is
                                              decoration
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
from dash import dash_table, dcc, html  # noqa: E402

# Shape, not vocabulary. This listed nine unit suffixes and a reviewer walked "1.4s" through it in
# under a minute; "ms", "MiB", "$", "e6" and a signed delta were all invisible for the same reason.
# A whitelist of units is a list of the shapes I happened to think of, which is the habit this whole
# file exists to break. Anything that reads as a number followed by a short tail counts, and columns
# holding a genuine string with digits, such as a version, are excluded by name below.
NUMERIC_LOOKING = re.compile(r"^[-+$]?[\d,]+(\.\d+)?(e[-+]?\d+)?\s*[A-Za-z%$/]{0,4}$", re.I)

# A placeholder is the other way a numeric column becomes text. `survivors` rendered "-" for a
# measured zero, so 44 rows claimed the count was unknown when it had been counted.
PLACEHOLDER = {"-", "--", "n/a", "N/A", "none", "unknown"}

# A version is "2.0.14" and a timestamp is "10:32:07". Both hold digits, neither is a quantity.
TEXT_BY_NATURE = {"version", "ts", "last active", "session_id", "uuid"}


def tables_in(node, out, seen=None):
    """Every DataTable anywhere in a component tree, by any PROP that can hold one.

    Not just `children`. Dash renders component-valued props such as `custom_spinner`, and a table
    parked in one was read as no table at all while its construction line still counted as reached,
    which is a wrong pass with a real table on screen.

    Props specifically, via `_prop_names`, not every attribute: walking `__dict__` blindly reaches
    Flask's request proxies, which raise "Working outside of application context" the moment they
    are touched.
    """
    seen = set() if seen is None else seen
    if id(node) in seen:
        return
    seen.add(id(node))

    if isinstance(node, dash_table.DataTable):
        out.append(node)
        return
    if isinstance(node, (list, tuple)):
        for child in node:
            tables_in(child, out, seen)
        return
    if isinstance(node, dict):
        for value in node.values():
            tables_in(value, out, seen)
        return

    props = getattr(node, "_prop_names", None)
    if props:
        for name in props:
            value = getattr(node, name, None)
            if isinstance(value, (list, tuple, dict)) or hasattr(value, "_prop_names"):
                tables_in(value, out, seen)
        return

    children = getattr(node, "children", None)
    if children is not None:
        tables_in(children, out, seen)


def findings(label, node, walked=None):
    out = []
    tables = []
    tables_in(node, tables)
    if walked is not None:
        # Identity, not a count. `sum(walked) == len(constructed)` was a total, so one component
        # object placed in the tree twice paid for one that was never walked at all.
        walked.update(id(t) for t in tables)
    for table in tables:
        tid = getattr(table, "id", None) or "(anonymous)"
        rows = getattr(table, "data", None) or []
        declared = {c["id"] for c in (getattr(table, "columns", None) or [])
                    if c.get("type") == "numeric"}
        # A column is numeric BY CONTENT if any row holds a real number in it. Declared type is not
        # enough: only numeric_columns() sets it, so five of this app's thirteen tables declare
        # nothing and a placeholder in one of them was invisible to a rule that asked for the type.
        by_content = {col for row in rows for col, val in row.items()
                      if isinstance(val, (int, float)) and not isinstance(val, bool)}
        numeric = declared | by_content
        for row in rows:
            for col, val in row.items():
                if col in TEXT_BY_NATURE or not isinstance(val, str):
                    continue
                text = val.strip()
                if text and NUMERIC_LOOKING.match(text):
                    out.append(("stringified", label, tid, col, val))
                elif col in numeric and (text in PLACEHOLDER or not text):
                    # An empty string is a placeholder too, and the commonest one: a NULL pushed
                    # into every column of a row lands as "" in the numeric ones as well.
                    out.append(("placeholder", label, tid, col, val))
    return out


@contextlib.contextmanager
def recording_construction(built, chain, constructed):
    """Record the app.py line of every DataTable actually constructed while this is open.

    Reachability by observation, not by name. The first attempt tracked which functions the audit
    called, which reported evidence_block() and breakdown_body() as unreached when both are helpers
    a tab layout calls: the audit had rendered their tables and still failed itself.

    `built` collects construction sites, `chain` every caller line above them, and `constructed`
    maps the id of every table made to the table itself, so one built and never walked can be told
    from one never built.
    """
    original = dash_table.DataTable.__init__

    def spy(self, *args, **kwargs):
        # Keyed by id, but the object is held: id() is unique only among LIVE objects, and a
        # table built and dropped freed its address for the next one, which made two constructions
        # look like one and put this gate to sleep on exactly the case it exists for.
        constructed[id(self)] = self
        # The construction site is the innermost app.py frame; every app.py frame above it is a
        # link in the chain that reached it, and those are what prove a CALLER was exercised
        # rather than merely a line.
        first = True
        for frame in reversed(traceback.extract_stack()):
            if os.path.basename(frame.filename) == "app.py":
                built.add(frame.lineno) if first else chain.add(frame.lineno)
                first = False
        return original(self, *args, **kwargs)

    dash_table.DataTable.__init__ = spy
    try:
        yield
    finally:
        dash_table.DataTable.__init__ = original


def table_sites():
    """Static picture of app.py: where tables are built, and every call that can reach one.

    Two returns. `sites` is each DataTable construction site with its enclosing top-level function.
    `calls` is every call site of a function that can build a table, directly or through anything it
    calls, which is what makes coverage a question about CALLERS rather than lines. evidence_block
    builds one table from eight callers, so reaching its line proves only that one of the eight ran.
    """
    tree = ast.parse(open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    sites, edges = [], {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            here = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.col_offset == 0 else fn)
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Attribute) and child.func.attr == "DataTable":
                    sites.append((here, child.lineno))
                # Bare names only. app.py calls its own functions unqualified, and matching
                # `.attr` too meant any method sharing a builder's name became a call site the
                # audit demanded be taken, which it never could be.
                if isinstance(child.func, ast.Name) and here:
                    edges.setdefault(here, []).append((child.func.id, child.lineno))
            walk(child, here)

    walk(tree, None)

    builders = {fn for fn, _ in sites if fn}
    growing = True
    while growing:
        growing = False
        for caller, called in edges.items():
            if caller not in builders and any(c in builders for c, _ in called):
                builders.add(caller)
                growing = True

    calls = [(caller, callee, lineno)
             for caller, called in edges.items()
             for callee, lineno in called if callee in builders]
    return sites, calls


def coverage_errors(built, chain, constructed, walked, sites, calls):
    """The four coverage gates. One definition, called by the audit and by its own self-test.

    They were written twice once, and a reviewer deleted a gate out of the audit while the
    self-test, which restated the same rule inline, stayed green over a run that then printed
    50 constructed, 47 walked and AUDIT PASS. A check exercising a copy of its subject is not
    checking the subject.
    """
    errors = []
    site_lines = {lineno for _, lineno in sites}

    for fn, lineno in sites:
        if lineno not in built:
            errors.append(f"app.py:{lineno} builds a DataTable inside {fn}(), "
                          f"which this audit never reached")

    # A shared builder reached through one caller says nothing about its other callers, and
    # evidence_block has eight.
    reached = built | chain
    for caller, callee, lineno in calls:
        if lineno not in reached:
            errors.append(f"app.py:{lineno} in {caller}() calls {callee}(), which can build a "
                          f"table, and this audit never takes that path")

    # A construction the static scan did not predict means the scan is blind to how it was written:
    # an alias or a wrapper rather than dash_table.DataTable spelled out.
    for lineno in sorted(built - site_lines):
        errors.append(f"app.py:{lineno} constructed a DataTable that the static scan did not find")

    # A table built but not walked is a table not audited, which is how a callback returning a bare
    # list slips through: the line is reached, the rows are never read. By identity, because a
    # count let one component walked twice pay for one never walked at all.
    for missed in sorted(set(constructed) - set(walked)):
        errors.append(f"a DataTable (id {missed}) was constructed but never walked, "
                      f"so its rows were never inspected")
    return errors


def main():
    hits, errors = [], []
    built, chain, constructed, walked = set(), set(), {}, set()

    session_id = m.q("""SELECT session_id FROM compactions GROUP BY 1
                        ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0]["session_id"]
    cohorts = [o["value"] for o in m.cohort_options()
               if str(o["value"]).startswith("section::")]
    cases = [("no selection", (None, "main", None)),
             ("one session", (session_id, "main", None)),
             ("a cohort", (None, "main", cohorts[0] if cohorts else None))]

    recorder = recording_construction(built, chain, constructed)
    recorder.__enter__()

    for tab_id, _label, layout in m.TABS:
        for case, args in cases:
            try:
                hits += findings(f"{tab_id} / {case}", layout(*args), walked)
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
                         m._compaction_clicked({"row": 0}, [{"uuid": uuid}]), walked)

    # The compare table is built by a callback too, and it is driven through that callback rather
    # than by calling compare_table directly: calling the builder proves the builder works and says
    # nothing about the path that delivers it. Both branches are taken, because a comparison against
    # a cohort and one against a single session assemble their arms differently, and the unequal-arm
    # rows only appear in the first.
    other = m.q("""SELECT session_id FROM api_calls WHERE session_id != ?
                   GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""",
                (session_id,)).iloc[0]["session_id"]
    arms = [("session", other, "callback / compare, session against session")]
    if cohorts:
        arms.insert(0, ("cohort", cohorts[0], "callback / compare, session against cohort"))
    else:
        errors.append("no cohort available, so the cohort arm of compare was never audited")
    for kind, target, label in arms:
        hits += findings(label, m._cmp_render(kind, target, session_id, None, "main"), walked)

    recorder.__exit__(None, None, None)

    # Reachability, observed rather than claimed.
    sites, calls = table_sites()
    site_lines = {lineno for _, lineno in sites}
    reached_calls = built | chain
    errors += coverage_errors(built, chain, constructed, walked, sites, calls)

    # The audit must be able to fail, or a clean run means nothing. Every shape it claims to catch
    # is fed to it: a formatted number, a dash placeholder in a declared numeric column, and an
    # empty string in a column that is numeric only by content, which is the shape that reached
    # production in a table declaring no types at all.
    fixture = findings("fixture", html.Div(dash_table.DataTable(
        columns=[{"name": "n", "id": "n", "type": "numeric"},
                 {"name": "u", "id": "u"}],
        data=[{"n": "1,234", "u": 7}, {"n": "-", "u": ""}])))
    fixture_kinds = {(kind, col) for kind, _label, _tid, col, _val in fixture}
    fixture_ok = fixture_kinds == {("stringified", "n"), ("placeholder", "n"),
                                   ("placeholder", "u")}

    seen = set()
    for kind, label, tid, col, val in hits:
        key = (tid, col, kind)
        if key not in seen:
            seen.add(key)
            print(f"  {kind.upper():<12} {tid}.{col} = {val!r}   (first seen in {label})")
    for err in errors:
        print(f"  ERROR  {err}")

    print(f"  {len(sites)} DataTable sites across {len({f for f, _ in sites})} functions, "
          f"{len(built & site_lines)} reached")
    print(f"  {len(calls)} calls that can reach a table, "
          f"{len([1 for _, _, ln in calls if ln in reached_calls])} taken")
    print(f"  {len(constructed)} tables constructed, "
          f"{len(set(constructed) & walked)} walked")
    print(f"  columns holding a number as text: {len(seen)}")
    print(f"  known-bad fixture fully detected: {fixture_ok}  {sorted(fixture_kinds)}")

    ok = not seen and not errors and fixture_ok
    print("AUDIT PASS" if ok else "AUDIT FAIL")
    return 0 if ok else 1


def self_test():
    """Every gate above, shown firing on a case built to defeat it.

    A gate that has never been seen to fire is decoration, and this file exists only because two
    earlier versions of it passed while a real defect stood.
    """
    results = []

    def check(name, fired, detail):
        results.append(fired)
        print(f"  {'FIRES ' if fired else 'SILENT'}  {name}: {detail}")

    # A construction the static scan cannot see, because it was not written as
    # dash_table.DataTable(...). Compiled under the filename app.py so the recorder attributes it
    # there, at a line no site occupies.
    built, chain, constructed = set(), set(), {}
    code = compile("\n" * 4999 + "ALIAS(columns=[], data=[])\n", "app.py", "exec")
    with recording_construction(built, chain, constructed):
        exec(code, {"ALIAS": dash_table.DataTable})
    sites, calls = table_sites()
    stray_errors = coverage_errors(built, chain, constructed, set(constructed), sites, [])
    
    check("stray construction line",
          any("static scan did not find" in e for e in stray_errors),
          f"recorded app.py:{sorted(built - {ln for _, ln in sites})}, "
          f"which the static scan does not list")

    # A table built and then dropped. The line is reached, the rows are never read. Note the
    # decoy: a second table walked TWICE, which the old count-based gate let pay for the missing
    # one, so this case also pins the move from counting to identity.
    built, chain, constructed = set(), set(), {}
    walked = set()
    with recording_construction(built, chain, constructed):
        dash_table.DataTable(columns=[{"name": "n", "id": "n"}], data=[{"n": "997.8k"}])
        decoy = dash_table.DataTable(columns=[], data=[])
        findings("walked twice", html.Div([decoy, decoy]), walked)
    dropped = coverage_errors(built, chain, constructed, walked, [], [])
    check("constructed but not walked",
          any("never walked" in e for e in dropped),
          f"{len(constructed)} constructed, {len(set(constructed) & walked)} walked, "
          f"and a count would have matched")

    # The caller gate, run through the same function the audit uses rather than restated here.
    # compare_table is delivered only through _cmp_render, so an audit that called the builder
    # directly would leave that call site untaken.
    cmp_calls = [ln for _c, callee, ln in calls if callee == "compare_table"]
    untaken = coverage_errors(set(), set(), {}, set(), [], calls)
    check("caller path is tracked",
          bool(cmp_calls) and len(untaken) == len(calls),
          f"{len(calls)} call sites can reach a table; compare_table is called at {cmp_calls}, "
          f"and none-taken yields {len(untaken)} errors")

    # An unreached construction site, through the same function.
    unreached = coverage_errors(set(), set(), {}, set(), sites, [])
    check("unreached site", len(unreached) == len(sites),
          f"{len(unreached)} of {len(sites)} sites reported when none is reached")

    # A bare list is a legal component tree and must be walked, not counted as nothing.
    listed = []
    tables_in([html.Div(dash_table.DataTable(columns=[], data=[]))], listed)
    check("walks a bare list", len(listed) == 1, f"{len(listed)} table found in a list")

    # The three shapes the detectors claim.
    shapes = findings("shapes", html.Div(dash_table.DataTable(
        columns=[{"name": "n", "id": "n", "type": "numeric"}, {"name": "u", "id": "u"}],
        data=[{"n": "1,234", "u": 7}, {"n": "-", "u": ""}])))
    kinds = {(k, c) for k, _l, _t, c, _v in shapes}
    check("detects all three shapes",
          kinds == {("stringified", "n"), ("placeholder", "n"), ("placeholder", "u")},
          f"{sorted(kinds)}")

    # A table in a prop that renders but is not children. This was a wrong AUDIT PASS with the
    # table on screen: the construction line counted as reached and the rows were never read.
    spun = []
    tables_in(dcc.Loading(children=[], custom_spinner=dash_table.DataTable(
        columns=[{"name": "n", "id": "n"}], data=[{"n": "997.8k"}])), spun)
    check("walks a prop that is not children", len(spun) == 1,
          f"{len(spun)} table found in custom_spinner")

    # Shapes outside the nine-unit whitelist this used to carry. A reviewer walked "1.4s" past it
    # in under a minute, so this is the check that catches the regex being narrowed back to a
    # vocabulary of units someone happened to think of.
    suffixed = findings("suffixes", html.Div(dash_table.DataTable(
        columns=[{"name": "d", "id": "d"}, {"name": "r", "id": "r"}, {"name": "c", "id": "c"},
                 {"name": "e", "id": "e"}, {"name": "g", "id": "g"}],
        data=[{"d": "1.4s", "r": "12 ms", "c": "$1,234", "e": "1.4e6", "g": "+18.3%"}])))
    check("catches units it was never told about",
          {c for _k, _l, _t, c, _v in suffixed} == {"d", "r", "c", "e", "g"},
          f"{sorted({c for _k, _l, _t, c, _v in suffixed})} of d r c e g")

    # And must stay quiet on columns that hold digits without holding quantities.
    clean = findings("clean", html.Div(dash_table.DataTable(
        columns=[{"name": "version", "id": "version"}, {"name": "ts", "id": "ts"}],
        data=[{"version": "2.0.14", "ts": "10:32:07"}])))
    check("no false positive on version and ts", not clean, f"{clean}")

    ok = all(results)
    print(f"SELF-TEST {'PASS' if ok else 'FAIL'} ({len(results)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())

"""Fail if any table in the dashboard holds a number as a string, or if a table went unaudited.

Formatting a number for display used to mean replacing it: fmt_tokens(997800) produced the string
"997.8k" and that string went into the table. Two things break. Native sort becomes lexicographic,
so 9 sorts after 80,000 and "1000k" sorts before "99.2k". And every table here inherits
export_format="csv" from TABLE_STYLE, so the CSV receives display text rather than values, which is
the whole point of offering an export.

This audit exists because the fix kept being applied to whichever columns someone happened to look
at. Two columns, then eleven, then a thirteenth site in a callback the second sweep could not reach
and still called itself a sweep of every table.

So coverage is measured rather than asserted, in five ways that each caught something:

  every ENTRY POINT is exercised          every callback DASH REGISTERS, not every function
                                          carrying a decorator spelled `callback`, plus the layout,
                                          which needs no callback at all
  every construction site is reached      a site the run never builds is a failure, not an omission
  every call that can reach one is taken  evidence_block builds one table from eight callers, so a
                                          reached position proves one caller ran, not all of them
  nothing is built that was not predicted an alias or a wrapper defeats the static scan, and this
                                          is what says so
  everything built was also walked        a table built and never inspected is not audited, however
                                          green the coverage number looks

Positions are (line, column), not lines. A reviewer put an untaken call beside a taken one on the
same line and the audit reported both as covered.

The entry-point gate is what closes the family of evasions the call graph cannot see. Edges are
matched by NAME, so a builder invoked through `_EB = evidence_block`, a dict of handlers, `getattr`
or a call inside a class body is not an edge, and a table built in an unexercised callback rendered
under AUDIT PASS. Resolving names better only moves that line, because the next spelling is always
available. Running every door does not.

Which doors, though, was wrong the first time this was written, and the correction is the point.
It said a table reaches a person only through a tab render or a callback return, and enumerated
callbacks by looking for the `@callback` decorator in the source. Both halves failed to a reviewer
in minutes: `app.layout` renders on first paint with no callback at all, and its tables are built at
IMPORT, before this audit is watching, so they are neither constructed nor walked; and a callback
registered as `_register = app.callback(...)` then `@_register` is dispatched by Dash and invisible
to a scan looking for a name. So the entry points come from Dash's own registry, which is what Dash
dispatches from, and the layout is walked separately. A DataTable found in the layout that this
audit never saw constructed is reported, because its rows can be read and nothing else about it can.

What it still does NOT check: a clientside callback, which is JavaScript and can set a table's data
with no Python involved; it is reported as unauditable rather than counted as covered. And a branch
inside a function that this run's session, cohort and
compaction never take. Inputs are picked to be awkward rather than typical, which is a choice, not
a proof. And `_tick` runs with `refresh_store` stubbed out, because the real one spawns a harvest
that writes to a 943 MB store: the rendering half runs, the harvest half does not.

An unreached call site is reported as an error even when the path is genuinely defensive and this
store cannot trigger it. Give the audit an input that takes the branch. Do not delete the gate.

Run: python tools/table_audit.py              audits the app
     python tools/table_audit.py --self-test  shows every gate above firing on a case built to
                                              defeat it, because a gate never seen to fire is
                                              decoration
"""
import ast
import contextlib
import dis
import functools
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash import dash_table, dcc, html  # noqa: E402
from dash._callback import GLOBAL_CALLBACK_LIST, GLOBAL_CALLBACK_MAP  # noqa: E402
from dash.exceptions import PreventUpdate  # noqa: E402

# THE AUDIT MUST NOT WRITE TO THE STORE IT AUDITS. The dashboard is a writer: `_tick` harvests
# the live ~/.claude/projects into whatever store C4X_DB names, and a stub on `app.refresh_store`
# never reached it, because `_tick` lives in c4x/ui/callbacks/selection.py and calls
# `header.refresh_store()` through the module object. An independent reviewer watched a 1.6 MB
# fixture become 819 MB of live rows during one audit run; the same thing had silently turned an
# earlier scratch fixture into a copy of the live store and sent a diagnosis down the wrong path.
# C4X_READ_ONLY is the switch refresh_store honours first, so it is set here, before app imports.
os.environ.setdefault("C4X_READ_ONLY", "1")

import app as m  # noqa: E402
from c4x.ui import header as _header  # noqa: E402


def _store_size():
    from c4x import store
    try:
        return os.path.getsize(store.DB_PATH)
    except OSError:
        return None


_STORE_SIZE_BEFORE = _store_size()


def store_changed(before, after):
    """The guard's whole judgement, as a function so the self-test can feed it a store that
    really grew and watch it say so. None on either side means the size was unknowable and is
    reported as unchanged: the audit cannot fail over a file it could not stat."""
    if before is None or after is None:
        return None
    if after == before:
        return None
    return (f"the audit CHANGED the store it audited: {before:,} bytes before, {after:,} after; "
            f"an audit that writes is not an audit")

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
# Short values, so only their names can exempt them.
TEXT_BY_NATURE = {"version", "ts", "last active", "session_id", "uuid"}

# Prose is exempted by MEASUREMENT rather than by name. A reviewer found the numeric rule firing on
# `tbl-messages.preview = '1'`, a message whose whole text is the character 1: 385 previews in the
# store match the pattern and the shipped run reported none only because of which session it picked.
# A column holding a value this long is prose, and a lone digit in it is prose too. A column that
# really did stringify its numbers has no long values at all, so this cannot hide the defect.
PROSE_MIN_LENGTH = 40

# _render_tab catches a failing pane and renders the exception rather than raising, which is right
# for a dashboard and would let this audit call a broken tab a success.
# One definition, in c4x/theme.py, shared with the two callbacks that PRODUCE it.
from c4x.theme import RENDER_FAILED  # noqa: E402


def app_files():
    """Every source file the app is made of, taken from the IMPORT GRAPH.

    Not a hardcoded list of names. This audit used to say "app.py" in seven places, so splitting
    that file into modules would have left the static scan looking at whatever remained, reporting
    fewer construction sites, and passing. Asking sys.modules which files the app actually imported
    cannot go stale when someone adds a module.

    tools/ is excluded: those are the CLI tools, and this audit is about the dashboard.
    """
    root = os.path.realpath(ROOT)
    tools = os.path.join(root, "tools")
    found = {}
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        real = os.path.realpath(path)
        if real.startswith(root) and not real.startswith(tools):
            found[real] = os.path.relpath(real, root).replace(os.sep, "/")
    return found


# Recomputed rather than snapshotted. A module imported inside a function is not in sys.modules
# when this file is first imported, and a set fixed at that moment cannot see it.
APP_FILES = app_files()


def refresh_app_files():
    """Re-read the import graph. Returns the names that were not there before."""
    global APP_FILES
    before = set(APP_FILES.values())
    APP_FILES = app_files()
    return sorted(set(APP_FILES.values()) - before)


def owning_file(frame):
    """The repo-relative name of the app file a frame belongs to, or None."""
    return APP_FILES.get(os.path.realpath(frame.f_code.co_filename))


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
        specs = getattr(table, "columns", None) or []
        # Column specs, before any cell is read. dash_table renders `name` as a React child, so a
        # non-string there is not a formatting nit: React throws error #31, unmounts the tree, and
        # every component in the app remounts at its default. That is what a helper wrapping
        # already-wrapped specs did here, and every existing check looked at `data` instead.
        for spec in specs:
            if not isinstance(spec, dict):
                out.append(("column-spec", label, tid, "(spec)", repr(spec)[:80]))
                continue
            for key in ("name", "id"):
                value = spec.get(key)
                if not isinstance(value, str):
                    out.append(("column-spec", label, tid, key, repr(value)[:80]))
        declared = {c["id"] for c in specs
                    if isinstance(c, dict) and isinstance(c.get("id"), str)
                    and c.get("type") == "numeric"}
        # A column is numeric BY CONTENT if any row holds a real number in it. Declared type is not
        # enough: only numeric_columns() sets it, so five of this app's thirteen tables declare
        # nothing and a placeholder in one of them was invisible to a rule that asked for the type.
        by_content = {col for row in rows for col, val in row.items()
                      if isinstance(val, (int, float)) and not isinstance(val, bool)}
        numeric = declared | by_content
        prose = {col for row in rows for col, val in row.items()
                 if isinstance(val, str) and len(val) > PROSE_MIN_LENGTH}
        for row in rows:
            for col, val in row.items():
                # NaN is the one representation of "missing" that breaks JSON and reads as a
                # value, and it reached here unseen: a float, so numeric by content, never a
                # string, so never a placeholder. pandas 3 produces it for any NULL text cell.
                if isinstance(val, float) and val != val:
                    out.append(("nan-cell", label, tid, col, "nan"))
                    continue
                if col in TEXT_BY_NATURE or col in prose or not isinstance(val, str):
                    continue
                text = val.strip()
                if text and NUMERIC_LOOKING.match(text):
                    out.append(("stringified", label, tid, col, val))
                elif col in numeric and (text in PLACEHOLDER or not text):
                    # An empty string is a placeholder too, and the commonest one: a NULL pushed
                    # into every column of a row lands as "" in the numeric ones as well.
                    out.append(("placeholder", label, tid, col, val))
    return out


def code_position(frame):
    """The (line, column) a frame is currently executing, matching what ast reports for that call.

    `traceback.extract_stack()` leaves `colno` as None here, so the position comes from the code
    object's own instruction table. Columns matter: an untaken call placed beside a taken one on
    the same line was reported as covered by a gate that compared line numbers.
    """
    try:
        instructions = dis.get_instructions(frame.f_code, show_caches=True)
    except TypeError:                                # show_caches went away again
        instructions = dis.get_instructions(frame.f_code)
    for instruction in instructions:
        if instruction.offset == frame.f_lasti:
            position = instruction.positions
            if position and position.lineno is not None:
                return (position.lineno, position.col_offset)
            break
    return (frame.f_lineno, None)


@contextlib.contextmanager
def recording_construction(built, chain, constructed, builders=(), module=None):
    """Record where every DataTable constructed while this is open was built, and by whom.

    Reachability by observation, not by name. The first attempt tracked which functions the audit
    called, which reported evidence_block() and breakdown_body() as unreached when both are helpers
    a tab layout calls: the audit had rendered their tables and still failed itself.

    `built` collects construction positions, `chain` every caller position above them, and
    `constructed` maps the id of every table made to the table itself, so one built and never walked
    can be told from one never built.

    `builders` are the functions that can reach a table. Each is wrapped so that ENTERING it records
    the position it was called from, which is what makes the caller gate answerable: a call that
    runs and builds nothing is still a call that ran.
    """
    original = dash_table.DataTable.__init__
    wrapped = {}

    def spy(self, *args, **kwargs):
        # Keyed by id, but the object is held: id() is unique only among LIVE objects, and a table
        # built and dropped freed its address for the next one, which made two constructions look
        # like one and put that gate to sleep on exactly the case it exists for.
        constructed[id(self)] = self
        frame, innermost = sys._getframe(1), True
        while frame is not None:
            name = owning_file(frame)
            if name:
                line, col = code_position(frame)
                (built if innermost else chain).add((name, line, col))
                innermost = False
            frame = frame.f_back
        return original(self, *args, **kwargs)

    def watch(func):
        @functools.wraps(func)
        def entered(*args, **kwargs):
            frame = sys._getframe(1)
            name = owning_file(frame)
            if name:
                line, col = code_position(frame)
                chain.add((name, line, col))
            return func(*args, **kwargs)
        return entered

    dash_table.DataTable.__init__ = spy
    for name in builders:
        target = getattr(module, name, None) if module is not None else None
        if callable(target):
            wrapped[name] = target
            setattr(module, name, watch(target))
    try:
        yield
    finally:
        dash_table.DataTable.__init__ = original
        for name, target in wrapped.items():
            setattr(module, name, target)


def table_sites(module=None):
    """Static picture of app.py: where tables are built, what can reach one, and every entry point.

    Four returns. `sites` is each DataTable construction position with its enclosing top-level
    function. `calls` is every call position of a function that can build a table, directly or
    through anything it calls, which makes coverage a question about CALLERS rather than positions,
    because evidence_block builds one table from eight of them. `entries` is every callback, which
    is the set that has to be exercised for any of the rest to mean anything.

    Given the live module, a module-level alias of a builder resolves to the same object and counts
    as the builder. That is a courtesy, not the defence: a local alias, a dict of handlers or a
    getattr still resolves to nothing here, which is why the entry-point gate exists. `opaque` is
    the set of calls whose callee cannot be named at all, which is the residue the entry-point gate
    cannot cover either, because a call on a branch nothing takes is invisible to every dynamic
    observation as well.
    """
    sites, edges, entries, opaque = [], {}, {}, []
    STALE_EXEMPTIONS.clear()
    for real, name in sorted(APP_FILES.items(), key=lambda kv: kv[1]):
        scan_one(real, name, sites, edges, entries, opaque, module)
    calls, opaque_calls, stale = finish_scan(sites, edges, opaque, module)
    # Kept on the module rather than threaded through four signatures. coverage_errors reads it, so
    # an exemption that stopped describing its call is reported by the same gate that would have
    # caught the call in the first place.
    STALE_EXEMPTIONS.extend(stale)
    return sites, calls, entries, opaque_calls


# The ONE bare name this scan accepts as a table construction, and the reason it is not simply
# "any callable named DataTable".
#
# dash ships a `DataTable` submodule and rebinds that name to the component class at import, so
# mypy reads `dash_table.DataTable` as a module and reports every call. c4x/dash_compat.py holds
# the one suppression for that, and the eleven modules that build tables import the name from it,
# which makes every construction a bare `DataTable(...)` rather than an attribute access.
#
# Accepting exactly this spelling keeps the anti-alias gate intact. `ALIAS(...)`, `DT(...)`, a dict
# of handlers or a getattr still resolve to nothing here and are still caught by the
# "constructed a DataTable that the static scan did not find" error, which is the check that stops
# a table from being hidden behind a name this scan cannot follow.
ACCESSOR = "DataTable"


def is_table_construction(func):
    """Whether an ast callee names the DataTable constructor, in either spelling used here."""
    if isinstance(func, ast.Attribute):
        return func.attr == ACCESSOR                # dash_table.DataTable(...)
    return isinstance(func, ast.Name) and func.id == ACCESSOR   # the c4x.dash_compat accessor


def scan_one(real, name, sites, edges, entries, opaque, module):
    """Scan ONE app file, appending to the shared collections."""
    tree = ast.parse(open(real, encoding="utf-8").read())

    # Line numbers for the message come from the source; the SET of entry points comes from Dash's
    # registry, because a decorator can be renamed and the source scan would not know it.
    lines = {node.name: node.lineno for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for callback_name in registered_callbacks()[0]:
        if callback_name in lines:
            entries[callback_name] = f"{name}:{lines[callback_name]}"

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            here = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.col_offset == 0 else fn)
            if isinstance(child, ast.Call):
                position = (name, child.lineno, child.col_offset)
                if is_table_construction(child.func):
                    sites.append((here, position))
                # A call whose callee this scan cannot name at all: a dict of handlers, the result
                # of another call, `getattr`. Those are the two evasions that survived everything
                # else, and they survived on an unexecuted branch, where nothing dynamic can see
                # them either. app.py contains none, so the gate is silent until someone writes one.
                if (isinstance(child.func, (ast.Subscript, ast.Call))
                        or (isinstance(child.func, ast.Name) and child.func.id == "getattr")):
                    opaque.append((here, position))
                # Bare names only. app.py calls its own functions unqualified, and matching `.attr`
                # too meant any method sharing a builder's name became a call site the audit
                # demanded be taken, which it never could be.
                if isinstance(child.func, ast.Name) and here:
                    # Keyword LITERALS at the call site, so an exemption can be checked against
                    # what the call actually passes rather than trusted.
                    literals = {kw.arg: kw.value.value for kw in child.keywords
                                if kw.arg and isinstance(kw.value, ast.Constant)}
                    edges.setdefault(here, []).append((child.func.id, position, literals))
            walk(child, here)

    walk(tree, None)


STALE_EXEMPTIONS = []

# Calls that name a table-building function but cannot reach a table, with the reason.
#
# Keyed by (caller, callee, keyword, value): the exemption applies only while the call still passes
# that keyword. Checked, not assumed - exempt_is_still_true() re-reads the source and reports an
# exemption that no longer describes the code, so a stale one fails rather than hides a real gap.
EXEMPT_CALLS = {
    ("_session_controls", "session_view"): (
        "with_cards", False,
        "session_view returns at `if not with_cards` before building anything, so this call can "
        "never reach a table and no exercise could make it"),
}


def exempt_is_still_true(calls_with_keywords):
    """Every exemption must still describe the call it exempts.

    Returns the errors, so a keyword that was removed or flipped is reported rather than silently
    granting coverage to a call that now really can build a table.
    """
    errors = []
    seen = {(caller, callee) for caller, callee, _position, _kw in calls_with_keywords}
    for (caller, callee), (keyword, value, reason) in EXEMPT_CALLS.items():
        if not reason.strip():
            errors.append(f"the exemption for {caller}->{callee} carries no reason")
        if (caller, callee) not in seen:
            errors.append(f"{caller}() no longer calls {callee}(), so its exemption is stale "
                          f"and should be deleted")
            continue
        actual = next(kw for c, k, _p, kw in calls_with_keywords
                      if (c, k) == (caller, callee))
        if actual.get(keyword) != value:
            errors.append(f"{caller}() calls {callee}() with {keyword}={actual.get(keyword)!r}, "
                          f"not {value!r}, so the exemption no longer holds and this call must be "
                          f"covered like any other")
    return errors


def finish_scan(sites, edges, opaque, module):
    """Close over the call graph ACROSS files, then list the calls that can reach a table.

    Run once over everything rather than per file: a helper defined in one module and called from
    another is exactly what splitting app.py creates, and a per-file closure would miss it.
    """
    builders = {fn for fn, _ in sites if fn}
    growing = True
    while growing:
        growing = False
        for caller, called in edges.items():
            if caller not in builders and any(c in builders for c, _, _ in called):
                builders.add(caller)
                growing = True

    if module is not None:
        objects = {id(getattr(module, attr, None)) for attr in builders}
        objects.discard(id(None))
        for attr in dir(module):
            if id(getattr(module, attr, None)) in objects:
                builders.add(attr)

    every = [(caller, callee, position, literals)
             for caller, called in edges.items()
             for callee, position, literals in called if callee in builders]
    stale = exempt_is_still_true(every)
    calls = [(caller, callee, position) for caller, callee, position, _lit in every
             if (caller, callee) not in EXEMPT_CALLS]
    return calls, opaque, stale


def coverage_errors(built, chain, constructed, walked, sites, calls,
                    entries=None, exercised=None, opaque=None, reachability=True):
    """The five coverage gates. One definition, called by the audit and by its own self-test.

    `reachability=False` drops the TWO gates that ask whether a table-building path was taken, and
    nothing else. It exists for a store on which some paths are legitimately not taken - a first
    run, with no probe recorded, where the Window sub-panels correctly return a written "no probe
    reading yet" panel and never build their tables.

    IT IS DELIBERATELY NOT A SWITCH ON THIS WHOLE FUNCTION. It was written that way first, as one
    `if` around the call, and an independent reviewer broke two of the other four gates and watched
    the run pass: a stale EXEMPT_CALLS entry went unreported, and a DataTable constructed and
    discarded printed "105 constructed, 102 walked" and PASS on the same screen. Three tables' rows
    had never been inspected while the flag's own banner said cell values were checked. Suppressing
    a gate you did not mean to suppress is the failure this file exists to catch, so the narrowing
    happens per gate, here, where each gate is written.

    They were written twice once, and a reviewer deleted a gate out of the audit while the
    self-test, which restated the same rule inline, stayed green over a run that then printed
    50 constructed, 47 walked and AUDIT PASS. A check exercising a copy of its subject is not
    checking the subject.
    """
    errors = []
    site_positions = {position for _, position in sites}

    def at(position):
        """file:line:col, so a message points somewhere once the app is more than one file."""
        where, lineno, col = position
        return f"{where}:{lineno}:{col}"

    for fn, position in (opaque or []):
        errors.append(f"{at(position)} in {fn}() calls something this scan cannot name, so "
                      f"whether it builds a table is unknowable from the source. Two evasions "
                      f"survived here. If the call is legitimate, teach this audit to observe that "
                      f"line executing. Do not delete the gate.")

    for name, where in sorted((entries or {}).items(), key=lambda kv: str(kv[1])):
        if name not in (exercised or set()):
            errors.append(f"{where} defines the callback {name}(), which this audit never "
                          f"runs, so anything it builds is invisible to every other gate here")

    for fn, position in sites:
        if position in built:
            continue
        # GATE 1 OF 2 that `reachability=False` drops. Everything else in this function keeps
        # running, because nothing else here depends on what the store happens to contain.
        if not reachability:
            continue
        errors.append(f"{at(position)} builds a DataTable inside {fn}(), "
                      f"which this audit never reached")

    # A shared builder reached through one caller says nothing about its other callers, and
    # evidence_block has eight.
    reached = set(built) | set(chain)
    # An exemption that no longer describes its call is a failure here, not a note: it would
    # otherwise grant coverage to a call that can now really build a table.
    errors.extend(STALE_EXEMPTIONS)

    for caller, callee, position in calls:
        if position not in reached:
            if reachability:
                errors.append(f"{at(position)} in {caller}() calls {callee}(), which can build "
                              f"a table, and this audit never takes that path")

    # A construction the static scan did not predict means the scan is blind to how it was written:
    # an alias or a wrapper rather than dash_table.DataTable spelled out.
    for position in sorted(set(built) - site_positions):
        errors.append(f"{at(position)} constructed a DataTable that the static scan "
                      f"did not find")

    # A table built but not walked is a table not audited, which is how a callback returning a bare
    # list slips through: the position is reached, the rows are never read. By identity, because a
    # count let one component walked twice pay for one never walked at all.
    for missed in sorted(set(constructed) - set(walked)):
        errors.append(f"a DataTable (id {missed}) was constructed but never walked, "
                      f"so its rows were never inspected")
    return errors


def registered_callbacks():
    """Every callback Dash will actually dispatch, from Dash's own registry.

    Not from the `@callback` decorator name in the source. A reviewer registered one as
    `_register = app.callback(...)` then `@_register`, which the AST scan did not recognise as an
    entry point, and the audit passed while that callback returned a stringified table over
    /_dash-update-component. The registry is what Dash dispatches from, so it is the only complete
    answer to "what entry points exist".

    Returns the Python callbacks by name, and separately the clientside ones, which have no Python
    function to run and so cannot be audited here at all.
    """
    python, clientside = {}, []
    for entry in GLOBAL_CALLBACK_LIST:
        if entry.get("clientside_function") is not None:
            clientside.append(str(entry.get("output")))
    for key, entry in GLOBAL_CALLBACK_MAP.items():
        func = entry.get("callback")
        name = getattr(getattr(func, "__wrapped__", func), "__name__", None)
        if name:
            python[name] = key
    return python, clientside


def layout_of(module):
    """The app's own layout, whatever form it is stored in.

    A reviewer put a table straight into `app.layout` through an alias and got AUDIT PASS: it is
    built at IMPORT, before the recorder opens, and nothing ever walked the layout. Two premises
    were wrong at once, that a table reaches a person only through a tab render or a callback
    return, and that everything a person can see is constructed while this audit is watching.
    """
    layout = getattr(getattr(module, "app", None), "layout", None)
    return layout() if callable(layout) else layout


def render_failed(node):
    """True if _render_tab swallowed an exception into a panel instead of raising it."""
    found = []

    def walk(item, depth=0):
        if found or depth > 40:
            return
        if isinstance(item, str):
            if RENDER_FAILED in item:
                found.append(item)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)
            return
        children = getattr(item, "children", None)
        if children is not None:
            walk(children, depth + 1)

    walk(node)
    return bool(found)


def exercise(name, errors, exercised, call, hits=None, walked=None):
    """Run one entry point, record that it really ran, and INSPECT whatever it returned.

    Inspecting the return is not optional. _message_clicked was being run and its output thrown
    away, so a table it built was caught only by the built-but-not-walked gate, one step removed
    from the rows nobody had read. Every door is opened and everything behind it is looked at.

    PreventUpdate counts as exercised: refusing to update is a legitimate outcome and the function
    was entered. Anything else raising is an error, because an entry point this audit cannot run is
    an entry point whose tables are invisible to every other gate.
    """
    try:
        result = call()
    except PreventUpdate:
        exercised.add(name)
        return None
    except Exception as exc:                        # noqa: BLE001 - the point is to report it
        errors.append(f"{name}() raised {type(exc).__name__}: {exc}")
        return None
    exercised.add(name)
    if hits is not None:
        hits += findings(f"callback / {name}", result, walked)
    return result


# Set from argv at dispatch, read by main(). A module-level flag rather than a parameter because
# main() takes none and the coverage check is buried three hundred lines below its own call.
RENDER_ONLY = False


class CannotAudit(Exception):
    """The store does not hold what this audit needs to say anything.

    NOT the same as a failure, and not the same as a pass. On a store with no compactions the audit
    used to raise IndexError out of `.iloc[0]` and the runner reported a traceback, which reads as
    a broken audit rather than an unauditable store - the exact confusion this file exists to
    prevent everywhere else. But it must not print AUDIT PASS either: `tools/run_tests.mjs:14`
    states the rule this repo runs on, that a check which could not run is a FAILURE and never a
    warning. So it declines, out loud, with the reason and a distinct exit code, and the runner
    turns that into a SKIPPED row that still fails under --strict.
    """


def one(frame, column, why):
    """The first value of a column, or a decline naming what the store would need."""
    if frame.empty:
        raise CannotAudit(why)
    return frame.iloc[0][column]


def main():
    hits, errors, exercised = [], [], set()
    built, chain, constructed, walked = set(), set(), {}, set()

    session_id = one(m.q("""SELECT session_id FROM compactions GROUP BY 1
                        ORDER BY COUNT(*) DESC LIMIT 1"""), "session_id",
                     "this store records no compaction, and every selection case below is built "
                     "around a session that has one. Build a fixture: "
                     "node tools/make_fixture.mjs --out tmp/fixture.db")
    other = one(m.q("""SELECT session_id FROM api_calls WHERE session_id != ?
                   GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""",
                    (session_id,)), "session_id",
                "this store holds only one session, so the second-session cases cannot be built")
    cohorts = [o["value"] for o in m.cohort_options()
               if str(o["value"]).startswith("section::")]
    cohort = cohorts[0] if cohorts else None
    if cohort is None:
        errors.append("no section cohort exists, so every cohort case below is untested")

    uuid = None
    for _, row in m.q("SELECT uuid FROM compactions ORDER BY rowid DESC LIMIT 40").iterrows():
        if not m.compaction_dropped(row["uuid"]).empty:
            uuid = row["uuid"]
            break
    if uuid is None:
        errors.append("no compaction with dropped rows, so the detail table was never audited")
    message_uuid = one(m.q("SELECT uuid FROM messages ORDER BY rowid DESC LIMIT 1"), "uuid",
                       "this store holds no message, so the message drawer cannot be opened")

    # An early scan, only to learn WHICH functions to wrap before exercising begins. The
    # authoritative scan happens afterwards, once every module the app touches has been imported.
    _sites, early_calls, _entries, _opaque = table_sites(m)
    builders = {callee for _caller, callee, _position in early_calls}

    # _tick spawns a harvest that WRITES to the store. The rendering half is what can build a table,
    # so it runs and the harvest does not. Stated here and in the output rather than left implied.
    # On `header`, not on `app`: app.py re-exports the name (F401) and nothing calls it there.
    real_refresh, _header.refresh_store = _header.refresh_store, lambda *a, **k: None

    from dash._callback_context import context_value
    from dash._utils import AttributeDict

    recorder = recording_construction(built, chain, constructed, builders, m)
    recorder.__enter__()
    try:
        cases = [("no selection", None, None),
                 ("one session", session_id, None),
                 ("a cohort", None, cohort)]
        for index, (tab_id, _label, _fn) in enumerate(m.TABS):
            for case, sid, coh in cases:
                pane = exercise("_render_tab", errors, exercised,
                                lambda i=index, s=sid, c=coh: m._render_tab(i, s, "main", c))
                if pane is None:
                    continue
                if render_failed(pane):
                    errors.append(f"{tab_id} / {case} rendered an exception panel, which "
                                  f"_render_tab produces instead of raising")
                hits += findings(f"{tab_id} / {case}", pane, walked)

        context_value.set(AttributeDict(
            triggered_inputs=[{"prop_id": f"btn-{m.TABS[-1][0]}.n_clicks"}]))
        exercise("_switch_tab", errors, exercised, lambda: m._switch_tab(*([1] * len(m.TABS)), 0),
                 hits, walked)
        context_value.set(AttributeDict(triggered_inputs=[]))

        # The style half of the nav, split out so a callback other than a button click can move
        # the reader between tabs. It builds no table, but an unexercised callback is a hole in
        # every other gate below, so it is driven at both ends of the range.
        for index in (0, len(m.TABS) - 1):
            exercise("_tab_styles", errors, exercised, lambda i=index: m._tab_styles(i),
                     hits, walked)

        # A finding is a door: clicking one sets the header selection and switches the tab. Driven
        # with a row that has a destination, one that has none, and no row at all, because the
        # PreventUpdate branches are the ones that decide whether a click quietly does nothing.
        _rows = [{"goes to": m.TAB_IDS[-1], "session_id": session_id},
                 {"goes to": None, "session_id": None}]
        for _cell in ({"row": 0}, {"row": 1}, {"row": 99}, None):
            exercise("_finding_clicked", errors, exercised,
                     lambda c=_cell: m._finding_clicked(c, _rows), hits, walked)

        exercise("_selector_options", errors, exercised, lambda: m._selector_options(cohort, 0),
                 hits, walked)
        exercise("_cohort_options", errors, exercised, lambda: m._cohort_options(0, None),
                 hits, walked)
        exercise("_cohort_options", errors, exercised, lambda: m._cohort_options(0, [{"a": 1}]),
                 hits, walked)
        # Both cross-filters BUILD A TABLE's rows, so every gate below depends on them running:
        # a stringified number introduced by a filter would be invisible to an audit that only
        # ever saw the unfiltered render. Driven through their real branches, including the two
        # that refuse: an empty box, and a click on the reference diagonal.
        _rows = [{"session_id": session_id, "turns": 1, "peak": 2, "compactions": 0}]
        for _sel in (None, {"points": []},
                     {"points": [{"customdata": ["t", "p", 0, session_id]}]}):
            exercise("_sessions_crossfilter", errors, exercised,
                     lambda s=_sel: m._sessions_crossfilter(s, _rows), hits, walked)
        _held = {"rows": [{"reads": 3, "bytes": 1.0}], "population": 9}
        for _click in (None, {"points": [{"curveNumber": 1, "x": 5, "y": 50}]},
                       {"points": [{"curveNumber": 0, "x": 1, "y": 40.0}]},
                       {"points": [{"curveNumber": 0, "x": 99, "y": 99.0}]},
                       {"points": [{"curveNumber": 0, "x": None}]}):
            exercise("_reread_crossfilter", errors, exercised,
                     lambda c=_click: m._reread_crossfilter(c, _held), hits, walked)

        exercise("_pick_from_table", errors, exercised,
                 lambda: m._pick_from_table([0], [{"session_id": session_id}]), hits, walked)
        exercise("_pick_from_table", errors, exercised, lambda: m._pick_from_table([], []),
                 hits, walked)
        exercise("_tick", errors, exercised, lambda: m._tick(0, session_id, "main"),
                 hits, walked)
        exercise("_mirror", errors, exercised, lambda: m._mirror(850000, 1000000),
                 hits, walked)
        # The whole session as the compared range, so the diff panel builds all three of its
        # tables. A degenerate range returns a prompt instead, and would leave them unbuilt.
        # Sub-panels, driven by the registry rather than by a hardcoded list. A tab body renders
        # only its FIRST panel, so walking TABS alone saw a third of the Window tab and reported
        # 40 tables where there were 74. Every panel of every panelled tab, under the same
        # selections the tabs get.
        from c4x.ui import subpanels
        for prefix, entry in sorted(subpanels.PANELLED.items()):
            for index, (key, _label, _description) in enumerate(entry["panels"]):
                for case, sid, coh in cases:
                    pane = exercise(f"{prefix} panel {key}", errors, exercised,
                                    lambda i=index, s=sid, c=coh, e=entry:
                                    e["body"](i, s, "main", c))
                    if pane is None:
                        continue
                    if render_failed(pane):
                        errors.append(f"{prefix}/{key} / {case} rendered an exception panel")
                    hits += findings(f"{prefix}/{key} / {case}", pane, walked)
            # The callbacks that switch and render those panels, so their own construction sites
            # are taken rather than merely their builders.
            exercise("_window_panel", errors, exercised,
                     lambda: m._window_panel(1, session_id, "main", None), hits, walked)
            context_value.set(AttributeDict(
                triggered_inputs=[{"prop_id": f"{subpanels.button_id(prefix, 'items')}.n_clicks"}]))
            exercise("_window_panel_chosen", errors, exercised,
                     lambda e=entry: m._window_panel_chosen(*([1] * len(e["panels"])), 0),
                     hits, walked)
            context_value.set(AttributeDict(triggered_inputs=[]))

        exercise("_session_controls", errors, exercised,
                 lambda: m._session_controls(80, [1, 10 ** 6], session_id, "main"), hits, walked)
        exercise("_message_clicked", errors, exercised,
                 lambda: m._message_clicked({"row": 0}, [{"uuid": message_uuid}]), hits, walked)

        if uuid is not None:
            exercise("_compaction_clicked", errors, exercised,
                     lambda: m._compaction_clicked({"row": 0}, [{"uuid": uuid}]), hits, walked)

        # Both arms of compare, driven through the callback rather than by calling the builder:
        # calling the builder proves the builder works and says nothing about the path that
        # delivers it. A session against a cohort and a session against a session assemble their
        # arms differently, and the unequal-arm rows only appear in the first.
        for kind, target in [("cohort", cohort), ("session", other)]:
            if target is None:
                continue
            exercise("_cmp_targets", errors, exercised, lambda k=kind: m._cmp_targets(k, cohort),
                     hits, walked)
            exercise("_cmp_render", errors, exercised,
                     lambda k=kind, t=target: m._cmp_render(k, t, session_id, None, "main"),
                     hits, walked)
    finally:
        recorder.__exit__(None, None, None)
        _header.refresh_store = real_refresh

    # A clientside callback is JavaScript. It can set a table's data with no Python involved, and
    # nothing in this process can see it, so it is reported rather than silently uncovered.
    for output in registered_callbacks()[1]:
        errors.append(f"a clientside callback writes {output}, which is JavaScript and cannot be "
                      f"audited from Python: whatever it puts in a table is unchecked here")

    # The layout renders on first paint without any callback, and it was built at import, so its
    # tables are not in `constructed` and cannot be. Walking it is the only way to read those rows.
    layout = layout_of(m)
    layout_tables = []
    tables_in(layout, layout_tables)
    hits += findings("app.layout", layout, walked)
    for table in layout_tables:
        if id(table) not in constructed:
            tid = getattr(table, "id", None) or "(anonymous)"
            errors.append(f"app.layout holds a DataTable ({tid}) built before this audit was "
                          f"watching, at import, so no coverage gate here can see how it was made. "
                          f"Its rows were read; nothing else about it was.")

    # Now that everything has run, re-read the import graph and scan for real. A module imported
    # inside a function only exists by this point.
    late = refresh_app_files()
    for name in late:
        errors.append(f"{name} became part of the app only while it ran, so the first scan could "
                      f"not see it. Import it at module level, or this audit is reading a "
                      f"different set of files than the one that executed.")
    sites, calls, entries, opaque = table_sites(m)
    # --render-only DROPS THE REACHABILITY HALF, and only that half.
    #
    # This audit answers two questions at once: did any surface render an exception panel, and was
    # every table-construction site reached. The first is answerable on ANY store. The second is
    # answerable only on a store rich enough to take every branch, and on a FIRST-RUN store it is
    # not merely unanswerable, it is guaranteed to report: with no probe recorded, the Window
    # sub-panels correctly return "No probe reading yet" and never build their tables, so every
    # site inside them is honestly unreached.
    #
    # That difference is why a first-run store had never been audited at all. Running the whole
    # audit there fails for a reason that is not a defect, so nobody ran it, so the reason that IS
    # a defect - three surfaces raising - went unseen for as long as they existed. Splitting the
    # questions is what makes the first one askable.
    #
    # The full audit is unchanged and still the default. This flag only ever REMOVES a check, and
    # says so on the line above the verdict, so a --render-only run can never be mistaken for one.
    errors += coverage_errors(built, chain, constructed, walked, sites, calls, entries,
                              exercised, opaque, reachability=not RENDER_ONLY)

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

    site_positions = {position for _, position in sites}
    print(f"  {len(entries)} callbacks registered with Dash, {len(set(entries) & exercised)} "
          f"exercised (_tick with refresh_store stubbed, since the real one writes)")
    print(f"  {len(sites)} DataTable sites across {len({f for f, _ in sites})} functions, "
          f"{len(built & site_positions)} reached")
    print(f"  {len(calls)} calls that can reach a table, "
          f"{len([1 for _, _, p in calls if p in (built | chain)])} taken")
    print(f"  {len(opaque)} calls whose callee cannot be named from the source")
    print(f"  {len(constructed)} tables constructed, {len(set(constructed) & walked)} walked")
    print(f"  columns holding a number as text: {len(seen)}")
    print(f"  known-bad fixture fully detected: {fixture_ok}  {sorted(fixture_kinds)}")

    if RENDER_ONLY:
        print("  --render-only: the two REACHABILITY gates are off, because a first-run store "
              "cannot take a path that needs a probe. Every other gate is on, including "
              "constructed-but-never-walked, so a table whose rows went uninspected still fails.")
    ok = not seen and not errors and fixture_ok
    # The store must be byte-for-byte the size it was. A grown store means something harvested
    # into it, which is the defect above; a shrunk one means something worse. Measured on the
    # file rather than trusted from the switch, because the switch is what failed to be enough.
    grew = store_changed(_STORE_SIZE_BEFORE, _store_size())
    if grew:
        errors.append(grew)
        ok = False
        print(f"  ERROR  {grew}")
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

    sites, calls, entries, _opaque = table_sites(m)

    # A call whose callee the scan cannot name. This is what closes the last two evasions, which
    # both hid an unresolvable callee on a branch no input takes, where nothing dynamic sees them.
    unnameable = coverage_errors(set(), set(), {}, set(), [], [], {}, set(),
                                 [("f", ("app.py", 10, 4))])
    check("a callee that cannot be named",
          len(unnameable) == 1 and "cannot name" in unnameable[0],
          f"{len(unnameable)} reported for one opaque call")

    # An entry point that was never run. This is the gate that closes the whole family of evasions
    # the call graph cannot see, because a table built in an unexercised callback rendered under
    # AUDIT PASS no matter how the builder was named.
    missing = coverage_errors(set(), set(), {}, set(), [], [], entries, set())
    check("unexercised entry point", len(missing) == len(entries),
          f"{len(missing)} of {len(entries)} callbacks reported when none is run")

    # A construction the static scan cannot see, because it was not written as
    # dash_table.DataTable(...). Compiled under the filename app.py so the recorder attributes it
    # there, at a position no site occupies.
    built, chain, constructed = set(), set(), {}
    app_file = next(real for real, name in APP_FILES.items() if name == "app.py")
    code = compile("\n" * 4999 + "ALIAS(columns=[], data=[])\n", app_file, "exec")
    with recording_construction(built, chain, constructed):
        exec(code, {"ALIAS": dash_table.DataTable})
    stray = coverage_errors(built, chain, constructed, set(constructed), sites, [], {}, set())
    check("stray construction position",
          any("static scan did not find" in e for e in stray),
          f"recorded app.py:{sorted(built - {p for _, p in sites})}, "
          f"which the static scan does not list")

    # The predicate that gate rests on, fed all three spellings at once. Both forms the codebase
    # actually uses have to be seen, and a third name has to stay unseen: if ALIAS ever reads as a
    # construction, the check above can no longer fail and stops being a gate.
    spellings = {src: is_table_construction(ast.parse(src, mode="eval").body.func)
                 for src in ("dash_table.DataTable(columns=[], data=[])",
                             "DataTable(columns=[], data=[])",
                             "ALIAS(columns=[], data=[])")}
    check("both spellings seen, a third one not",
          list(spellings.values()) == [True, True, False],
          ", ".join(f"{src.split('(')[0]}={seen}" for src, seen in spellings.items()))

    # Two calls on ONE line, one taken and one not. The gate compared line numbers and reported
    # both as covered.
    same_line = coverage_errors({("app.py", 10, 4)}, set(), {}, set(), [],
                                [("f", "g", ("app.py", 10, 4)),
                                 ("f", "h", ("app.py", 10, 20))], {}, set())
    same_line = [e for e in same_line if "never takes that path" in e]
    check("untaken call beside a taken one",
          len(same_line) == 1 and "10:20" in same_line[0],
          f"{len(same_line)} of 2 calls on line 10 reported as untaken")

    # A table built and then dropped. The position is reached, the rows are never read. Note the
    # decoy: a second table walked TWICE, which the old count-based gate let pay for the missing
    # one, so this case also pins the move from counting to identity.
    built, chain, constructed = set(), set(), {}
    walked = set()
    with recording_construction(built, chain, constructed):
        dash_table.DataTable(columns=[{"name": "n", "id": "n"}], data=[{"n": "997.8k"}])
        decoy = dash_table.DataTable(columns=[], data=[])
        findings("walked twice", html.Div([decoy, decoy]), walked)
    dropped = coverage_errors(built, chain, constructed, walked, [], [], {}, set())
    check("constructed but not walked",
          any("never walked" in e for e in dropped),
          f"{len(constructed)} constructed, {len(set(constructed) & walked)} walked, "
          f"and a count would have matched")

    # The caller gate and the site gate, both through the same function the audit uses rather than
    # restated here.
    cmp_calls = [p for _c, callee, p in calls if callee == "compare_table"]
    untaken = coverage_errors(set(), set(), {}, set(), [], calls, {}, set())
    check("caller path is tracked",
          bool(cmp_calls) and len(untaken) == len(calls),
          f"{len(calls)} call sites can reach a table; compare_table is called at {cmp_calls}, "
          f"and none-taken yields {len(untaken)} errors")

    unreached = coverage_errors(set(), set(), {}, set(), sites, [], {}, set())
    check("unreached site", len(unreached) == len(sites),
          f"{len(unreached)} of {len(sites)} sites reported when none is reached")

    # --render-only DROPS THE REACHABILITY GATES AND NOTHING ELSE, asserted here rather than
    # trusted from the flag's own banner. It was first written as one `if` around this whole
    # function, and an independent reviewer broke two unrelated gates and watched the audit pass:
    # a discarded DataTable printed "105 constructed, 102 walked" and AUDIT PASS on one screen.
    # A flag that can silently widen what it suppresses is the defect this file exists to catch.
    off = coverage_errors(set(), set(), {}, set(), sites, [], {}, set(), reachability=False)
    check("--render-only silences the unreached-site gate", not off, f"{len(off)} still reported")
    stray = {1: "a", 2: "b"}
    for flag in (True, False):
        kept = coverage_errors(set(), set(), stray, {1}, [], [], {}, set(), reachability=flag)
        check(f"constructed-but-never-walked survives reachability={flag}", len(kept) == 1,
              f"{len(kept)} reported")
        blind = coverage_errors(set(), set(), {}, set(), [], [], {}, set(),
                                opaque=[("fn", ("app.py", 1, 0))], reachability=flag)
        check(f"the opaque-call gate survives reachability={flag}", len(blind) == 1,
              f"{len(blind)} reported")

    # A pane that raised. _render_tab renders the exception rather than raising it, so an audit
    # driving tabs through it would otherwise call a broken tab a success.
    check("a rendered exception panel is not a success",
          render_failed(html.Div([html.Div("Waste could not be rendered"), html.Pre("KeyError")])),
          "the panel _render_tab builds for a failing tab is detected")

    # A table in a prop that renders but is not children.
    spun = []
    tables_in(dcc.Loading(children=[], custom_spinner=dash_table.DataTable(
        columns=[{"name": "n", "id": "n"}], data=[{"n": "997.8k"}])), spun)
    check("walks a prop that is not children", len(spun) == 1,
          f"{len(spun)} table found in custom_spinner")

    # A bare list is a legal component tree and must be walked, not counted as nothing.
    listed = []
    tables_in([html.Div(dash_table.DataTable(columns=[], data=[]))], listed)
    check("walks a bare list", len(listed) == 1, f"{len(listed)} table found in a list")

    # The three shapes the detectors claim.
    spec_column = {"name": "tool", "id": "tool"}
    malformed = findings("malformed", html.Div(dash_table.DataTable(
        columns=[{"name": spec_column, "id": spec_column}], data=[{"tool": "Read"}])))
    spec_hits = [f for f in malformed if f[0] == "column-spec"]
    check("a column spec nested inside a column spec is caught, since dash_table renders name "
          "as a React child and a dict there throws React #31",
          bool(spec_hits), f"{len(spec_hits)} finding(s): {sorted(f[3] for f in spec_hits)}")
    check("and it names both offending keys, not just the first",
          {f[3] for f in spec_hits} == {"name", "id"},
          "name and id are both reported")
    clean = [f for f in findings("fine", html.Div(dash_table.DataTable(
        columns=[{"name": "tool", "id": "tool"}], data=[{"tool": "Read"}])))
        if f[0] == "column-spec"]
    nan_hits = [f for f in findings("nan", html.Div(dash_table.DataTable(
        columns=[{"name": "target", "id": "target"}], data=[{"target": float("nan")}])))
        if f[0] == "nan-cell"]
    # The size guard, fed a store that grew and one that did not. Without this the guard has
    # never been seen to fire: the stub on the right object stops every harvest before the guard
    # gets a size to compare, which is the point of the stub and the reason this case exists.
    check("the size guard fires on a store that grew",
          "CHANGED the store" in (store_changed(1_667_072, 819_000_000) or ""),
          repr(store_changed(1_667_072, 819_000_000))[:80])
    check("and stays silent on one that did not, or on one it could not measure",
          store_changed(1_667_072, 1_667_072) is None and store_changed(None, 5) is None,
          "guard spoke when nothing changed")
    check("a NaN cell is a fault, since it is the missing value that is not JSON",
          len(nan_hits) == 1, f"{len(nan_hits)} nan-cell finding(s)")
    check("a well-formed spec raises nothing, so the gate is not simply always on",
          not clean, f"{len(clean)} finding(s) on a good table")

    shapes = findings("shapes", html.Div(dash_table.DataTable(
        columns=[{"name": "n", "id": "n", "type": "numeric"}, {"name": "u", "id": "u"}],
        data=[{"n": "1,234", "u": 7}, {"n": "-", "u": ""}])))
    kinds = {(k, c) for k, _l, _t, c, _v in shapes}
    check("detects all three shapes",
          kinds == {("stringified", "n"), ("placeholder", "n"), ("placeholder", "u")},
          f"{sorted(kinds)}")

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

    # Columns that hold digits without holding quantities. The values are chosen so the REGEX
    # matches and only the name exempts them: with "2.0.14" and "10:32:07" the pattern rejected
    # both on its own, so this check stayed green with TEXT_BY_NATURE emptied and was testing
    # nothing.
    clean = findings("clean", html.Div(dash_table.DataTable(
        columns=[{"name": "version", "id": "version"}, {"name": "ts", "id": "ts"}],
        data=[{"version": "2", "ts": "10"}])))
    check("exempt by name, not by pattern", not clean and NUMERIC_LOOKING.match("2"), f"{clean}")

    # Prose, exempted by measurement. A message whose entire text is "1" is not a stringified
    # number, and 385 of them are in this store.
    prose = findings("prose", html.Div(dash_table.DataTable(
        columns=[{"name": "preview", "id": "preview"}],
        data=[{"preview": "1"}, {"preview": "x" * (PROSE_MIN_LENGTH + 1)}])))
    short = findings("short", html.Div(dash_table.DataTable(
        columns=[{"name": "preview", "id": "preview"}], data=[{"preview": "1"}])))
    check("prose exempted, short columns still checked", not prose and bool(short),
          f"{len(prose)} in a column with prose, {len(short)} in one without")

    ok = all(results)
    print(f"SELF-TEST {'PASS' if ok else 'FAIL'} ({len(results)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    RENDER_ONLY = "--render-only" in sys.argv
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    try:
        sys.exit(main())
    except CannotAudit as why:
        # Exit 3 and a marker of its own, so the runner can tell "this store cannot answer" from
        # "this audit is broken". Both are non-zero: neither is a pass.
        print(f"AUDIT SKIPPED: {why}")
        sys.exit(3)

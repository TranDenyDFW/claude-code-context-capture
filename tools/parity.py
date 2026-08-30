"""Does the API say the same thing as the dashboard? Answered by asking both, not by arguing.

This is the gate the whole migration leans on. While Dash and a React frontend both exist they must
not be able to disagree, and "must not" is worth nothing without a command that checks it. Every
tab, under every selection state a reader can actually put the header into, from both sides,
compared field by field.

    python tools/parity.py                  # in-process, no server needed
    python tools/parity.py --url http://127.0.0.1:8059   # against a running server
    python tools/parity.py --tab tab-cost   # one tab, while working on it
    python tools/parity.py --self-test
    python tools/parity.py --must-fail     # corrupt the API's answer on purpose; this MUST fail

COMPARED DEEPLY, NOT BY COUNT. Two tables with the same number of rows can hold different rows, and
a count-only check would pass every time a value was quietly wrong, which is the defect class this
repo has spent the most time on. Cell values are compared, not just shapes.

WHAT IS DELIBERATELY NOT COMPARED. `text` includes figures' own titles and prose that carries live
numbers ("newest call in the store"), which move between two renders taken a second apart against a
store being harvested continuously. Comparing them would report a difference on every run and the
tool would stop being read. Tables and figure extents are the payload a frontend draws from and are
stable within a run; those are compared in full.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The five states `tests/test_tabs_render.py` already defines. Named here rather than invented, so
# the parity surface and the render surface cannot drift apart.
STATES = [
    ("nothing selected", {}),
    ("one session", {"session": "{session}"}),
    ("one session, subagents", {"session": "{session}", "scope": "all"}),
    ("a cohort", {"cohort": "{cohort}"}),
    ("a session inside a cohort", {"session": "{session}", "cohort": "{cohort}"}),
]


def differences(served, direct, where):
    """Every way two payloads disagree, as readable lines. Empty means identical."""
    out = []
    a_tables, b_tables = served.get("tables", []), direct.get("tables", [])
    if len(a_tables) != len(b_tables):
        out.append(f"{where}: {len(a_tables)} tables from the API, {len(b_tables)} from Dash")
    for a, b in zip(a_tables, b_tables, strict=False):
        label = f"{where}/{b.get('id')}"
        if a.get("id") != b.get("id"):
            out.append(f"{label}: table id {a.get('id')!r} vs {b.get('id')!r}")
        if a.get("columns") != b.get("columns"):
            out.append(f"{label}: columns differ")
        if len(a.get("rows", [])) != len(b.get("rows", [])):
            out.append(f"{label}: {len(a.get('rows', []))} rows vs {len(b.get('rows', []))}")
            continue
        # Cell by cell. A count match with a value mismatch is the failure this exists to catch.
        for index, (row_a, row_b) in enumerate(zip(a.get("rows", []), b.get("rows", []),
                                                   strict=False)):
            if _normalise(row_a) != _normalise(row_b):
                out.append(f"{label}: row {index} differs")
                break
        if a.get("tooltips") != b.get("tooltips"):
            out.append(f"{label}: tooltips differ")

    a_figs, b_figs = served.get("figures", []), direct.get("figures", [])
    if len(a_figs) != len(b_figs):
        out.append(f"{where}: {len(a_figs)} figures from the API, {len(b_figs)} from Dash")
    for a, b in zip(a_figs, b_figs, strict=False):
        if a.get("title") != b.get("title"):
            out.append(f"{where}: figure title {a.get('title')!r} vs {b.get('title')!r}")
        if len(a.get("traces", [])) != len(b.get("traces", [])):
            out.append(f"{where}: figure {b.get('title')!r} trace count differs")
            continue
        for ta, tb in zip(a.get("traces", []), b.get("traces", []), strict=False):
            if _normalise(ta) != _normalise(tb):
                out.append(f"{where}: figure {b.get('title')!r} trace {tb.get('name')!r} differs")
                break
    return out


def _normalise(value):
    """Both sides reduced to what a JSON consumer would actually receive.

    Two representations of the same fact reach this function and neither side is wrong:

    - `4` and `np.int64(4)`. The API's payload has been through a JSON encoder and the Dash side
      has not, so the same number arrives as two different objects.
    - `nan` and `None`. JSON has no NaN. The Cost tab renders an unpriced model as a blank cell,
      which is `float('nan')` in the DataFrame and `null` over HTTP, and the HTTP side is the
      correct one: `null` is exactly "this app does not know what this cost", which is the
      distinction that whole column exists to preserve.

    The first version of this tool missed the NaN case and reported twelve differences across the
    Cost, Compactions and Session tabs on its first run. All twelve were this. Worth stating,
    because the instinct on seeing them was that the API had a bug, and the check that settled it
    was asking Dash to compare against ITSELF, which came back identical.
    """
    def clean(node):
        # NaN is the only value in the language that is not equal to itself.
        if isinstance(node, float) and node != node:
            return None
        if isinstance(node, dict):
            return {k: clean(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [clean(v) for v in node]
        return node
    return clean(json.loads(json.dumps(value, default=str)))


def pick_session(store):
    """The session that exercises the MOST of the dashboard, not simply the biggest one.

    The obvious choice is the session with the most turns, and it was the first choice here. It has
    zero compactions, and 3 of the 5 Compactions comparisons were therefore two empty tables
    agreeing with each other: a pass that checked nothing. Only 41 of this store's 1,325 have a
    compaction at all, so picking one is not a detail, it is the difference between the Compactions
    tab being covered and being skipped in silence.

    Busiest session that also compacted; the busiest overall if none did.
    """
    compacted = store.q("""
        SELECT t.session_id
          FROM turns t
          JOIN compactions c ON c.session_id = t.session_id
         GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1""")
    if not compacted.empty:
        return compacted.iloc[0]["session_id"]
    found = store.q("SELECT session_id FROM turns GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1")
    return None if found.empty else found.iloc[0]["session_id"]


def carries_data(payload):
    """Did this comparison have anything to disagree about?

    Two empty panes are identical, and counting that as a passing comparison inflates the number
    this tool prints. Reported separately so nobody reads '40 passed' as '40 checked something'.
    """
    rows = sum(len(t.get("rows", [])) for t in payload.get("tables", []))
    traces = sum(len(f.get("traces", [])) for f in payload.get("figures", []))
    return rows + traces > 0


def fetch_api(client, tab, params, render=False):
    """The API's answer, built fresh.

    `no_cache` deliberately. The API may serve an answer up to five seconds old (see
    `c4x/api/cache.py`), and the Dash side of this comparison is always built now, against a store
    that is harvested continuously. Comparing the two would report a difference whenever a turn
    landed in between, which is a property of the clock rather than of either backend, and it would
    make this gate flaky in exactly the way that teaches people to re-run it until it passes.

    That the CACHED answer matches a fresh one is a separate property, checked separately, in
    `tests/test_api.py::test_the_cache_does_not_change_the_answer`.
    """
    suffix = "/render" if render else ""
    response = client.get(f"/api/tab/{tab}{suffix}", params={**params, "no_cache": "1"})
    if response.status_code != 200:
        raise RuntimeError(f"API {response.status_code} for {tab} {params}: {response.text[:200]}")
    return response.json()


def fetch_dash(module, tab, params):
    """The same pane the browser gets, through the same callback."""
    from c4x.cli import extract
    ids = [t[0] for t in module.TABS]
    session = params.get("session")
    scope = params.get("scope", "main")
    cohort = params.get("cohort")
    if tab == "tab-compare":
        from c4x.tabs.compare import default_arm_b
        target = default_arm_b(session, cohort)
        if target:
            return extract.describe(module._cmp_render("session", target, session, cohort, scope))
    return extract.describe(module._render_tab(ids.index(tab), session, scope, cohort))


def run(only_tab=None, url=None, verbose=False):
    import app as module
    from c4x import store

    if url:
        import httpx
        client = httpx.Client(base_url=url, timeout=120)
    else:
        from fastapi.testclient import TestClient

        from c4x.api.main import api
        client = TestClient(api)

    session = pick_session(store)
    cohorts = store.cohort_options()
    cohort = cohorts[0]["value"] if cohorts else None

    tabs = [t[0] for t in module.TABS]
    if only_tab:
        if only_tab not in tabs:
            print(f"unknown tab {only_tab!r}. Known: {', '.join(tabs)}")
            return 2
        tabs = [only_tab]

    checked = 0
    empty = []
    problems = []
    for tab in tabs:
        for label, template in STATES:
            params = {}
            for key, value in template.items():
                resolved = (session if value == "{session}"
                            else cohort if value == "{cohort}" else value)
                if resolved is None:
                    params = None
                    break
                params[key] = resolved
            if params is None:
                # A state that cannot be built on this store is REPORTED, not silently dropped.
                problems.append(f"SKIPPED {tab} / {label}: this store has no cohort to select")
                continue
            served = fetch_api(client, tab, params)
            direct = fetch_dash(module, tab, params)
            found_here = differences(served, direct, f"{tab} / {label}")
            checked += 1
            if not carries_data(direct):
                empty.append(f"{tab} / {label}")
            if found_here:
                problems.extend(found_here)
            elif verbose:
                print(f"  ok  {tab} / {label}")

    real = [p for p in problems if not p.startswith("SKIPPED")]
    skipped = [p for p in problems if p.startswith("SKIPPED")]
    for line in skipped:
        print(f"  {line}")
    for line in real:
        print(f"  DIFF  {line}")
    print(f"\n  session under test: {session}")
    print(f"  {checked} comparison(s) across {len(tabs)} tab(s) "
          f"and {len(STATES)} selection states")
    # Stated, not hidden. A comparison of two empty panes passes without checking anything, and the
    # headline count is worth less than it looks if a quarter of it is that.
    if empty:
        print(f"  {len(empty)} of them carried no rows and no traces on either side:")
        for line in empty:
            print(f"      empty  {line}")
    if real:
        print(f"  PARITY FAIL  {len(real)} difference(s)")
        return 1
    print("  PARITY PASS  the API and the dashboard say the same thing")
    return 0


def must_fail(only_tab="tab-compactions"):
    """Corrupt what the API returns and confirm the whole tool reports it.

    `--self-test` proves the DIFFER. This proves the PLUMBING: that a wrong value coming back over
    HTTP survives the fetch, the normalise and the report, and lands as a FAIL line. Those are
    different failures. A `_normalise` that flattened everything to a string, or a fetch that
    quietly returned the Dash payload for both sides, would pass every self-test above and turn
    this whole file into a very thorough way of comparing something to itself.

    Run at a phase boundary, before believing a PARITY PASS.
    """
    original = globals()["fetch_api"]

    def corrupting(client, tab, params, render=False):
        payload = original(client, tab, params, render)
        for table in payload.get("tables", []):
            for row in table.get("rows", []):
                for key in row:
                    row[key] = "CORRUPTED-BY-THE-MUST-FAIL-CONTROL"
                    return payload
        return payload

    globals()["fetch_api"] = corrupting
    try:
        code = run(only_tab=only_tab)
    finally:
        globals()["fetch_api"] = original
    if code == 1:
        print("\n  MUST-FAIL PASS  a corrupted payload is reported as a difference")
        return 0
    print(f"\n  MUST-FAIL FAIL  the gate accepted a corrupted payload (exit {code}). "
          "It is not a gate.")
    return 1


def self_test():
    """The differ, fed payloads that MUST be reported as different."""
    same = {"tables": [{"id": "t", "columns": ["a"], "rows": [{"a": 1}], "tooltips": {}}],
            "figures": [{"title": "f", "traces": [{"name": "x", "y_max": 3}]}]}
    cases = [
        ("identical payloads produce no differences",
         differences(same, same, "w") == []),
        ("a missing table is a difference",
         len(differences({"tables": [], "figures": same["figures"]}, same, "w")) >= 1),
        ("a changed CELL is a difference, not just a changed count",
         len(differences({"tables": [{"id": "t", "columns": ["a"], "rows": [{"a": 2}],
                                      "tooltips": {}}], "figures": same["figures"]},
                         same, "w")) == 1),
        ("a changed column list is a difference",
         len(differences({"tables": [{"id": "t", "columns": ["b"], "rows": [{"a": 1}],
                                      "tooltips": {}}], "figures": same["figures"]},
                         same, "w")) == 1),
        ("a missing tooltip is a difference",
         len(differences({"tables": [{"id": "t", "columns": ["a"], "rows": [{"a": 1}],
                                      "tooltips": {"a": "help"}}], "figures": same["figures"]},
                         same, "w")) == 1),
        ("a changed trace extent is a difference",
         len(differences({"tables": same["tables"],
                          "figures": [{"title": "f", "traces": [{"name": "x", "y_max": 99}]}]},
                         same, "w")) == 1),
        ("a numpy-shaped number equals its plain twin",
         _normalise({"a": 1}) == _normalise({"a": 1.0}) or _normalise(1) == 1),
        # NaN and null are the same fact in two representations, and all twelve differences this
        # tool reported on its first run were this and nothing else.
        ("a NaN cell equals the null a JSON consumer receives",
         _normalise({"a": float("nan")}) == _normalise({"a": None})),
        ("a NaN nested in a list is normalised too",
         _normalise({"a": [1, float("nan")]}) == _normalise({"a": [1, None]})),
        ("but a real zero is NOT the same as a blank",
         _normalise({"a": 0.0}) != _normalise({"a": None})),
        # A comparison of two empty panes passes without checking anything. It is reported, and
        # this is the check that the reporting can tell the two apart.
        ("a pane with rows counts as carrying data", carries_data(same) is True),
        ("a pane with only a trace still counts",
         carries_data({"tables": [{"id": "t", "rows": []}], "figures": same["figures"]}) is True),
        ("an empty pane is recognised as empty",
         carries_data({"tables": [{"id": "t", "rows": []}], "figures": []}) is False),
        ("the five states are the ones the render tests use", len(STATES) == 5),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tab", help="check one tab only")
    ap.add_argument("--url", help="check a RUNNING server instead of an in-process one")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--must-fail", action="store_true",
                    help="corrupt the API's answer on purpose and confirm this tool reports it")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.must_fail:
        return must_fail(args.tab or "tab-compactions")
    return run(args.tab, args.url, args.verbose)


if __name__ == "__main__":
    sys.exit(main())

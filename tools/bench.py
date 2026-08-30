"""Per-tab render latency, recorded so a migration cannot quietly make the app slower.

This exists because of one constraint on the React migration: "just make sure it doesn't get
slower". That is easy to say and impossible to check after the fact, because nobody remembers what
the old app felt like and a rewrite lands as one large diff. So the numbers are taken BEFORE the
work starts, committed, and turned into a gate.

    python tools/bench.py                            # measure and print
    python tools/bench.py --write                    # record tools/bench-baseline.json
    python tools/bench.py --against                   # compare to it, exit 1 on regression
    python tools/bench.py --self-test

WHAT IT MEASURES, and what it deliberately does not. It calls `app._render_tab`, the same callback
the browser and the CLI go through, and times the server-side build of one pane. It does not time
the network, the browser, or Plotly's own draw. Those are real, and they are also the parts a
frontend rewrite changes on purpose; the point of this gate is the part that is supposed to stay
the same, which is how long it takes to turn SQLite into a page.

THE TOLERANCES ARE MEASURED, NOT CHOSEN. Running one tab seven times in a row on this machine:

    tab-summary       median 1719 ms   spread  11.7%
    tab-cost          median  992 ms   spread   6.7%
    tab-compactions   median   16 ms   spread 193.1%

A relative tolerance alone is useless at the fast end: a 16 ms tab wanders to 47 ms just from GC and
scheduler noise, which is a 193% "regression" every other run. An absolute tolerance alone is
useless at the slow end: 50 ms of drift on a 1,700 ms tab is invisible and a 400 ms one is not. So a
tab fails only when it exceeds BOTH, which is what makes this gate quiet enough to be believed.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tools" / "bench-baseline.json"

# Above the 11.7% worst-case spread measured on the slowest tab, with room for a busier machine.
SLOWER_BY_FRACTION = 0.25
# Below this, a difference is not something a reader could feel, and it is inside the noise these
# tabs actually produce. 100 ms is the usual perceptibility threshold, and it is also what the
# measurements demanded: at a 50 ms floor this gate reported tab-diagnostics "regressing" from
# 12 ms to 64 ms, a +425% failure that was a transient on an idle machine and nothing a user could
# notice. A gate that cries wolf on a 12 ms tab teaches people to ignore it, which costs more than
# the regression it was watching for.
#
# The cost of the wider floor is stated rather than hidden: a genuinely 10 ms tab degrading to
# 100 ms will not fail this check. Nothing in this app is near that shape, and the tabs that matter
# for the migration are the three over 900 ms, where the relative threshold binds first.
SLOWER_BY_MS = 100.0

REPEATS = 5

# How far the store may have moved and still be the same store.
#
# Exact equality was the first attempt and it made the gate unrunnable: this store is harvested
# continuously, so eight turns arrived in the sixty seconds between recording a baseline and
# checking against it, and the check refused every time. A gate that never runs is not a gate.
# Ten percent is far wider than a session's worth of drift and far narrower than the difference
# between a real store and the CI fixture, which differ by three orders of magnitude.
SAME_STORE_TOLERANCE = 0.10


def fingerprint():
    """What store the numbers describe.

    A baseline taken on a 1,324-session store says nothing about a 9-session one, and comparing
    them would produce a confident, meaningless verdict. Recorded so the comparison can refuse.
    """
    from c4x import store
    row = store.q("""SELECT (SELECT COUNT(*) FROM sessions) AS sessions,
                            (SELECT COUNT(*) FROM turns)    AS turns,
                            (SELECT COUNT(*) FROM tool_calls) AS tool_calls""").iloc[0]
    return {k: int(row[k]) for k in ("sessions", "turns", "tool_calls")}


def same_store(base, now):
    """Is this recognisably the store the baseline was taken on?

    Proportional, not exact: see SAME_STORE_TOLERANCE. A missing fingerprint on either side is
    treated as "cannot tell", which refuses rather than assumes.
    """
    if not base or not now:
        return False
    for key in ("sessions", "turns", "tool_calls"):
        a, b = base.get(key), now.get(key)
        if a is None or b is None:
            return False
        if a == 0 and b == 0:
            continue
        if abs(b - a) / max(a, 1) > SAME_STORE_TOLERANCE:
            return False
    return True


def measure(session_id=None, repeats=REPEATS, only=None):
    """Cold and warm milliseconds per tab. Warm is a MEDIAN, because one sample is a coin toss.

    `only` restricts the run to named tabs, which is what the confirmation pass below uses: when a
    tab looks regressed it is measured again, harder, rather than reported on one reading.
    """
    sys.path.insert(0, str(ROOT))
    import app as module

    ids = [t[0] for t in module.TABS]
    if session_id is None:
        from c4x import store
        found = store.q("SELECT session_id FROM turns GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1")
        session_id = None if found.empty else found.iloc[0]["session_id"]

    def build(index, tab):
        """One pane, the way a reader gets it.

        Compare is special and it is not a special case invented here: its pane is a pair of
        dropdowns and its content arrives from a SECOND callback, so timing `_render_tab` alone
        measures an empty room. The original baseline recorded 83 ms for Compare on exactly that
        basis, and the tab really costs about 1.7 seconds to fill. The CLI, the API and the browser
        all render the body; this now measures the same thing they do.
        """
        if tab == "tab-compare":
            from c4x.tabs.compare import default_arm_b
            target = default_arm_b(session_id, None)
            if target:
                return module._cmp_render("session", target, session_id, None, "main")
        return module._render_tab(index, session_id, "main", None)

    out = {}
    for index, tab in enumerate(ids):
        if only and tab not in only:
            continue
        start = time.perf_counter()
        build(index, tab)
        cold = (time.perf_counter() - start) * 1000
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            build(index, tab)
            samples.append((time.perf_counter() - start) * 1000)
        out[tab] = {"cold_ms": round(cold, 1),
                    "warm_ms": round(statistics.median(samples), 1),
                    "warm_min_ms": round(min(samples), 1),
                    "warm_max_ms": round(max(samples), 1)}
    return {"store": fingerprint(), "session": session_id, "repeats": repeats, "tabs": out}


def measure_api(session_id=None, repeats=REPEATS, only=None):
    """The same tabs, over the API, which is the path the migration actually ends on.

    THREE NUMBERS, because one would mislead. `cold_ms` is a rebuild with the cache explicitly
    bypassed: the true cost of a first view, and the only number comparable like for like against
    the Dash baseline, since it pays the same SQL plus the describe and the JSON encode that HTTP
    adds. `warm_ms` is a cached answer, which is what a reader gets on every view after the first
    and is the reason the end state is faster than what it replaces. Reporting only the warm figure
    would be a way of saying "we made it 400x faster" while hiding that the first view did not move.

    In-process through `TestClient`, deliberately: measuring across a real socket would add the
    loopback round trip to every sample and measure the operating system rather than this code.
    Payloads here top out at 543 KB on loopback, which is single-digit milliseconds and does not
    change any conclusion below.
    """
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    import app as module
    from c4x.api.main import api

    ids = [t[0] for t in module.TABS]
    if session_id is None:
        from c4x import store
        found = store.q("SELECT session_id FROM turns GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 1")
        session_id = None if found.empty else found.iloc[0]["session_id"]

    client = TestClient(api)
    params = {"session": session_id} if session_id else {}
    out = {}
    for tab in ids:
        if only and tab not in only:
            continue
        # Discarded. The first request to any tab pays import and first-touch costs that no reader
        # ever pays twice, and including it would make the uncached median a measurement of startup.
        forced = client.get(f"/api/tab/{tab}", params={**params, "no_cache": "1"})
        if forced.status_code != 200:
            raise SystemExit(f"{tab} did not answer: {forced.status_code} {forced.text[:200]}")

        # A MEDIAN, not one reading, and this is the number the gate uses. The Dash baseline it is
        # compared against is a median of five; comparing a single sample to it would fail on noise
        # and pass on noise in roughly equal measure.
        uncached = []
        for _ in range(repeats):
            start = time.perf_counter()
            client.get(f"/api/tab/{tab}", params={**params, "no_cache": "1"})
            uncached.append((time.perf_counter() - start) * 1000)

        client.get(f"/api/tab/{tab}", params=params)          # prime the cache
        samples, served_from = [], set()
        for _ in range(repeats):
            start = time.perf_counter()
            response = client.get(f"/api/tab/{tab}", params=params)
            samples.append((time.perf_counter() - start) * 1000)
            served_from.add(response.headers.get("x-c4x-cache", "?"))
        out[tab] = {"cold_ms": round(statistics.median(uncached), 1),
                    "warm_ms": round(statistics.median(samples), 1),
                    "warm_min_ms": round(min(samples), 1),
                    "warm_max_ms": round(max(samples), 1),
                    # Recorded, because a "warm" number taken entirely from cache misses would look
                    # like a regression in this tool rather than what it is: the cache not working.
                    "served": sorted(served_from)}
    return {"store": fingerprint(), "session": session_id, "repeats": repeats,
            "via": "api", "tabs": out}


def drift(base, now):
    """How much the WHOLE machine moved between the two runs, as a median ratio.

    This is the fix for the finding an independent review raised: the gate flapped FAIL/PASS/PASS
    on an unchanged tree. The cause was not the thresholds, it was comparing wall-clock against
    wall-clock. On a busier machine every tab slows together, and a fixed threshold eventually
    calls that a code regression.

    Observed on one such run: summary 1.19x, session 1.19x, window 1.18x, cost 1.14x. Four
    unrelated tabs moving by the same fraction is a machine, not a change to four code paths.

    So the comparison below asks a better question: did this tab get slower THAN THE OTHERS. A
    single regressed tab still stands out against a median of its peers, and a uniformly busier
    machine no longer registers at all.

    THE COST, stated: a change that slows every tab equally, say a shared helper in `store.q`,
    moves the median with it and hides inside it. That is why the drift is printed on every run,
    however the verdict comes out. A drift of 1.4 is worth looking at even when nothing failed.
    """
    ratios = []
    for tab, was in base.get("tabs", {}).items():
        has = now.get("tabs", {}).get(tab)
        if has and was.get("warm_ms", 0) > 0:
            ratios.append(has[field_for(now)] / was["warm_ms"])
    return statistics.median(ratios) if ratios else 1.0


def field_for(run):
    """Which number in `run` is comparable to the baseline's warm figure.

    The baseline is a Dash pane, rebuilt on every callback, so its warm number IS its build cost.
    An API run has two numbers and only one of them answers the same question: `cold_ms` is a build
    with the cache bypassed, which pays the same SQL plus what HTTP adds, and `warm_ms` is a cached
    answer that Dash has no equivalent for at all.

    So the GATE compares builds to builds. Comparing a 3 ms cache hit against a 1,581 ms Dash render
    and reporting "400x faster, no regressions" would be true, useless, and would hide a first view
    that had got slower. The cache win is reported separately, where it cannot be mistaken for the
    build getting cheaper.
    """
    return "cold_ms" if run.get("via") == "api" else "warm_ms"


def regressions(base, now, machine=None):
    """Which tabs got slower by BOTH thresholds, allowing for how the machine as a whole moved.

    `machine` is the median ratio from `drift()`. Passing 1.0 compares against the raw baseline,
    which is what the pure self-test cases below do.
    """
    machine = drift(base, now) if machine is None else machine
    field = field_for(now)
    bad = []
    for tab, was in base.get("tabs", {}).items():
        has = now.get("tabs", {}).get(tab)
        if not has:
            bad.append(f"{tab}: present in the baseline and missing now")
            continue
        before, after = was["warm_ms"], has[field]
        delta = after - before
        # Compared against what the baseline WOULD be on today's machine, not against what it was
        # on the day it was recorded.
        expected = before * machine
        beyond_peers = (after - expected) / expected if expected > 0 else 0
        if delta > SLOWER_BY_MS and beyond_peers > SLOWER_BY_FRACTION:
            bad.append(f"{tab}: {before:.0f} ms -> {after:.0f} ms "
                       f"(+{delta:.0f} ms; {after / expected:.2f}x its peers)")
    return bad


def confirm(base, first, again):
    """Which suspected regressions survive a second, harder measurement.

    Split out of the command so both of its branches can be exercised without waiting on a live
    machine to produce a borderline reading, which is not something a test can arrange on demand.

    THE MERGE MATTERS. The first version passed only the suspect tabs to `regressions()`, and
    `regressions()` treats any baseline tab missing from the set it is given as a regression, so
    one suspected tab was reported as eight: the one real finding plus seven "present in the
    baseline and missing now" for tabs that were never re-measured. Found by running it, not by
    reading it.

    AND THE MERGE MUST CARRY `via`. Without it the merged payload looks like a Dash run, so
    `field_for` reads `warm_ms`, which on an API run is a 3 ms cache hit. Every suspected
    regression would then be cleared by a number that measures the cache rather than the build.
    Two different bugs, both in this one small function, both only visible when it was run.
    """
    merged = dict(first.get("tabs", {}))
    merged.update(again.get("tabs", {}))
    via = again.get("via") or first.get("via")
    return regressions(base, {"tabs": merged, "via": via} if via else {"tabs": merged})


def report(now, base=None):
    print(f"  store: {now['store']['sessions']:,} sessions, {now['store']['turns']:,} turns, "
          f"{now['store']['tool_calls']:,} tool calls")
    api = now.get("via") == "api"
    if api:
        print("  measured over the API. 'build' bypasses the cache and is what the gate compares")
        print("  against the Dash baseline; 'cached' is what a reader gets on every view after the")
        print("  first, and Dash has no equivalent for it.")
    header = f"  {'tab':18}{'build' if api else 'cold':>9}{'cached' if api else 'warm':>9}"
    if base:
        header += f"{'baseline':>10}{'delta':>9}"
    print(header)
    field = field_for(now)
    for tab, has in now["tabs"].items():
        line = f"  {tab:18}{has['cold_ms']:9.0f}{has['warm_ms']:9.0f}"
        if base:
            was = base.get("tabs", {}).get(tab)
            if was:
                d = has[field] - was["warm_ms"]
                line += f"{was['warm_ms']:10.0f}{d:+9.0f}"
        if has.get("served") and has["served"] != ["hit"]:
            # A warm sample that was not a cache hit is not a warm sample. Said out loud rather
            # than averaged in, because it makes the number in this row mean something else.
            line += f"   <- served {'/'.join(has['served'])}, not all cached"
        print(line)
    builds = sum(t["cold_ms"] for t in now["tabs"].values())
    cached = sum(t["warm_ms"] for t in now["tabs"].values())
    if api:
        print(f"  {'TOTAL':18}{builds:9.0f}{cached:9.0f}")
        if base:
            was = sum(t["warm_ms"] for t in base.get("tabs", {}).values())
            print(f"  every tab once, built: {builds:.0f} ms against {was:.0f} ms of Dash")
            print(f"  every tab again, cached: {cached:.0f} ms. That difference is the phase 3 win,"
                  " and it is a cache, not a faster query.")
    else:
        print(f"  {'TOTAL warm':18}{'':9}{cached:9.0f}")
    if base:
        moved = drift(base, now)
        # The note names the MACHINE only when both runs measured the same thing. Comparing an API
        # run to the Dash baseline moves this ratio for a reason that has nothing to do with load,
        # and an unqualified "the whole machine moved" would be a plain untruth in the output.
        if 0.85 <= moved <= 1.15:
            note = ""
        elif api:
            note = "   <- the backend changed, not the machine"
        else:
            note = "   <- the whole machine moved, not one tab"
        print(f"  drift vs the baseline: {moved:.2f}x{note}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help=f"record {BASELINE.name}")
    ap.add_argument("--against", action="store_true", help="compare and gate on the baseline")
    ap.add_argument("--session", help="session id to render with (default: the busiest)")
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--via", default="dash", choices=["dash", "api"],
                    help="measure the Dash callback (the baseline) or the API (the end state)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    # The baseline is the DASH numbers, recorded before the migration started, and it is the only
    # record of what the app used to cost. Writing API numbers over it would destroy the one thing
    # every later phase is measured against, and the run would print a cheerful confirmation. It is
    # refused rather than warned about.
    if args.write and args.via != "dash":
        print("REFUSED: the baseline records the pre-migration Dash numbers.")
        print("  Overwriting it with API numbers would delete the only thing to compare against.")
        print("  To see the API's numbers:  python tools/bench.py --via api --against")
        return 2

    now = measure_api(args.session, args.repeats) if args.via == "api" \
        else measure(args.session, args.repeats)

    if args.against:
        if not BASELINE.exists():
            print(f"no baseline at {BASELINE}. Record one first: python tools/bench.py --write")
            return 2
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        # A baseline from a different store is not a baseline. Refused rather than compared,
        # because the comparison would still print a confident table.
        if not same_store(base.get("store"), now["store"]):
            print("REFUSED: the baseline describes a different store.")
            print(f"  baseline: {base.get('store')}")
            print(f"  now     : {now['store']}")
            print("  re-record it, or run against the store it was taken on.")
            return 2
        report(now, base)
        bad = regressions(base, now)
        if bad:
            # CONFIRM BEFORE ACCUSING.
            #
            # An independent review ran this three times on an unchanged tree and got
            # FAIL / PASS / PASS: tab-window's warm time sits close enough to both thresholds that
            # a single reading flips the verdict. A gate whose answer depends on which run you
            # looked at is worse than no gate, and the first version of this file claimed in its
            # own commit message that the dual threshold prevented exactly that. It did not.
            #
            # So a suspected regression is re-measured, on those tabs only, with three times the
            # repeats, and it has to survive that to be reported. Noise does not survive it;
            # something genuinely slower does.
            suspects = sorted({line.split(":")[0] for line in bad})
            print(f"\n  {len(suspects)} tab(s) look slower. Re-measuring them "
                  f"with {args.repeats * 3} repeats before reporting.")
            # THE SAME BACKEND, and this was wrong once. The confirmation used to call `measure`
            # unconditionally, so an API run that flagged tab-compare at 1,698 ms was re-measured
            # through DASH at 75 ms and the difference was dismissed as noise. It was not noise: the
            # two numbers describe different things, because Dash's Compare pane is a pair of
            # dropdowns and the API serves the filled body. A confirmation that can clear a real
            # regression using the other backend's numbers is worse than no confirmation at all.
            again = (measure_api(args.session, args.repeats * 3, only=suspects) if args.via == "api"
                     else measure(args.session, args.repeats * 3, only=suspects))
            bad = confirm(base, now, again)
            for tab in suspects:
                was, first, second = (base["tabs"][tab]["warm_ms"],
                                      now["tabs"][tab][field_for(now)],
                                      again["tabs"][tab][field_for(again)])
                verdict = "confirmed" if any(line.startswith(tab) for line in bad) else "was noise"
                print(f"    {tab}: baseline {was:.0f}, first {first:.0f}, "
                      f"confirm {second:.0f}  ->  {verdict}")
        if bad:
            print(f"\n  SLOWER on {len(bad)} tab(s), by more than "
                  f"{SLOWER_BY_MS:.0f} ms AND {SLOWER_BY_FRACTION * 100:.0f}%:")
            for line in bad:
                print(f"    {line}")
            print("  BENCH FAIL")
            return 1
        print("\n  BENCH PASS  no tab slower than the baseline")
        return 0

    report(now)
    if args.write:
        # THE OLD BASELINE IS KEPT, NEVER OVERWRITTEN IN PLACE. It is the only record of what this
        # app cost before the migration, and a `--write` run that replaced it would delete that
        # with no error, no diff and a cheerful confirmation line. The archive is named after the
        # newest turn in the store it describes rather than after the clock, so the same store
        # always produces the same name and re-running this cannot quietly mint a second copy.
        if BASELINE.exists():
            previous = json.loads(BASELINE.read_text(encoding="utf-8"))
            marker = previous.get("store", {}).get("turns", "unknown")
            kept = BASELINE.with_name(f"{BASELINE.stem}-{marker}-turns.json")
            if not kept.exists():
                kept.write_text(json.dumps(previous, indent=2) + "\n", encoding="utf-8")
                print(f"  kept the previous baseline as {kept.name}")
            else:
                print(f"  the previous baseline is already archived as {kept.name}")
        BASELINE.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote {BASELINE}")
    return 0


def self_test():
    """The gate, fed inputs it MUST fail on.

    A regression check that only ever passes is furniture.
    """
    base = {"store": {"sessions": 1, "turns": 2, "tool_calls": 3},
            "tabs": {"slow": {"warm_ms": 1000.0}, "fast": {"warm_ms": 16.0}}}
    same = {"tabs": {"slow": {"warm_ms": 1000.0}, "fast": {"warm_ms": 16.0}}}
    cases = [
        ("an unchanged run is not a regression", regressions(base, same, machine=1.0) == []),
        ("noise on a fast tab is not a regression (16 -> 47 ms, +193%)",
         regressions(base, {"tabs": {"slow": {"warm_ms": 1000.0},
                                     "fast": {"warm_ms": 47.0}}}, machine=1.0) == []),
        ("drift inside the measured spread is not a regression (1000 -> 1100 ms, +10%)",
         regressions(base, {"tabs": {"slow": {"warm_ms": 1100.0},
                                     "fast": {"warm_ms": 16.0}}}, machine=1.0) == []),
        ("a real slowdown IS a regression (1000 -> 1400 ms, +40%)",
         len(regressions(base, {"tabs": {"slow": {"warm_ms": 1400.0},
                                         "fast": {"warm_ms": 16.0}}}, machine=1.0)) == 1),
        ("a tab that stopped existing is a regression",
         len(regressions(base, {"tabs": {"slow": {"warm_ms": 1000.0}}}, machine=1.0)) == 1),
        # WHICH NUMBER THE GATE READS. An API run carries a cache hit in `warm_ms`, and reading
        # that would compare a 3 ms lookup against a 1,000 ms render and call every build in the
        # world an improvement. The gate must read the uncached build instead.
        ("a Dash run is gated on its warm render", field_for({"tabs": {}}) == "warm_ms"),
        ("an API run is gated on its UNCACHED build",
         field_for({"via": "api", "tabs": {}}) == "cold_ms"),
        ("a slower API build is caught even though its cache hit looks instant",
         len(regressions(base, {"via": "api",
                                "tabs": {"slow": {"cold_ms": 1400.0, "warm_ms": 3.0},
                                         "fast": {"cold_ms": 16.0, "warm_ms": 3.0}}},
                         machine=1.0)) == 1),
        ("and a fast API build is not",
         regressions(base, {"via": "api",
                            "tabs": {"slow": {"cold_ms": 1000.0, "warm_ms": 3.0},
                                     "fast": {"cold_ms": 16.0, "warm_ms": 3.0}}},
                     machine=1.0) == []),
        ("getting faster is never a regression",
         regressions(base, {"tabs": {"slow": {"warm_ms": 200.0},
                                     "fast": {"warm_ms": 2.0}}}, machine=1.0) == []),
        ("a transient on a fast tab is not a regression (12 -> 64 ms, +425%)",
         regressions({"tabs": {"d": {"warm_ms": 12.0}}},
                     {"tabs": {"d": {"warm_ms": 64.0}}}, machine=1.0) == []),
        ("but a fast tab genuinely blowing up still fails (12 -> 900 ms)",
         len(regressions({"tabs": {"d": {"warm_ms": 12.0}}},
                         {"tabs": {"d": {"warm_ms": 900.0}}}, machine=1.0)) == 1),
        ("both thresholds are required, not either",
         regressions(base, {"tabs": {"slow": {"warm_ms": 1040.0},   # +40 ms
                                     # +14 ms, +88%: over the fraction, under the floor
                                     "fast": {"warm_ms": 30.0}}}, machine=1.0) == []),
        # The store fingerprint. Exact equality made this unrunnable against a live store.
        ("a store that grew by a few turns is the same store",
         same_store({"sessions": 1325, "turns": 345316, "tool_calls": 193619},
                    {"sessions": 1325, "turns": 345324, "tool_calls": 193622})),
        ("the CI fixture is NOT the same store as a real one",
         not same_store({"sessions": 1325, "turns": 345316, "tool_calls": 193619},
                        {"sessions": 5, "turns": 470, "tool_calls": 300})),
        ("a store that grew 40% is not the same store",
         not same_store({"sessions": 100, "turns": 1000, "tool_calls": 500},
                        {"sessions": 100, "turns": 1400, "tool_calls": 500})),
        ("a missing fingerprint refuses rather than assumes",
         not same_store(None, {"sessions": 1, "turns": 1, "tool_calls": 1})),
        # The confirmation pass. An independent review caught this gate flapping FAIL/PASS/PASS on
        # an unchanged tree, so a suspected regression is now re-measured before being reported.
        ("a suspicion that does not survive re-measurement is dropped",
         confirm({"tabs": {"a": {"warm_ms": 400.0}, "b": {"warm_ms": 50.0}}},
                 {"tabs": {"a": {"warm_ms": 560.0}, "b": {"warm_ms": 50.0}}},
                 {"tabs": {"a": {"warm_ms": 410.0}}}) == []),
        ("a suspicion that survives re-measurement is reported",
         len(confirm({"tabs": {"a": {"warm_ms": 400.0}, "b": {"warm_ms": 50.0}}},
                     {"tabs": {"a": {"warm_ms": 900.0}, "b": {"warm_ms": 50.0}}},
                     {"tabs": {"a": {"warm_ms": 950.0}}})) == 1),
        # Machine drift. Four tabs all 1.2x slower is a busier machine, not four regressions.
        ("a machine that got uniformly slower is not a regression",
         regressions({"tabs": {"a": {"warm_ms": 1000.0}, "b": {"warm_ms": 800.0},
                               "c": {"warm_ms": 600.0}, "d": {"warm_ms": 400.0}}},
                     {"tabs": {"a": {"warm_ms": 1300.0}, "b": {"warm_ms": 1040.0},
                               "c": {"warm_ms": 780.0}, "d": {"warm_ms": 520.0}}}) == []),
        ("one tab slower than its peers still fails, on that same busier machine",
         len(regressions({"tabs": {"a": {"warm_ms": 1000.0}, "b": {"warm_ms": 800.0},
                                   "c": {"warm_ms": 600.0}, "d": {"warm_ms": 400.0}}},
                         {"tabs": {"a": {"warm_ms": 2600.0}, "b": {"warm_ms": 1040.0},
                                   "c": {"warm_ms": 780.0}, "d": {"warm_ms": 520.0}}})) == 1),
        ("drift is the median ratio, not the mean, so one outlier cannot set it",
         abs(drift({"tabs": {"a": {"warm_ms": 100.0}, "b": {"warm_ms": 100.0},
                             "c": {"warm_ms": 100.0}}},
                   {"tabs": {"a": {"warm_ms": 120.0}, "b": {"warm_ms": 120.0},
                             "c": {"warm_ms": 900.0}}}) - 1.2) < 0.001),
        ("tabs that were not re-measured are not reported as missing",
         len(confirm({"tabs": {"a": {"warm_ms": 400.0}, "b": {"warm_ms": 50.0},
                               "c": {"warm_ms": 60.0}}},
                     {"tabs": {"a": {"warm_ms": 900.0}, "b": {"warm_ms": 50.0},
                               "c": {"warm_ms": 60.0}}},
                     {"tabs": {"a": {"warm_ms": 950.0}}})) == 1),
        # The confirmation must keep reading the BUILD on an API run. Dropping `via` in the merge
        # made it read a 3 ms cache hit instead, which clears every suspicion ever raised.
        ("an API suspicion that survives is still reported after the merge",
         len(confirm({"tabs": {"a": {"warm_ms": 400.0}, "b": {"warm_ms": 50.0}}},
                     {"via": "api", "tabs": {"a": {"cold_ms": 900.0, "warm_ms": 3.0},
                                             "b": {"cold_ms": 50.0, "warm_ms": 3.0}}},
                     {"via": "api", "tabs": {"a": {"cold_ms": 950.0, "warm_ms": 3.0}}})) == 1),
        ("and an API suspicion that does not survive is still dropped",
         confirm({"tabs": {"a": {"warm_ms": 400.0}, "b": {"warm_ms": 50.0}}},
                 {"via": "api", "tabs": {"a": {"cold_ms": 560.0, "warm_ms": 3.0},
                                         "b": {"cold_ms": 50.0, "warm_ms": 3.0}}},
                 {"via": "api", "tabs": {"a": {"cold_ms": 410.0, "warm_ms": 3.0}}}) == []),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

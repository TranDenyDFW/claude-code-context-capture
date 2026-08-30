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

    out = {}
    for index, tab in enumerate(ids):
        if only and tab not in only:
            continue
        start = time.perf_counter()
        module._render_tab(index, session_id, "main", None)
        cold = (time.perf_counter() - start) * 1000
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            module._render_tab(index, session_id, "main", None)
            samples.append((time.perf_counter() - start) * 1000)
        out[tab] = {"cold_ms": round(cold, 1),
                    "warm_ms": round(statistics.median(samples), 1),
                    "warm_min_ms": round(min(samples), 1),
                    "warm_max_ms": round(max(samples), 1)}
    return {"store": fingerprint(), "session": session_id, "repeats": repeats, "tabs": out}


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
            ratios.append(has["warm_ms"] / was["warm_ms"])
    return statistics.median(ratios) if ratios else 1.0


def regressions(base, now, machine=None):
    """Which tabs got slower by BOTH thresholds, allowing for how the machine as a whole moved.

    `machine` is the median ratio from `drift()`. Passing 1.0 compares against the raw baseline,
    which is what the pure self-test cases below do.
    """
    machine = drift(base, now) if machine is None else machine
    bad = []
    for tab, was in base.get("tabs", {}).items():
        has = now.get("tabs", {}).get(tab)
        if not has:
            bad.append(f"{tab}: present in the baseline and missing now")
            continue
        before, after = was["warm_ms"], has["warm_ms"]
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
    """
    merged = dict(first.get("tabs", {}))
    merged.update(again.get("tabs", {}))
    return regressions(base, {"tabs": merged})


def report(now, base=None):
    print(f"  store: {now['store']['sessions']:,} sessions, {now['store']['turns']:,} turns, "
          f"{now['store']['tool_calls']:,} tool calls")
    header = f"  {'tab':18}{'cold':>9}{'warm':>9}"
    if base:
        header += f"{'baseline':>10}{'delta':>9}"
    print(header)
    for tab, has in now["tabs"].items():
        line = f"  {tab:18}{has['cold_ms']:9.0f}{has['warm_ms']:9.0f}"
        if base:
            was = base.get("tabs", {}).get(tab)
            if was:
                d = has["warm_ms"] - was["warm_ms"]
                line += f"{was['warm_ms']:10.0f}{d:+9.0f}"
        print(line)
    total = sum(t["warm_ms"] for t in now["tabs"].values())
    print(f"  {'TOTAL warm':18}{'':9}{total:9.0f}")
    if base:
        moved = drift(base, now)
        note = "" if 0.85 <= moved <= 1.15 else "   <- the whole machine moved, not one tab"
        print(f"  machine drift vs the baseline: {moved:.2f}x{note}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help=f"record {BASELINE.name}")
    ap.add_argument("--against", action="store_true", help="compare and gate on the baseline")
    ap.add_argument("--session", help="session id to render with (default: the busiest)")
    ap.add_argument("--repeats", type=int, default=REPEATS)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    now = measure(args.session, args.repeats)

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
            again = measure(args.session, args.repeats * 3, only=suspects)
            bad = confirm(base, now, again)
            for tab in suspects:
                was, first, second = (base["tabs"][tab]["warm_ms"],
                                      now["tabs"][tab]["warm_ms"], again["tabs"][tab]["warm_ms"])
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
        BASELINE.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
        print(f"\n  wrote {BASELINE}")
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

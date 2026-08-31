"""Regenerate the README's images from a running dashboard.

The images in a README go stale the moment a tab is renamed, and a stale screenshot is worse than
none: it shows a reader an interface that no longer exists and they trust it because it is a
photograph. This makes them a build artifact instead of a memory, so refreshing them after a UI
change is one command rather than a manual capture session nobody repeats.

    python tools/screenshots.py                     # every tab, into docs/images/
    python tools/screenshots.py --url http://127.0.0.1:8067   # a server on another port
    python tools/screenshots.py --only summary,cost # just the ones that changed

It drives the REAL page rather than rendering components to a canvas, because the thing a README
should show is the thing a reader will see, including the fonts, the dark theme and the live
context bar in the header.

Requires playwright (`pip install playwright && playwright install chromium`). Deliberately NOT in
requirements.txt: it is a maintenance tool for this repo, not something an installing user needs.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "images"
DEFAULT_URL = "http://127.0.0.1:8059"

# The tab id, the file it becomes, and how far down the page to start.
#
# A full-page capture of a tab that is 2,700px tall is unreadable at README width, so each entry
# says which BAND of the page is worth showing. `scroll` is where the interesting part begins;
# `height` is how much of it to keep. Both are in CSS pixels at the viewport width below.
#
# THESE OFFSETS WERE TUNED AGAINST THE DASH LAYOUT and have not been re-tuned for the React page,
# which has a shorter header and different section spacing. A smoke run of `compactions` came back
# with the top of the chart clipped. They are left as they are on purpose: `docs/images/` was
# reviewed and chosen, and quietly regenerating eight images with the wrong crop would replace
# curated pictures with worse ones. Re-tune the band per tab before the next regeneration, one tab
# at a time with --only, and look at each result.
SHOTS = [
    ("tab-summary",     "summary",     0,    980, "findings with actions, store totals"),
    ("tab-sessions",    "sessions",    260,  900, "every session as a point, table shaded by rank"),
    ("tab-session",     "session",     170,  900, "context growth, compaction marks, anomaly band"),
    ("tab-compactions", "compactions", 120,  820, "every compaction and what it discarded"),
    ("tab-window",      "window",      120,  900, "what is in the window right now, as area"),
    ("tab-cost",        "cost",        120,  900, "re-reads, concentration, estimated cost"),
    ("tab-compare",     "compare",     100,  700, "two populations measured the same way"),
    ("tab-diagnostics", "diagnostics", 100,  820, "is the capture healthy, does the model agree"),
]

VIEWPORT = {"width": 1440, "height": 950}


def capture(url, out_dir, only=None, settle=4500):
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {s.strip() for s in only.split(",")} if only else None
    written, skipped = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        # "load", not "networkidle". The dashboard carries a dcc.Interval that polls every few
        # seconds to keep the header's context bar live, so the network is NEVER idle and
        # networkidle waits the full timeout and then fails on a page that rendered fine in two
        # seconds. The explicit settle below is what actually makes the page ready.
        page.goto(url, wait_until="load", timeout=60_000)
        # The first paint runs every tab's callbacks. Without this the hero image catches the page
        # mid-render and the tables are empty, which is a photograph of a bug that is not there.
        page.wait_for_timeout(9_000)

        for tab_id, name, scroll, height, what in SHOTS:
            if wanted and name not in wanted:
                skipped.append(name)
                continue
            page.click(f"#btn-{tab_id}")
            # WAIT FOR CONTENT, do not sleep and hope.
            #
            # A fixed settle photographed the Cost tab mid-render: a 20 KB image of a Dash
            # spinner on an empty page, which is precisely the "photograph of a bug that is not
            # there" this file's own docstring warns about. The Cost tab builds six tables and a
            # curve and is several times slower than the rest, so any single delay is either too
            # short for it or wasteful for the other seven.
            #
            # Ready means: a table or a figure exists, and no spinner is left on the page.
            # "Some content exists" is NOT enough. Several tabs deliver their heavy part from a
            # SECOND callback: the Session tab's chart comes from the scrubber's callback and the
            # Window tab's body from its panel callback, so a check that fires on the first table
            # photographs the shell with a spinner where the chart belongs. Both did exactly that.
            #
            # So: no spinner, content present, AND the page height unchanged between two polls.
            # A tab that is still filling in is a tab that is still growing.
            page.evaluate("window.__lastHeight = -1")
            try:
                page.wait_for_function(
                    """(want) => {
                        // THE PAGE SAYS WHEN IT IS READY, so this no longer has to infer it.
                        // The pane carries data-tab and data-loading, so "the tab I asked for is
                        // finished" is a fact to read rather than a guess from page height. The
                        // old heuristic was needed because Dash offered nothing better, and it
                        // photographed the Cost tab mid-render more than once.
                        const pane = document.querySelector('main [data-tab]');
                        if (pane) {
                            return pane.dataset.tab === want
                                && pane.dataset.loading === 'false';
                        }
                        // Fallback, for a page that does not carry the signal: no spinner, some
                        // content, and a height that stopped changing between two polls. A tab
                        // still filling in is a tab still growing.
                        const spinning = document.querySelector(
                            '.dash-spinner, ._dash-loading, .dash-loading');
                        const content = document.querySelectorAll(
                            '.dash-table-container, .js-plotly-plot, table').length;
                        const h = document.documentElement.scrollHeight;
                        const settled = (h === window.__lastHeight);
                        window.__lastHeight = h;
                        return content > 0 && !spinning && settled;
                    }""",
                    arg=tab_id, timeout=90_000, polling=900)
            except Exception:                       # noqa: BLE001 - reported, not raised
                print(f"  WARNING {name}: still rendering after 90s, photographing anyway")
            page.wait_for_timeout(settle)
            # Accordions ship closed so a tab is scannable. For a screenshot the opposite is
            # wanted: a reader looking at an image cannot click, so the evidence is opened for
            # them and the image shows what the tab actually holds.
            page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
            page.wait_for_timeout(1_200)
            page.evaluate(f"window.scrollTo(0, {scroll})")
            page.wait_for_timeout(600)
            target = out_dir / f"{name}.png"
            page.screenshot(path=str(target),
                            clip={"x": 0, "y": 0, "width": VIEWPORT["width"],
                                  "height": min(height, VIEWPORT["height"])})
            written.append((name, target, what))
        browser.close()

    return written, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"dashboard to photograph [{DEFAULT_URL}]")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="directory to write into")
    ap.add_argument("--only", help="comma-separated names, e.g. summary,cost")
    ap.add_argument("--settle", type=int, default=4500, help="ms to wait after each tab click")
    args = ap.parse_args(argv)

    # Refuse a URL that is not local. These images go into a public README, and pointing this at
    # a remote host is how someone else's data ends up in one.
    if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/?$", args.url.rstrip("/") + "/"):
        if not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?", args.url):
            print(f"refusing {args.url}: this writes into a public README, so it only "
                  f"photographs a dashboard on loopback. Tunnel a remote one to localhost.")
            return 2

    try:
        written, skipped = capture(args.url, Path(args.out), args.only, args.settle)
    except Exception as exc:                        # noqa: BLE001 - report, do not traceback
        print(f"could not photograph {args.url}: {type(exc).__name__}: {exc}")
        print("is the server running?  python -m c4x.api")
        print("is playwright installed?   pip install playwright && playwright install chromium")
        return 1

    for name, target, what in written:
        size = target.stat().st_size / 1024
        print(f"  {name:14} {size:7.0f} KB  {what}")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")
    print(f"{len(written)} image(s) into {args.out}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())

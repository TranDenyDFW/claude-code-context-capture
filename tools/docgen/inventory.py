"""Walk the running dashboard and record every documentable item, with its pixel rectangle.

The point of deriving this from the LIVE PAGE rather than from the source is that a document
written from source describes what the code intends; a document written from the page describes
what a reader will actually see. Those differ exactly where documentation is most useful.

Per page (a tab, or one of the Window tab's sub-panels) it writes:

    docs/docgen/<page>.png      a full-page screenshot
    docs/docgen/<page>.json     sections, and inside each section the items to annotate

An ITEM is anything a reader could point at and ask "what is this": a stat card, one column of one
table, a chart, a control, a stated caveat. Every item carries a rectangle in page coordinates so
the annotator can draw a red box around it without anyone placing boxes by hand.

A SECTION is the band of the page an item belongs to, usually one accordion or one evidence block.
Sections exist because a full page can be 5,000 pixels tall, and a 5,000 pixel screenshot pasted
into Word is a grey smear. The document shows one section at a time.

    python tools/docgen/inventory.py --url http://127.0.0.1:8057
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "docs" / "docgen"

# The pages to walk. A Window sub-panel is reached by clicking its button after the tab, which is
# why panels are named separately rather than treated as part of the Window tab: each renders its
# own body and a reader sees one at a time.
PAGES = [
    ("summary", "tab-summary", None),
    ("sessions", "tab-sessions", None),
    ("session", "tab-session", None),
    ("compactions", "tab-compactions", None),
    ("window-composition", "tab-window", "window-panel-composition"),
    ("window-configuration", "tab-window", "window-panel-configuration"),
    ("window-conversation", "tab-window", "window-panel-conversation"),
    ("window-injected", "tab-window", "window-panel-injected"),
    ("cost", "tab-cost", None),
    ("compare", "tab-compare", None),
    ("diagnostics", "tab-diagnostics", None),
]

VIEWPORT = {"width": 1440, "height": 950}

# Collect everything in one pass in the page, because a rectangle read in a second pass can be
# stale: Dash re-renders on its refresh tick and every y coordinate moves.
COLLECT = r"""
() => {
  const abs = (el) => {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
            w: Math.round(r.width), h: Math.round(r.height)};
  };
  const text = (el) => (el ? el.textContent.trim().replace(/\s+/g, ' ') : '');
  const items = [];
  const push = (o) => { if (o.rect.w > 4 && o.rect.h > 4) items.push(o); };

  // Header controls, documented once but present on every page.
  for (const [sel, label] of [['#sel-cohort', 'Population selector'],
                              ['#sel-session', 'Session selector'],
                              ['#session-scope', 'Scope radio'],
                              ['#live-context', 'Live context readout']]) {
    const el = document.querySelector(sel);
    if (el) push({kind: 'control', key: sel, label, value: text(el).slice(0, 120), rect: abs(el)});
  }

  // Stat cards: the label is the uppercase line, the card is its parent.
  document.querySelectorAll('div').forEach((d) => {
    const st = getComputedStyle(d);
    if (st.textTransform !== 'uppercase') return;
    const label = text(d);
    if (!label || label.length > 40) return;
    const card = d.parentElement;
    if (!card || card.children.length < 2) return;
    push({kind: 'card', key: 'card:' + label, label,
          value: text(card.children[1]), sub: text(card.children[2] || null),
          rect: abs(card)});
  });

  // Tables, one item per COLUMN, because a column is the unit a reader asks about. The block
  // around the table carries its title, its stated row count and the SQL accordion.
  document.querySelectorAll('.dash-table-container').forEach((t) => {
    // Climb to the block that OWNS this table, and stop climbing the moment the candidate
    // contains a second table. Without that guard a plain DataTable, which has no SQL accordion
    // anywhere above it, walks all the way to the page root and takes the page's banner as its
    // title: four Diagnostics tables were captioned "Store-wide. Not affected by the header
    // selection." That is a wrong caption in a document made of captions.
    let block = t, hops = 0;
    while (block && block.parentElement && hops < 4) {
      const up = block.parentElement;
      if (up.querySelectorAll('.dash-table-container').length > 1) break;
      block = up; hops++;
      if (block.querySelector('details')) break;
    }
    const pre = block ? block.querySelector('details pre') : null;
    // The caption is the nearest PRECEDING text sibling, walking up until one is found.
    //
    // Not the owning block's first child: a Dash DataTable renders its own export toolbar as the
    // first thing inside its container, so that rule captioned four tables "Export". And not the
    // page's first child either, which was the previous rule and captioned them with the page
    // banner. The caption in this app is always laid out as a sibling above the table.
    let head = null, noteEl = null, node = t;
    for (let up = 0; up < 5 && node && !head; up++) {
      let sib = node.previousElementSibling;
      while (sib && !head) {
        const s = text(sib);
        // The row-count line is a NOTE, not a caption. evidence_block prints "N rows." directly
        // above its table, so the nearest sibling is that note and the caption is one further
        // back. Skipped by shape rather than by position, because only some tables have it.
        const isRowCount = /^[\d,]+ rows?\./.test(s);
        if (s && s.length > 2 && s.length < 160 && !isRowCount &&
            !sib.querySelector('.dash-table-container, .js-plotly-plot')) {
          head = sib;
          noteEl = sib.nextElementSibling === node ? null : sib.nextElementSibling;
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    const tableId = t.id || (t.closest('[id]') ? t.closest('[id]').id : null);
    push({kind: 'table', key: 'table:' + (tableId || text(head).slice(0, 30)),
          label: text(head).slice(0, 120), note: text(noteEl).slice(0, 400),
          table_id: tableId, sql: pre ? pre.textContent.trim() : null, rect: abs(t)});
    t.querySelectorAll('th .column-header-name').forEach((h) => {
      push({kind: 'column', key: 'col:' + (tableId || '') + ':' + text(h),
            label: text(h), table_id: tableId, rect: abs(h.closest('th') || h)});
    });
  });

  // Charts.
  document.querySelectorAll('.js-plotly-plot').forEach((c) => {
    const g = c.querySelector('.gtitle');
    push({kind: 'chart', key: 'chart:' + text(g).slice(0, 40), label: text(g) || '(untitled chart)',
          rect: abs(c)});
  });

  // Sliders and buttons a reader can operate.
  document.querySelectorAll('.rc-slider').forEach((s, i) => {
    const wrap = s.closest('div');
    push({kind: 'control', key: 'slider:' + i,
          label: text(wrap && wrap.previousElementSibling) || ('slider ' + (i + 1)),
          rect: abs(s)});
  });

  // Stated caveats: the muted or coloured paragraphs the app uses to qualify a number. They are
  // documentable items in their own right, because most of them exist to stop a misreading.
  document.querySelectorAll('div').forEach((d) => {
    if (d.children.length) return;
    // NOT table cells. Every cell of a DataTable is a leaf div of about this size, so without
    // this the walker reports ten cells of one table as ten separate fields. The field is the
    // COLUMN; a cell is one value in it, and the columns are collected above.
    if (d.closest('.dash-table-container')) return;
    const t = text(d);
    if (t.length < 60 || t.length > 700) return;
    const st = getComputedStyle(d);
    if (parseFloat(st.fontSize) > 13.5) return;
    push({kind: 'note', key: 'note:' + t.slice(0, 40), label: t, rect: abs(d)});
  });

  // Sections: each accordion is one, plus a synthetic one for anything above the first accordion.
  const sections = [];
  document.querySelectorAll('details').forEach((d) => {
    sections.push({key: text(d.querySelector('summary')).slice(0, 90), rect: abs(d)});
  });
  return {items, sections, pageHeight: document.documentElement.scrollHeight,
          title: document.title};
}
"""


def ready(page, settle):
    """Wait for the page to stop growing, then open every accordion.

    Same rule as tools/screenshots.py, and for the same reason: several tabs deliver their heavy
    half from a second callback, so "some content exists" is true well before the page is done.
    """
    page.evaluate("window.__h = -1")
    try:
        page.wait_for_function(
            """() => {
                const spin = document.querySelector('.dash-spinner, ._dash-loading, .dash-loading');
                const sel = '.dash-table-container, .js-plotly-plot';
                const n = document.querySelectorAll(sel).length;
                const h = document.documentElement.scrollHeight;
                const settled = h === window.__h;
                window.__h = h;
                return n > 0 && !spin && settled;
            }""", timeout=90_000, polling=900)
    except Exception:                               # noqa: BLE001 - reported by the caller
        pass
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_timeout(settle)


def walk(url, out_dir, settle=2500, only=None):
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.goto(url, wait_until="load", timeout=60_000)
        page.wait_for_timeout(9_000)

        for name, tab, panel in PAGES:
            if only and name not in only:
                continue
            page.click(f"#btn-{tab}")
            ready(page, settle)
            if panel:
                page.click(f"#{panel}")
                ready(page, settle)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(400)
            shot = out_dir / f"{name}.png"
            page.screenshot(path=str(shot), full_page=True)
            data = page.evaluate(COLLECT)
            data.update({"page": name, "tab": tab, "panel": panel,
                         "viewport": VIEWPORT, "scale": 2, "image": shot.name})
            (out_dir / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
            written.append((name, len(data["items"]), len(data["sections"]),
                            data["pageHeight"], shot.stat().st_size // 1024))
        browser.close()
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8057")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--only", help="comma-separated page names")
    ap.add_argument("--settle", type=int, default=2500)
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    try:
        written = walk(args.url, Path(args.out), args.settle, only)
    except Exception as exc:                        # noqa: BLE001 - report, do not traceback
        print(f"could not walk {args.url}: {type(exc).__name__}: {exc}")
        print("is the dashboard running?  python app.py")
        return 1

    total = 0
    for name, items, sections, height, kb in written:
        total += items
        print(f"  {name:22} {items:4} items  {sections:2} sections  {height:5}px  {kb:5} KB")
    print(f"{total} items across {len(written)} pages into {args.out}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())

"""Draw the red boxes and numbered badges, from the rectangles the inventory measured.

Nothing here decides WHERE a box goes. The inventory read every rectangle off the live DOM, so a
box lands on the element because the browser said that is where the element is. Hand-placed boxes
drift the moment a column is renamed or a chart gets taller, and a document whose arrows point at
the wrong thing is worse than one with no arrows.

Bands, not whole pages. The Cost tab is 5,163 pixels tall; pasted whole into Word it is a grey
smear. Items are grouped into bands by what they belong to (one table, one row of cards, one
chart), each band is cropped out of the full-page screenshot, and the numbering restarts nowhere:
an item keeps one number across the whole page so the notes can be read in one list.

    python tools/docgen/annotate.py            # every page the inventory wrote
    python tools/docgen/annotate.py --only cost
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCGEN = ROOT / "docs" / "docgen"

RED = (232, 62, 62)
BADGE_TEXT = (255, 255, 255)
PAGE_BG = (13, 17, 23)         # the app's own background, so the gutter is invisible
GUTTER = 34                    # unscaled pixels of margin added on the left, for the badges
PAD = 26                       # pixels of page kept around a band, so a box is never flush


def font(size):
    from PIL import ImageFont
    for name in ("consolab.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:                           # noqa: BLE001 - fall through to the default
            continue
    return ImageFont.load_default()


def bands(items, page_height, gap=90):
    """Group items into vertical bands a reader can look at one at a time.

    Anchored on the big things (a table, a chart, a row of cards) and then absorbing every smaller
    item that overlaps them vertically, because a caveat printed under a table belongs to that
    table's picture rather than to a band of its own.
    """
    anchors = [i for i in items if i["kind"] in ("table", "chart")]
    anchors.sort(key=lambda i: i["rect"]["y"])
    out = []
    for a in anchors:
        top, bottom = a["rect"]["y"], a["rect"]["y"] + a["rect"]["h"]
        out.append({"top": top, "bottom": bottom, "items": []})
    # Cards and controls cluster into their own bands where no table covers them.
    loose = [i for i in items if i["kind"] in ("card", "control")]
    loose.sort(key=lambda i: i["rect"]["y"])
    run = []
    for i in loose:
        if run and i["rect"]["y"] - (run[-1]["rect"]["y"] + run[-1]["rect"]["h"]) > gap:
            out.append({"top": run[0]["rect"]["y"],
                        "bottom": max(r["rect"]["y"] + r["rect"]["h"] for r in run), "items": []})
            run = []
        run.append(i)
    if run:
        out.append({"top": run[0]["rect"]["y"],
                    "bottom": max(r["rect"]["y"] + r["rect"]["h"] for r in run), "items": []})

    out.sort(key=lambda b: b["top"])
    # Merge bands that overlap, so one table and the cards sitting on it are one picture.
    merged = []
    for b in out:
        if merged and b["top"] <= merged[-1]["bottom"] + 12:
            merged[-1]["bottom"] = max(merged[-1]["bottom"], b["bottom"])
        else:
            merged.append(dict(b))

    # Every item joins the band it sits inside; anything left over gets a band of its own so it
    # cannot be silently dropped from the document.
    placed = set()
    for b in merged:
        for n, i in enumerate(items):
            mid = i["rect"]["y"] + i["rect"]["h"] / 2
            if b["top"] - PAD <= mid <= b["bottom"] + PAD:
                b["items"].append(n)
                placed.add(n)
    orphans = [n for n in range(len(items)) if n not in placed]
    for n in orphans:
        r = items[n]["rect"]
        merged.append({"top": r["y"], "bottom": r["y"] + r["h"], "items": [n]})
    merged.sort(key=lambda b: b["top"])
    for b in merged:
        b["top"] = max(0, b["top"] - PAD)
        b["bottom"] = min(page_height, b["bottom"] + PAD)
    return [b for b in merged if b["items"]]


def draw(page_json, out_dir):
    from PIL import Image, ImageDraw

    data = json.loads(page_json.read_text(encoding="utf-8"))
    src = page_json.with_suffix(".png")
    if not src.exists():
        return None
    scale = data.get("scale", 1)
    image = Image.open(src).convert("RGB")
    items = data["items"]

    # Numbered in READING ORDER over the whole page, top to bottom then left to right, so the
    # document's list runs down the page the way an eye does.
    order = sorted(range(len(items)),
                   key=lambda n: (items[n]["rect"]["y"] // 24, items[n]["rect"]["x"]))
    number = {n: k + 1 for k, n in enumerate(order)}

    produced = []
    for bi, band in enumerate(bands(items, data["pageHeight"])):
        top, bottom = int(band["top"] * scale), int(band["bottom"] * scale)
        raw = image.crop((0, top, image.width, min(bottom, image.height)))
        # A LEFT GUTTER, so a badge always has somewhere to sit outside its box.
        #
        # The page starts its content at x=18, which is narrower than a badge, so items at the
        # left margin had to fall back to a badge INSIDE the box, covering the first characters of
        # the thing being labelled. Widening the canvas costs nothing and makes every badge legible
        # by the same rule instead of most of them by one rule and the rest by another.
        crop = Image.new("RGB", (raw.width + GUTTER * scale, raw.height), PAGE_BG)
        crop.paste(raw, (GUTTER * scale, 0))
        d = ImageDraw.Draw(crop)
        big = font(max(19, int(15 * scale)))
        for n in sorted(band["items"], key=lambda n: number[n]):
            r = items[n]["rect"]
            x0 = int(r["x"] * scale) + GUTTER * scale
            y0 = int(r["y"] * scale) - top
            x1, y1 = x0 + int(r["w"] * scale), y0 + int(r["h"] * scale)
            d.rectangle([x0, y0, x1, y1], outline=RED, width=max(2, scale))
            # The badge sits INSIDE the top-left corner, because a badge outside the box overlaps
            # whatever is above it and on a dense table that is another box's number.
            label = str(number[n])
            tw = d.textlength(label, font=big)
            bw, bh = int(tw) + 14 * scale, int(13 * scale) + 10
            # OUTSIDE the box where there is margin, inside only when there is not.
            #
            # A badge inside the top-left corner sits on top of the first characters of whatever
            # it labels, which on a table of prose hides the start of every cell. That was the
            # first version and it made the annotated image less readable than the plain one.
            bx = x0 - bw - 6 if x0 - bw - 6 >= 0 else x0
            by = y0 if y0 >= 0 else 0
            d.rectangle([bx, by, bx + bw, by + bh], fill=RED)
            d.text((bx + 7 * scale, by + 4), label, fill=BADGE_TEXT, font=big)
        name = f"{data['page']}-band{bi + 1}.png"
        crop.save(out_dir / name)
        produced.append({"image": name, "top": band["top"], "bottom": band["bottom"],
                         "items": [{"n": number[n], **items[n]} for n in
                                   sorted(band["items"], key=lambda n: number[n])]})

    out = {"page": data["page"], "tab": data["tab"], "panel": data["panel"],
           "bands": produced, "numbered": len(items)}
    (out_dir / f"{data['page']}.bands.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DOCGEN))
    ap.add_argument("--only", help="comma-separated page names")
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    d = Path(args.dir)
    pages = sorted(p for p in d.glob("*.json")
                   if not p.name.endswith((".bands.json", ".spec.json"))
                   and (not only or p.stem in only))
    if not pages:
        print(f"no inventory in {d}; run tools/docgen/inventory.py first")
        return 1
    total_bands = total_items = 0
    for p in pages:
        out = draw(p, d)
        if not out:
            print(f"  {p.stem:22} SKIPPED, no screenshot beside it")
            continue
        total_bands += len(out["bands"])
        total_items += out["numbered"]
        print(f"  {out['page']:22} {len(out['bands']):2} bands  {out['numbered']:3} items numbered")
    print(f"{total_bands} band images, {total_items} numbered items")
    return 0


if __name__ == "__main__":
    sys.exit(main())

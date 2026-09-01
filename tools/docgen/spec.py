"""Merge the numbering into the facts, producing the one file a writer needs per page.

The inventory measured the items, the annotator numbered them, and the enricher attached what the
repository knows about each. This joins the three into `<page>.spec.json`, which is what the note
writers read: an item's number, what it is, where it sits, and every fact already established
about it. Rectangles are dropped, because a writer does not need pixels and they are most of the
file's weight.

    python tools/docgen/spec.py
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCGEN = ROOT / "docs" / "docgen"


def build(page, out_dir):
    inv = json.loads((out_dir / f"{page}.json").read_text(encoding="utf-8"))
    bands = json.loads((out_dir / f"{page}.bands.json").read_text(encoding="utf-8"))

    numbered = {}
    for band in bands["bands"]:
        for item in band["items"]:
            numbered[item["key"]] = (item["n"], band["image"])

    items = []
    for item in inv["items"]:
        n, image = numbered.get(item["key"], (None, None))
        if n is None:
            continue
        slim = {k: v for k, v in item.items() if k != "rect"}
        slim["n"] = n
        slim["band_image"] = image
        items.append(slim)
    items.sort(key=lambda i: i["n"])

    spec = {
        "page": page, "tab": inv["tab"], "panel": inv["panel"],
        "item_count": len(items),
        "bands": [{"image": b["image"], "items": [i["n"] for i in b["items"]]}
                  for b in bands["bands"]],
        "items": items,
    }
    path = out_dir / f"{page}.spec.json"
    path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
    return len(items), path.stat().st_size // 1024


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DOCGEN))
    args = ap.parse_args(argv)
    d = Path(args.dir)
    pages = sorted(p.stem for p in d.glob("*.bands.json"))
    pages = [p.replace(".bands", "") for p in pages]
    if not pages:
        print(f"no annotated pages in {d}; run inventory then annotate first")
        return 1
    total = 0
    for page in pages:
        n, kb = build(page, d)
        total += n
        print(f"  {page:22} {n:3} items  {kb:3} KB")
    print(f"{total} items across {len(pages)} page specs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

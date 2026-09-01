"""Attach the facts each item's notes must be built from, so nothing is written from memory.

The inventory knows what a reader SEES. This adds what the repository KNOWS about it:

  columns   the tooltip text from c4x/theme.py COLUMN_HELP, plus the SQL of the table the column
            belongs to, so "how was this derived" is answered by the query rather than by a guess
  tables    the row count the page states, and the SQL already captured from its accordion
  cards     the source file and function that builds it, located by searching for the card's label
  charts    the same, located by the chart's title

Where a fact cannot be found it is recorded as null rather than invented, and the writer is told to
say "not established" instead of filling the gap. A documentation generator that guesses is worse
than one that leaves a blank, because a blank is visibly a blank.

    python tools/docgen/enrich.py
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCGEN = ROOT / "docs" / "docgen"
SEARCH_DIRS = [ROOT / "c4x", ROOT / "tools", ROOT / "hooks"]


def column_help():
    """COLUMN_HELP as data, read from the module rather than re-parsed out of the source."""
    sys.path.insert(0, str(ROOT))
    try:
        from c4x.theme import COLUMN_HELP
        return dict(COLUMN_HELP)
    except Exception as exc:                        # noqa: BLE001 - recorded, not raised
        print(f"  WARNING could not import COLUMN_HELP: {exc}")
        return {}


def source_index():
    """Every source line, so a label can be traced to the file and function that emits it."""
    index = []
    for d in SEARCH_DIRS:
        for path in sorted(d.rglob("*")):
            if path.suffix not in (".py", ".mjs") or "__pycache__" in str(path):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:                       # noqa: BLE001 - unreadable file is not fatal
                continue
            index.append((path.relative_to(ROOT).as_posix(), lines))
    return index


def locate(index, needle, limit=3):
    """Where a literal appears in the source, with the enclosing def.

    Literal, not fuzzy. A fuzzy match would find something for every needle and the resulting
    citation would be worse than none: it would look sourced.
    """
    if not needle or len(needle) < 4:
        return []
    hits = []
    for rel, lines in index:
        for n, line in enumerate(lines):
            if needle in line:
                owner = None
                for back in range(n, max(-1, n - 400), -1):
                    m = re.match(r"\s*(?:async )?(?:def|function|export function) (\w+)",
                                 lines[back])
                    if m:
                        owner = m.group(1)
                        break
                hits.append({"file": rel, "line": n + 1, "function": owner,
                             "text": line.strip()[:200]})
                if len(hits) >= limit:
                    return hits
    return hits


def enrich(page_json, helps, index):
    data = json.loads(page_json.read_text(encoding="utf-8"))
    sql_by_table = {i.get("table_id"): i.get("sql")
                    for i in data["items"] if i["kind"] == "table"}
    note_by_table = {i.get("table_id"): i.get("note")
                     for i in data["items"] if i["kind"] == "table"}

    for item in data["items"]:
        found = {}
        if item["kind"] == "column":
            found["column_help"] = helps.get(item["label"])
            found["table_sql"] = sql_by_table.get(item.get("table_id"))
            found["table_note"] = note_by_table.get(item.get("table_id"))
            found["source"] = locate(index, f'"{item["label"]}"')
        elif item["kind"] == "table":
            found["source"] = locate(index, item["label"][:40]) if item.get("label") else []
        elif item["kind"] == "card":
            found["source"] = locate(index, f'"{item["label"]}"')
        elif item["kind"] == "chart":
            found["source"] = locate(index, item["label"][:38])
        elif item["kind"] == "note":
            found["source"] = locate(index, item["label"][:45])
        elif item["kind"] == "control":
            key = item.get("key", "").lstrip("#")
            found["source"] = locate(index, f'"{key}"')
        item["facts"] = found

    data["enriched"] = True
    page_json.write_text(json.dumps(data, indent=1), encoding="utf-8")
    traced = sum(1 for i in data["items"] if i.get("facts", {}).get("source"))
    helped = sum(1 for i in data["items"] if i.get("facts", {}).get("column_help"))
    sqled = sum(1 for i in data["items"] if i.get("facts", {}).get("table_sql")
                or (i["kind"] == "table" and i.get("sql")))
    return len(data["items"]), traced, helped, sqled


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DOCGEN))
    ap.add_argument("--only", help="comma-separated page names")
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",")} if args.only else None

    helps = column_help()
    index = source_index()
    print(f"  {len(helps)} column-help entries, {len(index)} source files indexed")
    d = Path(args.dir)
    # Both derived kinds are excluded. Once spec.py has run, "*.json" also matches
    # <page>.spec.json, and enriching a spec file rewrites it with an inventory's shape.
    pages = sorted(p for p in d.glob("*.json")
                   if not p.name.endswith((".bands.json", ".spec.json"))
                   and (not only or p.stem in only))
    if not pages:
        print(f"no inventory in {d}")
        return 1
    for p in pages:
        n, traced, helped, sqled = enrich(p, helps, index)
        print(f"  {p.stem:22} {n:3} items  {traced:3} traced to source  "
              f"{helped:3} with column help  {sqled:3} with SQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())

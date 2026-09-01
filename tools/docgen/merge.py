"""Apply repaired notes over the originals, and say exactly what changed.

A repair pass returns only the items it was asked to fix. This lays those over the full set and
reports the count, so "the repair ran" is a number rather than an assumption. An item that was
flagged and did NOT come back repaired is reported by name, because that is the case most likely
to be missed: a silent no-op looks identical to a successful merge.

    python tools/docgen/merge.py --repaired docs/docgen/repaired.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCGEN = ROOT / "docs" / "docgen"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes", default=str(DOCGEN / "notes.json"))
    ap.add_argument("--repaired", default=str(DOCGEN / "repaired.json"))
    ap.add_argument("--flagged", default=str(DOCGEN / "unsupported.json"))
    ap.add_argument("--out", default=str(DOCGEN / "notes.json"))
    args = ap.parse_args(argv)

    notes = json.loads(Path(args.notes).read_text(encoding="utf-8"))
    repaired = json.loads(Path(args.repaired).read_text(encoding="utf-8"))
    flagged = {}
    if Path(args.flagged).exists():
        raw = json.loads(Path(args.flagged).read_text(encoding="utf-8"))
        flagged = {page: {x["n"] for x in items} for page, items in raw.items()}

    fixes = {r["page"]: {int(x["n"]): x for x in r["notes"]} for r in repaired}
    applied = missed = 0
    for page_notes in notes:
        page = page_notes["page"]
        page_fixes = fixes.get(page, {})
        for i, note in enumerate(page_notes["notes"]):
            fix = page_fixes.get(int(note["n"]))
            if fix:
                page_notes["notes"][i] = {**note, **fix}
                applied += 1
        still = sorted(flagged.get(page, set()) - set(page_fixes))
        if still:
            missed += len(still)
            print(f"  {page:24} {len(page_fixes):3} applied   NOT repaired: {still}")
        else:
            print(f"  {page:24} {len(page_fixes):3} applied")

    Path(args.out).write_text(json.dumps(notes, indent=1), encoding="utf-8")
    print(f"{applied} note(s) replaced, {missed} flagged item(s) came back unrepaired")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Refuse notes that quote things the spec does not contain.

A generated document is only worth reading if its specifics are real, and specifics are exactly
what a language model will supply when it has none. The first run produced 51 fabricated claims out
of 315 notes: invented table ids, invented dollar and token figures, and descriptions of what "the
screenshot shows" written by an agent that could not see one.

An instruction in a prompt did not stop that. This does, because it is arithmetic:

  ids        every `table-xxxxxxxx` named in a note must appear in that page's spec
  paths      every `c4x/...`, `tools/...` or `hooks/...` file named must exist on disk
  figures    every long digit group (1,234,567 or 863.4k style) must appear in the spec, since a
             note has no other legitimate source for one
  screenshot phrases like "the screenshot shows" are refused outright: the writer cannot see it,
             and a live value is stale the moment the store updates anyway

Exit code 1 when anything is unsupported, so this can gate the build rather than decorate it.

    python tools/docgen/check_notes.py
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCGEN = ROOT / "docs" / "docgen"

# A Dash auto-id is `table-` plus a random alphanumeric run, so it always carries a digit.
# Requiring one stops the gate flagging ordinary hyphenated English: "table-neutral" and
# "table-specific" were both reported as fabricated ids on the first run.
TABLE_ID = re.compile(r"\btable-(?=[a-z0-9]*\d)[a-z0-9]{6,}\b")
PATHS = re.compile(r"\b((?:c4x|tools|hooks|tests)/[\w./-]+\.(?:py|mjs|json))\b")
# Four or more digits, with or without separators, and the 863.4k / 1.68B shapes.
FIGURE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d{4,}(?:\.\d+)?\b|\b\d+(?:\.\d+)?[kMB]\b")
SEEING = re.compile(r"\b(?:the )?screenshot (?:shows|reads|displays)|as (?:shown|seen) in the "
                    r"(?:image|screenshot)|in the captured (?:screenshot|image)", re.I)
# "harvest.mjs:1357", "line 1096", "lines 44-52": a pointer into a file, not a measurement.
CITATION = re.compile(r"(?:\.(?:py|mjs|json):\d+(?:-\d+)?)|(?:\blines?\s+\d+(?:\s*[-to]+\s*\d+)?)",
                      re.I)

FIELDS = ("purpose", "derivation", "sql", "issues", "recommendations", "other")


def spec_corpus(spec):
    """Every string the spec establishes, as one haystack a claim can be checked against."""
    return json.dumps(spec, ensure_ascii=False)


def source_corpus():
    """The whole source tree as one string, so a CONSTANT counts as supported.

    Checking figures against the spec alone was too blunt and would have made this gate useless:
    it flagged 967000 (the compaction threshold), 1000000 (the window) and 20000 (max output) as
    fabrications, when each is a real constant that legitimately appears only in the source. What
    is actually fabricated is a figure that exists in NEITHER: a live reading like "863.4k" that
    the writer could not have seen and that is stale the moment the store updates.
    """
    parts = []
    for d in (ROOT / "c4x", ROOT / "tools", ROOT / "hooks"):
        for path in d.rglob("*"):
            # NOT this generator's own source. The corpus is meant to be the APPLICATION, and
            # tools/docgen is not part of it. Including it made the gate certify itself: the
            # docstring above cites "863.4k" as the example fabrication, so every note repeating
            # that exact invented figure scored as supported, and the gate reported zero
            # occurrences of the one fabrication it was written to catch.
            if "docgen" in path.parts:
                continue
            if path.suffix in (".py", ".mjs", ".json") and "__pycache__" not in str(path):
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
                except Exception:                   # noqa: BLE001 - unreadable file is not fatal
                    pass
    return "\n".join(parts)


def normalise(fig):
    """The forms a figure could legitimately be written in, so 1,000,000 matches 1000000."""
    bare = fig.replace(",", "")
    out = {fig, bare}
    if bare.replace(".", "").isdigit() and "." not in bare:
        try:
            out.add(f"{int(bare):,}")
        except ValueError:
            pass
    return out


def check_page(spec, notes, source=""):
    corpus = spec_corpus(spec) + source
    known_ids = {i.get("table_id") for i in spec["items"] if i.get("table_id")}
    problems = []
    for note in notes:
        n = note.get("n")
        blob = " ".join(str(note.get(f) or "") for f in FIELDS)

        for tid in set(TABLE_ID.findall(blob)):
            if tid not in known_ids:
                problems.append((n, "fabricated-id", tid))

        for path in set(PATHS.findall(blob)):
            if not (ROOT / path).exists():
                problems.append((n, "missing-path", path))

        # A LINE NUMBER is a citation, not a quantity. "harvest.mjs:1357" and "line 1096" both
        # matched the figure pattern and were reported as unsupported, though both pointed at
        # exactly the line they claimed. Stripped before the figure check rather than whitelisted
        # after it, so the count reflects claims about DATA and nothing else.
        blob = CITATION.sub(" ", blob)
        for fig in set(FIGURE.findall(blob)):
            # Supported if the spec captured it OR the source defines it, in any of the forms a
            # writer might spell it. Anything else has no source at all and is the fabrication
            # this gate exists to catch.
            if not any(form in corpus for form in normalise(fig)):
                problems.append((n, "unsupported-figure", fig))

        if SEEING.search(blob):
            problems.append((n, "describes-an-image", SEEING.search(blob).group(0)[:60]))
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DOCGEN))
    ap.add_argument("--notes", default=str(DOCGEN / "notes.json"))
    ap.add_argument("--out", default=str(DOCGEN / "unsupported.json"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    d = Path(args.dir)
    source = source_corpus()
    notes_all = json.loads(Path(args.notes).read_text(encoding="utf-8"))
    report, total = {}, 0
    for page_notes in sorted(notes_all, key=lambda x: x["page"]):
        page = page_notes["page"]
        spec_file = d / f"{page}.spec.json"
        if not spec_file.exists():
            continue
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        problems = check_page(spec, page_notes["notes"], source)
        total += len(problems)
        report[page] = [{"n": n, "kind": k, "detail": v} for n, k, v in problems]
        if not args.quiet:
            kinds = {}
            for _n, k, _v in problems:
                kinds[k] = kinds.get(k, 0) + 1
            print(f"  {page:24} {len(problems):4} unsupported  {kinds or ''}")

    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"{total} unsupported claim(s) across {len(report)} pages -> {args.out}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

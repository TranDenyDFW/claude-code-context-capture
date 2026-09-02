"""Generate constraints-ci.txt from a real resolve, and check that it still covers what CI installs.

WHY THIS EXISTS AS A COMMITTED TOOL rather than a command in a comment.

`constraints-ci.txt` pins the exact versions CI installs, so a run six months from now resolves the
graph today's green run resolved. Its header used to carry the pip command that produced it, with an
instruction not to hand-edit. That is not the same as a check, and the difference cost something
real: `mypy` was added to `requirements-dev.txt` in the same commit that added the constraints file,
with no pin, and stayed unpinned through the commit that made the mypy step BLOCKING. For weeks the
one tool whose output could fail the job was the one tool free to change under CI, and nothing said
so. An independent review found it, not the file's own instructions.

So the generation lives here, and so does a check that runs in the suite with no network:

    python tools/make_constraints.py --self-test    # offline: is every direct requirement pinned?
    python tools/make_constraints.py --regenerate   # network: re-resolve and rewrite the file

WHAT THE OFFLINE CHECK CAN AND CANNOT SEE. It reads the requirements files and the constraints file
and asks whether every package NAMED in the former carries an exact pin in the latter. That is the
gap that actually opened. It cannot see a new TRANSITIVE arriving from an existing dependency,
because knowing the transitive set requires a resolve, and a suite step that reaches the network
fails for reasons nobody chose. `--regenerate` covers that case and is run deliberately.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS = ROOT / "constraints-ci.txt"
REQUIREMENTS = ["requirements.txt", "requirements-dev.txt"]

# The interpreter CI resolves for, which is not necessarily the one running this script. Stated as a
# constant because a resolve for a different version silently produces a different graph.
CI_PYTHON = "3.12"


def named_requirements():
    """Every package NAMED in the requirements files, lowercased, with the raw line kept.

    Comments, blank lines and environment markers are stripped. The name is whatever precedes the
    first version operator, so `mypy>=1.11.0` and `dash==4.4.1` both yield their bare name.
    """
    found = {}
    for name in REQUIREMENTS:
        path = ROOT / name
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#")[0].strip()
            if not line:
                continue
            package = re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip().lower()
            if package:
                found.setdefault(package, (name, line))
    return found


def pins(text=None):
    """Every pin in the constraints file, as {name: version}. Only exact `==` pins count."""
    body = text if text is not None else CONSTRAINTS.read_text(encoding="utf-8")
    out = {}
    for raw in body.splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if "==" not in line:
            # Recorded as a non-pin rather than skipped, so a range in a constraints file is a
            # finding instead of an absence.
            out[re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].strip().lower()] = None
            continue
        name, version = line.split("==", 1)
        out[name.strip().lower()] = version.strip()
    return out


def uncovered(text=None):
    """Direct requirements with no exact pin. Empty means the file covers what it claims to."""
    have = pins(text)
    return sorted(name for name in named_requirements() if have.get(name) is None)


def resolve():
    """Ask pip what CI would install. Needs the network; used by --regenerate only."""
    with tempfile.TemporaryDirectory() as target:
        report = Path(target) / "resolve.json"
        command = [sys.executable, "-m", "pip", "install", "--dry-run", "--report", str(report),
                   "--python-version", CI_PYTHON, "--only-binary=:all:", "--target",
                   str(Path(target) / "site"), "-q"]
        for name in REQUIREMENTS:
            command += ["-r", str(ROOT / name)]
        run = subprocess.run(command, capture_output=True, text=True)
        if run.returncode != 0:
            print(run.stderr.strip()[-800:])
            raise SystemExit(f"pip could not resolve (exit {run.returncode})")
        data = json.loads(report.read_text(encoding="utf-8"))
    return sorted((i["metadata"]["name"].lower(), i["metadata"]["version"])
                  for i in data["install"])


def render(rows):
    """The file body, header included, so the counts in the header cannot drift from the pins."""
    direct = set(named_requirements())
    covered = sum(1 for name, _ in rows if name in direct)
    header = f'''# The exact versions CI installs. NOT the public requirements.
#
# requirements.txt and requirements-dev.txt state lower bounds on purpose: someone installing this
# to look at their own context should get a working modern set, and pinning them would make this
# repo dictate the versions of a dashboard it does not own. But a lower bound means a CI run six
# months from now resolves a different graph than today's green one, and when it goes red the
# question "did the code change or did a dependency?" has no answer.
#
# So CI installs with `-c constraints-ci.txt`, which pins everything including transitives, and the
# published requirements stay flexible. A `-c` file constrains what IS installed; it does not
# install anything itself, which is why a platform-specific entry below is harmless elsewhere.
#
# GENERATED. Do not hand-edit: run `python tools/make_constraints.py --regenerate`, which re-runs
# the resolve and rewrites this file whole. Editing one line turns the set from a resolve into a
# guess, and the guess will be wrong in a way nothing here can detect.
#
# `python tools/make_constraints.py --self-test` runs in the suite and answers the narrower
# question offline: does every package NAMED in the requirements files carry an exact pin here? It
# was added because mypy did not, for as long as the mypy step was non-blocking and then for the
# commit that made it blocking, which meant the one tool that could fail the job was the one tool
# free to change underneath it.
#
# Resolved for Python {CI_PYTHON}, matching the workflow's setup-python. Environment markers
# come from the machine that ran the resolve, so a platform-specific package can appear that
# another platform never installs; CI is the authority on whether these pins are satisfiable,
# and a wrong pin fails the install loudly rather than being quietly ignored.
#
# {len(rows)} packages, of which {covered} are named in the requirements files.
'''
    lines = [header]
    for name, version in rows:
        lines.append(f"{name}=={version}{'  # direct' if name in direct else ''}")
    return "\n".join(lines) + "\n"


def regenerate():
    rows = resolve()
    CONSTRAINTS.write_text(render(rows), encoding="utf-8", newline="\n")
    missing = uncovered()
    print(f"constraints-ci.txt written: {len(rows)} pins")
    print("direct requirements still unpinned:", missing or "none")
    return 1 if missing else 0


def self_test():
    """Offline. The question is coverage, and the last case is the one that makes it a gate."""
    have = pins()
    direct = named_requirements()

    ranged = sorted(name for name, version in have.items() if version is None)
    # A constraints file missing a pin it should have. Built in memory, because a check that only
    # ever sees the real file cannot tell "correct" from "incapable of noticing".
    without_mypy = "\n".join(
        line for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines()
        if not line.lower().startswith("mypy=="))

    cases = [
        ("every package named in the requirements files carries a pin", uncovered() == []),
        ("every pin is exact, none is a range", ranged == []),
        ("the requirements files were actually read", len(direct) > 0),
        ("the constraints file was actually read", len(have) > 10),
        # The must-fail case. Remove one real pin and the check has to notice, or a green result
        # from it means nothing at all.
        ("a missing pin IS detected, so a pass is worth something",
         "mypy" in uncovered(without_mypy)),
        ("the header's package count matches the pins present",
         _header_count() == len([v for v in have.values() if v is not None])),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    if uncovered():
        print(f"  unpinned: {', '.join(uncovered())}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


def _header_count():
    """The count the header states, so a hand-edit that changes the pins is caught by the header."""
    match = re.search(r"^# (\d+) packages, of which",
                      CONSTRAINTS.read_text(encoding="utf-8"), re.MULTILINE)
    return int(match.group(1)) if match else -1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="offline coverage check, the one the suite runs")
    ap.add_argument("--regenerate", action="store_true",
                    help="re-resolve against the network and rewrite constraints-ci.txt")
    args = ap.parse_args(argv)
    if args.regenerate:
        return regenerate()
    return self_test()


if __name__ == "__main__":
    sys.exit(main())

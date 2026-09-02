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

WHAT THE OFFLINE CHECK SEES. Every package NAMED in the requirements files carries an exact pin
here; no pin is a range; every pin SATISFIES the floor its own requirement states; and the header's
stated count matches the pins present. Names are compared PEP 503 normalised, because a resolve
emits `mypy_extensions` where a requirements file would write `mypy-extensions`, and comparing those
as raw strings reports a pinned package as unpinned.

WHAT IT CANNOT SEE, named in full because a review found this section naming one of four:

1. A NEW TRANSITIVE arriving from an existing dependency. Knowing the transitive set requires a
   resolve, a resolve requires the network, and a suite step that reaches the network fails for
   reasons nobody chose. `--regenerate` covers this and is run deliberately.
2. A pin to a version that DOES NOT EXIST on the index. `ruff==999.999.999` satisfies every offline
   check here and fails at install time in CI, loudly. That is the tradeoff taken, not an oversight.
3. Whether the pinned set is mutually CONSISTENT. Two pins can each satisfy their own floor and
   still be unresolvable together; only a resolver can answer that.
4. Anything about packages no requirements file names. A transitive pinned to a broken version is
   invisible here, for the same reason as 1.

Points 2 and 3 are why CI installing with `-c` is the authority, and why a wrong pin is a failed
install rather than a silently wrong version.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
CONSTRAINTS = ROOT / "constraints-ci.txt"
REQUIREMENTS = ["requirements.txt", "requirements-dev.txt"]

# The interpreter CI resolves for, which is not necessarily the one running this script. Stated as a
# constant because a resolve for a different version silently produces a different graph.
CI_PYTHON = "3.12"


def normalise(name):
    """PEP 503. `mypy_extensions`, `mypy-extensions` and `Mypy.Extensions` are one package.

    Not cosmetic: the resolve emits underscores for several packages in this graph, and comparing
    raw strings reported a pinned package as unpinned. A review proved it by respelling one.
    """
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def named_requirements():
    """Every requirement NAMED in the requirements files, as {normalised name: Requirement}."""
    found = {}
    for name in REQUIREMENTS:
        for raw in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = raw.split("#")[0].strip()
            if not line:
                continue
            requirement = Requirement(line)
            found.setdefault(normalise(requirement.name), requirement)
    return found


def pins(text=None):
    """Every pin in the constraints file, as {normalised name: version}. Only `==` counts."""
    body = text if text is not None else CONSTRAINTS.read_text(encoding="utf-8")
    out = {}
    for raw in body.splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        if "==" not in line:
            # Recorded as a non-pin rather than skipped, so a range in a constraints file is a
            # finding instead of an absence.
            out[normalise(re.split(r"[<>=!~;\[]", line, maxsplit=1)[0])] = None
            continue
        name, version = line.split("==", 1)
        out[normalise(name)] = version.strip()
    return out


def uncovered(text=None):
    """Direct requirements with no exact pin. Empty means the file covers what it claims to."""
    have = pins(text)
    return sorted(name for name in named_requirements() if have.get(name) is None)


def contradicted(text=None):
    """Pins that do NOT satisfy the floor their own requirement states.

    The gap this closes: `mypy==1.0.0` against `mypy>=1.11.0` passed every earlier check here. It is
    pinned, exactly, and to a version the project says is too old. pip refuses that pair at install
    time, so CI catches it eventually; catching it offline names the file that is wrong.
    """
    have = pins(text)
    bad = []
    for name, requirement in named_requirements().items():
        version = have.get(name)
        if version is None or not str(requirement.specifier):
            continue
        try:
            if not requirement.specifier.contains(Version(version), prereleases=True):
                bad.append(f"{name}=={version} does not satisfy {requirement.specifier}")
        except Exception as exc:                    # an unreadable version is itself a finding
            bad.append(f"{name}=={version} is not a readable version ({exc})")
    return sorted(bad)


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
    covered = sum(1 for name, _ in rows if normalise(name) in direct)
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
# `python tools/make_constraints.py --self-test` runs in the suite and answers offline what it can:
# every package named in the requirements files is pinned, every pin satisfies that package's own
# floor, and the count below matches the pins present. Its docstring names the four things it
# cannot see. It exists because mypy sat unpinned here from the commit that created this file
# through the commit that made the mypy step blocking, which meant the one tool that could fail the
# job was the one tool free to change underneath it.
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
        lines.append(f"{name}=={version}{'  # direct' if normalise(name) in direct else ''}")
    return "\n".join(lines) + "\n"


def regenerate():
    """Rewrite the file, PRESERVING its line endings.

    The newline matters on Windows. Writing LF into a CRLF checkout rewrites every line's bytes, so
    `git diff` shows the whole file and says nothing about whether any pin actually moved, which is
    the one question a regenerate needs to answer.
    """
    existing = CONSTRAINTS.read_text(encoding="utf-8", newline="") if CONSTRAINTS.exists() else ""
    newline = "\r\n" if "\r\n" in existing else "\n"
    rows = resolve()
    CONSTRAINTS.write_text(render(rows), encoding="utf-8", newline=newline)
    missing, wrong = uncovered(), contradicted()
    print(f"constraints-ci.txt written: {len(rows)} pins, line endings {newline!r}")
    print("direct requirements still unpinned:", missing or "none")
    print("pins that do not satisfy their own floor:", wrong or "none")
    return 1 if (missing or wrong) else 0


def self_test():
    """Offline. Every case that matters is paired with a planted failure of the same kind."""
    have = pins()
    direct = named_requirements()
    real = CONSTRAINTS.read_text(encoding="utf-8")
    ranged = sorted(name for name, version in have.items() if version is None)

    def is_pin_for(line, package):
        return "==" in line and normalise(line.split("==")[0]) == normalise(package)

    def without(package):
        """The file with one pin removed, in memory. Never touches disk."""
        return "\n".join(line for line in real.splitlines() if not is_pin_for(line, package))

    def downgraded(package, version):
        """The file with one pin moved below its floor, in memory."""
        return "\n".join(f"{package}=={version}" if is_pin_for(line, package) else line
                         for line in real.splitlines())

    def respelled(package):
        """The file with one pin written in the other PEP 503 spelling, in memory."""
        flipped = (package.replace("-", "_") if "-" in package else package.replace("_", "-"))
        return "\n".join(
            f"{flipped}=={have[normalise(package)]}" if is_pin_for(line, package) else line
            for line in real.splitlines())

    cases = [
        ("every package named in the requirements files carries a pin", uncovered() == []),
        ("every pin is exact, none is a range", ranged == []),
        ("every pin satisfies the floor its own requirement states", contradicted() == []),
        ("the requirements files were actually read", len(direct) > 0),
        ("the constraints file was actually read", len(have) > 10),
        ("the header's package count matches the pins present",
         _header_count() == len([v for v in have.values() if v is not None])),
        # Planted failures. Without these, a green result says only that the check ran.
        ("a REMOVED pin is detected", "mypy" in uncovered(without("mypy"))),
        ("a pin BELOW its own floor is detected",
         any("mypy" in line for line in contradicted(downgraded("mypy", "1.0.0")))),
        # The other direction, which matters as much: a difference that is NOT a defect must not be
        # reported as one. This spelling flip was a false failure until names were normalised.
        ("a PEP 503 respelling is NOT reported as missing",
         uncovered(respelled("python-multipart")) == []),
    ]
    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    for line in uncovered():
        print(f"  unpinned: {line}")
    for line in contradicted():
        print(f"  contradicted: {line}")
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
                    help="offline coverage check, the one the suite runs and the default")
    ap.add_argument("--regenerate", action="store_true",
                    help="re-resolve against the network and rewrite constraints-ci.txt")
    args = ap.parse_args(argv)
    # Both flags is a contradiction rather than a preference, and it used to be accepted silently
    # because `args.self_test` was never read at all.
    if args.regenerate and args.self_test:
        ap.error("--regenerate and --self-test do different things; pick one")
    if args.regenerate:
        return regenerate()
    return self_test()


if __name__ == "__main__":
    sys.exit(main())

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
    python tools/make_constraints.py --regenerate   # network: re-resolve HERE and rewrite the file
    python tools/make_constraints.py --resolve-only OUT.json         # network: this platform's set
    python tools/make_constraints.py --regenerate --from A.json B.json   # union those, rewrite

WHY THE FILE IS A UNION OF TWO RESOLVES. pip evaluates environment markers on the interpreter that
runs it, and `--platform` changes only which wheel tags it accepts, so a machine cannot resolve
another platform's set: from Windows, `pywinpty ; sys_platform == "win32"` is still requested and
has no Linux wheel; from Linux it is never requested and never pinned. The monthly job therefore
resolves on an Ubuntu runner and a Windows runner, each with `--resolve-only`, and one `--regenerate
--from` unions them: a package every platform installs at one version is a plain pin, a package
only some platforms install carries the marker naming them, and a version the platforms disagree
on is refused, because that would be two sets and this file is one.

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
        # `pywinpty==3.0.5 ; sys_platform == "win32"`: the marker is where the pin applies, not
        # part of what it pins to.
        spec = line.partition(";")[0].strip()
        if "==" not in spec:
            # Recorded as a non-pin rather than skipped, so a range in a constraints file is a
            # finding instead of an absence.
            out[normalise(re.split(r"[<>=!~;\[]", spec, maxsplit=1)[0])] = None
            continue
        name, version = spec.split("==", 1)
        out[normalise(name)] = version.strip()
    return out


def drifted(installed=None):
    """Direct requirements whose INSTALLED version differs from the pin. Empty means this machine
    runs what CI runs.

    The check that would have saved a day. On 2026-09-02 a change passed locally, passed an
    independent review on the same machine, and failed CI twice, because CI installs pandas 3.0.5
    from these pins and the machine had 2.3.3, and pandas 3 represents a missing cell differently.
    Nothing said the two environments differed. Now the suite does, by name, with the fix.

    `installed` is injectable so the self-test can plant a drift; by default it reads the running
    environment. A direct requirement that is not installed at all is not drift, it is absence,
    and pip check reports that elsewhere.
    """
    from importlib import metadata
    have = pins()
    out = []
    for name in named_requirements():
        pinned = have.get(name)
        if pinned is None:
            continue
        if installed is not None:
            actual = installed.get(name)
        else:
            try:
                actual = metadata.version(name)
            except metadata.PackageNotFoundError:
                actual = None
        if actual is None:
            continue
        if normalise(actual) != normalise(pinned):
            out.append(f"{name}: installed {actual}, constraints-ci.txt pins {pinned}")
    return sorted(out)


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


def platform_set():
    """This machine's resolve, labelled with the platform whose markers pip evaluated it under."""
    return {"sys_platform": sys.platform, "python": CI_PYTHON, "pins": dict(resolve())}


def union(sets):
    """One list of (name, version, marker) rows from per-platform resolves.

    A package every set holds at one version is a plain pin. A package only some sets hold carries
    the marker naming those platforms, `sys_platform == "win32"` for a Windows-only wheel, so pip
    applies the pin exactly where the resolve saw it. A package two sets hold at DIFFERENT versions
    is refused with both named: that is two sets, and choosing one would be the hand-edit this file
    forbids.
    """
    if not sets:
        raise SystemExit("nothing to union")
    by_set = [{normalise(name): (name, version) for name, version in s["pins"].items()}
              for s in sets]
    labels = [s["sys_platform"] for s in sets]
    rows, clashes = [], []
    for key in sorted(set().union(*by_set)):
        seen = {label: found[key] for label, found in zip(labels, by_set, strict=True)
                if key in found}
        versions = {version for _, version in seen.values()}
        if len(versions) > 1:
            clashes.append(key + ": " + ", ".join(
                f"{label} {version}" for label, (_, version) in sorted(seen.items())))
            continue
        name = next(iter(seen.values()))[0]
        marker = ("" if len(seen) == len(sets) else
                  " or ".join(f'sys_platform == "{label}"' for label in sorted(seen)))
        rows.append((name, versions.pop(), marker))
    if clashes:
        raise SystemExit("the platforms disagree on a version, so this is two sets, not one:\n  "
                         + "\n  ".join(clashes))
    return rows


def render(rows, platforms=()):
    """The file body, header included, so the counts in the header cannot drift from the pins."""
    direct = set(named_requirements())
    covered = sum(1 for name, _, _ in rows if normalise(name) in direct)
    origin = (", ".join(platforms) if platforms else "one machine")
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
# Resolved for Python {CI_PYTHON}, matching the workflow's setup-python, on: {origin}. pip
# evaluates environment markers on the interpreter that runs it and `--platform` does not change
# that, so no one machine can resolve another platform's set; .github/workflows/pins.yml resolves
# on an Ubuntu runner and a Windows runner and this file is their union. A package only some
# platforms install carries the marker naming them, so pip applies that pin exactly where the
# resolve saw it and nowhere else. A version the platforms disagree on is refused, not chosen.
# CI is the authority on whether the set is satisfiable: a wrong pin fails the install loudly.
#
# {len(rows)} packages, of which {covered} are named in the requirements files.
'''
    lines = [header]
    for name, version, marker in rows:
        lines.append(f"{name}=={version}" + (f" ; {marker}" if marker else "")
                     + ("  # direct" if normalise(name) in direct else ""))
    return "\n".join(lines) + "\n"


def rewrite(rows, path=CONSTRAINTS, platforms=()):
    """Write the rendered set to `path`, PRESERVING its line endings, and return the ending used.

    The newline matters on Windows. Writing LF into a CRLF checkout rewrites every line's bytes, so
    `git diff` shows the whole file and says nothing about whether any pin actually moved, which is
    the one question a regenerate needs to answer.

    Bytes, not `read_text(newline="")`: that keyword exists only from Python 3.13, and the first run
    of the monthly pins job died on it on the 3.12 floor, after three green legs, because nothing in
    the suite had ever executed this path. The self-test now drives it on every interpreter the
    matrix runs, so an interpreter-specific call here fails the suite rather than the job.
    """
    existing = path.read_bytes().decode("utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in existing else "\n"
    path.write_text(render(rows, platforms), encoding="utf-8", newline=newline)
    return newline


def resolve_only(out):
    """This platform's set to a JSON file, for a `--regenerate --from` on another machine."""
    found = platform_set()
    Path(out).write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{out} written: {len(found['pins'])} pins resolved on {found['sys_platform']}")
    return 0


def regenerate(inputs=()):
    """Rewrite the file from the union of `inputs` (each a `--resolve-only` file), or from a
    resolve on this machine when there are none. Only the resolve needs the network."""
    if inputs:
        sets = [json.loads(Path(name).read_text(encoding="utf-8")) for name in inputs]
    else:
        sets = [platform_set()]
    rows = union(sets)
    platforms = tuple(s["sys_platform"] for s in sets)
    newline = rewrite(rows, platforms=platforms)
    missing, wrong = uncovered(), contradicted()
    print(f"constraints-ci.txt written: {len(rows)} pins from {len(sets)} platform set(s) "
          f"({', '.join(platforms)}), line endings {newline!r}")
    if len(sets) == 1:
        print("ONE platform only: a package another platform installs is not pinned here. The "
              "monthly job resolves on Ubuntu and Windows and unions them.")
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

    def rewritten(seed):
        """The bytes rewrite() leaves behind on a scratch file seeded with `seed`."""
        import tempfile
        with tempfile.TemporaryDirectory() as scratch_dir:
            scratch = Path(scratch_dir) / "constraints.txt"
            scratch.write_bytes(seed)
            rewrite([("packaging", "1.0", ""), ("pywinpty", "3.0.5", 'sys_platform == "win32"')],
                    scratch)
            return scratch.read_bytes()

    def refused(*sets):
        """True when union() refuses these sets, which is the answer for a version clash."""
        try:
            union(list(sets))
        except SystemExit:
            return True
        return False

    win = {"sys_platform": "win32", "pins": {"shared": "1.0", "pywinpty": "3.0.5"}}
    lin = {"sys_platform": "linux", "pins": {"shared": "1.0", "uvloop": "0.21.0"}}
    merged = {name: (version, marker) for name, version, marker in union([win, lin])}

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
        # This machine runs what CI runs. When it does not, the suite says which package and what
        # to do about it, instead of passing here and failing there.
        ("every installed direct requirement matches its pin "
         "(else: pip install -c constraints-ci.txt -r requirements.txt -r requirements-dev.txt)",
         drifted() == []),
        # Planted drift, so the row above is known to be able to fail.
        ("a drifted install IS detected",
         any("pandas" in line for line in drifted({"pandas": "2.3.3"}))),
        ("and a matching install is not",
         drifted({name: version for name, version in have.items() if version}) == []),
        # The rewrite the monthly job performs, driven here on every interpreter the matrix runs.
        # Its first run died on the 3.12 floor calling an API that exists only from 3.13, and no
        # leg had noticed because no check had ever executed the path.
        ("a rewrite over a CRLF file keeps CRLF",
         b"\r\n" in rewritten(b"a==1\r\n")
         and b"\n" not in rewritten(b"a==1\r\n").replace(b"\r\n", b"")),
        ("a rewrite over an LF file keeps LF", b"\r" not in rewritten(b"a==1\n")),
        ("and the rewrite carries the pin it was given", b"packaging==1.0" in rewritten(b"")),
        # A pin that applies on one platform only. The marker is where it applies, not part of the
        # version, and it must survive a rewrite and a read back, or the union is lost on the way.
        ("a pin that carries a marker is read as a pin",
         pins('pywinpty==3.0.5 ; sys_platform == "win32"').get("pywinpty") == "3.0.5"),
        ("and the marker survives the rewrite and the read back",
         pins(rewritten(b"").decode("utf-8")).get("pywinpty") == "3.0.5"
         and b'pywinpty==3.0.5 ; sys_platform == "win32"' in rewritten(b"")),
        # The union of two platform resolves, which is what the monthly job writes.
        ("a package both platforms install at one version is a plain pin",
         merged.get("shared") == ("1.0", "")),
        ("a package only Windows installs carries the win32 marker",
         merged.get("pywinpty") == ("3.0.5", 'sys_platform == "win32"')),
        ("a package only Linux installs carries the linux marker",
         merged.get("uvloop") == ("0.21.0", 'sys_platform == "linux"')),
        ("a version the platforms disagree on is REFUSED, not chosen",
         refused({"sys_platform": "win32", "pins": {"x": "1.0"}},
                 {"sys_platform": "linux", "pins": {"x": "2.0"}})),
        ("and agreeing platforms are not refused", not refused(win, lin)),
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
    for line in drifted():
        print(f"  drifted: {line}")
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
                    help="rewrite constraints-ci.txt from --from files, or from a resolve here")
    ap.add_argument("--from", dest="inputs", nargs="+", metavar="JSON", default=(),
                    help="with --regenerate: per-platform --resolve-only files to union")
    ap.add_argument("--resolve-only", metavar="OUT",
                    help="resolve on this platform and write the set to OUT; no rewrite")
    args = ap.parse_args(argv)
    # Both flags is a contradiction rather than a preference, and it used to be accepted silently
    # because `args.self_test` was never read at all.
    if args.regenerate and args.self_test:
        ap.error("--regenerate and --self-test do different things; pick one")
    if args.inputs and not args.regenerate:
        ap.error("--from only means something with --regenerate")
    if args.resolve_only and (args.regenerate or args.self_test):
        ap.error("--resolve-only writes one platform's set and nothing else; pick one")
    if args.resolve_only:
        return resolve_only(args.resolve_only)
    if args.regenerate:
        return regenerate(args.inputs)
    return self_test()


if __name__ == "__main__":
    sys.exit(main())

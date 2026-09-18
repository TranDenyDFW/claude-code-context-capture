"""Where the code, its bundled assets and its install live, whether or not it is frozen.

Three places, and they are one directory until PyInstaller pulls them apart:

    REPO_ROOT       the checkout this file sits in
    bundle_root()   where the built page (`frontend/dist`) and `c4x/prices.json` were packed
    install_root()  where the store, the node tools and `tmp/` live

Not frozen, all three are the checkout. Frozen, the bundle is PyInstaller's extraction directory
(`_internal` beside the exe in a one-dir build), and the install is whichever directory at or above
the exe holds `tools/harvest.mjs`: a checkout the exe was dropped into, or the download's own
folder, which carries the tools beside the exe for exactly this reason.

THE EXE REPLACES PYTHON, NOT NODE, and the download therefore carries node too (`node/node.exe`,
found by `node_exe`). The hooks and the harvester are node, the store they write is what this
serves, and a machine that wanted the download has neither interpreter.

FAILS CLOSED. An exe copied to a folder with no checkout above it used to be worth guessing about:
its own directory as the install would mean a store that does not exist, node scripts that are
not there, and a page that loads and then answers 500 on every tab. That is a working-looking page
over nothing, so the answer is exit 2 with the reason, before anything imports the store. The one
override is a store named by `C4X_DB` (which `python -m c4x.api --db` exports before the store is
imported): a store inside a checkout names that checkout.
"""
import os
import sys
from collections.abc import Mapping
from pathlib import Path

# `vars(sys).get`, not `getattr`: tools/table_audit.py reads every `getattr(...)` call as a callee
# it cannot name (the evasion gate for hidden table constructions) and fails the suite on it. The
# two attributes are PyInstaller's and absent from typeshed, so a plain `sys.frozen` fails mypy.
FROZEN: bool = bool(vars(sys).get("frozen", False))
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# What makes a directory a c4x install: the harvester, which every install has and which nothing
# else on a machine is likely to carry at this relative path.
MARKER = ("tools", "harvest.mjs")

NOT_AN_INSTALL = (
    "c4x.exe must run from inside a c4x install (for example <root>/dist/c4x/), or be given "
    "--db <store> inside one. It replaces Python, not node: the hooks and the harvester are node, "
    "and the store they write is what this serves.\n"
)


def bundle_root() -> Path:
    """Where the packed assets are: PyInstaller's extraction directory if frozen, else the repo."""
    if FROZEN:
        packed = vars(sys).get("_MEIPASS", "")
        if packed:
            return Path(packed)
    return REPO_ROOT


def node_exe(root: Path | None = None, exists=None) -> str:
    """The node to run: the one the install carries, else whatever `node` PATH resolves to.

    THE DOWNLOAD HAS NEITHER PYTHON NOR NODE. It is one folder: `c4x.exe`, `node/node.exe`, and
    the tools the exe shells out to. Spelling the interpreter `node` and hoping would fail on the
    machine the download exists for, so the bundled one is named by its path when it is there.

    A checkout keeps the bare name, which is what a developer already has on PATH and what every
    existing install's hook commands say.
    """
    here = REPO_ROOT if root is None else Path(root)
    bundled = here / "node" / ("node.exe" if os.name == "nt" else "node")
    is_file = os.path.isfile if exists is None else exists
    return str(bundled) if is_file(str(bundled)) else "node"


def find_install(start: Path) -> Path | None:
    """The first directory at or above `start` that holds the marker, or None."""
    for candidate in (start, *start.parents):
        if candidate.joinpath(*MARKER).is_file():
            return candidate
    return None


def install_root(frozen: bool | None = None, executable: str | None = None,
                 env: Mapping[str, str] | None = None) -> Path:
    """The install: the checkout when not frozen, else the one the exe or the store sits in.

    The parameters exist for the tests; every real caller passes nothing.
    """
    if not (FROZEN if frozen is None else frozen):
        return REPO_ROOT
    environment = os.environ if env is None else env
    exe = Path(executable or sys.executable).resolve()
    found = find_install(exe.parent)
    if found is None:
        named = environment.get("C4X_DB")
        if named:
            found = find_install(Path(named).resolve().parent)
    if found is None:
        sys.stderr.write(NOT_AN_INSTALL)
        raise SystemExit(2)
    return found

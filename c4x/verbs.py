"""What `c4x.exe <verb>` means, decided before anything heavy is imported.

THE USER'S RULE: "Make it purely an executable - if someone wants to use CLI options, they can add
options after calling the executable." So the download is one folder and one program. `c4x.exe`
with no arguments installs itself into Claude and opens the page; `c4x.exe install`, `status`,
`uninstall`, `reset` and `harvest` are the node tools that already do those jobs, run with the node
the folder carries; `c4x.exe serve` and any bare flag list are the server this exe has always been.

NOTHING IS REWRITTEN IN PYTHON. `tools/install.mjs` is the installer, and it stays the installer:
the verbs spawn it with every remaining argument passed through, so a flag added there works here
the day it is added, and the two can never drift into two behaviours.

THE PLAN IS PURE. `plan()` decides what to run from argv alone and returns it; `main()` runs it.
That is what lets a test state every shape of the dispatch, including the frozen no-argument case,
without a node, a browser or a server anywhere near it.
"""
from __future__ import annotations

from pathlib import Path

# Each verb is the tool that already does the job, and the flags the person typed go after it.
SCRIPTS = {
    "install": ("tools", "install.mjs"),
    "status": ("tools", "install.mjs"),
    "uninstall": ("tools", "install.mjs"),
    "reset": ("tools", "install.mjs"),
    "harvest": ("tools", "harvest.mjs"),
    "dashboard": ("tools", "dashboard.mjs"),
}
# `status`, `uninstall` and `reset` are install.mjs's own verbs, so the word is passed along.
INSTALL_VERBS = {"install", "status", "uninstall", "reset"}
SERVE = "serve"
VERSION_FLAGS = ("--version", "-V")

USAGE = """c4x: what a Claude Code session cost, and where the window went.

  c4x                     install into Claude, then open the dashboard
  c4x install [flags]     wire the hooks (--no-dashboard, --adopt <dir>, --rewire, ...)
  c4x status              is it wired, is it capturing
  c4x uninstall           unwire it, keeping the store
  c4x reset               start the store again
  c4x harvest [flags]     read the transcripts into the store now
  c4x serve [flags]       the dashboard server (--db, --port, --watchdog)
  c4x --version           what this build is

Any flags after the verb go to the tool that does the work, unchanged.
"""


def wants_dispatch(argv: list[str], *, frozen: bool) -> bool:
    """Whether this argv is a verb at all, answered without touching the filesystem.

    THE ROOT IS NOT FREE. Finding the install reads directories and, when it finds none, exits the
    program with a message about installs, which is the wrong answer to `--reload` and to every
    other flag the server itself is about to explain. A bare flag list is the server, and the
    server needs no verb machinery, so nothing is resolved for it.
    """
    if not argv:
        return frozen
    first = argv[0]
    return not first.startswith("-") or first in VERSION_FLAGS or first in ("--help", "-h")


def plan(argv: list[str], *, frozen: bool, root: Path, exe: str) -> dict:
    """What to do about `argv`, as data.

    `{"kind": "serve", "argv": [...]}` runs the server this module's caller already is;
    `{"kind": "node", "argv": [...]}` spawns a tool; `{"kind": "say", "text": ..., "code": n}`
    prints and exits; `{"kind": "first-run", "steps": [...]}` is the double-clicked exe.
    """
    rest = list(argv)
    if any(flag in rest for flag in VERSION_FLAGS):
        return {"kind": "version"}
    if rest and rest[0] in ("--help", "-h", "help"):
        return {"kind": "say", "text": USAGE, "code": 0}

    # A BARE FLAG LIST IS THE SERVER, unchanged. `tools/dashboard.mjs` launches this exe with
    # `--db ... --port ... --watchdog`, and `c4x/server.py` restarts it with its own argv; neither
    # knows about verbs, and neither has to.
    if not rest or rest[0].startswith("-"):
        if not rest and frozen:
            # DOUBLE-CLICKED, which is how the download is met: nobody types a verb the first time.
            return {"kind": "first-run", "steps": [
                {"kind": "node", "argv": [str(root / "tools" / "install.mjs"), "install",
                                          "--launcher", exe]},
                {"kind": "node", "argv": [str(root / "tools" / "dashboard.mjs"), "launch"]},
                {"kind": "open"},
            ]}
        return {"kind": "serve", "argv": rest}

    verb, *flags = rest
    if verb == SERVE:
        return {"kind": "serve", "argv": flags}
    if verb in SCRIPTS:
        script = str(root.joinpath(*SCRIPTS[verb]))
        passed = ([verb] if verb in INSTALL_VERBS else []) + flags
        return {"kind": "node", "argv": [script, *passed]}
    return {"kind": "say", "text": f"c4x: no verb {verb!r}.\n\n{USAGE}", "code": 2}


MISSING_NODE = """c4x: {node} is not there, so nothing can be run.

This folder carries the node its tools need, at node/node.exe beside c4x.exe. If it has been
removed or quarantined, unzip the download again over this folder.
"""


def _spawn(call, node: str, argv: list[str], *, root: Path, write) -> int:
    """Run a tool, and say what is missing rather than raising through the program's own exit.

    A folder somebody unzipped always has its node. An antivirus that quarantined node.exe puts it
    in exactly this state, and a traceback is not an answer to that.
    """
    try:
        return int(call([node, *argv], cwd=str(root)).returncode)
    except FileNotFoundError:
        write(MISSING_NODE.format(node=node))
        return 2


def run(decision: dict, *, root: Path, node: str, serve, call, open_page, write, version) -> int:
    """Carry out what `plan` decided. Every door is a parameter, so the tests need none of them."""
    kind = decision["kind"]
    if kind == "serve":
        return int(serve(decision["argv"]))
    if kind == "version":
        write(version() + "\n")
        return 0
    if kind == "say":
        write(decision["text"])
        return int(decision["code"])
    if kind == "node":
        return _spawn(call, node, decision["argv"], root=root, write=write)
    if kind == "first-run":
        for step in decision["steps"]:
            if step["kind"] == "open":
                open_page()
                continue
            code = _spawn(call, node, step["argv"], root=root, write=write)
            if code != 0:
                return code
        return 0
    raise AssertionError(f"unknown plan {kind!r}")

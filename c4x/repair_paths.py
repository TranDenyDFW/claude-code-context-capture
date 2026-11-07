r"""Give a chat back its working folder after the folder moved on disk.

The desktop app shows "Working folder no longer exists. Choose a different folder to continue." for
any chat whose record names a `cwd` that is gone. Moving a tree (here `P:\` became `R:\Projects\`)
leaves every record that named the old path pointing at nothing, and the records live outside any
repository, so a pass that rewrites paths in a checkout never touches them.

TWO FIELDS MOVE, AND ONLY TWO. `cwd` is what the app tests for existence, and `originCwd` is read
beside it as `originCwd || cwd`. Measured on this machine: all 1,350 stale record files carry both,
naming the same gone directory.

WHAT IS DELIBERATELY LEFT ALONE MATTERS MORE. A record also carries the directories the user has
granted permission to (`sessionPermissionUpdates[].directories`, 18 records here) and the text of
their own earlier prompts (`scratchPromptRecents`, 37 records). Rewriting a permission grant would
quietly extend a permission to a directory nobody granted, and rewriting someone's prompt text
would edit what they said. Both are counted and reported, never touched.

THE HISTORY STAYS WHERE IT IS. Claude Code keeps a chat's transcript under
`~/.claude/projects/<slug>`, and on this machine every chat's slug matches its own `cwd`, which
makes it tempting to move the folders along. The app does not need that: it finds a transcript by
scanning the projects directory for the session id, and the missing-folder banner is driven by the
existence of `cwd` alone. Moving 1.6 GB of history to satisfy a rule the app does not apply is a
large irreversible change bought for nothing, so `folders=True` exists for a deliberate operator
and is off by default.

NOTHING IS GUESSED. A record is rewritten only when a candidate directory for its new path is
really on disk; a path that resolves to nothing is reported by name and left exactly as it was. No
file is ever overwritten, and the report names every change so each one can be undone.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

RECORD = "local_"
PART = ".c4x-part"

# The only fields this module ever writes.
FIELDS = ("cwd", "originCwd")

# Fields that carry a path and are none of this module's business, counted so a run can say what it
# chose not to touch rather than leaving it to be discovered later.
KEEP = ("sessionPermissionUpdates", "scratchPromptRecents")

# Each entry is an old prefix and the places it might live now, in the order they are tried. The
# first candidate that is really a directory wins. Order matters and is not cosmetic: `P:\WorkNotes`
# resolves under both, and `R:\WorkNotes` is an empty decoy while `R:\Projects\WorkNotes` holds
# every subfolder the records actually name.
DEFAULT_MOVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("P:\\", ("R:\\Projects\\", "R:\\")),
    # The same tree was gathered under `Projects` a second time, so chats that already named `R:\`
    # went missing too. Harmless where it does not apply: a path only moves when the original is
    # gone AND the candidate is really there.
    ("R:\\", ("R:\\Projects\\",)),
)


def moves_for(appdata=None, roaming=None, moves=DEFAULT_MOVES):
    """The default moves plus this machine's own relocation of the app's scratch workspaces.

    The desktop app keeps a scratch workspace per chat beside its records, so when the app moved
    its data directory those paths died with the old one. Derived from the resolved roots rather
    than written down, because the new name carries the install's publisher suffix.
    """
    from c4x import store
    here = store.claude_appdata() if appdata is None else appdata
    was = roaming if roaming is not None else os.path.join(
        os.environ.get("APPDATA") or "", "Claude")
    if not here or not was or os.path.normcase(str(here)) == os.path.normcase(str(was)):
        return moves
    return moves + ((os.path.join(str(was), "scratch-workspaces") + os.sep,
                     (os.path.join(str(here), "scratch-workspaces") + os.sep,)),)


def slug(path: str) -> str:
    """The `~/.claude/projects` folder name for a working directory.

    Every character that is not a letter, a digit or a hyphen becomes a hyphen, which is why
    `P:\\WorkNotes\\Weather.com-Awake` is `P--WorkNotes-Weather-com-Awake`. Checked against every
    record on this machine that has a transcript: 256 records, no disagreements.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", path or "")


def resolve(cwd: str, moves=DEFAULT_MOVES, isdir=None) -> str | None:
    """Where `cwd` lives now, or None when it is fine already or nothing on disk matches.

    None for a path that still exists is what makes the repair idempotent: a second run finds
    nothing to do rather than rewriting what it already wrote.
    """
    is_dir = os.path.isdir if isdir is None else isdir
    if not cwd or not isinstance(cwd, str):
        return None
    if is_dir(cwd):
        return None
    for old, candidates in moves:
        if cwd.upper().startswith(old.upper()):
            rest = cwd[len(old):]
            for new in candidates:
                found = new + rest
                if is_dir(found):
                    return found
    return None


def records_under(root) -> list:
    """Every record file under a records root, across all its `<account>\\<org>` pairs."""
    return sorted(Path(root).glob(f"*/*/{RECORD}*.json"))


def untouched_in(body) -> dict[str, int]:
    """How many paths this record carries that the repair refuses to rewrite."""
    found: dict[str, int] = {}
    for update in (body.get("sessionPermissionUpdates") or []):
        if isinstance(update, dict):
            n = sum(1 for d in (update.get("directories") or []) if isinstance(d, str))
            if n:
                found["sessionPermissionUpdates[].directories"] = (
                    found.get("sessionPermissionUpdates[].directories", 0) + n)
    recents = [x for x in (body.get("scratchPromptRecents") or []) if isinstance(x, str)]
    if recents:
        found["scratchPromptRecents"] = len(recents)
    return found


def plan(root, projects=None, moves=DEFAULT_MOVES, isdir=None) -> dict[str, Any]:
    """What a run would do. It writes nothing.

    `rewrites` is one entry per record FILE, because the same chat has a copy in every pair.
    """
    is_dir = os.path.isdir if isdir is None else isdir
    home = Path(projects) if projects is not None else Path.home() / ".claude" / "projects"
    rewrites, unresolved, targets, kept = [], {}, {}, {}
    for path in records_under(root):
        try:
            body = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        cwd = body.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        found = resolve(cwd, moves, is_dir)
        if found is None:
            # Only a path that is GONE is a problem. One that still exists needs no repair, and
            # reporting it would turn every healthy record into a skip.
            if not is_dir(cwd):
                unresolved[cwd] = unresolved.get(cwd, 0) + 1
            continue
        fields = {}
        for name in FIELDS:
            value = body.get(name)
            if isinstance(value, str) and value and resolve(value, moves, is_dir):
                fields[name] = resolve(value, moves, is_dir)
        targets[cwd] = found
        for name, n in untouched_in(body).items():
            kept[name] = kept.get(name, 0) + n
        rewrites.append({"file": str(path), "from": cwd, "to": found, "fields": fields,
                         "id": body.get("sessionId") or Path(path).stem})

    folders = []
    for cwd, found in sorted(targets.items()):
        source, destination = home / slug(cwd), home / slug(found)
        if not is_dir(str(source)):
            continue
        entries = sorted(p.name for p in source.iterdir())
        folders.append({"from": str(source), "to": str(destination), "entries": len(entries),
                        "clashes": [n for n in entries if (destination / n).exists()],
                        "exists": is_dir(str(destination))})
    return {"rewrites": rewrites, "folders": folders, "untouched": kept,
            "unresolved": dict(sorted(unresolved.items())),
            "chats": len({r["id"] for r in rewrites})}


def write_fields(path, fields) -> None:
    """Replace just those fields, atomically, keeping every other byte of the record.

    Through a temporary file and `os.replace`, so a record is never half written: the app reads
    these at startup and a truncated one is a chat that will not open. The modification time is
    carried over, because `claude_appdata()` ranks roots by it and a bulk rewrite would otherwise
    make every record look new.
    """
    path = Path(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    before = {k: body.get(k) for k in body if k not in fields}
    body.update(fields)
    after = {k: body.get(k) for k in body if k not in fields}
    if before != after:                      # cannot happen, and if it does nothing is written
        raise ValueError("the rewrite would have changed a field it was not asked to change")
    stat = path.stat()
    temporary = path.with_name(path.name + PART)
    temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
    os.utime(path, (stat.st_atime, stat.st_mtime))


def move_folder(source, destination) -> dict[str, Any]:
    """Merge one history folder into another, never overwriting anything already there."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    moved, kept = 0, []
    for entry in sorted(source.iterdir()):
        target = destination / entry.name
        if target.exists():
            kept.append(entry.name)
        else:
            shutil.move(str(entry), str(target))
            moved += 1
    if source.is_dir() and not any(source.iterdir()):
        source.rmdir()
    return {"from": str(source), "to": str(destination), "moved": moved, "kept_both": kept}


def repair(root=None, projects=None, dry_run=True, moves=None, isdir=None,
           folders=False) -> dict[str, Any]:
    """Point every record whose working folder moved at the folder that exists now."""
    from c4x import store
    where = store.sessions_root() if root is None else root
    said = plan(where, projects, moves_for() if moves is None else moves, isdir)
    said.update({"root": str(where), "dry_run": bool(dry_run), "rewritten": 0,
                 "failed": [], "moved": []})
    if dry_run:
        return said
    for item in said["rewrites"]:
        try:
            write_fields(item["file"], item["fields"])
            said["rewritten"] += 1
        except (OSError, ValueError) as exc:
            said["failed"].append({"file": item["file"], "why": str(exc)})
    if folders:
        for item in said["folders"]:
            try:
                said["moved"].append(move_folder(item["from"], item["to"]))
            except OSError as exc:
                said["failed"].append({"folder": item["from"], "why": str(exc)})
    return said


def sentence(said) -> str:
    """One line a person can act on."""
    chats, files = said["chats"], len(said["rewrites"])
    stuck = sum(said["unresolved"].values())
    tail = (f"; {stuck} record file(s) name a folder that is nowhere on disk and were left alone"
            if stuck else "")
    if said.get("dry_run"):
        if not files:
            return f"nothing to repair: every chat's working folder is there{tail}"
        return (f"dry run: {files} record file(s) for {chats} chat(s) would be pointed at a folder "
                f"that exists{tail}")
    failed = f", {len(said['failed'])} failed" if said["failed"] else ""
    moved = sum(m["moved"] for m in said["moved"])
    history = f", {moved} history item(s) moved" if moved else ""
    return (f"{said['rewritten']} record file(s) for {chats} chat(s) now point at a folder that "
            f"exists{history}{failed}{tail}")


def main(argv=None) -> int:
    """`python -m c4x.repair_paths [--dry-run | --run] [--json] [--folders]`"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    said = repair(dry_run="--run" not in argv, folders="--folders" in argv)
    if "--json" in argv:
        print(json.dumps(said, indent=1))
        return 1 if said["failed"] else 0
    print(sentence(said))
    for cwd, found in sorted({r["from"]: r["to"] for r in said["rewrites"]}.items()):
        n = sum(1 for r in said["rewrites"] if r["from"] == cwd)
        print(f"  {n:4d} record file(s)  {cwd}  ->  {found}")
    for name, n in sorted(said["untouched"].items()):
        print(f"  left alone: {n} path(s) in {name}")
    for cwd, n in said["unresolved"].items():
        print(f"  NO FOLDER ANYWHERE for {cwd} ({n} record file(s), unchanged)")
    return 1 if said["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

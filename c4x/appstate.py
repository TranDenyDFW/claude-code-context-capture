"""The state CLAUDE CODE keeps for a project, which c4x's own store is not.

WHY THIS EXISTS. `export` wrote a complete copy of what c4x had PARSED and nothing of what Claude
Code itself holds, so an imported project appeared in c4x and was invisible to the desktop app.
Measured on a second machine: an import touched exactly one file, `context.db`, and left
`~/.claude/projects`, `~/.claude.json`, `history.jsonl` and `settings.json` byte-identical. A disk
clone moves the project because it copies those files; c4x moved one of them, so it did not.

WHAT IS PER-PROJECT, from `claude project purge --dry-run` on Claude Code 2.1.263. The footprint
is NOT fixed at two items, which an earlier version of this file asserted after measuring exactly
one project. `P:\\Skills` prints two; `P:\\ClaudeExt\\QuestionExtension` prints FOUR, adding a
`~/.claude/tasks/<session id>` directory per session. So:

    dir:    ~/.claude/projects/<slug>       transcripts (.jsonl) and memory/
    dir:    ~/.claude/tasks/<session id>    one per session that has tasks
    config: ~/.claude.json projects[path]   trust, history, MCP servers

`shell-snapshots/` are NOT project-scoped, the tool says so itself, and `~/.claude/backups` keeps
up to five rotating snapshots in which a purged config entry survives. `history.jsonl` keys its
lines on the directory a prompt was TYPED in, not on the project.

THE CONFIG ENTRY IS THE HALF PEOPLE FORGET, and it carries TRUST: 52 of the 91 project entries on
the machine this was written on hold `hasTrustDialogAccepted`.

THE API TAKES DIRECTORIES, NEVER A PROJECT NAME, and that is the whole shape of this module. A
"project" in c4x can be a PAGE LABEL: `session_rows()` appends `\\archived` once the desktop app
has archived a chat. Passing that label where a working directory belongs went wrong FIVE times
while this was being built, three of them silently, twice past gates written for it. The signature
is the fix a comment could not be: there is no parameter here a label can occupy.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

#: Everything a captured bundle can hold, so a reader of an export can ask what is in it.
TRANSCRIPT = "transcript"
CONFIG = "config"
DESKTOP = "desktop"
TASKS = "tasks"

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
CONFIG_PATH = HOME / ".claude.json"


def slug_for(cwd: str) -> str:
    """The transcript folder name Claude Code derives from a working directory.

    Every character that is not a letter or a digit becomes a hyphen, so `P:\\Skills` is
    `P--Skills`. Verified against the real directory rather than inferred: the purge plan for
    `P:\\Skills` names `~/.claude/projects/P--Skills`.
    """
    return "".join(ch if ch.isalnum() else "-" for ch in str(cwd))


def project_dir(cwd: str) -> Path:
    return CLAUDE_DIR / "projects" / slug_for(cwd)


def tasks_dir(session_id: str) -> Path:
    return CLAUDE_DIR / "tasks" / str(session_id)


def config_entry(cwd: str):
    """That working directory's entry in ~/.claude.json, or None.

    Read as JSON and returned as a plain dict: the caller stores it, and nothing here goes near the
    rest of that file, which holds the OAuth session.
    """
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data.get("projects") or {}).get(str(cwd))


def cwds_for(session_ids):
    """The WORKING DIRECTORIES those sessions ran in, which is what Claude Code keys its state on.

    THE CALLER MUST RUN THIS BEFORE DELETING ANYTHING. `delete` called it afterwards, when the
    `sessions` rows were already gone, so it returned an empty list on every run and the caller's
    fallback, a project NAME, was the only branch that ever executed. That handed a page label to
    `claude project purge`, which purged nothing, while the store rows and the desktop records were
    already destroyed. Fifth instance of the same substitution.
    """
    from c4x import store

    if not session_ids:
        return []
    ids = [str(s) for s in session_ids]
    marks = ",".join("?" * len(ids))
    frame = store.q(f"SELECT DISTINCT cwd FROM sessions WHERE session_id IN ({marks}) "
                    "AND cwd IS NOT NULL AND cwd <> ''", tuple(ids))
    return [str(c) for c in frame["cwd"].tolist()] if not frame.empty else []


def desktop_records(session_ids, with_mtime=False):
    """The DESKTOP APP's own record for each session, as (relative path, bytes).

    One `local_<uuid>.json` per chat, holding `cwd`, `isArchived`, `originCwd` and the
    `cliSessionId` that is what this store calls `session_id`.

    IDENTIFIED BY store.read_archived_record, not by re-parsing: that function already knows the
    shape and already knows the same directory holds `scheduled-tasks.json` files which are not
    session records.
    """
    import glob

    from c4x import store

    wanted = {str(s) for s in session_ids}
    root = Path(store.sessions_root())
    out = []
    if not wanted or not root.is_dir():
        return out
    for path in glob.glob(str(root / "*" / "*" / "*.json")):
        row = store.read_archived_record(path)
        if row is None or str(row[0]) not in wanted:
            continue
        p = Path(path)
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        blob = _read(p)
        out.append((rel, _stat(p), blob) if with_mtime else (rel, blob))
    return out


def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _stat(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def capture(cwds, session_ids=()):
    """Every Claude Code file these sessions own, as (kind, relative path, mtime, bytes).

    `cwds` is a LIST OF WORKING DIRECTORIES, from `cwds_for`. Never a project name: see the module
    docstring for why that is a signature rather than a convention.

    TRANSCRIPTS ARE SELECTED BY SESSION ID, not by walking the folder. A transcript is named
    `<session id>.jsonl`, and the folder is shared by every session with that working directory,
    including ones the caller did not ask for. Walking it meant a single archived session's export
    carried all 1,445 files and 725 MB of its 67-session sibling, held in memory as a list of blobs
    inside the web app's export route.

    `memory/` belongs to the directory rather than to a session, so it is taken whole.
    """
    ids = [str(s) for s in session_ids]
    items = []
    for cwd in cwds:
        root = project_dir(cwd)
        for sid in ids:
            f = root / f"{sid}.jsonl"
            if f.is_file() and not f.is_symlink():
                items.append((TRANSCRIPT, f"{sid}.jsonl", _stat(f), _read(f)))
        memory = root / "memory"
        if memory.is_dir():
            for base, _dirs, files in os.walk(memory):
                for name in files:
                    p = Path(base) / name
                    # A SYMLINK IS NOT THIS PROJECT'S CONTENT. Following one puts whatever
                    # it points at into the export, which then travels to another machine
                    # under a name that says `memory/`.
                    if p.is_symlink():
                        continue
                    rel = str(p.relative_to(root)).replace(os.sep, "/")
                    items.append((TRANSCRIPT, rel, _stat(p), _read(p)))
        entry = config_entry(cwd)
        if entry is not None:
            items.append((CONFIG, "claude.json", _stat(CONFIG_PATH),
                          json.dumps(entry).encode("utf-8")))
    # `~/.claude/tasks/<session id>`, which the purge plan lists and which an earlier version of
    # this file did not collect. Delegating the delete to that command therefore destroyed state
    # the verified backup did not contain: 15 files for one project, measured.
    for sid in ids:
        troot = tasks_dir(sid)
        if not troot.is_dir():
            continue
        for base, _dirs, files in os.walk(troot):
            for name in files:
                p = Path(base) / name
                rel = f"{sid}/" + str(p.relative_to(troot)).replace(os.sep, "/")
                items.append((TASKS, rel, _stat(p), _read(p)))
    for rel, stamp, blob in desktop_records(ids, with_mtime=True):
        items.append((DESKTOP, rel, stamp, blob))
    return items


def summarise(items):
    """What a captured bundle holds, for a report or a manifest."""
    readable = [i for i in items if i[3] is not None]
    out = {"unreadable": [p for _k, p, _m, b in items if b is None]}
    for kind in (TRANSCRIPT, CONFIG, DESKTOP, TASKS):
        got = [b for k, _p, _m, b in readable if k == kind]
        out[kind] = {"files": len(got), "bytes": sum(len(b) for b in got)}
    return out


def _contained(root: Path, rel: str):
    """`root / rel` when it genuinely stays under root, else None.

    Compared after RESOLVING both, because the check has to answer where the write actually lands,
    not what the string looks like: a prefix test on the raw text passes `..` and fails a
    legitimate path through a symlinked home. An absolute path in the export is rejected by the
    same test, since it resolves outside root on its own.

    THE ROOT ITSELF IS NOT A VALID TARGET. An empty `rel`, or `.`, resolves to the directory, and
    an earlier version returned it: `restore` then wrote a regular FILE over the project's
    transcript directory, reported `written: 1`, and every later restore into that project raised
    FileExistsError forever.
    """
    rel = str(rel)
    if not rel.strip() or rel.strip() in (".", "./", ".\\"):
        return None
    try:
        target = (root / rel).resolve()
        base = root.resolve()
    except (OSError, ValueError):
        return None
    return target if base in target.parents else None


def restore(cwds, items, when_exists="newer"):
    """Write a captured bundle back into THIS machine's Claude Code state.

    `cwds` is a list of working directories, as for `capture`.

    `when_exists` decides what happens to a file already on disk:

      "newer" (default)  take the incoming file when its recorded mtime is later
      "never"            never replace anything; report every collision instead
      "always"           replace unconditionally

    THE SHORTER-BUT-NEWER CASE IS REPORTED, NOT PREVENTED. A transcript can be rewritten IN PLACE
    and come out smaller, because compaction writes a sibling temp file and renames it over the
    original, so a compacted transcript is newer AND shorter and "newer wins" replaces a complete
    record with a compacted one. That is a real way to lose conversation, it is not detectable from
    the bytes, and the policy is deliberate, so every such replacement is listed.

    THE RESTORED FILE KEEPS ITS RECORDED MTIME. Without that a restore stamps every file with
    "now", so a second import of a corrected export compares the new file against a newer one on
    disk and silently does nothing.
    """
    from c4x import store

    report = {"written": 0, "bytes": 0, "kept_existing": [], "replaced": [],
              "newer_but_shorter": [], "rejected_paths": [], "failed": [],
              "by_kind": {}, "config": "not present"}
    landed = {}
    roots = {DESKTOP: Path(store.sessions_root()), TASKS: CLAUDE_DIR / "tasks"}
    if cwds:
        roots[TRANSCRIPT] = project_dir(cwds[0])

    for kind, rel, incoming, blob in items:
        if kind == CONFIG or kind not in roots:
            continue
        if not isinstance(blob, (bytes, bytearray)):
            # A blob that is not bytes is a corrupt or hostile export, not a crash. Letting it
            # reach write_bytes raises out of the loop and abandons the restore halfway.
            report["failed"].append(f"{kind}:{rel}: not bytes ({type(blob).__name__})")
            continue
        target = _contained(roots[kind], rel)
        if target is None:
            # AN EXPORT IS UNTRUSTED INPUT. It arrives from another machine and this function
            # writes the paths it names: `../../../../evil.txt` under the transcript root resolved
            # to C:\\Users\\evil.txt before this guard existed.
            report["rejected_paths"].append(f"{kind}:{rel}")
            continue

        # TWO ROWS LANDING ON ONE FILE is a silent loss of the first, and it is a
        # fact about the EXPORT rather than about what is on disk, so it is decided
        # before the exists-and-newer branch. Placed after it, the second row exited
        # as kept_existing and the collision was never reported at all. Windows
        # compares paths case-insensitively, so `a.jsonl` and `A.JSONL` are two rows
        # and one file.
        key = str(target).casefold()
        if key in landed:
            report["failed"].append(f"{kind}:{rel}: collides with {landed[key]}")
            continue
        try:
            if target.exists():
                if target.is_dir():
                    report["failed"].append(f"{kind}:{rel}: a directory is already there")
                    continue
                take = (when_exists == "always"
                        or (when_exists == "newer" and incoming is not None
                            and incoming > target.stat().st_mtime))
                if not take:
                    report["kept_existing"].append(rel)
                    continue
                if len(blob) < target.stat().st_size:
                    report["newer_but_shorter"].append(
                        f"{rel} ({target.stat().st_size} -> {len(blob)} bytes)")
                report["replaced"].append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            if incoming is not None:
                os.utime(target, (incoming, incoming))
            # WHAT LANDED, NOT WHAT WAS ATTEMPTED. `write_bytes` to a Windows device name such as
            # `nul` or `con` SUCCEEDS and swallows the content: measured, size 0, reads back empty,
            # absent from the directory listing, while the report said written with the full byte
            # count. Counting the attempt is a coverage number that rewards fabrication.
            if target.stat().st_size != len(blob):
                report["failed"].append(
                    f"{kind}:{rel}: wrote {len(blob)} bytes and {target.stat().st_size} landed")
                continue
            landed[key] = rel
        except OSError as exc:
            report["failed"].append(f"{kind}:{rel}: {type(exc).__name__}: {exc}")
            continue
        report["written"] += 1
        report["bytes"] += len(blob)
        report["by_kind"][kind] = report["by_kind"].get(kind, 0) + 1

    entry = next((b for k, _p, _m, b in items
                  if k == CONFIG and isinstance(b, (bytes, bytearray))), None)
    if entry is not None and cwds:
        report["config"] = _merge_config_entry(
            cwds[0], json.loads(entry.decode("utf-8")), overwrite=(when_exists == "always"))
    return report


def _merge_config_entry(cwd: str, entry: dict, overwrite=False) -> str:
    """Set one working directory's entry in ~/.claude.json without disturbing anything else.

    Written to a sibling temp file and renamed over the original, because a partial write here
    costs the OAuth session and every other project's trust state, not just this one's.
    """
    if not CONFIG_PATH.exists():
        return "no ~/.claude.json on this machine"
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"could not read ~/.claude.json: {type(exc).__name__}"
    projects = data.setdefault("projects", {})
    if str(cwd) in projects and not overwrite:
        return "left alone: this machine already has an entry for that project"
    projects[str(cwd)] = entry
    tmp = CONFIG_PATH.with_suffix(".json.c4x-tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return "merged"


def has_state(cwd: str) -> bool:
    """Whether Claude Code holds anything for this working directory.

    THE GUARD THAT KEEPS A TEST SUITE FROM DELETING A REAL PROJECT. `projects.delete` is exercised
    17 times by tests/test_projects.py against the fixture name `P:\\Alpha`, and every one of those
    ran a real `claude project purge P:\\Alpha --yes` on the developer's own machine. It was
    harmless only because no project of that name existed. Checking first turns the no-state case
    into a reported no-op instead of a subprocess, which is also the right answer for a real
    caller: `claude project purge` exits 1 on a path it holds nothing for.
    """
    return project_dir(cwd).is_dir() or config_entry(cwd) is not None


def purge(cwd: str, dry_run=False):
    """Delete Claude Code's own state for a working directory, THROUGH ITS OWN COMMAND.

    `claude project purge <path>` is used rather than a hand-rolled sweep because it is the only
    thing that knows the full footprint for the build it ships with, and that footprint is not
    fixed: it prints two items for one project here and four for another, the difference being a
    `tasks/<session id>` directory per session.

    Returns the command's own plan and exit code. The caller decides what a non-zero means; this
    does not guess, because "no state for that path" and "the command is missing" both need saying.
    """
    if not has_state(cwd):
        return {"ran": False, "reason": "Claude Code holds no state for that path", "code": None,
                "output": ""}
    exe = shutil.which("claude")
    if not exe:
        return {"ran": False, "reason": "the claude CLI is not on PATH", "code": None,
                "output": ""}
    argv = [exe, "project", "purge", str(cwd), "--dry-run" if dry_run else "--yes"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}", "code": None, "output": ""}
    return {"ran": True, "reason": None, "code": done.returncode,
            "output": (done.stdout or "") + (done.stderr or "")}

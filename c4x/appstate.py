"""The state CLAUDE CODE keeps for a project, which c4x's own store is not.

WHY THIS EXISTS. `export` wrote a complete copy of what c4x had PARSED and nothing of what Claude
Code itself holds, so an imported project appeared in c4x and was invisible to the desktop app.
Measured on a second machine: an import touched exactly one file, `context.db`, and left
`~/.claude/projects`, `~/.claude.json`, `history.jsonl` and `settings.json` byte-identical. A disk
clone moves the project because it copies those files; c4x moved one of them, so it did not.

WHAT IS ACTUALLY PER-PROJECT, measured on Claude Code 2.1.263 rather than taken from the docs,
which name five locations for this build's two. `claude project purge --dry-run` is the authority
and it prints exactly two items:

    dir:    ~/.claude/projects/<slug>      transcripts (.jsonl) and memory/
    config: ~/.claude.json projects[path]  trust, history, MCP servers

and says in its own output that `shell-snapshots/` are NOT project-scoped. Checked here:
`history.jsonl` keys its lines on the directory a prompt was TYPED in, not on the project, and
`tasks/`, `todos/`, `debug/` and `file-history/` are empty. The docs' five-location list is
therefore wrong for this build, which is why this module was written from the tool's own plan.

The config entry is the half people forget, and it is the half that carries TRUST. 52 of the 91
project entries on the machine this was written on hold `hasTrustDialogAccepted`, so a project
restored without it is a project the app will interrogate the user about again.
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

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
CONFIG_PATH = HOME / ".claude.json"


def slug_for(project: str) -> str:
    """The transcript folder name Claude Code derives from a working directory.

    Every character that is not a letter or a digit becomes a hyphen, so `P:\\Skills` is
    `P--Skills`. Verified against the real directory rather than inferred: the purge plan for
    `P:\\Skills` names `~/.claude/projects/P--Skills`.
    """
    return "".join(ch if ch.isalnum() else "-" for ch in str(project))


def project_dir(project: str) -> Path:
    return CLAUDE_DIR / "projects" / slug_for(project)


def config_entry(project: str):
    """That project's entry in ~/.claude.json, or None.

    Read as JSON and returned as a plain dict: the caller stores it, and nothing here goes near the
    rest of that file, which holds the OAuth session.
    """
    if not CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data.get("projects") or {}).get(str(project))


def desktop_records(session_ids, with_mtime=False):
    """The DESKTOP APP's own record for each of these sessions, as (relative path, bytes).

    One `local_<uuid>.json` per chat, holding `cwd`, `isArchived`, `originCwd` and the
    `cliSessionId` that is what this store calls `session_id`. Deleting a project without these
    leaves the app still listing the chat; restoring without them leaves the copy unarchivable and
    unnamed on the far side.

    IDENTIFIED BY store.read_archived_record, NOT by re-parsing. That function already knows the
    shape, already reads a bounded prefix before falling back to a full parse, and already knows
    that the same directory holds `scheduled-tasks.json` files which are not session records. A
    second parser here would be a second answer to the same question.
    """
    import glob

    from c4x import store

    wanted = {str(s) for s in session_ids}
    root = Path(store.sessions_root())
    out = []
    if not root.is_dir():
        return out
    for path in glob.glob(str(root / "*" / "*" / "*.json")):
        row = store.read_archived_record(path)
        if row is None or str(row[0]) not in wanted:
            continue
        p = Path(path)
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        try:
            blob = p.read_bytes()
        except OSError:
            blob = None
        out.append((rel, _stat(p), blob) if with_mtime else (rel, blob))
    return out


def cwds_for(session_ids):
    """The WORKING DIRECTORIES those sessions ran in, which is what Claude Code keys its state on.

    NOT the project label. `session_rows()` appends `\\archived` to a label once the desktop app
    has archived the chat, so a caller holding `P:\\Skills\\archived` and asking for its transcript
    folder gets `P--Skills--archived`, which does not exist, and its config entry is looked up
    under a path nothing uses. Both come back empty and the export silently carries neither.

    Measured while wiring this: an export keyed on the label carried the desktop record, whose own
    lookup goes through session ids, and neither of the two that go through the path.
    """
    from c4x import store

    if not session_ids:
        return []
    ids = [str(s) for s in session_ids]
    marks = ",".join("?" * len(ids))
    frame = store.q(f"SELECT DISTINCT cwd FROM sessions WHERE session_id IN ({marks}) "
                    "AND cwd IS NOT NULL AND cwd <> ''", tuple(ids))
    return [str(c) for c in frame["cwd"].tolist()] if not frame.empty else []


def capture(project: str, session_ids=()):
    """Every Claude Code file this project owns, as (kind, relative path, bytes).

    `project` may be a page label; the working directories are resolved from the SESSIONS, because
    that is what Claude Code keys its own state on. Returns a LIST rather than writing anything, so
    the caller decides where it lands and the same function serves an export, a dry run and a
    report.
    """
    # (kind, path, mtime, bytes). THE MTIME COMES BACK WITH THE BYTES, from the same stat of
    # the same resolved path. Looking it up afterwards through a second function took the
    # LABEL again, resolved a directory that does not exist, and recorded None, which
    # silently turned the caller's newer-wins policy into never-overwrite for every
    # transcript. Third time that label-for-cwd substitution went wrong in this file.
    items = []
    for real in cwds_for(session_ids) or [project]:
        items.extend(_capture_one(real))
    for rel, stamp, blob in desktop_records(session_ids, with_mtime=True):
        items.append((DESKTOP, rel, stamp, blob))
    return items


def _stat(path):
    """The file's mtime, or None when it cannot be read. Never raises into a capture."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _capture_one(project: str):
    """Layers 2 for ONE working directory: its transcript folder and its config entry."""
    items = []
    root = project_dir(project)
    if root.is_dir():
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = Path(base) / name
                rel = str(path.relative_to(root)).replace(os.sep, "/")
                try:
                    items.append((TRANSCRIPT, rel, _stat(path), path.read_bytes()))
                except OSError:
                    # NAMED, not skipped in silence. A transcript this cannot read is a hole in
                    # the export and the caller has to be able to say so.
                    items.append((TRANSCRIPT, rel, None, None))
    entry = config_entry(project)
    if entry is not None:
        items.append((CONFIG, "claude.json", _stat(CONFIG_PATH),
                      json.dumps(entry).encode("utf-8")))
    return items


def summarise(items):
    """What a captured bundle holds, for a report or a manifest."""
    readable = [i for i in items if i[3] is not None]
    return {
        "transcripts": sum(1 for k, _p, _m, _b in readable if k == TRANSCRIPT),
        "transcript_bytes": sum(len(b) for k, _p, _m, b in readable if k == TRANSCRIPT),
        "config_entry": any(k == CONFIG for k, _p, _m, _b in readable),
        "desktop_records": sum(1 for k, _p, _m, _b in readable if k == DESKTOP),
        "unreadable": [p for _k, p, _m, b in items if b is None],
    }


def restore(project: str, items, when_exists="newer"):
    """Write a captured bundle back into THIS machine's Claude Code state.

    `when_exists` decides what happens to a file already on disk:

      "newer" (default)  take the incoming file when its recorded mtime is later
      "never"            never replace anything; report every collision instead
      "always"           replace unconditionally

    THE SHORTER-BUT-NEWER CASE IS REPORTED, NOT PREVENTED. A transcript can be rewritten IN PLACE
    and come out smaller: `performCompactTranscript` writes a sibling temp file and renames it over
    the original, so a compacted transcript is newer AND shorter, and "newer wins" then replaces a
    complete record with a compacted one. That is a real way to lose conversation, it is not
    detectable from the bytes alone, and the policy here is deliberate rather than accidental, so
    every such replacement is listed under `newer_but_shorter` for a reader to see.

    The config entry is MERGED, never written over the file: `~/.claude.json` holds the OAuth
    session and every other project, so this reads it, sets one key under `projects`, and writes it
    back through a temporary file so an interrupted write cannot truncate it.
    """
    from c4x import store

    report = {"written": 0, "bytes": 0, "kept_existing": [], "replaced": [],
              "newer_but_shorter": [], "rejected_paths": [], "desktop_written": 0,
              "config": "not present"}
    roots = {TRANSCRIPT: project_dir(project), DESKTOP: Path(store.sessions_root())}

    for kind, rel, incoming, blob in items:
        if blob is None or kind not in roots:
            continue
        target = _contained(roots[kind], rel)
        if target is None:
            # AN EXPORT IS UNTRUSTED INPUT. It is a file that arrives from another machine, and
            # this function writes whatever paths it names. `../../../../evil.txt` under the
            # transcript root resolved to C:\Users\evil.txt on the machine this was written on,
            # which is arbitrary file write out of a data file. Adding the restore capability is
            # what made a harmless stored string dangerous.
            report["rejected_paths"].append(f"{kind}:{rel}")
            continue
        if target.exists():
            existing = target.stat().st_mtime
            take = (when_exists == "always"
                    or (when_exists == "newer" and incoming is not None and incoming > existing))
            if not take:
                report["kept_existing"].append(rel)
                continue
            if len(blob) < target.stat().st_size:
                # The one case the chosen policy gets wrong, named rather than hidden.
                report["newer_but_shorter"].append(
                    f"{rel} ({target.stat().st_size} -> {len(blob)} bytes)")
            report["replaced"].append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        report["bytes"] += len(blob)
        if kind == DESKTOP:
            report["desktop_written"] += 1
        else:
            report["written"] += 1

    entry = next((b for k, _p, _m, b in items if k == CONFIG and b is not None),
                 None)
    if entry is not None:
        report["config"] = _merge_config_entry(
            project, json.loads(entry.decode("utf-8")), overwrite=(when_exists == "always"))
    return report


def _contained(root: Path, rel: str):
    """`root / rel` when it genuinely stays under root, else None.

    Compared after RESOLVING both, because the check has to answer where the write actually
    lands, not what the string looks like: a prefix test on the raw text passes `..` and
    fails a legitimate path through a symlinked home. An absolute path in the export is
    rejected by the same test, since it resolves outside root on its own.
    """
    try:
        target = (root / rel).resolve()
        base = root.resolve()
    except OSError:
        return None
    return target if target == base or base in target.parents else None


def _merge_config_entry(project: str, entry: dict, overwrite=False) -> str:
    """Set one project's entry in ~/.claude.json without disturbing anything else.

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
    if str(project) in projects and not overwrite:
        return "left alone: this machine already has an entry for that project"
    projects[str(project)] = entry
    tmp = CONFIG_PATH.with_suffix(".json.c4x-tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return "merged"


def purge(project: str, dry_run=False):
    """Delete Claude Code's own state for a project, THROUGH ITS OWN COMMAND.

    `claude project purge <path>` is used rather than a hand-rolled sweep because it is the only
    thing that knows the full footprint for the build it ships with, and because the footprint is
    not stable: this build deletes two items where the documentation describes five. A sweep
    written here would be correct until the next release and wrong silently after it.

    Returns the command's own plan and exit code. The caller decides what a non-zero means; this
    does not guess, because "no state for that path" and "the command is missing" both need saying
    rather than swallowing.
    """
    exe = shutil.which("claude")
    if not exe:
        return {"ran": False, "reason": "the claude CLI is not on PATH", "code": None,
                "output": ""}
    argv = [exe, "project", "purge", str(project), "--dry-run" if dry_run else "--yes"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}", "code": None, "output": ""}
    return {"ran": True, "reason": None, "code": done.returncode,
            "output": (done.stdout or "") + (done.stderr or "")}

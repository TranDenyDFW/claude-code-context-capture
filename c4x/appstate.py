"""The five kinds of Claude Code state that live OUTSIDE the store, captured and restored.

c4x's export carries what c4x parsed. Claude Code itself holds the rest, and without them
an imported project is a set of rows: visible in c4x, invisible in the desktop app, unopenable by
`/resume`.

    transcripts   ~/.claude/projects/<slug>/<session id>*  and  <session id>/**
    memory        ~/.claude/projects/<slug>/memory/**
    tasks         ~/.claude/tasks/<session id>/**
    config        ~/.claude.json  projects[<cwd>]      (this is `hasTrustDialogAccepted`)
    desktop       %APPDATA%/Claude/claude-code-sessions/<account>/<org>/local_<uuid>.json

EVERY PATH HERE IS RELATIVE TO A NAMED ROOT, and the roots are resolved on the machine doing the
restoring. Carrying an absolute path would make an import reproduce the EXPORTER's own user
profile,
which is a broken import rather than a faithful one. The measurements behind the desktop layer,
including why its two directory levels cannot be copied across machines, are in
`docs/desktop-records.md`.

A PROJECT IS A WORKING DIRECTORY AND NEVER A LABEL. `store.session_rows()` appends `\\archived` to
a project's label when the desktop app has archived the chat, and passing that label where a
working directory belongs has gone wrong six times, three of them silently, twice past gates
written to stop it. The last time it was `restore("P:\\Skills\\archived", ...)`, which wrote into
`~/.claude/projects/P` and reported success, because a `str` satisfies `list[str]`: it is an
iterable of `str`. So `_cwds()` raises TypeError on a bare string and is called at the top of every
public function here.
"""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Resolved on THIS machine, at import, and never read from an export.
CLAUDE_DIR = Path.home() / ".claude"
CONFIG_PATH = Path.home() / ".claude.json"

TRANSCRIPT, MEMORY, TASKS, CONFIG, DESKTOP = (
    "transcript", "memory", "tasks", "config", "desktop")
KINDS = (TRANSCRIPT, MEMORY, TASKS, CONFIG, DESKTOP)

# The two fields in a desktop record that name the source machine's filesystem, and the only two an
# import rewrites. Everything else in the record is carried byte for byte.
DESKTOP_CWD_FIELDS = ("cwd", "originCwd")

# What those fields are replaced with before hashing, so a record captured on one machine and
# restored onto another can still be PROVEN identical everywhere it is supposed to be identical.
REBASE_MARK = "<c4x destination working directory>"

# Windows resolves these names to devices no matter which directory they appear in. A write to
# `nul` SUCCEEDS, reports a size of 0, reads back empty, and never appears in a listing, so a
# restore that allowed one would report a file written and land nothing.
_RESERVED = {"con", "prn", "aux", "nul", "clock$"}
_RESERVED |= {f"com{n}" for n in range(1, 10)} | {f"lpt{n}" for n in range(1, 10)}

_SLUG_CHAR = re.compile(r"[^A-Za-z0-9]")


# ---------------------------------------------------------------------------
# The boundary that stops a label being used as a directory
# ---------------------------------------------------------------------------
def _cwds(value):
    """Working directories, as a list. A bare string is refused rather than iterated.

    `list[str]` is a hint and hints do not run. This does.
    """
    if isinstance(value, (str, bytes, os.PathLike)):
        raise TypeError(
            "cwds is a LIST of working directories, not one path and never a project label; "
            f"got {type(value).__name__} {value!r}")
    out = [str(c) for c in value]
    for cwd in out:
        if not cwd.strip():
            raise ValueError("a working directory cannot be empty")
    return out


def _mapping(value):
    """{source working directory: destination working directory}, guarded on BOTH halves.

    A dict, never a name. `_cwds(mapping.keys())` alone does not stop a string: `"P:\\A".keys()`
    raises AttributeError, which reads as a broken caller rather than the label substitution this
    guard exists to name.
    """
    if not isinstance(value, dict):
        raise TypeError(
            "mapping is a dict of {source working directory: destination working directory}, "
            f"not {type(value).__name__} {value!r}")
    _cwds(list(value.keys()))
    _cwds(list(value.values()))
    return dict(value)


def slug_for(cwd):
    """The directory name Claude Code gives a working directory under ~/.claude/projects.

    Every character that is not a letter or a digit becomes a hyphen, so `P:\\Skills` is
    `P--Skills`.

    `tests/test_appstate.py::TestTheSlugAgainstTheRealMachine` pins this against the directories
    Claude Code actually created, by resolving `~/.claude.json`'s own project keys and requiring
    that every one with a directory maps onto it. It SKIPS where there is nothing to compare, so a
    fresh checkout is not told its rule is wrong; that sentence used to claim the test existed
    when it did not, which an independent sweep caught.
    """
    if not isinstance(cwd, str):
        cwd = str(cwd)
    if not cwd.strip():
        raise ValueError("a working directory cannot be empty")
    return _SLUG_CHAR.sub("-", cwd)


def project_dir(cwd):
    return CLAUDE_DIR / "projects" / slug_for(cwd)


def tasks_dir():
    return CLAUDE_DIR / "tasks"


def sessions_root():
    """Where the desktop app keeps its records, as THIS module sees it.

    A thin delegate to `store.sessions_root()` and not a duplicate of it: the resolution rule, which
    has to tell a Store install's package container from `%APPDATA%`, lives in one place.

    It exists so the suite can isolate what this module WRITES without changing what the rest of
    the app READS. Patching `store.sessions_root` did both, and the difference was not academic:
    `store.archived_sessions()` feeds the `\\archived` marker on every session row, so pointing the
    store's root at an empty directory silently removed that marker from the whole suite, and the
    one test that checks the marker against the real records failed with an empty dict.
    """
    from c4x import store
    return store.sessions_root()


def read_config():
    """`~/.claude.json`, parsed. Raises rather than pretending an unreadable file is an empty one.

    THIS DESTROYED A REAL CONFIG. `_merge_config` read the file inside a
    `try/except (OSError, ValueError): config = {}`, then wrote its result back, so any file it
    could not parse was REPLACED by one containing just the project being imported. Measured on the
    test laptop: 26 project entries became 1, and the only reason all but two came back is that
    Claude Code keeps its own `.claude.json.backup`.

    The trigger was not exotic. A file written by Windows PowerShell's `Set-Content -Encoding UTF8`
    carries a byte order mark, `json.loads` raises `ValueError` on it, and the fallback did the
    rest. So this reads `utf-8-sig`, which accepts a BOM instead of dying on one, and a file that
    still will not parse stops the import instead of overwriting a file full of other projects'
    settings and this machine's credentials.

    A MISSING file is different and is not an error: that is a fresh machine, and `{}` is the
    truthful answer.
    """
    if not CONFIG_PATH.exists():
        return {}
    raw = CONFIG_PATH.read_text(encoding="utf-8-sig")
    try:
        config = json.loads(raw)
    except ValueError as exc:
        raise ValueError(
            f"{CONFIG_PATH} exists and does not parse as JSON ({exc}). Refusing to touch it: it "
            "holds every other project's settings and this machine's credentials, and writing a "
            "fresh one here would destroy them.") from exc
    if not isinstance(config, dict):
        raise ValueError(f"{CONFIG_PATH} is not a JSON object, so it is not a Claude Code config")
    return config


def destination_cwd(row_cwd, mapping):
    """Where a row belongs, resolved the way two spellings of one directory are the same directory.

    NOT `mapping.get(row_cwd, row_cwd)`, which is what this was and which quietly defeated the
    whole feature. The mapping is keyed on `sessions.cwd`, and a row's `cwd` comes from three
    different places: a transcript's is that same value, a CONFIG row's is the raw
    `~/.claude.json` key, and a DESKTOP row's is the string inside the record. Those do not have to
    agree character for character, and `normalised()` exists in this module precisely because they
    do not: 4 of the 91 projects on this machine carry both slash spellings.

    Measured on a fixture where the config key and the record both said `P:/FakeSrc/Proj` while
    `sessions.cwd` said `P:\\FakeSrc\\Proj`: the transcript moved to the destination, the config
    gained the EXPORTER's key, the chat kept pointing at the exporter's directory, and
    `verify_mirror` returned ok. An import that writes another machine's absolute path into this
    user's config is the exact outcome this feature exists to prevent.

    A row under a mapped directory moves with it, so `P:\\Proj\\sub` follows `P:\\Proj`.
    Anything that matches nothing is returned unchanged, and callers report it.
    """
    if row_cwd in mapping:
        return mapping[row_cwd]
    target = normalised(row_cwd)
    for source, dest in mapping.items():
        if normalised(source) == target:
            return dest
    for source, dest in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        prefix = normalised(source) + "\\"
        if target.startswith(prefix):
            tail = str(row_cwd).replace("/", "\\")[len(str(source)):].lstrip("\\")
            return str(dest).rstrip("\\") + "\\" + tail
    return row_cwd


def normalised(cwd):
    """A working directory reduced to what makes two spellings the same directory.

    `~/.claude.json` holds both slash spellings for 4 of the 91 projects on this machine, and an
    export that carried only the exact string would leave the other entry behind: the imported
    project would then ask to be trusted again the first time it is opened by the spelling that
    was not carried.
    """
    text = str(cwd).replace("/", "\\").rstrip("\\")
    return text.casefold()


# ---------------------------------------------------------------------------
# Hashes, which are what make "mirror image" a checkable claim
# ---------------------------------------------------------------------------
def sha256_bytes(blob):
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def canonical_json(obj):
    """Bytes that depend on the CONTENT of a JSON object and not on how it was written."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def rebase_marked(blob, fields=DESKTOP_CWD_FIELDS):
    """The same record with its cwd fields neutralised, as canonical bytes.

    A desktop record and a config entry are the two things an import deliberately CHANGES, so their
    bytes cannot match the source and a plain sha256 would report every correct import as a
    mismatch. Neutralising exactly the fields that are rewritten leaves everything else under the
    hash, which is the part the mirror actually promises.

    Returns None when the blob is not JSON, so the caller reports it rather than guessing.
    """
    try:
        record = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    marked = dict(record)
    for field in fields:
        if field in marked:
            marked[field] = REBASE_MARK
    return canonical_json(marked)


# ---------------------------------------------------------------------------
# Where the desktop app files a session on THIS machine
# ---------------------------------------------------------------------------
def desktop_pair(root=None):
    """(account uuid, org uuid) for the pair the desktop app is currently writing, or None.

    THE TWO DIRECTORY LEVELS ARE THE MACHINE'S, NOT THE PROJECT'S. One pair on this machine holds
    171 records covering 57 different working directories, and 6 working directories appear under
    two pairs, so nothing about a project can produce them. Reproducing the source machine's pair
    files the record under an account uuid the destination app never reads, which is the exact
    failure that makes an import invisible.

    Three independent sources are consulted and the answer says which agreed:

        config.json lastKnownAccountUuid          the account
        plan-usage-history.json newest org sample the organisation
        the newest local_*.json already on disk   both, as the app itself last used them

    THE ACCOUNT IS DECIDED FIRST AND DISK ONLY PICKS THE ORGANISATION UNDER IT. Letting the newest
    record on disk win outright looks reasonable and is wrong: this machine carries a second
    account whose newest record is 12 days stale, and an import filed under it would be invisible
    to the signed-in user while every count said it had been written. So a record directory is
    used only when `config.json` names no account or names that one.

    When nothing on disk sits under the signed-in account, the organisation comes from
    `plan-usage-history.json` and the directory is created. When there is no evidence at all, this
    returns None and the caller REPORTS that rather than inventing a directory the app will ignore.
    """
    root = Path(root or sessions_root())
    appdata = root.parent                                    # %APPDATA%/Claude
    account = org = None
    try:
        config = json.loads((appdata / "config.json").read_text(encoding="utf-8"))
        account = config.get("lastKnownAccountUuid") or None
    except (OSError, ValueError):
        pass
    try:
        history = json.loads((appdata / "plan-usage-history.json").read_text(encoding="utf-8"))
        samples = [s for s in (history.get("samples") or [])
                   if isinstance(s, dict) and s.get("org")]
        if samples:
            org = max(samples, key=lambda s: s.get("t") or 0)["org"]
    except (OSError, ValueError):
        pass

    newest: dict[tuple[str, str], float] = {}
    if root.is_dir():
        for path in root.glob("*/*/local_*.json"):
            pair = (path.parent.parent.name, path.parent.name)
            try:
                when = path.stat().st_mtime
            except OSError:
                continue
            if when > newest.get(pair, 0):
                newest[pair] = when
    usable = {p: t for p, t in newest.items() if account is None or p[0] == account}
    if usable:
        chosen = max(usable, key=lambda p: usable[p])
        return {"account": chosen[0], "org": chosen[1],
                "source": ("the newest record on disk under the signed-in account" if account
                           else "the newest record on disk; config.json names no account"),
                "config_account": account, "config_org": org}
    if account and org:
        note = ("this machine has no records under the signed-in account yet"
                if newest else "this machine has no records yet")
        return {"account": account, "org": org,
                "source": f"config.json and plan-usage-history.json; {note}",
                "config_account": account, "config_org": org}
    return None


def desktop_dir(root=None):
    """The directory a restored desktop record belongs in, or None when it cannot be determined."""
    pair = desktop_pair(root)
    if not pair:
        return None
    return Path(root or sessions_root()) / pair["account"] / pair["org"]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def _row(kind, cwd, relpath, mtime, blob, rebase_fields=()):
    marked = rebase_marked(blob, rebase_fields) if rebase_fields else None
    return {"kind": kind, "cwd": cwd, "relpath": relpath, "mtime": float(mtime),
            "sha256": sha256_bytes(blob),
            "rebased_sha256": sha256_bytes(marked) if marked else sha256_bytes(blob),
            "blob": blob}


def _walk(base, entry, kind, cwd, rows, skipped):
    """Every file under `entry`, as rows relative to `base`. Symlinks are named, never followed."""
    stack = [entry]
    while stack:
        item = stack.pop()
        try:
            if item.is_symlink():
                skipped.append({"path": str(item), "why": "symlink, not followed"})
                continue
            if item.is_dir():
                stack.extend(sorted(item.iterdir()))
                continue
            blob = item.read_bytes()
            mtime = item.stat().st_mtime
        except OSError as exc:
            skipped.append({"path": str(item), "why": f"unreadable: {exc.strerror or exc}"})
            continue
        rows.append(_row(kind, cwd, str(item.relative_to(base)).replace("\\", "/"), mtime, blob))


def _count_files(entry):
    if entry.is_file():
        return 1
    total = 0
    for _dirpath, _dirs, files in os.walk(entry):
        total += len(files)
    return total


def capture(cwds, session_ids, sessions_root=None, sink=None):
    """Everything the four layers hold for these working directories and sessions.

    Returns (rows, report). A row is a dict with kind, cwd, relpath, mtime, sha256, rebased_sha256
    and blob, and `relpath` is ALWAYS relative to the root its kind names.

    THE CAPTURE RULE IS "EVERY NAME THAT BEGINS WITH A SESSION ID, PLUS memory/", and it is
    measured rather than assumed. Every entry in a slug directory is one of four shapes, counted
    over three real directories: 106 `<session id>.jsonl`, 55 `<session id>/` directories holding
    subagents, tool results and workflows at 22 to 137 MB each, 2 `memory/`, and 1
    `<session id>.desktop-released.json`. A previous attempt captured the `.jsonl` alone and left
    326 MB of per-session directories behind.

    WHAT IS NOT CARRIED IS REPORTED. The slug directory is shared by every session with that
    working directory, so a file belonging to a session this store has no row for is real, is not
    ours to move, and is named in `not_carried` rather than dropped in silence.

    PASS A `sink` AND NOTHING IS ACCUMULATED. Every row is handed to it and dropped, and the
    returned list is empty. `_write_app_state` uses that to insert straight into the export, which
    matters at the size this reaches: an export of this repo's own project is 694.5 MB, and holding
    all of it as blobs in a list before writing any of it is a way to run a machine out of memory
    while doing nothing useful with the bytes.
    """
    from c4x import store
    cwds = _cwds(cwds)
    ids = [str(s) for s in session_ids]
    id_set = set(ids)
    skipped: list[dict] = []
    not_carried: list[dict] = []
    kept: list[dict] = []
    counted: dict[str, int] = dict.fromkeys(KINDS, 0)
    tally: dict[str, int] = {"files": 0, "bytes": 0}

    class _Rows(list):
        """Looks like the list this used to build, and keeps nothing when a sink is given."""

        def append(self, row):
            tally["files"] += 1
            tally["bytes"] += len(row["blob"])
            counted[row["kind"]] += 1
            if sink is None:
                kept.append(row)
            else:
                sink(row)

        def extend(self, more):
            for row in more:
                self.append(row)

    rows = _Rows()

    for cwd in cwds:
        base = project_dir(cwd)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_symlink():
                skipped.append({"path": str(entry), "why": "symlink, not followed"})
                continue
            if entry.name == "memory" and entry.is_dir():
                _walk(base, entry, MEMORY, cwd, rows, skipped)
            elif any(entry.name.startswith(sid) for sid in id_set):
                _walk(base, entry, TRANSCRIPT, cwd, rows, skipped)
            else:
                not_carried.append({
                    "path": str(entry), "files": _count_files(entry),
                    "why": "does not begin with a session id this export carries"})

    tasks = tasks_dir()
    for sid in ids:
        entry = tasks / sid
        if entry.is_dir() or entry.is_file():
            _walk(tasks, entry, TASKS, cwds[0] if cwds else "", rows, skipped)

    rows.extend(_capture_config(cwds, skipped))
    rows.extend(_capture_desktop(id_set, sessions_root, skipped))

    report = {
        "files": tally["files"],
        "bytes": tally["bytes"],
        "by_kind": dict(counted),
        "skipped": skipped,
        "not_carried": not_carried,
        "not_carried_files": sum(n["files"] for n in not_carried),
        "desktop_pair": desktop_pair(sessions_root),
        "source_store": str(store.DB_PATH),
    }
    return kept, report


def _capture_config(cwds, skipped):
    """Every `~/.claude.json` projects entry whose key names one of these directories."""
    try:
        config = read_config()
        mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
    except (OSError, ValueError) as exc:
        skipped.append({"path": str(CONFIG_PATH), "why": f"unreadable: {exc}"})
        return []
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return []
    wanted = {normalised(c) for c in cwds}
    rows = []
    for key, entry in projects.items():
        if normalised(key) in wanted:
            rows.append(_row(CONFIG, key, key, mtime, canonical_json(entry)))
    return rows


def _capture_desktop(id_set, root, skipped):
    """The desktop app's own record for each session, keyed by cliSessionId.

    The relative path is the FILENAME ALONE. The account and organisation directories above it
    belong to the machine, not to the project, and are resolved again on the destination.
    """
    from c4x import store
    root = Path(root or sessions_root())
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.glob("*/*/local_*.json")):
        found = store.read_archived_record(str(path))
        if not found or found[0] not in id_set:
            continue
        try:
            blob = path.read_bytes()
            mtime = path.stat().st_mtime
        except OSError as exc:
            skipped.append({"path": str(path), "why": f"unreadable: {exc.strerror or exc}"})
            continue
        try:
            cwd = json.loads(blob.decode("utf-8")).get("cwd") or ""
        except (UnicodeDecodeError, ValueError):
            skipped.append({"path": str(path),
                            "why": "not readable as JSON, so it cannot be rebased"})
            continue
        rows.append(_row(DESKTOP, cwd, path.name, mtime, blob, DESKTOP_CWD_FIELDS))
    return rows


# ---------------------------------------------------------------------------
# Where a row lands on THIS machine
# ---------------------------------------------------------------------------
def _safe_parts(relpath):
    """The parts of a relative path, or a reason it is refused.

    Refused: absolute paths, drive letters, `..`, `.`, empty names, a Windows device name, and any
    part with a trailing dot or space. Windows strips those trailing characters, so `a.jsonl.` and
    `a.jsonl` are one file and a restore that accepted both would silently write one over the other
    while reporting two.
    """
    text = str(relpath).replace("\\", "/")
    if not text.strip():
        return None, "empty"
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None, "absolute, and an export only ever carries relative paths"
    parts = [p for p in text.split("/") if p != ""]
    if not parts:
        return None, "names no file"
    for part in parts:
        if part in (".", ".."):
            return None, "contains a parent or current directory reference"
        if part != part.rstrip(". "):
            return None, f"{part!r} ends in a dot or a space, which Windows strips"
        if part.split(".")[0].casefold() in _RESERVED:
            return None, f"{part!r} is a Windows device name; a write to it lands nothing"
    return parts, None


def destination(row, dest_cwd, sessions_root=None):
    """(path, refusal). The path this row is written to on this machine, or why it is refused."""
    kind = row["kind"]
    parts: list[str] | None = None
    if kind in (TRANSCRIPT, MEMORY, TASKS, DESKTOP):
        parts, why = _safe_parts(row["relpath"])
        if why or parts is None:
            return None, f"{row['relpath']!r}: {why}"
    if kind in (TRANSCRIPT, MEMORY):
        base = project_dir(dest_cwd)
    elif kind == TASKS:
        base = tasks_dir()
    elif kind == DESKTOP:
        base = desktop_dir(sessions_root)
        if base is None:
            return None, ("this machine has no desktop account and organisation to file a record "
                          "under, so the session would not appear in the app")
        if parts is None or len(parts) != 1:
            return None, f"{row['relpath']!r}: a desktop record is carried as a filename alone"
    elif kind == CONFIG:
        return CONFIG_PATH, None
    else:
        return None, f"unknown kind {kind!r}"

    if parts is None:              # unreachable: CONFIG is the only kind that leaves it unset,
        return None, f"{kind!r} carries no relative path"    # and CONFIG returned above
    path = base.joinpath(*parts)
    try:
        resolved = path.resolve()
        root = base.resolve()
    except OSError as exc:
        return None, f"{row['relpath']!r}: could not be resolved: {exc}"
    if resolved != root and root not in resolved.parents:
        return None, f"{row['relpath']!r}: resolves outside {root}"
    return path, None


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def restore(rows, mapping, sessions_root=None, dry_run=False):
    """Write every row onto this machine, under the destination each cwd maps to.

    `mapping` is {source working directory: destination working directory}. It is a dict and never
    a name, and `_cwds` guards both halves of it.

    SOURCE ALWAYS WINS. A destination file that already exists is replaced, so the result is
    byte-identical to the export. Newer-wins was tried and dropped: it makes "complete mirror"
    unprovable, since a file the policy declined to write is indistinguishable from one it failed
    to write. The one case this gets wrong is REPORTED rather than prevented: a compacted
    transcript is shorter than the one it replaced, so every write that shrinks a file is named in
    `replaced_shorter`.

    NOTHING IS DELETED. The slug directory is shared by every session with that working directory,
    so removing what the export does not carry would take another session's transcript with it.

    The order below is the order, because order was the defect last time. A type check after the
    containment check lets an export with a TEXT mtime verify clean, write rows, and only then
    raise; a collision check after the exists branch reports the second of two colliding rows as
    "kept existing" and never names it.
    """
    mapping = _mapping(mapping)
    report: dict[str, Any] = {
        "written": [], "replaced": [], "replaced_shorter": [], "refused": [],
        "config_keys": [], "config_spellings_merged": [], "desktop": [], "bytes": 0,
        "dry_run": bool(dry_run)}

    # 1. TYPES, before anything is opened.
    for row in rows:
        if not isinstance(row.get("blob"), (bytes, bytearray)):
            raise TypeError(f"{row.get('relpath')!r}: blob is "
                            f"{type(row.get('blob')).__name__}, not bytes")
        if isinstance(row.get("mtime"), bool) or not isinstance(row.get("mtime"), (int, float)):
            raise TypeError(f"{row.get('relpath')!r}: mtime is "
                            f"{type(row.get('mtime')).__name__}, not a number")
        if row.get("kind") not in KINDS:
            raise ValueError(f"{row.get('relpath')!r}: unknown kind {row.get('kind')!r}")

    # 2. CONTAINMENT, and 3. COLLISION, both decided before a single byte is written.
    planned: list = []
    seen: dict[str, str] = {}
    for row in rows:
        dest_cwd = destination_cwd(row["cwd"], mapping)
        path, refusal = destination(row, dest_cwd, sessions_root)
        if refusal:
            report["refused"].append({"relpath": row["relpath"], "kind": row["kind"],
                                      "why": refusal})
            continue
        if row["kind"] != CONFIG:
            key = str(path).casefold()
            if key in seen:
                raise ValueError(
                    f"two rows resolve to one file on this machine: {seen[key]!r} and "
                    f"{row['relpath']!r} both land on {path}")
            seen[key] = row["relpath"]
        planned.append((row, path, dest_cwd))

    if dry_run:
        for row, path, dest_cwd in planned:
            report["written"].append({"relpath": row["relpath"], "kind": row["kind"],
                                      "path": str(path), "into": dest_cwd,
                                      "exists": path.exists()})
        return report

    # 4. WRITE, restore the mtime, then RE-HASH. `written: N` is a coverage number and coverage
    # numbers reward fabrication: a write to a Windows device name succeeds and lands nothing.
    config_rows = []
    for row, path, dest_cwd in planned:
        if row["kind"] == CONFIG:
            config_rows.append((row, dest_cwd))
            continue
        blob = bytes(row["blob"])
        if row["kind"] == DESKTOP:
            blob, refusal = _rebased_desktop(blob, dest_cwd)
            if refusal:
                report["refused"].append({"relpath": row["relpath"], "kind": DESKTOP,
                                          "why": refusal})
                continue
        existed = path.exists()
        before = path.stat().st_size if existed else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        os.utime(path, (row["mtime"], row["mtime"]))
        landed = path.read_bytes()
        expected = row["rebased_sha256"] if row["kind"] == DESKTOP else row["sha256"]
        actual = sha256_bytes(rebase_marked(landed, DESKTOP_CWD_FIELDS) or landed) \
            if row["kind"] == DESKTOP else sha256_bytes(landed)
        if actual != expected:
            raise RuntimeError(
                f"{path} was written and does not read back as what was written: "
                f"expected {expected}, found {actual}")
        report["bytes"] += len(blob)
        entry = {"relpath": row["relpath"], "kind": row["kind"], "path": str(path)}
        report["written"].append(entry)
        if existed:
            report["replaced"].append(entry)
            if before is not None and len(blob) < before:
                report["replaced_shorter"].append({**entry, "was": before, "now": len(blob)})
        if row["kind"] == DESKTOP:
            report["desktop"].append({"path": str(path), "cwd": dest_cwd})

    if config_rows:
        report["config_keys"], report["config_spellings_merged"] = _merge_config(
            config_rows, mapping)
    return report


def _rebased_desktop(blob, dest_cwd):
    """The record with its two cwd fields pointed at this machine's directory."""
    try:
        record = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"not readable as JSON, so it cannot be rebased: {exc}"
    if not isinstance(record, dict):
        return None, "is not a JSON object, so it is not a desktop record"
    for field in DESKTOP_CWD_FIELDS:
        if field in record:
            record[field] = dest_cwd
    return json.dumps(record, ensure_ascii=False).encode("utf-8"), None


def config_winners(config_rows, mapping):
    """One entry per destination key, chosen the same way everywhere, and the ones it displaced.

    TWO SPELLINGS OF ONE DIRECTORY ARE ONE DIRECTORY, so once the destination is resolved properly
    both of a project's `~/.claude.json` keys land on the same key, and their entries need not be
    equal. That is a genuine conflict, not a detail: last-one-wins would let the answer depend on
    dict order, and the mirror check would then report the loser as a difference forever.

    The winner is the spelling the STORE uses, which is the one a mapping is keyed on, because that
    is the spelling every other layer of this project already agrees on. Failing that, the first by
    sorted key, so the answer is at least the same in both places that ask.

    Returns ({destination key: row}, [{"key", "displaced", "kept"}]).
    """
    # EXACT, not normalised. Normalising is what makes both spellings collide in the first place,
    # so normalising the preference too makes both of them "preferred" and the tie falls to
    # whichever sorts first, which on these two is the forward-slash one because `/` is 0x2F and
    # `\` is 0x5C. The point of the preference is to pick the spelling the STORE uses.
    canonical = set(mapping)
    by_key: dict[str, list[dict]] = {}
    for row, dest_cwd in config_rows:
        by_key.setdefault(dest_cwd, []).append(row)
    winners, displaced = {}, []
    for dest_cwd, rows in by_key.items():
        if len(rows) == 1:
            winners[dest_cwd] = rows[0]
            continue
        preferred = [r for r in rows if r["cwd"] in canonical]
        chosen = min(preferred or rows, key=lambda r: str(r["cwd"]))
        winners[dest_cwd] = chosen
        for row in rows:
            if row is not chosen:
                displaced.append({"key": dest_cwd, "displaced": row["cwd"],
                                  "kept": chosen["cwd"]})
    return winners, displaced


def _merge_config(config_rows, mapping):
    """Put each carried entry under its DESTINATION key in `~/.claude.json`, touching nothing else.

    Read, set one key, write to a temporary file beside the real one and replace. The file holds
    this machine's credentials and every other project's settings, so it is never rewritten from
    anything but its own current contents, and `read_config()` RAISES rather than handing back an
    empty dict for a file it could not parse. That fallback existed here and destroyed 26 project
    entries on the test laptop.

    A copy is kept beside it before the replace, so even a correct write is undoable.
    """
    config = read_config()
    projects = config.setdefault("projects", {})
    winners, displaced = config_winners(config_rows, mapping)
    keys = []
    for dest_cwd, row in winners.items():
        try:
            entry = json.loads(bytes(row["blob"]).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        projects[dest_cwd] = entry
        keys.append(dest_cwd)
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".c4x-before"))
    temporary = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".c4x-import")
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.replace(temporary, CONFIG_PATH)
    return sorted(keys), displaced


# ---------------------------------------------------------------------------
# The proof
# ---------------------------------------------------------------------------
def compare(rows, mapping, sessions_root=None):
    """What is on this machine, against what the export says should be. The mirror check.

    `missing` is carried and absent. `differs` is carried and a different hash. `extra` is present
    in a slug directory this import wrote to and not in the export, which is REPORTED and never
    deleted, because that directory is shared with every other session that has the same working
    directory.
    """
    mapping = _mapping(mapping)
    missing, differs, unresolved, expected_paths = [], [], [], set()
    # THE SAME WINNER RULE THE RESTORE USED. Two spellings of one directory land on one key, so
    # only one of their entries can be there; asking about the other would report a difference no
    # import could ever clear.
    config_rows = [(row, destination_cwd(row["cwd"], mapping))
                   for row in rows if row["kind"] == CONFIG]
    winners, merged_spellings = config_winners(config_rows, mapping)
    for row in rows:
        dest_cwd = destination_cwd(row["cwd"], mapping)
        path, refusal = destination(row, dest_cwd, sessions_root)
        if refusal:
            unresolved.append({"relpath": row["relpath"], "why": refusal})
            continue
        if row["kind"] == CONFIG:
            if winners.get(dest_cwd) is not row:
                continue
            if not _config_matches(row, dest_cwd):
                differs.append({"relpath": dest_cwd, "kind": CONFIG,
                                "why": "the entry under this key is not the one exported"})
            continue
        expected_paths.add(str(path).casefold())
        if not path.exists():
            missing.append({"relpath": row["relpath"], "kind": row["kind"], "path": str(path)})
            continue
        landed = path.read_bytes()
        if row["kind"] == DESKTOP:
            actual = sha256_bytes(rebase_marked(landed, DESKTOP_CWD_FIELDS) or landed)
            wanted = row["rebased_sha256"]
            # AND THE TWO FIELDS THE HASH DELIBERATELY IGNORES. Neutralising them is what lets a
            # rebased record be compared at all, and it left the one field an import REWRITES as
            # the one field the acceptance test could not see: a record pointed at
            # `Q:\\nowhere` passed. The hash covers everything the import must not change; this
            # covers the little it must.
            for field, value in _desktop_cwd_fields(landed).items():
                if value != dest_cwd:
                    differs.append({
                        "relpath": row["relpath"], "kind": DESKTOP, "path": str(path),
                        "why": f"{field} is {value!r}, not the destination {dest_cwd!r}"})
        else:
            actual = sha256_bytes(landed)
            wanted = row["sha256"]
        if actual != wanted:
            differs.append({"relpath": row["relpath"], "kind": row["kind"], "path": str(path),
                            "expected": wanted, "found": actual})

    extra = []
    for dest_cwd in sorted(set(mapping.values())):
        base = project_dir(dest_cwd)
        if not base.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = os.path.join(dirpath, name)
                if full.casefold() not in expected_paths:
                    extra.append(full)
    return {"ok": not missing and not differs and not unresolved,
            "missing": missing, "differs": differs, "extra": sorted(extra),
            "unresolved": unresolved, "config_spellings_merged": merged_spellings}


def _desktop_cwd_fields(blob):
    """{field: value} for the working-directory fields a landed record carries.

    Only the fields that are actually present: a record without `originCwd` is not a record with a
    wrong `originCwd`, and reporting one would make the check fail on a shape the app itself
    writes.
    """
    try:
        record = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {field: record[field] for field in DESKTOP_CWD_FIELDS if field in record}


def _config_matches(row, dest_cwd):
    try:
        config = read_config()
    except (OSError, ValueError):
        return False
    entry = (config.get("projects") or {}).get(dest_cwd)
    if entry is None:
        return False
    return sha256_bytes(canonical_json(entry)) == row["sha256"]


# ---------------------------------------------------------------------------
# Checks that need no store and no fixture
# ---------------------------------------------------------------------------
def self_test():
    cases: list[tuple[str, bool]] = []

    def check(what, ok):
        cases.append((what, bool(ok)))

    try:
        _cwds("P:\\Skills\\archived")
        check("a bare string is refused where a list of directories belongs", False)
    except TypeError:
        check("a bare string is refused where a list of directories belongs", True)
    check("a list of directories is accepted", _cwds(["P:\\Skills"]) == ["P:\\Skills"])
    check("a slug replaces every non-alphanumeric character",
          slug_for("P:\\Skills") == "P--Skills")
    check("both slash spellings normalise to one directory",
          normalised("P:/Skills") == normalised("P:\\Skills\\"))

    # `candidate`, not `bad`: `bad` is the failure COUNTER below, and shadowing it here made the
    # counter start life as a string. mypy caught it; nothing else would have, because the loop
    # ends before the counter is used.
    for candidate, why in (("../evil.txt", "parent reference"),
                           ("C:\\evil.txt", "absolute"),
                           ("nul", "device name"),
                           ("a.jsonl.", "trailing dot"),
                           ("", "empty")):
        check(f"a relative path that is {why} is refused", _safe_parts(candidate)[0] is None)
    check("an ordinary relative path is accepted",
          _safe_parts("abc-def/subagents/x.json")[0] == ["abc-def", "subagents", "x.json"])

    blob = json.dumps({"cwd": "P:\\A", "originCwd": "P:\\A", "title": "t"}).encode()
    other = json.dumps({"originCwd": "D:\\B", "cwd": "D:\\B", "title": "t"}).encode()
    check("two desktop records differing only in cwd hash the same once rebased",
          rebase_marked(blob) == rebase_marked(other))
    changed = json.dumps({"cwd": "P:\\A", "originCwd": "P:\\A", "title": "OTHER"}).encode()
    check("a record differing in anything else does NOT hash the same",
          rebase_marked(blob) != rebase_marked(changed))

    bad = 0
    for what, ok in cases:
        if not ok:
            bad += 1
            print(f"  FAIL  {what}")
    print(f"SELF-TEST {'PASS' if not bad else 'FAIL'} ({len(cases)} checks)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(self_test())

r"""Show every chat to every account signed into this machine, or keep them separate.

THE SEPARATION IS A DIRECTORY, and that is the whole of it. The desktop app keeps one record per
chat at

    <records root>\<account uuid>\<organisation uuid>\local_<uuid>.json

and the directory listing IS the session list (`docs/desktop-records.md` §2). An account sees what
is in its own pair and nothing else, so the same machine holds several chat histories that never
meet. Measured on the author's machine: four account directories, nine pairs, 171 records under one
pair and 16 under another.

`share_all()` makes every pair on a root resolve to ONE directory, a junction on Windows and a
directory symlink elsewhere, so whichever account is signed in reads the same records.
`share_current()` puts it back.

WHAT WAS MEASURED BEFORE THIS WAS WRITTEN, on the test laptop, because the app defends itself
against link tricks and most of those defences would have made this impossible:

    a record reached through a junction   regular file, not a symlink, nlink 1
    the app's private reader              refuses a symlinked file, refuses nlink > 1 at 11 sites
    the junction, to Node's lstat         isSymbolicLink true, isDirectory FALSE
    the junction, to stat                 isDirectory true
    the app, started on it                listed all 17 records and rewrote three of them

So a junction clears the file-level guards that a symlink or a hard link would trip, and the app
resolves the directory with `stat` rather than filtering directory entries by type. That last line
is the one that decides the feature, it was measured rather than argued, and it can change in any
release: `verify()` exists to notice when it does.

THREE COSTS, none of them hidden:

  - One directory holds one `scheduled-tasks.json` and one `archived-sessions.idx`. The copies
    belonging to the pairs that become junctions are moved into the backup, never merged.
  - Either account can rewrite, or delete, the other's chats. That is what sharing means.
  - The app must be closed while the directories move, and started again afterwards.
"""
import json
import os
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECORD = "local_"
# Per pair, not per chat, so they cannot travel into a shared directory without a decision.
PER_PAIR = ("scheduled-tasks.json", "archived-sessions.idx")

ALL, CURRENT, MIXED = "all", "current", "mixed"


def supported():
    """(bool, reason). Whether this machine can point one records directory at another.

    EVERY PLATFORM CAN, and it took a red Linux leg to make that worth saying. This refused
    outright off Windows, which made the two guard tests below untestable there and the module a
    thing CI could only skip: the runner counts a skip against the fixture as a fixture gap, so a
    module that skips itself is a module nobody checks. The MECHANISM differs, a junction on
    Windows and a directory symlink elsewhere, and the records under either are ordinary files,
    which is the property the app's own reader cares about.
    """
    return True, ""


def _stat_fails(path) -> bool:
    try:
        os.stat(str(path))
    except OSError:
        return True
    return False


def resolves_to(link, target) -> bool:
    """Does the kernel land `link` on `target`? By identity, never by the string it was made with.

    `os.stat` follows a junction, so this compares the directory BEHIND the link with the target,
    which `link_target()` cannot do: it reads the substitute name back, and that string opened
    from inside the app's process tree is the one directory this code sees, whatever the kernel
    does with it. False for a dangling link (the stat raises) and for an inaccessible directory
    (Windows answers inode 0 there, and two of those would compare equal).
    """
    try:
        a, b = os.stat(str(link)), os.stat(str(target))
    except OSError:
        return False
    if not a.st_ino or not b.st_ino:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _spellings(path) -> list:
    """Every spelling of `path` that opens the same directory, the package's own spelling first.

    THE KERNEL RESOLVES A JUNCTION TARGET PHYSICALLY. The Store build of the desktop app
    virtualises `%APPDATA%\\Claude` into its package's `LocalCache` for itself and for every process
    it spawns, this server included: from inside, both spellings open one directory, `realpath`
    answers the virtual one, and nothing in the process can tell them apart. Measured 2026-09-15 on
    two machines: a junction made from inside with the virtual spelling as its substitute name
    resolved to a leftover physical directory holding one record on one machine and to nothing on
    the other, and every account but the shared one listed one chat, or none. So a target is
    re-rooted under every candidate the store knows, each spelling kept only when it opens the same
    directory (`store._identity`), the spelling under `Packages` first; off Windows, and on a
    machine with one spelling, the answer is the path it was given.
    """
    from c4x import store
    given = os.path.abspath(str(path))
    key = store._identity(given)
    found: list[str] = []
    if key is not None:
        roots = [os.path.join(c, "claude-code-sessions")
                 for c in store._claude_appdata_candidates()]
        for a in roots:
            try:
                rel = os.path.relpath(given, a)
            except ValueError:
                continue                    # another drive
            if rel == os.curdir or rel.startswith(os.pardir):
                continue                    # not under this root, whole components only
            for b in roots:
                other = os.path.join(b, rel)
                if store._identity(other) == key:
                    found.append(other)

    def packaged(spelling):
        return "packages" in os.path.normcase(spelling).replace("/", os.sep).lower().split(os.sep)

    ordered = [s for s in found if packaged(s)] + [s for s in found if not packaged(s)] + [given]
    out: list[str] = []
    seen: set[str] = set()
    for spelling in ordered:
        norm = os.path.normcase(spelling)
        if norm not in seen:
            seen.add(norm)
            out.append(spelling)
    return out


def _raw_link(link, spelling):
    """Make `link` point at `spelling`, checking only that a link now exists. Raises on failure."""
    if platform.system() == "Windows":
        from c4x import proc
        run = proc.run(["cmd", "/c", "mklink", "/J", str(link), str(spelling)],
                       capture_output=True, text=True)
        if run.returncode != 0 or link_target(link) is None:
            raise RuntimeError((run.stderr or run.stdout).strip() or "mklink failed")
        return
    os.symlink(str(spelling), str(link), target_is_directory=True)


def _make_link(link, target) -> str:
    """Point `link` at `target` so that the kernel lands on it. Returns the spelling written.

    Each spelling `_spellings` offers is written and then checked by identity (`resolves_to`); a
    link that was made and does not resolve is removed and the next spelling tried, so nothing is
    left behind. A spelling that cannot be written at all raises at once.
    """
    target = os.path.abspath(str(target))
    tried: list[str] = []
    for spelling in _spellings(target):
        tried.append(spelling)
        _raw_link(link, spelling)
        if resolves_to(link, target):
            return spelling
        _remove_link(link)
    raise RuntimeError(f"no spelling of {target} resolves through {link}; tried {tried}")


def _remove_link(link):
    """Remove the LINK and never what it points at. Raises when it is not a link or stays.

    `os.unlink` removes a directory junction on Windows (CPython routes a directory reparse point
    to `RemoveDirectoryW`, and its own `test_unlink_removes_junction` says so) and a symlink
    elsewhere, and raises when it cannot; the `rmdir` this used to spawn discarded its exit code,
    and a removal that silently failed is the one path by which `share_current` would have moved
    records THROUGH the junction into the directory behind it.
    """
    link = str(link)
    if link_target(link) is None:
        raise RuntimeError(f"{link} is not a link; nothing removed")
    try:
        os.unlink(link)
    except OSError as exc:
        raise RuntimeError(f"could not remove the link {link}: {exc}") from exc
    if os.path.lexists(link):
        raise RuntimeError(f"the link {link} is still there after its removal")


def app_running():
    """Whether Claude is running, which decides whether these directories can be moved at all.

    A directory the app has open cannot be replaced, and a half-moved pair is the one state worth
    avoiding: the first run of this by hand moved four files and then stopped on a collision, which
    left the signed-in account pointing at an empty directory until it was finished.

    NO CHILD PROCESS. This asked `tasklist`, and it is called on every page load through
    `/api/adopt` and `/api/accounts`; once the server ran without a console, every one of those
    calls flashed a console window. The rule is the same one `tasklist /FI "IMAGENAME eq
    claude.exe"` applied: a process whose image name is `claude.exe`, in any case. A process that
    refuses its name (another user's) has `None` there and is skipped.
    """
    if platform.system() != "Windows":
        return False
    for process in psutil.process_iter(["name"]):
        if str(process.info.get("name") or "").lower() == "claude.exe":
            return True
    return False


def link_target(path):
    """Where this path points, or None when it is an ordinary directory.

    `Path.is_symlink()` is FALSE for a junction on Windows, which is why this does not use it. The
    same call in Node answers true, and the difference is not academic: it is what decides whether
    the app sees a directory at all.
    """
    try:
        target = os.readlink(path)
    except OSError:
        return None
    # WITHOUT THE EXTENDED-LENGTH PREFIX. Windows answers a junction with the `\\?\` spelling of
    # the same directory, which nothing else here uses: comparing it against the path that was
    # linked reported every healthy link as pointing somewhere else.
    return target[4:] if target.startswith("\\\\?\\") else target


def pairs_on(root):
    """Every `<account>/<org>` directory under one records root, linked or not."""
    out: list[dict[str, Any]] = []
    root = Path(root)
    if not root.is_dir():
        return out
    for account in sorted(root.iterdir()):
        if not account.is_dir() and link_target(account) is None:
            continue
        try:
            orgs = sorted(account.iterdir())
        except OSError:
            continue
        for org in orgs:
            target = link_target(org)
            if target is None and not org.is_dir():
                continue
            out.append({
                "root": str(root), "account": account.name, "org": org.name,
                "path": str(org), "link_to": target,
                "records": len(list(org.glob(f"{RECORD}*.json"))),
            })
    return out


def _same_path(a, b) -> bool:
    """Two spellings of one directory, by identity: a junction's target is read back in whatever
    form the platform gives it, and the Store build's virtual spelling and its package spelling
    open one directory. Strings are compared only when a side cannot be stat'ed, or when Windows
    answers inode 0 (an inaccessible directory; two of those would otherwise compare equal)."""
    a, b = str(a), str(b)
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    if sa.st_ino and sb.st_ino:
        return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def own_records(pair, pairs, owners):
    """How many chats this pair would list on its own: what `share_current` would hand it back.

    A pair that is neither a link nor a link's target holds its own files, so the answer is its
    `records`. Under sharing every file sits in one directory and only the newest backup's
    manifest says which came from where: a pair's own are the records the manifest filed under
    it that are still there, plus, for the directory the others point at, every record the
    manifest never saw (written since sharing began, which `share_current` leaves in place).
    None when links exist and no manifest does, a junction made by hand: a guess would be a wrong
    number under a right-looking label.
    """
    targets = [p["link_to"] for p in pairs if p["link_to"]]
    if pair["link_to"]:
        shared = pair["link_to"]
    elif any(_same_path(pair["path"], t) for t in targets):
        shared = pair["path"]
    else:
        return int(pair["records"])
    if owners is None:
        return None
    try:
        present = {f.name for f in Path(shared).glob(f"{RECORD}*.json")}
    except OSError:
        return None
    key = (pair["root"], pair["account"], pair["org"])
    mine = {name for name, who in owners.items()
            if name.startswith(RECORD) and who == key and name in present}
    if not pair["link_to"]:
        mine |= {name for name in present if name not in owners}
    return len(mine)


def _tag_counts():
    """({account: live tagged records}, untagged live records) from harvest's owner columns, or
    (None, 0) on a store without the columns, without the table, or without a store at all.

    Keyed on the ACCOUNT: under sharing every organisation of an account lists the same
    directory, so the organisation harvest wrote beside the account is a best guess and the
    account is not (docs/desktop-records.md section 6).
    """
    from c4x import store
    try:
        if not store.tables_present("desktop_records") \
                or not store.column_present("desktop_records", "owner_account"):
            return None, 0
        df = store.q("SELECT owner_account AS account, COUNT(*) AS n FROM desktop_records "
                     "WHERE gone_at IS NULL AND deleted_at IS NULL GROUP BY owner_account")
    except Exception:  # noqa: BLE001 - no store answers the way a store without the columns does
        return None, 0
    counts: dict = {}
    untagged = 0
    for account, n in zip(df["account"], df["n"], strict=True):
        if account is None or (isinstance(account, float) and account != account):
            untagged += int(n)
        else:
            counts[str(account)] = int(n)
    return (counts or None), untagged


def signed_in_pair():
    """{account, org} for the pair the desktop app is writing, or None when nothing says."""
    from c4x import appstate
    try:
        pair = appstate.desktop_pair(appstate.sessions_root())
    except (OSError, ValueError):
        return None
    return {"account": pair["account"], "org": pair["org"]} if pair else None


def state():
    """What every pair on every records root resolves to, and whether they are shared.

    `chats_visible` is what All shows: every record in the directories that hold files. `own` on
    each pair and `current_chats` for the signed-in one are what Current would show, read from
    the newest backup manifest only while a pair is linked (a page load in Current mode pays for
    a directory listing and nothing more).
    """
    from c4x import store
    ok, why = supported()
    roots = [{"root": r, "pairs": pairs_on(r)} for r in store.sessions_roots()]
    every = [p for r in roots for p in r["pairs"]]
    linked = [p for p in every if p["link_to"]]
    # ONLY A LINK THE KERNEL LANDS ON THE SHARED DIRECTORY COUNTS AS SHARING. One that resolves
    # elsewhere (the Store build's virtual spelling, see `_spellings`) reads as MIXED, and the
    # `uncovered` list below says which and why.
    sharing: list = []
    for r in roots:
        head = canonical_pair(r["pairs"])
        sharing += [p for p in r["pairs"]
                    if p["link_to"] and head and resolves_to(p["path"], head["path"])]
    mode = CURRENT
    if linked:
        mode = ALL if len(sharing) == len([p for p in every if p["records"] or p["link_to"]]) - 1 \
            else MIXED
    owners: dict | None = {}
    if linked:
        manifest = _newest_manifest()
        owners = _record_owners(manifest) if manifest else None
    for p in every:
        p["own"] = own_records(p, every, owners)
    signed = signed_in_pair()
    current = next((p["own"] for p in every
                    if signed and p["account"] == signed["account"] and p["org"] == signed["org"]),
                   None)
    current_source = (None if (linked and owners is None)
                      else ("manifest" if linked else "directory"))
    # THE TAGS, once harvest has written any: the account each chat was made under, counted over
    # live records. Truer than the manifest, which only knows where a record sat before sharing
    # began, and the only answer that stays true as chats are made under sharing.
    counts, untagged = _tag_counts()
    if counts is not None:
        for p in every:
            p["own"] = counts.get(p["account"], 0)
        current = counts.get(signed["account"], 0) if signed else None
        current_source = "tags"
    # WHAT THIS PAGE LISTS, beside what the app lists: the same number the population list's
    # "Signed-in account's chats" carries, from the one function both read. None without a store.
    listed = current_listed = None
    try:
        who = store.listed_by_account()
        listed = who["listed"]
        current_listed = who["mine"] if signed else None
    except Exception:  # noqa: BLE001 - no store, no frame; the hover keeps the app's numbers
        pass
    meant = intent(linked=bool(linked))
    # THE PAIRS SHARING DOES NOT COVER YET, while sharing is meant: the ones the app created
    # since. The page says how many, and `reconcile` folds them in when Claude next closes.
    uncovered = ([_brief(p) for r in roots for p in uncovered_pairs(r["pairs"])]
                 if meant["mode"] == ALL else [])
    return {
        "supported": ok, "why_not": why, "app_running": app_running(),
        # WHAT WAS ASKED FOR, beside what is on disk. They disagree when a migration has undone the
        # sharing, and that difference is the whole of what `verify` reports.
        "mode": mode, "intended": meant["mode"], "intended_source": meant["source"],
        "roots": roots, "pairs": len(every), "linked": len(sharing),
        "chats_visible": sum(p["records"] for p in every if not p["link_to"]),
        "signed_in": signed, "current_chats": current, "uncovered": uncovered,
        # WHERE THE CURRENT NUMBER CAME FROM: the tags, the sharing backup's manifest, or the
        # directories themselves; None when links exist and nothing can say.
        "current_source": current_source, "untagged": untagged,
        "listed": listed, "current_listed": current_listed,
    }


def marker_path():
    """Where the INTENDED mode is recorded, beside the store and never inside the app's tree.

    `verify()` cannot do its job without this. With no links present it cannot tell a machine that
    was never shared from one whose links a migration turned back into directories, which is the
    exact failure it exists to catch: a test that simulated the migration passed while the sharing
    was gone.
    """
    from c4x import store
    return Path(store.DB_PATH).parent / "account-sharing.json"


def intent(linked=None) -> dict:
    """What sharing is meant to be, and what says so: `{"mode": all|current, "source":
    marker|disk|none}`.

    THE MARKER FIRST, THE DISK SECOND. The marker is what a person asked for; but a marker lives
    beside the store and a reset of `data/` takes it with it, while the junctions stay where they
    are. Measured on the author's machine: eight of nine pairs linked, no marker, and the page
    said Current while every account read one list. Links on disk are not made by accident, so
    with no marker they ARE the intent, and `reconcile()` writes the marker back. `linked` may be
    passed by a caller that has already walked the roots.
    """
    try:
        recorded = json.loads(marker_path().read_text(encoding="utf-8")).get("mode")
        if recorded in (ALL, CURRENT):
            return {"mode": recorded, "source": "marker"}
    except (OSError, ValueError):
        pass
    if linked is None:
        from c4x import store
        linked = any(p["link_to"] for r in store.sessions_roots() for p in pairs_on(r))
    return {"mode": ALL, "source": "disk"} if linked else {"mode": CURRENT, "source": "none"}


def intended_mode():
    """`all` when sharing was turned on and not turned off, else `current`."""
    return intent()["mode"]


def _recorded_links() -> list:
    """The links the marker recorded, or none."""
    try:
        return json.loads(marker_path().read_text(encoding="utf-8")).get("links") or []
    except (OSError, ValueError):
        return []


def _write_marker(links, by="share_all"):
    marker_path().parent.mkdir(parents=True, exist_ok=True)
    marker_path().write_text(json.dumps({
        "mode": ALL, "set_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "by": by, "links": links}, indent=1), encoding="utf-8")


def _clear_marker():
    marker_path().unlink(missing_ok=True)


def backups_dir():
    """Beside the store, the way `snapshots_dir` is: a fixture run backs up beside the fixture."""
    from c4x import store
    return Path(store.DB_PATH).parent / "record-backups"


def _backup(roots, note):
    """Copy every pair, with hashes, into a DATED directory. Never over the previous one.

    LINKS ARE NOT WALKED. On Windows a junction is a directory to `rglob`, so a backup of a linked
    root copied the shared directory once per junction and the manifest filed every record under
    whichever pair was walked last, which is the one map `share_current` and `own_records` read.
    `os.walk` with the linked directories pruned copies each file once, under the pair that holds
    it. The stamp carries microseconds: `share_all` and `reconcile` can each take a backup within
    one second, and two backups with one name raised.
    """
    import hashlib
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    dest = backups_dir() / stamp
    dest.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"note": note, "taken_at": stamp, "files": []}
    for n, root in enumerate(roots):
        src = Path(root)
        if not src.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if link_target(Path(dirpath) / d) is None)
            for name in sorted(filenames):
                f = Path(dirpath) / name
                if not f.is_file():
                    continue
                rel = f.relative_to(src)
                out = dest / f"root{n}" / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, out)
                manifest["files"].append({
                    "root": str(src), "rel": str(rel), "bytes": f.stat().st_size,
                    "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return dest, manifest


def _newest_manifest():
    """The newest backup's manifest, or None when no backup has been taken."""
    stamps = sorted(backups_dir().glob("*/manifest.json")) if backups_dir().is_dir() else []
    if not stamps:
        return None
    try:
        return json.loads(stamps[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record_owners(manifest) -> dict:
    """name -> (root, account, org) for every record and per-pair file the manifest filed under
    a pair: where `share_current` sends each back, and what `own_records` counts."""
    where: dict = {}
    for item in manifest.get("files", []):
        rel = Path(item["rel"])
        if len(rel.parts) == 3 and rel.name.startswith(RECORD) or rel.name in PER_PAIR:
            where[rel.name] = (item["root"], rel.parts[0], rel.parts[1])
    return where


def canonical_pair(pairs):
    """The pair every other one points at: the one the links already point at, else the one
    holding the most chats.

    Not a new directory of its own. The app writes into whichever pair it is signed in to, and a
    pair it has never written to is a pair it may recreate; the fullest existing one is the one
    with the most to lose and the least to prove. ONCE LINKS EXIST, THEIR TARGET WINS: a pair the
    app created after sharing began can hold more records than the shared directory does on a
    quiet day, and folding the shared directory into it would chain every junction through a
    junction and move the one list the accounts read.
    """
    real = [p for p in pairs if not p["link_to"]]
    if not real:
        return None
    targets = [p["link_to"] for p in pairs if p["link_to"]]
    # EVERY SPELLING OF A TARGET, so the CLI run from a plain terminal (where the virtual spelling
    # opens something else) picks the same head the server does.
    pointed = [p for p in real
               if any(_same_path(p["path"], s) for t in targets for s in _spellings(t))]
    return max(pointed or real, key=lambda p: (p["records"], p["path"]))


NOT_LINKED, ELSEWHERE, DANGLING = "not linked", "points elsewhere", "dangling"


def uncovered_pairs(pairs) -> list:
    """The pairs on a root that do not read the shared directory, each with a `why`.

    `not linked`: a real pair beside the canonical one, with or without records (the app creates
    `<account>/<org>` when an account signs in with that organisation, before any chat is
    written, and an empty directory it holds open is still a directory it reads instead of the
    shared one). `points elsewhere`: a link the kernel lands somewhere other than the shared
    directory (a substitute name in the Store build's virtual spelling, see `_spellings`).
    `dangling`: a link whose target is not there. Empty when the root has one pair.
    """
    if len(pairs) < 2:
        return []
    head = canonical_pair(pairs)
    if head is None:
        return []
    out = []
    for p in pairs:
        if p["link_to"]:
            if resolves_to(p["path"], head["path"]):
                continue
            out.append(dict(p, why=DANGLING if _stat_fails(p["path"]) else ELSEWHERE))
        elif not _same_path(p["path"], head["path"]):
            out.append(dict(p, why=NOT_LINKED))
    return out


def _brief(p) -> dict:
    """A pair as the page and the reports name it."""
    return {"root": p["root"], "account": p["account"], "org": p["org"], "path": p["path"],
            "records": p["records"], "why": p.get("why", NOT_LINKED)}


def _fold_pair(pair, head, backup, dry_run=False) -> dict:
    """Move one pair's files into the head and make the pair a link to it.

    The per-pair half of `share_all`, and the whole of what `reconcile` does for a pair the app
    created later. Files of a name the head already holds are set aside under the backup, never
    merged: `scheduled-tasks.json` is a different account's schedule and nothing here can say
    which wins. A directory that still holds anything after the move is left as it is, named in
    the error with the backup's path.
    """
    out: dict[str, Any] = {"moved": [], "set_aside": [], "linked": []}
    here = Path(pair["path"])
    for f in sorted(here.iterdir()):
        if not f.is_file():
            continue
        target = Path(head["path"]) / f.name
        if target.exists():
            out["set_aside"].append(
                {"path": str(f), "why": "a file of that name is already shared"})
            if not dry_run:
                keep = Path(str(backup)) / "set-aside" / pair["account"][:8]
                keep.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(keep / f.name))
            continue
        out["moved"].append({"from": str(f), "to": str(target)})
        if not dry_run:
            shutil.move(str(f), str(target))
    if dry_run:
        out["linked"].append({"link": pair["path"], "to": head["path"]})
        return out
    rest = list(here.iterdir())
    if rest:
        raise RuntimeError(
            f"{here} still holds {len(rest)} entr(ies) and cannot become a link; "
            f"nothing else was changed. The backup is at {backup}.")
    here.rmdir()
    try:
        written = _make_link(here, head["path"])
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"could not link {here}: {exc}. The backup is at {backup}.") from exc
    out["linked"].append({"link": pair["path"], "to": written})
    return out


def _relink_pair(pair, head, backup, dry_run=False) -> dict:
    """Point a link that resolves elsewhere, or nowhere, at the head again.

    WHAT WAS VISIBLE THROUGH IT IS KEPT FIRST: every file the link shows is copied into
    `<backup>/through-link/<account8>/`, and a record whose name the head does not hold is copied
    into the head; a colliding name is reported, its copy in the backup being the one kept. Copied,
    never moved: the directory behind a mispointed link is not this code's, and from inside the
    app's process tree it has no name of its own. Then the old link is removed and a new one made;
    a make that fails puts the old link back with its old substitute name and raises, since a pair
    with no directory at all is the one outcome worse than a mispointed one: the app recreates it
    as a real directory at the next sign-in and that account starts a private list.
    """
    here = Path(pair["path"])
    was = link_target(here)
    out: dict[str, Any] = {"link": pair["path"], "was": was, "to": None, "why": pair.get("why"),
                           "copied": [], "set_aside": [], "restored": False}
    try:
        visible = [f for f in sorted(here.iterdir()) if f.is_file()]
    except OSError:
        visible = []
    kept = Path(str(backup)) / "through-link" / pair["account"][:8]
    for f in visible:
        target = Path(head["path"]) / f.name
        if target.exists():
            out["set_aside"].append({"path": str(f), "kept": str(kept / f.name),
                                     "why": "a file of that name is already shared"})
            continue
        out["copied"].append({"from": str(f), "to": str(target)})
    if dry_run:
        out["to"] = head["path"]
        return out
    if visible:
        kept.mkdir(parents=True, exist_ok=True)
        for f in visible:
            shutil.copy2(str(f), str(kept / f.name))
        for c in out["copied"]:
            shutil.copy2(c["from"], c["to"])
    _remove_link(here)
    try:
        out["to"] = _make_link(here, head["path"])
    except (OSError, RuntimeError) as exc:
        if was is not None and not os.path.lexists(here):
            _raw_link(here, was)
            out["restored"] = True
        raise RuntimeError(
            f"could not re-point {here} at {head['path']}: {exc}; "
            + (f"the old link to {was} was put back. " if out["restored"] else "")
            + f"The backup is at {backup}.") from exc
    return out


def share_all(dry_run=False):
    """Point every pair on each root at that root's fullest pair. Returns a report."""
    from c4x import store
    ok, why = supported()
    if not ok:
        raise RuntimeError(why)
    if app_running():
        raise RuntimeError(
            "Claude is running, and a directory it has open cannot be moved. Quit Claude and try "
            "again; nothing has been changed.")
    roots = store.sessions_roots()
    report: dict[str, Any] = {"mode": ALL, "dry_run": bool(dry_run), "roots": [],
                              "restart_required": True, "backup": None}
    if not dry_run:
        dest, _manifest = _backup(roots, "before sharing every account's records")
        report["backup"] = str(dest)
    for root in roots:
        pairs = pairs_on(root)
        head = canonical_pair(pairs)
        if head is None or len(pairs) < 2:
            continue
        moved: list = []
        aside: list = []
        linked: list = []
        for pair in pairs:
            if pair["path"] == head["path"] or pair["link_to"]:
                continue
            folded = _fold_pair(pair, head, report["backup"], dry_run)
            moved += folded["moved"]
            aside += folded["set_aside"]
            linked += folded["linked"]
        report["roots"].append({"root": str(root), "canonical": head["path"],
                                "moved": moved, "set_aside": aside, "linked": linked})
    if not dry_run:
        _write_marker([link for r in report["roots"] for link in r["linked"]])
    report["state"] = state()
    return report


def reconcile(dry_run=False) -> dict:
    """Cover every pair sharing does not cover yet, and refresh what sharing rests on.

    WHY. The app creates `<account>/<org>` when an account signs in with an organisation the
    junctions never named, and from then on that account reads its own new directory: a private
    list beside the shared one. Measured on the author's machine on 2026-09-15: Account #1 signed
    in with a newer organisation, got a real directory and fifteen chats of its own, and at the
    next account switch the app folded that directory into the one the junctions point at and
    removed it, so the account came back to nothing. Nothing c4x could have caught while the app
    was open, and everything it can put right the moment the app is closed: the server's watchdog
    calls this before it stops, sixty seconds after the last Claude process.

    WHAT. Under intent ALL only (`intent()`: the marker, or links on disk when the marker is
    gone): every real pair beside the canonical one is folded into it and linked, after a backup
    whose manifest says which record came from where; a machine with links and no manifest gets
    a manifest even with nothing to fold, since `share_current` and the Current count read it;
    the marker is written back every time, so the page stops saying Current over a shared disk.
    A link the kernel lands elsewhere, or nowhere (`uncovered_pairs`: a substitute name in the
    Store build's virtual spelling, or a target that is gone), is re-pointed at the shared
    directory (`_relink_pair`), what was visible through it copied into the backup first.
    Refused, and nothing moved, while the app runs: the report names the pending pairs instead.
    """
    from c4x import store
    ok, why = supported()
    if not ok:
        raise RuntimeError(why)
    # KEYED AS `pairs_on` SPELLS ITS ROOT (`str(Path(root))`): an override with a trailing
    # separator or forward slashes would otherwise match no pair and "cover" nothing while
    # reporting that it had.
    roots = [str(Path(r)) for r in store.sessions_roots()]
    pairs_by_root = {r: pairs_on(r) for r in roots}
    linked_any = any(p["link_to"] for ps in pairs_by_root.values() for p in ps)
    meant = intent(linked=linked_any)
    report: dict[str, Any] = {"ran": False, "dry_run": bool(dry_run), "intended": meant["mode"],
                              "intended_source": meant["source"], "app_running": False,
                              "why": "", "pending": [], "backup": None, "roots": [],
                              "marker_written": False, "restart_required": False}
    if meant["mode"] != ALL:
        report["why"] = "sharing is off; nothing to cover"
        report["state"] = state()
        return report
    pending = [p for ps in pairs_by_root.values() for p in uncovered_pairs(ps)]
    report["pending"] = [_brief(p) for p in pending]
    if app_running():
        report["app_running"] = True
        report["why"] = (
            "Claude is running, and a directory it has open cannot be moved. Quit Claude and try "
            "again; nothing has been changed." if pending
            else "Claude is running; nothing to cover")
        report["state"] = state()
        return report
    if not dry_run and (pending or _newest_manifest() is None):
        dest, _manifest = _backup(roots, "before covering the pairs the app created since sharing"
                                  if pending else "the sharing manifest, taken by reconcile")
        report["backup"] = str(dest)
    for root, pairs in pairs_by_root.items():
        head = canonical_pair(pairs)
        todo = [p for p in pending if p["root"] == root]
        if head is None or not todo:
            continue
        moved: list = []
        aside: list = []
        linked: list = []
        relinked: list = []
        for pair in todo:
            if pair["why"] == NOT_LINKED:
                folded = _fold_pair(pair, head, report["backup"], dry_run)
                moved += folded["moved"]
                aside += folded["set_aside"]
                linked += folded["linked"]
            else:
                done = _relink_pair(pair, head, report["backup"], dry_run)
                aside += done["set_aside"]
                relinked.append(done)
        report["roots"].append({"root": root, "canonical": head["path"], "moved": moved,
                                "set_aside": aside, "linked": linked, "relinked": relinked})
    if not dry_run:
        links = [{"link": p["path"], "to": p["link_to"]}
                 for r in roots for p in pairs_on(r) if p["link_to"]]
        _write_marker(links, by="reconcile")
        report["marker_written"] = True
    report["ran"] = True
    folded_n = len([p for p in pending if p["why"] == NOT_LINKED])
    relinked_n = len(pending) - folded_n
    report["why"] = ((f"covered {folded_n} pair(s)" if folded_n else "")
                     + ("; " if folded_n and relinked_n else "")
                     + (f"re-pointed {relinked_n} link(s)" if relinked_n else "")
                     or "nothing to cover; the marker and the manifest are in place")
    report["restart_required"] = bool(pending) and not dry_run
    report["pending"] = []
    report["state"] = state()
    return report


def share_current(dry_run=False):
    """Undo it: remove every link and put each file back under the pair it came from.

    The LINK is removed and never the target, which on Windows is what `rmdir` on a junction does.
    Files come back by name from the newest backup's manifest, so a record the app has written
    since is moved with its current contents rather than the copy in the backup.
    """
    from c4x import store
    ok, why = supported()
    if not ok:
        raise RuntimeError(why)
    if app_running():
        raise RuntimeError(
            "Claude is running, and these directories cannot be moved while it is. Quit Claude "
            "and try again; nothing has been changed.")
    manifest = _newest_manifest()
    if manifest is None:
        raise RuntimeError("no backup to restore the layout from, so nothing was changed")
    where = _record_owners(manifest)
    stamps = sorted(backups_dir().glob("*/manifest.json"))
    report: dict[str, Any] = {"mode": CURRENT, "dry_run": bool(dry_run), "restored": [],
                              "left": [], "restart_required": True,
                              "from_backup": str(stamps[-1].parent)}
    for root in store.sessions_roots():
        for pair in pairs_on(root):
            if not pair["link_to"]:
                continue
            here = Path(pair["path"])
            shared = Path(pair["link_to"])
            if not dry_run:
                _remove_link(here)
                here.mkdir(parents=True, exist_ok=True)
            for name, (was_root, account, org) in where.items():
                if account != pair["account"] or org != pair["org"] or was_root != pair["root"]:
                    continue
                source = shared / name
                if source.exists():
                    report["restored"].append({"name": name, "to": str(here)})
                    if not dry_run:
                        shutil.move(str(source), str(here / name))
                else:
                    aside = Path(stamps[-1]).parent / "set-aside" / account[:8] / name
                    if aside.exists():
                        report["restored"].append({"name": name, "to": str(here)})
                        if not dry_run:
                            shutil.move(str(aside), str(here / name))
                    else:
                        report["left"].append({"name": name, "why": "not found to move back"})
    if not dry_run:
        _clear_marker()
    report["state"] = state()
    return report


def verify():
    """Is the sharing still in what state it was asked to be in?

    AGAINST THE RECORDED INTENT, not against whatever is on disk. An app update can migrate these
    directories: `claude-code-sessions` is named in the app's own migration list, and this machine
    already holds two records roots because that migration has run once. A migration that replaces
    a junction with a real directory ends the sharing and says nothing, and with no links left there
    is nothing on disk to notice. The marker is what makes that visible.
    """
    from c4x import store
    intended = intended_mode()
    out: dict[str, Any] = {"ok": True, "intended": intended, "problems": [], "roots": [],
                           "uncovered": []}
    recorded = _recorded_links()
    for link in recorded:
        here = Path(link["link"])
        target = link_target(here)
        if target is None:
            out["ok"] = False
            out["problems"].append(
                f"{here} was linked to {link['to']} and is a plain directory now, so that account "
                "is back to its own chats; an update's migration does exactly this")
        elif _stat_fails(here):
            out["ok"] = False
            out["problems"].append(f"{here} points at {target}, which is not there (dangling)")
        elif not _same_path(target, link["to"]):
            out["ok"] = False
            out["problems"].append(f"{here} points at {target}, not at {link['to']}")
    for root in store.sessions_roots():
        pairs = pairs_on(root)
        linked = [p for p in pairs if p["link_to"]]
        # A PAIR SHARING DOES NOT COVER, records or not: the app creates the directory at sign-in
        # and reads it instead of the shared one from then on.
        missing = uncovered_pairs(pairs) if intended == ALL else []
        reported = set()
        for p in missing:
            out["ok"] = False
            out["uncovered"].append(_brief(p))
            reported.add(p["path"])
            out["problems"].append(
                f"{p['path']}: {p['why']}, so that account does not read the shared list; "
                "reconcile covers it when Claude next closes")
        if intended == CURRENT and linked:
            out["ok"] = False
            out["problems"].append(
                f"{root}: sharing is off and {len(linked)} pair(s) are still links")
        for pair in linked:
            if pair["path"] in reported:
                continue                    # said once above, with its why
            names = sorted(p.name for p in Path(pair["path"]).glob(f"{RECORD}*.json"))
            theirs = sorted(p.name for p in Path(pair["link_to"]).glob(f"{RECORD}*.json"))
            if names != theirs:
                out["ok"] = False
                out["problems"].append(
                    f"{pair['path']} and the directory it points at do not list the same records")
        holding = [p for p in pairs if not p["link_to"] and p["records"]]
        out["roots"].append({"root": str(root), "linked": len(linked), "holding": len(holding),
                             "uncovered": len(missing)})
    return out


def main(argv=None):
    """`python -m c4x.accounts [--state | --all | --current | --reconcile | --verify]
    [--dry-run]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    if "--all" in argv:
        print(json.dumps(share_all(dry_run=dry), indent=1))
    elif "--current" in argv:
        print(json.dumps(share_current(dry_run=dry), indent=1))
    elif "--reconcile" in argv:
        answer = reconcile(dry_run=dry)
        print(json.dumps(answer, indent=1))
        return 0 if answer["ran"] or not answer["pending"] else 1
    elif "--verify" in argv:
        answer = verify()
        print(json.dumps(answer, indent=1))
        return 0 if answer["ok"] else 1
    else:
        print(json.dumps(state(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

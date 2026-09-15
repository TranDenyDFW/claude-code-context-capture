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


def _make_link(link, target):
    """Point `link` at `target`, using whatever this platform calls that. Raises on failure."""
    if platform.system() == "Windows":
        from c4x import proc
        run = proc.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
        if run.returncode != 0 or link_target(link) is None:
            raise RuntimeError((run.stderr or run.stdout).strip() or "mklink failed")
        return
    os.symlink(str(target), str(link), target_is_directory=True)


def _remove_link(link):
    """Remove the LINK and never what it points at, which is what `rmdir` does to a junction."""
    if platform.system() == "Windows":
        from c4x import proc
        proc.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
        return
    os.unlink(link)


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
    """Two spellings of one directory: a junction's target is read back in whatever form the
    platform gives it, which need not be the string the pair list holds."""
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return str(a) == str(b)


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
    mode = CURRENT
    if linked:
        mode = ALL if len(linked) == len([p for p in every if p["records"] or p["link_to"]]) - 1 \
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
    return {
        "supported": ok, "why_not": why, "app_running": app_running(),
        # WHAT WAS ASKED FOR, beside what is on disk. They disagree when a migration has undone the
        # sharing, and that difference is the whole of what `verify` reports.
        "mode": mode, "intended": intended_mode(), "roots": roots,
        "pairs": len(every), "linked": len(linked),
        "chats_visible": sum(p["records"] for p in every if not p["link_to"]),
        "signed_in": signed, "current_chats": current,
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


def intended_mode():
    """`all` when sharing was turned on and not turned off, else `current`."""
    try:
        return json.loads(marker_path().read_text(encoding="utf-8")).get("mode") or CURRENT
    except (OSError, ValueError):
        return CURRENT


def _write_marker(links):
    marker_path().parent.mkdir(parents=True, exist_ok=True)
    marker_path().write_text(json.dumps({
        "mode": ALL, "set_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "links": links}, indent=1), encoding="utf-8")


def _clear_marker():
    marker_path().unlink(missing_ok=True)


def backups_dir():
    """Beside the store, the way `snapshots_dir` is: a fixture run backs up beside the fixture."""
    from c4x import store
    return Path(store.DB_PATH).parent / "record-backups"


def _backup(roots, note):
    """Copy every pair, with hashes, into a DATED directory. Never over the previous one."""
    import hashlib
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    dest = backups_dir() / stamp
    dest.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"note": note, "taken_at": stamp, "files": []}
    for n, root in enumerate(roots):
        src = Path(root)
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
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
    """The pair every other one points at: the one already holding the most chats.

    Not a new directory of its own. The app writes into whichever pair it is signed in to, and a
    pair it has never written to is a pair it may recreate; the fullest existing one is the one
    with the most to lose and the least to prove.
    """
    real = [p for p in pairs if not p["link_to"]]
    if not real:
        return None
    return max(real, key=lambda p: (p["records"], p["path"]))


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
        moved, aside, linked = [], [], []
        for pair in pairs:
            if pair["path"] == head["path"] or pair["link_to"]:
                continue
            here = Path(pair["path"])
            for f in sorted(here.iterdir()):
                if not f.is_file():
                    continue
                target = Path(head["path"]) / f.name
                if target.exists():
                    # ONE DIRECTORY, ONE OF EACH. Reported, never merged: `scheduled-tasks.json`
                    # is a different account's schedule and nothing here can say which wins.
                    aside.append({"path": str(f), "why": "a file of that name is already shared"})
                    if not dry_run:
                        keep = Path(str(report["backup"])) / "set-aside" / pair["account"][:8]
                        keep.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(keep / f.name))
                    continue
                moved.append({"from": str(f), "to": str(target)})
                if not dry_run:
                    shutil.move(str(f), str(target))
            if dry_run:
                linked.append({"link": pair["path"], "to": head["path"]})
                continue
            rest = list(here.iterdir())
            if rest:
                raise RuntimeError(
                    f"{here} still holds {len(rest)} entr(ies) and cannot become a link; "
                    f"nothing else was changed. The backup is at {report['backup']}.")
            here.rmdir()
            try:
                _make_link(here, head["path"])
            except OSError as exc:
                raise RuntimeError(
                    f"could not link {here}: {exc}. The backup is at {report['backup']}.") from exc
            except RuntimeError as exc:
                raise RuntimeError(
                    f"could not link {here}: {exc}. The backup is at {report['backup']}.") from exc
            linked.append({"link": pair["path"], "to": head["path"]})
        report["roots"].append({"root": str(root), "canonical": head["path"],
                                "moved": moved, "set_aside": aside, "linked": linked})
    if not dry_run:
        _write_marker([link for r in report["roots"] for link in r["linked"]])
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
    out: dict[str, Any] = {"ok": True, "intended": intended, "problems": [], "roots": []}
    recorded: list[dict[str, Any]] = []
    try:
        recorded = json.loads(marker_path().read_text(encoding="utf-8")).get("links") or []
    except (OSError, ValueError):
        recorded = []
    for link in recorded:
        here = Path(link["link"])
        target = link_target(here)
        if target is None:
            out["ok"] = False
            out["problems"].append(
                f"{here} was linked to {link['to']} and is a plain directory now, so that account "
                "is back to its own chats; an update's migration does exactly this")
        elif Path(target) != Path(link["to"]):
            out["ok"] = False
            out["problems"].append(f"{here} points at {target}, not at {link['to']}")
    for root in store.sessions_roots():
        pairs = pairs_on(root)
        linked = [p for p in pairs if p["link_to"]]
        holding = [p for p in pairs if not p["link_to"] and p["records"]]
        if intended == ALL and len(holding) > 1:
            out["ok"] = False
            out["problems"].append(
                f"{root}: sharing is on and {len(holding)} pairs hold records of their own, so the "
                "accounts are no longer reading one list")
        if intended == CURRENT and linked:
            out["ok"] = False
            out["problems"].append(
                f"{root}: sharing is off and {len(linked)} pair(s) are still links")
        for pair in linked:
            names = sorted(p.name for p in Path(pair["path"]).glob(f"{RECORD}*.json"))
            theirs = sorted(p.name for p in Path(pair["link_to"]).glob(f"{RECORD}*.json"))
            if names != theirs:
                out["ok"] = False
                out["problems"].append(
                    f"{pair['path']} and the directory it points at do not list the same records")
        out["roots"].append({"root": str(root), "linked": len(linked), "holding": len(holding)})
    return out


def main(argv=None):
    """`python -m c4x.accounts [--state | --all | --current | --verify] [--dry-run]`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    if "--all" in argv:
        print(json.dumps(share_all(dry_run=dry), indent=1))
    elif "--current" in argv:
        print(json.dumps(share_current(dry_run=dry), indent=1))
    elif "--verify" in argv:
        answer = verify()
        print(json.dumps(answer, indent=1))
        return 0 if answer["ok"] else 1
    else:
        print(json.dumps(state(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

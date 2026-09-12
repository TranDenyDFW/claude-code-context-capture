r"""Show every chat to every account signed into this machine, or keep them separate.

THE SEPARATION IS A DIRECTORY, and that is the whole of it. The desktop app keeps one record per
chat at

    <records root>\<account uuid>\<organisation uuid>\local_<uuid>.json

and the directory listing IS the session list (`docs/desktop-records.md` §2). An account sees what
is in its own pair and nothing else, so the same machine holds several chat histories that never
meet. Measured on the author's machine: four account directories, nine pairs, 171 records under one
pair and 16 under another.

`share_all()` makes every pair on a root resolve to ONE directory, using a Windows junction, so
whichever account is signed in reads the same records. `share_current()` puts it back.

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
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECORD = "local_"
# Per pair, not per chat, so they cannot travel into a shared directory without a decision.
PER_PAIR = ("scheduled-tasks.json", "archived-sessions.idx")

ALL, CURRENT, MIXED = "all", "current", "mixed"


def supported():
    """(bool, reason). A junction is a Windows construct and this feature is one on purpose."""
    if platform.system() != "Windows":
        return False, f"junctions are a Windows feature and this is {platform.system()}"
    return True, ""


def app_running():
    """Whether Claude is running, which decides whether these directories can be moved at all.

    A directory the app has open cannot be replaced, and a half-moved pair is the one state worth
    avoiding: the first run of this by hand moved four files and then stopped on a collision, which
    left the signed-in account pointing at an empty directory until it was finished.
    """
    if platform.system() != "Windows":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude.exe" in out.lower()


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


def state():
    """What every pair on every records root resolves to, and whether they are shared."""
    from c4x import store
    ok, why = supported()
    roots = [{"root": r, "pairs": pairs_on(r)} for r in store.sessions_roots()]
    every = [p for r in roots for p in r["pairs"]]
    linked = [p for p in every if p["link_to"]]
    mode = CURRENT
    if linked:
        mode = ALL if len(linked) == len([p for p in every if p["records"] or p["link_to"]]) - 1 \
            else MIXED
    return {
        "supported": ok, "why_not": why, "app_running": app_running(),
        # WHAT WAS ASKED FOR, beside what is on disk. They disagree when a migration has undone the
        # sharing, and that difference is the whole of what `verify` reports.
        "mode": mode, "intended": intended_mode(), "roots": roots,
        "pairs": len(every), "linked": len(linked),
        "chats_visible": sum(p["records"] for p in every if not p["link_to"]),
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
            run = subprocess.run(["cmd", "/c", "mklink", "/J", str(here), head["path"]],
                                 capture_output=True, text=True)
            if run.returncode != 0 or link_target(here) is None:
                raise RuntimeError(
                    f"could not link {here}: {(run.stderr or run.stdout).strip()}. The backup is "
                    f"at {report['backup']}.")
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
    stamps = sorted(backups_dir().glob("*/manifest.json")) if backups_dir().is_dir() else []
    if not stamps:
        raise RuntimeError("no backup to restore the layout from, so nothing was changed")
    manifest = json.loads(stamps[-1].read_text(encoding="utf-8"))
    where = {}
    for item in manifest["files"]:
        rel = Path(item["rel"])
        if len(rel.parts) == 3 and rel.name.startswith(RECORD) or rel.name in PER_PAIR:
            where[rel.name] = (item["root"], rel.parts[0], rel.parts[1])
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
                subprocess.run(["cmd", "/c", "rmdir", str(here)], capture_output=True, text=True)
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

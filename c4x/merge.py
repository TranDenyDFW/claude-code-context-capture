r"""Every account sees every chat: the union of the record files across the pairs.

WHY THIS EXISTS. The desktop app lists the chats whose record files sit in the directory of the
account and organisation signed in, and nothing else:
`claude-code-sessions\<account>\<org>\local_<uuid>.json`. Measured 2026-10-22, build 3.6556.1.0:
the sidebar, the Sessions pane (Active and Archived) and the command palette, including its content
search, all read ONE in-memory list filled from that one directory; the list is rebuilt at app
start, at window creation and on an account or organisation change, and there is no directory
watcher. So a chat made under one account is invisible under every other, and switching accounts
hides work rather than losing it.

That used to be papered over by pointing every pair at one directory with a junction. This build
refuses to write through a reparse point (`PlantDetectedError`), so `accounts.share_all` and the
watchdog's `reconcile` refuse to make one, and the junctions were replaced with real directories on
2026-10-10, each seeded with a copy of what the shared directory held that evening. Every account
has written only into its own since, so they have drifted apart: on the author's machine 13 chats
existed in one pair and nowhere else, the one that prompted this among them.

WHAT THIS DOES. It copies record files so that every real pair holds the union of them all, which
is the same remedy a junction gave without asking the app to follow one. ADD ONLY: it never
overwrites a record a pair already holds, never deletes anything, and never resurrects a chat the
user deleted in the app (a `deleted_<uuid>` marker in ANY pair settles that). `isArchived` travels
inside the record, and the per pair `archived-sessions.idx` is merged so an archived chat arrives
archived rather than back in the Active list.

WHAT IT CANNOT DO. The app reads records when it starts, so a chat copied in appears at its next
start, which is why the server runs this when the app closes. The palette lists at most 50 live and
50 archived local rows, so "everything" means everything its search can reach, not every row at
once. And the cloud half of those surfaces is scoped to the signed-in organisation by the server,
which nothing here touches.
"""
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

RECORD = "local_"
DELETED = "deleted_"
IDX = "archived-sessions.idx"
# The app skips a record file larger than this, so copying one would only waste the bytes.
MAX_RECORD_BYTES = 10 * 1024 * 1024
PART = ".c4x-part"


def pairs(roots=None) -> list:
    """Every `<account>\\<org>` directory the store knows, linked or not."""
    from c4x import accounts, store
    where = store.sessions_roots() if roots is None else roots
    return [p for r in where for p in accounts.pairs_on(r)]


def name_of(pair) -> str:
    """How a pair is named in a report: the two uuids, shortened the way the page shows them."""
    return f"{pair['account'][:8]}\\{pair['org'][:8]}"


def read_idx(folder) -> set:
    """The ids a pair calls archived. An unreadable or missing index is an empty set.

    THE APP TREATS IT AS A HINT and the truth is `isArchived` inside each record, but the hint is
    what it reads first, so a record copied in without its entry can come back as Active.
    """
    path = Path(folder) / IDX
    try:
        said = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    names = said.get("archived")
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def idx_bytes(ids) -> bytes:
    """The index as the app writes it: one line, version 1, sorted, UTF-8 with no BOM."""
    return json.dumps({"v": 1, "archived": sorted(ids)}, separators=(",", ":")).encode("utf-8")


def record_id(path) -> str | None:
    """The record's own `sessionId`, which is the key the app lists by, never the filename."""
    try:
        said = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    said_id = said.get("sessionId")
    return said_id if isinstance(said_id, str) and said_id else None


def index_pair(pair) -> dict:
    """What one pair holds: its record ids, its delete markers and its archived set.

    KEYED ON THE FILE STEM, not on the `sessionId` inside every file: the pairs hold a few hundred
    records of a few hundred KB each, and parsing all of them to build a set would read hundreds of
    megabytes for a question the filename already answers. Every writer names the file after the
    id (the app, `adopt`, and this module), and a file whose name and field disagree is caught when
    it is actually copied, which is the only moment it matters.
    """
    folder = Path(pair["path"])
    records: dict[str, str] = {}
    sizes: dict[str, int] = {}
    try:
        listing = sorted(folder.glob(f"{RECORD}*.json"))
    except OSError:
        listing = []
    for f in listing:
        try:
            sizes[f.stem] = f.stat().st_size
        except OSError:
            continue
        records[f.stem] = str(f)
    deleted = set()
    try:
        for marker in folder.glob(f"{DELETED}*"):
            if marker.is_file():
                deleted.add(RECORD + marker.name[len(DELETED):])
    except OSError:
        pass
    return {"pair": name_of(pair), "path": str(folder), "link": pair["link_to"],
            "records": records, "sizes": sizes, "deleted": deleted,
            "archived": read_idx(folder)}


def pick(wanted, ids) -> str | None:
    """One record id from a full id or an unambiguous prefix, or None."""
    if not wanted:
        return None
    if wanted in ids:
        return wanted
    hits = sorted(i for i in ids if i.startswith(wanted))
    return hits[0] if len(hits) == 1 else None


def plan_merge(indexes, only=None) -> dict:
    """Who copies what, decided without touching the disk.

    THE RULES, in the order they apply: a pair that is a link is left alone, because the app
    refuses to write through one; a record the pair already has is never overwritten, so two
    accounts that titled one chat differently keep their own copy and the difference is reported;
    a chat with a delete marker in ANY pair stays deleted; a record too large for the app to load
    is not copied anywhere.
    """
    source: dict[str, dict] = {}
    deleted: set = set()
    archived_in: dict[str, set] = {}
    for i in indexes:
        deleted |= i["deleted"]
        archived_in[i["pair"]] = i["archived"]
        for rid, path in i["records"].items():
            if rid not in source or i["sizes"][rid] > source[rid]["bytes"]:
                source[rid] = {"from": path, "bytes": i["sizes"][rid], "pair": i["pair"]}
    chosen = pick(only, set(source)) if only else None
    out: dict[str, Any] = {
        "ids": len(source), "copies": [], "idx_adds": {}, "targets": [],
        "skipped": {"link": [], "deleted": [], "too_large": [], "present": 0},
        "divergent": [], "only": only, "resolved": chosen}
    if only and chosen is None:
        out["skipped"]["unknown_id"] = only
        return out
    for rid in sorted(source):
        holders = [i for i in indexes if rid in i["records"]]
        sizes = {i["sizes"][rid] for i in holders}
        if len(sizes) > 1:
            out["divergent"].append({"id": rid, "pairs": [i["pair"] for i in holders],
                                     "bytes": sorted(sizes)})
    for i in indexes:
        if i["link"]:
            out["skipped"]["link"].append(i["pair"])
            continue
        out["targets"].append(i["pair"])
        for rid, src in sorted(source.items()):
            if chosen and rid != chosen:
                continue
            if rid in i["records"]:
                out["skipped"]["present"] += 1
                continue
            if rid in deleted:
                out["skipped"]["deleted"].append({"pair": i["pair"], "id": rid})
                continue
            if src["bytes"] > MAX_RECORD_BYTES:
                out["skipped"]["too_large"].append({"pair": i["pair"], "id": rid,
                                                    "bytes": src["bytes"]})
                continue
            out["copies"].append({"id": rid, "from": src["from"], "pair": i["pair"],
                                  "to": str(Path(i["path"]) / f"{rid}.json"),
                                  "bytes": src["bytes"]})
            # THE INDEX ENTRY FOLLOWS THE BYTES THAT TRAVEL, not the union across every pair. The
            # record carries its own `isArchived` and the copy is byte for byte, so asking any
            # other pair whether the chat is archived could file a record that says active under an
            # index that says archived. An independent review measured 7 of 223 ids archived under
            # one account and active under another, which is exactly that disagreement.
            if rid in archived_in.get(src["pair"], set()):
                out["idx_adds"].setdefault(i["pair"], []).append(rid)
    return out


def survey(roots=None, indexes=None) -> dict:
    """What each pair holds and what it lacks, writing nothing. The dashboard's question."""
    seen = indexes if indexes is not None else [index_pair(p) for p in pairs(roots)]
    said = plan_merge(seen)
    return {
        "pairs": [{"pair": i["pair"], "path": i["path"], "link": bool(i["link"]),
                   "records": len(i["records"]), "archived": len(i["archived"]),
                   "deleted_markers": len(i["deleted"]),
                   "missing": sum(1 for c in said["copies"] if c["pair"] == i["pair"])}
                  for i in seen],
        "ids": said["ids"], "to_copy": len(said["copies"]),
        "divergent": said["divergent"], "skipped": said["skipped"]}


def copy_one(item, copy=None, replace=None) -> dict:
    """One record into one pair, through a part file so the app never sees half of it.

    THE ID IS CHECKED HERE and nowhere else: `index_pair` keys on the filename for speed, so this
    is where a file whose `sessionId` disagrees with its name is caught and refused, since the app
    lists by the field and a mismatch would copy a chat under a name nothing resolves.
    """
    copier = shutil.copy2 if copy is None else copy
    move = os.replace if replace is None else replace
    inside = record_id(item["from"])
    if inside != item["id"]:
        return dict(item, done=False, why=f"the record calls itself {inside!r}, not {item['id']!r}")
    part = item["to"] + PART
    try:
        copier(item["from"], part)
        move(part, item["to"])
    except (OSError, shutil.Error) as exc:
        try:
            os.unlink(part)
        except OSError:
            pass
        return dict(item, done=False, why=str(exc))
    return dict(item, done=True, why=None)


def write_idx(folder, ids, write=None) -> None:
    """Replace a pair's index with the union it should hold now."""
    writer = (lambda p, b: Path(p).write_bytes(b)) if write is None else write
    writer(str(Path(folder) / IDX), idx_bytes(ids))


def merge(dry_run=False, only=None, roots=None, running=None, indexes=None,
          copy=None, replace=None, write=None) -> dict:
    """Make every real pair hold the union. Returns a report naming every file written.

    THE APP MAY BE OPEN FOR THE RECORDS, which are new files it will read at its next start, the
    same way `adopt` writes one while the app runs. It may NOT be open for the index: the app
    rewrites that file wholesale from its own set whenever the archived set changes, so a merge
    into it while the app is running is a write one side will lose. With the app open the index
    entries are reported as pending and the records still land.
    """
    from c4x import accounts
    alive = accounts.app_running() if running is None else running
    seen = indexes if indexes is not None else [index_pair(p) for p in pairs(roots)]
    said = plan_merge(seen, only=only)
    by_name = {i["pair"]: i for i in seen}
    report: dict[str, Any] = {
        "dry_run": bool(dry_run), "app_running": bool(alive), "only": only,
        "resolved": said["resolved"], "ids": said["ids"], "targets": said["targets"],
        "written": [], "failed": [], "idx_written": [], "idx_pending": {},
        "skipped": said["skipped"], "divergent": said["divergent"],
        "restart_required": False}
    if only and said["resolved"] is None:
        report["why"] = f"no record id starts with {only!r}, or more than one does"
        return report
    # AN ARCHIVED RECORD AND ITS INDEX ENTRY TRAVEL TOGETHER, OR NOT AT ALL. The index is the hint
    # the app reads first, and it can only be rewritten with the app closed (below), so copying an
    # archived record now would put it in the Active list until the next merge. There are about a
    # hundred archived chats per pair, and that is not a list to flood.
    archived_now = {rid for ids in said["idx_adds"].values() for rid in ids}
    report["deferred_archived"] = sorted(archived_now) if alive else []
    for item in said["copies"]:
        if alive and item["id"] in archived_now:
            continue
        if dry_run:
            report["written"].append(dict(item, done=False, why="dry run"))
            continue
        done = copy_one(item, copy=copy, replace=replace)
        (report["written"] if done["done"] else report["failed"]).append(done)
    for pair_name, ids in sorted(said["idx_adds"].items()):
        folder = by_name[pair_name]["path"]
        union = by_name[pair_name]["archived"] | set(ids)
        if dry_run or alive:
            report["idx_pending"][pair_name] = sorted(ids)
            continue
        write_idx(folder, union, write=write)
        report["idx_written"].append({"pair": pair_name, "added": sorted(ids),
                                      "entries": len(union)})
    report["restart_required"] = bool(report["written"]) and not dry_run
    return report


def sentence(report) -> str:
    """One line for the server's log and the page."""
    if report.get("why"):
        return report["why"]
    wrote = len([w for w in report["written"] if w.get("done")])
    # WHAT IS WAITING COUNTS AS WORK OUTSTANDING. An independent review found the line below
    # answering "every pair already holds every chat" to a real run that had deferred eight copies,
    # because the count of what was written is zero in exactly that case. The same misreport was
    # fixed once for the dry run alone; it is the deferral, not the dry run, that makes a zero
    # count mean something other than "nothing to do".
    held = len(set(report.get("deferred_archived") or []))
    if report.get("dry_run"):
        said = (f"dry run: {len(report['written'])} record(s) would be copied into "
                f"{len(report['targets'])} pair(s)")
        if held:
            said += (f", and {held} archived chat(s) would wait for Claude to close"
                     if report["written"] else
                     f"; {held} archived chat(s) wait for Claude to close")
        return said
    if not wrote and not report["failed"]:
        if held:
            return (f"nothing copied now: {held} archived chat(s) wait for Claude to close, so "
                    "they arrive archived rather than in Active")
        return "every pair already holds every chat"
    said = f"{wrote} record(s) copied into {len(report['targets'])} pair(s)"
    if report["failed"]:
        said += f", {len(report['failed'])} failed"
    if report.get("deferred_archived"):
        said += (f", {len(set(report['deferred_archived']))} archived chat(s) held back until "
                 "Claude closes, so they arrive archived rather than in Active")
    elif report["idx_pending"]:
        said += (f", {sum(len(v) for v in report['idx_pending'].values())} archived entr(ies) "
                 "pending until Claude closes")
    return said


def main(argv=None):
    """`python -m c4x.merge [--survey | --dry-run | --run] [--only <record id>] [--json]`"""
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    only = None
    if "--only" in argv:
        at = argv.index("--only")
        only = argv[at + 1] if at + 1 < len(argv) else None
    if "--run" in argv or "--dry-run" in argv:
        said = merge(dry_run="--dry-run" in argv, only=only)
        print(json.dumps(said, indent=1) if as_json else sentence(said))
        return 1 if said["failed"] else 0
    said = survey()
    if as_json:
        print(json.dumps(said, indent=1))
        return 0
    print(f"{said['ids']} chat(s) have a record somewhere; {said['to_copy']} copy/copies would "
          "make every pair hold them all")
    for p in said["pairs"]:
        mark = " (link, left alone)" if p["link"] else ""
        print(f"  {p['pair']}  {p['records']:>4} records, {p['archived']:>3} archived, "
              f"missing {p['missing']}{mark}")
    if said["divergent"]:
        print(f"  {len(said['divergent'])} id(s) differ between pairs and are left as they are")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

r"""Which chats lost their record because the app refused to write it, and what to adopt.

THE DAMAGE THIS REPORTS. On a machine where the account directories are links, build 2.26454.0.0
refuses every record write with `PlantDetectedError` and logs it (see `c4x/applog.py`). A chat
whose record never landed is not in the app's list at all, however complete its transcript is. The
record is also the only file tying a desktop id to a transcript, so the app's own pairing line is
what makes recovery possible at all.

WHAT IT DOES, and what it deliberately does not. It reads the log, pairs every refused record id
with its CLI session, asks whether a record exists for that session now, and whether the transcript
is still on disk; then it groups what is missing by working directory, because `c4x.adopt` writes
records a folder at a time. It writes nothing: adopting is a separate, asking step (the dashboard's
Adopt drawer, or `python -m c4x.adopt`), so a report can be read before anything is written.

EVERY SESSION IS ACCOUNTED FOR. A refused id with no pairing line, no transcript or no readable
`cwd` is printed with the reason rather than dropped, since a silent skip in a recovery tool is
indistinguishable from nothing to recover.

    python tools/recover_refused.py [--json]
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c4x import applog  # noqa: E402

TRANSCRIPTS = Path(os.path.expanduser("~")) / ".claude" / "projects"


def records_on_disk(pair_dirs) -> tuple:
    """`(record id to file, cliSessionId to file)` over the pairs given.

    BOTH KEYS MATTER, and the first version had only the second, which reported a chat that is
    sitting in the app's list as unrecoverable: a refused write often lands later, under the real
    directory, keeping its own id, and that id is the only thing some refusals can be matched by
    (the pairing line that would give the transcript is in a log that has since rotated away).
    """
    by_id: dict[str, str] = {}
    by_session: dict[str, str] = {}
    for d in pair_dirs:
        for marker in sorted(Path(d).glob("deleted_*")):
            # DELETED ON PURPOSE, NOT LOST. The app leaves `deleted_<uuid>` when a chat is deleted
            # in the interface, and the uuid is the record's. Measured on this machine: two of the
            # refused sessions were app scratch workspaces the user had deleted, and the first
            # version of this tool offered both for adoption, which would have resurrected them.
            # `c4x.adopt` refuses them for the same reason, so it never offered those folders.
            by_id.setdefault("local_" + marker.name[len("deleted_"):], "deleted:" + str(marker))
        for path in sorted(Path(d).glob("local_*.json")):
            by_id.setdefault(path.stem, str(path))
            try:
                said = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cli = said.get("cliSessionId")
            if isinstance(cli, str) and cli:
                by_session.setdefault(cli, str(path))
    return by_id, by_session


def transcript_of(session: str, root=None) -> str | None:
    """The transcript for a CLI session id, or None. One file per session, named for it."""
    base = TRANSCRIPTS if root is None else Path(root)
    found = sorted(base.glob(f"*/{session}.jsonl"))
    return str(found[0]) if found else None


def cwd_of(transcript) -> str | None:
    """The working directory the transcript records, from the first line that carries one."""
    try:
        with open(str(transcript), encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n > 40:
                    break
                try:
                    said = json.loads(line)
                except ValueError:
                    continue
                if isinstance(said, dict) and isinstance(said.get("cwd"), str) and said["cwd"]:
                    return said["cwd"]
    except OSError:
        return None
    return None


def report(pair_dirs=None, transcripts=None, refused=None, paired=None) -> dict:
    """Refused records, each with what became of it. Nothing is written.

    EVERY DOOR INJECTABLE, so `--self-test` can drive it on a fixture tree: the log scan, the
    pairing map, the pair directories and the transcripts root. Left alone they come from this
    machine.
    """
    from c4x import accounts, store
    refused = applog.scan() if refused is None else refused
    paired = applog.trace() if paired is None else paired
    if pair_dirs is None:
        pair_dirs = [p["path"] for r in store.sessions_roots() for p in accounts.pairs_on(r)]
    by_id, have = records_on_disk(pair_dirs)
    out: dict = {"refused": refused["count"], "sessions": len(refused["sessions"]),
                 "pairs": refused["pairs"], "first": refused["first"], "last": refused["last"],
                 "recovered_already": [], "to_adopt": {}, "deleted_on_purpose": [],
                 "unrecoverable": []}
    for record_id in refused["sessions"]:
        # THE RECORD ITSELF FIRST. A refusal is not a loss when the same id landed later, under
        # the real directory, and that is the common case for a chat the user kept using.
        if record_id in by_id:
            where = by_id[record_id]
            group = "deleted_on_purpose" if where.startswith("deleted:") else "recovered_already"
            out[group].append({"record": record_id, "session": paired.get(record_id),
                               "record_file": where})
            continue
        cli = paired.get(record_id)
        if cli is None:
            out["unrecoverable"].append({"record": record_id,
                                         "why": "no record on disk and no pairing line in the log"})
            continue
        if cli in have:
            out["recovered_already"].append({"record": record_id, "session": cli,
                                             "record_file": have[cli]})
            continue
        path = transcript_of(cli, transcripts)
        if path is None:
            out["unrecoverable"].append({"record": record_id, "session": cli,
                                         "why": "no transcript on disk"})
            continue
        folder = cwd_of(path)
        if folder is None:
            out["unrecoverable"].append({"record": record_id, "session": cli,
                                         "why": f"no cwd in {path}"})
            continue
        out["to_adopt"].setdefault(folder, []).append({"record": record_id, "session": cli,
                                                       "transcript": path})
    return out


def self_test() -> int:
    """Drive `report` on a fixture tree, including the case that made it lie.

    THE DEFECT THIS PINS. The first version asked the log for a pairing line before asking whether
    a record exists, so every session whose refused write landed later, keeping its own id, was
    reported as a loss: 63 of 73 on the machine this was written for, the chat that started the
    investigation among them. A record present on disk is the first question now.
    """
    import tempfile
    checks: list = []

    def ok(name, got, want):
        checks.append((name, got == want, got, want))

    with tempfile.TemporaryDirectory() as box:
        box = Path(box)
        pair = box / "records" / "acct" / "org"
        pair.mkdir(parents=True)
        # A record whose own id was refused and landed later, keeping that id.
        (pair / "local_landed.json").write_text(json.dumps({"cliSessionId": "s-landed"}),
                                                encoding="utf-8")
        # A record for a session whose refused id differs (a fork writes a new desktop id).
        (pair / "local_other.json").write_text(json.dumps({"cliSessionId": "s-renamed"}),
                                               encoding="utf-8")
        # THE CASE THAT MADE THE FIRST VERSION LIE, and the one the fixture first lacked: a record
        # on disk whose id the log never paired with a session, because the log that held the
        # pairing has since rotated away. It is in the app's list; only its own id can find it.
        (pair / "local_nopair.json").write_text(json.dumps({"cliSessionId": "s-unseen",
                                                            "title": "n8n"}), encoding="utf-8")
        # A chat the user DELETED in the app: the record is gone on purpose and the marker says so.
        (pair / "deleted_gonebyhand").write_text("1760000000000", encoding="utf-8")
        projects = box / "projects"
        (projects / "P--thing").mkdir(parents=True)
        (projects / "P--thing" / "s-orphan.jsonl").write_text(
            json.dumps({"type": "user", "cwd": r"P:\thing", "sessionId": "s-orphan"}) + "\n",
            encoding="utf-8")
        (projects / "P--thing" / "s-nocwd.jsonl").write_text(
            json.dumps({"type": "summary", "summary": "no cwd here"}) + "\n", encoding="utf-8")
        refused = {"count": 9, "records": 9, "other": 0, "pairs": {"acct\\org": 9}, "days": {},
                   "sessions": ["local_landed", "local_renamed", "local_orphan", "local_gone",
                                "local_unpaired", "local_nocwd", "local_nopair",
                                "local_gonebyhand"],
                   "first": "2026-09-14 23:31:41", "last": "2026-10-10 12:32:40"}
        paired = {"local_landed": "s-landed", "local_renamed": "s-renamed",
                  "local_orphan": "s-orphan", "local_gone": "s-vanished",
                  "local_nocwd": "s-nocwd"}
        said = report(pair_dirs=[pair], transcripts=projects, refused=refused, paired=paired)

        got = {i["record"] for i in said["recovered_already"]}
        ok("a refused id whose record is on disk is not a loss", "local_landed" in got, True)
        ok("a record naming the session under another id is not a loss either",
           "local_renamed" in got, True)
        ok("a record the log never paired is found by its OWN id, not called a loss",
           "local_nopair" in got, True)
        ok("and it is not in the unrecoverable list",
           "local_nopair" in {i["record"] for i in said["unrecoverable"]}, False)
        ok("a chat deleted in the app is named as that, not offered for adoption",
           [i["record"] for i in said["deleted_on_purpose"]], ["local_gonebyhand"])
        ok("so it is not in the adoption list either",
           "local_gonebyhand" in {i["record"] for v in said["to_adopt"].values() for i in v},
           False)
        ok("the session with a transcript and no record is offered for adoption",
           {i["record"] for v in said["to_adopt"].values() for i in v}, {"local_orphan"})
        ok("grouped by the folder its transcript records", list(said["to_adopt"]), [r"P:\thing"])
        why = {i["record"]: i["why"] for i in said["unrecoverable"]}
        ok("a pairing line with no transcript is named", why.get("local_gone"),
           "no transcript on disk")
        ok("a session the log never paired is named", why.get("local_unpaired"),
           "no record on disk and no pairing line in the log")
        ok("a transcript with no cwd is named rather than dropped",
           (why.get("local_nocwd") or "").startswith("no cwd in"), True)
        every = (len(said["recovered_already"]) + sum(len(v) for v in said["to_adopt"].values())
                 + len(said["deleted_on_purpose"]) + len(said["unrecoverable"]))
        ok("every refused session is in exactly one group", every, len(refused["sessions"]))
        ok("the refusal totals are carried through", (said["refused"], said["sessions"]),
           (9, len(refused["sessions"])))
        ok("the transcript lookup finds one by name",
           Path(transcript_of("s-orphan", projects)).name, "s-orphan.jsonl")
        ok("and answers None for one that is not there", transcript_of("s-missing", projects), None)
        ok("the cwd comes from the first line that carries one",
           cwd_of(projects / "P--thing" / "s-orphan.jsonl"), r"P:\thing")
        ok("a transcript with no cwd answers None",
           cwd_of(projects / "P--thing" / "s-nocwd.jsonl"), None)
        by_id, by_session = records_on_disk([pair])
        ok("records are indexed by their own id", sorted(by_id),
           ["local_landed", "local_nopair", "local_other"])
        ok("and by the session they name", sorted(by_session),
           ["s-landed", "s-renamed", "s-unseen"])
        ok("a directory with no records is empty rather than an error",
           records_on_disk([box / "nowhere"]), ({}, {}))

    bad = [c for c in checks if not c[1]]
    for name, passed, got, want in checks:
        if not passed:
            print(f"  FAIL {name}: got {got!r}, wanted {want!r}")
    print(f"SELF-TEST {'FAIL' if bad else 'PASS'} ({len(checks)} checks)")
    return 1 if bad else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in argv:
        return self_test()
    said = report()
    if "--json" in argv:
        print(json.dumps(said, indent=1))
        return 0
    print(applog.sentence({"count": said["refused"], "pairs": said["pairs"],
                           "sessions": [None] * said["sessions"],
                           "first": said["first"], "last": said["last"]}))
    print(f"  {len(said['recovered_already'])} of those sessions has a record now "
          "(the write landed later, under the real directory)")
    if said["deleted_on_purpose"]:
        print(f"  {len(said['deleted_on_purpose'])} were deleted in the app afterwards, so their "
              "record is gone on purpose and nothing should bring them back")
    missing = sum(len(v) for v in said["to_adopt"].values())
    print(f"  {missing} session(s) in {len(said['to_adopt'])} folder(s) have a transcript and no "
          "record, so the app does not list them:")
    for folder, items in sorted(said["to_adopt"].items()):
        print(f"    {folder}  ({len(items)} session(s))")
    for item in said["unrecoverable"]:
        print(f"  unrecoverable: {item.get('record')}: {item['why']}")
    if said["to_adopt"]:
        print("\nAdopt those folders to put them back: the dashboard's Adopt drawer, or "
              "python -m c4x.adopt --folder <path>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

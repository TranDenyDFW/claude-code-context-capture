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


def report(pair_dirs=None, transcripts=None) -> dict:
    """Refused records, each with what became of it. Nothing is written."""
    from c4x import accounts, store
    refused = applog.scan()
    paired = applog.trace()
    if pair_dirs is None:
        pair_dirs = [p["path"] for r in store.sessions_roots() for p in accounts.pairs_on(r)]
    by_id, have = records_on_disk(pair_dirs)
    out: dict = {"refused": refused["count"], "sessions": len(refused["sessions"]),
                 "pairs": refused["pairs"], "first": refused["first"], "last": refused["last"],
                 "recovered_already": [], "to_adopt": {}, "unrecoverable": []}
    for record_id in refused["sessions"]:
        # THE RECORD ITSELF FIRST. A refusal is not a loss when the same id landed later, under
        # the real directory, and that is the common case for a chat the user kept using.
        if record_id in by_id:
            out["recovered_already"].append({"record": record_id, "session": paired.get(record_id),
                                             "record_file": by_id[record_id]})
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    said = report()
    if "--json" in argv:
        print(json.dumps(said, indent=1))
        return 0
    print(applog.sentence({"count": said["refused"], "pairs": said["pairs"],
                           "sessions": [None] * said["sessions"],
                           "first": said["first"], "last": said["last"]}))
    print(f"  {len(said['recovered_already'])} of those sessions has a record now "
          "(the write landed later, under the real directory)")
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

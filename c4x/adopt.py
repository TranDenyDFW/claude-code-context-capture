"""Give the desktop app a record for a session it has none for.

The app lists a session because a `local_<uuid>.json` names it under `<records root>/<account>/
<org>/`, and it writes that file itself. A reinstall (a new device identity, a renamed data
directory) leaves every transcript in `~/.claude/projects` and none of the records, so the app
shows a cloud list pointing at a device that no longer exists and nothing local. Measured on the
test laptop after such a reinstall: 121 transcripts, 1 record.

WHAT WAS PROVEN before this was written. One record built from the store for a 37-turn session,
the nine fields every real record carries plus the transcript's id and a title, written under
the signed-in account's pair while the app was open: after a restart the sidebar listed it as a
local session under its project folder. That is the whole mechanism, and this module is that
write made repeatable and chosen.

CHOSEN, never wholesale. On this machine 923 of the 1,027 desktop sessions with a transcript
have no record, 915 of them from one month, and the app leaves a `deleted_<uuid>` marker when a
chat is deleted on purpose: a deleted chat and a reinstall orphan look the same to the rule below,
and the store cannot tell them apart because it never learns a record's uuid. So `state` groups
the candidates by folder and reports how many deleted markers the pair holds, and the page
preselects nothing.

NEVER ACROSS PAIRS. A record is written only into the signed-in pair (`appstate.desktop_pair`,
the same decision an import makes); a session whose record sits under another account's pair is
counted as `other_account` and left alone. Under sharing All the signed-in pair is a junction and
the bytes land in its target, which IS the shared list; the report says so. `c4x/accounts.py` is
not touched by anything here.

EVERY RECORD CARRIES A NAME. The first cut wrote `title` only from the store's `custom` or `ai`
kinds, on the theory that the app would name the rest by its own rule. It does: a record with no
title shows as "General coding session", every one of them, and on the test laptop that was 64 of
82. The store has a real name for every session (`session_titles`, where `last-prompt` is the
OPENING request cut to 200, then the first typed prompt in `messages`), so `title_for` always
answers, and `retitle` gives the records a first build left nameless the same name through the
ledger.
"""
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DESKTOP = "claude-desktop"
LEDGER = "adopted-records.json"
# A prompt-derived name is cut here, on a word boundary. Longer than the page's 40 (labels.py):
# the sidebar has the room, and a name cut mid-word reads as a defect.
TITLE_MAX = 60
# Every field every real record carries (189 of 189 on this machine), plus the link and the name.
FIELDS = ("createdAt", "cwd", "isArchived", "lastActivityAt", "model", "originCwd",
          "permissionMode", "remoteMcpServersConfig", "sessionId")


class SharingMismatch(RuntimeError):
    """Sharing is on and the signed-in pair is not part of it: a write would start a second list."""


def ledger_path() -> Path:
    """Beside the store, like the sharing marker: the one place that knows what c4x wrote."""
    from c4x import store
    return Path(store.DB_PATH).parent / LEDGER


def to_ms(ts: str) -> int:
    """An ISO timestamp as the app stores time: integer milliseconds since the epoch, UTC."""
    text = str(ts).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def is_subagent_path(path: Any) -> bool:
    """Split on both separators: a Windows-written row is judged the same way on ubuntu."""
    return "subagents" in re.split(r"[\\/]", str(path or ""))


def is_desktop(entrypoint: Any) -> bool:
    """The page's own rule (`store.py` classify): a NULL entrypoint is a desktop session."""
    return not isinstance(entrypoint, str) or not entrypoint or entrypoint == DESKTOP


def _records_in(folder: Path) -> set[str]:
    """The transcript ids the records in one directory name (through a junction, if it is one)."""
    from c4x import store
    found: set[str] = set()
    if not folder.is_dir():
        return found
    for path in folder.glob("*.json"):
        row = store.read_archived_record(str(path))
        if row is not None:
            found.add(row[0])
    return found


def _sessions() -> list:
    """Every session with what a record needs, from the store; turns decide the times and model."""
    from c4x import store
    df = store.q("""
        SELECT s.session_id, s.cwd, s.entrypoint, s.transcript_path,
               MIN(t.ts) AS first_ts, MAX(t.ts) AS last_ts, COUNT(t.uuid) AS turns,
               (SELECT t2.model FROM turns t2
                 WHERE t2.session_id = s.session_id AND t2.model IS NOT NULL
                 ORDER BY t2.ts DESC LIMIT 1) AS model
          FROM sessions s LEFT JOIN turns t ON t.session_id = s.session_id
         GROUP BY s.session_id""")
    # THE OPENING TYPED PROMPT, for the sessions the titles table does not name. Only the typed
    # rows are read (15K of 367K messages on the author's store) and only their first 400
    # characters; the earliest per session wins, decided here rather than in a correlated query.
    first: dict[str, tuple[str, str]] = {}
    typed = store.q_optional("""
        SELECT session_id, ts, substr(text, 1, 400) AS text FROM messages
         WHERE type = 'typed' AND role = 'user' AND COALESCE(is_sidechain, 0) = 0
           AND COALESCE(chars, 0) > 0 AND text IS NOT NULL""", columns=("session_id", "ts", "text"))
    for m in typed.itertuples(index=False):
        sid, ts = str(m.session_id), str(m.ts)
        if sid not in first or ts < first[sid][0]:
            first[sid] = (ts, str(m.text))
    rows = []
    for r in df.itertuples(index=False):
        rows.append({"session_id": r.session_id, "cwd": r.cwd, "entrypoint": r.entrypoint,
                     "transcript_path": r.transcript_path, "first_ts": r.first_ts,
                     "last_ts": r.last_ts, "turns": int(r.turns or 0),
                     "model": r.model if isinstance(r.model, str) else None,
                     "first_prompt": first.get(str(r.session_id), ("", None))[1]})
    return rows


def cut(text: Any, limit: int = TITLE_MAX) -> str:
    """Whitespace collapsed, and cut on a word boundary with `...` when longer than `limit`."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    head = flat[:limit]
    if " " in head:
        head = head[:head.rfind(" ")]
    return head.rstrip(" ,;:.") + "..."


def title_for(kinds: dict, first_prompt: Any = None, last_ts: Any = None) -> tuple[str, str]:
    """(title, titleSource) for a record: a name a person or a model chose, else the opening
    request cut short, else the date. The source is `user` for a `custom` title, which a person
    typed, and `auto` for everything else, which is what the app writes for names it made itself."""
    for kind, source in (("custom", "user"), ("ai", "auto")):
        text = kinds.get(kind)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split()), source
    text = kinds.get("last-prompt")
    if isinstance(text, str) and text.strip():
        return cut(text), "auto"
    if isinstance(first_prompt, str) and first_prompt.strip():
        return cut(first_prompt), "auto"
    return f"Chat from {str(last_ts or '')[:10] or 'an unknown date'}", "auto"


def state(root=None, include_cli=False) -> dict:
    """What could be adopted, grouped by folder, and where a record would land."""
    from c4x import accounts, appstate, reviews, store
    root = str(root or appstate.sessions_root())
    pair = appstate.desktop_pair(root)
    base: dict[str, Any] = {"supported": False, "why_not": "", "pair": None, "physical": None,
                            "groups": [], "candidates": 0, "cli_candidates": 0, "other_account": 0,
                            "deleted_markers": 0, "app_running": False, "sharing": None}
    if not pair:
        base["why_not"] = ("this machine has no desktop account and organisation to file a record "
                           "under, so an adopted session would not appear in the app")
        return base
    pair_dir = Path(root) / pair["account"] / pair["org"]
    physical = accounts.link_target(pair_dir) or str(pair_dir)

    # EVERY root the app might read: a record under the root the app is not writing to is still a
    # record (see store.claude_appdata_roots).
    known: set = set()
    for one in appstate.sessions_roots():
        known.update(store.desktop_records(one, ttl=0))
    mine = _records_in(pair_dir)
    other_account = known - mine

    rows = _sessions()
    ids = [r["session_id"] for r in rows]
    titles = store.titles_for(ids) if ids else {}
    ledger = _ledger_records()
    eligible: list = []
    counted_cli = 0
    seen_other = 0
    for r in rows:
        sid = r["session_id"]
        path = r["transcript_path"]
        if not isinstance(path, str) or not path or not os.path.exists(path):
            continue
        if is_subagent_path(path):
            continue
        if r["turns"] < 1 or not r["model"] or not r["first_ts"] or not r["last_ts"]:
            continue
        if sid in mine:
            continue
        if sid in other_account:
            seen_other += 1
            continue
        cli = not is_desktop(r["entrypoint"])
        if cli:
            counted_cli += 1
            if not include_cli:
                continue
        eligible.append((r, cli))
    # A REVIEW RUN IS NEVER OFFERED. It folds into the chat it reviewed (harvest's `review_links`,
    # read through `c4x.reviews`), and a record for it would put a reviewer's reading of a chat
    # in the app's sidebar as a chat of its own, which is what the first build did 58 times on
    # the test laptop. Counted, so the page can say how many were left out and why.
    runs = reviews.reviewed_by([r["session_id"] for r, _cli in eligible])
    eligible = [(r, cli) for r, cli in eligible if r["session_id"] not in runs]
    every_run = reviews.reviewed_by()
    by_cwd: dict[str, list] = {}
    for r, cli in eligible:
        sid = r["session_id"]
        title, source = title_for(titles.get(sid, {}), r.get("first_prompt"), r["last_ts"])
        by_cwd.setdefault(str(r["cwd"] or ""), []).append({
            "session_id": sid, "title": title, "title_source": source, "last_ts": r["last_ts"],
            "first_ts": r["first_ts"], "turns": r["turns"], "model": r["model"], "cli": cli,
            "cwd": r["cwd"]})
    groups = []
    for cwd, sessions in by_cwd.items():
        sessions.sort(key=lambda s: str(s["last_ts"]), reverse=True)
        name = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] if cwd else "(no folder)"
        groups.append({"cwd": cwd, "project": name or cwd, "count": len(sessions),
                       "newest": sessions[0]["last_ts"], "sessions": sessions})
    groups.sort(key=lambda g: str(g["newest"]), reverse=True)
    return {**base, "supported": True,
            "pair": {"account": pair["account"], "org": pair["org"], "root": root,
                     "source": pair["source"]},
            "physical": physical, "groups": groups,
            "candidates": sum(1 for g in groups for s in g["sessions"] if not s["cli"]),
            "cli_candidates": counted_cli, "other_account": seen_other,
            "deleted_markers": len(list(pair_dir.glob("deleted_*"))) if pair_dir.is_dir() else 0,
            "untitled_adopted": sum(1 for _e, rec, _p in ledger
                                    if rec is not None and not _titled(rec)),
            # The runs among what would otherwise be offered, and the records a first build
            # wrote for runs that are still on disk: the page offers to take those back.
            "review_runs": len(runs),
            "review_records": sum(1 for e, rec, _p in ledger
                                  if rec is not None and str(e.get("session_id")) in every_run),
            "app_running": accounts.app_running(), "sharing": accounts.intended_mode()}


def record_for(session: dict) -> dict:
    """The record: the nine fields, the link, and always a name with where it came from."""
    cwd = session["cwd"]
    title = session.get("title") or f"Chat from {str(session.get('last_ts') or '')[:10]}"
    return {
        "sessionId": f"local_{uuid.uuid4()}",
        "cliSessionId": session["session_id"],
        "cwd": cwd,
        "originCwd": cwd,
        "createdAt": to_ms(session["first_ts"]),
        "lastActivityAt": to_ms(session["last_ts"]),
        "model": session["model"],
        "isArchived": False,
        "permissionMode": "default",
        "remoteMcpServersConfig": [],
        "title": title,
        "titleSource": session.get("title_source") or "auto",
    }


def _titled(record: dict) -> bool:
    text = record.get("title")
    return isinstance(text, str) and bool(text.strip())


def _load_ledger() -> list:
    """The ledger's entries, or an empty list for a missing or unreadable file."""
    try:
        entries = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def _save_ledger(entries: list) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")


def _resolve_record(entry: dict, roots: list) -> tuple:
    """(record or None, path or None): the ledger's path when it exists, else the same file name
    under the same pair below every records root this machine has.

    THE LEDGER PATH IS THE WRITER'S VIEW. On a packaged install the server is a descendant of the
    app and its `%APPDATA%` writes are redirected into the package's `LocalCache`, so the path it
    recorded need not exist for a reader outside that container.
    """
    written = Path(str(entry["path"]))
    candidates = [written] + [root / written.parent.parent.name / written.parent.name
                              / written.name for root in roots]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        return None, None
    try:
        loaded = json.loads(found.read_text(encoding="utf-8"))
        return (loaded if isinstance(loaded, dict) else None), found
    except (OSError, ValueError):
        return None, found


def _ledger_records(roots=None) -> list:
    """(entry, record or None, path or None) for every ledger entry still in force, the file
    resolved. An entry stamped `removed_at` was taken back and is no record of c4x's any more."""
    from c4x import appstate
    roots = [Path(r) for r in (roots if roots is not None else appstate.sessions_roots())]
    out = []
    for entry in _load_ledger():
        if not isinstance(entry, dict) or not entry.get("path") or entry.get("removed_at"):
            continue
        record, found = _resolve_record(entry, roots)
        out.append((entry, record, found))
    return out


def retitle(root=None) -> dict:
    """Name every record c4x wrote that has no name, the way `record_for` would today.

    Only the ledger's records, and only the nameless: a title a person typed (`titleSource`
    `user`) or any non-blank title is left alone and counted as kept. Read, written, read back.
    """
    from c4x import store
    report: dict[str, Any] = {"renamed": [], "kept": 0, "missing": 0, "restart_required": False}
    resolved = _ledger_records()
    wanted = {str(e.get("session_id")) for e, _r, _p in resolved}
    by_id = {r["session_id"]: r for r in _sessions() if r["session_id"] in wanted}
    titles = store.titles_for(list(wanted)) if wanted else {}
    for entry, record, path in resolved:
        if record is None or path is None:
            report["missing"] += 1
            continue
        if _titled(record):
            report["kept"] += 1
            continue
        sid = str(entry.get("session_id"))
        session = by_id.get(sid)
        title, source = title_for(titles.get(sid, {}), (session or {}).get("first_prompt"),
                                  (session or {}).get("last_ts") or record.get("lastActivityAt"))
        record["title"], record["titleSource"] = title, source
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        try:
            path.write_bytes(blob)
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashlib.sha256(blob).hexdigest():
                raise OSError(f"{path} does not read back as what was written")
        except OSError as exc:
            report.setdefault("failed", []).append({"session_id": sid, "why": str(exc)})
            continue
        report["renamed"].append({"session_id": sid, "path": str(path), "title": title})
    report["restart_required"] = bool(report["renamed"])
    return report


def unadopt_reviews() -> dict:
    """Take back the records c4x wrote for review runs.

    A run folds into the chat it reviewed (harvest's `review_links`) and is no chat of its own, in
    this store or in the app; the first build adopted 58 of them on the test laptop. Only the
    ledger's records and only the runs: the file is removed, the ledger entry is stamped
    `removed_at` and kept, so what was written and taken back stays on record, and a stamped entry
    is out of every later count. The app reads the directory when it starts, hence the restart.

    WHAT THE APP ITSELF WRITES ON A DELETE is not mimicked beyond the removal, because it has not
    been measured: docs/desktop-records.md records that a `deleted_<record uuid>` marker appears
    and nothing about its content. It is measured on the laptop before this runs there.
    """
    from c4x import appstate, reviews
    report: dict[str, Any] = {"removed": [], "missing": 0, "kept": 0, "restart_required": False}
    runs = reviews.reviewed_by()
    roots = [Path(r) for r in appstate.sessions_roots()]
    entries = _load_ledger()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path") or entry.get("removed_at"):
            continue
        sid = str(entry.get("session_id"))
        if sid not in runs:
            report["kept"] += 1
            continue
        _record, path = _resolve_record(entry, roots)
        if path is None:
            # Nothing left to remove; the entry is closed all the same, so it is never counted
            # again as a record c4x holds.
            report["missing"] += 1
            entry["removed_at"] = stamp
            changed = True
            continue
        try:
            path.unlink()
        except OSError as exc:
            report.setdefault("failed", []).append({"session_id": sid, "why": str(exc)})
            continue
        entry["removed_at"] = stamp
        changed = True
        report["removed"].append({"session_id": sid, "path": str(path), "reviewed": runs[sid]})
    if changed:
        _save_ledger(entries)
    report["restart_required"] = bool(report["removed"])
    return report


def _check_sharing(root: str, pair_dir: Path) -> None:
    """Under sharing All the signed-in pair must be a link or the directory the links point at."""
    from c4x import accounts
    if accounts.intended_mode() != accounts.ALL:
        return
    if accounts.link_target(pair_dir) is not None:
        return
    head = accounts.canonical_pair(accounts.pairs_on(root))
    if head and Path(head["path"]).resolve() == pair_dir.resolve():
        return
    raise SharingMismatch(
        "sharing is on but the signed-in pair is not part of it; turn sharing off and on again, "
        "then adopt")


def _append_ledger(entries: list) -> None:
    current = _load_ledger()
    current.extend(entries)
    _save_ledger(current)


def adopt(cwds, root=None, include_cli=False, dry_run=False) -> dict:
    """Write a record for every candidate under the chosen folders. Same discipline as
    `appstate.restore`: write bytes, read back, compare, count only what read back."""
    from c4x import appstate
    root = str(root or appstate.sessions_root())
    current = state(root, include_cli=include_cli)
    report: dict[str, Any] = {"supported": current["supported"], "why_not": current["why_not"],
                              "pair": current["pair"], "physical": current["physical"],
                              "note": None, "written": [], "skipped": [], "selected": 0,
                              "restart_required": False, "dry_run": bool(dry_run)}
    if not current["supported"]:
        return report
    wanted = {str(c) for c in (cwds or []) if isinstance(c, str) and c}
    chosen = [s for g in current["groups"] if g["cwd"] in wanted for s in g["sessions"]]
    if not chosen:
        raise ValueError("nothing to adopt under the folders given: they hold no session without "
                         "a record, or they were not among the folders offered")
    report["selected"] = len(chosen)
    pair_dir = Path(root) / current["pair"]["account"] / current["pair"]["org"]
    _check_sharing(root, pair_dir)
    if str(current["physical"]) != str(pair_dir):
        report["note"] = ("sharing is on: these records live under the shared directory and stay "
                          "there if sharing is turned off")

    entries = []
    for session in chosen:
        record = record_for(session)
        path = pair_dir / f"{record['sessionId']}.json"
        if dry_run:
            report["written"].append({"session_id": session["session_id"], "path": str(path),
                                      "title": record.get("title")})
            continue
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        try:
            pair_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            landed = path.read_bytes()
        except OSError as exc:
            report["skipped"].append({"session_id": session["session_id"], "why": str(exc)})
            continue
        if hashlib.sha256(landed).hexdigest() != hashlib.sha256(blob).hexdigest():
            report["skipped"].append({"session_id": session["session_id"],
                                      "why": f"{path} does not read back as what was written"})
            continue
        report["written"].append({"session_id": session["session_id"], "path": str(path),
                                  "title": record.get("title")})
        entries.append({"session_id": session["session_id"], "record": record["sessionId"],
                        "path": str(path),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if entries:
        _append_ledger(entries)
    report["restart_required"] = bool(report["written"]) and not dry_run
    return report

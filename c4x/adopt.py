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
    rows = []
    for r in df.itertuples(index=False):
        rows.append({"session_id": r.session_id, "cwd": r.cwd, "entrypoint": r.entrypoint,
                     "transcript_path": r.transcript_path, "first_ts": r.first_ts,
                     "last_ts": r.last_ts, "turns": int(r.turns or 0),
                     "model": r.model if isinstance(r.model, str) else None})
    return rows


def best_title(kinds: dict) -> str | None:
    """`custom`, then `ai`. NEVER `last-prompt`: that is the raw last prompt, up to 200 characters,
    and the app titles a record with no title by its own rule, which is what was observed."""
    for kind in ("custom", "ai"):
        text = kinds.get(kind)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def state(root=None, include_cli=False) -> dict:
    """What could be adopted, grouped by folder, and where a record would land."""
    from c4x import accounts, appstate, store
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
    by_cwd: dict[str, list] = {}
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
        by_cwd.setdefault(str(r["cwd"] or ""), []).append({
            "session_id": sid, "title": best_title(titles.get(sid, {})), "last_ts": r["last_ts"],
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
            "app_running": accounts.app_running(), "sharing": accounts.intended_mode()}


def record_for(session: dict) -> dict:
    """The record, the nine fields plus the link and, when the store has a real name, the title."""
    cwd = session["cwd"]
    out = {
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
    }
    if session.get("title"):
        out["title"] = session["title"]
    return out


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
    path = ledger_path()
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, list):
            current = []
    except (OSError, ValueError):
        current = []
    current.extend(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=1, ensure_ascii=False), encoding="utf-8")


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

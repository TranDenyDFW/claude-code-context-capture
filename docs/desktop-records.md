# How the desktop app addresses a session

The requirement is that an imported project SHOWS UP IN THE DESKTOP APP. That turns on two
questions this file answers with evidence rather than assumption: where a record has to be written,
and whether writing the file is enough.

## 1. The path is `<account uuid>/<org uuid>/local_<uuid>.json`, and both uuids are the MACHINE's

    %APPDATA%\Claude\claude-code-sessions\<account>\<org>\local_<desktop session uuid>.json

Counted on this machine: 184 `local_*.json` records plus 9 `scheduled-tasks.json`, every one of
them at exactly that depth.

The two directory levels are NOT per project. One pair holds 171 records covering 57 distinct
working directories, and 6 working directories appear under two different pairs, so a pair cannot
be derived from a cwd. They were identified by finding the same uuids named elsewhere:

| Level | What it is | Where it is named |
|---|---|---|
| 1 | account uuid | `config.json` key `lastKnownAccountUuid`, and the top-level keys of `ant-device-registry.json` |
| 2 | organisation uuid | `plan-usage-history.json` samples, field `org` |

**This is the finding that makes or breaks the feature.** An import that reproduces the source
machine's relative path files the record under a FOREIGN account uuid, where the destination app
never looks. So the record's relative path is carried as the FILENAME ONLY, and the account and
org directories are resolved on the importing machine.

Three sources agree on which pair is active here, which is why the resolver cross-checks them
rather than trusting one:

    config.json lastKnownAccountUuid      54e8e2c2-...
    newest plan-usage-history org sample  5f6eb959-...
    newest local_*.json on disk           54e8e2c2-... / 5f6eb959-...   2026-09-09T00:49:49

The runner-up pair on disk is 12 days stale (`ba7ccf25-.../2108d9a2-...`, 2026-08-28), so the
disk answer is decisive here rather than a coin toss. When a destination has no records at all,
there is no evidence and the resolver REPORTS that instead of guessing a directory the app will
ignore.

## 2. The directory is the list. There is no index to also update

Measured against the app's `Local Storage/leveldb`, read as raw bytes:

    desktop session ids present in Local Storage : 33 of 184
    working directories present                  :  0 of 59

151 sessions the app lists are absent from Local Storage, and no working directory appears there
at all, so Local Storage is UI state and not the session list. The record files are what the app
enumerates. That is what makes a file-level import sufficient, and it is checked for real rather
than argued: verification step 7 opens the imported project in the desktop app on LT.

## 3. What a record carries

Top-level fields: `cliSessionId` (which is c4x's `session_id`), `sessionId` (`local_<uuid>`, the
desktop namespace, and the filename), `cwd`, `originCwd`, `isArchived`, `title`, `createdAt`,
`lastActivityAt`, `completedTurns`, `model`, `permissionMode`, plus MCP and permission settings.

`cwd` and `originCwd` are the only fields that name the source machine's filesystem, so they are
the only two an import rewrites. Everything else is carried byte for byte.

## 4. The config entry, for completeness

`~/.claude.json` `projects[<cwd>]` holds `hasTrustDialogAccepted`,
`hasClaudeMdExternalIncludesApproved`, `hasClaudeMdExternalIncludesWarningShown`, `allowedTools`,
`enabledMcpjsonServers`, `disabledMcpjsonServers`, `mcpContextUris`. No value inside is a
filesystem path, so only the KEY is rebased. `hasTrustDialogAccepted` is why an imported project
otherwise asks to be trusted again on first open.

## 5. One chat is several sessions, and only the transcripts say which

Everything below was measured on 2026-09-11, on a test laptop with 73 sessions and 16 records and
on the author's machine with 1,110 transcripts and 170 records. The commands are named in
`.md/20260911-chat-grouping-scope.md` and the plan that superseded it.

**The record filename is the chat.** `local_<uuid>.json` is stable across resumes; `cliSessionId`
inside it is only the chat's CURRENT session and is overwritten in place. One record was seen
rewritten across five different session ids while keeping its name. `priorCliSessionIds`, the
field that would list the earlier ones, was present on 1 record in 16 and 1 in 185.

**A resume copies the transcript.** Claude Code starts a new session and writes a new transcript
holding the previous one's records with the SAME message uuids and the new `sessionId`, then
appends. Successive transcripts of one chat shared 808 of 823, 1296 of 1304, 1349 of 1418 and 1680
of 1800 uuids. So the earlier sessions are prefixes of the newest, and the chain is reconstructible
from overlap alone: A is a prefix of B when B is larger and holds at least 90 percent of A's uuids.
That rule reproduced the record's own `priorCliSessionIds` order exactly.

**Nothing else links them.** Transcripts carry no parent-session field: every occurrence of an
earlier session id in a later transcript was content (shell commands, tool output). The
transcript's `bridge-session` record carries a `cse_...` id; the record's `bridgeSessionIds` are
`session_...` ids; the two never match. The app's IndexedDB and LevelDB name record files, not
session ids.

**Forks copy too, and are separate chats.** A fork has its own record with `forkedFromSessionId`
naming the parent RECORD. Overlap with the parent ranged from 0.0 to 1.0 across 23 forks, so
overlap cannot tell a resume from a fork. The record can, for the fork's CURRENT session: a
session with a record is a chat and is never folded. For everything else the copied lines can:
**a resume rewrites every copied line's `sessionId` to its own; a fork copies them verbatim,
the parent's `sessionId` included.** Measured over 17 links on one store, every resume held its
predecessor's records under its own id (100 percent) and every fork held them under the
parent's (0 percent). So a session succeeds another only when it holds the other's records
natively, only when it is the later transcript (first timestamp, then size, then id, so no chain
can loop), only when it holds something the other wrote itself (a parent-chat session shares a
fork's copied history but none of the fork's own work), and not when it reached those records
through a fork (the fork's first transcript still names the parent for them, and the candidate
holds that fork's own records). Rank among what is left: copies before continuations, the highest
overlap, a non-fork record holder, then a record-less session, then a fork's record holder.

**Which session produced a copied row.** The session the line names when the copies agree and
that session's transcript is gone (two forks of a deleted parent still say who wrote the parent's
lines); else the one transcript that holds it natively; else the earliest-starting transcript
that holds it, since a copy can only land in a session that started later, and this is also
right for a fork taken early from a parent that kept growing, where "the smaller transcript"
would hand the parent's rows to the fork. Harvest reads transcripts in first-timestamp order and
refuses to move a row between sessions; `--backfill-chains` repairs stores written before that
rule.

**Where a session lives.** A session can change directory; its transcript does not move, and
the slug directory it sits in is what export, delete, `memory/` and the trust entry are keyed
by. So `sessions.cwd` is the first cwd in the transcript whose slug IS the file's directory (a
fork of a parent that had changed directory begins with the parent's old lines and finds its
home further down), `project_slug` is that directory's name, and `transcript_path` is the
session's own top-level file, never a subagent file under `<session>/` that names it. Measured
on 1,050 top-level transcripts: the rule locates the directory for 1,050, "the last cwd seen"
for 994; it matched the app's own record cwd for 47 of 47 record holders against 43 of 47. The
same pass that writes links repairs these three columns on older rows (59 directories and 73
paths on the author's store).

The store table is `session_links`; its schema comment in `tools/harvest.mjs` carries the same
facts beside the code that uses them.

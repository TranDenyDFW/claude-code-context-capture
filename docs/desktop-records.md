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

**Two roots, both read.** A Microsoft Store install keeps its state under
`%LOCALAPPDATA%\Packages\Claude_<publisher>\LocalCache\Roaming\Claude` and can leave older
records under `%APPDATA%\Claude`; on most machines the two names are one directory. Harvest and
the page both read every distinct root (`claude_appdata_roots()`), so a record is a record
wherever the app left it; imports and purges use the root the app writes to (`claude_appdata()`,
chosen by `config.json`, record count and newest mtime). Measured on the test laptop: 16
records in the container, 1 under `%APPDATA%`.

The store table is `session_links`; its schema comment in `tools/harvest.mjs` carries the same
facts beside the code that uses them.

## 6. One machine, several accounts, and how to make them share

Signing into a second account does not hide anything. It points the app at a different directory,
because `<account uuid>/<org uuid>` is the whole of the separation and the listing is the list.
Measured on the author's machine: four account directories, nine account and organisation pairs,
171 records under one pair and 16 under another, and seven pairs empty.

The CLI side is not separated at all. All 518 transcript directories under `~/.claude/projects`
are named for a working directory and none for an account, `~/.claude.json` holds one
`oauthAccount` and one `userID`, and `CLAUDE.md`, the skills, the hooks and the 31 `memory/`
folders are one set per Windows user. So the conversations already sit in one pile on disk; only
the app's own list is cut into slices. The other per-account stores beside the records are
`local-agent-mode-sessions`, `scratch-workspaces` and `spaces-present`, each keyed the same way.

`c4x/accounts.py` makes every pair on a records root resolve to ONE of them, with a junction on
Windows and a directory symlink elsewhere, so whichever account is signed in reads the same chats,
and puts it back on request.

**What each side shows, as numbers.** `state()` reports `chats_visible` (what All shows: every
record in the directories that hold files) and, per pair, `own`, with `current_chats` for the
signed-in pair (`appstate.desktop_pair`): what Current would show. On an unshared machine that
is the pair's own file count. Under sharing the files sit in one directory and only the newest
backup's manifest says whose each was, so a pair's own are the records the manifest filed under
it that are still there, plus, for the directory the others point at, every record the manifest
never saw (written since sharing began, which `share_current` leaves in place). With links but
no manifest (a junction made by hand) the answer is None, never a guess. The header shows both
numbers on hover of All and Current.
`python -m c4x.accounts [--state | --all | --current | --reconcile | --verify]`, or the Account
switch in the page's header.

**A pair the app creates later is covered when Claude next closes.** A junction covers the pair it
was made for. When an account signs in with an organisation the junctions never named, the app
creates `<account>/<org>` as a real directory beside the shared one, and that account reads its
own list from then on. Measured on the author's machine on 2026-09-15: Account #1 signed in with a
newer organisation, wrote fifteen chats into a directory of its own, and at the next account
switch the app moved those fifteen files into the directory the junctions point at and removed
the directory; Account #2 saw them, Account #1 came back to nothing. c4x had written nothing: it
cannot move a directory the app holds open, and neither `share_all` nor `share_current` runs
while the app is up. So `reconcile()` runs at the one moment the server is alive with the app
closed: the watchdog's stop, sixty seconds after the last Claude process (`reconcile_then_stop`),
and the server's start when the app is not running (`reconcile_at_start`), and on demand through
`POST /api/accounts/reconcile` (the header's **Cover now**, which the server refuses with 409
while Claude is open). Under intent ALL it folds every real pair beside the canonical one into it
(`_fold_pair`, the same move `share_all` makes), after a backup whose manifest files each record
under the pair it came from, and writes the marker back with every link. `state()` lists such
pairs as `uncovered`, with or without records (the app creates the directory before the first
chat), and `verify()` reports them. A pair the app creates while it is open stays private to that
account until it is closed once; the fifteen chats are never lost, only listed under the shared
directory once the app has moved them there.

**A junction's target is a string the kernel resolves physically, and the Store build's process
tree does not see physical paths.** Measured 2026-09-15 on both machines: the app is the Store
(MSIX) build, and for it and every process it spawns (the hook, node, the c4x server the hook
starts) `%APPDATA%\Claude\claude-code-sessions` is virtualised into
`%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code-sessions`: both
spellings open one directory (same inode), neither shows a reparse tag, `realpath` answers the
virtual one, and `GetCurrentPackageFullName` reports no package. `share_all` ran inside that server
and wrote the virtual spelling as every junction's substitute name; the kernel resolved it to the
physical `%APPDATA%` directory, a leftover of an earlier install holding one record here (WMI, which
runs outside the app's tree, counts 1 file there and 224 under LocalCache), and to nothing on the
laptop. `link_target()` read the right string back from every junction while every account but
the shared one listed one chat, or none. The rule, measured with junctions made beside the app's
directories: a junction opened through the virtual spelling is redirected once, and its reparse
target is then opened as written, so a virtual target lands on the physical leftover while a
`Packages` target lands on the shared directory; the same junction opened through its `Packages`
spelling resolves either target to the shared directory, and so does one placed on another drive,
which is why a check outside the virtualised tree proves nothing. So `_make_link` now writes the
spelling the kernel lands
on the target: `_spellings()` re-roots the target under every candidate directory the store knows,
keeps the spellings that open the same directory (`store._identity`), tries the one under
`Packages` first, and `resolves_to()` (an `os.stat` of the link against the target, by inode)
decides which is kept; `_same_path` compares by identity too. A link that resolves elsewhere or
nowhere is an `uncovered` pair with `why` `points elsewhere` or `dangling`: `state()` reads
`mixed`, `verify()` names it, and `reconcile()` re-points it at the shared directory, after copying
what was visible through it into `<backup>/through-link/<account8>/` (and into the shared directory
for a name it does not hold; a colliding name stays in the backup only). A relink that fails puts
the old link back and raises: a pair with no directory is the one outcome worse than a mispointed
one. The CLI is meant to run where the server runs; from a plain terminal the virtual spelling
opens the leftover, which is why `canonical_pair` matches every spelling of a link's target.

**Each chat is tagged with the account it was made under.** A record file carries no account
field, and under sharing every pair lists one directory, so the directory a record sits in says
nothing about who made the chat. Harvest tags each record ONCE, when it first sees it, in
`desktop_records.owner_account`, `owner_org` and `owner_source`, from the first of these that
answers: the ledger (`data/adopted-records.json`: c4x wrote that record into the adopting
account's pair, `ledger`); the record's own directory when no other pair links to it (`dir`);
the account the app is signed in as right now (`signed-in`: `config.json`'s
`lastKnownAccountUuid`, which the app rewrites at a switch; harvest runs at the chat's first
prompt, seconds after the app wrote the record). Rows from before the tag are filled from the
ledger, from the newest sharing backup's manifest (where each record was filed before sharing,
`manifest`) or from an unshared directory, and the rest are stamped `unknown` once. A tag is never
re-decided: `dir` follows the record when the app moves it, `owner_*` does not. The organisation
is a best guess under sharing (every organisation of an account lists the same directory), so
every reader keys on the account. Limits: a chat created and switched away from before its first
prompt is tagged with the next account; an adopted chat is tagged with the adopting account. The
page reads the tag on the All sessions `account` column, in the population list ("Signed-in
account's chats", one entry per account, "No account known"), and in the header's Current hover,
whose number is the live records tagged with the signed-in account (`current_source: tags` on
`/api/accounts`, else `manifest` or `directory` as before).

**Account switches are logged, and the cache cost of one is measured.** Every harvest appends a
row to `account_log` when the signed-in account differs from the last row: `seen_at` is the
harvest, `switched_at` is `config.json`'s mtime, so a switch made while no session ran is still
dated. The prompt-caching docs say caches are isolated between organizations and per workspace
within one, and each account on a machine is its own account and organisation pair, so a chat
continued under another organisation cannot read its cache and writes its whole context again.
The Summary tab names such calls: the first call of a session after a logged switch that read no
cache and wrote at least 0.9 x what the session had resident on its previous call, that previous
call being within its own cache lifetime (one hour when it asked for one, five minutes otherwise),
so an ordinary expiry is not counted, and a compaction (a fraction of the old context) is not
either. Measured on the author's store before this was written: 161 whole-context rewrites, 68M
tokens, every one after a gap longer than five minutes and none around that day's switches; the
five-minute lifetime cost more than any switch had. Nothing on the machine can carry a cache
across organisations; what c4x can do is say what a switch cost.

**Intent is read from the disk when the marker is gone.** The marker lives beside the store, and a
reset of `data/` takes it while the junctions stay. Measured: eight of nine pairs linked, no
marker, and the page said Current while every account read one list. Links are not made by
accident, so with no marker any linked pair means ALL (`intent()` answers `{"mode", "source"}`,
`source` being `marker`, `disk` or `none`, and `/api/accounts` carries it as `intended_source`);
the next reconcile writes the marker back (`by: reconcile`) and takes a manifest if none exists, so
`current_chats` becomes a number again. `canonical_pair` prefers the pair the links already point
at over the fullest one: a new real pair can hold more records than the shared directory on a
quiet day, and folding the shared directory into it would move the one list every account reads.
The backup walks with links pruned (`os.walk`, not `rglob`, which follows a junction and copied
the shared directory once per link, filing every record under the pair walked last), and its
stamp carries microseconds so `share_all` and `reconcile` within one second cannot collide.

**What was measured before it was written**, because the app defends itself against link tricks
and most of those defences would have made this impossible. Its own reader refuses a file that is
a symlink, and refuses one whose link count is above one at 11 of its 17 call sites, which rules
out both symlinks and hard links for the record files. A junction is a reparse point on the
DIRECTORY, so a record reached through one is still a regular file with a link count of one, and
clears both. Node's `lstat` reports the junction itself as a symbolic link and NOT a directory,
while `stat` reports a directory, so the arrangement rests on the app resolving that path rather
than filtering directory entries by type. On the test laptop it resolved it: 17 records listed
under an account that owned one of them, and three records rewritten through the link.

**Three costs, none of them hidden.** One directory holds one `scheduled-tasks.json` and one
`archived-sessions.idx`, so the copies belonging to the pairs that become links are moved into the
backup rather than merged. Either account can rewrite or delete the other's chats. And the app
must be closed while the directories move, then started again.

**It can be undone from the outside.** `claude-code-sessions` is named in the app's own migration
list, and this machine already holds two records roots because that migration has run. A migration
that replaces a junction with a real directory ends the sharing silently, which is why the intended
mode is recorded beside the store and `--verify` compares the two rather than reading the disk
alone.

## 7. Adopting a session the app has no record of

A reinstall (a renamed data directory, a new device identity) keeps every transcript under
`~/.claude/projects` and loses every record, so the app shows the account's cloud list pointing at
a device that no longer exists and nothing local. Measured on the test laptop after one: 121
transcripts, 1 record.

**What was proven before it was written.** One record built from the store for a 37-turn session
and written under the signed-in account's pair while the app was open: after a restart the sidebar
listed it as a local session under its project folder, retitled by the app's own rule, and the
stale cloud entries were sorted into "Other". The fields it carried are the ones every real record
has, measured over 189 records on the author's machine, present on all: `createdAt`, `cwd`,
`isArchived`, `lastActivityAt`, `model`, `originCwd`, `permissionMode`,
`remoteMcpServersConfig`, `sessionId`; plus `cliSessionId`, the link to the transcript.

`c4x/adopt.py` is that write made repeatable, through the Adopt drawer (the button beside the
Account switch) or `POST /api/adopt`. A candidate is a session in the store whose transcript is on disk and not a
subagent's, that has at least one turn, whose entrypoint is `claude-desktop` (or unset), and that
has no record under any records root. CLI and SDK sessions never had a record and are offered only
behind a checkbox. `createdAt` and `lastActivityAt` come from the first and last turn; `model`
from the newest.

**Every record carries a name, and where it comes from was learned the hard way.** The first build
wrote `title` only from the store's `custom` or `ai` kinds, on the theory that the app would name
the rest by its own rule. It does: a record with no title shows as "General coding session", a
string that is in neither the app bundle, its locale files nor the CLI, so it is generated at
render, and on the test laptop that was 64 of 82. The store has a name for every session, in this
order: `custom` (a person typed it; `titleSource` `user`), `ai`, the opening request that the
titles table keeps as `last-prompt` (cut to 200 by harvest, cut again here at 60 on a word
boundary), the first typed prompt in `messages` (`role='user', type='typed'`), and failing all of
those the date. Everything but `custom` is written with `titleSource` `auto`, the app's own value
for a name it made. Records a first build left nameless are named after the fact through the
ledger: `POST /api/adopt/retitle`, or the page's "Name them", touches only c4x's own records and
only the ones with no name.

**A review run folds into the chat it reviewed.** A Stop hook in the test laptop's old install
(`sonnet-review.mjs`) ran `claude -p --model sonnet` after each turn with a fixed prompt whose second
half was the last 250 records of the transcript under review, pasted in as `USER:` / `CLAUDE SAID:`
/ `OUTPUT WAS:` blocks. Each run wrote a one-prompt transcript of its own into the reviewed session's
folder, with the app's entrypoint inherited from its environment, so the store held 58 of them as
sessions, Adopt offered and adopted every one, and the sidebar listed them as chats named "You are
reviewing another Claude instance's work before it...". The prompt carries no session id, and timing
cannot tell them apart: a run that approves starts after the session's last turn, and five long
sessions overlapped in one folder. Quotation can, and harvest does it (`deriveReviews` in
`tools/harvest.mjs`, writing `review_links`; the schema comment there records the measurement).
Only one-shots are tested (one typed prompt, at most three messages); the pool is the sessions of
the run's cwd alive when it started (begun no later, last active within an hour), not one-shots
themselves; up to eight ASCII lines from the end of the prompt (an all-caps label dropped, the last
120 characters kept; a non-ASCII line can never match, since the excerpt crossed a shell pipe and
the store holds U+FFFD where the transcript had anything else) are counted per pool session in one
query. A session whose assistant text and tool results contain none of them is out, whatever its
typed prompts say: a one-shot that repeats a person's prompt word for word (a test harness running
the same command in seven sessions here) matches typed rows alone and is not a review. Among the
rest every quoted line counts, and the unique top with at least two is the reviewed session. A
found link is permanent; a miss is asked again only when a session joins its pool. Measured on the
laptop's store: 54 of 58 tied to 16 chats, the four others quoting lines no session alive in their
folder says; here, 3 sdk-py security reviews of one chat.

**What a session "said" includes its compaction summaries.** One of the four quoted the chat
beside it exactly, and still tied to nothing: the chat had just compacted, so the hook's "last 250
records" were its compaction summary, which the transcript carries as a user-role record nobody
typed (`compact_summary` in the store). The said rule now counts assistant text, tool results and
compaction summaries; a person cannot type a compaction summary, so the guard against a repeated
prompt is unchanged, and that run ties to its chat like any other (55 of 58 on the laptop).

**A review the store cannot place is still a review: an orphan.** The other three quote lines no
session in their folder says: one ran in a skills `references` directory whose folder holds no
session at all, two ran in a fixtures subfolder and quote fixture lines two chats said evenly. A
one-shot whose lines are said (as above) by sessions anywhere in the store, at least two of them,
AND whose reply begins with APPROVED or PROBLEMS, is recorded with `head_id NULL`. The verdict is
what keeps a person's real chat safe: someone who pastes another chat's output into a new session
gets an answer, not a one-word verdict, and the self-test pins that case. The store-wide look runs
only past the verdict test and only once per miss (a miss is asked again when its pool changes),
and the `review_links` table from the build before orphans, whose `head_id` was `NOT NULL`, is
rebuilt on first open with its rows kept and its misses forgotten, so every miss is judged once
under the new rule. An orphan folds into nothing and is listed nowhere: it maps to `None` in the
store's review map, counts toward no chat, is never offered by Adopt, and its record is taken back
with the rest (`reviewed: null` in the report).

**What the fold changes, and what it leaves alone.** The store reads `review_links` beside
`session_links`: a run resolves to the head of the chat it reviewed, so a link naming the run opens
that chat. It is listed nowhere on its own: not in the Sessions list, the pickers, Compare's arms,
or the Summary's session count. The chat carries it as a count, `reviews`, in the work column and
in the picker's label, which is what a search for "review" finds. Its tokens and cost reach the
chat's numbers under "Including Subagents" and never under "Main Thread Only", the user's choice:
it ran beside the chat, outside its context, like a subagent turn; the Cost tab asks for that
scope, so cost always includes it. The chat's Messages and tool calls never show a reviewer's prompt
as something the chat typed. The chat's own page and the drawer beside the Sessions list add a
sixth list, Reviews: when each run started, its verdict (the reply's first word when it is APPROVED
or PROBLEMS), its round among the chat's reviews, the prompt the chat was answering when it started
(the newest typed prompt before the run's first turn, which is where it was dispatched), and its
tokens and cost. Adopt never offers a run, says how many it left out, and takes back the records an
earlier build wrote for runs (`POST /api/adopt/unadopt-reviews`, the drawer's "Remove them from
Claude"): the file is removed and the ledger entry is stamped `removed_at` and kept, so what was
written and taken back stays on record. What the app itself writes on a delete beyond the removal
is not mimicked, because it has not been measured beyond the marker's name (`deleted_<record
uuid>`); that measurement precedes the removal on the laptop.

**A headless child run folds into the chat that spawned it, or under the project above it.** A
chat's shell command ran `claude -p` in a folder (a benchmark harness, a script, a hook), hundreds
of times, and each run left a one-prompt session of its own under its own working directory,
carrying the app's entrypoint it inherited from its environment. Measured on the author's machine,
2026-09-16: 888 such sessions under one project's `tmp` folder (bashrec 513, fidpool 282, fid2 43,
crit-live 37, fidelity 8, fid 5), one typed prompt each, at most 12 messages, and the Adopt page
offered 870 of them as one-chat folders; no shell call anywhere in the store spawned them (the
harness ran outside any captured chat), and the one session in the folder above them has no tool
use. Three children of one chat do exist (`P:\WorkNotes\Claude-Access`, 2026-09-14): each began
2.9 s after a Bash or PowerShell call of the parent whose input carried the child's prompt, and
the call's result came back 0.9 s after the child's last record and quoted its 72 character
reply. The review rule cannot reach any of these (it needs a quoted chat in the same folder), so
harvest derives a second table, `run_links` (`deriveRuns` in `tools/harvest.mjs`; the schema
comment records the numbers). A candidate is a one-shot: one prompt a person typed, at most 16
messages. Hook feedback Claude Code injects as a user message ("Stop hook feedback: ..."), the
note it writes when a request is interrupted, and a block it injects between angle brackets are
not prompts (65 of the harness's runs read as two or three prompts because a Stop hook talked
back; measured store-wide, 455 hook feedbacks, 271 interruption notes and 2,762 injected blocks
sit among the 14,555 typed user messages after a session's first). A
CHILD is a one-shot begun inside the span of another chat's shell call (from the call's `ts` to
its `result_ts`, a column added to `tool_calls` for this, else to the parent's next typed prompt,
else an hour), the parent not a one-shot itself; the tiers of evidence, strongest first, are
`prompt` (the JSON-escaped head of the child's prompt occurs in the call's input), `cwd` (the
input names the child's folder at a path boundary, and that folder is strictly under the
parent's: a child in the parent's own directory is named by every `cd` the parent ever ran,
which on the author's store tied 78 SDK one-shots to whichever command last mentioned the
directory they shared with the chat), `quoted` (the child's folder is the parent's or under it
and a reply line of 40 ASCII characters or more occurs in the parent's tool results inside the
span) and `under` (strict containment and the span alone: the script-file case); two parents at
the same strength name nobody, and a one-shot in the parent's own folder with nothing but a span
is not its child. A BATCH is a one-shot with no such call and at least three other one-shots
sharing its folder's parent or grandparent directory begun within ten minutes (measured on the
author's store: the parent level alone batches 805 of the 808 corpus runs and the grandparent
the 12 nested one level deeper; none of the 137 misses batch at either level; a person's desktop
one-shots reach at most one sibling within an hour); its `project` is the nearest directory at
or above its folder that is the working directory of a session somebody prompted which is not
a run and would not be batched itself (a workflow's SDK agents run in the chat's own directory
and fold under it; a case folder holding two of its own runs, or an empty transcript, is no
project), and NULL when there is none. Tried on a copy of the author's store before it shipped:
1,095 one-shots, 873 batched under `P:\ClaudeExt\ccx-engineering-work`, the three Claude-Access
children tied to their chat, the 14 SDK one-shots of the c4x and claude-appx-restart dev chats
batched under those folders, and only the 15 empty transcripts left alone. A linked run whose transcript grows a second
typed prompt is unlinked on the next pass that touches it: the guard for a person's chat opened
in a subfolder while a parent's command ran. The store reads the table beside `review_links`: a
child resolves to the chat that spawned it and counts toward it under "Including Subagents", a
batch resolves to itself; both are listed nowhere (`hidden_sessions_sql`); the chat's page lists
its children under Runs (how the tie was made, the folder's leaf, the prompt, the cost); the
population list says "(N listed, M runs)" for a project that carries runs; the Summary's "Tool
Bytes by Project" bar folds a run's bytes into its project's and says so on hover; the Adopt
window offers no run, shows a Runs column per folder and a line saying how many are folded
where, and takes back the records an earlier build wrote for runs with the review records.
Derived after every harvest pass for the directories it touched and by `--backfill-runs`
(`--backfill-tool-outcomes` first, which now also fills `result_ts` on rows from before the
column, so every call's span has an end); a fresh install's first harvest derives it as it goes.

**The app, never the CLI; and the launch, two ways.** Measured on the author's machine,
2026-09-16: sixteen processes named `claude.exe`, fourteen the Store app's
(`C:\Program Files\WindowsApps\Claude_<version>_<hash>\app\Claude.exe`) and two Claude Code CLI
sessions (`~/.local/bin/claude.exe`, and the copy the app carries under its LocalCache
`claude-code\<version>\claude.exe`). A restart on the name alone would have ended every terminal
session, and `app_running` refused a switch whenever a terminal was open, so `desktop.is_app_exe`
keeps only the app's own executable (a WindowsApps package directory, or the installer's path)
and reads the path only for the processes named `claude.exe`. The relaunch: on the test laptop,
2026-09-16, app 2.110, the startup sweep took back two stale run records, terminated the app's
twelve processes, ran `explorer.exe shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude` and nothing
came back in 20 s (the same activation from an SSH session returned 1 and started nothing in
135 s), so the watchdog stopped the server with Claude closed and the person asked why nothing
started any more; a scheduled task registered for the interactive logon (`-LogonType Interactive`)
started the app, twelve processes within thirty seconds. `launch_app` now tries the activation,
then the task, and waits for a process of the app's own after each; `restart_app`,
`with_restart` and the sweep all go through it.

**A confirmed restart around a page write.** `with_restart(action, quit_first)` (`c4x/desktop.py`)
runs quit, act, relaunch for sharing and the fold (a directory the app holds cannot be moved)
and act, quit, relaunch for adopt, retitle, unadopt and import (new files are safe with the app
open; the restart only when the write reports `restart_required`). One at a time; the watchdog
treats the window as Claude alive so a long fold cannot stop the server under it; the app not
running means the action runs plainly and the report says so; an app that will not close changes
nothing; an action that fails after the quit brings the app back and re-raises. The six routes
take `restart: true` (`/api/accounts/sharing`, `/api/accounts/reconcile`, `/api/adopt`,
`/api/adopt/retitle`, `/api/adopt/unadopt-reviews`, `/api/project/import` as a form field); the
flagless request is what it was, 409 while the app is open where a directory is about to move.
The page asks first, always (`docs/dashboard.md`, "The header").

**The sweep runs itself when the app starts, and restarts the app.** The user's decision: the
button should not be needed. "The app starts" has one observable in c4x, the SessionStart hook
finding nobody on the port and starting the server, so the server runs the sweep once, right after
it binds (`c4x/api/__main__.py`, `start_review_sweep`, a thread that waits for the server's own
`/__health__` answer; `adopt.sweep_reviews`): `unadopt_reviews`, the ledger's records only, never
one the app wrote; and when that removed anything, `desktop.restart_app`: every `claude.exe`
terminated (killed after 10 s), then the app started again, the Store build through
`explorer.exe shell:AppsFolder\<PackageFamilyName>!<Application Id>` with the id read from the
package's `AppxManifest.xml` (`Claude_pzs8sxrjxfjjc!Claude` on the laptop), the installer's build
by its exe path; kill first because the app keeps a single instance, so a launch while it runs
only fronts the old window. The server itself is `pythonw.exe` and is never touched. Guards: only
while Claude is running (nothing to restart otherwise); never a second restart within ten minutes
(a record that cannot be removed cannot restart the app on a loop); off under `--no-writes`; off
with `install --no-review-sweep` (the receipt's `reviewSweep`, carried into the server's argv as
`--no-review-sweep`, `--review-sweep` undoes it) or `C4X_NO_REVIEW_SWEEP=1`. The report lands in
`data/raw/.review-sweep` and in `dashboard.log`; `GET /api/adopt/sweep` reads it and the drawer
shows it as one line. The drawer keeps its button for a sweep without a restart.

**What the app writes on a delete, measured.** Twenty chats deleted by hand in the app on the
laptop on 2026-09-15: each delete removes the record and writes `deleted_<record uuid>` beside
it, 13 ASCII digits, the delete time in epoch milliseconds. Twenty more markers named records
that never existed locally: the cloud-side ghosts in "Other" that were deleted at the same time
get one too. The four records the sweep had removed got no marker: the app marks its own deletes
only. The transcript under `~/.claude/projects` is left alone, which is why a deleted chat stayed
listed here: the store knew the chat by its transcript and never knew the record's uuid.

**A chat deleted in Claude is hidden everywhere** (the user's choice). Harvest keeps
`desktop_records`: every record it sees, uuid and cliSessionId together, plus the records c4x
wrote, seeded from `data/adopted-records.json` so a record the app deleted before the table
existed still maps (19 of the laptop's 20). A record that is gone with a marker beside it is
stamped `deleted_at` from the marker; one gone without is stamped `gone_at` only (c4x took it
back, a move, a reinstall) and hides nothing; a record that returns has both stamps cleared. The
store hides a deleted chat with every session of its chain, through one subquery the session
frame and the Summary's count share (`hidden_sessions_sql`), so it leaves the Sessions list, the
pickers, the cohorts and the counts; Adopt never offers it again and counts it in the drawer; a
ledger entry whose record the app deleted is no record of c4x's. A raw session id still opens the
chat page. The one limit: a chat whose record the app itself wrote and deleted before harvest
ever saw it cannot be mapped and stays listed (the laptop has one).

**"No folder" is the app's rule, not a defect.** Fifty of the laptop's 82 adopted sessions had a
scratch-workspace working directory (`AppData\Roaming\Claude\scratch-workspaces\<account>\
<org>\scratch-<date>-<hash>`), and the app's own bundle says of such a session that it "shows this
session as 'No folder' and never shows the workspace's location". They are listed, under that
heading, by design.

**"Other" is cloud-side.** The sidebar's Other section holds sessions the account has on the
server: some reachable without any device (they open), the rest pointing at the old device
identity ("Can't reach your computer"), which after adoption are ghosts of chats that now exist
locally under their folders. c4x can see neither kind; archiving the ghosts in the app is the fix.

**Never across pairs.** The record goes into the signed-in account's pair, the one an import writes
to (`appstate.desktop_pair`); a session whose record sits under another account's pair is counted
and left alone. Under sharing All that pair is a junction and the bytes land in its target, which
is the shared list; the report names both directories and says the records stay there if sharing
is turned off. A signed-in pair that sharing does not cover is refused rather than started as a
second list. Every record c4x wrote is named in `data/adopted-records.json`, with the path as the
writer saw it: on a packaged install the server is a descendant of the app, its `%APPDATA%` writes
are redirected into the package's `LocalCache`, and a reader outside that container resolves the
same `<account>/<org>/<name>` under every records root instead.

**Why nothing is preselected.** The app leaves a `deleted_<record uuid>` marker in the pair when a
chat is deleted on purpose and removes the record; the store never learns a record's uuid, so a
deleted chat and a reinstall orphan look the same to the rule above. On the author's machine 923
of 1,027 desktop sessions with a transcript had no record, 915 of them from one month. So the
control groups the candidates by folder, says how many markers the pair holds, and adopts only
what was ticked. Claude may stay open: these are new files, read when it next starts.

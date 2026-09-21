# Changelog

Dates are when the work landed. This project is versioned from 0.1.0, the first release with a test
runner, CI and a linted tree.

## Unreleased

### Added

- **Store maintenance, on the Diagnostics tab: the two one-off passes, each asking first.**
  Record tool outcomes and Fold headless runs, numbered in the order they must run. The fold
  runs its dry run before it asks, so the question quotes the harvester's own numbers, the
  runs it can place nowhere included, and says to record tool outcomes first when shell calls
  still lack a result time (a link made on a loose span stays). Only the fold has a dry run:
  `--backfill-tool-outcomes --dry-run` writes, so the server has no such job. A one-off pass
  on an install with no store is refused rather than left to create an empty one and report
  zeros. Found on the way: the page reloaded only when it watched a job go from running to
  finished, which misses any job that ends before it is seen running (a quick fold, an update
  in another tab), so the reload is now keyed on the server naming a finished job the page had
  not accounted for; and the job lock is now freed on every path by whoever took it (a clock
  that raised before the spawn left it held; freeing "whatever is locked" could free a later
  job's). The two backfills' reports are pinned contracts like `run()`'s
  (`TOOL_OUTCOMES_REPORT_KEYS`, `RUNS_REPORT_KEYS`), and the tool-outcomes pass takes its
  transcripts root as a parameter so the self-test can run it. Tests: `tests/test_harvest.py`
  (97), the harvester self-test (345), vitest `StoreMaintenance.test.tsx` (10),
  `UpdateData.test.tsx` (23), `harvest.test.ts` (20), `App.test.tsx`, `a11y.test.tsx`.
- **Update data, in the header.** The user, on learning the store could only be updated from a
  terminal: "there's no button for this in the app?" One click starts the same incremental
  harvest the hooks run, says what came of it beside the button, and reloads every pane; the
  hover says how fresh the store is. It opens no dialog, because nothing is closed or
  restarted. The server still never harvests on its own (`read_only` stays true and now means
  that). What was measured on the way decided the design: an incremental run is usually under
  a second and once took 53.7 minutes (9,599 transcripts, 12.5 GB), so the POST starts a job
  and the page follows `GET /api/store/harvest`, with a two hour ceiling that only frees the
  lock from a hung node; a harvesting process pointed at a redacted copy once un-redacted it,
  so the child is never told which store is served (no `--db`, `C4X_DB` taken out of its
  environment) and can only open the install's own, and the route refuses a served copy, a
  redacted copy and `--no-writes` with the reason; the harvester exits 0 when a sub-pass
  failed, so its report is read and a failed pass is said as partial; the response cache
  serves entries under five seconds old after the store moved, so it is emptied when a job
  ends; a click that races a prompt hook's harvest is retried once; the watchdog waits for a
  running update. `run()` in `tools/harvest.mjs` had no coverage at all and now takes its
  inputs as parameters, runs in the self-test on scratch inputs, and exports
  `RUN_REPORT_KEYS` and `RUN_REPORT_NESTED`, which the Python parser is held to. An
  adversarial review of the diff, each finding checked by a second reader, then found and
  this fixed: the redacted-copy gate read a memo kept for the life of the process and a
  helper that turns "file is not a database" into "not a copy", so it now reads the mark from
  the file on every call and fails closed; job ids restarted at 1 with every server, so a
  run Restart C4X killed could be reported with a later run's result, and they now name
  the process; one failed status read dropped the run for good (a red note for ever, no
  reload), and the page now keeps asking; a 409 whose job had just ended was shown as a
  failed click; a run the page found under way was not held to its id; `dry_run: "yes"`
  was read as false and would have run the job that writes; the freshness line printed a
  UTC wall time with no zone; and the test tripwire could not fail a test, because a job
  swallows what its runner raises, so it records and fails at teardown. Tests:
  `tests/test_harvest.py` (73), the harvester self-test (341), `tests/test_api.py` (a read
  starts nothing, with the gate open and the job synchronous so it could be seen), vitest
  `UpdateData.test.tsx` (21), `harvest.test.ts` (15), `App.test.tsx` (the page reloads when
  an update ends), `a11y.test.tsx`. No test may run the harvester: its transcripts root
  cannot be redirected.

### Fixed

- **The release's checksum file can be read by the command that checks it.** It was written with a
  CRLF, which `sha256sum -c` reads as part of the filename, so it reported "No such file" about the
  zip sitting beside it. The digest itself was always correct. The release job now writes the
  sidecar with `sha256sum` and verifies it on the runner, so one that cannot be read never reaches
  a release, and the README says how to check a download.

## 0.2.0 - 2026-09-18

Everything since 0.1.0. This section exists because the file sat at one entry for 58 commits, long
enough that it described a build nobody was running: the privacy line it lists under 0.1.0 had been
removed and nothing here said so.

### Added

- **A download that is one folder and needs nothing installed.** The user's words: "Make it purely
  an executable - if someone wants to use CLI options, they can add options after calling the
  executable." `tools/build_exe.py --bundle` assembles `dist/bundle/c4x/`: the exe, the Node it
  runs (pinned, fetched from nodejs.org, checked against the digest published beside it), and the
  tools and hooks Claude and the server shell out to. The folder holds `tools/harvest.mjs`, which
  is already the marker that makes a directory an install, so it is one wherever it is unzipped.
  - `c4x.exe` with no arguments installs itself into Claude, starts the dashboard and opens it.
    `c4x.exe install|status|uninstall|reset|harvest` run the node tools that already do those
    jobs, with every flag after the verb passed through, so the program and a checkout cannot
    drift into two behaviours. `c4x.exe serve`, and any bare flag list, is the server it has always
    been, which is what the hook launches. `c4x/verbs.py` decides that from argv alone, before
    uvicorn, dash or the store are imported.
  - The hooks the installer writes now name the Node beside them when the install carries one
    (`paths.node_exe`, `install.mjs runtimeFor`), and keep the bare `node` a checkout has always
    used. `install --launcher <exe>` records the program that installed itself, which skips a
    Python probe that would find nothing on that machine.
  - Three defects the first real assembly found, none of which a unit test could: the assembler
    reused a stale exe from `dist/`, so the folder shipped a program built before the verbs
    existed, and a check passed because the old exe answered it by accident; the smoke pointed the
    store variable at a path that does not exist inside the folder; and it passed a relative store
    path to a child running in another directory. The assembler now rebuilds when the exe is older
    than its source, and the smoke asks `--version` first, precisely so a stale build cannot pass.
  - CI assembles the folder and runs it with a PATH holding Windows and nothing else, so anything
    it forgot to carry is absent rather than supplied by the runner. Deleting `node/` turns that
    red. On a tag the folder is zipped onto the release with its sha256.
  Tests: `tests/test_verbs.py` (19, every verb shape including the double-clicked first run),
  `tests/test_paths.py` (the Node lookup both ways), `install.mjs --self-test` 103,
  `dashboard.mjs --self-test` 46, `build_exe.py --self-test` 44.

- **Every control that closes or restarts Claude, or C4X, asks first, then does it.** The user's
  rule: "prompt the user to continue; for example, if changing to all from current, it requires
  a reboot, prompt the user when they click all and only continue if they confirm." All,
  Current, Cover now, Adopt, Name them, Remove them, Import, Stop C4X and Restart C4X open a
  window (`Confirm.tsx`, on the `Dialog` chrome) naming exactly what will happen; Cancel does
  nothing; Continue sends `restart: true` and the server quits the desktop app, acts and starts
  it again (`desktop.with_restart`: quit, act, relaunch for the directory moves; act, quit,
  relaunch for the writes the app may stay open for), one at a time, the watchdog told to wait,
  and the outcome stays on screen. The page no longer refuses while Claude is open or tells the
  person to quit it. Two things measured on the way, both fixed here: `claude.exe` is also the
  CLI (16 processes on the author's machine, 2 of them terminal sessions; a restart on the name
  alone would have ended them, and `app_running` refused a switch because a terminal was open),
  so `desktop.is_app_exe` tells the app by its executable's path; and on the test laptop the
  startup sweep killed Claude and could not start it again (`explorer.exe shell:AppsFolder`
  brought nothing back from the hook-started server, nor from an SSH session), which the person
  saw as "it doesn't start up automatically anymore", so `launch_app` falls back to a transient
  scheduled task in the interactive logon session, which brought the app back in thirty
  seconds. `projects.import_` reports `restart_required`. Tests: `tests/test_desktop.py` (the
  identity table, the CLI never the app, the path read only for `claude.exe`),
  `tests/test_desktop_restart.py` (18: the launch two ways, the task command, quit-act-relaunch
  and act-quit-relaunch in order, not running, off Windows, a failed relaunch said not claimed,
  an app that will not close changes nothing, a failing action relaunches then raises, one at a
  time, the watchdog window), `tests/test_adopt.py` (the flag reaches `with_restart`; a busy
  restart is 409), `tests/test_accounts.py` (a terminal's claude is not the app); vitest
  `Confirm.test.tsx`, `restart.test.ts`, and the four controls' suites.
- **A headless child run folds into the chat that spawned it, or under the project above it.**
  The Adopt window offered 870 one-chat folders under one project's `tmp` (bashrec, fidpool,
  crit-live and the rest): a harness's `claude -p` one-shots, one typed prompt each, at most 12
  messages, carrying the app's entrypoint from their environment, and no shell call in any chat
  spawned them. The user's words: "lots of them look awfully like subagents ... They should still
  be associated with their parent project where applicable." Harvest now derives `run_links`
  beside `review_links` (`deriveRuns`, `--backfill-runs`): a one-shot (one prompt a person
  typed; hook feedback, interruption notes and injected blocks are not prompts) begun inside the
  span of another chat's shell call is that chat's child (the call's input carries its prompt or
  names a folder strictly under the chat's at a path boundary, the call's result quotes its reply,
  or its folder sits under the chat's), and a one-shot with no such call and three or more sibling
  one-shots within ten minutes is a batch folded under the nearest folder at or above it that
  holds a real chat (somebody prompted it, it is not a run, it would not be batched). Tried on a
  copy of the author's store before it shipped, which is where the folder tier lost "the
  parent's own directory" (78 SDK one-shots had tied to whichever `cd` last named the directory
  they shared with the chat) and the walk learned that a case folder holding two of its own runs,
  or an empty transcript, is no project: 873 of the harness's runs under
  `P:\ClaudeExt\ccx-engineering-work`, the three real children tied to their chat, 14 SDK one-shots
  under the two dev projects, the 15 empty transcripts left alone. `tool_calls` gained `result_ts`
  (the span's end; `--backfill-tool-outcomes` fills it on old rows), and the review rule's
  one-shot query took a `max` so both rules share it. The store folds a child into its chat (head, members under the
  subagent scope, the work column's `runs`, a Runs section on the chat page with how the tie was
  made) and hides every run from the lists; the population list says "(N listed, M runs)"; the
  Summary's project bars fold a run's bytes into its project (and the query groups by position:
  `GROUP BY project` had bound to `run_links.project`, which merged every project's own bytes
  into one bar, caught by the new test); Adopt never offers a run, shows a Runs column per folder
  and a line saying how many are folded where, and takes their records back with the reviews.
  `c4x/runs.py` reads the map. Tests: harvest's self-test (21 new checks; three mutations red:
  no span end, loose containment, the folder tier without strict containment), `tests/test_store_runs.py` (8),
  `tests/test_adopt_runs.py` (4), the chat page and Adopt window suites. Existing stores get the tables on first open and the fold through
  `node tools/harvest.mjs --backfill-tool-outcomes` then `--backfill-runs`, run by hand.
- **The Adopt window: a table over the page, with a search box.** The Adopt button opened a
  drawer down the right edge, a stacked list of folders; the user's words: "looks so stuffy on
  the side like that". It now opens a window over the page the way the Project button does (the
  page dimmed behind it, Escape or the backdrop to close, focus into the search box and back to
  the button), and the folders are a table: Adopt, Folder (a one-chat folder carries its chat's
  title), Chats, Newest, Path, and Runs when the server sends it. The search box narrows the
  table as you type by folder name, path and chat title, every word in any order (the palette's
  rule, `matches()`), says "N of M folders match", and opens a folder whose chat title matched so
  the row shows why it is there; Escape clears the search before it closes the window; Select
  all takes the folders shown. The chrome is one component, `frontend/src/components/Dialog.tsx`,
  lifted from the project dialog: Escape scoped by DOM containment (a window opened from inside
  another one closes alone), Tab kept inside the panel, the backdrop click ignored while a write
  is under way. Every handler, API call and sentence of the drawer is kept. Tests:
  `Dialog.test.tsx` (4), `AdoptSessions.test.tsx` (17, four new), the a11y gate over the opened
  window.
- **The server's children open no console window.** Once the hook started the server as
  `pythonw.exe`, every console program it ran got a console of its own from Windows, a box that
  flashed on every page load: `tasklist` behind `/api/adopt` and `/api/accounts`, `node` behind
  the mirror routes. Every spawn in the package now goes through `c4x/proc.py`, which adds
  `CREATE_NO_WINDOW` on Windows and reaches `subprocess.run` at call time so the delete guard still
  sees it; `accounts.app_running()` reads the process table through psutil instead of spawning
  anything. A source sweep in `tests/test_proc.py` refuses a new direct spawn, and is fed a
  known-bad module so it cannot pass vacuously.
- **The Adopt control is a drawer, and the project modal covers the page.** The header keeps one
  Adopt button beside the Account switch; everything else opens in a non-modal drawer at the side
  of the page (Escape, a close button, focus in on open and back on the button on close). It is
  rendered through a portal at the end of the document, because the header's backdrop filter makes
  it the containing block for a fixed descendant: measured in the live page, a fixed element inside
  the header was clipped to the header's box, and the project mover's `inset-0` backdrop covered
  only the header strip while its dialog declared `aria-modal`. Both now cover what they claim.
- **A review run folds into the chat it reviewed.** A Stop hook's `claude -p` left 58 one-prompt
  sessions on the test laptop, each quoting the last 250 records of the transcript it reviewed,
  and the store held them as chats: adopted into the app, listed in the pickers, their tokens
  their own. Harvest now derives `review_links` (a one-shot whose prompt's long ASCII lines occur
  in one session's assistant text and tool results, same folder, alive when the run started, at
  least two lines, unique; a one-shot that merely repeats a person's prompt matches typed rows
  alone and is not a review) after every pass and by `--backfill-reviews`: 54 of 58 there, to 16
  chats; 3 sdk-py security reviews of one chat here. The store folds a run into that chat beside
  the chain map: out of the Sessions list, the pickers and the Summary's session count; counted
  on the chat (`reviews`, in the work column and the picker's label); into the chat's numbers and
  measured cost under "Including Subagents" only, which the Cost tab always uses; never into its
  Messages. The chat's page and the drawer list each review with its verdict, its round, the
  prompt the chat was answering when it started, and its cost. Adopt never offers a run and takes
  back the records an earlier build wrote for them (`POST /api/adopt/unadopt-reviews`); the
  "Reviewer - <chat>" naming of the previous entry is withdrawn with it.
- **The sweep runs itself when the app starts, restarts Claude, and reaches the runs the store
  cannot place.** Four reviewer chats stayed in the laptop's sidebar after "Remove them": the runs
  `review_links` could not tie. One quoted the chat beside it exactly, from its compaction summary,
  a user-role record the model wrote; the said rule now counts compaction summaries beside
  assistant text and tool results (a person cannot type one, so the repeated-prompt guard holds).
  The other three quote lines no session in their folder says; a one-shot whose lines are said
  somewhere in the store, at least two, and whose reply is a bare verdict, is now recorded as a
  review with no chat (`head_id NULL`; the table is rebuilt from the `NOT NULL` shape with its rows
  kept and its misses forgotten, so each is judged once under the new rule): folded into nothing,
  listed nowhere, never offered, its record taken back with the rest. Then the server runs the
  sweep once it is up (`adopt.sweep_reviews`, a thread waiting for its own health answer): the
  ledger's records for runs are removed and, when any was, the desktop app is restarted
  (`c4x/desktop.py`: every `claude.exe` terminated, the Store build relaunched through
  `explorer.exe shell:AppsFolder\<family>!<id>` with the id read from the package manifest, the
  installer's build by its exe). Only while Claude runs, never twice within ten minutes, off under
  `--no-writes`, `install --no-review-sweep` (`--review-sweep` undoes it; the receipt's
  `reviewSweep` reaches the server's argv) or `C4X_NO_REVIEW_SWEEP=1`. The report lands in
  `data/raw/.review-sweep`; `GET /api/adopt/sweep` reads it and the drawer shows it in one line.
- **Stop C4X and Restart C4X, in the header.** `POST /api/server/stop` stops the server (the page
  asks first and says the next Claude session starts one again); `POST /api/server/restart` starts
  a replacement with the same flags (`c4x/server.py restart_server`, through the package's one
  detached spawn `proc.detach`, spared from the shutdown's kill of its children) and exits, and
  the replacement waits for its predecessor's pid (`--after`) before binding. The page waits for a
  DIFFERENT process to answer `/api/health`, which now carries `pid`, then refetches everything.
  Both routes are JSON-bodied, so a foreign page's request meets the preflight the middleware
  refuses. `tokenFrom` in `tools/dashboard.mjs` takes the newest token in the log, since a restart
  appends its replacement's.
- **A chat deleted in the desktop app leaves every list.** Measured on the laptop: a delete
  removes the record and writes `deleted_<record uuid>` beside it (13 digits, epoch ms), leaves
  the transcript alone, and marks nothing c4x removed. Harvest keeps `desktop_records` (every
  record it sees, uuid and cliSessionId, seeded from the ledger for the records c4x wrote) and
  stamps a gone record `deleted_at` from the marker or `gone_at` without one. The store hides a
  deleted chat with its whole chain through `hidden_sessions_sql`, shared by the session frame
  and the Summary's count; Adopt never offers it again (`deleted_in_app` in the drawer) and a
  ledger entry the app deleted is no record of c4x's. A raw session id still opens the chat.
- **The header says less and shows it on hover.** The Population list names a project by its
  folder alone (more of the path only when two folders read the same; a folder-less chat by its
  name; no "Project:" prefix) and every option carries `path` on `/api/cohorts`; the control is a
  select-only combobox drawn by the page (`Dropdown.tsx`), because a native select's open list
  cannot show a hover. The Account switch says on hover how many chats All shows and how many
  the signed-in account would see under Current (`own` per pair and `current_chats` on
  `/api/accounts`, derived from the newest sharing backup manifest); the label's hover carries the
  restart note and turns amber after a switch; the row's sentence is gone; the numbers refresh on
  focus and after any change. Stop C4X sits behind a divider, Adopted Chats (renamed) after
  Restart C4X behind another.
- **Each chat is tagged with the account it was made under; Current asks first; account switches
  are logged and their cache cost measured.** Under sharing nothing said which account made a
  chat, and the header's Current number came from the sharing backup, which only knows where a
  record sat before sharing began (it read 0 for the signed-in account). Harvest now tags each
  record once, at first sight (`desktop_records.owner_account`, `owner_org`, `owner_source`: the
  ledger, an unshared directory, the account signed in now, the backup's manifest for older rows,
  else unknown), and appends `account_log` rows dated by the app's own `config.json`. The frame,
  the population list ("Signed-in account's chats", one entry per account, "No account known"),
  All sessions (an `account` column) and the header's Current hover read the tag; an import keeps
  the local tag. Current opens a confirmation before un-sharing. The Summary tab names the calls
  that rewrote their whole context right after a switch (no cache read, 0.9 x the previous call's
  resident context, within that call's own cache lifetime, so an expiry or a compaction is not
  counted): caches are isolated between organisations, so finish a chat under the account it
  started with. `docs/desktop-records.md` section 6 carries the rules and the measurements.
- **One number for the signed-in account, projects A to Z, the Summary chart named the way the
  list is.** The header's Current hover said 175 while the population list said 107 for the same
  words: the header counted the app's records, the list the chats this page lists (the rest are
  chats whose transcripts the store never held). Both now read the page's count from one
  function (`store.listed_by_account`), and the hover says "107 listed here; 175 in the app";
  All says the same both ways. The population list keeps the forty projects with the most work
  and shows them A to Z by the name on the screen. "Tool Bytes by Project" labels its bars with
  the list's names (`store.project_labels`, lifted out of the list) instead of the shortened
  path, the full path staying on hover.
- **The README says what c4x does for a person.** Use another account when one runs out and
  keep every chat; get back the chats the app lost; move a project to another machine; search
  every chat; a warning before a compaction. Then install, the account switch in plain words
  (with a header screenshot from the test laptop), Adopt, the two project commands (the export
  from one machine imported on another was tested end to end before this was written), what it
  keeps, and links. No token statistics: those live on the dashboard and in `docs/`. Every
  paragraph of detail the README used to carry moved verbatim to `docs/dashboard.md` first.
- **Junctions are made with the spelling the kernel resolves to the shared directory, and
  re-pointed when they land elsewhere.** Measured on both machines after the reconcile shipped:
  the Claude app is the Store (MSIX) build, and for it and every process it spawns, the c4x
  server included, `%APPDATA%\Claude` is virtualised into the package's LocalCache; both
  spellings open one directory from inside and nothing in the process can tell. `share_all` had
  written the virtual spelling as every junction's substitute name, the kernel resolved it
  physically, and every junction on the author's machine landed in a leftover directory holding
  one record (WMI: 1 file under `%APPDATA%`, 224 under LocalCache) while on the laptop it landed on
  nothing: every account but the shared one listed one chat, or none, with `link_target` reading
  the right string back from each. `_make_link` now tries each spelling `_spellings` offers (the
  target re-rooted under every candidate the store knows, identity-filtered, `Packages` first) and
  keeps the first that `resolves_to` the target by inode; `_same_path` compares by identity;
  `_remove_link` uses `os.unlink` and raises instead of discarding `rmdir`'s exit code (a silent
  failure was the path by which `share_current` would have moved records through the junction).
  `uncovered_pairs` carries `why` (`not linked`, `points elsewhere`, `dangling`), `state()` reads
  `mixed` and counts only links that resolve, `verify()` names each such pair once by identity,
  and `reconcile()` re-points them (`relinked` in the report and the watchdog's log line), copying
  what was visible through the link into `<backup>/through-link/` and into the shared directory
  for names it lacks; a relink that fails puts the old link back. The header hover names each
  uncovered pair with its why.
- **Sharing keeps itself whole across sign-ins.** Measured on the author's machine: eight of nine
  account pairs were junctions to one directory from an earlier install, with no marker and no
  backup beside the store; Account #1 signed in with a newer organisation, the app gave it a real
  directory outside the sharing and fifteen chats of its own, and at the next account switch the
  app folded that directory into the shared one, so Account #1 came back to nothing while
  Account #2 listed everything. `accounts.reconcile()` folds every pair the app created since
  sharing into the shared directory (the same move `share_all` makes, after a backup whose
  manifest files each record under its pair) and writes the marker back with every link; the
  watchdog's stop runs it first (`reconcile_then_stop`, the one moment the server is alive with
  the app closed), the server's start runs it when the app is not open (`reconcile_at_start`),
  and `POST /api/accounts/reconcile` runs it on demand (409 with the pending pairs while Claude
  is open). Intent is read from the links on disk when the marker is gone (`intent()`,
  `intended_source` on `/api/accounts`), `canonical_pair` keeps the pair the links point at,
  the backup walks with junctions pruned and stamps microseconds, `state()` lists `uncovered`
  pairs and `verify()` reports them, and `python -m c4x.accounts --reconcile [--dry-run]` does
  it from a terminal. The header, under All, names such pairs ("N account pair(s) not yet
  covered; covered when Claude next closes", paths on hover) with a **Cover now** button that is
  disabled while Claude runs.
- **The header, second pass.** A folder holding one chat reads as that chat's title (the frame's,
  the same name the Sessions list shows) and a folder holding several by its name, the folder
  appended only where two rows would read the same; the "Population" word is gone (the label
  stays for screen readers); the "Account" word is gone and the All / Current hovers carry, one
  line each, what the side shows, "Quit Claude before switching" and the restart note, the switch
  turning amber after a change; the Adopt button reads "Adopt (N)" with its sentence on hover. A
  chat the desktop app holds a live record for is listed whatever its size: the laptop's T02 (one
  transcript row), T09 and T14-A sat in the sidebar and nowhere on the page, since the five-row
  floor took them for harness one-shots; the floor now exempts any chat with a live record in
  `desktop_records` (`live_records_sql`), in the SQL half and the pandas half alike.
- **Every adopted record carries a name, and the ones a first build left nameless can be named.**
  The desktop app shows a record with no `title` as "General coding session", every one of them,
  and the first build wrote a title only from the store's `custom` or `ai` kinds: 64 of 82 on the
  test laptop. The store has a name for every session (a person's, a model's, the opening request
  the titles table keeps as `last-prompt`, the first typed prompt in `messages`, or the date), so
  `title_for` always answers, prompt-derived names cut at 60 on a word boundary, and
  `POST /api/adopt/retitle` (the page's "Name them") names c4x's own nameless records through the
  ledger, resolving a path written from inside a packaged app's redirected `%APPDATA%` under every
  records root. "No folder" for scratch-workspace sessions is the app's own rule and is documented
  as such.
- **The hook's server has no console window.** The first build spawned `py -3` with the hide flag
  on py.exe; the launcher then started python.exe from a parent with no console and Windows gave
  it one. The probe now answers with `sys.executable`, that path is what is spawned, as
  `pythonw.exe` when it sits beside `python.exe` on Windows, and a receipt or cache carrying the
  old shape is re-resolved once.
- **The executable is `c4x.exe`, with a version resource and the app's icon.** `dist/c4x/c4x.exe`,
  "c4x dashboard (Claude Code context capture)" in its file properties, the version folded from
  `git describe`; the icon is read out of the Claude desktop app installed on the building machine
  (`--icon FILE.exe,0`, the Store build first, then the non-Store one, then `C4X_ICON`) and is
  never committed, so a release build carries PyInstaller's own icon. `--check-icon` proves the
  built exe's icon is the app's, pixel for pixel.
- **The dashboard starts with Claude and stops when Claude is gone.** Capture was automatic and the
  page was not: nothing started `python -m c4x.api`, and nothing ever stopped it. The SessionStart
  hook now asks the port who holds it (a bounded probe against `/__health__`, comparing the store
  it names with this install's, resolved and case-folded) and, when nobody does, spawns
  `tools/dashboard.mjs` detached to start the server, off the hook's ten-second clock: one python
  import of the dashboard's modules costs 3.2 to 3.6 s warm on the author's machine, so the
  interpreter is resolved once at install (or once a week) and never inline. The server carries a
  watchdog (`--watchdog`) that stops it through the hardened shutdown once no process named
  `claude` (or node running the npm package) has been seen for 60 s, and it refuses to bind on a
  port another c4x dashboard already answers, which Windows would otherwise allow. `install status`
  gained a `dashboard` line with the stop command, `install uninstall` stops a server the hook
  started, and `install --no-dashboard` records the opt-out in the receipt, where a re-run keeps it.
- **Adopt: a record for every chat the desktop app has no record of, per folder.** A reinstall
  keeps every transcript and loses every `local_<uuid>.json`, so the sidebar shows a cloud list
  pointing at a device that no longer exists. One record built from the store, the nine fields
  every real record carries plus the transcript's id, written under the signed-in account's pair,
  was listed by the app after a restart; `c4x/adopt.py` is that write made repeatable and chosen.
  Chosen, because the rule cannot tell a reinstall orphan from a chat deleted in the app (923 of
  1,027 desktop sessions with a transcript had no record on the author's machine, 915 from one
  month), so the control groups by folder, preselects nothing and says how many `deleted_<uuid>`
  markers the pair holds. A title is written only from the `custom` or `ai` kind, never the raw
  last prompt; the account switch is untouched, and under sharing All the bytes land through the
  junction in the shared directory, which the report says.
- **An executable, for a machine with node and no Python.** `tools/build_exe.py` freezes
  `python -m c4x.api` with PyInstaller into `dist/c4x-api/` and proves the build by running it: the
  shell from the bundle, the tab list (which imports `app.py`, so dash is in) and a rendered pane
  (plotly). `c4x/paths.py` separates the bundle (the packed page and `prices.json`) from the
  install (the store, the node tools, `tmp/`), and a frozen exe with no checkout above it exits 2
  with the reason rather than serving a page whose every tab is a 500. The `build-exe` workflow
  builds and smoke-tests on Windows and attaches the zip to every release.

- **An Account switch: show every account's chats, or only the signed-in one's.** Signing into a
  second account on the same machine hides nothing, it points the desktop app at a different
  directory: `<account uuid>/<org uuid>` is the whole of the separation and the listing is the
  list. Four account directories and nine pairs on the author's machine, 171 records under one and
  16 under another. The CLI side is not separated at all, so the conversations already sit in one
  pile: 518 transcript directories, none named for an account, one `oauthAccount`, one set of
  skills, hooks and memory. `c4x/accounts.py` makes every pair on a records root resolve to one of
  them, with a junction on Windows and a directory symlink elsewhere, backs up both trees first, sets aside rather than merges the files
  that are per pair, records what was asked for beside the store, and puts it all back on request.
  The app's own reader refuses a symlinked file and refuses a link count above one at 11 of its 17
  call sites, so neither symlinks nor hard links would have worked; a junction leaves the records
  as regular files with a link count of one, and the test laptop then listed 17 chats under an
  account that owned one of them. Two things are not hidden: either account can rewrite or delete
  the other's chats, and an app update's migration can quietly turn a link back into a directory,
  which is what `--verify` and the page's warning exist to catch.
- **An export says how many CHATS it carries, and an import proves each one folded here.** The
  manifest gained `chats` and `chains`, which name the sessions that have to fold into one head on
  the far side, and `import` checks the store against them and reports any chat whose sessions
  landed in more than one chat here. Nothing could see this before: `verify_mirror` is a
  filesystem verdict that reads no store row, a replaced link reports zero rows inserted, and a
  destination whose build has no `session_links` table is skipped with a note, so a chat that
  arrived as two rows looked exactly like one that arrived as one. An export also names any chat
  whose desktop record is not on this machine under a session it carries, which is what decides
  whether the destination app lists the chat at all.
- **One row per chat, the way the desktop app lists them.** The app resumes a chat by starting a
  NEW CLI session whose transcript is a copy of the old one plus what follows, so a chat resumed
  four times was five rows here under five names. Harvest now derives the chain from transcript
  uuid overlap into `session_links` (per directory, whenever a new transcript appears, and for
  existing stores via `node tools/harvest.mjs --backfill-chains`), and the Sessions list, the
  picker, cohorts, Compare and every scoped tab fold a chat's older sessions into its newest one:
  named by that session's title, with turns, peak and compactions counted over the whole chain,
  and a `cli sessions` column saying how many it spans. Selecting a superseded id, from a URL, a
  table row or Compare's arm B, resolves to the chat server-side; the API says so in an
  `x-c4x-session-requested` header rather than in the cached payload. A session with a desktop
  record of its own is never folded: a fork copies history too, and the app shows it separately.
  For every other session the copied lines decide: a resume rewrites them to its own id, a fork
  copies them verbatim, so a session succeeds another only when it holds the other's records
  under its own id, is the later transcript, holds something the other wrote itself, and did not
  reach those records through a fork. Measured over 17 links on the author's store, every resume
  was native 100 percent and every fork 0 percent. Before that rule a parent chat's history
  folded into its fork whenever the parent's own successor was a post-compaction resume: one
  fork showed 8,076 turns of which 7,598 were the parent's.
- **Copied rows now belong to the session that produced them.** `turns`, `messages`,
  `compactions` and `tool_calls` were written with `INSERT OR REPLACE` on their uuid, so a copied
  row went to whichever transcript was harvested last, in directory order: one session that
  produced 388 turns was left holding 3. Ingest now reads transcripts in first-timestamp order
  and refuses to move a row to another session; `--backfill-chains` returns the rows written
  under the old rule to their producer. The ordinary harvest does the same for any directory that
  gains a transcript, so by the time the backfill ran on a copy of the author's store the
  directories worked in that day were already right and it reported 16,179 turns, 14,268 messages,
  21 compactions and 7,580 tool calls still to move, most of them under one fork.
  A producer whose transcript is gone still owns its rows: two forks of a deleted parent name it
  on every copied line, and the first rule moved all of the parent's rows into whichever fork
  sorted first. `attachments` and `record_types` are per-session counters and still count a copy
  twice; that is stated here rather than fixed.
- **A session row now says where its transcript actually is.** `sessions.cwd` was the last cwd a
  transcript named, and a session that changed directory was filed under a subdirectory its file
  is not in: export walked the wrong slug directory and carried no transcript, delete left the
  file and excluded the wrong directory, and a resumed chat could span two "projects" and refuse
  to delete. It is now the first cwd whose slug is the file's directory (1,050 of 1,050 top-level
  transcripts on the author's store, against 994 for the old rule; it matched the app's own
  record for 47 of 47 record holders against 43). `transcript_path` is the session's own
  top-level file, where 69 rows pointed at a subagent file that directory order had reached
  first, and `project_slug` is the project directory, not `subagents`. The chains pass and
  `--backfill-chains` repair older rows and report `rows_repaired`.
- **An HTTP API and a React frontend.** `python -m c4x.api` serves `/api/tab/{id}` and a built
  bundle from `frontend/dist`, which is tracked on purpose so the documented install pulls nothing
  from npm. A suite check now compares the bundle's commit time against its source, because the
  tests run against `src` and the users get `dist`.
- **A response cache** (`c4x/api/cache.py`) keyed on the store's file state, with a five second
  upper bound on staleness. Both clauses are load-bearing and the docstring says which does the
  work.
- **`tools/contract_audit.py`**, a payload-shaped gate meant to outlive Dash, and a shared
  `RENDER_FAILED` marker so a crashed tab is visible to the CLI's exit code and to every audit
  rather than only to a reader.
- **Capture liveness in `status`**: hook events and their recency, status-line samples against
  those events, and when the SessionStart self-heal last rewrote your settings. `C4X_NO_SELF_HEAL=1`
  turns that rewrite off.
- **`install --evict-missing`**, which removes c4x wiring pointing at a root that no longer exists.
  Moving a checkout used to strand hooks that `status` never named.
- **Project import and export**, with the manifest verified before a row is written.
- **Export and import now move the CONVERSATIONS, not only the rows**, and the import lands where
  you choose. `c4x/appstate.py` carries the four things Claude Code holds outside the store: the
  transcripts and their per-session directories, project memory, `~/.claude/tasks/<session>`, the
  `~/.claude.json` entry (which is `hasTrustDialogAccepted`), and the desktop app's own record.
  Before this, an import touched exactly one file, `context.db`, so the project appeared in c4x and
  was invisible in the desktop app.
  - `import --into "<working directory>"`, and the page offers the same field with the directory
    name it produces shown beside it. Everything is rebuilt from that path on the CURRENT user's
    machine: the slug directory, the config key, the desktop record, and the `cwd` in the rows.
    Nothing absolute from the exporting machine is used as a destination.
  - **The desktop record is filed under THIS machine's account and organisation.** Those two
    directory levels are the machine's, not the project's, so copying the source path would put
    the record where the destination app never looks. Measured, with the resolution rule and the
    evidence for it, in `.md/20260909-desktop-record-addressing.md`.
  - **Source always wins**, and every file is re-read and re-hashed after writing. A replacement
    that SHRINKS a file is named, because a compacted transcript is newer and shorter.
  - `verify-mirror <export> [--into ...]` answers "does this machine hold what the export
    carries?" standalone, exits non-zero on a difference, and runs automatically at the end of
    every import. Files the export does not carry are reported and never deleted: a slug directory
    is shared by every session with the same working directory.
- **`--dry-run` on import**, which names every destination and writes nothing.
- **Pre-compaction transcript snapshots are documented**, including where they live, the size cap,
  `C4X_SNAPSHOT=0`, and that `--purge` deletes them.
- **What a tool call turned out to be**, on `tool_calls` as `outcome` and `denial_kind`, from
  the `toolDenialKind` field Claude Code writes at the top level of the record. Both nullable
  TEXT through the existing migration path, so an older store gains them the next time harvest
  runs. `is_error` stays: it is the raw transcript fact, and it simply stops being read by
  anything that reports a number. `tools/outcomes.mjs` holds the one definition the writer and
  the readers share; `c4x/store.py` mirrors it in SQL.
- **A Refusals table on the Cost tab, and `waste.mjs --refusals`**, carrying Claude Code's own
  vocabulary unchanged. A table rather than a note, because a note cannot be sorted, filtered
  or exported, and this is the one place the values are quoted verbatim rather than summarised.
- **`harvest --backfill-tool-outcomes`**, which classifies every existing row from the
  transcripts still on disk. UPDATE only, idempotent, and guarded on a high-water rowid rather
  than a row count, because three writers share this store.

### Changed

- **Markdown is rendered where the text is markdown, it can be saved as a file, and the messages a
  compaction dropped are a table with a search box.** The user's words: "FOR ALL MARKDOWN TEXTS IN
  THE ENTIRE PROJECT, DISPLAY AS MARKDOWN AND ADD A MARKDOWN/TEXT EXPORT" and "FOR THE 'BEFORE
  COMPACTION' DATA - SPLIT THE TEXT AND DATE INTO DIFFERENT COLUMNS AND ADD A SEARCH BAR TO THAT
  DATA".
  - `react-markdown` and `remark-gfm` are bundled (the user's choice, made with the bundle cost in
    front of them). No `rehype-raw`, so no HTML string is ever built: a `<script>` in a transcript
    is text, a link opens only for http, https and mailto and never with this page behind it, and
    an image is not fetched, since an `img src` in a transcript tells a third party that somebody
    opened that record.
  - NOTHING IS DROPPED, AND THAT IS PINNED RATHER THAN TRUSTED. The plan for this change said
    react-markdown deletes raw HTML and that a remark plugin was needed to put it back. Measured
    against 10.1.0, it does not: `<uuid>`, `<div>x</div>` and `<script>...</script>` render as
    their literal characters with the plugin and without it, so the plugin was removed as the dead
    code it was, and the test stayed. It is worth a test because this store's markdown is full of
    such placeholders: measured here, 343 across 100 documents in the repo's own
    collection, `<stdin>` 42 times and `<uuid>` 31. A version that started treating them as markup would
    delete them silently.
  - Rendered: the compaction summary, each plan, and the drawer's full text when the row's own
    `type` says a person or Claude wrote it. A `tool_result` opens raw (86.5% of the records typed
    `user` are tool output), and a 220-character preview is never markdown, because the server
    already replaced its newlines with spaces. Raw is the same preformatted text as before, byte
    for byte, so the renderer can always be checked; Copy and both file exports carry the source,
    never what is on screen.
  - The messages before a boundary are a table: outcome, chars, role, type, date and time, and the
    message, each a column, the survivors still tinted green, the widths held still by the same
    machinery as every other table and each edge draggable. A search box narrows it with the
    matcher the Adopt window uses, says how many of the rows match, and says that it reads the
    first 220 characters, which is what the server sends. The CSV now follows both the checkbox and
    the search; it used to ignore the checkbox, so the file never matched the screen.
  - The table Export menu gains Markdown, a pipe table whose cells escape a pipe and collapse a
    newline, so no row can silently split.
  Tests: `Markdown.test.tsx` (12), cases in `exporters.test.ts`, four in `Windows.test.tsx`, and
  the axe gate over a rendered document.

- **A column keeps its width when the table is sorted, and every column, both side panels and the
  Adopt window's table can be dragged wider.** The user's words: "CHANGE ALL TABLES TO NOT RESIZE
  COLUMNS WHEN SORTING", "CHANGE ALL TABLES TO ALLOW RESIZING", "ALL SIDE PANELS (LEFT AND RIGHT),
  ALLOW RESIZING".
  - The table had no width rule at all: `table-layout` was never set, so the browser measured the
    cells in the DOM, and only one page of rows ever is. Sorting changed which values were measured,
    so the columns re-fitted and the whole table jumped. It is now `table-layout: fixed` with an
    explicit colgroup, and the widths come from `columnWidths.ts`, a pure function handed EVERY row
    of the table and nothing about sorting, paging or filtering. That is the fix: the widths cannot
    follow the page because the function cannot see it. `w-full` had to go with it, since under
    fixed layout a 100% width redistributes the surplus and overrides the colgroup; the surplus now
    goes to one column. Every cell gained `truncate`, because a fixed layout clips regardless and
    the choice is an ellipsis or a cut mid-character; the whole value is still in each cell's
    `title`. The sort arrow's space is reserved on every header, so the first click no longer widens
    the sorted column by a glyph, and the header gained `aria-sort` for readers who cannot see it.
  - One primitive does every resize (`useDragSize.ts`, `ResizeHandle.tsx`): a `role="separator"`
    with arrow keys, Home and End, Escape to put the size back, double-click to reset, and a hit
    area that cannot start the column reorder that shares the same header cell. Column widths are
    remembered per table (the table's id plus its column ids, never the slot it was drawn in, which
    is the defect `PaneTableIdentity.test.tsx` exists for), and the Columns menu gains Reset Widths
    once anything has been dragged. The left rail and the right drawer remember their widths too,
    and the rail keeps its collapse preference separate, so collapsing does not forget a width.
  - Three defects the running browser found that no jsdom test could: the window listeners were
    bound by an effect keyed on state, so a `pointermove` in the same tick as the `pointerdown`
    reached nothing and a quick drag did nothing at all (they are bound at pointerdown now); the
    `wide` column's 24rem cap was applied to the DRAG as well as to the estimate, so the widest
    column, the one most worth widening, refused to move; and the rail's width transition turned a
    drag into a rail lagging a fifth of a second behind the pointer, so it now animates the collapse
    alone.
  Tests: `useDragSize.test.tsx` (18), `columnWidths.test.ts` (13), `TableColumnWidths.test.tsx`
  (18, including the case that fails on the old code), `PanelResize.test.tsx` (7), plus cases in
  `Sidebar.test.tsx` and `AdoptSessions.test.tsx`, and the axe gate over every surface that now
  carries a separator.

- **The header and the sidebar say less, and say it once.** The user's list, after looking at the
  running page: uppercase the sidebar's two group headings, rename the tab that reads "All Sessions"
  to "Sessions", make "All" and "Current" the same width, combine "Stop C4X" and "Restart C4X" into
  one pair reading "Stop" and "Restart", and remove the "This Selection" chip, which "doesn't change
  based on what you select". Each one, and what it cost:
  - The group headings are uppercased by CSS, not by the string: the words in the DOM stay "All" and
    "Selection", so the accessible name, find-in-page and every assertion still see what was written.
  - The tab label has exactly one definition (`c4x/ui/layout.py`), which the React sidebar, the Dash
    tab strip and `c4x.cli tabs` all read; the id `tab-sessions` is untouched, since it is the
    `?tab=` value, the icon key and the button id `tools/screenshots.py` selects on. The three
    sentences on the page that pointed a reader at the tab by name were renamed with it. The
    population dropdown's "All sessions (N listed)" is a different thing and keeps its name.
  - Both sides of a switch now grow from a zero basis, so each is as wide as the wider label and the
    pair stays symmetric through a font change or a longer word.
  - The two server buttons became one bordered pair, the shape the Account switch already used, and
    say the verb alone because the group is already named "C4X server". Each keeps "Stop C4X" and
    "Restart C4X" as its accessible name: the dialog each opens has its own button reading "Stop"
    and "Restart", nothing marks the page behind a dialog inert, and two buttons answering to one
    name is both a screen-reader ambiguity and a broken query. All seven existing tests passed
    untouched, which is what that decision bought.
  - The chip now renders only when a tab is describing the whole store while a selection is set,
    which is the one state worth an interruption. "This Selection" is gone, and so are the two data
    attributes it carried, which nothing in the repo read. The pane body stops suppressing the
    population sentence except where the chip is actually saying it, so the selection case keeps it.
  Tests: `SidebarGroups.test.tsx` (the shout is CSS), `AccountSharing.test.tsx` (one width rule for
  both sides), `ServerControls.test.tsx` (one pair, the verb alone, and the dialog's "Stop" is the
  one inside the dialog), `App.test.tsx` (nothing at all when the tab describes the selection),
  `tests/test_tab_groups.py` (the tab is not named after a population it does not list), and
  `ServerControls` joins the axe gate, where `label-content-name-mismatch` guards the labels above.

- **The dashboard header no longer carries a privacy paragraph.** 0.1.0 added it; it repeated the
  same three sentences on every tab of every session, which is how a standing notice becomes
  furniture. The store's path is in the header and what is in the store is in the README.
  `c4x/ui/layout.py` records the reasoning where the paragraph used to be.
- **`harvest --stats` on a machine with no store yet** prints the zeroed shape with a note and exits
  0, and `install` runs one harvest when it finds no store. The README's own "confirm it captured
  something" step used to fail on every correct first install.
- **Hook events are ingested from the last offset** rather than by re-reading the whole log, which
  on one install meant 29,049 runs each re-reading a 36 MB file to learn what the previous run
  already knew.
- **`errors` no longer counts calls that never ran.** Claude Code sets `is_error` on a tool
  that RAN AND FAILED and on a tool that was REFUSED before it ran, and every "errors" number
  this app printed was the two added together. After the backfill this store reads 6,871 flagged
  calls: 1,848 refusals, 3,945 genuine failures, and 1,078 that predate the field and cannot be
  told apart. So 26.9% of what was called an error never ran, and of the calls that can be
  classified at all it is 31.9%. Per tool it is far worse, because refusal is not spread
  evenly: the `ExitPlanMode` row read 41 errors and NONE of them can be proven to have run and
  failed. That row now reads "13 refused, 30 unknown", because the shipped classifier will
  not claim a refusal it cannot prove and 28 of the flagged calls predate the field that would have
  proved it. (41 is the flagged count, `is_error = 1`; 30 is the unknown count across all 285 calls
  of that tool, which is what the row shows. This entry said 39 for the first figure while three
  code comments said 41, and 41 is what the store contains: an independent sweep found the
  disagreement.) Reading the result text of the calls that could be matched to a transcript:
  23 say the user did not want to proceed, 2 are permission failures, and exactly ONE is a genuine
  tool error. The plan said 3 and two rounds of commit messages repeated it unmeasured. Only the exact signal ships,
  so the app says unknown where an inference would have said refused.
  Six surfaces now read one merged `outcome`
  column that is BLANK when there is nothing to say, with the three counts kept as hidden
  columns so the CSV export still carries the numbers. It does NOT make them sortable: a hidden
  column has no header, so nothing in the browser can order by it, and the merged text cell
  sorts lexicographically. The plan claimed sorting too; that half was not deliverable and is
  stated here rather than left as a promise the page does not keep.
- **A store harvested before those columns existed reports "N unknown", not zero.** A blank
  column there would be indistinguishable from "everything succeeded", which is the defect
  being fixed. The page names the command that resolves it.
- **Calls flagged by a build older than 2.1.202 are Unknown and stay Unknown.** That build
  recorded no reason, and its refusal records are identical to its failure records down to the
  key set, so roughly 818 real failures move out of `errors`. Reporting them as errors would be
  the same defect, twenty times smaller. The era is stored, so this is a one-line policy change
  later.
- **Hook and user are NOT separated**, because Claude Code does not separate them: a settings
  deny rule and a hook block both record `permission-rule`, and the leak runs both ways. The
  vocabulary is reported as recorded and the column help says why.

### Fixed

- **An imported project no longer moves itself back to the machine it came from.** An import
  rewrote a session's working directory and its transcript path and left `project_slug` naming the
  EXPORTER's slug directory, which breaks the invariant harvest repairs rows against
  (`slug_of(cwd) = project_slug`, true for 1,438 of 1,439 rows on the author's store). The first
  harvest pass over the destination then rebuilt `cwd` from the transcript's own lines, which an
  import deliberately does not rewrite and which still name the source machine, and the project
  reappeared under a directory that does not exist here. The import now writes the slug with the
  directory, and the repair in `tools/harvest.mjs` takes a session's `cwd` only from a cwd the
  transcript proves is its home, keeping what the store holds when that agrees with the directory
  the file is in.
- **A chat resumed after a move folds into the session it resumed.** Links are only derived between
  transcripts of one working directory, read from the files themselves, so an imported transcript
  (naming the source) and a resume taken here (naming the destination) looked like two projects and
  the chat stayed two rows under two names for good. The link pass now reads the store's own cwd
  for a transcript that cannot name its home.
- **A delete no longer removes a transcript file that a session it did not touch is also in.**
  One file can hold two sessions' records, and the delete already computed which files those were,
  and spent the answer on the exclusion decision and the snapshots while purging the file anyway.
  Measured on the author's store: 7 files are claimed by more than one session row, and in one of
  them the second session is not in the project being deleted. The file is kept and named in the
  report, the way `memory/` and the trust entry already were, AND SO IS ITS HARVEST OFFSET. That
  second half was found by an independent reviewer: a directory that shares a file is left
  capturing on purpose, harvest reads a path with no offset row from byte zero, and a delete that
  kept the file while removing its offset was undone by the next pass. Measured on a copy of the
  author's store, one harvest pass run twice over the same delete: with the offset kept the pass
  read nothing, and without it the pass re-read 9,108 lines and put back 2 sessions, 4,140 turns,
  2,943 messages and 1,574 tool calls.
- **An export carries, and a delete removes, the harvest offset of EVERY transcript a session
  wrote.** The app-state layer has always carried the whole `<session id>/` directory, subagent
  transcripts and tool output included, and `files` was scoped to the session's own top-level
  path: only 1,436 of the author's 9,094 offset rows were reachable that way, and one project's
  export carried 138 offsets where it should have carried 1,253. An offset left behind says a file
  has been read to its end, so those bytes were skipped forever if the file came back. The rule is
  now the session id in the path, which is what the capture matches on; deriving a prefix from
  `sessions.transcript_path` instead still missed 19 of that project's offsets, because one session
  on this store has a `transcript_path` naming a subagent file. The preview, the export, the
  delete's acceptance check and the delete itself share one scoping rule instead of four copies
  of it.
- **A moved transcript keeps the timestamp that orders ingest.** The offset row copied to the
  destination path was written from a hand-typed column list that predates `files.first_ts`, so the
  copy was not the row it claimed to be. It is read from the table now, and the offsets of the
  subagent transcripts move with it.
- **Every desktop records root is read and purged, not only the one this machine writes to.** A
  packaged install can keep records under `%APPDATA%` beside its own container (the author's test
  laptop holds 16 under one and 1 under the other), and the page has read both since the Sessions
  list began mirroring the app. An export therefore carried chats without the file that names them,
  and a delete left the deleted chat listed under its old name. One copy travels, the app's, and
  the copy that does not is named; a purge removes every copy whose bytes the backup holds and
  keeps and names any that differ.
- **An export from a store harvested before `session_links` existed no longer fails.** The row
  copy skipped the missing table and the manifest's count loop then raised `no such table` on
  the same file, so `delete`, whose backup is an export, refused too. Every loop over the
  per-session tables now reads the store's own table list; a preview, an export, a delete and an
  import of such a store carry what it has.
- **The compaction pages count over the same chat as the list they were reached from.** The SQL
  twin of the member map resolved `head_id` one hop while every other reader followed a chain to
  its end, so on rows that outlived the pass that wrote them (`B -> A` beside `A -> Z`) a
  compaction's dropped-message count covered a different set of sessions. One flattened map is
  now bound into those queries. `cli sessions` also counts a member with no turns row of its own,
  and an arm B that resolves into arm A's own chat falls back to the default arm and is reported
  in an `x-c4x-compare-requested` header rather than rendering the chat against itself.
- **`node tools/harvest.mjs --help` printed nothing and ran a harvest.** Any flag the tool does
  not know is now refused with the usage; `--help` prints it.
- **The page reads every desktop record root, as harvest already did.** A packaged install keeps
  its records in its own container and can leave older ones under `%APPDATA%`; the page read
  only the root the app writes to, so a record under the other one named its chat by the
  transcript and never marked it archived while harvest had treated it as a chat. Readers now
  walk `claude_appdata_roots()`; writers (import, purge) keep using the app's own root. Found by
  an independent check against the test laptop's records, where it was one record of seventeen.
- **An import no longer writes the EXPORTER's path into your `~/.claude.json`.** The destination
  was resolved with `mapping.get(row["cwd"], row["cwd"])`, exact string equality on a value that
  arrives from three places: `sessions.cwd` for a transcript, the raw config KEY for a config
  entry, and the string inside the record for a desktop row. Those need not be spelled the same,
  which is why `normalised()` exists at all: 4 of the 91 projects on this machine carry both slash
  spellings. So a project whose config key used forward slashes had the exporter's absolute path
  written into the importing user's config, kept the imported chat pointing at a directory that
  does not exist on that machine, and still passed the mirror check. Found by independent review.
- **The mirror check now sees the two fields an import rewrites.** A desktop record is hashed with
  `cwd` and `originCwd` neutralised, so a rebased record can be compared at all, which left exactly
  those two fields outside every check: a record pointed at a directory that does not exist
  returned ok. The hash still covers everything the import must not change; the rewritten values
  are now checked against the destination.
- **`--into` is validated before anything is derived from it.** A relative path, an existing file,
  and a page label ending in the archived suffix were all accepted. Nothing failed loudly; the
  project was filed under a name nothing would look for, since `..\x` slugs to `---x`.
- **A rows-only export is no longer called a mirror.** `delete` takes its backup that way, so
  pointing `verify-mirror` at one returned ok while every file in that project's directory was
  carried by nothing.
- **"byte for byte" was an overclaim** and the wording now says what holds: transcripts, memory and
  tasks land byte-identical, while a config entry and a desktop record are content-identical with
  the working directory rebased. A real record grew from 181,366 to 190,303 bytes on being
  re-serialised.
- **An export no longer holds itself in memory.** The whole capture was built as a list of blobs
  before a byte reached the disk, 694.5 MB for this repo's own project, all of it already on the
  disk it was read from. Capture now streams into the export row by row.
- **A delete no longer orphans the two cost tables.** `cost_state` and `cost_state_models` were
  appended to `BY_SESSION` after `sessions`, which put deletion back in the window that ordering
  exists to close: `sessions` gone while rows still pointed at it. The self-test had been
  reporting it as a FAIL. Nothing but the deletion order reads that sequence, so exports and
  footprints are unaffected.
- **No test WRITES to the real `~/.claude`, `~/.claude.json` or `%APPDATA%\Claude`,** and none of
  them reaches those paths through `c4x/appstate.py`. An autouse fixture points that module's three
  roots at a temporary directory. The risk was not in the tests that mean to touch those paths; it
  was in the export tests that now capture app state and did not know they would read the
  developer's own machine.
  The claim stops there on purpose, because a wider one would be false: `c4x/store.py` still READS
  the real machine, by design, and the suite depends on it. `archived_sessions()` is what puts the
  `\archived` marker on a session row and `transcript_ids()` is what tells an imported session from
  a local one, so pointing those at an empty directory silently changed what every pane test saw.
  One test asserts the marker against the real records and is right to. Two of these reads are
  named in `tests/test_appstate.py::TestTheSlugAgainstTheRealMachine`, which skips where there is
  nothing to compare.
- **Mutation routes refuse a cross-origin request.** A multipart POST is a CORS simple request, so
  it reached the import handler and staged the upload to disk before anything validated it. The
  guard is middleware rather than a route dependency, because FastAPI reads the body first.
- **The store directory is created with a narrow ACL** on every path that creates it, not just the
  installer's. On a data drive it had been inheriting `Authenticated Users`.
- **Redaction covers values, not just variable names**, and its stand-in identifiers are stable
  across processes; they were derived from `hash()`, which Python salts per process.
- **Optional tables no longer raise.** A store with no probe or calibration rows renders an empty
  state instead of an exception panel, and the suite now runs against a first-run fixture that
  actually has those tables missing.
- **Every table on the turn-diff panel shows the query that actually ran.** Three of them were
  handed a retyping of their own query. One said `SUM(is_error)` where the real one said a
  `CASE`, and all three dropped the scope clause AND its bound arguments, so a reader who copied
  what was shown got the whole store back instead of the session in front of them.
- **`errors` left the Tool Calls heat map.** `heat_cells` filters to numbers, so a text column
  in that list is a silent no-op: a shading feature quietly doing nothing. It had also been
  shading a count that was 27% refusals, so the darkest cells pointed at tools that had not
  failed.
- **`make_fixture.mjs` can exercise any of this.** It hard-coded `is_error` to 0 for every
  synthetic row, so CI could not reach a single one of these paths.

### Known limits

- A hand-run `tools/statusline.mjs` whose payload session id matches the ambient one is still
  counted as a genuine sample. The cross-check is a session-id heuristic and `statusline.mjs`
  says so.
- `data/raw/events.ndjson` is ingested incrementally but never rotated, so it grows.
- A refused call cannot be attributed to a hook rather than to a person, or the reverse. Claude
  Code records `permission-rule` for both a settings deny rule and a hook block, so this store
  holds no evidence that would separate them. An earlier draft of this entry put numbers on
  how often the two recorded kinds cross over; those came from a text heuristic, do not
  reproduce, and have been removed rather than restated. The limit is in the source, and
  nothing here guesses at the split.
- A call flagged by a build older than 2.1.202 can never be classified: that build recorded no
  reason at all, and its refusal records are identical to its failure records.

## 0.1.0 - 2026-08-29

First tagged version. Everything below already worked; what changed is that it can now be checked by
someone who did not write it.

### Added

- **`tools/run_tests.mjs`**, one command for the whole suite: 384 checks across 16 files at this
  release, which is a record of that release and not the total today. Exit 0 is
  not treated as a pass, because two tools exit 0 for any argument and a runner that trusts the exit
  code reports a total that is a lie. A file with no self-test fails the run; each Python check
  declares the string its success must print.
- **`tools/make_fixture.mjs`**, a synthetic store so the store-dependent checks can run without a
  real capture. Built through the owning tools' own schemas, never a pasted copy of them.
- **`.github/workflows/tests.yml`**: builds the fixture, runs the suite, deletes the store after.
- **Linting**: `ruff` for Python (config in `pyproject.toml`) and `eslint` for the node tools
  (`eslint.config.mjs`, no dependencies, run with `npx --yes eslint .`).
- **`CONTRIBUTING.md`** and **`docs/architecture.md`**, including the invariant that catches
  everyone: sum `api_calls`, never `turns`.
- **`package.json`** for `engines.node >= 24` and script aliases. Deliberately no dependencies.
- **A privacy line in the dashboard header**, stating with a live count that the store holds the
  text of your conversations. The README said so already; the page a person actually looks at did
  not.

- **The dashboard is a package.** `app.py` was 3124 lines and is now 995, with `c4x/theme.py`,
  `c4x/store.py`, `c4x/panels.py`, `c4x/breakdown.py` and a `c4x/tabs/` package of ten modules,
  one per tab, beside it. `tools/table_audit.py` was taught to
  read the whole package first, because it named `app.py` in seven places and splitting the file
  under it would have left the static scan reporting fewer construction sites and passing.

### Fixed

- **`/__shutdown__` accepted GET.** Binding to `127.0.0.1` is no defence, because the browser is on
  loopback too, so any page you visited could stop the dashboard with an `img` tag. POST only now,
  and GET returns 405 rather than falling through to Dash's catch-all and answering 200.
- **A shadowed variable made a self-test pass for the wrong reason.** An inner block declared its own
  flag while a later line still read the outer one, which was already true. Found by turning on
  `no-shadow`, which is the entire argument for the rule.
- Four `zip()` calls now state `strict=`, so a length mismatch between columns of one frame is an
  error rather than a silent truncation.

### Known limits

- `tools/table_audit.py` does not cover clientside callbacks, which are JavaScript, or a branch this
  run's inputs never take. Both are named in its docstring rather than left to be discovered.
- The window math is pinned to a Claude Code build. Re-run `node tools/mirror.mjs --validate` after
  an update before trusting the predictor.

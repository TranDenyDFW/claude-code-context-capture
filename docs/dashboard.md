# The dashboard, in detail

What the README says in a few sentences, in full. Every paragraph here was the README's own
until the beta simplified it; the measurements and the reasons are unchanged. The desktop-app
features (the Account switch, Adopt, review runs) go deeper in
[docs/desktop-records.md](desktop-records.md).

## How it starts and stops

`http://127.0.0.1:8059/`. It starts with Claude: the SessionStart hook asks the port who is there
and, when nobody is, starts the server detached; the server watches for Claude processes and stops
itself about a minute after the last one exits. `node tools/install.mjs status` says whether it is
up and how to stop it by hand; `install --no-dashboard` turns the autostart off (`--dashboard` turns
it back on), as does `C4X_NO_DASHBOARD=1`. `python -m c4x.api` by hand still works and does not
stop itself unless asked to with `--watchdog`. The header's **Stop** stops the server from the
page (it asks first; the next Claude session starts one again) and **Restart** starts a fresh
one with the same flags and reloads the page once a different process answers. Once the server is
up it runs one sweep: the records c4x wrote for review runs (below) are taken back and, when that
removed any, Claude is restarted so its sidebar reflects it; never twice within ten minutes, only
while Claude is running, off with `install --no-review-sweep` (`--review-sweep` undoes it) or
`C4X_NO_REVIEW_SWEEP=1`. Closing the page does not stop capture. The page is
committed built, so npm is needed only to change the frontend. The same server serves every tab as
JSON: `curl 127.0.0.1:8059/api/tab/tab-cost`. The server runs without a console and gives none to
the programs it runs (`c4x/proc.py`), so nothing flashes when the page loads.

## The header

The header says less and shows it on hover: the population list (the unlabelled dropdown after
Search) names a folder holding one chat by that chat's title and a folder holding several by its
name, more of the path only when two rows would read the same, a folder-less chat by its name,
and every row shows its full path on hover, which is why it is drawn by the page rather than as
a native select; the **All** / **Current** switch says on hover how many chats each side shows,
and that switching closes Claude and starts it again, after asking. Under All, sharing keeps itself
whole: an account signing in with an organisation the sharing never saw gets a directory of its
own from the app, and the server folds it into the shared one a minute after Claude closes (the
watchdog's stop, or the server's start with Claude closed), so every account reads the same list
after any sign-in; the header names such a pair while it waits ("not yet covered") with a **Cover
now** button for a machine where Claude is already closed, and `intended` is read from the links
on disk when the marker beside the store is gone. A junction is written with the spelling the
kernel resolves to the shared directory (the Store build virtualises `%APPDATA%\Claude` into its
package's LocalCache for everything it spawns, this server included, and a junction made with the
virtual spelling lands somewhere else), pairs are compared by identity, and a link that lands
elsewhere or nowhere is re-pointed the same way, what was visible through it copied into the
backup first. A chat the desktop app holds a
record for is listed whatever its size (the five-row floor keeps only record-less one-shots out).
A chat deleted in the desktop app is hidden from every list here (the app leaves a
`deleted_<uuid>` marker beside where its record was; harvest reads it; the transcript is never
touched and a raw session id still opens the chat).

**Every switch asks, then the server does the rest.** A click on All, Current or Cover now opens
a window naming exactly what will happen ("This closes every Claude window, including any chat
in progress, links the account directories so every account reads the same chats, and starts
Claude again. Continue?"; "Claude is not running, so nothing is quit" when it is closed; Current
adds that it takes chats away from every other account on the machine). Cancel does nothing at
all. Continue sends the request with `restart: true`, and the server quits the desktop app,
moves the directories, and starts the app again (`c4x/desktop.py`, `with_restart`; a progress
line follows Claude running, closed, back); the outcome stays on screen: "Claude was closed and
started again", "through the task scheduler" when the app's own activation brought nothing back,
or "Claude is closed: ... start it by hand", the one outcome that asks for something. The same
window guards Adopt, Name them, Remove them, Import (each writes a file Claude reads when it
starts, so those act first and restart after), and the Stop and Restart pair. The CLI is never
touched: `claude.exe` in a terminal shares the app's image name, and the server tells the two
apart by the executable's path. The Current hover says where its number came from: the account
each chat was made under (harvest's tag, with the count of chats no tag names an account for),
or the sharing backup's manifest, or the directories themselves.

**The account a chat was made under.** The population list offers "Signed-in account's chats
(N)", one "Account <id>" entry per account seen, and "No account known"; Sessions carries an
`account` column; `docs/desktop-records.md` section 6 says how the tag is decided and what it
cannot know.

**One number for the signed-in account.** The header's hover and the population list read the
same count from one function (`store.listed_by_account`): the chats THIS PAGE lists for the
signed-in account. The desktop app lists more (its records: on the author's machine 175 records
against 107 listed chats, the rest being chats whose transcripts the store never held, or with no
turns), so the hover says both: "107 listed here; 175 in the app". All says the same two ways
round: the chats listed here, and the records in the app across the account directories.

**Projects A to Z, by the name shown.** The forty projects offered are still the forty with the
most work in them (the rule above the ranking says why); they are listed in alphabetical order
of the name on the screen, case folded, the count suffix left out of the comparison.

**The Summary chart is named the way the list is.** "Tool Bytes by Project" labels its bars with
the same names the population list shows (`store.project_labels`: the folder's leaf, a one-chat
folder by its chat's title, a folder-less chat by its name, two names that read the same with
their folder appended), instead of the shortened path it used to draw. The bar's own value keeps
the full path, so the hover still reports it. The suffix that tells two same-named folders apart
is computed per list, so the chart's fifteen and the dropdown's forty can differ where only one
of them holds both folders. The Summary tab names the calls that rewrote their whole context right after an
account switch ("The cache was rewritten after an account switch"), from harvest's account log,
and says what to do: finish a chat under the account it started with.

## Adopt, and review runs

After Restart, the **Adopt (N)** button (the hover says what N is) opens a window over the
page (the page dimmed behind it, the way the Project dialog opens; Escape or the backdrop closes
it) listing the chats on this machine that the desktop app has no record of, as a table with one
row per folder: the folder (a folder holding one chat shows that chat's title under it), how many
chats, the newest, and the path. A search box at the top narrows the table as you type, by folder
name, path or chat title (every word must appear somewhere, in any order), says how many folders
match, and a folder that is in the table because a chat title matched opens to show its chats;
the arrow beside a folder opens it by hand. Escape clears the search first and closes the window
second. Select all takes the folders shown. A reinstall leaves every transcript and none of the
records, so the app shows a cloud list pointing at a device that no longer exists; ticking a
folder writes the records into the signed-in account's directory and Claude lists the chats after
a restart. Nothing is preselected: a chat you deleted in the app looks the same to this rule as
one a reinstall orphaned, and the page says how many of those the app has deleted. The same
window names the records c4x wrote that carry no name, after the store's name for each. A
headless child run (a harness's `claude -p` one-shot a chat's shell command spawned, or a batch of
them with no chat behind it) is never offered: it folds into the chat that spawned it or under
the folder above it, the window's Runs column says how many each folder carries, a line says how
many are folded where, and the records an earlier build wrote for runs are taken back with the
review records (`docs/desktop-records.md` section 7 has the rule).
A review run (a hook's `claude -p` that read another chat) is never offered: harvest ties it to
the chat it read by quotation (`desktop-records.md` §7), it is listed nowhere on its own,
the chat's page lists it with its verdict and the prompt it followed, and its tokens count toward
the chat under "Including Subagents" (the Cost tab always). A run whose lines are said somewhere
in the store but by no session in its folder, and whose reply is a bare verdict, is a review the
store cannot place: recorded with no chat, listed nowhere, never offered. The drawer offers to take
back the records an earlier build wrote for such runs (`POST /api/adopt/unadopt-reviews`), which
the server does on its own at startup (`GET /api/adopt/sweep` says what the last sweep did).

## Markdown, where the text is markdown

A compaction summary and an `ExitPlanMode` plan are documents Claude wrote in markdown, and they
were shown as one preformatted wall of 12,000 to 17,000 characters. They render now: headings,
lists, fenced code, block quotes and tables, with **Raw** one click away showing the same
preformatted text byte for byte, a **Copy** that copies the source, and **.md** and **.txt**
buttons that write the source with no byte-order mark. The drawer's FULL TEXT renders the same way
when the row says the text is a document (`assistant`, `compact_summary` and `typed`); a
`tool_result` opens raw, because 86.5% of the records typed `user` are tool output, where markdown
eats the indentation and turns a `#` comment into a heading. A 220-character preview is never
rendered as markdown: the server already replaced its newlines with spaces, so it is not the
document any more. Each table's Export menu also offers **Markdown**, a pipe table of the rows on
screen with the whole message hydrated in.

Nothing raw is ever parsed. There is no `rehype-raw`, so no HTML string is ever built and a
`<script>` in a transcript is text; a link opens only for http, https and mailto, and never with
this page behind it; an image is not fetched at all, because an `img src` in a transcript is a
request to a third party saying somebody opened that record. Raw HTML that IS in the text keeps its
literal characters: measured against react-markdown 10.1.0, `<uuid>`, `<div>x</div>` and
`<script>...</script>` all render as they were written. That is worth a test rather than trust,
because this store's markdown is full of angle-bracket placeholders (343 of them across 100 of the
732 documents this repo has collected, `<stdin>` 42 times and `<uuid>` 31), and a version that
started treating them as markup would delete them without a word.

## Running without Python

**Without Python.** `tools/build_exe.py` builds the server into `dist/c4x/` (`c4x.exe`) with
PyInstaller, and the `build-exe` workflow attaches that directory to every release as
`c4x-windows.zip`. Unpack it into `dist/c4x/` under the checkout and the hook uses it when no
Python imports the dashboard. It replaces Python only: the hooks and the harvester are node, and
the exe runs from inside a checkout, never on its own. Built on a machine with the Claude desktop
app installed, the exe carries the app's icon, read out of the installed app at build time and
never committed (`C4X_ICON=<file.ico or file.exe>` names another source); the release build has
PyInstaller's icon.

## The tabs

The CLI renders the same callbacks the browser does, so a dump is what the page shows rather than a
parallel implementation of it.

| Tab | What it answers | Dump it |
|---|---|---|
| Summary | what is worth doing about this store | `python -m c4x.cli dump --tab tab-summary` |
| Sessions | every chat as a point and a row, with a resumed chat's sessions folded into its newest one | `--tab tab-sessions` |
| Session | where one session's window went | `--tab tab-session --session <id>` |
| Compactions | what each compaction discarded | `--tab tab-compactions` |
| Window | what is in the window right now | `--tab tab-window --session <id>` |
| Cost | what was read twice, and what it cost | `--tab tab-cost` |
| Compare | two populations, measured the same way | `--tab tab-compare --compare-with <id>` |
| Diagnostics | is the capture healthy | `--tab tab-diagnostics` |

![The Summary tab: findings that each name a session and an action, store totals, and tool bytes by working directory](images/summary.png)

Every finding is clickable: it selects the session it names and jumps to the tab that proves it, so
a claim on the front page is one click from its evidence.

![The Cost tab: re-read groups, the concentration curve, and how each tool call turned out](images/cost.png)

Cost is an estimate and says so, from a price table at `c4x/prices.json` refreshed by CI against
two published sources that have to agree. A model with no entry renders blank, never zero.

**`errors` counts what failed, not what was refused.** Claude Code sets one flag on a tool that ran
and failed and on a tool that was stopped before it ran. Measured on this store, 26.9% of what was
called an error never ran. The `outcome` column reports the two apart, and says `unknown` where the
transcript cannot prove which it was.

![The Compactions tab: every compaction with its predicted trigger, its overshoot, and the survivors it kept](images/compactions.png)

`overshoot` is how far past the predicted trigger the session actually got. Clicking a row opens the
summary the compaction wrote, in full and rendered, plus the messages absent from its survivor
list, recovered from the store rather than reconstructed. On the compaction's own page those
messages are a table: outcome, characters, role, type, date and time, and the message, each in a
column of its own, with the survivors still tinted green. A search box above it narrows the list
(every word must appear, in any order) and says how many of them match; it reads the first 220
characters of each message, which is what the server sends and what the page shows, and the line
beside it says so. The CSV export carries what the page is showing, both the checkbox and the
search included.

**Two tabs need a reading the install does not take.** Window and Diagnostics stay empty until you
record one, and say so on the page rather than looking broken. Nothing else depends on either,
which is why the install does not run something that costs money on your behalf.

```bash
node tools/probe.mjs                  # one billable session, about 12 seconds
node tools/breakdown.mjs --calibrate  # your configuration's fixed overhead
```

![The Window tab: what is in the context window right now, as area, grouped into configuration, messages and free space](images/window.png)

### What did you pay for twice?

The Cost tab's question, from the command line, with a sample from the redacted store.

**What did you pay for twice?**

```bash
node tools/waste.mjs --duplicates
```

```
duplicate reads (>= 3 reads of one file in one session)
  groups: 1129   re-reads beyond the first: 7323   bytes in the repeats: 72778.4 KB

    614x     297.1 KB  identical     582a3e1c  /work/categories.json
    340x    8819.3 KB  42 variants   7fe4cdc8  /books/_standards_catalog.md
```

`variants` is how a re-read hides: the same file reached by 42 different spellings of its path.

## Moving a project between machines

```bash
python -m c4x.projects export "P:\Work\Thing" --out thing.db
python -m c4x.projects import thing.db --into "D:\Elsewhere\Thing"
```

The export carries the conversations, not only the rows: the transcripts, project memory, tasks,
the trust setting, and the desktop app's own record, so the project opens in Claude Code on the
other machine. Everything is rebuilt from the destination you choose, on the importing user's own
paths. `--dry-run` names every destination and writes nothing; `verify-mirror` re-hashes what
landed and exits non-zero on a difference.

It moves CHATS. A chat resumed four times is five CLI sessions and one entry in the app, and the
export says so, carries every session it spans, and names any chat whose record it could not find
under a session it carries. The import checks the store afterwards and reports any chat whose
sessions landed here as more than one. Records are read from, and removed from, every root the app
uses, which on a packaged install can be two.


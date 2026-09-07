"""Display strings for paths, with no rendering dependency.

WHY THIS IS NOT IN theme.py. `short_path` started there, next to the other formatters, and that
was right while the Summary chart was its only caller. The population dropdown needs the same
shortening and is built in `c4x/store.py`, which imports NOTHING from this package: it is the data
layer, and `theme.py` pulls in dash and plotly. Importing it there to reach one pure string
function would put a rendering stack behind every query the CLI makes.

So the helper moves here, to a module with no imports at all, and `theme.py` re-exports it. That
keeps one definition. Two copies is what produced the defect this file exists to fix: the chart was
given `short_path` and the dropdown was not, so the axis labels were repaired while the dropdown
went on rendering two different projects as the same string.
"""


def short_path(value, keep=2):
    """A path shortened from the LEFT, keeping the tail that identifies it.

    The Summary chart labels its bars with the raw working directory, and on this store those run
    to about 150 characters, so the labels overflowed the panel and the bars they belong to were
    pushed off it. Truncating from the right is worse than useless here: every one of these paths
    shares a long prefix, so the first N characters are the part that is identical between them.
    The last two segments are what tells one project from another.

    Display only. The full value stays in the hover and in the data, because the shortened form is
    ambiguous by construction and nothing should ever match on it.
    """
    text = str(value or "")
    parts = [p for p in text.replace(chr(92), '/').split('/') if p]
    if len(parts) <= keep:
        return text
    return ".../" + "/".join(parts[-keep:])


def distinct_short_paths(values, keep=2):
    """Shortened paths, lengthened only where two of them would otherwise read the same.

    SHORTENING ALONE DOES NOT FIX THE REPORTED DEFECT. The complaint was not that the labels were
    long, it was that two different projects rendered as one identical string, so the list offered
    the same choice twice and neither could be told from the other. A fixed `keep` can reproduce
    that exactly: sibling scratch directories differ only further up the path.

    So collisions are resolved by giving the colliding entries more segments, and only them, until
    they differ or the whole path is showing. Everything unambiguous stays short.

    Returns a dict keyed by the original value, so the caller keeps the full path for the option's
    value and matches on that. Nothing may ever match on the label.
    """
    values = list(values)
    segments = {v: len([p for p in str(v or "").replace(chr(92), '/').split('/') if p])
                for v in values}
    depth = dict.fromkeys(values, keep)
    # Bounded: each pass gives at least one colliding entry another segment, and no entry can be
    # given more segments than its path has. Two entries that are the SAME path stop it early,
    # because no depth can separate them and `grew` stays False.
    for _ in range(max(segments.values(), default=0) + 1):
        out = {v: short_path(v, depth[v]) for v in values}
        seen: dict = {}
        for value, label in out.items():
            seen.setdefault(label, []).append(value)
        clashing = [group for group in seen.values() if len(group) > 1]
        if not clashing:
            return out
        grew = False
        for group in clashing:
            for value in group:
                if depth[value] < segments[value]:
                    depth[value] += 1
                    grew = True
        if not grew:
            return out
    return {v: short_path(v, depth[v]) for v in values}


def plural(n, one, many=None):
    """A count and its noun, agreeing.

    Four sites spelled this out by hand and a fifth did not, so a cohort holding exactly one
    session rendered "1 sessions in project ...". The caption sits directly beside a count that
    readers were already being asked to reconcile, and a sentence that looks broken there
    undermines the number next to it.

    Here rather than in c4x/theme.py for the same reason short_path is: c4x/store.py builds this
    caption and imports nothing from the package, while theme.py pulls in dash and plotly.
    """
    word = one if n == 1 else (many or one + "s")
    return f"{n:,} {word}"


# A chat started without opening a project. Claude Code puts those in a scratch workspace, and the
# path is the only signal: `entrypoint` reads `claude-desktop` for these and for ordinary desktop
# sessions alike, so nothing else in `sessions` tells them apart. Measured on this store, 3 of 1366.
SCRATCH_MARK = "scratch-workspaces"

# A last-prompt is a fallback, not a title, and tools/harvest.mjs says so where it stores one: "a
# whole opening request and can be thousands of characters". It arrives here already cut to 200,
# which is still far too long for a path label, so it is cut again and marked as cut.
PROMPT_LABEL_MAX = 40


def is_folderless(path) -> bool:
    """Whether this working directory is a scratch workspace rather than a project.

    A PATH HEURISTIC, AND IT LIVES HERE ON PURPOSE. c4x/store.py refuses one in as many words:
    "Nothing here knows or should know that 'tmp' means scratch: a directory earns its place by the
    work done in it, which is the same rule for every project." That rule governs which projects are
    LISTED and RANKED, and it still does. This is a display question, so it lives in the display
    module and may never reach a WHERE clause, a ranking, or a cohort's membership.
    """
    return SCRATCH_MARK in str(path or "").replace(chr(92), "/")


def titled_path(path, titles, keep=2):
    """A shortened path, with the chat's name appended when the path itself names nothing.

    A scratch workspace is a generated id under two more generated ids, so the shortened form is
    ".../<uuid>/scratch-2026-09-07-433162": correct, unique, and telling the reader nothing about
    what the chat was. Every other project's path is its own name and needs no help.

    `titles` is a mapping of kind to text for this session. The order is deliberate and the fallback
    is the risky one: a `custom` title was typed by a person, an `ai` one was written to be a title,
    and a `last-prompt` is merely whatever was said first. Measured on this store, all three
    folder-less chats have ONLY a last-prompt, so the fallback is not the rare path, it is the
    normal one, and it is cut hard and marked so it cannot be mistaken for a name someone chose.
    """
    if not is_folderless(path):
        return short_path(path, keep)
    # ONE SEGMENT, not two. The parent of a scratch workspace is a uuid, and on this store it is the
    # SAME uuid for every one of them, so keeping it spends 37 characters saying nothing and pushes
    # the part that does carry meaning, the chat's own name, off the end of the label. The final
    # segment already carries a date and a random suffix, so it stays unique on its own.
    short = short_path(path, 1)
    for kind in ("custom", "ai", "last-prompt"):
        text = (titles or {}).get(kind)
        if not text:
            continue
        text = " ".join(str(text).split())
        if not text:
            continue
        if len(text) > PROMPT_LABEL_MAX:
            text = text[:PROMPT_LABEL_MAX].rstrip() + "..."
        return f"{short} - {text}"
    return short


def stamp(value) -> str:
    """An ISO timestamp as a reader's date and time, or the value unchanged if it is not one.

    THE DATE IS THE POINT. The Messages table cut this to `HH:MM:SS` while the Compactions table on
    the SAME TAB showed the whole thing, so one page spoke two dialects. Worse than untidy: session
    4038e473 holds 97 rows from 2026-09-06 and 281 from 2026-09-07, so at row 97 the column ran
    backwards from 18:41:52 to 01:28:15 with nothing marking a new day, and sorting on it interleaved
    the two days. Store-wide, 4 clock times already occur on more than one date.

    Cutting happened server-side, so the date was missing from the value the table SORTS on, not only
    from what it displayed. That is why this returns a string the sort can still order: the ISO form
    with its T replaced sorts identically to the ISO form itself.

    Anything that is not a stamp comes back untouched. A formatter that mangles what it does not
    recognise is worse than one that declines, because the mangling is what reaches the page.
    """
    text = str(value or "")
    if len(text) < 19 or text[4] != "-" or text[7] != "-" or text[10] not in "T ":
        return text
    return text[:10] + " " + text[11:19]

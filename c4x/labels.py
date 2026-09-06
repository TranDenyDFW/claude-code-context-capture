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

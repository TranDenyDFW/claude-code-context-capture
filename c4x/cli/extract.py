"""Turn a rendered Dash component tree back into plain data.

The dashboard's numbers only existed as pixels, so the only way to ask "is that right?" was to look
at it. Everything here is deliberately dumb: it walks a tree and returns dicts and lists, so a test
can assert on a figure the browser would have drawn without a browser being involved.

Props are read through `_prop_names`. Walking `__dict__` instead reaches into Dash internals and
returns components that are not in the tree at all, which is a mistake this repo has already made
once in its table audit.
"""

import numbers

TABLE_TYPE = "DataTable"
GRAPH_TYPE = "Graph"


def _is_component(node):
    return getattr(node, "_prop_names", None) is not None


def _numbers(values):
    """The numeric entries of a sequence, as floats.

    isinstance(v, (int, float)) is NOT enough: plotly hands back numpy scalars, and numpy.int64 is
    not a Python int, so a chart full of numbers reported none at all. numbers.Real covers both,
    and bool is excluded because True is a Real and is never a measurement here.
    """
    out = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, numbers.Real):
            out.append(float(v))
    return out


def _seq(value):
    """A sequence, or empty. Never `value or []`.

    A plotly trace holds numpy arrays, and `array or []` raises "truth value of an array with more
    than one element is ambiguous" rather than being falsy. That turned every figure on the page
    into a crash the first time this ran.
    """
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def tables(node, found=None):
    """Every DataTable in the tree, as {id, columns, rows, tooltips}.

    Tooltips are carried ON the table rather than in a separate mapping keyed by id. Keyed by id
    they collapsed: the Breakdown tab renders nine tables and most are anonymous, so every one of
    them landed under "(anonymous)" and eight tables' tooltips were reported as the ninth's.
    """
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            tables(child, found)
        return found
    if not _is_component(node):
        return found
    if type(node).__name__ == TABLE_TYPE:
        found.append({
            "id": getattr(node, "id", None) or "(anonymous)",
            "columns": [c.get("id") for c in _seq(getattr(node, "columns", None))
                        if isinstance(c, dict)],
            "rows": _seq(getattr(node, "data", None)),
            "tooltips": {
                column: (value.get("value") if isinstance(value, dict) else value)
                for column, value in (getattr(node, "tooltip_header", None) or {}).items()
            },
            "sorts": getattr(node, "sort_action", None) == "native",
            # None means "stay up while hovering". Dash's default of 2000ms is shorter than these
            # tooltips take to read, so the value matters and a test asserts on it.
            "tooltip_duration": getattr(node, "tooltip_duration", 2000),
        })
        return found
    for name in getattr(node, "_prop_names", ()):
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or _is_component(value):
            tables(value, found)
    return found


def table_by_id(node, table_id):
    """One DataTable by id, or None. Tests name the table they mean rather than indexing by
    position, because position changes whenever a tab gains a section."""
    for table in tables(node):
        if table["id"] == table_id:
            return table
    return None


def figures(node, found=None):
    """Every plotly figure, as {title, traces, bands}.

    A chart carries numbers no table holds. A threshold band drawn at the wrong height is a wrong
    number, and it is invisible to any check that only reads tables.
    """
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            figures(child, found)
        return found
    if not _is_component(node):
        return found
    if type(node).__name__ == GRAPH_TYPE:
        fig = getattr(node, "figure", None)
        if fig is not None:
            found.append(describe_figure(fig))
        return found
    for name in getattr(node, "_prop_names", ()):
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple)) or _is_component(value):
            figures(value, found)
    return found


def describe_figure(fig):
    """The checkable parts of a plotly figure: trace extents and rectangle bands."""
    traces = []
    for trace in _seq(getattr(fig, "data", None)):
        # `or []` is not usable here: a plotly trace's y is often a numpy array, and the truth
        # value of an array with more than one element raises rather than being falsy.
        # BOTH axes. A horizontal bar chart carries its numbers in x and its labels in y, so a
        # reader of y alone reports None for every value on it, which is how the store-wide
        # "tokens by working directory" chart first came back looking empty.
        xs = _numbers(_seq(getattr(trace, "x", None)))
        ys = _numbers(_seq(getattr(trace, "y", None)))
        traces.append({
            "name": getattr(trace, "name", None),
            "type": type(trace).__name__,
            "points": max(len(_seq(getattr(trace, "x", None))),
                          len(_seq(getattr(trace, "y", None)))),
            "x_min": min(xs) if xs else None,
            "x_max": max(xs) if xs else None,
            "x_sum": sum(xs) if xs else None,
            "y_min": min(ys) if ys else None,
            "y_max": max(ys) if ys else None,
            "y_sum": sum(ys) if ys else None,
        })
    layout = getattr(fig, "layout", None)
    title = getattr(getattr(layout, "title", None), "text", None) if layout is not None else None
    shapes = list(_seq(getattr(layout, "shapes", None))) if layout is not None else []
    return {
        "title": title,
        "traces": traces,
        "bands": sorted((s.y0, s.y1) for s in shapes if getattr(s, "type", None) == "rect"),
    }


def texts(node, found=None):
    """Every rendered string, in tree order.

    Prose carries assertions too: a tab that states its population, or says a reading is partial,
    is making a claim that can be wrong.
    """
    found = [] if found is None else found
    if isinstance(node, (list, tuple)):
        for child in node:
            texts(child, found)
        return found
    if isinstance(node, str):
        if node.strip():
            found.append(node.strip())
        return found
    if isinstance(node, (int, float)):
        found.append(str(node))
        return found
    if not _is_component(node):
        return found
    if type(node).__name__ == TABLE_TYPE:
        return found                               # cells are data, not prose
    for name in getattr(node, "_prop_names", ()):
        if name not in ("children", "title", "label"):
            continue
        value = getattr(node, name, None)
        if isinstance(value, (list, tuple, str, int, float)) or _is_component(value):
            texts(value, found)
    return found


def tooltips(node):
    """[(table id, {column: text})] for every table that has any, in tree order.

    A LIST, not a dict: table ids are not unique here, and most of the Breakdown tab's tables have
    no id at all.
    """
    return [(table["id"], table["tooltips"]) for table in tables(node) if table["tooltips"]]


def all_words(node):
    """Everything a reader can read: prose, figure titles, and tooltips.

    The right target for "does the page say X", because where a statement lives is a presentation
    decision and a test should not break when it moves from a paragraph to a tooltip.
    """
    parts = list(texts(node))
    parts += [str(f["title"]) for f in figures(node) if f.get("title")]
    for _table_id, columns in tooltips(node):
        parts += [str(v) for v in columns.values()]
    return "\n".join(parts)


def joined_text(node):
    """All prose in one string, for a substring assertion."""
    return "\n".join(texts(node))


def describe(node):
    """Everything checkable about a rendered pane, in one dict."""
    return {"tables": tables(node), "figures": figures(node), "text": texts(node)}

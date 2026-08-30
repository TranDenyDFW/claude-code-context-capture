"""Print what extract.py collected, for a human at a terminal.

Kept apart from the extraction so a test never imports formatting, and so changing how a dump looks
cannot change what a test sees.
"""
import json

MAX_ROWS = 12
MAX_TEXT = 40
MAX_CELL = 48


def _cell(value):
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL - 1] + "…"


def table_lines(table, max_rows=MAX_ROWS):
    """One table as aligned text, truncated with the count of what was dropped.

    Never silently: a table printed short with no note reads as a table that short.
    """
    rows = table["rows"]
    columns = table["columns"] or (list(rows[0]) if rows else [])
    lines = [f"-- table {table['id']}  ({len(rows)} rows)"]
    if not columns:
        lines.append("   (no columns)")
        return lines
    widths = [len(str(c)) for c in columns]
    shown = rows[:max_rows]
    for row in shown:
        for i, col in enumerate(columns):
            widths[i] = max(widths[i], len(_cell(row.get(col))))
    lines.append("   " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(columns)))
    lines.append("   " + "  ".join("-" * widths[i] for i in range(len(columns))))
    for row in shown:
        lines.append("   " + "  ".join(_cell(row.get(c)).ljust(widths[i])
                                       for i, c in enumerate(columns)))
    if len(rows) > max_rows:
        lines.append(f"   ... {len(rows) - max_rows} more rows (--json for all)")
    return lines


def figure_lines(fig):
    lines = [f"-- figure {fig['title']!r}"]
    for trace in fig["traces"]:
        axis = (f"y={trace['y_min']:,.0f}..{trace['y_max']:,.0f}"
                if trace.get("y_max") is not None
                else (f"x={trace['x_min']:,.0f}..{trace['x_max']:,.0f}"
                      if trace.get("x_max") is not None else "no numeric axis"))
        lines.append(f"   trace {trace['name']!r} {trace['type']} "
                     f"points={trace['points']} {axis}")
    if fig["bands"]:
        bands = ", ".join(f"{y0:,.0f}-{y1:,.0f}" for y0, y1 in fig["bands"][:6])
        lines.append(f"   {len(fig['bands'])} bands: {bands}")
    return lines


def human(payload):
    """The whole dump as text."""
    head = [f"== {payload.get('tab')}   session={payload.get('session')}   "
            f"scope={payload.get('scope')}   cohort={payload.get('cohort')}"]
    out = list(head)
    for table in payload["tables"]:
        out.append("")
        out += table_lines(table)
    for fig in payload["figures"]:
        out.append("")
        out += figure_lines(fig)
    if payload.get("text"):
        out.append("")
        out.append("-- text")
        for line in payload["text"][:MAX_TEXT]:
            out.append(f"   {line[:200]}")
        if len(payload["text"]) > MAX_TEXT:
            out.append(f"   ... {len(payload['text']) - MAX_TEXT} more lines")
    return "\n".join(out)


def as_json(payload):
    return json.dumps(payload, indent=2, default=str)

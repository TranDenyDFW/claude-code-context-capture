"""Frames to records, and records to JSON, with one rule: a missing value is None.

WHY THIS FILE EXISTS. pandas 3 made the string dtype the default, and its missing value is NaN.
pandas 2 returned None. So `df.to_dict("records")` on a frame with a NULL text cell hands back
`float('nan')` on one and `None` on the other, and everything downstream that treats a missing
cell as falsy, or serialises it, silently disagrees between the two:

- `json.dumps` writes the literal `NaN`, which is not JSON. Python's own parser accepts it; a
  strict one, and JSON.parse in a browser, rejects the whole document. The CLI's `dump --json`
  did exactly that on any fresh install, 32 times on one tab, while the API was safe only because
  it round-trips through Plotly's encoder first.
- A test asking "is this cell blank" gets True from None and False from NaN, because NaN is
  truthy. One such test skipped on CI and ran on a developer machine, and the difference was
  chased across two platforms before it turned out to be two pandas.
- Both table audits let NaN through: it is a float, so it counts as numeric-by-content, and it is
  never a string, so no placeholder rule sees it. The one representation that breaks JSON was the
  one nothing checked.

The rule, then: at the moment a frame becomes rows, every missing value becomes None, whatever
pandas called it. None is what the contract audit already calls "the correct way to say unknown",
what the API already emits after coercion, and what an empty cell in a DataTable renders as.
"""
import json
import math


def records(df):
    """`df.to_dict("records")` with every missing value as None, not NaN, on any pandas.

    `astype(object)` first, because `where` on a `str`-dtype column would refill with that dtype's
    own missing value and hand NaN straight back. On object dtype, None stays None. Non-frames pass
    through as a list, so a caller that already holds rows can use this unchanged.
    """
    if not hasattr(df, "to_dict"):
        return list(df)
    if df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")


def jsonable(payload):
    """A payload made of types JSON actually has: numpy scalars to Python, NaN and pandas NA to
    null, timestamps to strings.

    Plotly's encoder already does all of that for figures, so it is the one used, rather than a
    second list of special cases that would drift from the first. The round-trip through a string
    is the cost of reusing it; on the API that string is what gets cached anyway.
    """
    import plotly.utils as plotly_utils
    return json.loads(json.dumps(payload, cls=plotly_utils.PlotlyJSONEncoder))


def has_nan(value, _depth=0):
    """True if a float NaN is anywhere inside a nested payload. Used by the audits and the tests,
    because a NaN that reaches a payload is the whole defect this module exists to prevent."""
    if isinstance(value, float):
        return math.isnan(value)
    if _depth > 50:
        return False
    if isinstance(value, dict):
        return any(has_nan(v, _depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_nan(v, _depth + 1) for v in value)
    return False

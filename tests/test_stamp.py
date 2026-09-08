"""A timestamp column that hides the date runs backwards, and the page said nothing about it.

The Messages table cut its stamps to `HH:MM:SS` while the Compactions table on the SAME TAB showed
the whole thing. Session 4038e473 holds 97 rows from 2026-09-06 and 281 from 2026-09-07, so at row
97 the column ran backwards from 18:41:52 to 01:28:15 with nothing marking a new day. Store-wide, 4
clock times already occur on more than one date.

The cut happened SERVER-SIDE, so the date was missing from the value the table sorts on, not only
from the text. That is why the ordering test below is not decoration: a formatter that fixed the
display and left the sort would have looked fixed.

Nine sites had hand-rolled the same cut, in four spellings, and the report named one. The last test
here is the sweep, so the tenth cannot be written without failing.
"""
import pathlib

import pytest

from c4x.labels import stamp

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("given,want", [
    ("2026-09-07T01:28:15.123Z", "2026-09-07 01:28:15"),
    ("2026-09-07T01:28:15", "2026-09-07 01:28:15"),
    ("2026-09-06 18:41:52", "2026-09-06 18:41:52"),
])
def test_a_stamp_keeps_its_date(given, want):
    assert stamp(given) == want


@pytest.mark.parametrize("given", ["", None, "not a stamp", "07:18:34", "2026-09"])
def test_anything_that_is_not_a_stamp_comes_back_unharmed(given):
    """A formatter that mangles what it does not recognise is worse than one that declines, because
    the mangling is the thing that reaches the page."""
    assert stamp(given) == str(given or "")


def test_the_two_days_that_ran_backwards_now_sort_apart():
    """THE DEFECT, in the shape it was seen. Time-only, these two compare the wrong way round."""
    earlier, later = "2026-09-06T18:41:52", "2026-09-07T01:28:15"
    assert earlier[11:19] > later[11:19], "premise: time-only really does invert this pair"
    assert stamp(earlier) < stamp(later)


def test_the_formatted_value_still_sorts_like_the_raw_one():
    """The column sorts on what the server sends, so the fix has to survive being sorted, not only
    being read. Replacing T with a space preserves the ordering; anything friendlier would not."""
    raw = ["2026-09-07T01:28:15", "2026-09-06T18:41:52", "2026-09-07T19:38:25",
           "2026-09-05T00:00:01"]
    assert [stamp(v) for v in sorted(raw)] == sorted(stamp(v) for v in raw)


def test_no_module_cuts_a_timestamp_by_hand():
    """THE SWEEP, not the instance. Nine sites had rolled their own in four different spellings:
    two kept the date, two threw it away, and one left the raw T showing. Finding the reported one
    and stopping would have left eight, three of them with the same defect.

    Reads the source rather than the behaviour on purpose. There is no store state and no rendered
    page in which a tenth hand-rolled copy is visible until someone happens to look at that column
    on a day the session spans midnight, which is exactly how this survived.
    """
    cuts = ('[:19]', 'slice(0, 19)', 'slice(11, 19)', '[11:19]')
    hits = []
    for path in sorted((ROOT / "c4x").rglob("*.py")):
        text = open(path, encoding="utf-8").read()
        # labels.py is where the one implementation lives, so it is the one file allowed to slice.
        if path.name == "labels.py":
            continue
        for line_no, line in enumerate(text.split(chr(10)), 1):
            if any(c in line for c in cuts):
                hits.append(f"{path.relative_to(ROOT)}:{line_no}")
    assert hits == [], (
        "these cut a timestamp by hand instead of calling c4x.labels.stamp: " + ", ".join(hits))


def test_the_sweep_can_actually_fail():
    """A scan that reports nothing looks the same whether it works or matches nothing at all."""
    cuts = ('[:19]', 'slice(0, 19)', 'slice(11, 19)', '[11:19]')
    known_bad = 'show["ts"] = show["ts"].astype(str).str.slice(0, 19)'
    assert any(c in known_bad for c in cuts)
    assert not any(c in 'show["ts"] = show["ts"].map(stamp)' for c in cuts)

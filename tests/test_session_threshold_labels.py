"""One label per distinct threshold on the Session chart, however many segments there are.

A session that switches model or context window several times draws one threshold line per
segment, which is right: the line's x-range says which turns that threshold governed. It also drew
one ANNOTATION per segment, all at the same y and all anchored at the same left edge, so the words
printed over each other into a smear. Noticed on the README's own hero image while regenerating it.

The compaction loop three lines below has carried the same lesson since it was written: "Label them
only when there are few: a session with 46 compactions printed the word 46 times into one stack."

READS THE REAL PLOTLY FIGURE, through `test_charts.raw_figures`. `extract.figures` deliberately
reduces a chart to its extents with no annotations at all, so a check written against it SKIPPED on
every session and would have reported success forever.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_charts import raw_figures  # noqa: E402


def threshold_labels(figure):
    """The words this chart writes for a compaction threshold."""
    return [str(getattr(note, "text", "") or "")
            for note in (figure.layout.annotations or [])
            if "compact at" in str(getattr(note, "text", "") or "")]


def threshold_lines(figure):
    """The horizontal rules drawn at a threshold, one per segment."""
    return [s for s in (figure.layout.shapes or [])
            if getattr(s, "type", None) == "line"
            and getattr(s, "y0", None) == getattr(s, "y1", None)
            and getattr(s, "y0", None)]


def charts_with_thresholds(pane, session_id):
    return [f for f in raw_figures(pane("tab-session", session=session_id))
            if threshold_labels(f)]


def test_no_two_threshold_labels_share_a_position(app, pane, session_id, has_store):
    """THE POSITION, not the text, which is what a reader actually sees overlap.

    Deduplicating the text was tried first and did not fix the chart: several models share the
    same 967k threshold, so their labels differ while sitting at the same y and the same left
    anchor. The property is one label per threshold VALUE.
    """
    charts = charts_with_thresholds(pane, session_id)
    if not charts:
        pytest.skip("this session's chart draws no compaction threshold")
    for figure in charts:
        spots = [(float(getattr(n, "y", 0) or 0), float(getattr(n, "x", 0) or 0))
                 for n in (figure.layout.annotations or [])
                 if "compact at" in str(getattr(n, "text", "") or "")]
        assert len(spots) == len(set(spots)), (
            "two threshold labels are drawn at the same place, so they print over each other: "
            + repr(sorted(spots)))
        labels = threshold_labels(figure)
        assert len(labels) == len(set(labels)), repr(sorted(labels))


def test_every_segment_still_gets_its_line(app, pane, session_id, has_store):
    """The fix must not collapse the LINES, which are what say where each threshold applied.

    Removing a line per repeated label would still pass the check above, and would lose the one
    thing drawing per segment is for.
    """
    charts = charts_with_thresholds(pane, session_id)
    if not charts:
        pytest.skip("this session's chart draws no compaction threshold")
    for figure in charts:
        assert len(threshold_lines(figure)) >= len(threshold_labels(figure)), (
            "there are fewer threshold lines than labels, so a segment lost its line")

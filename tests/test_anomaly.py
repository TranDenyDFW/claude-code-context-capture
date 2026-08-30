"""The band that says "this call was unlike the rest of this session".

An anomaly detector is the easiest thing in a dashboard to ship broken and hard to notice: it draws
a shaded area, it marks some points, and whether those points mean anything is invisible from the
page. So the statistic is checked against a series written here, the rate is checked against the
store, and the two claims the chart now carries are checked against each other, because a band and
a published threshold line look identical and mean opposite things.
"""
import numpy as np
import pandas as pd
import pytest

from c4x.cli import extract
from c4x.tabs.session import (
    ANOMALY_MARKS,
    ANOMALY_MIN,
    ANOMALY_SIGMA,
    ANOMALY_WINDOW,
    rolling_band,
    session_view,
)


# --- the statistic ---------------------------------------------------------
def test_a_series_too_short_gets_no_band_at_all():
    """Rather than a band computed from four points and read as a measurement."""
    for length in range(0, ANOMALY_MIN):
        assert rolling_band(pd.Series(range(length))) is None
    assert rolling_band(pd.Series(range(ANOMALY_MIN))) is not None
    assert rolling_band(None) is None


def test_the_window_is_trailing_so_no_point_is_judged_against_its_future():
    """A centred window would judge each call partly by calls that had not happened yet.

    Not a bug a reader could see: it makes the detector marginally better on paper and impossible
    to reproduce live, which is the wrong trade for a figure this app asks anyone to act on.

    Checked on the STATISTIC, not on the outlier flags. The first version of this test asserted
    that a point just before a spike was not flagged, and a centred window passes that: the spike
    lands in that point's window, inflating the deviation, so the quiet point stays comfortably
    inside a band that is wrong for the opposite reason. Comparing the reported mean against a
    mean computed here from the preceding values only has no such escape.
    """
    series = pd.Series([10.0] * 60 + [1000.0] * 60)
    mean, _upper, _lower, _outside = rolling_band(series)
    at = 59                                     # the last quiet call, with the step just after it
    trailing = series.iloc[max(0, at - ANOMALY_WINDOW + 1):at + 1].mean()
    assert mean.iloc[at] == pytest.approx(trailing), (
        f"the mean at call {at} is {mean.iloc[at]:.1f}, not the {trailing:.1f} of the calls "
        f"before it: the window is reading ahead")
    assert mean.iloc[at] == pytest.approx(10.0), "the step after it moved the mean before it"


def test_a_flat_series_has_no_anomalies():
    _mean, _upper, _lower, outside = rolling_band(pd.Series([500.0] * 200))
    assert not outside.any(), "a series with no variation produced anomalies"


def test_a_spike_in_an_otherwise_quiet_series_is_one():
    rng = np.random.default_rng(7)
    series = pd.Series(list(rng.normal(1000, 40, 300)))
    series.iloc[250] = 9000.0
    _mean, _upper, _lower, outside = rolling_band(series)
    assert bool(outside.iloc[250])
    assert int(outside.sum()) < 15, f"{int(outside.sum())} anomalies in a series with one spike"


def test_the_lower_edge_never_goes_below_zero():
    """A negative number of tokens is not a lower bound anyone can be under, and drawing one puts
    the ribbon's floor off the bottom of a chart whose y axis starts at zero."""
    rng = np.random.default_rng(3)
    _mean, _upper, lower, _outside = rolling_band(pd.Series(list(rng.normal(50, 400, 200))))
    assert (lower.dropna() >= 0).all()


def test_the_settings_are_the_ones_the_measurement_chose():
    """The plan asked for 20 calls at 2 sigma. Measured against this store that flags 15.6% of the
    calls in its largest session, which is a description of the series rather than of anything
    unusual in it. Pinned so a later widening or narrowing is a deliberate change."""
    assert (ANOMALY_WINDOW, ANOMALY_SIGMA) == (50, 3.0)
    assert ANOMALY_MIN < ANOMALY_WINDOW


def test_the_detector_flags_a_small_share_of_a_real_session(q, has_store):
    """The rate is the whole question. A detector firing on 15% of calls is not one.

    Checked against the store rather than against a synthetic series, because the reason 2 sigma
    was rejected is a property of real cache-read data: it is bursty and heavily skewed, and no
    generated normal series would have shown that.
    """
    from c4x.store import session_turns
    busiest = q("""SELECT session_id FROM turns GROUP BY session_id
                    ORDER BY COUNT(*) DESC LIMIT 1""").iloc[0]["session_id"]
    series = session_turns(busiest, False)["cache_read_input_tokens"].fillna(0)
    if len(series) < 200:
        pytest.skip("no session long enough to measure a rate on")
    _mean, _upper, _lower, outside = rolling_band(series)
    rate = float(outside.sum()) / len(series) * 100
    assert rate < 5, f"the detector flags {rate:.1f}% of calls, which is texture rather than news"


# --- what reaches the page -------------------------------------------------
@pytest.fixture(scope="module")
def drawn(q, has_store):
    """A session with MORE anomalies than the marker cap, so the cap is actually exercised.

    The obvious choice, the session with the most rows, has 690 on the main thread and 11 calls
    outside its band: below the cap, so `min(total, cap)` and `total` are the same number there
    and a test using it cannot tell the two apart. Replanting the defect that reports the cap as
    the count passed against that session, which is how this fixture came to select on the
    property under test rather than on size.
    """
    from c4x.store import session_turns
    from c4x.tabs.session import rolling_band
    candidates = q("""SELECT session_id FROM turns GROUP BY session_id
                       HAVING COUNT(*) > 500 ORDER BY COUNT(*) DESC LIMIT 12""")
    best, most = None, -1
    for session_id in candidates["session_id"]:
        result = rolling_band(session_turns(session_id, False)["cache_read_input_tokens"].fillna(0))
        if result is None:
            continue
        count = int(result[3].sum())
        if count > most:
            best, most = session_id, count
    if best is None:
        pytest.fail("no session in this store is long enough to draw a band")
    figure, cards = session_view(best, "main")
    return extract.describe_figure(figure), " ".join(extract.texts(cards)), most


def test_the_band_is_drawn_behind_the_lines(drawn):
    """Plotly draws in the order traces are added, so a filled ribbon added last covers the series
    it describes. The band's two traces must come before resident and cache read."""
    names = [t["name"] for t in drawn[0]["traces"]]
    assert "resident" in names and "cache read" in names
    band = next(i for i, n in enumerate(names) if n and n.startswith("usual range"))
    assert band < names.index("resident")
    assert band < names.index("cache read")


def test_the_marks_are_capped_and_the_count_is_not(drawn):
    """A chart with 1,018 markers on it has no markers on it. The cap must never be reported as
    the number of anomalies, which is the way a cap quietly becomes a finding."""
    figure, note, outside = drawn
    if outside <= ANOMALY_MARKS:
        pytest.fail(f"the chosen session has only {outside} anomalies, at or below the cap of "
                    f"{ANOMALY_MARKS}, so this test cannot tell the cap from the count")
    marks = [t for t in figure["traces"] if t["name"] and t["name"].startswith("outside it")]
    assert marks, "calls fell outside the band and none were marked"
    assert marks[0]["points"] <= ANOMALY_MARKS, "the cap is not applied to what is drawn"
    stated = int(marks[0]["name"].split("(")[1].rstrip(")").replace(",", ""))
    assert stated == outside, (
        f"the legend says {stated:,} outside the band when {outside:,} were: the cap has been "
        f"reported as the count")
    assert f"{outside:,} of " in note, "the note does not state the real number outside the band"
    assert "furthest outside are circled" in note, "the cap is not disclosed"


def test_the_note_states_the_window_and_the_sigma(drawn):
    """A shaded area with no stated method is something the reader has to guess at, and the guess
    available on this chart is the wrong one."""
    note = drawn[1]
    assert f"{ANOMALY_WINDOW}-call mean" in note
    assert f"{ANOMALY_SIGMA:g} standard deviations" in note


def test_the_page_separates_this_claim_from_the_threshold_lines(drawn):
    """The dashed lines mark PUBLISHED model limits: a fact about the model. The band is this
    session's own spread: a fact about the session. They look alike on a chart and mean opposite
    things, so the page has to say which is which."""
    note = drawn[1]
    assert "published model" in note
    assert "well under every limit" in note


def test_a_session_too_short_for_a_band_says_why(q, has_store):
    short = q("""SELECT session_id FROM turns GROUP BY session_id
                  HAVING COUNT(*) < ? ORDER BY COUNT(*) DESC LIMIT 1""", (ANOMALY_MIN,))
    if short.empty:
        pytest.skip("every session in this store is long enough for a band")
    _figure, cards = session_view(short.iloc[0]["session_id"], "main")
    assert "no anomaly band is drawn" in " ".join(extract.texts(cards))

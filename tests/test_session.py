"""The Session tab: the chart's geometry and the A/B diff, against SQL written here.

The audit proves this tab's tables are reachable and numeric. It says nothing about whether a band
sits at the right height or whether the headroom sentence states the real headroom, and both of
those are numbers even though neither is in a table.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def turns(app, session_id, has_store):
    return app.session_turns(session_id)


@pytest.fixture(scope="module")
def windows(store, session_id):
    """The context window in force for each model segment of this session."""
    segments = store.segments_for(session_id).get("segments", [])
    resolved = [s["window"] for s in segments if s.get("window")]
    if not resolved:
        pytest.fail("no segment of this session resolved a window, so the bands and the budget "
                    "line could not be checked, which is a failure rather than a pass")
    return resolved


def test_bands_are_the_published_thresholds(app, session_id, turns, windows):
    """One warn/compact/blocked band per resolved segment, at the mirror's own numbers.

    A band drawn at the wrong height is a wrong number that no table would ever show.
    """
    fig, _ = app.session_view(session_id, "main", 80, (1, len(turns)), with_cards=False)
    drawn = sorted((s.y0, s.y1) for s in fig.layout.shapes if s.type == "rect")
    expected = []
    for window in windows:
        t = app.THRESHOLDS[window]
        expected += [(t["warn"], t["compact"]), (t["compact"], t["blocked"]),
                     (t["blocked"], window)]
    assert drawn == sorted(expected)


def test_the_budget_line_is_that_share_of_the_window(app, session_id, turns, windows):
    """Horizontal dotted lines in the budget colour, at budget% of each window.

    Horizontal specifically: the A and B marks are dotted too, and being vertical they carry y0=0,
    which an earlier version of this check read as a budget target of zero.
    """
    budget = 80
    fig, _ = app.session_view(session_id, "main", budget, (1, len(turns)), with_cards=False)
    dotted = [s for s in fig.layout.shapes
              if s.type == "line" and getattr(s.line, "dash", None) == "dot"
              and s.y0 == s.y1 and getattr(s.line, "color", None) == app.GOOD]
    drawn = sorted({round(s.y0) for s in dotted})
    assert drawn == sorted({round(w * budget / 100) for w in windows})


def test_the_stated_headroom_is_the_real_headroom(app, session_id, turns, windows):
    budget = 80
    fig, _ = app.session_view(session_id, "main", budget, (1, len(turns)), with_cards=False)
    latest = int(turns["total_resident"].iloc[-1])
    note = next((a.text for a in fig.layout.annotations if "budget" in str(a.text)), "")
    real = windows[-1] * budget / 100.0 - latest
    assert app.fmt_tokens(abs(real)) in note, f"{note!r} against {app.fmt_tokens(abs(real))}"


def test_a_and_b_are_marked_where_asked(app, session_id, turns):
    n = len(turns)
    if n < 6:
        pytest.fail("session too short to place two distinct marks")
    fig, _ = app.session_view(session_id, "main", None, (3, n - 2), with_cards=False)
    labels = {a.text: a.x for a in fig.layout.annotations if a.text in ("A", "B")}
    assert labels.get("A") == 3
    assert labels.get("B") == n - 2


def test_diff_spend_matches_independent_sql(app, session_id, turns, q):
    import c4x.panels as panels
    a, b = 2, min(len(turns), 40)
    ts_a, ts_b = str(turns["ts"].iloc[a - 1]), str(turns["ts"].iloc[b - 1])
    spend, _tools, _targets, _said = panels.turn_diff(session_id, "main", ts_a, ts_b)
    mine = q("""SELECT COUNT(*) AS calls, COALESCE(SUM(output_tokens),0) AS output,
                       COALESCE(SUM(cache_read_input_tokens),0) AS cache_read
                  FROM api_calls
                 WHERE session_id = ? AND ts > ? AND ts <= ? AND is_sidechain = 0""",
             (session_id, ts_a, ts_b)).iloc[0]
    row = spend.iloc[0]
    assert int(row["calls"]) == int(mine["calls"])
    assert int(row["output"]) == int(mine["output"])
    assert int(row["cache_read"]) == int(mine["cache_read"])


def test_diff_tool_totals_match_independent_sql(session_id, turns, q):
    import c4x.panels as panels
    a, b = 2, min(len(turns), 40)
    ts_a, ts_b = str(turns["ts"].iloc[a - 1]), str(turns["ts"].iloc[b - 1])
    _spend, tools, _targets, _said = panels.turn_diff(session_id, "main", ts_a, ts_b)
    mine = q("""SELECT COUNT(*) AS n, COALESCE(SUM(result_bytes),0) AS b FROM tool_calls
                 WHERE session_id = ? AND ts > ? AND ts <= ? AND is_sidechain = 0""",
             (session_id, ts_a, ts_b)).iloc[0]
    assert int(tools["calls"].sum()) == int(mine["n"])
    assert int(tools["result_bytes"].sum()) == int(mine["b"])


def test_an_empty_range_reports_nothing_rather_than_a_zero(app, session_id, turns):
    """A range containing no calls must say so, not print totals of zero as if measured."""
    panel = str(app.turn_diff_panel(session_id, "main", turns, 1, 1))
    assert "Move the two handles apart" in panel or "no" in panel.lower()


def test_the_delta_carries_its_sign(app, session_id, turns):
    """A falling range must show a negative, not an unsigned magnitude.

    The range is chosen from the peak to the end, so a fall is guaranteed by the high-water mark
    rather than by a fixed pair of turns that might happen to rise.
    """
    peak_at = int(turns["total_resident"].astype(float).idxmax()) + 1
    lo, hi = peak_at, len(turns)
    if lo >= hi:
        pytest.fail("no falling range exists in this session, so this could not be exercised, "
                    "which is a failure rather than a pass")
    drop = (int(turns["total_resident"].iloc[hi - 1] or 0)
            - int(turns["total_resident"].iloc[lo - 1] or 0))
    if drop >= 0:
        pytest.fail("the peak-to-end range does not fall, so this could not be exercised")
    text = str(app.turn_diff_panel(session_id, "main", turns, lo, hi))
    assert "-" + app.fmt_tokens(abs(drop)) in text


def test_the_tab_renders_for_a_session_with_no_compactions(pane, q, has_store):
    """A quiet session must not fall through a branch written for a compacted one."""
    quiet = q("""SELECT t.session_id FROM turns t
                  LEFT JOIN compactions c ON c.session_id = t.session_id
                 WHERE c.session_id IS NULL
                 GROUP BY t.session_id HAVING COUNT(*) >= 5 LIMIT 1""")
    if quiet.empty:
        pytest.fail("every session in this store has compacted, so this could not be exercised")
    body = pane("tab-session", session=quiet.iloc[0]["session_id"])
    text = "\n".join(extract.texts(body))
    assert "could not be rendered" not in text
    assert extract.figures(body), "no chart rendered for a session with no compactions"


def test_no_session_selected_says_so_rather_than_rendering_an_empty_chart(pane, has_store):
    body = pane("tab-session", session=None)
    assert "No session selected" in "\n".join(extract.texts(body))
    assert not extract.figures(body), "a chart was drawn with nothing selected"

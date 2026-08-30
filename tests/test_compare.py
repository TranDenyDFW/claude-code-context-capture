"""The Compare tab: every metric recomputed from SQL, and the arithmetic between the arms.

Compare is the tab most able to mislead, because a reader takes a ratio at face value. Two things
are therefore checked separately: that each arm's raw number is right, and that the ratio, verdict
and basis derived from those numbers are right.

The most important line here is API calls. It MUST come from the api_calls view, which takes one
row per request id, and not from the turns table, which counts a streamed message two to eight
times. The README calls that the easiest mistake in this codebase, and this is the tab where making
it would be least visible.
"""
import pytest

from c4x.cli import commands, extract


def metrics_of(payload):
    """The comparison table, keyed by metric name."""
    tables = payload["tables"]
    assert tables, "Compare rendered no table"
    return {row["metric"]: row for row in tables[0]["rows"]}


@pytest.fixture(scope="module")
def two_sessions(session_id, other_session_id):
    if other_session_id is None:
        pytest.fail("the store holds only one session, so Compare could not be exercised, "
                    "which is a failure rather than a pass")
    return session_id, other_session_id


@pytest.fixture(scope="module")
def compared(two_sessions, has_store):
    a, b = two_sessions
    return metrics_of(commands.render_compare("session", b, a))


def arm_truth(q, session_id):
    """Every figure for one arm, from SQL written here.

    Main thread only, matching the scope the tab reports, and off api_calls rather than turns.
    """
    calls = q("""SELECT COUNT(*) AS n,
                        SUM(cache_read_input_tokens) AS cache_read,
                        MAX(total_resident) AS peak,
                        AVG(total_resident) AS mean_resident,
                        SUM(output_tokens) AS output,
                        SUM(thinking_tokens) AS thinking
                   FROM api_calls
                  WHERE session_id = ? AND COALESCE(is_sidechain,0) = 0""", (session_id,)).iloc[0]
    comps = q("SELECT COUNT(*) AS n FROM compactions WHERE session_id = ?",
              (session_id,)).iloc[0]["n"]
    tools = q("""SELECT COUNT(*) AS n, SUM(COALESCE(result_bytes,0)) AS result_bytes
                   FROM tool_calls WHERE session_id = ? AND COALESCE(is_sidechain,0) = 0""",
              (session_id,)).iloc[0]
    return {
        "API calls": int(calls["n"] or 0),
        "cache re-reads": int(calls["cache_read"] or 0),
        "peak resident": int(calls["peak"] or 0),
        "output tokens": int(calls["output"] or 0),
        "thinking tokens": int(calls["thinking"] or 0),
        "compactions": int(comps or 0),
        "tool calls": int(tools["n"] or 0),
        "tool result bytes": int(tools["result_bytes"] or 0),
        "_mean_resident": float(calls["mean_resident"] or 0),
    }


def test_arm_a_matches_sql_written_here(compared, two_sessions, q):
    a, _b = two_sessions
    truth = arm_truth(q, a)
    for metric, expected in truth.items():
        if metric.startswith("_"):
            continue
        assert metric in compared, f"Compare does not show {metric}"
        assert float(compared[metric]["A"]) == pytest.approx(expected, rel=0, abs=0.51), (
            f"arm A {metric}: page {compared[metric]['A']} vs sql {expected}")


def test_arm_b_matches_sql_written_here(compared, two_sessions, q):
    _a, b = two_sessions
    truth = arm_truth(q, b)
    for metric, expected in truth.items():
        if metric.startswith("_"):
            continue
        assert float(compared[metric]["B"]) == pytest.approx(expected, rel=0, abs=0.51), (
            f"arm B {metric}: page {compared[metric]['B']} vs sql {expected}")


def test_api_calls_is_deduped_and_is_not_the_turn_count(compared, two_sessions, q):
    """The invariant the whole api_calls view exists for.

    If this row were ever built from `turns`, a session whose assistant messages streamed would
    report several times its real call count, and nothing else on the page would contradict it.
    """
    a, _b = two_sessions
    deduped = int(q("""SELECT COUNT(*) AS n FROM api_calls
                        WHERE session_id = ? AND COALESCE(is_sidechain,0) = 0""",
                    (a,)).iloc[0]["n"])
    raw_rows = int(q("""SELECT COUNT(*) AS n FROM turns
                         WHERE session_id = ? AND COALESCE(is_sidechain,0) = 0""",
                     (a,)).iloc[0]["n"])
    assert int(compared["API calls"]["A"]) == deduped
    if raw_rows == deduped:
        pytest.fail("this session has no streamed messages, so the dedup could not be "
                    "distinguished from a plain row count, which is a failure rather than a pass")
    assert int(compared["API calls"]["A"]) != raw_rows


def test_the_ratio_is_b_over_a(compared):
    """Every ratio is recomputed from the two numbers printed beside it."""
    for metric, row in compared.items():
        a, b, shown = float(row["A"]), float(row["B"]), row.get("B vs A")
        if shown in (None, "") or a == 0:
            continue
        assert float(shown) == pytest.approx(b / a, abs=0.006), (
            f"{metric}: shows {shown}, but {b} / {a} = {b / a:.4f}")


def test_the_rebilled_multiple_is_cache_reads_over_peak(compared):
    """A derived row, checked against the two rows it derives from rather than against itself."""
    for arm in ("A", "B"):
        peak = float(compared["peak resident"][arm])
        reads = float(compared["cache re-reads"][arm])
        if not peak:
            continue
        assert float(compared["re-billed, as a multiple of peak"][arm]) == pytest.approx(
            reads / peak, rel=0.001)


def test_every_metric_declares_whether_it_scales_with_population(compared):
    """A total and a per-unit figure cannot be read the same way.

    Comparing one session against a 200-session cohort makes every total larger for the cohort by
    construction, so a row that does not say which kind it is invites a false conclusion.
    """
    for metric, row in compared.items():
        assert row.get("basis"), f"{metric} states no basis"
        assert row["basis"] in ("per unit", "total, scales with population"), row["basis"]


def test_comparing_a_session_with_itself_is_a_flat_one(two_sessions, has_store):
    """The control. Same data both sides: every ratio 1.0, and nothing declared better.

    If the tab ever compared the wrong arm, or applied a scope to one side only, this is where it
    shows, because there is no legitimate way for identical inputs to differ.
    """
    a, _b = two_sessions
    same = metrics_of(commands.render_compare("session", a, a))
    for metric, row in same.items():
        assert str(row["A"]) == str(row["B"]), f"{metric} differs from itself: {row}"
        if row.get("B vs A") not in (None, ""):
            assert float(row["B vs A"]) == pytest.approx(1.0, abs=0.001), metric
        assert not row.get("verdict"), f"{metric} called a winner between identical arms"


def test_a_cohort_arm_states_its_size(two_sessions, cohort, has_store, store):
    """Comparing a session against a cohort must name the cohort's population, not imply one."""
    a, _b = two_sessions
    payload = commands.render_compare("cohort", cohort, a)
    text = "\n".join(payload["text"])
    n = len(store.cohort_sessions(cohort))
    assert f"{n:,} session" in text, f"the cohort arm does not state its {n} sessions: {text[:200]}"


def test_compare_labels_both_arms(two_sessions, has_store):
    a, b = two_sessions
    payload = commands.render_compare("session", b, a)
    text = "\n".join(payload["text"])
    assert "A" in text and "B" in text
    assert extract.tables(payload) is not None

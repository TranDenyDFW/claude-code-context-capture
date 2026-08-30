"""The Mirror tab: the thresholds a window implies, checked against the tool that publishes them.

Every band on the Session chart and every warning elsewhere is derived from these numbers, so a
wrong row here is wrong everywhere at once and nothing downstream would contradict it.
"""
import pytest

from c4x.cli import extract


@pytest.fixture(scope="module")
def body(pane, has_store):
    return pane("tab-mirror")


def test_the_table_lists_a_row_per_window(body, app):
    table = next((t for t in extract.tables(body) if "window" in (t["columns"] or [])), None)
    assert table is not None, "no threshold table on the Mirror tab"
    assert table["rows"], "the Mirror tab rendered an empty table"
    windows = {int(r["window"]) for r in table["rows"] if str(r.get("window", "")).isdigit()}
    known = set(app.THRESHOLDS)
    if windows:
        assert windows <= known, (
            f"the tab shows windows the mirror does not publish: {windows - known}")


def test_thresholds_rise_in_order(body):
    """warn < compact < blocked < window. Any other order would make a band inside-out."""
    table = next((t for t in extract.tables(body) if "window" in (t["columns"] or [])), None)
    for row in table["rows"]:
        values = [row.get(k) for k in ("warn at", "compact at", "blocked at", "window")]
        numeric = [v for v in values if isinstance(v, (int, float))]
        if len(numeric) < 2:
            continue
        assert numeric == sorted(numeric), f"thresholds out of order: {row}"


def test_the_tab_matches_the_thresholds_the_app_uses(body, app):
    """The tab and the chart bands must come from the same source, not two copies of it."""
    table = next((t for t in extract.tables(body) if "window" in (t["columns"] or [])), None)
    checked = 0
    for row in table["rows"]:
        try:
            window = int(row["window"])
        except (TypeError, ValueError, KeyError):
            continue
        if window not in app.THRESHOLDS:
            continue
        published = app.THRESHOLDS[window]
        for label, key in (("warn at", "warn"), ("compact at", "compact"),
                           ("blocked at", "blocked")):
            if isinstance(row.get(label), (int, float)):
                assert int(row[label]) == int(published[key]), f"{window} {label}"
                checked += 1
    assert checked > 0, "no threshold could be compared against the app's own table"


def test_the_mirror_callback_answers_for_a_given_resident(app, has_store):
    """The interactive half: ask it where a given resident figure sits."""
    out = app._mirror(850_000, 1_000_000)
    text = "\n".join(extract.texts(out))
    assert text.strip(), "the mirror returned nothing for a resident of 850k in a 1M window"
